"""ReAct 工具集合（M6/M7）：工具 JSON schema + 注册表 + 执行分发。

安全设计（最重要的一条）：
  query_sql 不是新开一条 SQL 通道，而是把 M3 的四节点链路
  （collect_schema → generate_sql → validate_sql → execute，含指标
  字典、AST 白名单校验、行数/超时护栏）整体当做一个"工具"被外层
  ReAct 循环调度。ReAct 只改变"谁决定要查数据"，安全断言一条都不放宽。

M7 图表可视化：
  plot_chart 画"最近一次 query_sql 的结果"——结构化 DataFrame 存在
  注册表闭包的 workspace 里，LLM 无需把数据文本抄回参数（既省 token
  也不会抄错数字）。绘图用 matplotlib Agg 后端（无界面服务器可用），
  输出 base64 data URI，由 react.py 放进 state 经 SSE 推给前端。
  图表类型走白名单（bar/line/pie/scatter），严防 LLM 传任意参数。

工具执行失败不抛异常：错误文本作为 tool 消息回喂 LLM，让它换
思路重试（与 SQL 链路的 fail → 反馈 → 重试哲学一致），而不是
让一次坏调用崩掉整个循环。
"""
from __future__ import annotations

import base64
import io
import json
from typing import Callable

import matplotlib

matplotlib.use("Agg")  # 无界面后端：服务器环境出图，不弹窗口
import matplotlib.pyplot as plt
import pandas as pd

import config
from agent.graph import run_analysis
from agent.llm import SqlGenerator
from agent.skill_loader import Skill, list_skill_catalog
from data.metrics import Metric
from data.service import DataService

# 中文字体（Windows 自带微软雅黑）：否则中文标题/标签渲染成方块
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

# 绘图元信息
_PLOT_KINDS = {"bar", "line", "pie", "scatter"}  # 类型白名单
_KIND_LABELS = {"bar": "柱状图", "line": "折线图", "pie": "饼图", "scatter": "散点图"}

# 工具 JSON schema（OpenAI function calling 格式）：
# LLM 依赖这份描述决定"该调哪个工具、参数怎么填"
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "query_sql",
            "description": (
                "对已加载的数据表执行自然语言查询，返回聚合/筛选后的"
                "结果文本。适合计数、求和、分组统计、条件筛选等分析问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "要回答的数据子问题（自然语言，会被翻译成只读 SQL）",
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": "列出当前已加载的全部数据表名。不确定有哪些表时先调这个。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plot_chart",
            "description": (
                "对最近一次 query_sql 返回的数据画图表（柱状图/折线图/"
                "饼图/散点图），图表会直接展示给用户。适用于用户想看图形、"
                "对比、趋势的场景；画完后基于结果给出简短文字结论。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chart_type": {
                        "type": "string",
                        "enum": ["bar", "line", "pie", "scatter"],
                        "description": "图表类型：bar 柱状图 / line 折线图 / pie 饼图 / scatter 散点图",
                    },
                    "title": {"type": "string", "description": "图表标题（可为空字符串）"},
                },
                "required": ["chart_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "加载一个分析技能（系统提示的「可用技能」目录里的技能名）。"
                "加载后技能指引注入对话，可用工具集按该技能的 allowedTools 收窄。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "技能名，见系统提示中的可用技能目录",
                    },
                },
                "required": ["name"],
            },
        },
    },
]


def build_tool_registry(
    service: DataService,
    sql_llm: SqlGenerator,
    metric_registry: list[Metric] | None = None,
    skills: dict[str, Skill] | None = None,
) -> dict[str, Callable]:
    """工具名 → 执行函数。query_sql 绑定 service + sql_llm，走生产同款链路。

    metric_registry（M8）：传给内层 match_metrics 的口径数据源；
    None 时回落到 metrics.py 内置默认集（迁移期兼容，见 build_graph）。

    skills（M8）：已解析的技能包，供 load_skill 校验与返回目录；
    未传 = 无技能可用，load_skill 一律报未找到（fail-closed）。

    plot_chart 依赖 query_sql 的 DataFrame 结果：两者通过闭包里的
    workspace 共享（query_sql 存、plot_chart 取），同一轮 ReAct 会话
    内有效，跨会话不残留。
    """
    workspace: dict = {"df": None}  # 最近一次成功查询的结构化结果

    def query_sql(question: str = "") -> str:
        """自然语言子问题 → 走完整四节点安全链路 → 结果文本。"""
        if not question.strip():
            return "错误: question 参数不能为空"
        result = run_analysis(
            service, sql_llm, question, metric_registry=metric_registry
        )
        if result.get("df") is not None:
            workspace["df"] = result["df"]
        return result.get("result_text") or result.get("error") or "（无输出）"

    def list_tables() -> str:
        tables = service.list_tables()
        if not tables:
            return "当前没有已加载的数据表。"
        return "已加载的数据表: " + ", ".join(tables)

    def plot_chart(chart_type: str = "bar", title: str = "") -> tuple[str, str | None]:
        """把最近一次查询结果画成图。返回 (回喂 LLM 的文本, 图 data URI)。"""
        df = workspace.get("df")
        if df is None or df.empty:
            return "错误: 请先通过 query_sql 查询数据，再调用 plot_chart 画图。", None
        if chart_type not in _PLOT_KINDS:
            return (
                f"错误: 不支持的图表类型 {chart_type}（可选 bar/line/pie/scatter）",
                None,
            )

        plot_df = df.head(config.MAX_PLOT_ROWS)
        if len(plot_df.columns) < 2:
            return (
                "错误: 数据只有一列，无法绘图。请先 query_sql 查询含分组列"
                "和数值列的结果（如 GROUP BY 分组统计）。",
                None,
            )
        x_col = plot_df.columns[0]
        if chart_type == "pie":
            if not pd.api.types.is_numeric_dtype(plot_df[plot_df.columns[1]]):
                return "错误: 饼图的数值列必须是数字类型。", None
        else:
            y_cols = [
                c for c in plot_df.columns[1:]
                if pd.api.types.is_numeric_dtype(plot_df[c])
            ]
            if not y_cols:
                return "错误: 没有数字类型的数值列可用于绘图。", None

        fig, ax = plt.subplots(figsize=(6, 4))
        try:
            if chart_type == "pie":
                plot_df.set_index(x_col)[plot_df.columns[1]].plot(
                    kind="pie", ax=ax, autopct="%1.1f%%"
                )
                ax.set_ylabel("")
            else:
                y_cols = [
                    c for c in plot_df.columns[1:]
                    if pd.api.types.is_numeric_dtype(plot_df[c])
                ]
                plot_df.plot(
                    kind=chart_type, x=x_col, y=y_cols or None,
                    ax=ax, legend=bool(y_cols),
                )
                ax.set_xlabel(x_col)
            if title:
                ax.set_title(title)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", bbox_inches="tight")
            buf.seek(0)
            uri = "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")
        finally:
            plt.close(fig)  # Agg 后端也要显式关闭，防空跑内存泄漏

        truncated = len(df) > config.MAX_PLOT_ROWS
        msg = (
            f"已生成{_KIND_LABELS[chart_type]}并展示给用户"
            f"（基于最近一次查询的 {len(plot_df)} 行数据"
            f"{'，超出绘图上限部分已截断' if truncated else ''}）。"
        )
        return msg, uri

    def load_skill(name: str = "") -> tuple[str, str | None]:
        """加载技能（M8）：返回 (回喂 LLM 的文本, 成功时技能名/失败 None)。

        技能名必须命中已解析的 skills 字典；未命中给错误文本 + 可用目录，
        让 LLM 自己纠偏重试——失败不抛异常，避免中断 ReAct 循环。
        """
        if not name.strip():
            return "错误: name 参数不能为空", None
        if not skills or name not in skills:
            catalog = list_skill_catalog(skills or {})
            hint = f"，可选: {catalog}" if catalog else "（当前没有已配置的技能）"
            return f"错误: 未找到技能 {name}{hint}", None
        return f"技能 {name} 已加载。", name

    return {
        "query_sql": query_sql,
        "list_tables": list_tables,
        "plot_chart": plot_chart,
        "load_skill": load_skill,
    }


def execute_tool_call(
    name: str,
    arguments_json: str,
    registry: dict[str, Callable],
) -> dict:
    """按 openai tool_call 分发执行一招。

    返回 {"text": 回喂 LLM 的文本, "chart": 图 data URI 或 None,
          "skill": M8 激活的技能名或 None}。
    skill 字段只有 load_skill 成功时非空，由外层 tool_execute 节点
    据此更新 state.active_skill 并注入技能全文。
    防御点：
      - 未知工具：返回错误文本（LLM 幻觉出的工具名不崩循环）
      - 坏 JSON 参数：降级为空参数集
      - 工具自身异常：错误文本回喂（同 fail-closed 哲学）
    """
    fn = registry.get(name)
    if fn is None:
        return {"text": f"错误: 未知工具 {name}", "chart": None, "skill": None}
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    try:
        if name in ("plot_chart", "load_skill"):
            # 双返回值工具：(文本, 额外产物)——plot_chart=图URI，load_skill=技能名
            text, extra = fn(**args)
            return {
                "text": text,
                "chart": extra if name == "plot_chart" else None,
                "skill": extra if name == "load_skill" else None,
            }
        return {"text": fn(**args), "chart": None, "skill": None}
    except Exception as exc:
        return {"text": f"工具执行失败: {exc}", "chart": None, "skill": None}