"""P0 硬伤修复的回归测试（2026-10-30）。

每个类对应一条 P0，测的是"修复前一定会失败 / 修复后必须成立"的行为，
而不是实现细节：

  P0-1  单进程双入口：/mcp 挂在 FastAPI 上，与 HTTP 共用同一个 service；
        DuckDB 文件锁冲突时给人话提示而不是英文 IOException。
  P0-2  无 key 也能 import / 起服务；只有真正要调 LLM 时才报错。
  P0-3  SSE 用同步生成器（线程池迭代），慢请求不阻塞事件循环。
  P0-4  上传超限在读盘过程中就 413，不会先把文件读进内存。
  P0-5  超时用 conn.interrupt() 真打断，且锁被释放（下一条查询不被拖死）。
  P0-6  用户级指标写一次，Web 与 MCP 都立刻可见（同一份 catalog）。
"""
from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api.server as server
import app_context
import config
from api.sessions import SessionStore
from app_context import AppContext, LLMNotConfiguredError, MetricCatalog
from data.engine import AnalysisEngine, QueryError
from data.metrics import Metric
from data.service import DataService
from tests.test_api import FakeChat, FakeLLM, final_resp, tool_resp

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    # 走白名单里的本机 Host（防 DNS rebinding 的校验不能关）
    "Host": "127.0.0.1:8000",
}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """内存引擎 + 空技能目录 + 临时用户指标文件。"""
    svc = DataService(AnalysisEngine(db_path=None))
    svc.engine.register_table(
        "fruit", pd.DataFrame({"name": ["apple", "banana"], "price": [5, 3]})
    )
    ctx = AppContext(
        service=svc,
        catalog=MetricCatalog(
            skills_dir=tmp_path / "skills",
            user_metrics_path=tmp_path / "user_metrics.md",
        ),
    )
    monkeypatch.setattr(app_context, "_context", ctx)
    return ctx


@pytest.fixture
def client(ctx, monkeypatch):
    ctx.sql_llm = FakeLLM("SELECT count(*) AS n FROM fruit")
    ctx.chat_llm = FakeChat(final_resp("共 1 行"))
    monkeypatch.setattr(server, "_sessions", SessionStore())
    return TestClient(server.app)


# ── P0-1 ─────────────────────────────────────────────────────────────

class TestP01SingleProcessMcp:
    def test_mcp_mounted_and_shares_context(self, ctx):
        """/mcp 与 /api 在同一进程：MCP 的 list_tables 能看到 HTTP 上传的表。"""
        with TestClient(server.app) as c:
            init = c.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "pytest", "version": "0"}},
            }, headers=_MCP_HEADERS)
            assert init.status_code == 200, init.text

            tools = c.post("/mcp", json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
            }, headers=_MCP_HEADERS)
            assert tools.status_code == 200
            assert "query_sql" in tools.text

            call = c.post("/mcp", json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "list_tables", "arguments": {}},
            }, headers=_MCP_HEADERS)
            assert call.status_code == 200
            # 表来自测试 ctx 的内存引擎 → 证明 MCP 工具跟 HTTP 用的是同一个上下文
            assert "fruit" in call.text

    def test_duckdb_lock_error_is_humanized(self, monkeypatch, tmp_path):
        """文件被另一个进程占用时，报中文可照做的错误，而不是 IOException。"""
        import duckdb

        def boom(*args, **kwargs):
            raise duckdb.IOException(
                'IO Error: Cannot open file "analysis.duckdb": 另一个程序正在使用此文件'
            )

        monkeypatch.setattr(duckdb, "connect", boom)
        with pytest.raises(QueryError, match="另一个进程"):
            AnalysisEngine(db_path=str(tmp_path / "analysis.duckdb"))


# ── P0-2 ─────────────────────────────────────────────────────────────

class TestP02LazyStartup:
    def test_server_imports_without_api_key(self):
        """回归：旧实现在 import 期构造 OpenAI 客户端，没有 key 直接崩。

        用子进程验证"干净进程"的 import 行为（当前进程早就 import 过了）。
        """
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        env["DATA_AGENT_DB"] = ":memory:"
        proc = subprocess.run(
            [sys.executable, "-c", "import api.server; print('IMPORT_OK')"],
            cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-1500:]
        assert "IMPORT_OK" in proc.stdout

    def test_missing_key_raises_friendly_error(self, ctx, monkeypatch):
        """懒加载：没有 key 时不是 import 崩，而是调用时给人话错误。"""
        monkeypatch.setattr(config, "DEEPSEEK_API_KEY", None)
        with pytest.raises(LLMNotConfiguredError, match="DEEPSEEK_API_KEY"):
            ctx.ensure_sql_llm()

    def test_non_llm_endpoints_work_without_key(self, client, ctx, monkeypatch):
        """/api/tables、上传等不依赖 LLM 的能力，没 key 也必须可用。"""
        monkeypatch.setattr(config, "DEEPSEEK_API_KEY", None)
        assert client.get("/api/tables").status_code == 200


# ── P0-3 ─────────────────────────────────────────────────────────────

class TestP03EventLoopNotBlocked:
    def test_chat_endpoint_and_stream_are_sync(self):
        """同步生成器才会被 Starlette 丢进线程池迭代（异步生成器会占住事件循环）。"""
        assert not inspect.iscoroutinefunction(server.chat)
        assert inspect.isgeneratorfunction(server._chat_stream)

    def test_tables_responds_while_chat_is_running(self, ctx, monkeypatch):
        """一个慢分析在跑时，/api/tables 必须立刻响应（并发 > 1）。"""

        def slow_chat(messages, tools=None):
            time.sleep(1.0)
            return {"content": "慢答案", "tool_calls": []}

        ctx.chat_llm = slow_chat
        ctx.sql_llm = FakeLLM("SELECT count(*) AS n FROM fruit")
        monkeypatch.setattr(server, "_sessions", SessionStore())

        async def scenario() -> float:
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
                async def run_chat():
                    async with ac.stream(
                        "POST", "/api/chat",
                        json={"session_id": "slow", "message": "慢慢来"},
                    ) as resp:
                        async for _ in resp.aiter_lines():
                            pass

                task = asyncio.create_task(run_chat())
                await asyncio.sleep(0.15)          # 让 chat 先进到阻塞调用里
                t0 = time.monotonic()
                resp = await ac.get("/api/tables")
                elapsed = time.monotonic() - t0
                assert resp.status_code == 200
                await task                         # 等它正常收尾（不留孤儿线程）
                return elapsed

        elapsed = asyncio.run(scenario())
        # 慢查询要 1s：若事件循环被阻塞，这里会等 ~0.85s 以上
        assert elapsed < 0.5, f"/api/tables 被阻塞了 {elapsed:.2f}s"

    def test_same_session_requests_are_serialized(self, ctx, monkeypatch):
        """同一会话两问并发：锁保证历史不互相覆盖（4 条而不是 2/3 条）。"""
        ctx.chat_llm = FakeChat(final_resp("答一"), final_resp("答二"))
        ctx.sql_llm = FakeLLM("SELECT count(*) AS n FROM fruit")
        monkeypatch.setattr(server, "_sessions", SessionStore())

        async def scenario():
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
                await asyncio.gather(
                    ac.post("/api/chat", json={"session_id": "s", "message": "问一"}),
                    ac.post("/api/chat", json={"session_id": "s", "message": "问二"}),
                )
            return server._sessions.history("s")

        history = asyncio.run(scenario())
        assert len(history) == 4
        assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]


# ── P0-4 ─────────────────────────────────────────────────────────────

class TestP04UploadLimit:
    def test_oversized_upload_rejected_with_413(self, client, monkeypatch):
        monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 100)
        payload = b"a,b\n" + b"1,2\n" * 200          # ~800 字节 > 100
        resp = client.post(
            "/api/upload", files={"file": ("big.csv", payload, "text/csv")}
        )
        assert resp.status_code == 413
        assert "大小限制" in resp.json()["detail"]

    def test_small_upload_still_works(self, client, monkeypatch):
        monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10_000)
        resp = client.post(
            "/api/upload",
            files={"file": ("tiny.csv", b"id,amount\n1,100\n", "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["tables"] == ["tiny"]


# ── P0-5 ─────────────────────────────────────────────────────────────

class TestP05TimeoutInterrupt:
    def test_timeout_interrupts_and_releases_lock(self):
        """超时必须真打断：否则被放弃的查询线程仍持锁，后续查询全排队。"""
        eng = AnalysisEngine(db_path=None)
        try:
            eng.register_table("t", pd.DataFrame({"x": range(10)}))
            t0 = time.monotonic()
            with pytest.raises(QueryError, match="未完成"):
                # 2 万 × 2 万 笛卡尔积≈3s：0.2s 必然超时
                eng.execute_query(
                    "SELECT sum(a.range*b.range) FROM range(20000) a, range(20000) b",
                    timeout_s=0.2,
                )
            assert time.monotonic() - t0 < 3.0     # 不能等查询自己跑完

            t1 = time.monotonic()
            df = eng.execute_query("SELECT count(*) AS n FROM t")
            assert int(df.iloc[0]["n"]) == 10
            assert time.monotonic() - t1 < 2.0     # 锁已释放，下一条不等
        finally:
            eng.close()


# ── P0-6 ─────────────────────────────────────────────────────────────

class TestP06UnifiedRegistry:
    def test_catalog_merges_skills_and_user_metrics(self, tmp_path):
        skill_dir = tmp_path / "skills" / "demo"
        (skill_dir / "references").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: 演示技能\n---\n\n正文",
            encoding="utf-8",
        )
        (skill_dir / "references" / "metrics.md").write_text(
            "## demo_metric\n- 别名: 演示指标\n- 责任表: fruit\n"
            "- 口径SQL: SELECT 1 FROM fruit\n- 说明: 演示口径\n",
            encoding="utf-8",
        )
        user_file = tmp_path / "user_metrics.md"
        catalog = MetricCatalog(skills_dir=tmp_path / "skills",
                                user_metrics_path=user_file)
        assert [m.name for m in catalog.registry] == ["demo_metric"]

        catalog.save_user_metric(Metric(
            name="vip", aliases=("高价值客户",), sql="SELECT 2 FROM fruit",
            tables=("fruit",), description="用户口径",
        ))
        assert {m.name for m in catalog.registry} == {"demo_metric", "vip"}
        # 重启后（新实例读同一文件）依然在 → 跨会话沿用
        again = MetricCatalog(skills_dir=tmp_path / "skills",
                              user_metrics_path=user_file)
        assert {m.name for m in again.registry} == {"demo_metric", "vip"}

    def test_metric_saved_via_mcp_is_used_by_web_chat(self, ctx, client):
        """MCP 保存的口径，Web 端下一次提问就能命中（旧版各读各的，命中不了）。"""
        ctx.catalog.save_user_metric(Metric(
            name="vip",
            aliases=("高价值客户",),
            sql="SELECT count(*) AS n FROM fruit WHERE price > 3",
            tables=("fruit",),
            description="单价大于 3 的客户",
        ))
        ctx.sql_llm = FakeLLM("SELECT count(*) AS n FROM fruit")
        ctx.chat_llm = FakeChat(
            tool_resp("query_sql", '{"question": "高价值客户"}', "c1"),
            final_resp("1 个"),
        )

        resp = client.post("/api/chat",
                           json={"session_id": "s", "message": "有几个高价值客户"})
        assert resp.status_code == 200

        # 内层 SQL 生成器收到的 schema 上下文里必须带上了用户级口径
        schema_text = ctx.sql_llm.calls[0][1]
        assert "SELECT count(*) AS n FROM fruit WHERE price > 3" in schema_text
        assert "高价值客户" in schema_text or "vip" in schema_text
