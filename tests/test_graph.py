"""M3 测试：LangGraph 主链路（用 fake LLM，不调真实 API）。"""
import pandas as pd
import pytest

from agent.graph import build_graph, run_analysis
from agent.llm import _extract_sql
from data.engine import AnalysisEngine
from data.metrics import Metric
from data.service import DataService


class FakeLLM:
    """可编程的假 LLM：按序返回预设 SQL，并记录每次调用参数。"""

    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, str]] = []  # (question, schema, error)

    def __call__(self, question: str, schema_text: str, error_hint: str = "") -> str:
        self.calls.append((question, schema_text, error_hint))
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


@pytest.fixture
def service():
    """内存引擎 + 一张 3 行的小表 fruit(name, price)。

    注意必须显式 db_path=None：默认引擎连的是持久化文件库
    (data/warehouse/analysis.duckdb)，测试间会互相污染。
    """
    svc = DataService(AnalysisEngine(db_path=None))
    df = pd.DataFrame({"name": ["apple", "banana", "cherry"], "price": [5, 7, 4]})
    svc.engine.register_table("fruit", df)
    return svc


class TestSuccessPath:
    def test_full_chain_returns_result(self, service):
        llm = FakeLLM("SELECT name, price FROM fruit ORDER BY price DESC")
        graph = build_graph(service, llm)
        state = graph.invoke({"question": "最贵的水果"})

        assert "共 3 行" in state["result_text"]
        assert "cherry" in state["result_text"]
        assert state["attempts"] == 1

    def test_schema_injected_to_llm(self, service):
        # 验证节点1 确实把表结构喂给了 LLM（这是 Agent 能写对 SQL 的前提）
        llm = FakeLLM("SELECT * FROM fruit")
        graph = build_graph(service, llm)
        graph.invoke({"question": "全部数据"})

        _, schema_text, _ = llm.calls[0]
        assert "fruit" in schema_text
        assert "name" in schema_text and "price" in schema_text

    def test_run_analysis_entry(self, service):
        # 便捷入口直接返回答案文本
        llm = FakeLLM("SELECT count(*) AS n FROM fruit")
        text = run_analysis(service, llm, "有多少种水果")["result_text"]
        assert "共 1 行" in text


class TestValidateRetry:
    def test_bad_sql_then_good_sql(self, service):
        # LLM 第一次生成非法 SQL（DELETE），校验拦截 → 重试 → 第二次成功
        llm = FakeLLM(
            "DELETE FROM fruit",
            "SELECT name FROM fruit WHERE price > 4",
        )
        graph = build_graph(service, llm)
        state = graph.invoke({"question": "价格大于4的水果"})

        assert state["attempts"] == 2
        assert "apple" in state["result_text"]
        # 第二次调用时，LLM 应该收到第一次的错误提示
        assert "只允许" in llm.calls[1][2]
        # P1 修复：重试时上次生成的 SQL 也要回喂，否则 LLM 会重复输出坏 SQL
        assert "DELETE FROM fruit" in llm.calls[1][2]

    def test_retry_exhausted(self, service):
        # LLM 一直输出非法 SQL，重试耗尽后给出错误答案而不是死循环
        llm = FakeLLM("DELETE FROM fruit")
        graph = build_graph(service, llm)
        state = graph.invoke({"question": "删除数据"})

        assert "生成 SQL 失败" in state["result_text"]
        assert state["attempts"] == 3  # 最多试 3 次


class TestExecuteRetry:
    def test_execute_error_triggers_retry(self, service):
        # 校验通过但执行失败（列名拼错）→ 也走重试修正
        llm = FakeLLM(
            "SELECT wrong_column FROM fruit",          # 执行会报错
            "SELECT name FROM fruit",                  # 修正后成功
        )
        graph = build_graph(service, llm)
        state = graph.invoke({"question": "名称列表"})

        assert state["attempts"] == 2
        assert "apple" in state["result_text"]


class TestNoTables:
    def test_no_tables_short_circuit(self, service):
        # 空库：collect_schema 直接结束，不调 LLM
        empty = DataService(AnalysisEngine(db_path=None))  # 全新内存引擎，无表
        llm = FakeLLM("SELECT 1")
        text = run_analysis(empty, llm, "随便问问")["result_text"]
        assert "上传文件" in text
        assert llm.calls == []  # LLM 一次都没被调用


class TestMultiTurn:
    """M5b：多轮对话（历史由 run_analysis 返回值携带）。"""

    def test_history_passed_to_llm(self, service):
        # 第二轮 invoke：LLM 收到的 question 里含上一轮问答的上下文
        history = [
            {"role": "user", "content": "水果有几种"},
            {"role": "assistant", "content": "共 3 种"},
        ]
        llm = FakeLLM("SELECT count(*) AS n FROM fruit")
        graph = build_graph(service, llm)
        graph.invoke({"question": "那它们的平均价格呢", "history": history})

        question_seen = llm.calls[0][0]
        assert "此前的对话" in question_seen
        assert "水果有几种" in question_seen  # 上一轮用户问题被带上
        assert "共 3 种" in question_seen      # 上一轮答案也被带上
        assert "当前问题: 那它们的平均价格呢" in question_seen

    def test_run_analysis_appends_history(self, service):
        # 调用侧按返回值滚动传递 history，实现会话记忆
        llm = FakeLLM("SELECT count(*) AS n FROM fruit", "SELECT 1")
        r1 = run_analysis(service, llm, "第一问")
        h1 = r1["history"]
        assert len(h1) == 2
        assert h1[0] == {"role": "user", "content": "第一问"}
        assert h1[1]["role"] == "assistant"

        r2 = run_analysis(service, llm, "第二问", history=h1)
        h2 = r2["history"]
        assert len(h2) == 4                       # 两轮共 4 条
        assert h2[2] == {"role": "user", "content": "第二问"}

    def test_no_history_no_context_prefix(self, service):
        # 单轮场景：question 只有"当前问题"前缀，不含历史段
        llm = FakeLLM("SELECT 1")
        graph = build_graph(service, llm)
        graph.invoke({"question": "独立提问"})
        q = llm.calls[0][0]
        assert "此前的对话" not in q
        assert q.strip() == "当前问题: 独立提问"

    def test_history_truncated_to_recent(self):
        # 历史拼进 prompt 只保留最近 6 条，防止无限膨胀
        from agent.graph import _build_history_text

        msgs = [{"role": "user", "content": f"q{i}"} for i in range(1, 11)]
        text = _build_history_text(msgs)
        assert "用户: q5\n" in text and "用户: q10" in text  # 最近 6 条都在
        assert "用户: q1\n" not in text                      # 最早 4 条被截掉
        assert "用户: q4\n" not in text


class FlakyLLM:
    """前 N 次调用抛异常，之后返回正常 SQL。模拟 LLM 网络抖动。"""

    def __init__(self, fail_times: int, sql_after: str):
        self.fail_remaining = fail_times
        self.sql_after = sql_after
        self.calls = 0

    def __call__(self, question: str, schema_text: str, error_hint: str = "") -> str:
        self.calls += 1
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            raise RuntimeError("connection reset by peer")
        return self.sql_after


class TestLLMFailureRecovery:
    """P0 修复：LLM 层故障必须被图捕获并走重试，不能穿透崩溃。"""

    def test_llm_failure_then_success(self, service):
        # 第一次 LLM 挂了，第二次恢复：图不崩，自动重试后出结果
        llm = FlakyLLM(fail_times=1, sql_after="SELECT name FROM fruit")
        graph = build_graph(service, llm)
        state = graph.invoke({"question": "名称"})

        assert state["attempts"] == 2
        assert "apple" in state["result_text"]

    def test_llm_always_failing_gives_graceful_error(self, service):
        # LLM 一直挂：重试耗尽后给出"生成 SQL 失败"而不是抛异常
        llm = FlakyLLM(fail_times=99, sql_after="SELECT 1")
        graph = build_graph(service, llm)
        state = graph.invoke({"question": "名称"})

        assert state["attempts"] == 3  # 受 MAX_SQL_RETRIES 限制
        assert "LLM 调用失败" in state["result_text"]


class TestMetricsInjection:
    """M4：指标口径注入 + 表名白名单拦截（链路级）。"""

    @pytest.fixture
    def ods_service(self):
        svc = DataService(AnalysisEngine(db_path=None))
        svc.engine.register_table(
            "ods_order_info",
            pd.DataFrame(
                {
                    "order_id": [1, 2, 3],
                    "user_id": [101, 102, 101],
                    "amount": [100, 200, 300],
                    "status": ["paid", "unpaid", "paid"],
                }
            ),
        )
        return svc

    @pytest.fixture
    def gmv_registry(self):
        # M8：口径数据源由上层显式传入（生产从 skills 包渲染，
        # 见 agent/skill_loader.py），测试自建单指标注册表
        return [
            Metric(
                name="gmv",
                aliases=("销售额", "成交额", "交易额", "gmv"),
                sql="SELECT SUM(amount) AS gmv FROM ods_order_info",
                tables=("ods_order_info",),
                description="GMV 统计口径：按下单金额计入（含未支付订单）。",
            )
        ]

    def test_matched_metric_injected_into_schema(self, ods_service, gmv_registry):
        # 问"销售额"且 ods_order_info 已加载 → LLM 收到的 schema 含 gmv 口径
        llm = FakeLLM("SELECT SUM(amount) AS gmv FROM ods_order_info")
        graph = build_graph(ods_service, llm, metric_registry=gmv_registry)
        graph.invoke({"question": "今天的销售额是多少"})

        _, schema_text, _ = llm.calls[0]
        assert "指标口径字典" in schema_text
        assert "SUM(amount)" in schema_text  # 标准口径 SQL 已注入

    def test_irrelevant_question_no_metric(self, ods_service, gmv_registry):
        # 问题与指标无关 → schema 不含口径字典
        llm = FakeLLM("SELECT * FROM ods_order_info")
        graph = build_graph(ods_service, llm, metric_registry=gmv_registry)
        graph.invoke({"question": "表里有几行数据"})

        _, schema_text, _ = llm.calls[0]
        assert "指标口径字典" not in schema_text

    def test_no_registry_no_metric_injected(self, ods_service):
        # M8 语义：registry 未配置（skills 包缺失/未渲染）→ 不匹配任何
        # 指标——不喂口径好过喂错口径（fail-closed）
        llm = FakeLLM("SELECT * FROM ods_order_info")
        graph = build_graph(ods_service, llm)
        graph.invoke({"question": "今天的销售额是多少"})

        _, schema_text, _ = llm.calls[0]
        assert "指标口径字典" not in schema_text

    def test_unknown_table_rejected_at_validate(self, service):
        # LLM 幻想出未注册的表 → 白名单拦截 → 带着可用表清单重试修正
        llm = FakeLLM(
            "SELECT * FROM order_info_bak",         # 未注册表
            "SELECT name FROM fruit",               # 修正
        )
        graph = build_graph(service, llm)
        state = graph.invoke({"question": "订单备份数据"})

        assert state["attempts"] == 2
        assert "apple" in state["result_text"]
        # 重试的 error_hint 里必须带"未注册的表"和可用表清单
        assert "未注册的表" in llm.calls[1][2]
        assert "fruit" in llm.calls[1][2]


class TestSqlExtract:
    def test_markdown_block(self):
        assert _extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"

    def test_trailing_semicolon(self):
        assert _extract_sql("SELECT 1;") == "SELECT 1"

    def test_plain_sql(self):
        assert _extract_sql("SELECT * FROM t") == "SELECT * FROM t"

    def test_empty(self):
        assert _extract_sql("") == ""
