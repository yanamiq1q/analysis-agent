"""指标字典（M4）：把数仓指标口径结构化，供 Agent 生成 SQL 时复用。

对应数仓经验：
  - 指标口径这个词就是 ADS 层的"口径说明书"——同一个业务名词
    （如"销售额"），不同团队算法可能不同（下单即计入 vs 支付才计入）。
  - 把口径写死在 SQL 里灌给 LLM，能根治 LLM 自由发挥导致的口径漂移。

设计：
  - Metric 的 tables 是该指标依赖的表（必须全部已加载进 DuckDB 才注入），
    与具体 MySQL 实例解耦：stdw 表导出 CSV 上传后同名即命中。
  - match_metrics 只做关键词包含匹配（个人项目不引入向量检索），
    命中条件是"问题文本含任一别名 且 依赖表齐全"。
  - STDW_METRICS 是内置的电商订单场景示例指标集（对齐 stdw 库口径），
    表名对不上时直接改这里即可，不影响任何代码。
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


# 电商订单场景示例（对齐 stdw 库分层：ODS 层表）
STDW_METRICS: list[Metric] = [
    Metric(
        name="gmv",
        aliases=("销售额", "成交额", "交易额", "gmv"),
        sql="SELECT SUM(amount) AS gmv FROM ods_order_info",
        tables=("ods_order_info",),
        description="GMV 统计口径：按下单金额计入（含未支付订单）。",
    ),
    Metric(
        name="order_cnt",
        aliases=("订单量", "订单数", "下单量", "成交单量"),
        sql="SELECT COUNT(*) AS order_cnt FROM ods_order_info",
        tables=("ods_order_info",),
        description="订单量口径：按订单主表行数统计，一笔订单记 1 次。",
    ),
    Metric(
        name="pay_order_cnt",
        aliases=("支付订单数", "支付订单量", "成交订单数"),
        sql=(
            "SELECT COUNT(*) AS pay_order_cnt FROM ods_order_info "
            "WHERE status = 'paid'"
        ),
        tables=("ods_order_info",),
        description="支付订单口径：status='paid' 的订单，区别于下单量。",
    ),
    Metric(
        name="pay_convert_rate",
        aliases=("支付转化率", "成交转化率", "转化率"),
        sql=(
            "SELECT "
            "SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) * 1.0 "
            "/ COUNT(*) AS pay_convert_rate FROM ods_order_info"
        ),
        tables=("ods_order_info",),
        description="支付转化率口径：支付订单数 / 总下单数（小数，非百分数）。",
    ),
    Metric(
        name="avg_order_amount",
        aliases=("客单价", "人均消费", "arpu"),
        sql=(
            "SELECT SUM(amount) * 1.0 / COUNT(DISTINCT user_id) "
            "AS avg_order_amount FROM ods_order_info"
        ),
        tables=("ods_order_info",),
        description="客单价口径：销售总额 / 去重用户数（按人平均，不按单平均）。",
    ),
]


def match_metrics(
    question: str,
    available_tables: set[str],
    registry: list[Metric] | None = None,
) -> list[Metric]:
    """从指标字典里挑出与问题相关且当前可用的指标。

    两个条件必须同时满足：
      ① 依赖表齐全：metric.tables 是 available_tables 的子集
      ② 别名命中：问题文本包含任一别名（大小写不敏感）

    registry 参数留给"用户自定义指标集"，默认用内置 STDW_METRICS。
    """
    metrics = registry if registry is not None else STDW_METRICS
    question_lower = question.lower()
    matched: list[Metric] = []
    for m in metrics:
        if not set(m.tables).issubset(available_tables):
            continue  # 依赖表没加载：指标当前不可用，静默跳过
        if any(alias.lower() in question_lower for alias in m.aliases):
            matched.append(m)
    return matched


def format_metrics_prompt(matched: list[Metric]) -> str:
    """把匹配到的指标拼成给 LLM 的口径说明文本（并入 schema 上下文）。"""
    if not matched:
        return ""
    lines = ["### 指标口径字典（必须优先按以下口径生成 SQL）"]
    for m in matched:
        lines.append(f"【{m.name}】{m.description}")
        lines.append(f"口径 SQL: {m.sql}")
    return "\n".join(lines)