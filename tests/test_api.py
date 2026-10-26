"""M5c/M6 测试：FastAPI + SSE 流式端点（M6 起走 ReAct 外层图）。

用 FastAPI 自带 TestClient 验证 SSE 帧格式、事件序列、会话多轮、
上传注册。内层 SQL LLM 注入 FakeLLM、外层调度 LLM 注入 FakeChat
（不 mock HTTP 层，测真 StreamingResponse）。
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
    """内层 SQL 模型：按顺序返回固定 SQL。"""
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, question, schema_text, error_hint=""):
        self.calls.append((question, schema_text, error_hint))
        return self.answers.pop(0) if self.answers else "SELECT 1"


class FakeChat:
    """外层 ReAct 调度模型：按序返回预设响应（最终答案或 tool_calls），
    并记录每轮收到的 messages（可断言历史回放与工具结果回喂）。"""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def __call__(self, messages, tools=None):
        self.calls.append(messages)
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


def final_resp(text: str) -> dict:
    """无 tool_calls 的最终答案响应。"""
    return {"content": text, "tool_calls": []}


def tool_resp(name: str, args: str = "{}", call_id: str = "call_1") -> dict:
    """携带一个工具调用的响应（openai 格式）。"""
    return {
        "content": "",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": args}}],
    }


@pytest.fixture
def client(monkeypatch):
    """每个测试独立的内存 service + fake 双模型 + 新会话表。"""
    svc = DataService(AnalysisEngine(db_path=None))
    svc.engine.register_table(
        "fruit",
        pd.DataFrame({"name": ["apple", "banana"], "price": [5, 3]}),
    )
    monkeypatch.setattr(server, "service", svc)
    monkeypatch.setattr(server, "llm", FakeLLM("SELECT count(*) AS n FROM fruit"))
    # 默认外层模型直接给出最终答案（测试工具链路时再换多轮 fake）
    monkeypatch.setattr(server, "chat_llm", FakeChat(final_resp("共 1 行")))
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
        # 事件按 节点进度 → 答案 → 历史 → done 的顺序到达；
        # 外层模型直接给终答时 ReAct 图只跑一轮 react_agent
        with client.stream("POST", "/api/chat",
                           json={"session_id": "s1", "message": "有几种水果"}) as resp:
            events = parse_sse_lines(resp.iter_lines())

        assert events[0]["type"] == "start"
        node_events = [e for e in events if e["type"] == "node"]
        assert [e["node"] for e in node_events] == ["react_agent"]
        answer = [e for e in events if e["type"] == "answer"][0]
        assert "共 1 行" in answer["text"]
        assert events[-1]["type"] == "done"

    def test_tool_roundtrip_in_api(self, client, monkeypatch):
        # 外层先调 query_sql 工具、拿到结果再给终答：
        # 节点事件应为 react_agent → tool_execute → react_agent 三轮
        chat = FakeChat(
            tool_resp("query_sql", '{"question": "多少种水果"}', "call_x"),
            final_resp("有 2 种水果"),
        )
        monkeypatch.setattr(server, "chat_llm", chat)
        with client.stream("POST", "/api/chat",
                           json={"session_id": "s1", "message": "多少种水果"}) as resp:
            events = parse_sse_lines(resp.iter_lines())

        node_events = [e for e in events if e["type"] == "node"]
        assert [e["node"] for e in node_events] == [
            "react_agent", "tool_execute", "react_agent",
        ]
        answer = [e for e in events if e["type"] == "answer"][0]
        assert answer["text"] == "有 2 种水果"
        # 第二轮决策时，工具结果已经作为 tool 消息回喂给外层模型
        second_round_msgs = chat.calls[1]
        tool_msg = [m for m in second_round_msgs if m["role"] == "tool"][0]
        assert tool_msg["name"] == "query_sql"
        assert "共 1 行" in tool_msg["content"]

    def test_no_tables_gives_error_answer(self, client, monkeypatch):
        # 空库：外层会先调 list_tables 得知无表，再给出友好回答
        empty = DataService(AnalysisEngine(db_path=None))
        chat = FakeChat(
            tool_resp("list_tables"),
            final_resp("当前没有已加载的数据表，请先上传文件。"),
        )
        monkeypatch.setattr(server, "service", empty)
        monkeypatch.setattr(server, "chat_llm", chat)
        with client.stream("POST", "/api/chat",
                           json={"session_id": "s1", "message": "随便问"}) as resp:
            events = parse_sse_lines(resp.iter_lines())
        answer = [e for e in events if e["type"] == "answer"][0]
        assert "上传文件" in answer["text"]

    def test_chart_event_streamed_once(self, client, monkeypatch):
        # query_sql → plot_chart → 终答：SSE 应恰好一个 chart 事件
        # （state.charts 在每个节点 update 里都带全量，差分后不重复）
        chat = FakeChat(
            tool_resp("query_sql", '{"question": "各水果总价"}', "c1"),
            tool_resp("plot_chart", '{"chart_type": "bar", "title": "总价对比"}', "c2"),
            final_resp("图表见上，banana 总价最高。"),
        )
        monkeypatch.setattr(server, "chat_llm", chat)
        monkeypatch.setattr(
            server, "llm",
            FakeLLM("SELECT name, sum(price) AS total FROM fruit GROUP BY name"),
        )
        with client.stream("POST", "/api/chat",
                           json={"session_id": "s1", "message": "统计总价并画图"}) as resp:
            events = parse_sse_lines(resp.iter_lines())

        chart_events = [e for e in events if e["type"] == "chart"]
        assert len(chart_events) == 1
        assert chart_events[0]["image"].startswith("data:image/png;base64,")
        answer = [e for e in events if e["type"] == "answer"][0]
        assert "banana" in answer["text"]
        # 图表事件在最终答案之前到达（前端先看图再看结论）
        chart_idx = events.index(chart_events[0])
        answer_idx = events.index([e for e in events if e["type"] == "answer"][0])
        assert chart_idx < answer_idx

    def test_tool_call_event_carries_args_and_result(self, client, monkeypatch):
        # 工具调用应以 tool_call 事件推送：含工具名、原始参数、结果摘要
        chat = FakeChat(
            tool_resp("query_sql", '{"question": "多少种水果"}', "c1"),
            final_resp("有 2 种水果"),
        )
        monkeypatch.setattr(server, "chat_llm", chat)
        with client.stream("POST", "/api/chat",
                           json={"session_id": "s1", "message": "多少种水果"}) as resp:
            events = parse_sse_lines(resp.iter_lines())

        tool_events = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_events) == 1
        ev = tool_events[0]
        assert ev["name"] == "query_sql"
        assert "多少种水果" in ev["arguments"]  # 参数原文（含子问题）
        assert ev["ok"] is True
        assert "共 1 行" in ev["result"]        # 工具结果摘要

    def test_tool_events_not_lost_across_cycles(self, client, monkeypatch):
        # 跨轮回归护栏：两轮工具调用必须都送达。
        # state 是全量替换语义，react_agent 若不原样携带 tool_events，
        # 第一轮累积的事件会被整轮清空（与 steps 计数同一个坑）
        chat = FakeChat(
            tool_resp("list_tables", "{}", "c1"),
            tool_resp("query_sql", '{"question": "多少种水果"}', "c2"),
            final_resp("完成"),
        )
        monkeypatch.setattr(server, "chat_llm", chat)
        with client.stream("POST", "/api/chat",
                           json={"session_id": "s1", "message": "先看表再统计"}) as resp:
            events = parse_sse_lines(resp.iter_lines())

        names = [e["name"] for e in events if e["type"] == "tool_call"]
        assert names == ["list_tables", "query_sql"]

    def test_failed_tool_marked_not_ok(self, client, monkeypatch):
        # 工具失败（未知工具名）：ok=false，前端据此标红
        chat = FakeChat(
            tool_resp("drop_table", "{}", "c1"),
            final_resp("无法执行该操作"),
        )
        monkeypatch.setattr(server, "chat_llm", chat)
        with client.stream("POST", "/api/chat",
                           json={"session_id": "s1", "message": "删表"}) as resp:
            events = parse_sse_lines(resp.iter_lines())

        ev = [e for e in events if e["type"] == "tool_call"][0]
        assert ev["ok"] is False
        assert "未知工具" in ev["result"]

    def test_node_message_carries_real_decision(self, client, monkeypatch):
        # 「思考」行的内容换成真实决策，不再是"正在分析决策…"占位文案。
        # tool_execute 步没有单独文案（空串），其真实内容由卡片承载
        chat = FakeChat(
            tool_resp("query_sql", '{"question": "多少种水果"}', "c1"),
            final_resp("有 2 种水果"),
        )
        monkeypatch.setattr(server, "chat_llm", chat)
        with client.stream("POST", "/api/chat",
                           json={"session_id": "s1", "message": "多少种水果"}) as resp:
            events = parse_sse_lines(resp.iter_lines())

        msgs = [e["message"] for e in events if e["type"] == "node"]
        # 第一轮（react_agent，决定调工具）：真实决策
        assert msgs[0] == "决定调用 query_sql"
        # 后续节点没有独立文案：tool_execute 由卡片承载，
        # 收尾轮由答案气泡承载
        assert all(m == "" for m in msgs[1:])
        # 占位文案已彻底消失
        assert "正在分析决策…" not in msgs
        assert "正在查询数据…" not in msgs


class TestSession:
    def test_multi_turn_history_persists(self, client, monkeypatch):
        # 同一 session_id 连问两轮：第二轮后端把第一轮历史喂给外层模型
        chat = FakeChat(final_resp("答一"), final_resp("答二"))
        monkeypatch.setattr(server, "chat_llm", chat)

        with client.stream("POST", "/api/chat",
                           json={"session_id": "sess", "message": "第一问"}) as resp:
            events1 = parse_sse_lines(resp.iter_lines())
        with client.stream("POST", "/api/chat",
                           json={"session_id": "sess", "message": "再按价格平均一下"}) as resp:
            events2 = parse_sse_lines(resp.iter_lines())

        # 第二轮的 history 事件里包含第一轮的问答
        hist_event = [e for e in events2 if e["type"] == "history"][0]
        assert len(hist_event["history"]) == 4

        # 外层模型第二次被调用时的消息列表带上了第一轮问答回放
        msgs2 = chat.calls[1]
        users = [m["content"] for m in msgs2 if m["role"] == "user"]
        assert "第一问" in users
        assert "再按价格平均一下" in users
        assistants = [m["content"] for m in msgs2 if m["role"] == "assistant"]
        assert "答一" in assistants  # 上一轮答案作为历史回放

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

    def test_retry_dedupes_history(self, client):
        # 第一次请求完成 → 历史 2 条；断线重发同消息带 retry → 剥掉后仍 2 条
        body = {"session_id": "s-retry", "message": "查一下水果"}
        client.post("/api/chat", json=body)
        resp2 = client.post("/api/chat", json={**body, "retry": True})
        assert len(self._hist(resp2)) == 2

    def test_same_question_without_retry_not_deduped(self, client):
        # 用户主动连问两次相同的问题（retry=false）→ 正常追加到 4 条
        body = {"session_id": "s-dup", "message": "查一下水果"}
        client.post("/api/chat", json=body)
        resp2 = client.post("/api/chat", json=body)
        assert len(self._hist(resp2)) == 4


class TestConnectMysql:
    """MySQL 接入端点：用 SQLite 冒充远端库验证路由，不依赖真实 MySQL。"""

    @staticmethod
    def _fake_remote():
        """建一个 SQLite 内存库当"远端"：MySQLSource 有 sqlite 方言分支。

        StaticPool 保证多次 connect() 共享同一个内存库——否则 sqlite://
        每次连接都是全新空库。用 engine.begin() 而不是 connect()：
        SQLAlchemy 2.0 下 connect() 不自动提交，退出时 INSERT 会被回滚
        （表在但 0 行，会把冷热判定带偏）。
        """
        from sqlalchemy import create_engine, text
        from sqlalchemy.pool import StaticPool

        eng = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        with eng.begin() as conn:
            conn.execute(text("CREATE TABLE remote_sales (region TEXT, amount INTEGER)"))
            conn.execute(text("INSERT INTO remote_sales VALUES ('east', 100)"))
            conn.execute(text("INSERT INTO remote_sales VALUES ('west', 200)"))
        return eng

    def test_hot_table_moved_into_local_engine(self, client, monkeypatch):
        # 小表（2 行 ≤ 默认阈值）应被搬进本地 DuckDB，并出现在表清单里
        monkeypatch.setattr(server, "build_mysql_engine",
                            lambda url: self._fake_remote())
        resp = client.post("/api/connect_mysql",
                           json={"url": "mysql+pymysql://u:p@h:3306/db"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["moved"] == ["remote_sales"]
        assert data["large"] == []
        assert "remote_sales" in data["tables"]

    def test_lazy_limit_forces_cold_routing(self, client, monkeypatch):
        # 阈值压到 1 行：2 行的表超过阈值 → 留远端下推，不搬本地
        monkeypatch.setattr(server, "build_mysql_engine",
                            lambda url: self._fake_remote())
        resp = client.post("/api/connect_mysql",
                           json={"url": "mysql+pymysql://u:p@h:3306/db",
                                 "lazy_limit": 1})
        assert resp.status_code == 200
        data = resp.json()
        assert data["moved"] == []
        assert data["large"] == ["remote_sales"]

    def test_bad_connection_returns_400(self, client, monkeypatch):
        # 连不上（密码错/库不存在）应是 400 业务错误，而不是 500
        def boom(url):
            raise RuntimeError("Access denied for user")

        monkeypatch.setattr(server, "build_mysql_engine", boom)
        resp = client.post("/api/connect_mysql",
                           json={"url": "mysql+pymysql://bad:bad@h:3306/db"})
        assert resp.status_code == 400
        assert "连接失败" in resp.json()["detail"]

    def test_mask_url_never_leaks_password(self):
        masked = server._mask_url("mysql+pymysql://root:secret@127.0.0.1:3306/stdw")
        assert "secret" not in masked
        assert "root" in masked and "127.0.0.1" in masked