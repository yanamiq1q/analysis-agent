"""Agent 状态定义：LangGraph 各节点之间传递的数据结构。

为什么用 TypedDict：LangGraph 的节点返回 dict 会自动"合并"进 state，
TypedDict 只做类型标注，不限制字段——total=False 允许节点只更新部分字段。
"""
from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict, total=False):
    question: str     # 用户原始问题（起点注入）
    history: list     # 多轮对话历史（M5b）：[{"role","content"},...]，约调用侧传入
    schema_text: str  # 节点1 产出：表结构摘要，喂给 LLM
    sql: str          # 节点2 产出：LLM 生成的 SQL
    last_sql: str     # 上一次生成的 SQL（重试时和 error 一起喂回 LLM）
    error: str        # 节点3/4 产出：校验/执行错误（重试时喂回 LLM）
    result_text: str  # 节点4 产出：最终给用户的答案文本
    attempts: int     # 已经尝试生成 SQL 的次数（重试上限用）
    df: object        # 节点4 产出：查询结果的 DataFrame（M7 图表用，
                      # plot_chart 工具从这里取结构化数据而非解析文本）
