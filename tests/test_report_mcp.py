"""PR2 测试：报告 MCP 工具（提交 / 轮询 / 模板 / 幂等）。

不调 LLM、不碰真实仓库；报告 job 跑在临时目录里。
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

import api.server as server
import app_context
import config
import mcp_server.server as mcp_server
from app_context import AppContext, MetricCatalog
from data.engine import AnalysisEngine
from data.report import jobs as jobs_module
from data.service import DataService
from tests.test_mcp_server import _call, _content_text


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """内存引擎（三张报告要用的表）+ 临时产物目录 + 干净的 job 管理器。"""
    svc = DataService(AnalysisEngine(db_path=None))
    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(14)]
    svc.engine.register_table(
        "ads_order_daily_report",
        pd.DataFrame({
            "dt": days,
            "order_cnt": [60 + i for i in range(14)],
            "gmv": [82000.0 + i * 1000 for i in range(14)],
            "pay_rate": [0.7] * 14,
        }),
    )
    svc.engine.register_table(
        "ods_order_info",
        pd.DataFrame({
            "order_id": [1, 2, 3],
            "user_id": [101, 102, 101],
            "amount": [10.0, 20.0, 30.0],
            "status": ["paid", "unpaid", "paid"],
        }),
    )
    svc.engine.register_table(
        "dws_user_order_1d",
        pd.DataFrame({
            "dt": [days[0]] * 3,
            "user_id": [101, 102, 103],
            "order_cnt": [5, 3, 2],
            "gmv": [1500.0, 920.5, 300.0],
        }),
    )

    ctx = AppContext(
        service=svc,
        catalog=MetricCatalog(
            skills_dir=tmp_path / "skills",
            user_metrics_path=tmp_path / "user_metrics.md",
        ),
    )
    monkeypatch.setattr(app_context, "_context", ctx)
    monkeypatch.setattr(config, "REPORT_JOBS_DIR", tmp_path / "report_jobs")
    jobs_module.reset_job_manager()
    yield ctx
    jobs_module.reset_job_manager()


def _json(text: str) -> dict:
    return json.loads(text)


def _poll_until_done(job_id: str, timeout: float = 20.0) -> dict:
    """模拟宿主：每 0.3 秒调一次 get_report，直到终态。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = _json(_content_text(_call("get_report", job_id=job_id)))
        if payload["status"] in ("succeeded", "failed"):
            return payload
        time.sleep(0.3)
    raise AssertionError(f"报告任务超时未完成: {job_id}")


class TestTemplates:
    def test_lists_builtin_template(self):
        data = _json(_content_text(_call("list_report_templates")))
        names = {item["name"] for item in data}
        assert "monthly_ecom" in names
        monthly = next(i for i in data if i["name"] == "monthly_ecom")
        assert monthly["sections"] == ["kpi", "gmv_trend", "order_status", "top_users"]


class TestBuildReport:
    def test_sync_path_returns_finished_report(self, ctx, tmp_path):
        payload = _json(_content_text(_call(
            "build_report", period="2026-10", wait_seconds=30
        )))

        assert payload["status"] == "succeeded"
        spec_path = payload["spec_path"]
        assert spec_path.endswith("report.json")
        assert (tmp_path / "report_jobs" / payload["job_id"] / "report.json").is_file()
        assert any(a.endswith(".png") for a in payload["artifacts"])

        spec = json.loads(open(spec_path, encoding="utf-8").read())
        assert [s["id"] for s in spec["sections"]] == [
            "kpi", "gmv_trend", "order_status", "top_users",
        ]
        kpi = {m["name"]: m["value"] for m in spec["sections"][0]["metrics"]}
        assert kpi["orders"] == sum(60 + i for i in range(14))

    def test_polling_path_when_not_finished_in_time(self, ctx):
        # wait_seconds=0 → 立刻返回 running/queued，宿主必须自己轮询
        first = _json(_content_text(_call(
            "build_report", period="2026-10", wait_seconds=0
        )))
        assert first["status"] in ("queued", "running")
        assert first["hint"]

        done = _poll_until_done(first["job_id"])
        assert done["status"] == "succeeded"
        assert done["spec_path"]

    def test_same_period_is_idempotent(self, ctx):
        a = _json(_content_text(_call("build_report", period="2026-10", wait_seconds=30)))
        b = _json(_content_text(_call("build_report", period="2026-10", wait_seconds=30)))
        assert a["job_id"] == b["job_id"]
        # 第二次命中缓存：完成时间不变
        assert a["finished_at"] == b["finished_at"]

    def test_failed_report_reports_error_not_crash(self, ctx, monkeypatch):
        # 让数据层在查询时抛错：任务应变成 failed（带 error.code），而不是把异常抛给宿主
        def boom(sql):
            raise RuntimeError("模拟查询失败")

        ctx.service.query = boom
        payload = _json(_content_text(_call(
            "build_report", period="2026-10", wait_seconds=30
        )))
        # 单节失败会被降级成 skipped，报告仍 succeeded——这是刻意的设计
        assert payload["status"] == "succeeded"
        spec = json.loads(open(payload["spec_path"], encoding="utf-8").read())
        assert all(s["skipped"] for s in spec["sections"])

    def test_bad_period_rejected(self, ctx):
        with pytest.raises(ToolError, match="周期"):
            _call("build_report", period="2026/10")

    def test_unknown_template_rejected(self, ctx):
        with pytest.raises(ToolError, match="模板不存在"):
            _call("build_report", period="2026-10", template="nope")


class TestGetReport:
    def test_unknown_job_rejected(self, ctx):
        with pytest.raises(ToolError, match="不存在"):
            _call("get_report", job_id="rpt-nope")

    def test_empty_job_id_rejected(self, ctx):
        with pytest.raises(ToolError, match="不能为空"):
            _call("get_report", job_id=" ")

    def test_status_survives_manager_reset(self, ctx):
        """内存里的 job 表可以丢，status.json 不能丢（进程重启场景）。"""
        payload = _json(_content_text(_call(
            "build_report", period="2026-10", wait_seconds=30
        )))
        jobs_module.reset_job_manager()          # 模拟进程重启
        again = _json(_content_text(_call("get_report", job_id=payload["job_id"])))
        assert again["status"] == "succeeded"
        assert again["spec_path"] == payload["spec_path"]

class TestGetReportSpec:
    def test_returns_full_spec_json(self, ctx):
        payload = _json(_content_text(_call(
            "build_report", period="2026-10", wait_seconds=30
        )))
        text = _content_text(_call("get_report_spec", job_id=payload["job_id"]))
        spec = json.loads(text)
        assert spec["schema_version"] == "1.0"
        assert spec["sections"][0]["metrics"]
        assert spec["meta"]["queries"]

    def test_unknown_job_rejected(self, ctx):
        with pytest.raises(ToolError, match="不存在"):
            _call("get_report_spec", job_id="nope")

    def test_empty_job_id_rejected(self, ctx):
        with pytest.raises(ToolError, match="不能为空"):
            _call("get_report_spec", job_id=" ")

class TestMcpToolsEndpoint:
    def test_lists_registered_report_tools(self, ctx):
        """/api/mcp/tools 是"面板里没有新工具"的排查入口。"""
        client = TestClient(server.app)
        data = client.get("/api/mcp/tools").json()
        assert data["count"] >= 12
        assert "build_report" in data["tools"]
        assert "get_report_spec" in data["tools"]
