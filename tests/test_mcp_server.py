"""M9 测试：MCP Server 的 3 个工具（不调真实 LLM / 不连真实库）。"""
import asyncio

import pandas as pd
import pytest
from mcp.server.fastmcp.exceptions import ToolError

import mcp_server.server as mcp_server
from data.engine import AnalysisEngine
from data.service import DataService


class FakeLLM:
    """可编程的假内层 LLM：按序返回预设 SQL。"""

    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, question: str, schema_text: str, error_hint: str = "") -> str:
        self.calls.append((question, schema_text, error_hint))
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


@pytest.fixture
def service():
    """内存引擎 + 一张 5 行小表 fruit(name, price)。

    必须显式 db_path=None：默认引擎连持久化文件库，测试间互相污染。
    """
    svc = DataService(AnalysisEngine(db_path=None))
    df = pd.DataFrame(
        {"name": ["apple", "banana", "cherry", "durian", "elderberry"],
         "price": [5, 7, 4, 9, 6]}
    )
    svc.engine.register_table("fruit", df)
    return svc


@pytest.fixture(autouse=True)
def _isolate_singletons(monkeypatch, service):
    """把模块级单例替换成内存版：service 用临时引擎，LLM 用 FakeLLM。"""
    monkeypatch.setattr(mcp_server, "service", service)
    monkeypatch.setattr(mcp_server, "_get_llm", lambda: FakeLLM("SELECT * FROM fruit"))


def _call(name: str, **kwargs):
    """同步调 FastMCP 工具（call_tool 是 async，测试里用 asyncio.run 包一层）。

    v1.x 的 convert_result 返回 (content_blocks, structured_output) 二元组，
    取第一个元素（文本内容块列表）。
    """
    result = asyncio.run(mcp_server.mcp.call_tool(name, kwargs or {}))
    return result[0] if isinstance(result, tuple) else result


def _list_tools():
    """同步拿工具注册表（list_tools 在 v1.x 也是 async）。"""
    return asyncio.run(mcp_server.mcp.list_tools())


def _content_text(result) -> str:
    """从 call_tool 结果里取文本内容（单个 TextContent 时直接返回字符串）。"""
    if isinstance(result, str):
        return result
    parts = []
    for block in result:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts)


class TestRegistration:
    def test_exactly_three_readonly_tools(self):
        tools = _list_tools()
        names = {t.name for t in tools}
        assert names == {"list_tables", "query_sql", "ask_data"}
        # 全部只读、不访问外部世界（宿主可据此优化并发/缓存）
        for t in tools:
            assert t.annotations.readOnlyHint is True
            assert t.annotations.openWorldHint is False
        # 描述必须非空：FastMCP 把 docstring 当 LLM 的 tool description
        for t in tools:
            assert t.description, f"{t.name} 缺少 description（docstring）"


class TestListTables:
    def test_returns_table_names(self):
        text = _content_text(_call("list_tables"))
        assert "fruit" in text

    def test_empty_service_gives_hint(self, monkeypatch, service):
        # 清空引擎：没有任何表时给出中文提示而不是报错
        monkeypatch.setattr(mcp_server, "service", DataService(AnalysisEngine(db_path=None)))
        text = _content_text(_call("list_tables"))
        assert "没有已加载的数据表" in text


class TestQuerySql:
    def test_valid_query_returns_csv(self):
        text = _content_text(_call("query_sql", sql="SELECT name, price FROM fruit"))
        assert "name,price" in text
        assert "cherry" in text
        assert "共 5 行" in text

    def test_limit_truncates_with_note(self):
        text = _content_text(_call(
            "query_sql", sql="SELECT name FROM fruit", limit=2
        ))
        assert "共 5 行，已截断返回前 2 行" in text
        assert "apple" in text
        assert "elderberry" not in text

    def test_rejects_drop_statement(self):
        with pytest.raises(ToolError, match="只允许"):
            _call("query_sql", sql="DROP TABLE fruit")

    def test_rejects_multi_statement_injection(self):
        with pytest.raises(ToolError, match="多语句"):
            _call("query_sql", sql="SELECT 1; DROP TABLE fruit")

    def test_rejects_unregistered_table(self):
        with pytest.raises(ToolError, match="未注册"):
            _call("query_sql", sql="SELECT * FROM ghost")

    def test_rejects_empty_sql(self):
        with pytest.raises(ToolError, match="不能为空"):
            _call("query_sql", sql="  ")


class TestAskData:
    def test_returns_analysis_text(self):
        text = _content_text(_call("ask_data", question="最贵的水果"))
        # FakeLLM 返回 SELECT * FROM fruit → 结果文本包含数据行
        assert "共 5 行" in text
        assert "cherry" in text

    def test_empty_question_rejected(self):
        with pytest.raises(ToolError, match="不能为空"):
            _call("ask_data", question="")
