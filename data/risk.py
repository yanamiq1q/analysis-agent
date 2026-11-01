"""高危 SQL 风险判定（Sprint 1 / HITL）：纯规则、不调 LLM、可单测。

为什么不用 LLM 判断"这条 SQL 危险吗"：
  1. 不确定 —— 同一条 SQL 两次问模型可能给不同答案，而"要不要拦"必须是确定的；
  2. 不可解释 —— 用户点确认时需要一个能看懂的理由（"预估扫描 120 万行"），
     而不是"模型觉得有风险"；
  3. 不划算 —— 多一次网络往返 + token，换来一个更差的结果。

所以规则只有四条，每条都能在 payload 里说清楚：
  R1 大表扫描：引用表基数之和 > HITL_MAX_SCAN_ROWS
  R2 无边界投影：SELECT * 且没有 WHERE
  R3 远端下推：命中留在 MySQL 的大表（可能打生产库）
  R4 口径偏离：命中指标口径，但 SQL 引用的表不在该指标责任表内

只有命中规则才要人工确认；普通聚合查询（count/sum + WHERE）零打扰——
否则用户第三次就会无脑点"同意"，审批机制等于失效。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

import sqlglot
import sqlglot.expressions as exp

import config
from data.metrics import Metric, match_metrics
from data.validate import collect_referenced_tables


@dataclass(frozen=True)
class PlanRisk:
    """一次查询计划的风险画像：既用于"要不要拦"，也是给用户看的确认卡片。"""

    sql: str
    tables: tuple[str, ...]
    estimated_rows: int
    reasons: tuple[str, ...]
    metric: Optional[str] = None

    @property
    def needs_approval(self) -> bool:
        return bool(self.reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "tables": list(self.tables),
            "estimated_rows": self.estimated_rows,
            "reasons": list(self.reasons),
            "metric": self.metric,
        }


def assess_plan(
    service,
    sql: str,
    question: str = "",
    registry: Optional[Sequence[Metric]] = None,
    max_scan_rows: Optional[int] = None,
) -> PlanRisk:
    """给一条已通过安全校验的 SQL 打风险分。

    service 只需要两个能力：`is_remote(table)` / `estimated_rows(table)`，
    因此单测可以传一个 5 行的假对象，不必起真引擎。
    """
    limit = config.HITL_MAX_SCAN_ROWS if max_scan_rows is None else max_scan_rows
    try:
        stmt = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception:
        # 走到这里说明校验层和解析层不一致（理论不该发生）：
        # 宁可让用户确认，也不要静默放行
        return PlanRisk(sql=sql, tables=(), estimated_rows=0,
                        reasons=("SQL 无法解析，无法评估代价",))

    tables = tuple(sorted(collect_referenced_tables(stmt)))
    reasons: list[str] = []

    # ── R1 / R3：代价与远端 ────────────────────────────────────────
    estimate = 0
    for table in tables:
        if service.is_remote(table):
            reasons.append(f"命中远端大表 {table}（SQL 将下推到生产库执行）")
            continue
        rows = service.estimated_rows(table)
        if rows:
            estimate += rows
    if estimate > limit:
        reasons.append(
            f"预估扫描 {estimate:,} 行，超过阈值 {limit:,} 行"
        )

    # ── R2：无边界投影 ────────────────────────────────────────────
    if _is_unbounded_star(stmt):
        reasons.append("SELECT * 且没有 WHERE 条件（可能把整表拉回内存）")

    # ── R4：口径偏离 ──────────────────────────────────────────────
    metric_name, deviation = _metric_deviation(service, question, tables, registry)
    if deviation:
        reasons.append(deviation)

    return PlanRisk(
        sql=sql,
        tables=tables,
        estimated_rows=estimate,
        reasons=tuple(reasons),
        metric=metric_name,
    )


def _is_unbounded_star(stmt) -> bool:
    """SELECT * 且没有 WHERE。

    注意不能用 `stmt.find_all(exp.Star)`：`count(*)` 里的 Star 也会命中，
    那是最常见的安全查询，误判会把审批做成"每次都弹"。
    """
    if not isinstance(stmt, exp.Select):
        return False
    has_star = any(isinstance(item, exp.Star) for item in stmt.expressions)
    has_where = stmt.find(exp.Where) is not None
    return has_star and not has_where


def _metric_deviation(
    service,
    question: str,
    tables: tuple[str, ...],
    registry: Optional[Sequence[Metric]],
) -> tuple[Optional[str], Optional[str]]:
    """命中口径但换了表 → 返回 (指标名, 偏离说明)。

    这是数据分析里最隐蔽的错误：数字算出来了、看着合理，但口径换了。
    比如用户问 GMV，口径要求 ods_order_info，LLM 却去查了汇总表。
    """
    if not registry or not question or not tables:
        return None, None
    try:
        available = set(service.list_tables())
    except Exception:  # pragma: no cover - 防御：元信息拿不到就跳过该规则
        return None, None
    matched = match_metrics(question, available, list(registry))
    if not matched:
        return None, None
    metric = matched[0]
    allowed = {t.casefold() for t in metric.tables}
    extra = [t for t in tables if t.casefold() not in allowed]
    if not extra:
        return metric.name, None
    return metric.name, (
        f"与指标 {metric.name} 的口径不一致：责任表是 "
        f"{'、'.join(metric.tables)}，SQL 还引用了 {'、'.join(extra)}"
    )
