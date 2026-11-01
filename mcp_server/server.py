"""data-agent MCP Server 实现（M9）。

职责边界（对齐"纯套壳复用现有能力"的约束）：
  - 不重写校验/执行逻辑：query_sql 直接走 data.validate.validate_sql
    + data.service.DataService.execute；ask_data 直接走
    agent.graph.run_analysis 四节点链路。
  - 只读铁律：三个工具全部标注 readOnlyHint，且 SQL 走 AST 白名单校验。
  - 错误人话回喂：校验/执行失败一律抛 ValueError（中文描述），
    FastMCP 会把它转成 isError 结果回给宿主，而不是裸异常。

P0 重构（2026-10-30）：
  - 工具装配改成工厂 ``build_mcp_server(ctx)``：既可以被 ``api/server.py``
    挂到 /mcp（同进程、共用同一个 DataService），也可以由 ``main()``
    跑 stdio。之前是模块级单例 + 模块级 service，导致"Web 一个进程、
    MCP 一个进程，两个进程抢同一个 DuckDB 文件锁"（P0-1）。
  - 依赖全部从 ``AppContext`` 取（懒加载 + 一份指标注册表），
    不再各写一份 ``service`` / ``_metric_registry``（P0-2 / P0-6）。

关键约束（MCP stdio 协议）：stdout 是协议通道，任何日志/print 都会
破坏 JSON-RPC 帧。因此本模块不调用 logging_setup.setup_logging()
（它写 stdout），而是在 main() 里把根日志显式重定向到 stderr。
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import config
from agent.graph import run_analysis
from app_context import AppContext, get_context
from data.metrics import Metric, match_metrics
from data.report.jobs import get_job_manager
from data.report.pipeline import (
    TEMPLATES_DIR,
    ReportError,
    ReportRequest,
    load_template,
    parse_period,
)
from data.validate import validate_sql

# 只读标注：查数据/结构/口径的工具全部 readOnlyHint；
# load_file 读宿主给的外部文件，标注 destructiveHint + openWorldHint；
# save_metric_definition 只动 Server 自己的持久化文件，不碰外部世界，
# 用独立的 LOCAL_WRITE 标注（destructive 但 openWorld=False）。
_RO_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
_LOCAL_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

# 结果返回上限：防止宿主无脑拉全表，超出的行数用提示注明总数
_DEFAULT_ROW_LIMIT = 200

# 字符级兜底（对齐参考项目的 CHARACTER_LIMIT）：只截行数防不住"行数少但单行超宽"
# 的结果（长文本字段、宽表），必须再加一道字符上限才不会撑爆宿主上下文。
_MAX_OUTPUT_CHARS = 25000

# ask_data 是四节点 LLM 链路，单次可能几十秒。超过上限立刻回喂超时错误，
# 避免宿主先超时、用户重复提交同一问题。
_ANALYSIS_TIMEOUT_SECONDS = 120


def _truncate(text: str) -> str:
    """按字符数兜底截断，超限时注明省略量并给出缩小范围的建议。"""
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    omitted = len(text) - _MAX_OUTPUT_CHARS
    return (
        text[:_MAX_OUTPUT_CHARS]
        + f"\n\n---\n*响应已截断：原始 {len(text)} 字符，省略 {omitted} 字符。"
        "请缩小范围（加 WHERE / LIMIT 或减少列数）后重试。*"
    )


def _humanize_error(exc: Exception, prefix: str) -> str:
    """把底层异常翻译成给 LLM 看的人话（对齐参考项目的 error-handler）。

    中文业务异常（QueryError 等）本身可读，直接透传；
    DuckDB / OpenAI 的英文原始报错按模式归类，并附"下一步该调哪个工具"的提示。
    """
    msg = str(exc).strip() or exc.__class__.__name__
    if isinstance(exc, TimeoutError):
        return f"{prefix}超时（{msg}），请缩小问题范围或稍后重试。"
    if any("\u4e00" <= ch <= "\u9fff" for ch in msg):
        return f"{prefix}失败：{msg}"
    low = msg.lower()
    if "timeout" in low or "timed out" in low:
        return f"{prefix}超时（{msg}），请缩小问题范围或稍后重试。"
    if any(k in low for k in ("api key", "api_key", "apikey", "401", "authentication")):
        return f"{prefix}失败：LLM API Key 未配置或无效，请在宿主环境变量里设置 DEEPSEEK_API_KEY。"
    if any(k in low for k in ("catalog error", "no such table", "table with name")):
        return f"{prefix}失败：表不存在（{msg}）。请先用 list_tables 查看当前可查的表。"
    if any(k in low for k in ("binder error", "no such column", "referenced column")):
        return f"{prefix}失败：列名不存在（{msg}）。请先用 describe_table 查看列名。"
    if any(k in low for k in ("parser error", "syntax error")):
        return f"{prefix}失败：SQL 语法错误（{msg}）。"
    if any(k in low for k in ("permission", "denied", "read-only")):
        return f"{prefix}失败：权限或只读限制（{msg}）。"
    return f"{prefix}失败：{msg}"


def _run_with_timeout(fn: Callable[[], Any], timeout_seconds: float) -> Any:
    """在守护线程里执行并限时等待，超时抛 TimeoutError。

    用 daemon 线程而不是 ThreadPoolExecutor：后者的工作线程非 daemon，
    超时后宿主断开时进程会一直等它跑完（等于没有超时）；守护线程则随
    主进程退出，遗留结果直接丢弃。
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # 线程内异常必须回传给调用方
            box["error"] = exc

    worker = threading.Thread(target=target, name="mcp-ask", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        raise TimeoutError(f"超过 {timeout_seconds:g} 秒")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def build_mcp_server(
    name: str = "data-agent",
    ctx: AppContext | None = None,
) -> FastMCP:
    """装配一个 MCP Server 实例。

    ctx=None 时用进程级上下文（stdio 模式）；传 ctx 则绑定到调用方的
    上下文——``api/server.py`` 用这种方式把 MCP 挂进同一个进程，
    与 HTTP 端点共用同一个 DataService（同一个 DuckDB 连接）。
    """
    server = FastMCP(name)

    def _ctx() -> AppContext:
        return ctx if ctx is not None else get_context()

    def _service():
        return _ctx().ensure_service()

    def _catalog():
        return _ctx().ensure_catalog()

    # ── 工具定义：装饰器注册到本实例，闭包只依赖上面的 _ctx() ──────────

    @server.tool(title="列出当前所有可查询的数据表", annotations=_RO_ANNOTATIONS)
    def list_tables() -> str:
        """当用户想知道"有哪些表 / 有什么数据 / 现在能查什么"时调用。
        返回当前已注册的所有数据表名，每行一个。"""
        tables = _service().list_tables()
        if not tables:
            return "（当前没有已加载的数据表，请先通过 web 服务上传文件。）"
        return _truncate("\n".join(tables))

    @server.tool(title="执行只读 SQL 查询", annotations=_RO_ANNOTATIONS)
    def query_sql(sql: str, limit: int = _DEFAULT_ROW_LIMIT) -> str:
        """当用户给出明确 SQL 查询语句时调用。执行一条只读 SQL 并把结果以 CSV 文本返回。
        仅支持 SELECT / WITH / UNION 查询：写操作（INSERT/UPDATE/DELETE/DROP 等）、
        多语句（分号注入）、文件写入与网络访问都会被安全校验拦截并返回中文错误。
        - sql: 完整的只读查询语句，如 "SELECT name, price FROM fruit ORDER BY price DESC LIMIT 5"
        - limit: 最多返回的行数（默认 200；查询结果超出时会注明总行数并截断返回）"""
        if not sql or not sql.strip():
            raise ValueError("SQL 不能为空。")
        if limit < 1:
            raise ValueError(f"limit 必须大于等于 1，收到: {limit}")
        service = _service()
        # 安全闸（纯套壳）：AST 级校验 + 当前已注册表的白名单
        err = validate_sql(sql, allowed_tables=service.list_tables())
        if err:
            raise ValueError(err)
        try:
            df: pd.DataFrame = service.execute(sql)
        except Exception as exc:
            raise ValueError(_humanize_error(exc, "SQL 执行")) from exc

        total = len(df)
        head = df.head(int(limit))
        csv_text = head.to_csv(index=False)
        if total > len(head):
            return _truncate(f"共 {total} 行，已截断返回前 {len(head)} 行：\n{csv_text}")
        return _truncate(f"共 {total} 行：\n{csv_text}")

    @server.tool(title="查看一张表的列结构与数据概况", annotations=_RO_ANNOTATIONS)
    def describe_table(table_name: str) -> str:
        """当用户想了解某张表有哪些列、每列的类型/缺失值/唯一值概况时调用。
        - table_name: 表名（可先用 list_tables 查看当前有哪些表）"""
        try:
            info = _service().describe(table_name)
        except Exception as exc:
            raise ValueError(_humanize_error(exc, "查看表结构")) from exc
        lines = [
            f"表 {info['table']}：共 {info['row_count']} 行",
            "",
            "| 列 | 类型 | 缺失值 | 唯一值 |",
            "|---|---|---|---|",
        ]
        for col, meta in info["columns"].items():
            lines.append(f"| {col} | {meta['type']} | {meta['nulls']} | {meta['unique']} |")
        return _truncate("\n".join(lines))

    @server.tool(title="预览一张表的前几行数据", annotations=_RO_ANNOTATIONS)
    def preview_table(table_name: str, max_rows: int = 10) -> str:
        """当用户想直接看某张表的样例数据（前几行长什么样）时调用。
        - table_name: 表名
        - max_rows: 预览行数，默认 10，上限 50"""
        if max_rows < 1 or max_rows > 50:
            raise ValueError(f"max_rows 必须在 1~50 之间，收到: {max_rows}")
        try:
            data = _service().preview(table_name, max_rows=max_rows)
        except Exception as exc:
            raise ValueError(_humanize_error(exc, "预览表")) from exc

        rows = data["rows"]
        if not rows:
            return f"表 {data['table']} 没有数据。"
        cols = [c["name"] for c in data["columns"]]
        lines = [f"表 {data['table']} 前 {len(rows)} 行：", "| " + " | ".join(cols) + " |"]
        lines.append("|" + "---|" * len(cols))
        for r in rows:
            lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
        return _truncate("\n".join(lines))

    @server.tool(title="查询指标口径定义（责任表 + 口径 SQL）", annotations=_RO_ANNOTATIONS)
    def get_metric_definition(question: str) -> str:
        """当用户问题涉及业务指标（销售额/GMV/毛利率等）时先调用，拿到官方口径。
        返回匹配指标的责任表与口径 SQL；无匹配时列出当前可用的全部指标。
        口径 SQL 是唯一合法算法，分析时不得更换责任表或改写口径。
        - question: 用户的指标相关提问，如 "近30天GMV" """
        if not question or not question.strip():
            raise ValueError("问题不能为空。")
        available = set(_service().list_tables())
        registry = _catalog().registry
        matched = match_metrics(question, available, registry)
        if not matched:
            if not registry:
                return "当前没有已注册的指标口径（skills/metrics 未配置）。"
            lines = ["未匹配到指标口径。当前可用指标："]
            for m in registry:
                if not set(m.tables).issubset(available):
                    continue
                lines.append(
                    f"- {m.name}（{m.description}；别名: {', '.join(m.aliases)}；"
                    f"责任表: {', '.join(m.tables)}）"
                )
            return _truncate("\n".join(lines) if len(lines) > 1 else "当前表集下没有可用指标。")
        parts = [f"匹配到 {len(matched)} 个指标口径（口径 SQL 为唯一合法算法，不得更换表或改写口径）："]
        for m in matched:
            parts.append(f"## {m.name}")
            parts.append(f"- 别名: {', '.join(m.aliases)}")
            parts.append(f"- 责任表: {', '.join(m.tables)}（必须使用这些表）")
            parts.append(f"- 口径 SQL: {m.sql}")
            parts.append(f"- 说明: {m.description}")
        return _truncate("\n".join(parts))

    @server.tool(title="把本地数据文件加载进数据仓库", annotations=_WRITE_ANNOTATIONS)
    def load_file(file_path: str, table_name: str = "") -> str:
        """当用户提供本地数据文件（CSV/Excel）并要求入库查询时调用。
        仅支持 DATA_DIR 目录下的文件（安全守卫：拒绝库外任意路径）。
        写入本地仓库并注册为新表，属破坏性操作，宿主会先弹确认框。
        - file_path: 数据文件路径（必须位于 DATA_DIR 内）
        - table_name: 可选，注册表名，默认取文件名"""
        if not file_path or not file_path.strip():
            raise ValueError("文件路径不能为空。")
        path = Path(file_path).expanduser()
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise ValueError(f"无法解析路径: {file_path}") from exc
        data_dir = Path(config.DATA_DIR).resolve()
        if not resolved.is_relative_to(data_dir):
            raise ValueError(f"仅支持 {data_dir} 目录下的文件: {file_path}")
        try:
            registered = _service().load_file(str(resolved), table_name)
        except Exception as exc:
            raise ValueError(_humanize_error(exc, "加载文件")) from exc
        return f"已注册 {len(registered)} 张表: {', '.join(registered)}"

    @server.tool(title="保存/覆盖一条用户自定义指标口径", annotations=_LOCAL_WRITE_ANNOTATIONS)
    def save_metric_definition(
        name: str,
        sql: str,
        tables: list[str],
        aliases: Optional[list[str]] = None,
        description: str = "",
    ) -> str:
        """当用户给出自己的业务口径、希望后续对话持续沿用（不用每次重说）时调用。
        写入 data/warehouse/user_metrics.md 并在本次运行内立刻生效，重启后跨会话沿用。
        同名指标会被覆盖；只影响用户级口径，项目级 skills/metrics 不受影响。
        - name: 指标名（英文，如 vip_customer）
        - sql: 口径 SQL（只读单语句，走 AST 安全校验）
        - tables: 责任表列表（必须全是当前已注册的表，此后该指标只允许查这些表）
        - aliases: 业务别名（如 ["高价值客户"]），省略时默认用指标名
        - description: 口径说明（为什么这么算，给 LLM 看）"""
        if not name or not name.strip():
            raise ValueError("指标名不能为空。")
        if not sql or not sql.strip():
            raise ValueError("口径 SQL 不能为空。")
        if not tables:
            raise ValueError("责任表不能为空。")
        available = set(_service().list_tables())
        for t in tables:
            if t not in available:
                raise ValueError(
                    f"责任表不在当前表集内: {t}（当前已注册: "
                    f"{', '.join(sorted(available)) or '无'}）"
                )
        err = validate_sql(sql, allowed_tables=available)
        if err:
            raise ValueError(f"口径 SQL 不合法: {err}")
        metric = Metric(
            name=name.strip(),
            aliases=tuple(a.strip() for a in (aliases or [name.strip()]) if a.strip()),
            sql=sql.strip(),
            tables=tuple(tables),
            description=description.strip(),
        )
        try:
            # P0-6：写盘 + 重载走同一份 catalog，Web 与 MCP 立刻都能命中
            _catalog().save_user_metric(metric)
        except Exception as exc:
            raise ValueError(_humanize_error(exc, "保存指标")) from exc
        return (
            f"已保存指标 {metric.name}（责任表: {', '.join(metric.tables)}；"
            f"别名: {', '.join(metric.aliases)}）。后续提问命中别名即自动套用该口径。"
        )

    @server.tool(title="用自然语言提问并自动分析数据", annotations=_RO_ANNOTATIONS)
    def ask_data(question: str) -> str:
        """当用户用自然语言描述数据分析需求（而非明确给出 SQL）时调用。
        内部自动完成"收集表结构 → LLM 生成 SQL → 安全校验 → 执行"全链路，
        返回可直接阅读的分析结果文本。
        - question: 用户的自然语言问题，如 "价格最高的前 3 种水果" """
        if not question or not question.strip():
            raise ValueError("问题不能为空。")
        try:
            result = _run_with_timeout(
                lambda: run_analysis(
                    _service(),
                    _ctx().ensure_sql_llm(),
                    question,
                    metric_registry=_catalog().registry,
                ),
                _ANALYSIS_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise ValueError(_humanize_error(exc, "分析")) from exc
        return _truncate(result["result_text"])

    # ── 报告任务（PR2）────────────────────────────────────────────
    # 长任务的 MCP 语义：提交 → 拿 job_id → 轮询。没有回调，所以工具描述里
    # 必须把"怎么轮询"写清楚——宿主的 LLM 是按描述决定行为的。

    @server.tool(title="列出可用的报告模板", annotations=_RO_ANNOTATIONS)
    def list_report_templates() -> str:
        """当用户要"生成经营报告 / 周报 / 月报"时先调用，确认有哪些模板可用。
        返回 JSON 数组，每项包含 name / title / description / sections；
        选好模板后再调用 build_report。"""
        items: list[dict[str, Any]] = []
        for path in sorted(TEMPLATES_DIR.glob("*.yaml")):
            try:
                tpl = load_template(path.stem)
                items.append({
                    "name": path.stem,
                    "title": tpl.get("title", ""),
                    "description": tpl.get("description", ""),
                    "sections": [s.get("id") for s in tpl.get("sections") or []],
                })
            except ReportError as exc:  # 单个模板坏了不影响其它模板
                items.append({"name": path.stem, "error": str(exc)})
        if not items:
            return "当前没有可用的报告模板（data/report/templates 为空）。"
        return _truncate(json.dumps(items, ensure_ascii=False, indent=2))

    @server.tool(title="生成结构化经营报告包", annotations=_LOCAL_WRITE_ANNOTATIONS)
    def build_report(
        period: str,
        template: str = "monthly_ecom",
        focus: str = "",
        wait_seconds: int = 20,
        force: bool = False,
    ) -> str:
        """当用户要"出本月/本周经营报告、数据报告"时调用。按模板跑 SQL、画图，
        产出**结构化报告包**（report.json + assets），供你之后渲染成 PPT / 飞书文档。
        本工具不生成 PPT、不发消息——那是宿主的事。
        - period: 周期，如 "2026-10" 或 "2026-10-01:2026-10-31"
        - template: 模板名（先调 list_report_templates 查看）
        - focus: 可选，本期关注点（只记录在 meta 里，不改变查询口径）
        - wait_seconds: 最多同步等待秒数（默认 20）；超时会返回 running + job_id
        - force: 已完成的任务是否强制重跑
        返回 JSON：status(succeeded/running/failed) / stage / spec_path / artifacts / summary。
        重要：status 是 running 时，请每 5 秒调用 get_report(job_id)，直到 succeeded 或 failed；
        不要重复调用 build_report（同一周期会命中同一个 job，不会重跑）。"""
        if not period or not period.strip():
            raise ValueError("period 不能为空，例如 2026-10 或 2026-10-01:2026-10-31")
        if wait_seconds < 0 or wait_seconds > 120:
            raise ValueError(f"wait_seconds 必须在 0~120 之间，收到: {wait_seconds}")
        try:
            start, end = parse_period(period)
            # 模板先同步校验：模板名写错要立刻报错，而不是提交一个后台任务
            # 然后返回 "failed"——宿主看到的状态会晚一步，用户也不知道错在哪
            load_template(template)
            request = ReportRequest(
                template=template,
                period_start=start,
                period_end=end,
                focus=focus,
                force=force,
            )
            manager = get_job_manager()
            job_id = manager.submit(_service(), request)
            payload = manager.wait(job_id, wait_seconds)
        except ReportError as exc:
            raise ValueError(str(exc)) from exc
        except Exception as exc:
            raise ValueError(_humanize_error(exc, "生成报告")) from exc
        return _truncate(json.dumps(payload, ensure_ascii=False, indent=2))

    @server.tool(title="查询报告任务状态", annotations=_RO_ANNOTATIONS)
    def get_report(job_id: str) -> str:
        """查询 build_report 提交的任务。返回 JSON：status / stage / progress /
        spec_path / artifacts / error。status 为 succeeded 时，把 spec_path 和
        artifacts 交给渲染流程（生成 PPT / 飞书文档）。
        - job_id: build_report 返回的 job_id """
        if not job_id or not job_id.strip():
            raise ValueError("job_id 不能为空")
        payload = get_job_manager().payload(job_id.strip())
        if payload is None:
            raise ValueError(f"报告任务不存在: {job_id}（请确认 job_id，或重新调用 build_report）")
        return _truncate(json.dumps(payload, ensure_ascii=False, indent=2))

    @server.tool(title="读取报告内容（report.json）", annotations=_RO_ANNOTATIONS)
    def get_report_spec(job_id: str) -> str:
        """读取已生成报告的**完整内容**（report.json 原文），供排版 PPT / 飞书文档。
        优先用这个，不要依赖"宿主能读本地文件"这个假设；内容超过 25000 字符会
        被截断，截断时按提示改用返回里的 spec_path 读文件。
        关键字段：sections[].metrics（数字，禁止改写）、narrative（可直接引用）、
        charts[].image_path（插图路径）、caveats（必须展示的口径说明）。
        - job_id: build_report 返回的 job_id """
        if not job_id or not job_id.strip():
            raise ValueError("job_id 不能为空")
        path = get_job_manager().job_dir(job_id.strip()) / "report.json"
        if not path.is_file():
            raise ValueError(
                f"报告内容不存在: {job_id}（请先 build_report，并确认 status 已是 succeeded）"
            )
        return _truncate(path.read_text(encoding="utf-8"))

    return server


# 模块级实例：stdio 模式（python -m mcp_server）直接用这个；
# web 服务会另建一个绑定自己上下文的实例挂到 /mcp。
mcp = build_mcp_server()


def main() -> None:
    """入口：把根日志重定向到 stderr（stdio 模式下 stdout 是协议通道，
    任何日志都会破坏 MCP 帧），然后启动 stdio 传输。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stderr,
        force=True,
    )
    mcp.run()  # 默认 stdio 传输，阻塞直到宿主断开


if __name__ == "__main__":
    main()
