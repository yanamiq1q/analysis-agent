"""M8 测试：skill 加载器（解析 / 收窄 / 目录 / 渲染，全部纯代码，无 LLM）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.skill_loader import (
    SkillParseError,
    filter_exposed_tools,
    list_skill_catalog,
    build_metric_registry,
    parse_metric_references,
    parse_skill_dir,
    parse_skill_file,
    render_skill_prompt,
)

# 测试用的 dummy 工具集（结构与 agent.tools.TOOL_SCHEMAS 一致）
DUMMY_TOOLS = [
    {"type": "function", "function": {"name": "list_tables", "description": "d"}},
    {"type": "function", "function": {"name": "query_sql", "description": "d"}},
    {"type": "function", "function": {"name": "plot_chart", "description": "d"}},
]

GMV_REF = (
    "## gmv\n"
    "- 别名: 销售额 / 成交额 / gmv\n"
    "- 责任表: ods_order_info\n"
    "- 口径SQL: SELECT SUM(amount) AS gmv FROM ods_order_info\n"
    "- 说明: 按下单金额计入（含未支付订单）。"
)


def write_skill(
    root: Path,
    dir_name: str = "metrics",
    *,
    name: str = "metrics",
    description: str = "电商指标口径查询技能",
    allowed: str = "list_tables, query_sql",
    body: str = "# 指引\n查询用口径。",
    refs: str | None = GMV_REF,
) -> Path:
    """在 root/<dir_name>/ 下写一套最小 skill 包，返回 SKILL.md 路径。"""
    skill_dir = root / dir_name
    fm = f"---\nname: {name}\ndescription: {description}"
    if allowed:
        fm += f"\nallowedTools: [{allowed}]"
    fm += f"\n---\n{body}\n"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(fm, encoding="utf-8")
    if refs is not None:
        ref_dir = skill_dir / "references"
        ref_dir.mkdir()
        (ref_dir / "metrics.md").write_text(refs, encoding="utf-8")
    return skill_dir / "SKILL.md"


class TestParseSkillFile:
    def test_full_fields(self, tmp_path):
        path = write_skill(tmp_path)
        skill = parse_skill_file(path)
        assert skill.name == "metrics"
        assert skill.description == "电商指标口径查询技能"
        assert skill.allowed_tools == ("list_tables", "query_sql")
        assert "查询用口径" in skill.prompt
        assert "SUM(amount)" in skill.references
        assert len(skill.metrics) == 1
        assert skill.metrics[0].name == "gmv"

    def test_no_allowed_tools_means_unrestricted(self, tmp_path):
        path = write_skill(tmp_path, allowed="")
        skill = parse_skill_file(path)
        assert skill.allowed_tools == ()

    def test_missing_name_rejected(self, tmp_path):
        path = write_skill(tmp_path, name="")
        with pytest.raises(SkillParseError, match="name"):
            parse_skill_file(path)

    def test_illegal_name_rejected(self, tmp_path):
        path = write_skill(tmp_path, name="Bad Name!")
        with pytest.raises(SkillParseError, match="非法"):
            parse_skill_file(path)

    def test_missing_description_rejected(self, tmp_path):
        path = write_skill(tmp_path, description="")
        with pytest.raises(SkillParseError, match="description"):
            parse_skill_file(path)

    def test_description_truncated(self, tmp_path):
        path = write_skill(tmp_path, description="长" * 200)
        skill = parse_skill_file(path)
        assert len(skill.description) == 120  # 截断不失败（目录行防膨胀）

    def test_missing_frontmatter_rejected(self, tmp_path):
        path = tmp_path / "nofm" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("没有 frontmatter", encoding="utf-8")
        with pytest.raises(SkillParseError, match="frontmatter"):
            parse_skill_file(path)

    def test_no_references_dir(self, tmp_path):
        path = write_skill(tmp_path, refs=None)
        skill = parse_skill_file(path)
        assert skill.references == ""
        assert skill.metrics == ()


class TestParseMetricReferences:
    def test_gmv_fields(self):
        metrics = parse_metric_references(GMV_REF)
        assert len(metrics) == 1
        m = metrics[0]
        assert m.name == "gmv"
        assert m.aliases == ("销售额", "成交额", "gmv")
        assert m.tables == ("ods_order_info",)
        assert m.sql == "SELECT SUM(amount) AS gmv FROM ods_order_info"
        assert "按下单金额计入" in m.description

    def test_multi_tables(self):
        text = (
            "## rate\n- 别名: 比率\n- 责任表: a、b\n"
            "- 口径SQL: SELECT 1\n- 说明: x\n"
        )
        m = parse_metric_references(text)[0]
        assert m.tables == ("a", "b")

    def test_missing_field_fails_closed(self):
        # 缺口径SQL 的指标：宁可不给 LLM 半个口径（错口径比没口径更危险）
        text = "## gmv\n- 别名: 销售额\n- 责任表: t\n- 说明: x\n"
        with pytest.raises(SkillParseError, match="缺少字段"):
            parse_metric_references(text)

    def test_empty_aliases_fails_closed(self):
        text = "## gmv\n- 别名: \n- 责任表: t\n- 口径SQL: SELECT 1\n- 说明: x\n"
        with pytest.raises(SkillParseError):
            parse_metric_references(text)


class TestParseSkillDir:
    def test_loads_all_and_skips_bad(self, tmp_path):
        write_skill(tmp_path, "m1", name="m1")
        write_skill(tmp_path, "m2", name="m2", refs=None)
        write_skill(tmp_path, "bad", name="Bad!")  # 非法名：跳过 + 诊断
        skills, diags = parse_skill_dir(tmp_path)
        assert set(skills) == {"m1", "m2"}
        assert any("Bad" in d.error or "bad" in d.path for d in diags)

    def test_missing_dir_returns_empty(self, tmp_path):
        skills, diags = parse_skill_dir(tmp_path / "nope")
        assert skills == {} and diags == []

    def test_duplicate_name_diagnosed(self, tmp_path):
        write_skill(tmp_path, "a", name="same")
        write_skill(tmp_path, "b", name="same")
        skills, diags = parse_skill_dir(tmp_path)
        assert list(skills) == ["same"]
        assert any("重复" in d.error for d in diags)


class TestCatalogAndRender:
    def test_catalog_lists_name_and_description(self, tmp_path):
        write_skill(tmp_path)
        skills, _ = parse_skill_dir(tmp_path)
        text = list_skill_catalog(skills)
        assert "metrics" in text
        assert "电商指标口径查询技能" in text
        assert "load_skill" in text

    def test_empty_catalog(self):
        assert list_skill_catalog({}) == ""

    def test_render_contains_prompt_and_references(self, tmp_path):
        write_skill(tmp_path)
        skills, _ = parse_skill_dir(tmp_path)
        text = render_skill_prompt(skills["metrics"])
        assert "已加载技能 metrics" in text
        assert "查询用口径" in text
        assert "SUM(amount)" in text


class TestRegistry:
    def test_aggregates_all_skills(self, tmp_path):
        write_skill(tmp_path, "m1", name="m1", refs=GMV_REF)
        write_skill(
            tmp_path, "m2", name="m2",
            refs="## cnt\n- 别名: 单量\n- 责任表: t\n- 口径SQL: SELECT COUNT(*) FROM t\n- 说明: x\n",
        )
        skills, _ = parse_skill_dir(tmp_path)
        registry = build_metric_registry(skills)
        assert {m.name for m in registry} == {"gmv", "cnt"}


class TestRealSkillPackage:
    def test_project_metrics_skill_loads_clean(self):
        # 项目配置自检：真实 skills/metrics 包零诊断、5 个指标字段齐全
        # （M8 后口径唯一数据源——这里替代旧的 STDW_METRICS 常量守护）
        import config

        skills, diags = parse_skill_dir(config.PROJECT_ROOT / "skills")
        assert diags == []
        assert "metrics" in skills
        expected = {
            "gmv", "order_cnt", "pay_order_cnt", "pay_convert_rate", "avg_order_amount",
        }
        names = {m.name for m in skills["metrics"].metrics}
        assert names == expected
        for m in skills["metrics"].metrics:
            assert m.aliases and m.tables and m.sql and m.description


class TestFilterExposedTools:
    def test_empty_allowed_returns_all(self):
        out = filter_exposed_tools(DUMMY_TOOLS, ())
        assert [s["function"]["name"] for s in out] == [
            "list_tables", "query_sql", "plot_chart",
        ]

    def test_intersection_narrows(self):
        out = filter_exposed_tools(DUMMY_TOOLS, ("query_sql",))
        assert [s["function"]["name"] for s in out] == ["query_sql"]

    def test_never_expands(self):
        # allowed 里混入不存在的工具名：只收窄不扩权
        out = filter_exposed_tools(DUMMY_TOOLS, ("query_sql", "drop_table"))
        assert [s["function"]["name"] for s in out] == ["query_sql"]

    def test_input_not_mutated(self):
        snapshot = [dict(s) for s in DUMMY_TOOLS]
        filter_exposed_tools(DUMMY_TOOLS, ("query_sql",))
        assert DUMMY_TOOLS == snapshot