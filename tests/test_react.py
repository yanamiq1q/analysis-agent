"""M6 测试：ReAct 多工具循环（全部 fake 注入，不调真实 API）。"""
from __future__ import annotations

import pandas as pd
import pytest

import config
from agent.react import (
    _decision_text,
    build_react_graph,
    initial_messages,
    run_react_analysis,
)
from agent.skill_loader import parse_skill_dir
from agent.tools import build_tool_registry, execute_tool_call
from data.engine import AnalysisEngine
from data.service import DataService


class FakeSqlLLM:
    """内层 SQL 模型：返回固定 SQL（走真实执行链路）。"""
    def __call__(self, question, schema_text, error_hint=""):
        return "SELECT count(*) AS n FROM fruit"


class FakeChat:
    """外层调度模型：按序返回预设响应，记录每轮收到的 messages 与 tools。

    tools_seen（M8）：每轮 chat 收到的工具集快照——收窄断言就看它。
    """
    def __init__(self, *responses: dict):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []
        self.tools_seen: list[list[dict] | None] = []

    def __call__(self, messages: list[dict], tools=None) -> dict:
        self.calls.append(messages)
        self.tools_seen.append(tools)
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


def tool_resp(name: str, args: str = "{}", call_id: str = "call_1") -> dict:
    return {
        "content": "",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": args}}],
    }


def final_resp(text: str) -> dict:
    return {"content": text, "tool_calls": []}


@pytest.fixture
def service():
    svc = DataService(AnalysisEngine(db_path=None))
    svc.engine.register_table(
        "fruit",
        pd.DataFrame({"name": ["apple", "banana", "cherry"], "price": [5, 7, 4]}),
    )
    return svc


class TestReactLoop:
    def test_query_sql_then_final_answer(self, service):
        # 外层先调 query_sql（内层走真实四节点链路），再给终答
        chat = FakeChat(
            tool_resp("query_sql", '{"question": "有多少种水果"}'),
            final_resp("共有 3 种水果。"),
        )
        result = run_react_analysis(
            service, FakeSqlLLM(), chat, question="有多少种水果"
        )
        assert result["result_text"] == "共有 3 种水果。"

        # 第二轮决策时，工具执行结果以 tool 消息回喂（tool_call_id 对应）
        second_round_msgs = chat.calls[1]
        tool_msg = [m for m in second_round_msgs if m["role"] == "tool"][0]
        assert tool_msg["name"] == "query_sql"
        assert tool_msg["tool_call_id"] == "call_1"
        assert "共 1 行" in tool_msg["content"]
        # assistant 消息带上了 tool_calls 明细（openai 多轮协议要求）
        assistant = [m for m in second_round_msgs if m["role"] == "assistant"][0]
        assert assistant["tool_calls"][0]["id"] == "call_1"
        assert assistant["tool_calls"][0]["type"] == "function"

    def test_list_tables_roundtrip(self, service):
        # 外层先问有哪些表，拿到清单后给出终答
        chat = FakeChat(
            tool_resp("list_tables"),
            final_resp("当前有 fruit 表可以查询。"),
        )
        result = run_react_analysis(service, FakeSqlLLM(), chat, "有哪些表")
        assert "fruit 表" in result["result_text"]

        tool_msg = [m for m in chat.calls[1] if m["role"] == "tool"][0]
        assert "fruit" in tool_msg["content"]

    def test_direct_answer_without_tools(self, service):
        # 闲聊类问题：外层直接回答，内层 SQL 链路完全不被触发
        sql_llm = FakeSqlLLM()
        chat = FakeChat(final_resp("你好，我是数据分析助手。"))
        result = run_react_analysis(service, sql_llm, chat, "你好")
        assert "你好" in result["result_text"]
        assert len(chat.calls) == 1

    def test_step_limit_stops_loop(self, service):
        # 外层模型永远要求调工具：步数上限强制收场，不能死循环
        chat = FakeChat(tool_resp("list_tables"))
        result = run_react_analysis(service, FakeSqlLLM(), chat, "查一下")
        # 每步一轮决策，最多 MAX_REACT_STEPS 步会强制终止
        assert "步骤过多" in result["result_text"]
        assert len(chat.calls) <= config.MAX_REACT_STEPS + 1

    def test_multi_turn_history_replayed(self, service):
        # 历史只回放纯问答文本，上层车轱辘话不遗留在新会话里
        chat = FakeChat(final_resp("答一"), final_resp("答二"))
        run_react_analysis(service, FakeSqlLLM(), chat, "第一问")
        run_react_analysis(
            service,
            FakeSqlLLM(),
            chat,
            "第二问",
            history=[{"role": "user", "content": "第一问"},
                     {"role": "assistant", "content": "答一"}],
        )
        msgs2 = chat.calls[1]
        contents = [m["content"] for m in msgs2 if m["role"] == "user"]
        assert "第一问" in contents and "第二问" in contents
        # system 提示始终在第一条（首轮与第二轮各一份注入点）
        assert msgs2[0]["role"] == "system"

    def test_tool_events_accumulate_across_cycles(self, service):
        # 两轮工具调用后 state 里应保留两条明细。
        # 回归护栏：LangGraph 1.x 的 dict state 是全量替换，
        # react_agent 漏带 tool_events 就会把上一轮明细整轮丢掉
        chat = FakeChat(
            tool_resp("list_tables", "{}", "c1"),
            tool_resp("query_sql", '{"question": "数量"}', "c2"),
            final_resp("完成"),
        )
        graph = build_react_graph(service, FakeSqlLLM(), chat)
        result = graph.invoke(
            {"messages": initial_messages("q", None), "steps": 0}
        )
        assert [e["name"] for e in result["tool_events"]] == [
            "list_tables", "query_sql",
        ]
        assert all(e["ok"] for e in result["tool_events"])
        # 参数与结果摘要都带上了（前端卡片据此渲染）
        assert result["tool_events"][1]["arguments"].startswith("{")
        assert result["tool_events"][1]["result"]


class TestToolDispatch:
    def test_unknown_tool_returns_error_text(self, service):
        registry = build_tool_registry(service, FakeSqlLLM())
        # LLM 幻觉出的工具名：错误文本回喂而不是异常崩掉循环
        out = execute_tool_call("drop_table", "{}", registry)
        assert out["text"] == "错误: 未知工具 drop_table"
        assert out["chart"] is None

    def test_bad_json_args_degrades_to_empty(self, service):
        registry = build_tool_registry(service, FakeSqlLLM())
        out = execute_tool_call("list_tables", "{not json", registry)
        assert "fruit" in out["text"]

    def test_query_sql_empty_question(self, service):
        registry = build_tool_registry(service, FakeSqlLLM())
        out = execute_tool_call("query_sql", '{"question": "  "}', registry)
        assert "不能为空" in out["text"]


class TestPlotTool:
    class GroupedSqlLLM:
        """返回分组统计 SQL（两列：分组列 + 数值列，绘画前提）。"""
        def __call__(self, question, schema_text, error_hint=""):
            return "SELECT name, sum(price) AS total FROM fruit GROUP BY name"

    def test_plot_requires_query_first(self, service):
        # 没查过数据直接画图：错误文本引导，不产出图表
        registry = build_tool_registry(service, FakeSqlLLM())
        out = execute_tool_call("plot_chart", '{"chart_type": "bar"}', registry)
        assert "请先通过 query_sql" in out["text"]
        assert out["chart"] is None

    def test_plot_bad_chart_type(self, service):
        registry = build_tool_registry(service, FakeSqlLLM())
        execute_tool_call("query_sql", '{"question": "数量"}', registry)
        out = execute_tool_call("plot_chart", '{"chart_type": "pie3d"}', registry)
        assert "不支持的图表类型" in out["text"]
        assert out["chart"] is None

    def test_plot_roundtrip_through_graph(self, service):
        # 完整循环：query_sql → plot_chart → 终答，图进 state.charts
        chat = FakeChat(
            tool_resp("query_sql", '{"question": "各水果数量"}', "c1"),
            tool_resp("plot_chart", '{"chart_type": "bar", "title": "数量对比"}', "c2"),
            final_resp("图表已展示，apple 是最多的。"),
        )
        graph = build_react_graph(service, TestPlotTool.GroupedSqlLLM(), chat)
        result = graph.invoke(
            {"messages": initial_messages("统计并画图", None), "steps": 0}
        )
        assert len(result["charts"]) == 1
        assert result["charts"][0].startswith("data:image/png;base64,")
        assert result["final_answer"] == "图表已展示，apple 是最多的。"
        # 画图成功声明作为 tool 消息回喂给 LLM（取最后一条 tool 消息，
        # 即 plot_chart 的执行结果，第一条是 query_sql 的结果文本）
        tool_msg = [m for m in chat.calls[2] if m["role"] == "tool"][-1]
        assert "已生成柱状图" in tool_msg["content"]


class TestInitialMessages:
    def test_filters_non_text_roles(self):
        msgs = initial_messages(
            "当前问题",
            [{"role": "user", "content": "旧问题"},
             {"role": "tool", "content": "不该出现"},
             {"role": "assistant", "content": ""}],  # 空内容也不带
        )
        roles = [m["role"] for m in msgs]
        assert roles == ["system", "user", "user"]
        assert msgs[-1]["content"] == "当前问题"

    def test_history_truncated_to_limit(self):
        big = [{"role": "user", "content": f"问{i}"} for i in range(config.MAX_HISTORY_MESSAGES + 5)]
        msgs = initial_messages("当前", big)
        # system + 截断后的历史 + 当前问题
        assert len(msgs) == 1 + config.MAX_HISTORY_MESSAGES + 1
        assert big[5]["content"] in [m["content"] for m in msgs]  # 最老的被裁掉
        assert msgs[-1]["content"] == "当前"

    def test_skill_catalog_injected(self):
        # M8：渐进式披露的"目录常驻"——目录以一行一个技能并入 system
        msgs = initial_messages("GMV 是多少", None, "- metrics｜电商指标口径查询技能")
        assert "- metrics｜电商指标口径查询技能" in msgs[0]["content"]

    def test_no_catalog_keeps_plain_prompt(self):
        # 未配置技能包：system 就是基础提示 + 规则 7
        # 断言用目录专有标题「### 可用技能」，与规则 7 的「可用技能」字样区分
        msgs = initial_messages("随便问", None)
        assert msgs[0]["role"] == "system"
        assert "### 可用技能" not in msgs[0]["content"]


class TestSkillLoading:
    """M8：load_skill 激活 → 工具集收窄 + 技能全文注入（fake chat 集成）。"""

    @pytest.fixture
    def metrics_skills(self, tmp_path):
        # 最小可用的 metrics 技能包（SKILL.md + references）
        d = tmp_path / "metrics"
        refs = d / "references"
        refs.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\n"
            "name: metrics\n"
            "description: 电商指标口径查询技能\n"
            "allowedTools: [list_tables, query_sql]\n"
            "---\n"
            "口径规则正文\n",
            encoding="utf-8",
        )
        (refs / "metrics.md").write_text(
            "## gmv\n"
            "- 别名: 销售额 / gmv\n"
            "- 责任表: ods_order_info\n"
            "- 口径SQL: SELECT SUM(amount) AS gmv FROM ods_order_info\n"
            "- 说明: 按下单金额计入。\n",
            encoding="utf-8",
        )
        skills, diags = parse_skill_dir(tmp_path)
        assert diags == []
        return skills

    def test_load_skill_narrows_tools_and_injects_prompt(self, service, metrics_skills):
        # 激活前工具全集 4 个；激活后收窄为技能声明的 2 个；
        # 技能全文以 system 消息注入下一轮决策上下文
        chat = FakeChat(
            tool_resp("load_skill", '{"name": "metrics"}', "c1"),
            tool_resp("query_sql", '{"question": "查询 GMV"}', "c2"),
            final_resp("GMV 为 300 元。"),
        )
        result = build_react_graph(
            service, FakeSqlLLM(), chat, skills=metrics_skills
        ).invoke({"messages": initial_messages("GMV 是多少"), "steps": 0})

        assert result["active_skill"] == "metrics"
        assert {s["function"]["name"] for s in chat.tools_seen[0]} == {
            "query_sql", "list_tables", "plot_chart", "load_skill",
        }
        assert {s["function"]["name"] for s in chat.tools_seen[1]} == {
            "query_sql", "list_tables",
        }
        system_msgs = [m for m in chat.calls[1] if m["role"] == "system"]
        assert any("已加载技能 metrics" in m["content"] for m in system_msgs)
        assert any("口径规则正文" in m["content"] for m in system_msgs)
        assert result["final_answer"] == "GMV 为 300 元。"

    def test_unknown_skill_error_back_to_llm(self, service, metrics_skills):
        # 加载未知名 → 错误文本回喂、状态不激活（fail-closed）、循环继续
        chat = FakeChat(
            tool_resp("load_skill", '{"name": "nope"}', "c1"),
            final_resp("没有这个技能，我直接回答。"),
        )
        result = build_react_graph(
            service, FakeSqlLLM(), chat, skills=metrics_skills
        ).invoke({"messages": initial_messages("随便问"), "steps": 0})

        tool_msg = [m for m in chat.calls[1] if m["role"] == "tool"][0]
        assert "未找到技能 nope" in tool_msg["content"]
        assert result.get("active_skill") is None
        assert result["final_answer"] == "没有这个技能，我直接回答。"

    def test_repeat_load_does_not_reinject(self, service, metrics_skills):
        # LLM 反复 load 同一技能：全文只注入一次，防上下文膨胀
        chat = FakeChat(
            tool_resp("load_skill", '{"name": "metrics"}', "c1"),
            tool_resp("load_skill", '{"name": "metrics"}', "c2"),
            final_resp("完成。"),
        )
        build_react_graph(service, FakeSqlLLM(), chat, skills=metrics_skills).invoke(
            {"messages": initial_messages("q"), "steps": 0}
        )
        hits = sum(
            m["content"].count("已加载技能 metrics")
            for m in chat.calls[2] if m["role"] == "system"
        )
        assert hits == 1

    def test_active_skill_survives_state_replace(self, service, metrics_skills):
        # D5 回归：active_skill 若在 react_agent 返回里漏带，会被全量
        # 替换抹掉 → 第三轮工具视野重新回到全集，收窄即失效
        chat = FakeChat(
            tool_resp("load_skill", '{"name": "metrics"}', "c1"),
            tool_resp("list_tables", "{}", "c2"),
            final_resp("完成。"),
        )
        result = build_react_graph(
            service, FakeSqlLLM(), chat, skills=metrics_skills
        ).invoke({"messages": initial_messages("q"), "steps": 0})
        assert result["active_skill"] == "metrics"
        assert {s["function"]["name"] for s in chat.tools_seen[2]} == {
            "query_sql", "list_tables",
        }

    def test_no_skills_config_keeps_full_toolset(self, service):
        # 未配置技能包（skills=None）：工具视野 = 全集（兼容 M7 行为）
        chat = FakeChat(final_resp("好的。"))
        build_react_graph(service, FakeSqlLLM(), chat).invoke(
            {"messages": initial_messages("q"), "steps": 0}
        )
        assert {s["function"]["name"] for s in chat.tools_seen[0]} == {
            "query_sql", "list_tables", "plot_chart", "load_skill",
        }


class TestDecisionText:
    """前端「思考」行显示的内容：真实决策，而非"正在分析决策…"占位文案。"""

    def test_content_used_when_calling_tools(self):
        # 调工具那一轮若模型真输出了文本（reasoner 模型会走到这条），用它
        resp = {
            "content": " 我先看看有哪些表 ",
            "tool_calls": [
                {"id": "1", "function": {"name": "list_tables", "arguments": "{}"}},
            ],
        }
        assert _decision_text(resp) == "我先看看有哪些表"

    def test_falls_back_to_tool_names(self):
        # deepseek-chat 调工具时 content 为空 → 用"决定调用哪些工具"兜底
        resp = {
            "content": "",
            "tool_calls": [
                {"id": "1", "function": {"name": "query_sql", "arguments": "{}"}},
                {"id": "2", "function": {"name": "query_sql", "arguments": "{}"}},
                {"id": "3", "function": {"name": "list_tables", "arguments": "{}"}},
            ],
        }
        # 同名工具去重，保持出现顺序
        assert _decision_text(resp) == "决定调用 query_sql、list_tables"

    def test_final_round_returns_empty(self):
        # 无工具调用 = 这一轮直接产出最终答案：答案气泡已经表达了它，
        # 「思考」行再写一遍就是重复（content 就是答案本身）
        assert _decision_text({"content": "有 2 种水果", "tool_calls": []}) == ""

    def test_step_text_in_state(self, service):
        # 收尾轮不产出思考行 → state 里 step_text 为空
        chat = FakeChat(
            tool_resp("query_sql", '{"question": "数量"}'),
            final_resp("完成"),
        )
        graph = build_react_graph(service, FakeSqlLLM(), chat)
        result = graph.invoke({"messages": initial_messages("q", None), "steps": 0})
        assert result["step_text"] == ""