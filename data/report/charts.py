"""图表渲染：给报告流水线和 plot_chart 工具共用的一份实现。

为什么单独成模块
----------------
图表逻辑原来长在 ``agent/tools.py`` 的 ``plot_chart`` 闭包里，报告流水线
也需要画图——如果再抄一份，中文字体、数值列判断、饼图校验这些细节就会
慢慢分叉（同一个数据在聊天里画出来和报告里画出来不一样，很难解释）。

所以这里只保留"把 DataFrame 变成 PNG"这一件事，参数校验也在这里；
调用方（工具 / 报告）各自决定错误怎么展示。

线程安全：matplotlib 的全局状态不是线程安全的，而报告 job 在自己的线程
里跑，聊天请求在另一个线程——**必须串行化渲染**，否则偶尔会画出乱码或
空图（这种 bug 极难复现）。
"""
from __future__ import annotations

import io
import math
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")  # 无界面后端：服务器环境出图，不弹窗口
import matplotlib.pyplot as plt
import pandas as pd

# 中文字体（Windows 自带微软雅黑）：否则中文标题/标签渲染成方块。
# 末尾留一个 DejaVu Sans 兜底，避免 Linux 容器里字体名一个都匹配不上。
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

PLOT_KINDS = {"bar", "line", "pie", "scatter"}
KIND_LABELS = {"bar": "柱状图", "line": "折线图", "pie": "饼图", "scatter": "散点图"}

# 渲染锁：见模块头注释
_RENDER_LOCK = threading.Lock()


def render_png(
    df: pd.DataFrame,
    kind: str,
    title: str = "",
    *,
    x: Optional[str] = None,
    y: Optional[str] = None,
    max_rows: Optional[int] = None,
) -> bytes:
    """把结果集画成 PNG 字节。参数不合法时抛 ValueError（中文、可直接展示）。

    x / y 不传时退回老行为：第一列当 x，其余数值列全部当 y。
    传了 x / y 则严格按指定列画——报告模板需要"明确画哪一列"，
    而不是让 pandas 猜。
    """
    if df is None or df.empty:
        raise ValueError("数据为空，无法绘图")
    if kind not in PLOT_KINDS:
        raise ValueError(f"不支持的图表类型 {kind}（可选 bar/line/pie/scatter）")

    plot_df = df.head(max_rows) if max_rows else df
    if len(plot_df.columns) < 2:
        raise ValueError("数据只有一列，无法绘图。请查询含分组列和数值列的结果")

    x_col = x or plot_df.columns[0]
    if x_col not in plot_df.columns:
        raise ValueError(f"x 列不存在: {x_col}")

    if y:
        if y not in plot_df.columns:
            raise ValueError(f"y 列不存在: {y}")
        y_cols = [y]
    else:
        y_cols = [
            c for c in plot_df.columns
            if c != x_col and pd.api.types.is_numeric_dtype(plot_df[c])
        ]
    if not y_cols:
        raise ValueError("没有可用的数值列，无法绘图")

    if kind == "pie":
        if not pd.api.types.is_numeric_dtype(plot_df[y_cols[0]]):
            raise ValueError("饼图的数值列必须是数字类型")
        y_cols = y_cols[:1]          # 饼图只画一个数值列

    fig, ax = plt.subplots(figsize=(6, 4))
    try:
        if kind == "pie":
            plot_df.set_index(x_col)[y_cols[0]].plot(
                kind="pie", ax=ax, autopct="%1.1f%%"
            )
            ax.set_ylabel("")
        else:
            plot_df.plot(kind=kind, x=x_col, y=y_cols, ax=ax, legend=len(y_cols) > 1)
            ax.set_xlabel(x_col)
        if title:
            ax.set_title(title)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        return buf.getvalue()
    finally:
        plt.close(fig)  # Agg 后端也要显式关闭，防空跑内存泄漏


def save_png(
    df: pd.DataFrame,
    kind: str,
    out_path: Path | str,
    title: str = "",
    *,
    x: Optional[str] = None,
    y: Optional[str] = None,
    max_rows: Optional[int] = None,
) -> Path:
    """渲染并落盘（报告流水线用）。返回实际写入路径。"""
    data = render_png(df, kind, title, x=x, y=y, max_rows=max_rows)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return out_path


def _cell(value):
    """把 pandas/numpy 的单元格值转成 JSON 安全的基本类型。

    日期列是 Timestamp、聚合列是 numpy 标量——直接 json.dumps 会抛
    "Object of type Timestamp is not JSON serializable"，而且是在作业
    跑完、正在落盘的时候炸。
    """
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):          # numpy 标量
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def chart_data(df: pd.DataFrame, max_rows: int = 200) -> dict:
    """图表背后的原始数据（供对方二次排版：换图型、改配色、导出 Excel）。

    astype(object) 是必须的：float 列上的 `where(notna, None)` 仍然是 NaN
    （列 dtype 不会因为 None 变成 object），而 NaN 写进 JSON 是非法值，
    对方用严格解析器会直接报错。先转 object 再填 None 才能得到真正的 null。
    """
    head = df.head(max_rows)
    safe = head.astype(object).where(pd.notna(head), None)
    return {
        "columns": [str(c) for c in safe.columns],
        "rows": [[_cell(v) for v in row] for row in safe.values.tolist()],
    }
