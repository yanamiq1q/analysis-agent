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


@pytest.fixture
def ctx(service, tmp_path, monkeypatch):
    """每个测试独立的运行时上下文：内存 service + 假 LLM + 临时指标目录。

    P0-2/P0-6 之后 MCP 工具从 AppContext 取依赖（不再有模块级
    ``service`` / ``_metric_registry`` 单例），所以测试改成注入上下文。
    """
    import app_context
    from app_context import AppContext, MetricCatalog

    ctx = AppContext(
        service=service,
        sql_llm=FakeLLM("SELECT * FROM fruit"),
        catalog=MetricCatalog(
            skills_dir=tmp_path / "skills",
            user_metrics_path=tmp_path / "user_metrics.md",
        ),
    )
    monkeypatch.setattr(app_context, "_context", ctx)
    return ctx


@pytest.fixture(autouse=True)
def _isolate_context(ctx):
    """autouse：让本模块每个测试都跑在自己的上下文里。"""
    return ctx


def _call(tool: str, **kwargs):
    """同步调 FastMCP 工具（call_tool 是 async，测试里用 asyncio.run 包一层）。

    v1.x 的 convert_result 返回 (content_blocks, structured_output) 二元组，
    取第一个元素（文本内容块列表）。
    """
    result = asyncio.run(mcp_server.mcp.call_tool(tool, kwargs or {}))
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
    def test_all_tools_readonly(self):
        tools = _list_tools()
        names = {t.name for t in tools}
        assert names == {
            "list_tables", "query_sql", "ask_data",
            "describe_table", "preview_table",
            "get_metric_definition", "load_file",
            "save_metric_definition",
            # PR2：报告任务（提交 / 轮询 / 模板）
            "list_report_templates", "build_report", "get_report", "get_report_spec",
        }
        for t in tools:
            # 描述必须非空：FastMCP 把 docstring 当 LLM 的 tool description
            assert t.description, f"{t.name} 缺少 description（docstring）"
            if t.name == "load_file":
                # 唯一"读外部文件"的写工具：宿主弹确认框
                assert t.annotations.readOnlyHint is False
                assert t.annotations.destructiveHint is True
                assert t.annotations.openWorldHint is True
            elif t.name in ("save_metric_definition", "build_report"):
                # 写工具但只动 Server 自己的持久化文件，不碰外部世界。
                # build_report 会往 report_jobs/ 写产物，同样属于 local write。
                assert t.annotations.readOnlyHint is False
                assert t.annotations.destructiveHint is True
                assert t.annotations.openWorldHint is False
            else:
                assert t.annotations.readOnlyHint is True
                assert t.annotations.openWorldHint is False


class TestListTables:
    def test_returns_table_names(self):
        text = _content_text(_call("list_tables"))
        assert "fruit" in text

    def test_empty_service_gives_hint(self, ctx):
        # 清空引擎：没有任何表时给出中文提示而不是报错
        ctx.service = DataService(AnalysisEngine(db_path=None))
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


class TestDescribeTable:
    def test_returns_schema_overview(self):
        text = _content_text(_call("describe_table", table_name="fruit"))
        assert "共 5 行" in text
        assert "| name |" in text
        assert "| price |" in text
        assert "唯一值" in text

    def test_unknown_table_rejected(self):
        with pytest.raises(ToolError, match="仅支持本地表"):
            _call("describe_table", table_name="ghost")


class TestPreviewTable:
    def test_returns_rows_as_table(self):
        text = _content_text(_call("preview_table", table_name="fruit"))
        assert "前 5 行" in text
        assert "| name | price |" in text
        assert "apple" in text

    def test_respects_max_rows(self):
        text = _content_text(_call("preview_table", table_name="fruit", max_rows=2))
        assert "前 2 行" in text
        assert "apple" in text
        assert "elderberry" not in text

    def test_invalid_max_rows_rejected(self):
        with pytest.raises(ToolError, match="1~50"):
            _call("preview_table", table_name="fruit", max_rows=999)

    def test_unknown_table_rejected(self):
        with pytest.raises(ToolError, match="表不存在"):
            _call("preview_table", table_name="ghost")


class TestGetMetricDefinition:
    @pytest.fixture(autouse=True)
    def _controlled_registry(self, ctx):
        """用受控指标替换真实解析结果（真实 metrics.md 依赖的表未加载会匹配不上）。"""
        from data.metrics import Metric

        gmv = Metric(
            name="gmv",
            aliases=("gmv", "销售额"),
            sql="SELECT sum(price) AS gmv FROM fruit",
            tables=("fruit",),
            description="按 fruit 汇总销售额",
        )
        ctx.catalog.registry = [gmv]

    def test_matches_metric(self):
        text = _content_text(_call("get_metric_definition", question="这个月的销售额"))
        assert "## gmv" in text
        assert "口径 SQL" in text
        assert "SELECT sum(price)" in text
        assert "责任表: fruit" in text
        assert "不得更换表" in text

    def test_no_match_lists_available(self):
        text = _content_text(_call("get_metric_definition", question="股票行情"))
        assert "未匹配到指标口径" in text
        assert "gmv" in text

    def test_empty_question_rejected(self):
        with pytest.raises(ToolError, match="不能为空"):
            _call("get_metric_definition", question="  ")


class TestLoadFile:
    def test_loads_csv_inside_data_dir(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        csv = tmp_path / "sales.csv"
        csv.write_text("city,amount\nbj,10\nsh,20\n", encoding="utf-8")
        text = _content_text(_call("load_file", file_path=str(csv)))
        assert "已注册 1 张表" in text
        assert "sales" in text
        assert "sales" in _content_text(_call("list_tables"))

    def test_rejects_path_outside_data_dir(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        outside = tmp_path.parent / "secret.csv"
        outside.write_text("a\n1\n", encoding="utf-8")
        with pytest.raises(ToolError, match="仅支持"):
            _call("load_file", file_path=str(outside))

    def test_empty_path_rejected(self):
        with pytest.raises(ToolError, match="不能为空"):
            _call("load_file", file_path=" ")


class TestSaveMetricDefinition:
    @pytest.fixture(autouse=True)
    def _isolated_user_file(self, ctx, tmp_path):
        # 写进 tmp 目录，避免污染真实 data/warehouse/user_metrics.md
        ctx.catalog.user_metrics_path = tmp_path / "user_metrics.md"

    def test_saves_and_applies_immediately(self):
        text = _content_text(_call(
            "save_metric_definition",
            name="vip_customer",
            sql="SELECT count(*) AS n FROM fruit WHERE price > 5",
            tables=["fruit"],
            aliases=["高价值客户"],
            description="单价超过 5 的客户",
        ))
        assert "已保存指标 vip_customer" in text
        # 写盘后本次运行内立即可用：口径查询能命中
        got = _content_text(_call("get_metric_definition", question="高价值客户"))
        assert "## vip_customer" in got
        assert "SELECT count(*)" in got
        assert "责任表: fruit" in got

    def test_upsert_overwrites_same_name(self, ctx):
        from agent.skill_loader import load_user_metrics

        _call("save_metric_definition", name="vip_customer",
              sql="SELECT 1 FROM fruit", tables=["fruit"])
        _call("save_metric_definition", name="vip_customer",
              sql="SELECT 2 FROM fruit", tables=["fruit"])
        metrics, diag = load_user_metrics(ctx.catalog.user_metrics_path)
        assert diag is None
        assert [m.name for m in metrics] == ["vip_customer"]
        assert metrics[0].sql == "SELECT 2 FROM fruit"

    def test_persists_on_disk_for_restart(self, ctx):
        from agent.skill_loader import load_user_metrics

        _call("save_metric_definition", name="vip_customer",
              sql="SELECT 1 FROM fruit", tables=["fruit"])
        metrics, diag = load_user_metrics(ctx.catalog.user_metrics_path)
        assert diag is None and len(metrics) == 1

    def test_rejects_unknown_table(self):
        with pytest.raises(ToolError, match="不在当前表集内"):
            _call("save_metric_definition", name="x",
                  sql="SELECT 1 FROM fruit", tables=["ghost"])

    def test_rejects_dangerous_sql(self):
        with pytest.raises(ToolError, match="口径 SQL 不合法"):
            _call("save_metric_definition", name="x",
                  sql="DROP TABLE fruit", tables=["fruit"])

    def test_rejects_empty_name(self):
        with pytest.raises(ToolError, match="不能为空"):
            _call("save_metric_definition", name=" ",
                  sql="SELECT 1 FROM fruit", tables=["fruit"])


class TestAskData:
    def test_returns_analysis_text(self):
        text = _content_text(_call("ask_data", question="最贵的水果"))
        # FakeLLM 返回 SELECT * FROM fruit → 结果文本包含数据行
        assert "共 5 行" in text
        assert "cherry" in text

    def test_empty_question_rejected(self):
        with pytest.raises(ToolError, match="不能为空"):
            _call("ask_data", question="")


class TestProductionGuards:
    """P1 生产级横切能力：字符兜底、错误人话、ask_data 超时。"""

    def test_short_text_not_truncated(self):
        assert mcp_server._truncate("abc") == "abc"

    def test_output_hits_char_limit(self, monkeypatch):
        # 把上限压到 30 字符，验证字符级兜底真的生效
        monkeypatch.setattr(mcp_server, "_MAX_OUTPUT_CHARS", 30)
        text = _content_text(_call("describe_table", table_name="fruit"))
        assert text.startswith("表 fruit")
        assert "响应已截断" in text

    def test_humanizes_missing_table(self):
        # service.describe 抛的中文 QueryError 原样透传，并带上动作前缀
        with pytest.raises(ToolError, match="查看表结构失败：describe 仅支持本地表"):
            _call("describe_table", table_name="ghost")

    def test_humanizes_english_column_error(self, ctx):
        # DuckDB 的英文 Binder Error 要被归类成"列名不存在"并指路 describe_table
        def boom(sql: str):
            raise RuntimeError(
                'Binder Error: Referenced column "ghost" not found in FROM clause!'
            )

        ctx.service.execute = boom
        with pytest.raises(ToolError, match="列名不存在") as exc_info:
            _call("query_sql", sql="SELECT ghost FROM fruit")
        assert "describe_table" in str(exc_info.value)

    def test_humanizes_missing_api_key(self, ctx):
        # 构造 LLM 客户端时就失败（没有 DEEPSEEK_API_KEY）：
        # 要翻译成"API Key 未配置或无效"，而不是把 SDK 英文报错丢给宿主
        def boom():
            raise RuntimeError("The api_key client option must be set")

        ctx.ensure_sql_llm = boom
        with pytest.raises(ToolError, match="API Key 未配置或无效"):
            _call("ask_data", question="最贵的水果")

    def test_ask_data_timeout(self, monkeypatch):
        import time

        # 直接替换 run_analysis 为纯 sleep：避免超时后遗留线程继续碰引擎
        def slow_analysis(*args, **kwargs):
            time.sleep(0.5)
            return {"result_text": "x"}

        monkeypatch.setattr(mcp_server, "run_analysis", slow_analysis)
        monkeypatch.setattr(mcp_server, "_ANALYSIS_TIMEOUT_SECONDS", 0.05)
        with pytest.raises(ToolError, match="分析超时"):
            _call("ask_data", question="最贵的水果")
