"""M5c 测试：FastAPI + SSE 流式端点。

用 FastAPI 自带 TestClient 验证 SSE 帧格式、事件序列、会话多轮、
上传注册。llm 用 FakeLLM 注入（不 mock HTTP 层，测真 StreamingResponse）。
"""
from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api.server as server
from data.engine import AnalysisEngine
from data.service import DataService


class FakeLLM:
    """按顺序返回固定 SQL。"""
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, question, schema_text, error_hint=""):
        self.calls.append((question, schema_text, error_hint))
        return self.answers.pop(0) if self.answers else "SELECT 1"


@pytest.fixture
def client(monkeypatch):
    """每个测试独立的内存 service + FakeLLM + 新会话表。"""
    svc = DataService(AnalysisEngine(db_path=None))
    svc.engine.register_table(
        "fruit",
        pd.DataFrame({"name": ["apple", "banana"], "price": [5, 3]}),
    )
    monkeypatch.setattr(server, "service", svc)
    monkeypatch.setattr(server, "llm", FakeLLM("SELECT count(*) AS n FROM fruit"))
    monkeypatch.setattr(server, "_sessions", {})
    return TestClient(server.app)


def parse_sse_lines(lines) -> list[dict]:
    """把 SSE 文本行解析成事件对象列表（跳过空行，剥离 data: 前缀）。"""
    events = []
    for line in lines:
        line = line.strip()
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


class TestSseChat:
    def test_content_type_is_event_stream(self, client):
        with client.stream("POST", "/api/chat",
                           json={"session_id": "s1", "message": "有几种水果"}) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")

    def test_event_sequence(self, client):
        # 事件按 节点进度 → 答案 → 历史 → done 的顺序到达
        with client.stream("POST", "/api/chat",
                           json={"session_id": "s1", "message": "有几种水果"}) as resp:
            events = parse_sse_lines(resp.iter_lines())

        assert events[0]["type"] == "start"
        node_events = [e for e in events if e["type"] == "node"]
        # 四节点全部出现一次（无重试时）
        assert [e["node"] for e in node_events] == [
            "collect_schema", "generate_sql", "validate_sql", "execute",
        ]
        answer = [e for e in events if e["type"] == "answer"][0]
        assert "共 1 行" in answer["text"]
        assert events[-1]["type"] == "done"

    def test_no_tables_gives_error_answer(self, client, monkeypatch):
        # 空库：answer 是友好错误而不是 500
        empty = DataService(AnalysisEngine(db_path=None))
        monkeypatch.setattr(server, "service", empty)
        with client.stream("POST", "/api/chat",
                           json={"session_id": "s1", "message": "随便问"}) as resp:
            events = parse_sse_lines(resp.iter_lines())
        answer = [e for e in events if e["type"] == "answer"][0]
        assert "上传文件" in answer["text"]


class TestSession:
    def test_multi_turn_history_persists(self, client, monkeypatch):
        # 同一 session_id 连问两轮：第二轮后端把第一轮历史喂给 LLM
        fake = FakeLLM("SELECT count(*) AS n FROM fruit",
                       "SELECT avg(price) AS a FROM fruit")
        monkeypatch.setattr(server, "llm", fake)

        with client.stream("POST", "/api/chat",
                           json={"session_id": "sess", "message": "第一问"}) as resp:
            events1 = parse_sse_lines(resp.iter_lines())
        with client.stream("POST", "/api/chat",
                           json={"session_id": "sess", "message": "再按价格平均一下"}) as resp:
            events2 = parse_sse_lines(resp.iter_lines())

        # 第二轮的 history 事件里包含第一轮的问答
        hist_event = [e for e in events2 if e["type"] == "history"][0]
        assert len(hist_event["history"]) == 4

        # LLM 第二次被调用时，question 里带上了第一轮的历史上下文
        q2 = fake.calls[1][0]
        assert "此前的对话" in q2
        assert "第一问" in q2

    def test_different_sessions_isolated(self, client):
        # 不同 session_id 的 history 互不可见
        with client.stream("POST", "/api/chat",
                           json={"session_id": "A", "message": "问题A"}) as resp:
            events_a = parse_sse_lines(resp.iter_lines())
        with client.stream("POST", "/api/chat",
                           json={"session_id": "B", "message": "问题B"}) as resp:
            events_b = parse_sse_lines(resp.iter_lines())
        hist_a = [e for e in events_a if e["type"] == "history"][0]
        hist_b = [e for e in events_b if e["type"] == "history"][0]
        assert "问题A" in hist_a["history"][0]["content"]
        assert "问题B" in hist_b["history"][0]["content"]
        assert len(hist_b["history"]) == 2  # B 会话只看得到自己


class TestUpload:
    def test_upload_csv_registers_table(self, client, tmp_path):
        csv = tmp_path / "orders.csv"
        csv.write_text("id,amount\n1,100\n2,200\n", encoding="utf-8")
        with open(csv, "rb") as f:
            resp = client.post("/api/upload",
                               files={"file": ("orders.csv", f, "text/csv")})
        assert resp.status_code == 200
        assert resp.json()["tables"] == ["orders"]

    def test_upload_bad_extension_rejected(self, client, tmp_path):
        bad = tmp_path / "evil.exe"
        bad.write_bytes(b"MZ...")
        with open(bad, "rb") as f:
            resp = client.post("/api/upload",
                               files={"file": ("evil.exe", f, "application/octet-stream")})
        assert resp.status_code == 400

    def test_tables_endpoint(self, client):
        resp = client.get("/api/tables")
        assert resp.status_code == 200
        assert "fruit" in resp.json()["tables"]


class TestSSERetry:
    """断线重连：retry=true 重发同一消息时历史幂等去重。"""

    @staticmethod
    def _hist(resp) -> list[dict]:
        frames = [
            json.loads(l[6:])
            for l in resp.text.splitlines()
            if l.startswith("data: ")
        ]
        return frames[-2]["history"]  # 帧序：[... answer, history, done]

    def test_retry_dedupes_history(self, client, monkeypatch):
        # 第一次请求完成 → 历史 2 条；断线重发同消息带 retry → 剥掉后仍 2 条
        fake = FakeLLM(
            "SELECT count(*) AS n FROM fruit",
            "SELECT count(*) AS n FROM fruit",
        )
        monkeypatch.setattr(server, "llm", fake)
        body = {"session_id": "s-retry", "message": "查一下水果"}
        client.post("/api/chat", json=body)
        resp2 = client.post("/api/chat", json={**body, "retry": True})
        assert len(self._hist(resp2)) == 2

    def test_same_question_without_retry_not_deduped(self, client, monkeypatch):
        # 用户主动连问两次相同的问题（retry=false）→ 正常追加到 4 条
        fake = FakeLLM(
            "SELECT count(*) AS n FROM fruit",
            "SELECT count(*) AS n FROM fruit",
        )
        monkeypatch.setattr(server, "llm", fake)
        body = {"session_id": "s-dup", "message": "查一下水果"}
        client.post("/api/chat", json=body)
        resp2 = client.post("/api/chat", json=body)
        assert len(self._hist(resp2)) == 4