"""ReAct 多工具循环（M6）+ 高危 SQL 人工确认（HITL，Sprint 1）。

图结构（hitl=True）：

  START → react_agent ──(无 tool_calls)→ END
              │ 有 tool_calls
              ▼
          plan_sql（dry-run：生成 + 校验，不执行）
              │
              ▼
          guard（风险判定；高风险 interrupt 等用户确认）
              │
              ▼
         tool_execute（执行"用户确认过的那条 SQL"）
              │
              └──────→ react_agent（循环，步数上限 MAX_REACT_STEPS）

hitl=False 时 plan_sql / guard 不参与路由：react_agent 直接进 tool_execute，
行为和 M6 完全一致（保留旧路径，便于 A/B 对比与回滚）。

职责分层（为什么不做成一个大图而是"外环 + 内链"两层）：
  外层（本文件）：LLM 看对话上下文决定"现在该查什么 / 该收尾了"，
    管理消息列表与循环步数，本身不碰 SQL
  内层（graph.py 四节点链，被 plan_sql / query_sql 复用）：安全地把自然
    语言变成 SQL 并执行，AST 校验/查询护栏/指标字典一条不少。
    这样 ReAct 的"自由调度"永远落在安全链路的后面，而不是绕开它。

手写而非 langgraph.prebuilt.create_react_agent 的原因：
  1. 节点内可以加日志/步骤上限（致死循环兜底）
  2. 工具执行走我们自己的 dispatch（错误当结果回喂，不崩循环）
  3. 需要在"决策"和"执行"之间插一个可中断的审批节点（prebuilt 做不到）
  4. 结构透明，方便学习与单测注入（fake chat 函数即可驱动全流程）

状态用 TypedDict + reducer（Sprint 0）：
  M6 时用 StateGraph(dict)，state 是"全量替换"语义——每个节点必须把
  上一节点的字段原样带回来，漏一个就静默丢弃（历史消息、图表、工具明细
  都被这个坑吃过）。现在 messages/charts/tool_events 声明成
  Annotated[list, operator.add]，节点只返回增量，由框架负责累加。
"""
from __future__ import annotations

import json
import operator
import time
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

import config
from agent.graph import run_plan
from agent.llm import ReActChatModel, SqlGenerator
from agent.skill_loader import (
    Skill,
    filter_exposed_tools,
    list_skill_catalog,
    render_skill_prompt,
)
from agent.tools import TOOL_SCHEMAS, build_tool_registry, execute_tool_call
from data.metrics import Metric
from data.risk import assess_plan
from data.service import DataService
from logging_setup import get_logger

logger = get_logger("agent.react")


class ReactState(TypedDict, total=False):
    """ReAct 图的状态。

    三个 list 通道用 operator.add 累加，节点只返回"这一轮新增的部分"；
    其余标量是 LastValue（后写覆盖先写）。这就是 Sprint 0 换掉
    StateGraph(dict) 的意义：不再靠"每个节点记得原样携带"来保状态。
    """

    messages: Annotated[list[dict], operator.add]
    charts: Annotated[list[str], operator.add]
    tool_events: Annotated[list[dict], operator.add]
    steps: int
    last_response: dict          # 本轮 LLM 原始响应（路由与工具执行都要读）
    step_text: str               # 推给前端「思考」行的真实文案
    final_answer: str | None
    active_skill: str | None     # M8：当前激活的技能（收窄工具集用）
    loaded_skills: list[str]     # 已注入全文的技能（防重复注入）
    plans: dict                  # HITL：tool_call_id -> {sql, error, risk}
    approvals: dict              # HITL：tool_call_id -> approved / rejected
    rejection_note: str          # HITL：用户拒绝时写的原因（回喂给 LLM）


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
   （如"查询 GMV"），不要自行指定表名——口径由技能定义统一决定。
8. 如果你的查询被用户拒绝，按用户给出的原因调整（换口径/加筛选/缩小
   范围）后重新查询，不要原样重试。"""


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
    *,
    checkpointer=None,
    hitl: bool = False,
) -> StateGraph:
    """构建 ReAct 循环图。参数全部可注入（生产传真实实现，测试传 fake）。

    M8 参数：
      metric_registry——内层口径匹配的数据源（透传给 plan_sql / query_sql）
      skills——已解析的技能包：供 load_skill 校验、工具集收窄与全文注入

    Sprint 1 参数：
      hitl——开启"高危 SQL 人工确认"。为 True 时必须在图里插入 plan_sql 与
        guard 两个节点，且必须传 checkpointer（interrupt 依赖检查点来暂停/
        恢复；没有 checkpointer 时 LangGraph 会直接抛错）。
      checkpointer——进程级 MemorySaver（或未来的 SqliteSaver）。同一 thread_id
        下，中断前的 state 存在这里面，恢复时从断点继续。
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
            # 兜底：LLM 反复调工具/字数超限也不会无限循环。
            # 注意必须把 last_response 清空：换成 reducer 语义后，上一轮的
            # last_response 会保留下来，路由读到旧的 tool_calls 就会继续
            # 调工具 → 步数超限 → 再调工具……死循环（已踩坑）
            logger.warning("react step limit reached steps=%d", steps)
            return {
                "steps": steps,
                "last_response": {},
                "final_answer": "分析步骤过多被终止，请换一个更具体的问题重新提问。",
            }

        t0 = time.monotonic()
        # chat_llm 异常（超时/网络）向上一层抛：server 层统一兜底，
        # 不做静默降级——外层没有"重试给 SQL"的语义闭环
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
        # 只返回增量：messages 由 reducer 追加，charts/tool_events 同理
        return {
            "steps": steps,
            "last_response": resp,
            "messages": [assistant_msg],
            # 本轮真实决策文本：server 拿它当 node 事件的 message 推给
            # 前端显示（替代原来"正在分析决策…"那种空泛占位文案）
            "step_text": _decision_text(resp),
            "final_answer": None if resp.get("tool_calls") else (resp.get("content") or ""),
        }

    # ── 节点2：dry-run 生成查询计划（HITL）────────────────────────
    def plan_sql(state: dict) -> dict:
        """把"要执行的 SQL"提前生成出来，但不执行。

        为什么单独一个节点而不是塞进 tool_execute：
          1. interrupt 之后的节点在恢复时会被**从头重跑**（实测），
             LLM 调用必须放在 interrupt 之前，否则确认一次要花两次钱；
          2. 计划先落进 state，审批和执行读的是同一份数据，
             "用户确认的 SQL" 与 "实际执行的 SQL" 才可能严格相等。

        每轮都必须重写 plans（哪怕是空 dict）：否则上一轮的旧计划会残留，
        guard 会对一个已经处理过的计划再弹一次确认。
        """
        if not hitl:
            return {}
        plans: dict[str, dict] = {}
        for tc in state.get("last_response", {}).get("tool_calls", []):
            if tc["function"]["name"] != "query_sql":
                continue
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            question = str(args.get("question") or "")
            t0 = time.monotonic()
            plan = run_plan(
                service, sql_llm, question, metric_registry=metric_registry
            )
            risk = None
            if plan["sql"]:
                risk = assess_plan(
                    service, plan["sql"], question, metric_registry
                )
            plans[tc["id"]] = {
                "question": question,
                "sql": plan["sql"],
                "error": plan["error"],
                "risk": risk.to_dict() if risk else None,
            }
            logger.info(
                "hitl plan call_id=%s sql_chars=%d needs_approval=%s ms=%.1f",
                tc["id"], len(plan["sql"]), bool(risk and risk.needs_approval),
                (time.monotonic() - t0) * 1000,
            )

        if not plans:
            # 本轮没有 query_sql（如 list_tables）：清空计划后直接往下走
            return {"plans": {}, "approvals": {}, "rejection_note": ""}

        first = next(iter(plans.values()))
        if first["error"]:
            text = f"SQL 生成失败：{first['error'][:120]}"
        else:
            rows = (first.get("risk") or {}).get("estimated_rows") or 0
            text = f"已生成查询计划（预估扫描 {rows:,} 行）"
        return {
            "plans": plans,
            "approvals": {},
            "rejection_note": "",
            "step_text": text,
        }

    # ── 节点3：风险判定 + 必要时中断等确认（HITL）─────────────────
    def guard(state: dict) -> dict:
        """纯函数：只看 state，不调 LLM、不查库。

        恢复时本节点会被重跑，所以这里任何副作用都会被放大成"做两次"。
        """
        if not hitl:
            return {}
        plans = state.get("plans") or {}
        risky = {
            cid: p for cid, p in plans.items()
            if p.get("risk") and p["risk"]["reasons"]
        }
        if not risky:
            if plans:
                return {"step_text": "安全检查通过，开始执行"}
            return {}

        payload = {
            "summary": "这条查询可能很重或口径可疑，执行前请你确认",
            "queries": [
                {
                    "sql": p["sql"],
                    "reasons": p["risk"]["reasons"],
                    "estimated_rows": p["risk"]["estimated_rows"],
                    "metric": p["risk"]["metric"],
                }
                for p in risky.values()
            ],
        }
        decision = interrupt(payload)
        if isinstance(decision, dict):
            approve = bool(decision.get("approve"))
            note = str(decision.get("note") or "")
        else:
            approve, note = bool(decision), ""
        logger.info("hitl decision approve=%s note_chars=%d", approve, len(note))

        if approve:
            return {
                "approvals": {cid: "approved" for cid in risky},
                "step_text": "用户已确认，开始执行",
            }
        return {
            "approvals": {cid: "rejected" for cid in risky},
            "rejection_note": note,
            "step_text": "用户拒绝执行该查询，正在调整",
        }

    # ── 节点4：执行全部工具调用，结果作为 tool 消息回填 ────────────
    def tool_execute(state: dict) -> dict:
        plans = state.get("plans") or {}
        approvals = state.get("approvals") or {}
        note = state.get("rejection_note") or ""

        tool_msgs = []
        new_charts = []
        new_tool_events = []
        activated_skill = None  # M8：本轮 load_skill 成功激活的技能名
        for tc in state.get("last_response", {}).get("tool_calls", []):
            name = tc["function"]["name"]
            args_str = tc["function"].get("arguments", "{}")
            call_id = tc.get("id", "")
            plan = plans.get(call_id)
            rejected = bool(plan and approvals.get(call_id) == "rejected")
            t0 = time.monotonic()
            if rejected:
                # 用户拒绝：不执行，把"拒绝 + 原因"当工具结果回喂，
                # 让外层 LLM 带着反馈改写（这才是人在回路的闭环）
                out = {
                    "text": f"用户拒绝执行该查询：{note or '未说明原因'}。"
                            "请按用户意见调整口径或缩小范围后重试。",
                    "chart": None,
                    "skill": None,
                }
            else:
                out = execute_tool_call(name, args_str, registry, plan=plan)
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
                # 被用户拒绝也算"没成功"：前端据此标红，否则会显示成一次
                # 成功查询（拒绝文本不以"错误"开头，不能只靠文本前缀判断）
                "ok": (not rejected)
                      and not out["text"].startswith(("错误", "工具执行失败")),
                "result": out["text"][:300],
            })
            tool_msgs.append({
                "role": "tool",
                "tool_call_id": call_id,  # 必须与 assistant.tool_calls 的 id 对应
                "name": name,
                "content": out["text"],
            })

        new_messages = list(tool_msgs)
        new_active = state.get("active_skill")
        loaded = list(state.get("loaded_skills") or [])
        if activated_skill:
            new_active = activated_skill
            if activated_skill not in loaded:
                # M8 激活注入：技能全文以 system 消息追加进消息列表，
                # 下一轮决策自然读到（零额外 LLM 调用）；重复加载
                # 同一技能不重复注入，防 LLM 反复 load 撑爆上下文
                skill = (skills or {}).get(activated_skill)
                if skill:
                    new_messages.append(
                        {"role": "system", "content": render_skill_prompt(skill)}
                    )
                loaded.append(activated_skill)
        return {
            "charts": new_charts,
            "tool_events": new_tool_events,
            "messages": new_messages,
            "active_skill": new_active,
            "loaded_skills": loaded,
            # tool_execute 没有独立文案：它的内容由 tool_call 卡片承载
            "step_text": "",
        }

    # ── 条件边 ────────────────────────────────────────────────────
    def route_after_agent(state: dict) -> str:
        if state.get("last_response", {}).get("tool_calls"):
            # hitl 关闭时跳过 plan/guard，行为与 M6 完全一致
            return "plan_sql" if hitl else "tool_execute"
        # 无 tool_calls：final_answer（即 content）已在节点里写入，
        # 直接进 END；content 为空串的情况由入口函数兜底
        return END

    builder = StateGraph(ReactState)
    builder.add_node("react_agent", react_agent)
    builder.add_node("tool_execute", tool_execute)
    builder.add_edge(START, "react_agent")
    # 分支映射必须和实际加进去的节点一致：LangGraph 会校验每个分支目标
    # 是否存在（即使我们运行时永远不会走到），关掉 HITL 时不能声明 plan_sql
    branches = {"tool_execute": "tool_execute", END: END}
    if hitl:
        branches["plan_sql"] = "plan_sql"
    builder.add_conditional_edges("react_agent", route_after_agent, branches)
    # 显式声明回边：LangGraph 会自动给无出边的节点补 END 边，
    # 漏写这条会导致 tool_execute 被当成死端直接结束（已踩坑）
    builder.add_edge("tool_execute", "react_agent")

    if hitl:
        # 只有开启 HITL 才挂载这两个节点：关闭时它们不可达，
        # 不参与编译，也就没有额外开销
        builder.add_node("plan_sql", plan_sql)
        builder.add_node("guard", guard)
        builder.add_edge("plan_sql", "guard")
        builder.add_edge("guard", "tool_execute")

    return builder.compile(checkpointer=checkpointer)


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
    """便捷入口：问题 → 最终答案文本 + 完整会话历史（对齐 run_analysis 返回约定）。

    不启用 HITL：这条入口给脚本/测试用，没有"等人确认"的交互通道。
    生产走 api/server.py 的 SSE 链路（那里才有审批卡片与恢复接口）。
    """
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
