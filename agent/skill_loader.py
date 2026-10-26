"""Skill 加载器（M8）：把 skills/ 目录下的 SKILL.md 包解析成运行时结构。

对照参考项目 Zafer-Liu/Data-Analysis-Agent 的 skill 系统做的极简版，保留
其三个最有价值的设计：
  1. 渐进式披露——目录常驻 system prompt，load_skill 激活后才注入全文
  2. 最小权限——filter_exposed_tools 按 allowedTools 交集收窄，只收窄不扩权
  3. 配置即数据——指标口径写在 references/*.md，改口径零代码

M8 明确不做（参考项目有、后续里程碑再说）：builtin/user/workspace 三级
目录覆盖、热重载、向量三通道检索 + RRF、$ARGUMENTS 参数渲染、trusted
技能敏感工具解锁。

fail-closed 原则：任何解析失败（frontmatter 缺失/非法、指标字段不全）
都让该技能整体不上架并记 diagnostic，绝不让半个口径溜进 prompt——
一个错口径比没有口径更危险（会被算出错数）。

frontmatter 手写解析而非引 PyYAML：规格只有 name/description/
allowedTools 三个键，不值得为 20 行解析新增依赖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from data.metrics import Metric

SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
MAX_DESCRIPTION_CHARS = 120       # 目录行展示上限，防长描述吞 prompt
MAX_SKILL_BYTES = 100_000         # 单 SKILL.md 字节上限（参考项目同款护栏）


class SkillParseError(ValueError):
    """SKILL.md 解析失败：fail-closed，该技能不上架。"""


@dataclass(frozen=True)
class Skill:
    name: str                     # 规范名（frontmatter，校验 SKILL_NAME_RE）
    description: str              # 一句话描述（目录行），≤ MAX_DESCRIPTION_CHARS
    allowed_tools: tuple[str, ...]  # 激活后暴露的工具白名单；空 = 不限制
    prompt: str                   # SKILL.md 正文（激活时注入的指引）
    references: str               # references/*.md 合并全文（激活时注入）
    metrics: tuple[Metric, ...]   # 从 references 解析出的指标（供内层口径匹配）


@dataclass(frozen=True)
class SkillDiagnostic:
    path: str                     # 出问题的文件（日志/排障定位用）
    error: str


# ── frontmatter 解析 ──────────────────────────────────────────────

def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆出 frontmatter 键值对与正文。格式：开头 --- 、下一行 --- 结束。"""
    if not text.startswith("---\n"):
        raise SkillParseError("SKILL.md 必须以 frontmatter（首行 ---）开头")
    rest = text[4:]
    end = rest.find("\n---\n")
    if end == -1:
        raise SkillParseError("frontmatter 缺少结束标记 ---")
    header, body = rest[:end], rest[end + 5:]
    meta: dict[str, str] = {}
    for raw in header.splitlines():
        line = raw.strip()
        if not line:
            continue
        if ":" not in line:
            raise SkillParseError(f"frontmatter 行格式非法: {raw}")
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, body.strip()


def _parse_allowed_tools(raw: str) -> tuple[str, ...]:
    """解析 allowedTools: [list_tables, query_sql] → 元组；空 = 不限制。"""
    raw = raw.strip().strip("[]").strip()
    if not raw:
        return ()
    return tuple(t.strip(" '\"") for t in raw.split(",") if t.strip())


# ── references 指标解析 ───────────────────────────────────────────

_METRIC_REQUIRED = ("aliases", "tables", "sql", "description")


def parse_metric_references(text: str) -> list[Metric]:
    """把 references 文件解析成 Metric 列表（复用 data.metrics 的数据结构）。

    固定行格式（不是自由 markdown，供程序解析）：
      ## 指标名
      - 别名: a / b / c
      - 责任表: t1、t2
      - 口径SQL: SELECT ...
      - 说明: 一段口径说明
    任一字段缺失/空 → SkillParseError（fail-closed：不上架半个口径）。
    """
    sections: list[dict] = []
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            current = {"name": line[3:].strip()}
            sections.append(current)
            continue
        if line.startswith("#"):
            continue  # 一级标题行（如「# 电商指标口径定义」），跳过
        if not line.startswith("- "):
            raise SkillParseError(f"指标定义行格式非法: {raw}")
        if current is None:
            raise SkillParseError(f"指标内容出现在标题之前: {raw}")
        key, _, value = line[2:].partition(":")
        key = key.strip()
        value = value.strip()
        if key == "别名":
            current["aliases"] = tuple(a.strip() for a in value.split("/") if a.strip())
        elif key == "责任表":
            current["tables"] = tuple(re.split(r"[,、]", value))
        elif key == "口径SQL":
            current["sql"] = value
        elif key == "说明":
            current["description"] = value
        else:
            raise SkillParseError(f"指标字段未知: {key}")

    metrics: list[Metric] = []
    for s in sections:
        missing = [k for k in _METRIC_REQUIRED if k not in s or not s[k]]
        if missing:
            raise SkillParseError(
                f"指标 {s.get('name', '?')} 缺少字段: {', '.join(missing)}"
            )
        metrics.append(
            Metric(
                name=s["name"],
                aliases=tuple(s["aliases"]),
                sql=s["sql"].strip(),
                tables=tuple(s["tables"]),
                description=s["description"],
            )
        )
    return metrics


# ── 文件加载 ──────────────────────────────────────────────────────

def parse_skill_file(path: Path) -> Skill:
    """解析单个 SKILL.md（含 references 目录下全部 .md 的索引与解析）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillParseError(f"读取失败: {exc}") from exc
    if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
        raise SkillParseError(f"文件超限（>{MAX_SKILL_BYTES} 字节）")

    meta, body = _split_frontmatter(text)
    name = meta.get("name", "").strip()
    if not name:
        raise SkillParseError("frontmatter 缺少 name")
    if not SKILL_NAME_RE.match(name):
        raise SkillParseError(f"name 非法（须匹配 {SKILL_NAME_RE.pattern}）: {name}")
    description = meta.get("description", "").strip()
    if not description:
        raise SkillParseError("frontmatter 缺少 description")
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS]  # 超长截断，不上架失败

    ref_dir = path.parent / "references"
    ref_parts = (
        sorted(ref_dir.glob("*.md")) if ref_dir.is_dir() else []
    )
    refs_text = ""
    for ref in ref_parts:
        try:
            refs_text += "\n\n" + ref.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SkillParseError(f"references 读取失败 {ref.name}: {exc}") from exc

    return Skill(
        name=name,
        description=description,
        allowed_tools=_parse_allowed_tools(meta.get("allowedTools", "")),
        prompt=body,
        references=refs_text,
        metrics=tuple(parse_metric_references(refs_text)) if refs_text else (),
    )


def parse_skill_dir(root: Path) -> tuple[dict[str, Skill], list[SkillDiagnostic]]:
    """扫描 root/*/SKILL.md。失败/重名的技能记 diagnostic 并跳过。"""
    skills: dict[str, Skill] = {}
    diagnostics: list[SkillDiagnostic] = []
    if not root.is_dir():
        return skills, diagnostics  # 目录不存在 = 没有技能，不是错误
    for md in sorted(root.glob("*/SKILL.md")):
        try:
            skill = parse_skill_file(md)
        except (SkillParseError, OSError) as exc:
            diagnostics.append(SkillDiagnostic(str(md), str(exc)))
            continue
        if skill.name in skills:
            diagnostics.append(
                SkillDiagnostic(str(md), f"技能名重复: {skill.name}")
            )
            continue
        skills[skill.name] = skill
    return skills, diagnostics


# ── 运行时助手（收窄 / 目录 / 渲染）──────────────────────────────

def filter_exposed_tools(
    tool_schemas: list[dict], allowed: Sequence[str] | None
) -> list[dict]:
    """按技能 allowedTools 收窄工具集。

    空 allowed = 不限制（返回全集）。交集收窄：结果恒 ⊆ 输入集合——
    即使 allowed 里写了不存在的工具名也不会凭空新增（只收窄不扩权）。
    """
    if not allowed:
        return list(tool_schemas)
    allowed_set = set(allowed)
    return [
        s for s in tool_schemas
        if s.get("function", {}).get("name") in allowed_set
    ]


def list_skill_catalog(skills: dict[str, Skill]) -> str:
    """目录行文本：注入 system prompt 用（渐进式披露的"目录常驻"部分）。"""
    if not skills:
        return ""
    lines = [
        "### 可用技能（涉及对应领域问题时，先调 load_skill 加载该技能，再拆解子问题）"
    ]
    for name in sorted(skills):
        lines.append(f"- {name}｜{skills[name].description}")
    return "\n".join(lines)


def render_skill_prompt(skill: Skill) -> str:
    """激活时注入的 system 消息全文：技能头 + 正文 + references。"""
    parts = [f"【已加载技能 {skill.name}】{skill.description}", skill.prompt]
    if skill.references:
        parts.append(skill.references)
    return "\n\n".join(parts)


def build_metric_registry(skills: dict[str, Skill]) -> list[Metric]:
    """汇总全部技能解析出的指标：内层 match_metrics 的 registry 数据源。"""
    registry: list[Metric] = []
    for skill in skills.values():
        registry.extend(skill.metrics)
    return registry