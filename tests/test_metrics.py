"""M4 测试：指标字典的匹配逻辑。"""
from data.metrics import STDW_METRICS, Metric, format_metrics_prompt, match_metrics


class TestMatchMetrics:
    def test_alias_hit_when_tables_ready(self):
        # "销售额"命中 gmv 别名，且依赖表 ods_order_info 已加载 → 命中
        matched = match_metrics("帮我算一下各商品的销售额", {"ods_order_info"})
        names = [m.name for m in matched]
        assert "gmv" in names

    def test_no_match_without_table(self):
        # 依赖表没加载 → 即使问题命中别名也不注入
        matched = match_metrics("销售额是多少", {"fruit"})
        assert matched == []

    def test_no_match_irrelevant_question(self):
        # 问题与任何指标别名无关 → 不命中
        matched = match_metrics("水果价格排序", {"ods_order_info"})
        assert matched == []

    def test_english_alias(self):
        # 英文别名（gmv）大小写不敏感
        matched = match_metrics("请统计 GMV", {"ods_order_info"})
        assert any(m.name == "gmv" for m in matched)

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
        # registry 参数支持自定义指标集（内置 stdw 不参与）
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


class TestStdwMetrics:
    def test_all_metrics_have_sql_and_tables(self):
        assert len(STDW_METRICS) >= 3
        for m in STDW_METRICS:
            assert m.sql.strip()
            assert m.tables
            assert m.aliases
            assert m.description


class TestFormatPrompt:
    def test_empty(self):
        assert format_metrics_prompt([]) == ""

    def test_contains_sql_and_description(self):
        m = STDW_METRICS[0]
        text = format_metrics_prompt([m])
        assert "指标口径字典" in text
        assert m.sql in text
        assert m.description in text