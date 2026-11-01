"""报告数据契约（ReportSpec v1）。

为什么要有这个文件
------------------
data-agent 与 WorkBuddy 之间**不能传自然语言**：一段散文交给对方，对方只能
再用 LLM 解析一遍——数字会被"润色"、口径会丢、失败了还会被排版成一张正经
PPT。所以跨系统边界只传结构化数据，而且这个结构要满足四条铁律：

  1. 数字是 number（不是 "1,234 万" 这种字符串），叙述与数字分离；
  2. 图表是文件路径（不是 base64），对方要插图、要上传都需要文件；
  3. 口径随包走（definition / caveats / metric_version），否则报告会变成
     没有出处的"真相"；
  4. 失败是 status + error.code，不是一段会被排进 PPT 的文字。

这份契约同时是 data-agent 的"对外 API 文档"：字段改一次，WorkBuddy 那边
的渲染模板就要跟着改，所以 schema_version 必须显式。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = "1.0"

# 允许出现在 metrics.value 里的数字类型（bool 是 int 的子类，要排除）
_NUMBER_TYPES = (int, float)


def _json_default(obj: Any) -> Any:
    """json.dumps 的兜底序列化器。

    报告里的表会直接来自 pandas：日期列是 datetime.date、聚合列是 numpy
    标量。不转换的话 json.dumps 直接抛 TypeError，而且是在"报告已经跑完、
    正在落盘"的时刻炸——最烦人的位置。这里统一兜住。
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "item"):          # numpy 标量（int64/float64/bool_）
        return obj.item()
    return str(obj)


def _num(value: Any) -> Optional[float]:
    """把 numpy/pandas 标量统一成 Python 数字；NaN/Inf 归零为 None。

    JSON 不能表达 NaN/Infinity（严格的解析器会直接报错），而聚合查询在
    空结果上恰好会产出 NaN——不处理的话 report.json 会在对方那边解析失败。

    末位再 round(10)：浮点累加会产出 0.7112499999999999 这种尾巴，
    渲染方看到会写进 PPT（"0.7112499999999999 → 71.1%"）。10 位小数
    既去掉二进制噪声，又保留业务精度（金额精确到分，远在 1e-10 之上）。
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    number = round(number, 10)
    # 整数保形：1234.0 → 1234，避免渲染方显示成 "1,234.0 元"
    return int(number) if number.is_integer() else number


@dataclass(frozen=True)
class MetricValue:
    """一个指标值：数字 + 单位 + 口径说明。"""

    name: str
    label: str
    value: Optional[float]
    unit: str = ""
    definition: str = ""
    format: str = "auto"          # auto / int / percent / money
    # 环比/同比。空 dict = 模板没要求对比。
    #   {"mom": {"prev": 1700000, "delta": 146900, "pct": 0.0864,
    #            "period": {"start": "...", "end": "..."},
    #            "reason": "上期无数据"}}
    # pct 为 null 时一定有 reason——不允许"空着让渲染方自己猜"。
    compare: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "name": self.name,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "definition": self.definition,
            "format": self.format,
        }
        if self.compare:
            data["compare"] = self.compare
        return data


@dataclass(frozen=True)
class ChartRef:
    """图表引用：只存相对路径，产物跟着 job 目录走。"""

    kind: str
    title: str
    image_path: str               # 相对 job 目录，如 assets/gmv_trend.png
    data_path: str = ""           # 图表原始数据（JSON），供对方二次排版

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "image_path": self.image_path,
            "data_path": self.data_path,
        }


@dataclass(frozen=True)
class TableBlock:
    columns: list[str]
    rows: list[list[Any]]
    source_sql: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"columns": self.columns, "rows": self.rows, "source_sql": self.source_sql}


@dataclass
class Section:
    id: str
    heading: str
    narrative: str = ""
    metrics: list[MetricValue] = field(default_factory=list)
    tables: list[TableBlock] = field(default_factory=list)
    charts: list[ChartRef] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    skipped: str = ""             # 非空 = 这一节没生成，原因写在这里
    # 数据覆盖度（只有模板声明了 coverage 的节才有）：
    #   {start, end, days, expected_days, complete, min_date, max_date}
    # 为什么放在节里而不是只放 meta：渲染方是"一节一页"排版的，
    # 缺数据的那一页需要就地标注，不能指望它去全局 meta 里找。
    coverage: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "heading": self.heading,
            "narrative": self.narrative,
            "metrics": [m.to_dict() for m in self.metrics],
            "tables": [t.to_dict() for t in self.tables],
            "charts": [c.to_dict() for c in self.charts],
            "caveats": list(self.caveats),
            "skipped": self.skipped,
        }
        if self.coverage:
            data["coverage"] = self.coverage
        return data


@dataclass
class ReportSpec:
    job_id: str
    template: str
    title: str
    period: dict[str, str]
    sections: list[Section]
    meta: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "template": self.template,
            "title": self.title,
            "period": self.period,
            "sections": [s.to_dict() for s in self.sections],
            "meta": self.meta,
        }

    def write(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                self.to_dict(), ensure_ascii=False, indent=2, default=_json_default
            ),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path | str) -> "ReportSpec":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        sections = []
        for raw in data.get("sections", []):
            sections.append(
                Section(
                    id=raw["id"],
                    heading=raw["heading"],
                    narrative=raw.get("narrative", ""),
                    metrics=[MetricValue(**m) for m in raw.get("metrics", [])],
                    tables=[TableBlock(**t) for t in raw.get("tables", [])],
                    charts=[ChartRef(**c) for c in raw.get("charts", [])],
                    caveats=raw.get("caveats", []),
                    skipped=raw.get("skipped", ""),
                    coverage=raw.get("coverage", {}),
                )
            )
        return cls(
            job_id=data["job_id"],
            template=data["template"],
            title=data["title"],
            period=data["period"],
            sections=sections,
            meta=data.get("meta", {}),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )

    def validate(self, base_dir: Path | str | None = None) -> list[str]:
        """返回问题列表（空 = 通过）。这是给对方渲染方的最小保证。"""
        problems: list[str] = []

        if not self.sections:
            problems.append("sections 为空：报告至少要有一节")

        seen: set[str] = set()
        for section in self.sections:
            if not section.id:
                problems.append("section.id 不能为空")
            if section.id in seen:
                problems.append(f"section.id 重复: {section.id}")
            seen.add(section.id)

            for metric in section.metrics:
                # bool 是 int 的子类，单独排除；字符串数字直接判错——
                # "1,234 万" 这种值会让对方算不了同比、也对不上 SQL
                if isinstance(metric.value, bool) or (
                    metric.value is not None and not isinstance(metric.value, _NUMBER_TYPES)
                ):
                    problems.append(
                        f"{section.id}.{metric.name} 的值不是数字: {metric.value!r}"
                    )
                for kind, entry in (metric.compare or {}).items():
                    if not isinstance(entry, dict):
                        problems.append(f"{section.id}.{metric.name}.{kind} 结构非法")
                        continue
                    pct = entry.get("pct")
                    if pct is not None and (
                        isinstance(pct, bool) or not isinstance(pct, _NUMBER_TYPES)
                    ):
                        problems.append(f"{section.id}.{metric.name}.{kind}.pct 不是数字")
                    if pct is None and not entry.get("reason"):
                        # 没有比率、又没有理由，渲染方只能显示 "—"，
                        # 用户看到的就是"数据缺了但没人解释"
                        problems.append(
                            f"{section.id}.{metric.name}.{kind} 没有 pct 却缺 reason"
                        )

            for chart in section.charts:
                if not chart.image_path:
                    problems.append(f"{section.id} 存在没有 image_path 的图表")
                elif base_dir is not None:
                    candidate = Path(base_dir) / chart.image_path
                    if not candidate.is_file():
                        problems.append(f"{section.id} 的图表文件不存在: {candidate}")

        if not self.meta.get("generated_at"):
            problems.append("meta.generated_at 缺失（对方无法判断数据新鲜度）")
        for section in self.sections:
            coverage = section.coverage
            if not coverage:
                continue
            # 覆盖度一旦给了，就必须能回答"缺了多少天"；缺字段等于给了个
            # 没法展示的警告，渲染方只能自己猜
            for key in ("start", "end", "days", "expected_days", "complete"):
                if key not in coverage:
                    problems.append(f"{section.id}.coverage 缺少字段: {key}")
        return problems


def summarize(spec: ReportSpec) -> str:
    """给 CLI / 日志用的一行摘要。"""
    metrics = sum(len(s.metrics) for s in spec.sections)
    charts = sum(len(s.charts) for s in spec.sections)
    skipped = sum(1 for s in spec.sections if s.skipped)
    tail = f"，跳过 {skipped} 节" if skipped else ""
    return f"{len(spec.sections)} 节 / {metrics} 个指标 / {charts} 张图{tail}"


def iter_metric_values(sections: Iterable[Section]) -> Iterable[MetricValue]:
    for section in sections:
        yield from section.metrics
