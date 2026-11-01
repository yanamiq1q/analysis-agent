"""报告流水线测试（PR1）：契约、模板、图、幂等、JSON 安全。

全部用内存 DuckDB + 真实模板，不调 LLM、不碰真实仓库。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from data.engine import AnalysisEngine
from data.report.charts import chart_data, render_png
from data.report.pipeline import (
    ReportError,
    ReportRequest,
    load_template,
    make_job_id,
    parse_period,
    run_report,
)
from data.report.spec import MetricValue, ReportSpec, Section
from data.service import DataService


@pytest.fixture
def service():
    """三张表：日汇总（有日期）+ 订单明细（无日期）+ 用户日汇总。"""
    svc = DataService(AnalysisEngine(db_path=None))
    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(14)]
    svc.engine.register_table(
        "ads_order_daily_report",
        pd.DataFrame({
            "dt": days,
            "order_cnt": [60 + i for i in range(14)],
            "gmv": [82000.0 + i * 1000 for i in range(14)],
            "pay_rate": [0.69, 0.71, 0.68, 0.72, 0.70, 0.73, 0.71,
                         0.74, 0.72, 0.69, 0.75, 0.73, 0.76, 0.74],
        }),
    )
    svc.engine.register_table(
        "ods_order_info",
        pd.DataFrame({
            "order_id": [1, 2, 3, 4],
            "user_id": [101, 102, 101, 103],
            "amount": [10.0, 20.0, 30.0, 40.0],
            "status": ["paid", "unpaid", "paid", "paid"],
        }),
    )
    svc.engine.register_table(
        "dws_user_order_1d",
        pd.DataFrame({
            "dt": [days[0]] * 4,
            "user_id": [101, 102, 103, 104],
            "order_cnt": [5, 3, 2, 1],
            "gmv": [1500.0, 920.5, 300.0, 100.0],
        }),
    )
    return svc


def _request(tmp_path: Path, period: str = "2026-10") -> ReportRequest:
    start, end = parse_period(period)
    return ReportRequest(template="monthly_ecom", period_start=start, period_end=end)


# ── 周期与 job_id ───────────────────────────────────────────────────

class TestPeriod:
    def test_month_range_is_half_open(self):
        assert parse_period("2026-10") == ("2026-10-01", "2026-11-01")

    def test_explicit_range_includes_end_day(self):
        assert parse_period("2026-10-01:2026-10-31") == ("2026-10-01", "2026-11-01")

    @pytest.mark.parametrize("bad", ["2026", "2026-13", "2026/10", "2026-10-01", "'; DROP TABLE x;--"])
    def test_bad_period_rejected(self, bad):
        with pytest.raises(ReportError):
            parse_period(bad)

    def test_job_id_is_deterministic(self):
        start, end = parse_period("2026-10")
        a = make_job_id(ReportRequest("m", start, end), start, end)
        b = make_job_id(ReportRequest("m", start, end), start, end)
        assert a == b
        c = make_job_id(ReportRequest("m", start, end, focus="渠道"), start, end)
        assert c != a


# ── 模板 ────────────────────────────────────────────────────────────

class TestTemplate:
    def test_builtin_template_loads(self):
        tpl = load_template("monthly_ecom")
        assert tpl["name"] == "monthly_ecom"
        assert len(tpl["sections"]) >= 3

    def test_unknown_template_lists_available(self):
        with pytest.raises(ReportError, match="可用"):
            load_template("nope")


# ── 端到端 ──────────────────────────────────────────────────────────

class TestRunReport:
    def test_generates_spec_assets_and_status(self, service, tmp_path):
        spec = run_report(service, _request(tmp_path), out_root=tmp_path)

        assert spec.job_id.startswith("rpt-2026-10-01_2026-11-01-monthly_ecom")
        assert [s.id for s in spec.sections] == ["kpi", "gmv_trend", "order_status", "top_users"]
        assert all(not s.skipped for s in spec.sections), [s.skipped for s in spec.sections]

        kpi = spec.sections[0]
        names = {m.name: m.value for m in kpi.metrics}
        assert names["orders"] == sum(60 + i for i in range(14))
        assert names["gmv"] == pytest.approx(sum(82000.0 + i * 1000 for i in range(14)))
        # 叙述由模板渲染，数字来自查询结果
        assert "GMV" in kpi.narrative and "订单" in kpi.narrative

        job_dir = tmp_path / spec.job_id
        assert (job_dir / "report.json").is_file()
        status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
        assert status["status"] == "succeeded"
        assert status["stage"] == "done"

        chart_files = list((job_dir / "assets").glob("*.png"))
        assert len(chart_files) == 2          # 趋势 + 状态分布
        assert all(f.stat().st_size > 1000 for f in chart_files)

        # 契约自检必须通过（否则对方渲染时会踩坑）
        assert spec.validate(base_dir=job_dir) == []

    def test_report_json_is_strict_json(self, service, tmp_path):
        """日期列和 NaN 都必须变成合法 JSON（对方用严格解析器）。"""
        spec = run_report(service, _request(tmp_path), out_root=tmp_path)
        text = (tmp_path / spec.job_id / "report.json").read_text(encoding="utf-8")
        assert "NaN" not in text and "Infinity" not in text
        data = json.loads(text)
        assert data["sections"][1]["charts"][0]["image_path"].startswith("assets/")

    def test_idempotent_second_run_uses_cache(self, service, tmp_path):
        first = run_report(service, _request(tmp_path), out_root=tmp_path)
        status_path = tmp_path / first.job_id / "status.json"
        before = status_path.read_text(encoding="utf-8")
        second = run_report(service, _request(tmp_path), out_root=tmp_path)
        assert second.job_id == first.job_id
        assert status_path.read_text(encoding="utf-8") == before   # 没有重跑

    def test_missing_table_skips_section_but_keeps_report(self, service, tmp_path):
        templates = {
            "demo": {
                "name": "demo",
                "title": "{year}-{month} 演示",
                "sections": [
                    {"id": "ok", "heading": "正常节",
                     "sql": "SELECT count(*) AS n FROM ods_order_info"},
                    {"id": "ghost", "heading": "缺表节",
                     "sql": "SELECT count(*) AS n FROM no_such_table"},
                ],
            }
        }
        spec = run_report(
            service, ReportRequest("demo", "2026-10-01", "2026-11-01"),
            out_root=tmp_path, templates=templates,
        )
        assert spec.sections[1].skipped.startswith("缺少表")
        assert spec.meta["skipped_sections"] == 1
        # 正常节仍然产出，且报告整体是 succeeded
        assert spec.sections[0].metrics[0].value == 4

    def test_bad_date_rejected_before_sql(self, service, tmp_path):
        with pytest.raises(ReportError, match="日期"):
            run_report(
                service, ReportRequest("monthly_ecom", "2026-10-01", "not-a-date"),
                out_root=tmp_path,
            )


# ── 契约校验 ────────────────────────────────────────────────────────

class TestSpecValidation:
    def test_string_number_is_rejected(self):
        spec = ReportSpec(
            job_id="j", template="t", title="x",
            period={"start": "2026-10-01", "end": "2026-11-01"},
            sections=[Section("s", "h", metrics=[MetricValue("gmv", "GMV", "1,234 万")])],  # type: ignore[arg-type]
            meta={"generated_at": "2026-11-01T00:00:00+08:00"},
        )
        assert any("不是数字" in p for p in spec.validate())

    def test_missing_generated_at_is_rejected(self, tmp_path):
        spec = ReportSpec(job_id="j", template="t", title="x",
                          period={"start": "a", "end": "b"},
                          sections=[Section("s", "h")])
        assert any("generated_at" in p for p in spec.validate())

    def test_missing_chart_file_is_rejected(self, tmp_path):
        from data.report.spec import ChartRef

        spec = ReportSpec(job_id="j", template="t", title="x",
                          period={"start": "a", "end": "b"},
                          sections=[Section("s", "h", charts=[ChartRef("bar", "t", "assets/none.png")])],
                          meta={"generated_at": "now"})
        assert any("图表文件不存在" in p for p in spec.validate(base_dir=tmp_path))

    def test_write_and_load_roundtrip(self, service, tmp_path):
        spec = run_report(service, _request(tmp_path), out_root=tmp_path)
        again = ReportSpec.load(tmp_path / spec.job_id / "report.json")
        assert again.to_dict() == spec.to_dict()


# ── 图表 ────────────────────────────────────────────────────────────

class TestCharts:
    def test_renders_png(self):
        df = pd.DataFrame({"k": ["a", "b"], "v": [1, 2]})
        png = render_png(df, "bar", "标题")
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_single_column_rejected(self):
        with pytest.raises(ValueError, match="只有一列"):
            render_png(pd.DataFrame({"k": ["a"]}), "bar")

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError, match="不支持的图表类型"):
            render_png(pd.DataFrame({"k": ["a"], "v": [1]}), "pie3d")

    def test_chart_data_has_null_for_nan(self):
        df = pd.DataFrame({"k": ["a", "b"], "v": [1.0, None]})
        assert chart_data(df)["rows"][1][1] is None

class TestCoverage:
    """数据覆盖度：报告必须自己说清"只有半个月数据"，别让渲染方去猜。"""

    def test_partial_month_is_flagged(self, service, tmp_path):
        spec = run_report(service, _request(tmp_path), out_root=tmp_path)
        kpi = spec.sections[0]
        assert kpi.coverage["days"] == 14
        assert kpi.coverage["expected_days"] == 31
        assert kpi.coverage["complete"] is False
        assert kpi.coverage["min_date"] == "2026-10-01"
        assert kpi.coverage["max_date"] == "2026-10-14"
        warnings = spec.meta["warnings"]
        assert any("ads_order_daily_report.dt" in w and "14/31" in w for w in warnings)
        # 同一张表的两节共用一条警告，不重复刷屏
        assert sum("ads_order_daily_report.dt" in w for w in warnings) == 1

    def test_full_coverage_has_no_warning(self, service, tmp_path):
        start, end = parse_period("2026-10-01:2026-10-14")   # 正好 14 天
        spec = run_report(
            service,
            ReportRequest("monthly_ecom", start, end),
            out_root=tmp_path,
        )
        assert spec.sections[0].coverage["complete"] is True
        assert not any("ads_order_daily_report" in w for w in spec.meta["warnings"])

    def test_bad_coverage_config_is_reported_not_crashing(self, service, tmp_path):
        templates = {
            "bad": {
                "name": "bad",
                "title": "{year}-{month} 演示",
                "sections": [{
                    "id": "x", "heading": "x",
                    "sql": "SELECT count(*) AS n FROM ods_order_info",
                    "coverage": {"table": "ods_order_info; DROP TABLE x", "date_column": "dt"},
                }],
            }
        }
        spec = run_report(
            service, ReportRequest("bad", "2026-10-01", "2026-11-01"),
            out_root=tmp_path, templates=templates,
        )
        cov = spec.sections[0].coverage
        assert cov["complete"] is False
        assert "error" in cov

    def test_float_noise_is_trimmed(self):
        from data.report.spec import _num

        assert _num(0.7112499999999999) == 0.71125
        assert _num(1846900.0) == 1846900
        assert _num(float("nan")) is None

class TestCompare:
    """环比/同比：派生指标只能由数据侧算，渲染方只展示。"""

    @pytest.fixture
    def two_months(self):
        """9 月 30 天（1000/天）+ 10 月 31 天（2000/天）。"""
        svc = DataService(AnalysisEngine(db_path=None))
        rows = []
        for month, days, gmv, cnt, rate in (
            (9, 30, 1000.0, 10, 0.5),
            (10, 31, 2000.0, 20, 0.6),
        ):
            for day in range(1, days + 1):
                rows.append({"dt": date(2026, month, day), "gmv": gmv,
                             "order_cnt": cnt, "pay_rate": rate})
        svc.engine.register_table("ads_order_daily_report", pd.DataFrame(rows))
        svc.engine.register_table(
            "ods_order_info",
            pd.DataFrame({"order_id": [1], "user_id": [1], "amount": [1.0], "status": ["paid"]}),
        )
        svc.engine.register_table(
            "dws_user_order_1d",
            pd.DataFrame({"dt": [date(2026, 10, 1)], "user_id": [1],
                          "order_cnt": [1], "gmv": [10.0]}),
        )
        return svc

    def test_mom_uses_previous_month(self, two_months, tmp_path):
        spec = run_report(two_months, _request(tmp_path), out_root=tmp_path)
        kpi = spec.sections[0]
        gmv = next(m for m in kpi.metrics if m.name == "gmv")
        # 10 月 62,000 vs 9 月 30,000 → +106.67%
        assert gmv.value == 62000
        assert gmv.compare["mom"]["prev"] == 30000
        assert gmv.compare["mom"]["delta"] == 32000
        assert gmv.compare["mom"]["pct"] == pytest.approx(32000 / 30000, rel=1e-6)
        assert gmv.compare["mom"]["period"] == {
            "start": "2026-09-01", "end": "2026-10-01",
        }
        assert spec.meta["compare"]["periods"]["mom"]["start"] == "2026-09-01"
        assert not any("环比不可用" in w for w in spec.meta["warnings"])

    def test_missing_previous_month_is_explained(self, service, tmp_path):
        spec = run_report(service, _request(tmp_path), out_root=tmp_path)
        gmv = next(m for m in spec.sections[0].metrics if m.name == "gmv")
        assert gmv.compare["mom"]["pct"] is None
        assert gmv.compare["mom"]["reason"] == "上期无数据"
        assert any("环比不可用" in w for w in spec.meta["warnings"])

    def test_period_shifts(self):
        from data.report.pipeline import shift_period

        assert shift_period("2026-10-01", "2026-11-01", "mom") == ("2026-09-01", "2026-10-01")
        assert shift_period("2026-07-01", "2026-10-01", "mom") == ("2026-04-01", "2026-07-01")
        # 非整月：紧邻的等长区间，不对齐自然月
        assert shift_period("2026-10-01", "2026-10-16", "mom") == ("2026-09-16", "2026-10-01")
        assert shift_period("2026-10-01", "2026-11-01", "yoy") == ("2025-10-01", "2025-11-01")
        # 闰日
        assert shift_period("2024-02-29", "2024-03-01", "yoy") == ("2023-02-28", "2023-03-01")

    def test_zero_base_keeps_delta_but_drops_ratio(self):
        from data.report.pipeline import compare_metric

        result = compare_metric(500, 0)
        assert result["delta"] == 500
        assert result["pct"] is None
        assert "基期为 0" in result["reason"]

    def test_negative_base_uses_abs(self):
        from data.report.pipeline import compare_metric

        # -100 → -50：亏损收窄 50，比率按 |prev| 算是 +50%
        result = compare_metric(-50, -100)
        assert result["delta"] == 50
        assert result["pct"] == pytest.approx(0.5)

    def test_compare_without_pct_needs_reason(self):
        spec = ReportSpec(
            job_id="j", template="t", title="x",
            period={"start": "a", "end": "b"},
            sections=[Section("s", "h", metrics=[
                MetricValue("gmv", "GMV", 1, compare={"mom": {"prev": 1, "delta": 0, "pct": None}})
            ])],
            meta={"generated_at": "now"},
        )
        assert any("reason" in p for p in spec.validate())
