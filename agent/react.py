"""ReAct 多工具循环（M6）：外层调度，手写 LangGraph 实现。

图结构：

  START → react_agent ──(无 tool_calls)→ END
              │ 有 tool_calls
              ▼
         tool_execute ──→ react_agent（循环，步数上限 MAX_REACT_STEPS）

职责分层（为什么不做成一个大图而是"外环 + 内链"两层）：
  外层（本文件）：LLM 看对话上下文决定"现在该查什么 / 该收尾了"，
    管理消息列表与循环步数，本身不碰 SQL
  内层（graph.py 四节点链，被 query_sql 工具复用）：安全地把自然
    语言变成 SQL 并执行，AST 校验/查询护栏/指标字典一条不少。
    这样 ReAct 的"自由调度"永远落在安全链路的后面，而不是绕开它。

手写而非 langgraph.prebuilt.create_react_agent 的原因：
  1. 节点内可以加日志/步骤上限（致死循环兜底）
  2. 工具执行走我们自己的 dispatch（错误当结果回喂，不崩循环）
  3. 结构透明，方便学习与单测注入（fake chat 函数即可驱动全流程）
"""
from __future__ import annotations

import time

from langgraph.graph import END, START, StateGraph

import config
from agent.llm import ReActChatModel, SqlGenerator
from agent.skill_loader import (
    Skill,
    filter_exposed_tools,
    list_skill_catalog,
    render_skill_prompt,
)
from agent.tools import TOOL_SCHEMAS, build_tool_registry, execute_tool_call
from data.metrics import Metric
from data.service import DataService
from logging_setup import get_logger

logger = get_logger("agent.react")

# 外层调度者的系统提示：强调"用工具、以工具结果为准、没表先查表"
_SYSTEM_PROMPT = """你是数据分析助手。你有以下工具可用：
- query_sql(question)：对已加载数据表执行自然语言查询，返回统计结果文本
- list_tables()：列出当前已加载的全部数据表名
- plot_chart(chart_type, title)：对最近一次 query_sql 的结果画图表
  （chart_type 可选 bar/line/pie/scatter），图表会直接展示给用户
- load_skill(name)：加载一个分析技能（如指标口径定义），加载后按该
  技能的指引执行，可用工具集将按技能声明的 allowedTools 收窄

行为规则：
1. 回答需要数据的问题前必须先调用工具，禁止凭空编造数字。
2. 不确定有哪些表时，先调 list_tables。
3. 工具结果就是事实依据，最终回答必须基于工具返回的内容。
4. 用户想看图形、对比、趋势时，先用 query_sql 查出分组数据，
   再调 plot_chart 画图，最后给出简短文字结论。
5. 拿到足够信息后直接给出简洁的中文回答，不要再调用工具。
6. 最终回答中如果用表格展示数据，使用 markdown 表格。
7. 若下方附有「可用技能」目录，涉及对应领域问题时先调 load_skill
   加载对应技能，再拆解子问题。指标类问题加载后子问题只写指标名
   （如"查询 GMV"），不要自行指定表名——口径由技能定义统一决定。"""


def _decision_text(resp: dict) -> str:
    """把本轮 LLM 决策转成一句给用户看的话（前端「思考」行内容）。

    只在"这一轮决定调工具"时才产出文本：
      - 有工具调用 + 模型有文本 → 用模型文本（reasoner 会走到这条）
      - 有工具调用 + 无文本 → "决定调用 X、Y"。deepseek-chat 的常态
        （实测 content_chars=0），这是能拿到的**真实决策信息**
      - 无工具调用 → 空串：这一轮直接产出最终答案，答案本身会显示成
        答案气泡，再补一行"思考"就是重复
    """
    calls = resp.get("tool_calls") or []
    if not calls:
        return ""
    content = (resp.get("content") or "").strip()
    if content:
        return content
    names: list[str] = []
    for tc in calls:
        n = tc["function"]["name"]
        if n not in names:
            names.append(n)
    return "决定调用 " + "、".join(names)


def build_react_graph(
    service: DataService,
    sql_llm: SqlGenerator,
    chat_llm: ReActChatModel,
    metric_registry: list[Metric] | None = None,
    skills: dict[str, Skill] | None = None,
) -> StateGraph:
    """构建 ReAct 循环图。参数全部可注入（生产传真实实现，测试传 fake）。

    M8 新增参数：
      metric_registry——内层口径匹配的数据源（透传给 query_sql 工具）
      skills——已解析的技能包：供 load_skill 校验、工具集收窄与全文
        注入。active_skill / loaded_skills 两个键必须在两个节点里都
        显式写回，漏带会在 LangGraph 全量替换语义下被抹掉（已踩坑）。
    """
    registry = build_tool_registry(service, sql_llm, metric_registry, skills)

    def current_exposed_tools(state: dict) -> list[dict]:
        """按当前激活技能的 allowedTools 收窄本轮暴露的工具集。

        未激活技能时返回全集——这是"未激活=不限制"语义的实现点：
        只有 load_skill 成功激活后，幻想工具才被移出视野。
        """
        active = state.get("active_skill")
        if active and skills:
            skill = skills.get(active)
            if skill:
                return filter_exposed_tools(TOOL_SCHEMAS, skill.allowed_tools)
        return filter_exposed_tools(TOOL_SCHEMAS, None)

    # ── 节点1：LLM 决策（调工具 or 最终回答）──────────────────────
    def react_agent(state: dict) -> dict:
        steps = state.get("steps", 0) + 1
        if steps > config.MAX_REACT_STEPS:
            # 兜底：LLM 反复调工具/字数超限也不会无限循环
            logger.warning("react step limit reached steps=%d", steps)
            return {
                "steps": steps,
                "final_answer": "分析步骤过多被终止，请换一个更具体的问题重新提问。",
                # M8：技能状态原样带过（全量替换语义）
                "active_skill": state.get("active_skill"),
                "loaded_skills": state.get("loaded_skills", []),
            }

        t0 = time.monotonic()
        # chat_llm 异常（超时/网络）向上一层抛：server 层统一兜底，
        # 不做静默降级——外层没有"重试给 SQL"的语义闭环
        # M8：tools 传收窄集（未激活技能时 = 全集），技能激活后
        # LLM 的视野随之收窄，减少被诱导调无关工具的概率
        resp = chat_llm(state.get("messages", []), current_exposed_tools(state))
        cost = (time.monotonic() - t0) * 1000

        # assistant 消息带着 tool_calls 追加进消息列表（openai 格式：
        # 多轮 function calling 要求 assistant.tool_calls 与 tool 消息
        # 通过 tool_call_id 一一对应，缺 Id 则 API 报错）
        assistant_msg: dict = {"role": "assistant", "content": resp.get("content", "") or ""}
        if resp.get("tool_calls"):
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                for tc in resp["tool_calls"]
            ]

        logger.info(
            "react step=%d llm_ms=%.1f tool_calls=%d",
            steps, cost, len(assistant_msg.get("tool_calls", [])),
        )
        return {
            "steps": steps,
            "last_response": resp,
            "assistant_msg": assistant_msg,
            "messages": state.get("messages", []) + [assistant_msg],
            "charts": state.get("charts", []),  # 全量替换语义下原样携带
            "tool_events": state.get("tool_events", []),  # 同上：漏带会被整轮清空
            # 本轮真实决策文本：server 拿它当 node 事件的 message 推给
            # 前端显示（替代原来"正在分析决策…"那种空泛占位文案）
            "step_text": _decision_text(resp),
            "final_answer": None if resp.get("tool_calls") else (resp.get("content") or ""),
            # M8：技能状态必须显式携带（漏带即丢，收窄/注入随即失效）
            "active_skill": state.get("active_skill"),
            "loaded_skills": state.get("loaded_skills", []),
        }

    # ── 节点2：执行全部工具调用，结果作为 tool 消息回填 ────────────
    def tool_execute(state: dict) -> dict:
        # 注意（LangGraph 1.x 语义坑）：StateGraph(dict) 的节点返回值
        # 是"全量替换"而非浅合并（0.x 是合并）。因此这里必须把上一
        # 节点产出的 steps/last_response/final_answer 原样带回来，
        # 否则每轮 steps 归零、步数护栏永不触发（已踩坑）
        tool_msgs = []
        new_charts = []
        new_tool_events = []
        activated_skill = None  # M8：本轮 load_skill 成功激活的技能名
        for tc in state.get("last_response", {}).get("tool_calls", []):
            name = tc["function"]["name"]
            args_str = tc["function"].get("arguments", "{}")
            t0 = time.monotonic()
            out = execute_tool_call(name, args_str, registry)
            if out.get("skill"):
                # 一轮多个 tool_calls 时以最后一个成功加载为准
                activated_skill = out["skill"]
            cost_ms = (time.monotonic() - t0) * 1000
            # args 一并打日志：这是排查"指标口径漂移"的关键证据——
            # 外层 LLM 会把用户原问题改写成子问题再交给 query_sql，
            # 改写后若丢了指标别名，内层口径字典就匹配不上（实测遇到过）
            logger.info(
                "react tool=%s ms=%.1f out_chars=%d args=%.160s",
                name, cost_ms, len(out["text"]), args_str,
            )
            if out.get("chart"):
                # plot_chart 产物：进 state.charts，由 server 转成 SSE
                # chart 事件推给前端渲染
                new_charts.append(out["chart"])
            # 工具调用明细：给前端渲染成"Agent 在做什么"的步骤卡片。
            # 这才是真正有信息量的"思考过程"——实测 deepseek-chat 在
            # 决定调工具时 content 几乎为空（日志里 content_chars 多为 0），
            # 模型没有"内心独白"可展示，但它的动作（用了哪个工具、
            # 传了什么参数、拿到什么结果）是实打实的
            new_tool_events.append({
                "name": name,
                # 参数是 JSON 字符串（内含子问题原文），截断避免超长刷屏
                "arguments": args_str[:300],
                "ok": not out["text"].startswith(("错误", "工具执行失败")),
                "result": out["text"][:300],
            })
            tool_msgs.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),  # 必须与 assistant.tool_calls 的 id 对应
                "name": name,
                "content": out["text"],
            })
        new_active = state.get("active_skill")
        loaded = list(state.get("loaded_skills") or [])
        messages = state.get("messages", []) + tool_msgs
        if activated_skill:
            new_active = activated_skill
            if activated_skill not in loaded:
                # M8 激活注入：技能全文以 system 消息追加进消息列表，
                # 下一轮决策自然读到（零额外 LLM 调用）；重复加载
                # 同一技能不重复注入，防 LLM 反复 load 撑爆上下文
                skill = (skills or {}).get(activated_skill)
                if skill:
                    messages = messages + [
                        {"role": "system", "content": render_skill_prompt(skill)}
                    ]
                loaded.append(activated_skill)
        return {
            "steps": state.get("steps", 0),
            "last_response": state.get("last_response", {}),
            "assistant_msg": state.get("assistant_msg"),
            "charts": state.get("charts", []) + new_charts,
            "tool_events": state.get("tool_events", []) + new_tool_events,
            "final_answer": state.get("final_answer"),
            "tool_messages": tool_msgs,
            "messages": messages,
            # M8：技能状态显式携带（全量替换语义下漏带即丢）
            "active_skill": new_active,
            "loaded_skills": loaded,
        }

    # ── 条件边：有工具调用继续执行，否则结束 ───────────────────────
    def route_after_agent(state: dict) -> str:
        if state.get("last_response", {}).get("tool_calls"):
            return "tool_execute"
        # 无 tool_calls：final_answer（即 content）已在节点里写入，
        # 直接进 END；content 为空串的情况由入口函数兜底
        return END

    builder = StateGraph(dict)
    builder.add_node("react_agent", react_agent)
    builder.add_node("tool_execute", tool_execute)
    builder.add_edge(START, "react_agent")
    builder.add_conditional_edges(
        "react_agent",
        route_after_agent,
        {"tool_execute": "tool_execute", END: END},
    )
    # 显式声明回边：LangGraph 会自动给无出边的节点补 END 边，
    # 漏写这条会导致 tool_execute 被当成死端直接结束（已踩坑）
    builder.add_edge("tool_execute", "react_agent")
    return builder.compile()


def initial_messages(
    question: str,
    history: list[dict] | None = None,
    skill_catalog: str = "",
) -> list[dict]:
    """构造 ReAct 首轮消息列表：system 提示 + 技能目录 + 裁剪后的问答史 + 当前问题。

    server 的 SSE 链路（stream updates 逐节点吐进度）与本模块的
    run_react_analysis 共用同一份构造逻辑，避免两端拼法不一致。
    history 只回放纯问答（user/assistant 文本消息）：上一轮的工具
    调用细节不跨会话携带，上下文保持精炼。

    skill_catalog（M8）：渐进式披露的"目录常驻"部分——一行一个的
    技能名+描述，全文由 load_skill 激活时注入。取代 M4~M7 的全量
    口径清单注入：口径数据本身搬进了 skills/metrics/references，
    外层引导（技能规则）+ 内层锁表（match_metrics）两层防线不变。
    """
    system = _SYSTEM_PROMPT
    if skill_catalog:
        system = system + "\n\n" + skill_catalog
    messages: list[dict] = [{"role": "system", "content": system}]
    for msg in (history or [])[-config.MAX_HISTORY_MESSAGES:]:
        # 只带文本消息；防御脏数据里出现的其它 role（如 tool）
        if msg.get("role") in ("user", "assistant") and msg.get("content"):
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": question})
    return messages


def run_react_analysis(
    service: DataService,
    sql_llm: SqlGenerator,
    chat_llm: ReActChatModel,
    question: str,
    history: list[dict] | None = None,
    metric_registry: list[Metric] | None = None,
    skills: dict[str, Skill] | None = None,
) -> dict:
    """便捷入口：问题 → 最终答案文本 + 完整会话历史（对齐 run_analysis 返回约定）。"""
    graph = build_react_graph(service, sql_llm, chat_llm, metric_registry, skills)
    catalog = list_skill_catalog(skills or {})
    result = graph.invoke(
        {"messages": initial_messages(question, history, catalog), "steps": 0}
    )
    text = result.get("final_answer") or result.get("last_response", {}).get("content") or "（无输出）"

    new_history = list(history or []) + [
        {"role": "user", "content": question},
        {"role": "assistant", "content": text},
    ]
    return {"result_text": text, "history": new_history}