"""指标字典（M4/M8）：把数仓指标口径结构化，供 Agent 生成 SQL 时复用。

M8 起口径数据源从本文件的内置常量搬进了 skills/ 包（配置即数据）：
  - 数据定义：skills/metrics/references/metrics.md（改口径零代码）
  - 解析渲染：agent/skill_loader.py 把 references 解析成 Metric 列表
本文件只保留：Metric 数据结构 + 匹配逻辑 + 锁表 prompt 格式化。
三者都是纯函数，registry 由上层显式传入——口径只能被喂进来，
不能由 LLM 自由发挥。

对应数仓经验：
  - 指标口径这个词就是 ADS 层的"口径说明书"——同一个业务名词
    （如"销售额"），不同团队算法可能不同（下单即计入 vs 支付才计入）。
  - 把口径写死灌给 LLM，能根治 LLM 自由发挥导致的口径漂移。

设计：
  - Metric 的 tables 是该指标依赖的表（必须全部已加载进 DuckDB 才注入），
    与具体 MySQL 实例解耦：stdw 表导出 CSV 上传后同名即命中。
  - match_metrics 只做关键词包含匹配（个人项目不引入向量检索），
    命中条件是"问题文本含任一别名 且 依赖表齐全"。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Metric:
    name: str                 # 标准指标名（英文，如 gmv）
    aliases: tuple[str, ...]  # 业务别名集合（中文口语 + 英文），问题命中任意一个即可
    sql: str                  # 口径 SQL：写死聚合逻辑，防 LLM 自由发挥
    tables: tuple[str, ...]   # 依赖表：必须全部已加载，指标才可用
    description: str          # 口径说明：给 LLM 看的"为什么这么算"


def match_metrics(
    question: str,
    available_tables: set[str],
    registry: list[Metric] | None = None,
) -> list[Metric]:
    """从指标字典里挑出与问题相关且当前可用的指标。

    两个条件必须同时满足：
      ① 依赖表齐全：metric.tables 是 available_tables 的子集
      ② 别名命中：问题文本包含任一别名（大小写不敏感）

    registry 为空（M8：口径数据源由上层从 skill 包渲染后显式传入）：
    不匹配任何指标——不喂口径，好过喂错口径。
    """
    metrics = registry or []
    question_lower = question.lower()
    matched: list[Metric] = []
    for m in metrics:
        if not set(m.tables).issubset(available_tables):
            continue  # 依赖表没加载：指标当前不可用，静默跳过
        if any(alias.lower() in question_lower for alias in m.aliases):
            matched.append(m)
    return matched


def format_metrics_prompt(matched: list[Metric]) -> str:
    """把匹配到的指标拼成给内层 LLM 的口径说明（并入 schema 上下文）。

    措辞刻意是"锁表"而不是"建议"：实测发现只写"必须优先套用口径 SQL"
    约束力不足——当上游把某张表名写进子问题（如"订单事实表
    dwd_order_fact 的总 GMV"）时，LLM 会优先听表名、绕开口径字典。
    所以这里把责任表显式点出、明令禁止换表，并堵掉"表为空就换张表算"
    这个兜底路径——那正是口径漂移最隐蔽的入口。
    """
    if not matched:
        return ""
    lines = [
        "### 指标口径字典（命中以下指标时，必须严格按口径生成 SQL）",
        "硬性约束：",
        "1. 只能查询「责任表」列出的表，禁止改用其它表（含汇总表、事实表）；",
        "2. 若责任表查不到数据，直接回复该口径表无数据，不得用其它表替代计算；",
        "3. 聚合逻辑必须与口径 SQL 完全一致，不得自行改写。",
    ]
    for m in matched:
        lines.append(f"【{m.name}】{m.description}")
        lines.append(f"责任表: {', '.join(m.tables)}（仅限）")
        lines.append(f"口径 SQL: {m.sql}")
    return "\n".join(lines)