"""HITL（高危 SQL 人工确认）测试：Sprint 1。

测的是"审批这件事"的完整语义，而不是实现细节：
  1. 高风险 → 必须拦住，且**没有执行**任何 SQL；
  2. 批准 → 执行的必须就是卡片上那条 SQL（不再重新生成）；
  3. 拒绝 → 理由要能回喂给 LLM，让它改写而不是原样重试；
  4. 低风险 → 零打扰（不能每条查询都弹）；
  5. 同一个 thread 只允许消费一次审批；断线重连要回放卡片而不是新开一轮；
  6. 关掉开关 → 完全回到旧路径（可回滚）。
"""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

import api.server as server
import app_context
import config
from api.sessions import SessionStore
from app_context import AppContext, MetricCatalog
from data.engine import AnalysisEngine
from data.metrics import Metric
from data.risk import assess_plan
from data.service import DataService
from tests.test_api import FakeChat, FakeLLM, final_resp, parse_sse_lines, tool_resp

RISKY_SQL = "SELECT * FROM big"          # 星号投影 + 无 WHERE + 200 行 > 阈值
SAFE_SQL = "SELECT count(*) AS n FROM fruit"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """3 行 fruit（安全）+ 200 行 big（超过阈值 → 需要确认）。"""
    svc = DataService(AnalysisEngine(db_path=None))
    svc.engine.register_table(
        "fruit", pd.DataFrame({"name": ["apple", "banana", "cherry"], "price": [5, 7, 4]})
    )
    svc.engine.register_table("big", pd.DataFrame({"x": range(200)}))
    ctx = AppContext(
        service=svc,
        catalog=MetricCatalog(
            skills_dir=tmp_path / "skills",
            user_metrics_path=tmp_path / "user_metrics.md",
        ),
    )
    monkeypatch.setattr(app_context, "_context", ctx)
    monkeypatch.setattr(server, "_sessions", SessionStore())
    monkeypatch.setattr(server, "_pending", {})
    monkeypatch.setattr(server, "_checkpointer", MemorySaver())
    # 把"大表"阈值压到 50 行，让 200 行的 big 触发 R1
    monkeypatch.setattr(config, "HITL_MAX_SCAN_ROWS", 50)
    return ctx


@pytest.fixture
def client(ctx):
    return TestClient(server.app)


def _spy_executed(monkeypatch, ctx) -> list[str]:
    """记录真正被执行的 SQL（spy 在 DataService.query 上）。"""
    executed: list[str] = []
    original = ctx.service.query

    def spy(sql: str):
        executed.append(sql)
        return original(sql)

    # 实例属性盖住类方法别名；ctx 是 per-test 的，不需要还原
    ctx.service.query = spy
    return executed


def _chat(client, message: str, session: str = "s1") -> list[dict]:
    with client.stream(
        "POST", "/api/chat", json={"session_id": session, "message": message}
    ) as resp:
        assert resp.status_code == 200
        return parse_sse_lines(resp.iter_lines())


def _approve(client, thread_id: str, decision: str = "approve", note: str = "") -> list[dict]:
    with client.stream(
        "POST", "/api/approve",
        json={"thread_id": thread_id, "decision": decision, "note": note},
    ) as resp:
        assert resp.status_code == 200
        return parse_sse_lines(resp.iter_lines())


# ── 拦截 ─────────────────────────────────────────────────────────────

class TestRiskyQueryIsIntercepted:
    def test_risky_sql_asks_for_approval_and_does_not_execute(self, client, ctx, monkeypatch):
        executed = _spy_executed(monkeypatch, ctx)
        ctx.sql_llm = FakeLLM(RISKY_SQL)
        ctx.chat_llm = FakeChat(
            tool_resp("query_sql", '{"question": "把 big 全拿出来"}', "c1"),
            final_resp("不该走到这里"),
        )

        events = _chat(client, "把 big 全拿出来")

        approval = [e for e in events if e["type"] == "approval_required"]
        assert len(approval) == 1, "高风险查询必须弹确认"
        payload = approval[0]
        assert payload["thread_id"]
        query = payload["queries"][0]
        assert query["sql"] == RISKY_SQL
        assert query["estimated_rows"] == 200
        assert any("200" in r for r in query["reasons"])
        assert any("SELECT *" in r for r in query["reasons"])
        # 关键：确认之前一条 SQL 都不能执行
        assert executed == []
        # 也没有答案（这一轮还没结束）
        assert not [e for e in events if e["type"] == "answer"]
        assert events[-1]["type"] == "done"

    def test_node_progress_fills_the_blank_between_decide_and_execute(self, client, ctx):
        """HITL 顺带修好了"决定调用 query_sql 之后一片空白"的体验。"""
        ctx.sql_llm = FakeLLM(RISKY_SQL)
        ctx.chat_llm = FakeChat(
            tool_resp("query_sql", '{"question": "全量"}', "c1"),
            final_resp("ok"),
        )
        events = _chat(client, "全量")
        nodes = [(e["node"], e["message"]) for e in events if e["type"] == "node"]
        assert nodes[0][0] == "react_agent"
        assert nodes[1][0] == "plan_sql"
        assert nodes[1][1].startswith("已生成查询计划")
        # guard 中断时不会有 node 事件（chunk 是 __interrupt__），
        # 由 approval_required 卡片承载
        assert [n for n, _ in nodes] == ["react_agent", "plan_sql"]


# ── 批准 ─────────────────────────────────────────────────────────────

class TestApprove:
    def test_approve_executes_the_exact_confirmed_sql(self, client, ctx, monkeypatch):
        executed = _spy_executed(monkeypatch, ctx)
        ctx.sql_llm = FakeLLM(RISKY_SQL)
        ctx.chat_llm = FakeChat(
            tool_resp("query_sql", '{"question": "全量"}', "c1"),
            final_resp("一共 200 行"),
        )
        events = _chat(client, "全量")
        thread_id = [e for e in events if e["type"] == "approval_required"][0]["thread_id"]

        resumed = _approve(client, thread_id, "approve")

        # 执行的正是卡片上那条 SQL（不是重新生成的另一条）
        assert executed == [RISKY_SQL]
        # 整个回合只调用了一次内层 LLM（plan 阶段），确认后不再生成
        assert len(ctx.sql_llm.calls) == 1
        assert [e for e in resumed if e["type"] == "answer"][0]["text"] == "一共 200 行"
        history = [e for e in resumed if e["type"] == "history"][0]["history"]
        assert [m["role"] for m in history] == ["user", "assistant"]
        assert history[0]["content"] == "全量"

    def test_reject_feeds_reason_back_to_agent(self, client, ctx):
        ctx.sql_llm = FakeLLM(RISKY_SQL)
        chat = FakeChat(
            tool_resp("query_sql", '{"question": "全量"}', "c1"),
            final_resp("好的，我改成只看前 10 行"),
        )
        ctx.chat_llm = chat
        events = _chat(client, "全量")
        thread_id = [e for e in events if e["type"] == "approval_required"][0]["thread_id"]

        resumed = _approve(client, thread_id, "reject", note="别全表扫，加 limit")

        # 恢复后 react_agent 收到的 tool 消息里带着拒绝理由
        tool_msgs = [m for m in chat.calls[-1] if m["role"] == "tool"]
        assert any("用户拒绝" in m["content"] for m in tool_msgs)
        assert any("别全表扫" in m["content"] for m in tool_msgs)
        # 被拒绝的调用在步骤卡片上要标红（ok=False），不能显示成成功查询
        rejected_cards = [e for e in resumed if e["type"] == "tool_call"]
        assert rejected_cards and rejected_cards[0]["ok"] is False

    def test_approve_is_single_use(self, client, ctx):
        ctx.sql_llm = FakeLLM(RISKY_SQL)
        ctx.chat_llm = FakeChat(
            tool_resp("query_sql", '{"question": "全量"}', "c1"),
            final_resp("done"),
        )
        events = _chat(client, "全量")
        thread_id = [e for e in events if e["type"] == "approval_required"][0]["thread_id"]

        _approve(client, thread_id, "approve")
        second = client.post("/api/approve", json={"thread_id": thread_id, "decision": "approve"})
        assert second.status_code == 409

    def test_unknown_thread_returns_409(self, client):
        resp = client.post("/api/approve", json={"thread_id": "nope", "decision": "approve"})
        assert resp.status_code == 409


# ── 零打扰 ───────────────────────────────────────────────────────────

class TestSafeQueryIsNotInterrupted:
    def test_safe_query_runs_without_approval(self, client, ctx):
        ctx.sql_llm = FakeLLM(SAFE_SQL)
        ctx.chat_llm = FakeChat(
            tool_resp("query_sql", '{"question": "几种水果"}', "c1"),
            final_resp("3 种"),
        )
        events = _chat(client, "几种水果")
        assert not [e for e in events if e["type"] == "approval_required"]
        assert [e for e in events if e["type"] == "answer"][0]["text"] == "3 种"

    def test_switch_off_restores_legacy_path(self, client, ctx, monkeypatch):
        """关掉 HITL：连大表也直接执行（可回滚的证明）。"""
        monkeypatch.setattr(config, "HITL_ENABLED", False)
        ctx.sql_llm = FakeLLM(RISKY_SQL)
        ctx.chat_llm = FakeChat(
            tool_resp("query_sql", '{"question": "全量"}', "c1"),
            final_resp("200 行"),
        )
        events = _chat(client, "全量")
        assert not [e for e in events if e["type"] == "approval_required"]
        assert [e for e in events if e["type"] == "answer"]


# ── 断线重连 / 并发 ──────────────────────────────────────────────────

class TestPendingLifecycle:
    def test_reconnect_replays_card_instead_of_new_turn(self, client, ctx):
        ctx.sql_llm = FakeLLM(RISKY_SQL)
        chat = FakeChat(
            tool_resp("query_sql", '{"question": "全量"}', "c1"),
            final_resp("ok"),
        )
        ctx.chat_llm = chat
        first = _chat(client, "全量")
        assert [e for e in first if e["type"] == "approval_required"]

        calls_before = len(chat.calls)
        second = _chat(client, "换个问题")   # 同一会话，还有待确认的审批

        replayed = [e for e in second if e["type"] == "approval_required"]
        assert len(replayed) == 1
        assert replayed[0]["thread_id"] == [e for e in first if e["type"] == "approval_required"][0]["thread_id"]
        # 没有新开一轮：外层模型没有再被调用
        assert len(chat.calls) == calls_before
        # 也没有写历史
        assert server._sessions.history("s1") == []

    def test_repair_after_approval_allows_next_turn(self, client, ctx):
        ctx.sql_llm = FakeLLM(RISKY_SQL)
        ctx.chat_llm = FakeChat(
            tool_resp("query_sql", '{"question": "全量"}', "c1"),
            final_resp("200 行"),
            final_resp("第二次回答"),
        )
        events = _chat(client, "全量")
        thread_id = [e for e in events if e["type"] == "approval_required"][0]["thread_id"]
        _approve(client, thread_id, "approve")

        next_turn = _chat(client, "再问一句")
        assert [e for e in next_turn if e["type"] == "answer"]
        assert not [e for e in next_turn if e["type"] == "approval_required"]


# ── 规则本身（纯函数，不经过 HTTP）───────────────────────────────────

class TestRiskRules:
    def test_star_without_where_is_risky(self, ctx):
        risk = assess_plan(ctx.service, "SELECT * FROM big", "", None, max_scan_rows=10**9)
        assert any("SELECT *" in r for r in risk.reasons)

    def test_count_star_is_not_star_projection(self, ctx):
        # count(*) 是最常见的安全查询，不能因为里面有 * 就弹确认
        risk = assess_plan(ctx.service, "SELECT count(*) AS n FROM big", "", None, max_scan_rows=10**9)
        assert risk.reasons == ()

    def test_large_scan_hits_threshold(self, ctx):
        risk = assess_plan(ctx.service, "SELECT sum(x) FROM big", "", None, max_scan_rows=100)
        assert any("200" in r for r in risk.reasons)

    def test_small_scan_passes(self, ctx):
        risk = assess_plan(ctx.service, "SELECT sum(price) FROM fruit", "", None, max_scan_rows=100)
        assert risk.reasons == ()

    def test_metric_deviation_flagged(self, ctx):
        metric = Metric(
            name="gmv", aliases=("gmv", "销售额"),
            sql="SELECT sum(price) AS gmv FROM fruit",
            tables=("fruit",), description="按 fruit 汇总",
        )
        risk = assess_plan(
            ctx.service, "SELECT sum(x) FROM big", "查一下 gmv", [metric],
            max_scan_rows=10**9,
        )
        assert risk.metric == "gmv"
        assert any("口径不一致" in r for r in risk.reasons)

    def test_remote_table_always_needs_approval(self, ctx, monkeypatch):
        monkeypatch.setattr(ctx.service, "is_remote", lambda t: t == "big")
        risk = assess_plan(ctx.service, "SELECT sum(x) FROM big", "", None, max_scan_rows=10**9)
        assert any("远端" in r for r in risk.reasons)
