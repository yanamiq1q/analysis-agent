"""M4/M8 测试：指标字典的匹配逻辑与锁表 prompt。

M8 起口径数据源由 skill 包渲染后显式传入（registry 参数），
本文件不再依赖 metrics.py 内置常量——常量已迁到
skills/metrics/references/metrics.md，其内容守护在
tests/test_skill_loader.py 的「真实技能包」测试里。
"""
from data.metrics import Metric, format_metrics_prompt, match_metrics

# gmv 单指标 fixture（与 skills/metrics/references/metrics.md 同口径）
GMV = Metric(
    name="gmv",
    aliases=("销售额", "成交额", "交易额", "gmv"),
    sql="SELECT SUM(amount) AS gmv FROM ods_order_info",
    tables=("ods_order_info",),
    description="GMV 统计口径：按下单金额计入（含未支付订单）。",
)


class TestMatchMetrics:
    def test_alias_hit_when_tables_ready(self):
        # "销售额"命中 gmv 别名，且依赖表 ods_order_info 已加载 → 命中
        matched = match_metrics("帮我算一下各商品的销售额", {"ods_order_info"}, [GMV])
        assert [m.name for m in matched] == ["gmv"]

    def test_no_match_without_table(self):
        # 依赖表没加载 → 即使问题命中别名也不注入
        assert match_metrics("销售额是多少", {"fruit"}, [GMV]) == []

    def test_no_match_irrelevant_question(self):
        # 问题与任何指标别名无关 → 不命中
        assert match_metrics("水果价格排序", {"ods_order_info"}, [GMV]) == []

    def test_english_alias(self):
        # 英文别名（gmv）大小写不敏感
        matched = match_metrics("请统计 GMV", {"ods_order_info"}, [GMV])
        assert any(m.name == "gmv" for m in matched)

    def test_no_registry_matches_nothing(self):
        # M8 语义：registry 未配置 → 不匹配任何指标（不喂口径好过喂错口径）
        assert match_metrics("销售额是多少", {"ods_order_info"}) == []

    def test_multi_table_metric_requires_all(self):
        # 依赖多张表的指标：缺任何一张都不注入
        metric = Metric(
            name="m",
            aliases=("复合指标",),
            sql="SELECT 1",
            tables=("a", "b"),
            description="x",
        )
        assert match_metrics("复合指标", {"a"}, [metric]) == []
        assert len(match_metrics("复合指标", {"a", "b"}, [metric])) == 1

    def test_custom_registry(self):
        # registry 参数支持自定义指标集（与 skill 包解耦测试用）
        custom = [
            Metric(
                name="my_metric",
                aliases=("自定义",),
                sql="SELECT 1",
                tables=("t1",),
                description="d",
            )
        ]
        matched = match_metrics("查自定义", {"t1"}, custom)
        assert matched[0].name == "my_metric"


class TestFormatPrompt:
    def test_empty(self):
        assert format_metrics_prompt([]) == ""

    def test_contains_sql_and_description(self):
        text = format_metrics_prompt([GMV])
        assert "指标口径字典" in text
        assert GMV.sql in text
        assert GMV.description in text

    def test_locks_responsible_table(self):
        # 锁表措辞：责任表显式列出 + 明令禁止换表 + 堵掉"表空就找替代表"
        matched = match_metrics("GMV 是多少", {"ods_order_info"}, [GMV])
        text = format_metrics_prompt(matched)
        assert "责任表: ods_order_info（仅限）" in text
        assert "禁止改用其它表" in text
        assert "不得用其它表替代计算" in text