"""报告流水线（PR1）：模板 + SQL → ReportSpec + 图表 + status.json。

为什么是一条"图"而不是一个函数
------------------------------
报告不是一次调用，而是四段有先后依赖的活：解析模板 → 查数 → 画图 → 组装。
用 LangGraph 的价值不在 v1（它现在就是线性的），而在于**后面要加的东西**：

  - 某一节失败时从断点重跑（checkpointer）；
  - 某一节风险过高时停下来等人确认（interrupt，复用 HITL 那套）；
  - 把阶段进度暴露给 MCP 的 get_report（stage/progress）。

如果现在就写成一个 200 行的大函数，这三件事以后都要手工补状态机——
那才是真的重复实现框架。

v1 的边界（刻意不做）：
  - 不调用 LLM：报表的 SQL 来自模板，确定性优先；自然语言问答走 ask_data。
  - 不做并行：DuckDB 是单连接，并行只会排队，还让进度变乱。
  - 不做自动修复：某节失败就标 skipped/error，让人看见，而不是静默重试。
"""
from __future__ import annotations

import calendar
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, TypedDict

import sqlglot
import yaml
from langgraph.graph import END, START, StateGraph

import config
from data.report.charts import KIND_LABELS, chart_data, save_png
from data.report.spec import (
    ChartRef,
    MetricValue,
    ReportSpec,
    Section,
    TableBlock,
    _num,
    summarize,
)
from data.validate import collect_referenced_tables
from logging_setup import get_logger

logger = get_logger("data.report")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class ReportError(Exception):
    """报告生成失败（消息面向用户/宿主，可直接展示）。"""


@dataclass(frozen=True)
class ReportRequest:
    template: str = "monthly_ecom"
    period_start: str = ""
    period_end: str = ""          # 开区间
    focus: str = ""
    force: bool = False


class ReportState(TypedDict, total=False):
    job_id: str
    job_dir: str
    request: dict
    template: dict
    title: str
    sections: list[dict]
    error: str


# ── 模板与周期 ──────────────────────────────────────────────────────

def load_template(name: str) -> dict:
    """读取模板 YAML；不存在时报错（不要静默回落到默认模板）。"""
    path = TEMPLATES_DIR / f"{name}.yaml"
    if not path.is_file():
        available = sorted(p.stem for p in TEMPLATES_DIR.glob("*.yaml"))
        raise ReportError(f"模板不存在: {name}（可用: {', '.join(available) or '无'}）")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ReportError(f"模板解析失败 {path.name}: {exc}") from exc
    if not data.get("sections"):
        raise ReportError(f"模板 {name} 没有 sections")
    names = [s.get("id") for s in data["sections"]]
    if len(names) != len(set(names)):
        raise ReportError(f"模板 {name} 的 section.id 有重复")
    return data


def parse_period(period: str) -> tuple[str, str]:
    """把 ``2026-10`` / ``2026-10-01:2026-10-31`` 解析成半开区间 [start, end)。

    只接受严格格式：这两个值会被拼进 SQL，任何自由文本都是注入面。
    """
    text = (period or "").strip()
    try:
        if ":" in text:
            start_s, end_s = text.split(":", 1)
            start = datetime.strptime(start_s.strip(), "%Y-%m-%d").date()
            end_inclusive = datetime.strptime(end_s.strip(), "%Y-%m-%d").date()
            end = end_inclusive + timedelta(days=1)
            return start.isoformat(), end.isoformat()
        start = datetime.strptime(text, "%Y-%m").date().replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start.isoformat(), end.isoformat()
    except ValueError as exc:
        raise ReportError(
            f"周期格式不对: {period!r}（支持 2026-10 或 2026-10-01:2026-10-31）"
        ) from exc


def make_job_id(request: ReportRequest, start: str, end: str) -> str:
    """确定的 job_id：同一周期 + 同一模板 + 同一 focus → 同一个 job（幂等）。"""
    focus_key = (request.focus or "").strip()
    suffix = f"-{abs(hash(focus_key)) % 10000:04d}" if focus_key else ""
    return f"rpt-{start}_{end}-{request.template}{suffix}"


# ── 状态文件 ────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def write_status(job_dir: Path, **fields: Any) -> None:
    """status.json 是 job 的唯一可观测状态（restart 后 get_report 靠它）。"""
    path = job_dir / "status.json"
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data.update(fields)
    data["updated_at"] = _now()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 图节点 ──────────────────────────────────────────────────────────

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _iso_date(value) -> str:
    if value is None:
        return ""
    try:
        return value.isoformat()[:10]
    except AttributeError:
        return str(value)[:10]


def compute_coverage(service, table: str, column: str, start: str, end: str) -> dict:
    """算一个日期列在 ``[start, end)`` 里的真实覆盖度。

    真机跑出来过一个问题：报告标题写"2026 年 10 月"，但
    ``ads_order_daily_report`` 只有 10-01 ~ 10-16，宿主是靠自己比对图表
    天数才发现"不能反推全月"，并在页面上手写警告。

    这种"数据缺口"必须由**产出数据的一方**说出来：它知道自己查了哪张表、
    哪个日期列、区间多大。渲染方不该去猜。

    只查 MIN/MAX/COUNT(DISTINCT)，走统计/索引，代价很小。
    """
    expected = (date.fromisoformat(end) - date.fromisoformat(start)).days
    base = {
        "start": start,
        "end": end,
        "days": 0,
        "expected_days": expected,
        "complete": False,
        "min_date": "",
        "max_date": "",
    }
    if not (_IDENT_RE.match(table) and _IDENT_RE.match(column)):
        return {**base, "error": "coverage 配置里的表名/列名不合法"}
    sql = (
        f'SELECT MIN("{column}") AS min_date, MAX("{column}") AS max_date, '
        f'COUNT(DISTINCT "{column}") AS days FROM "{table}" '
        f"WHERE \"{column}\" >= DATE '{start}' AND \"{column}\" < DATE '{end}'"
    )
    try:
        row = service.query(sql).iloc[0]
        days = int(row["days"] or 0)
    except Exception as exc:
        logger.warning("coverage query failed table=%s column=%s error=%s",
                       table, column, exc)
        return {**base, "error": str(exc)}
    return {
        **base,
        "days": days,
        "complete": days >= expected,
        "min_date": _iso_date(row.get("min_date")),
        "max_date": _iso_date(row.get("max_date")),
    }


_COMPARE_KINDS = {"mom": "环比", "yoy": "同比"}


def _normalize_compare(raw) -> list[str]:
    """模板里 `compare` 可以是 `[mom]`、`mom` 或 `[mom, yoy]`。

    未知类型直接丢掉（并留日志），不让一个拼写错误把整份报告搞挂。
    """
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    kinds: list[str] = []
    for item in raw:
        kind = str(item).strip().lower()
        if kind not in _COMPARE_KINDS:
            logger.warning("unknown compare kind ignored: %s", item)
            continue
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def _add_months(day: date, months: int) -> date:
    """按月偏移，月末自动收口（1-31 往前一个月 → 2-28/29）。"""
    total = (day.year * 12 + day.month - 1) + months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _minus_year(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:            # 2-29 → 2-28
        return day.replace(year=day.year - 1, day=28)


def shift_period(start: str, end: str, kind: str) -> tuple[str, str]:
    """算对比区间。

    mom（环比）：
      - 整月区间（起止都是 1 号）→ 前推相同月数：10 月 → 9 月，Q3 → Q2；
      - 其它区间 → **紧邻的等长区间**：10-01~10-16 → 09-16~10-01。
        不对齐自然月，否则会拿 15 天的数字去和 30 天比，那不叫环比。

    yoy（同比）：整体前推一年（2-29 落到 2-28）。
    """
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    if kind == "mom":
        months = (end_d.year - start_d.year) * 12 + (end_d.month - start_d.month)
        if start_d.day == 1 and end_d.day == 1:
            return _add_months(start_d, -months).isoformat(), start_d.isoformat()
        length = (end_d - start_d).days
        return (start_d - timedelta(days=length)).isoformat(), start_d.isoformat()
    if kind == "yoy":
        return _minus_year(start_d).isoformat(), _minus_year(end_d).isoformat()
    raise ValueError(f"未知对比类型: {kind}")


def compare_metric(current, previous) -> dict:
    """算一个指标的对比结果。

    三条规则：
      1. 任一侧缺失 → pct=null，并且**必须给 reason**（渲染方要能解释"为什么是 —"）；
      2. 基期为 0 → 比率没有意义，pct=null，但 delta 仍给（+500 元是事实）；
      3. 分母用 |prev|：上期为负时，涨跌方向不会被符号翻转。
    """
    cur = _num(current)
    prev = _num(previous) if previous is not None else None
    if cur is None or prev is None:
        return {"prev": None, "delta": None, "pct": None, "reason": "上期无数据"}
    delta = _num(cur - prev)
    if prev == 0:
        return {"prev": prev, "delta": delta, "pct": None, "reason": "基期为 0，比率不可比"}
    return {"prev": prev, "delta": delta, "pct": _num((cur - prev) / abs(prev))}


def _graph(service):
    """构建报告图。service 注入（生产 DataService，测试内存引擎）。"""
    def prepare(state: ReportState) -> ReportState:
        request = state["request"]
        template = state["template"]
        start, end = request["period_start"], request["period_end"]
        available = set(service.list_tables())

        sections: list[dict] = []
        for raw in template["sections"]:
            section = {
                "id": raw["id"],
                "heading": raw.get("heading", raw["id"]),
                "caveats": list(raw.get("caveats") or []),
                "skipped": "",
                "table": bool(raw.get("table")),
                "metrics_cfg": list(raw.get("metrics") or []),
                "chart_cfg": raw.get("chart") or None,
                "narrative_tpl": raw.get("narrative") or "",
                # 模板可声明"这一节的数据来自哪张表、哪个日期列"，
                # 用来算覆盖度（见 compute_coverage）
                "coverage_cfg": raw.get("coverage") or None,
                # 对比类型：["mom"] / ["yoy"] / 两者都要；字符串也容忍
                "compare_kinds": _normalize_compare(raw.get("compare")),
                "sql_tpl": raw.get("sql") or "",
                "sql": "",
            }
            if raw.get("sql"):
                section["sql"] = raw["sql"].format(start=start, end=end).strip()
                try:
                    stmt = sqlglot.parse_one(section["sql"], dialect="duckdb")
                    referenced = collect_referenced_tables(stmt)
                except Exception as exc:
                    section["skipped"] = f"SQL 解析失败: {exc}"
                    sections.append(section)
                    continue
                missing = sorted(t for t in referenced if t not in available)
                if missing:
                    # 缺表不报错：报告照样出，只是把这一节标成"跳过"
                    section["skipped"] = f"缺少表: {', '.join(missing)}"
            else:
                section["skipped"] = "模板未提供 sql"
            sections.append(section)

        write_status(Path(state["job_dir"]), stage="planning")
        return {"sections": sections}

    def execute(state: ReportState) -> ReportState:
        job_dir = Path(state["job_dir"])
        write_status(job_dir, stage="querying")
        sections = state["sections"]
        for section in sections:
            if section["skipped"]:
                continue
            try:
                df = service.query(section["sql"])
            except Exception as exc:
                # 单节失败不拖垮整份报告：标出来，让用户看到哪里缺了
                logger.warning("report section failed id=%s error=%s",
                               section["id"], exc)
                section["skipped"] = f"查询失败: {exc}"
                continue
            section["df"] = df

        # 覆盖度：同一张表 + 同一个日期列只查一次（kpi 和 trend 共用 ads 表）
        cache: dict[tuple[str, str], dict] = {}
        for section in sections:
            cfg = section.get("coverage_cfg")
            if section["skipped"] or not cfg:
                continue
            table = str(cfg.get("table") or "")
            column = str(cfg.get("date_column") or "")
            key = (table, column)
            if key not in cache:
                cache[key] = compute_coverage(
                    service, table, column,
                    state["request"]["period_start"],
                    state["request"]["period_end"],
                )
            section["coverage"] = cache[key]

        # 对比区间：同一节可能同时要环比 + 同比，各跑一次同一条 SQL
        start, end = state["request"]["period_start"], state["request"]["period_end"]
        for section in sections:
            kinds = section.get("compare_kinds") or []
            if section["skipped"] or not kinds:
                continue
            frames: dict[str, Any] = {}
            periods: dict[str, dict] = {}
            for kind in kinds:
                try:
                    p_start, p_end = shift_period(start, end, kind)
                    sql = section["sql_tpl"].format(start=p_start, end=p_end).strip()
                    frames[kind] = service.query(sql)
                    periods[kind] = {"start": p_start, "end": p_end}
                except Exception as exc:
                    # 对比失败不能拖垮主报告：记下来，由 compose 写进 warnings
                    logger.warning("compare query failed id=%s kind=%s error=%s",
                                   section["id"], kind, exc)
                    frames[kind] = None
            section["prev_frames"] = frames
            section["compare_periods"] = periods
        return {"sections": sections}

    def charts(state: ReportState) -> ReportState:
        job_dir = Path(state["job_dir"])
        write_status(job_dir, stage="charting")
        assets = job_dir / "assets"
        sections = state["sections"]
        for section in sections:
            cfg = section.get("chart_cfg")
            df = section.get("df")
            if section["skipped"] or not cfg or df is None or df.empty:
                continue
            name = f"{section['id']}.png"
            try:
                save_png(
                    df,
                    cfg.get("kind", "bar"),
                    assets / name,
                    title=cfg.get("title", section["heading"]),
                    x=cfg.get("x"),
                    y=cfg.get("y"),
                    max_rows=config.MAX_PLOT_ROWS,
                )
                data_path = assets / f"{section['id']}.json"
                data_path.write_text(
                    json.dumps(chart_data(df, config.MAX_PLOT_ROWS), ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning("report chart failed id=%s error=%s",
                               section["id"], exc)
                section["skipped"] = f"绘图失败: {exc}"
                continue
            section["chart"] = ChartRef(
                kind=cfg.get("kind", "bar"),
                title=cfg.get("title", section["heading"]),
                image_path=f"assets/{name}",
                data_path=f"assets/{section['id']}.json",
            )
        return {"sections": sections}

    def compose(state: ReportState) -> ReportState:
        job_dir = Path(state["job_dir"])
        write_status(job_dir, stage="composing")
        request = state["request"]
        built: list[Section] = []
        # (kind, period) 组合：上期为空的对比，最后统一转成 warnings
        compare_missing: list[dict] = []

        for section in state["sections"]:
            if section["skipped"]:
                built.append(
                    Section(
                        id=section["id"],
                        heading=section["heading"],
                        skipped=section["skipped"],
                        caveats=section["caveats"],
                    )
                )
                continue

            missing_kinds: set[str] = set()

            df = section.get("df")
            values: dict[str, Any] = {}
            metrics: list[MetricValue] = []
            first_row = df.iloc[0] if df is not None and not df.empty else None

            metrics_cfg = section["metrics_cfg"]
            if not metrics_cfg and df is not None and len(df) == 1:
                # 单行聚合 + 模板没写 metrics：默认把每一列都当指标暴露出来。
                # 否则 "SELECT count(*) AS n" 这种节会产出空 section，模板作者
                # 还得为每个聚合列重复写一遍 name/label。
                metrics_cfg = [{"name": str(c), "label": str(c)} for c in df.columns]

            for cfg in metrics_cfg:
                raw_value = None
                if first_row is not None and cfg["name"] in df.columns:
                    raw_value = first_row[cfg["name"]]
                value = _num(raw_value)
                values[cfg["name"]] = value

                # 对比结果：只在这里算一次，渲染方永远只做展示
                compare: dict[str, dict] = {}
                for kind in section.get("compare_kinds") or []:
                    prev_df = (section.get("prev_frames") or {}).get(kind)
                    prev_value = None
                    if (
                        prev_df is not None
                        and not prev_df.empty
                        and cfg["name"] in prev_df.columns
                    ):
                        prev_value = prev_df.iloc[0][cfg["name"]]
                    entry = compare_metric(raw_value, prev_value)
                    entry["period"] = (section.get("compare_periods") or {}).get(kind, {})
                    compare[kind] = entry
                    # 注意：不能用 prev_df.empty 判断"上期无数据"——
                    # 聚合查询在空区间上返回的是"一行 NULL"，不是 0 行。
                    # 只能看值。
                    if entry.get("reason") == "上期无数据":
                        missing_kinds.add(kind)

                metrics.append(
                    MetricValue(
                        name=cfg["name"],
                        label=cfg.get("label", cfg["name"]),
                        value=value,
                        unit=cfg.get("unit", ""),
                        definition=cfg.get("definition", ""),
                        format=cfg.get("format", "auto"),
                        compare=compare,
                    )
                )

            narrative = ""
            if section["narrative_tpl"] and all(v is not None for v in values.values()):
                try:
                    narrative = section["narrative_tpl"].format(**values)
                except (KeyError, ValueError, TypeError):
                    narrative = ""

            tables = []
            if section["table"] and df is not None and not df.empty:
                head = df.head(50)
                # astype(object) 才能把 NaN 变成真正的 null（见 charts.chart_data）
                safe = head.astype(object).where(head.notna(), None)
                tables.append(
                    TableBlock(
                        columns=[str(c) for c in safe.columns],
                        rows=safe.values.tolist(),
                        source_sql=section["sql"],
                    )
                )

            for kind in sorted(missing_kinds):
                compare_missing.append({
                    "kind": kind,
                    "period": (section.get("compare_periods") or {}).get(kind, {}),
                })

            built.append(
                Section(
                    id=section["id"],
                    heading=section["heading"],
                    narrative=narrative,
                    metrics=metrics,
                    tables=tables,
                    charts=[section["chart"]] if section.get("chart") else [],
                    caveats=section["caveats"],
                    coverage=section.get("coverage") or {},
                )
            )

        tables_used = sorted({
            t for s in state["sections"] if not s["skipped"]
            for t in _referenced(s["sql"])
        })
        coverage_map: dict[str, dict] = {}
        warnings: list[str] = []
        for section in state["sections"]:
            cov = section.get("coverage")
            cfg = section.get("coverage_cfg") or {}
            if not cov or not cfg:
                continue
            key = f"{cfg.get('table')}.{cfg.get('date_column')}"
            coverage_map[key] = cov
            if cov.get("complete"):
                continue
            msg = (
                f"{key} 仅覆盖 {cov.get('min_date') or '?'} ~ {cov.get('max_date') or '?'}"
                f"（{cov.get('days', 0)}/{cov.get('expected_days', 0)} 天）："
                "相关指标是该区间累计，不能当作整月口径"
            )
            if msg not in warnings:
                warnings.append(msg)

        # 对比区间与"对比不可用"的警告
        compare_periods: dict[str, dict] = {}
        for section in state["sections"]:
            for kind, period in (section.get("compare_periods") or {}).items():
                compare_periods.setdefault(kind, dict(period))

        seen_compare: set[tuple[str, str, str]] = set()
        for missing in compare_missing:
            kind = missing["kind"]
            period = missing.get("period") or {}
            key = (kind, period.get("start", ""), period.get("end", ""))
            if key in seen_compare:
                continue
            seen_compare.add(key)
            label = _COMPARE_KINDS.get(kind, kind)
            msg = (
                f"{label}不可用：{period.get('start', '?')} ~ "
                f"{period.get('end', '?')} 无数据"
            )
            if msg not in warnings:
                warnings.append(msg)

        spec = ReportSpec(
            job_id=state["job_id"],
            template=request["template"],
            title=state["title"],
            period={"start": request["period_start"], "end": request["period_end"]},
            sections=built,
            meta={
                "generated_at": _now(),
                "template": request["template"],
                "focus": request["focus"],
                "tables_used": tables_used,
                "queries": [s["sql"] for s in state["sections"] if not s["skipped"]],
                "skipped_sections": sum(1 for s in built if s.skipped),
                # 数据覆盖度 + 人话警告：渲染方不该靠肉眼比对图表天数来发现
                # "报告写的是整月、数据只有半个月"（真机跑出来的问题）
                "coverage": coverage_map,
                "compare": {"periods": compare_periods},
                "warnings": warnings,
                "engine": "data-agent report pipeline",
                "trace_id": f"trace-{uuid.uuid4().hex[:12]}",
            },
        )
        problems = spec.validate(base_dir=job_dir)
        if problems:
            raise ReportError("报告校验失败: " + "；".join(problems))
        spec.write(job_dir / "report.json")

        write_status(
            job_dir,
            status="succeeded",
            stage="done",
            progress=1.0,
            finished_at=_now(),
            spec_path=str(job_dir / "report.json"),
            summary=summarize(spec),
        )
        return {"sections": state["sections"]}

    builder = StateGraph(ReportState)
    builder.add_node("prepare", prepare)
    builder.add_node("execute", execute)
    builder.add_node("charts", charts)
    builder.add_node("compose", compose)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "execute")
    builder.add_edge("execute", "charts")
    builder.add_edge("charts", "compose")
    builder.add_edge("compose", END)
    return builder.compile()


def _referenced(sql: str) -> set[str]:
    try:
        return collect_referenced_tables(sqlglot.parse_one(sql, dialect="duckdb"))
    except Exception:
        return set()


# ── 对外入口 ────────────────────────────────────────────────────────

def run_report(
    service,
    request: ReportRequest,
    out_root: Optional[Path] = None,
    *,
    templates: Optional[dict[str, dict]] = None,
    job_id: Optional[str] = None,
) -> ReportSpec:
    """跑一份报告，返回 ReportSpec（同时已落盘 report.json / status.json）。

    幂等：同一 job_id 已完成且未 force → 直接读回已有 spec，不重跑。
    失败：写 status=failed（含 error.code），然后抛 ReportError——
          「失败」必须是一个状态，而不是一段会被排版进 PPT 的文字。
    """
    template = (templates or {}).get(request.template) or load_template(request.template)
    start, end = request.period_start, request.period_end
    if not start or not end:
        raise ReportError("缺少 period_start / period_end（CLI 用 --period 解析）")
    for value in (start, end):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            # 日期会被拼进 SQL，必须只接受严格 ISO 格式
            raise ReportError(f"周期不是合法日期: {value!r}") from exc

    job_id = job_id or make_job_id(request, start, end)
    job_dir = Path(out_root or config.REPORT_JOBS_DIR) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    status_path = job_dir / "status.json"
    if status_path.is_file() and not request.force:
        try:
            existing = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        if existing.get("status") == "succeeded" and (job_dir / "report.json").is_file():
            logger.info("report cache hit job=%s", job_id)
            return ReportSpec.load(job_dir / "report.json")

    title = template.get("title", request.template).format(
        year=start[:4], month=int(start[5:7])
    )
    write_status(
        job_dir,
        job_id=job_id,
        status="running",
        stage="queued",
        progress=0.0,
        started_at=_now(),
        title=title,
    )

    state: ReportState = {
        "job_id": job_id,
        "job_dir": str(job_dir),
        "title": title,
        "request": {
            "template": request.template,
            "period_start": start,
            "period_end": end,
            "focus": request.focus,
        },
        "template": template,
        "sections": [],
    }
    try:
        _graph(service).invoke(state)
    except Exception as exc:
        logger.exception("report failed job=%s", job_id)
        write_status(
            job_dir,
            status="failed",
            stage="failed",
            finished_at=_now(),
            error={"code": type(exc).__name__, "message": str(exc)},
        )
        raise ReportError(f"报告生成失败: {exc}") from exc

    spec = ReportSpec.load(job_dir / "report.json")
    logger.info("report done job=%s %s", job_id, summarize(spec))
    return spec
