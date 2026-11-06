"""黄金集评测：加载题目、跑用例、比对结果。

准确率的定义（这条要写进文档，别让人误解）：

    准确率 = Agent 执行结果与参考 SQL 结果一致的题数 / 总题数

三条边界：
  1. 比对的是**执行结果**（DataFrame），不是自然语言回答——所以不受"话术"影响；
  2. 参考 SQL 是人工写的 oracle，评测时现算，数据变化不影响题目有效性；
  3. 只评内层链路（问题 → SQL → 执行），不含外层 ReAct 的工具调度；
     调度正确性由单元测试覆盖，混进来会让这个数字失去意义。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import yaml

from agent.graph import run_analysis

_DEFAULT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class GoldenCase:
    id: str
    category: str
    question: str
    kind: str = "scalar"          # scalar（数值）| text（文本）| reject（应被拒绝）
    reference_sql: str = ""
    tolerance: float = _DEFAULT_TOLERANCE
    note: str = ""


def load_cases(path: Path | str) -> list[GoldenCase]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cases: list[GoldenCase] = []
    for raw in data.get("cases", []):
        cases.append(
            GoldenCase(
                id=str(raw["id"]),
                category=str(raw.get("category", "uncategorized")),
                question=str(raw["question"]),
                kind=str(raw.get("kind", "scalar")),
                reference_sql=str(raw.get("reference_sql") or ""),
                tolerance=float(raw.get("tolerance", _DEFAULT_TOLERANCE)),
                note=str(raw.get("note") or ""),
            )
        )
    return cases


# ── 比对 ────────────────────────────────────────────────────────────

def _first_cell(df):
    return df.iloc[0, 0]


def _as_number(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def compare(agent_df, ref_df, kind: str, tolerance: float) -> tuple[bool, str]:
    """比对两次执行结果。返回 (是否一致, 说明)。"""
    if agent_df is None or len(agent_df) == 0:
        return False, "Agent 没有返回结果"
    if kind == "reject":
        return False, "该题应被拒绝，但 Agent 执行出了结果"

    if kind == "text":
        got = str(_first_cell(agent_df)).strip().lower()
        want = str(_first_cell(ref_df)).strip().lower()
        return (got == want), f"got={got!r} want={want!r}"

    got_n = _as_number(_first_cell(agent_df))
    want_n = _as_number(_first_cell(ref_df))
    if got_n is None or want_n is None:
        # 期望值是 NULL（例如区间无数据）时，Agent 也应给空值
        return (got_n is None and want_n is None), f"got={got_n} want={want_n}"
    ok = abs(got_n - want_n) <= tolerance
    return ok, f"got={got_n} want={want_n} diff={abs(got_n - want_n):.6g}"


# ── 单题 ────────────────────────────────────────────────────────────

def run_case(service, sql_llm, case: GoldenCase, metric_registry=None) -> dict:
    """跑一题。reject 类只看"有没有执行出结果"。"""
    started = time.monotonic()
    result = {"id": case.id, "category": case.category, "question": case.question,
              "kind": case.kind, "ok": False, "detail": "", "ms": 0.0}

    if case.kind == "reject":
        out = run_analysis(service, sql_llm, case.question,
                           metric_registry=metric_registry)
        blocked = out.get("df") is None
        result["ok"] = blocked
        result["sql"] = out.get("sql", "")
        result["detail"] = "已拦截" if blocked else f"未拦截，执行了: {out.get('sql', '')[:80]}"
        result["ms"] = round((time.monotonic() - started) * 1000, 1)
        return result

    try:
        ref_df = service.query(case.reference_sql)
    except Exception as exc:  # 参考 SQL 自己都跑不通，是题目问题，不是 Agent 问题
        result["detail"] = f"参考 SQL 执行失败: {exc}"
        result["ms"] = round((time.monotonic() - started) * 1000, 1)
        return result

    out = run_analysis(service, sql_llm, case.question,
                       metric_registry=metric_registry)
    ok, detail = compare(out.get("df"), ref_df, case.kind, case.tolerance)
    result.update(ok=ok, detail=detail, sql=out.get("sql", ""),
                  ms=round((time.monotonic() - started) * 1000, 1))
    return result


# ── 批量 ────────────────────────────────────────────────────────────

def evaluate(
    service,
    sql_llm,
    cases: list[GoldenCase],
    progress: Optional[Callable[[dict], None]] = None,
    metric_registry=None,
) -> dict:
    results: list[dict] = []
    for index, case in enumerate(cases, start=1):
        result = run_case(service, sql_llm, case, metric_registry)
        results.append(result)
        if progress:
            progress(result)

    by_category: dict[str, dict] = {}
    for item in results:
        bucket = by_category.setdefault(item["category"], {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += 1 if item["ok"] else 0

    # reject 类不计入"数值准确率"：它衡量的是拦截能力，混在一起会让数字失去意义
    numeric = [r for r in results if r["kind"] != "reject"]
    reject = [r for r in results if r["kind"] == "reject"]
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r["ok"]),
        "accuracy": round(sum(1 for r in results if r["ok"]) / len(results), 4) if results else 0.0,
        "numeric_total": len(numeric),
        "numeric_passed": sum(1 for r in numeric if r["ok"]),
        "reject_total": len(reject),
        "reject_passed": sum(1 for r in reject if r["ok"]),
        "by_category": by_category,
        "results": results,
    }


def oracle_check(service, cases: list[GoldenCase]) -> dict:
    """参考集自检：参考 SQL 能不能跑通、是不是返回一行。

    这不是准确率，是"题目本身没写错"的证明。先跑它，再跑 Agent。
    """
    rows = []
    for case in cases:
        if case.kind == "reject" or not case.reference_sql:
            rows.append({"id": case.id, "ok": True, "detail": "跳过（拒绝类无需参考 SQL）"})
            continue
        try:
            df = service.query(case.reference_sql)
            ok = len(df) >= 1
            detail = "ok" if ok else "参考 SQL 返回 0 行"
        except Exception as exc:
            ok, detail = False, f"参考 SQL 执行失败: {exc}"
        rows.append({"id": case.id, "ok": ok, "detail": detail})
    return {"total": len(rows), "passed": sum(1 for r in rows if r["ok"]), "rows": rows}
