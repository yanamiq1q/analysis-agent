"""LangGraph 主链路（M3）：用户问题 → 分析结果。

四节点 + 两条失败重试边：

  START → collect_schema ─→ generate_sql ─→ validate_sql
                                             │ 校验失败且未超重试
                                             ▼
              execute ◀─────────────┐
                │                    │
                │ 执行失败且未超重试   │
                ▼                    │
               END ◀─────────────────┘

设计要点：
  - 每个节点只负责一件事，状态通过 dict 合并传递（LangGraph 默认行为）
  - LLM 调用注入式：测试传 fake，生产传 build_deepseek_generator()
  - 重试上限 config.MAX_SQL_RETRIES，防止 LLM 反复输出坏 SQL 死循环
"""
from __future__ import annotations

import time

from langgraph.graph import END, START, StateGraph

import config
from agent.llm import SqlGenerator
from agent.state import AgentState
from data.metrics import Metric, format_metrics_prompt, match_metrics
from data.service import DataService
from data.validate import validate_sql
from logging_setup import get_logger

logger = get_logger("agent.graph")


_MAX_HISTORY_MESSAGES = config.MAX_HISTORY_MESSAGES  # 拼进 prompt 的最近消息数，防无限膨胀


def _build_history_text(history: list[dict]) -> str:
    """把会话历史转成给 LLM 看的文本（截断最近 N 条保证长度可控）。

    多轮场景 LLM 需要知道"再按月份分"指的是什么，历史就是最短路径：
    与其让 LLM 猜，不如把上一轮问答原样贴给它。
    """
    recent = history[-_MAX_HISTORY_MESSAGES:]
    if not recent:
        return ""
    lines = ["### 此前的对话（用于理解当前问题的上下文）"]
    for msg in recent:
        role = "用户" if msg.get("role") == "user" else "助手"
        lines.append(f"{role}: {msg.get('content', '')}")
    return "\n".join(lines) + "\n"


def build_graph(
    service: DataService,
    llm: SqlGenerator,
    metric_registry: list[Metric] | None = None,
) -> StateGraph:
    """构建并编译 LangGraph。llm 必须是 (question, schema, error) -> sql 的函数。

    metric_registry（M8）：指标口径数据源改为显式传入，由上层
    （server / 工具注册表）从 skill 包渲染；None 时 match_metrics
    回落到内置默认集（M4 行为，供旧调用方与迁移期兼容）。
    """

    # ── 节点1：收集表结构 + 匹配指标口径 ──────────────────────────
    def collect_schema(state: dict) -> dict:
        """摸清有哪些表、每张表长什么样，拼成给 LLM 的 schema 文本。

        同时用 M4 的指标字典做口径匹配：问题命中某指标别名且依赖表
        已加载时，把该指标的口径 SQL 一并注入——LLM 会优先套用标准
        口径，而不是自由发挥导致口径漂移。
        """
        t0 = time.monotonic()
        tables = service.list_tables()
        logger.info("collect_schema start tables=%d", len(tables))
        if not tables:
            # 无表 = 无法分析：置 error，由 route_after_schema 直接引到 END
            logger.warning("collect_schema no tables loaded, abort")
            return {
                "schema_text": "",
                "error": "当前没有已加载的数据表，请先上传文件。",
            }
        parts = []
        for t in tables:
            cols = service.table_schema(t)
            col_desc = ", ".join(f"{c['name']}({c['type']})" for c in cols)
            parts.append(f"表 {t}: {col_desc}")
        schema_text = "\n".join(parts)

        # 指标口径注入：M4
        question = state.get("question", "")
        matched = match_metrics(question, set(tables), metric_registry)
        if matched:
            logger.info("collect_schema matched_metrics=%s",
                        [m.name for m in matched])
        metrics_prompt = format_metrics_prompt(matched)
        if metrics_prompt:
            schema_text = schema_text + "\n\n" + metrics_prompt
        logger.info("collect_schema done ms=%.1f", (time.monotonic() - t0) * 1000)
        return {"schema_text": schema_text}

    # ── 条件边：无表直接结束（没有数据无从生成 SQL）────────────────
    def route_after_schema(state: dict) -> str:
        if state.get("error"):
            return END
        return "generate_sql"

    # ── 节点2：LLM 生成 SQL ───────────────────────────────────────
    def generate_sql(state: dict) -> dict:
        """调 LLM 把问题翻译成 SQL；若带 error 则是重试（让 LLM 修正）。

        重试时把"上次生成的 SQL"和错误一起喂回：否则 LLM 只看到
        错误文本，很可能重复输出同一个坏 SQL，重试次数白费。
        LLM 层故障（网络抖动/key 失效）也在这里捕获：写入 error 走
        统一重试链路，而不是让异常穿透整个 invoke() 直接崩溃。
        """
        t0 = time.monotonic()
        attempts = state.get("attempts", 0) + 1
        error_hint = state.get("error", "")
        last_sql = state.get("last_sql", "")
        if error_hint:
            logger.warning("generate_sql retry attempt=%d error=%s",
                           attempts, error_hint)
        if error_hint and last_sql:
            # 拼接示例："XX 校验失败。\n上次生成的 SQL: DELETE FROM fruit"
            error_hint = f"{error_hint}\n上次生成的 SQL: {last_sql}"
        try:
            sql = llm(
                _build_history_text(state.get("history", []))
                + "当前问题: " + state.get("question", ""),
                state.get("schema_text", ""),
                error_hint,
            )
        except Exception as exc:
            logger.error("generate_sql llm failed attempt=%d error=%s",
                         attempts, exc)
            return {"attempts": attempts, "error": f"LLM 调用失败: {exc}"}
        # 生成新 SQL 后清掉旧错误，避免污染后续判断
        logger.info("generate_sql ok attempt=%d ms=%.1f sql_chars=%d",
                    attempts, (time.monotonic() - t0) * 1000, len(sql))
        return {"sql": sql, "last_sql": sql, "attempts": attempts, "error": ""}

    # ── 节点3：AST 安全校验 ───────────────────────────────────────
    def validate_node(state: dict) -> dict:
        """用 M2 的 validate_sql 做安全闸；失败把错误写进 state 供重试。

        M4：传入表名白名单（当前已注册表集合），LLM 幻想的表会被这里
        拦住并带着"可用表清单"回喂重试——这就是旧项目的分析范围思想
        的简化版（旧项目是用户手动勾选，我们是自动=已加载集合）。
        """
        if state.get("error") and not state.get("sql"):
            # 上一节点 LLM 调用失败（sql 根本没生成）：保留原始错误，
            # 不能在这里把 error 覆盖成"SQL 语句为空"
            return {}
        err = validate_sql(
            state.get("sql", ""),
            allowed_tables=service.list_tables(),
        )
        if err:
            logger.warning("validate_sql failed attempt=%d error=%s",
                           state.get("attempts", 0), err)
        return {"error": err or ""}

    # ── 条件边：校验结果路由 ──────────────────────────────────────
    def route_after_validate(state: dict) -> str:
        """校验失败且没超重试次数 → 回 generate_sql 重写；否则执行。"""
        if state.get("error") and state.get("attempts", 0) < config.MAX_SQL_RETRIES:
            return "generate_sql"
        return "execute"

    # ── 节点4：执行查询 ───────────────────────────────────────────
    def execute(state: dict) -> dict:
        """执行 SQL 并把结果转成用户能读的文本（前 N 行）。"""
        if state.get("error"):
            # 走到这里说明重试次数已耗尽，直接返回错误作为最终答案
            logger.warning("execute skipped, retries exhausted error=%s",
                           state["error"])
            return {"result_text": f"生成 SQL 失败（已重试 {config.MAX_SQL_RETRIES} 次）: {state['error']}"}
        t0 = time.monotonic()
        try:
            df = service.query(state.get("sql", ""))
        except Exception as exc:
            # 执行失败（如列名拼错）：写 error，走条件边触发重试
            logger.warning("execute failed attempt=%d error=%s",
                           state.get("attempts", 0), exc)
            return {"error": f"SQL 执行失败: {exc}"}
        logger.info("execute ok rows=%d ms=%.1f",
                    len(df), (time.monotonic() - t0) * 1000)
        head = df.head(config.MAX_RESULT_ROWS)
        result_text = (
            f"共 {len(df)} 行，展示前 {len(head)} 行：\n"
            + head.to_string(index=False)
        )
        # df 一并放进 state：图走链路（plot_chart 工具）需要结构化
        # 数据而非文本二次解析（M7 图表可视化）
        return {"result_text": result_text, "error": "", "df": df}

    # ── 条件边：执行结果路由 ──────────────────────────────────────
    def route_after_execute(state: dict) -> str:
        """执行出错且没超重试次数 → 回 generate_sql 修正；否则结束。"""
        if state.get("error") and state.get("attempts", 0) < config.MAX_SQL_RETRIES:
            return "generate_sql"
        return END

    # ── 组装图 ─────────────────────────────────────────────────────
    builder = StateGraph(AgentState)
    builder.add_node("collect_schema", collect_schema)
    builder.add_node("generate_sql", generate_sql)
    builder.add_node("validate_sql", validate_node)
    builder.add_node("execute", execute)

    builder.add_edge(START, "collect_schema")
    builder.add_conditional_edges(
        "collect_schema",
        route_after_schema,
        {"generate_sql": "generate_sql", END: END},
    )
    # 注意：这层必须显式连接 generate_sql → validate_sql。
    # 漏掉的话 LangGraph 会把 generate_sql 当"死端"自动补 END 边，
    # 并把从 START 不可达的 validate/execute 节点整体剪掉（已踩坑）。
    builder.add_edge("generate_sql", "validate_sql")
    builder.add_conditional_edges(
        "validate_sql",
        route_after_validate,
        {"generate_sql": "generate_sql", "execute": "execute"},
    )
    builder.add_conditional_edges(
        "execute",
        route_after_execute,
        {"generate_sql": "generate_sql", END: END},
    )
    return builder.compile()


def run_analysis(
    service: DataService,
    llm: SqlGenerator,
    question: str,
    history: list[dict] | None = None,
    metric_registry: list[Metric] | None = None,
) -> dict:
    """便捷入口：问题 → 答案。返回 dict 而非裸文本（M5b）。

    返回 {"result_text": ..., "history": [...]}，history 是追加了本轮
    问答后的完整会话历史：调用侧把它原样传给下一轮，Agent 就有了
    多轮记忆。图本身保持无状态（对齐旧项目"无状态 Agent + 有状态
    后端"的分工），历史由调用侧（未来的 FastAPI session 层）持有。
    """
    graph = build_graph(service, llm, metric_registry=metric_registry)
    result = graph.invoke({"question": question, "history": history or []})
    text = result.get("result_text") or result.get("error") or "（无输出）"
    # 只保留 role/content 两个字段，删掉 state 里的内部字段，避免污染历史
    new_history = list(history or []) + [
        {"role": "user", "content": question},
        {"role": "assistant", "content": text},
    ]
    # df 供上层（plot_chart 工具）直接取结构化结果，M7 图表用
    return {"result_text": text, "history": new_history, "df": result.get("df")}
