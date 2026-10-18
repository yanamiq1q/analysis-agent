"""LLM 接入层：DeepSeek（openai 兼容协议）。

核心设计：把"LLM 调用"封装成普通函数
    generate(question, schema_text, error_hint) -> sql
这样 graph.py 只依赖这个函数签名，不直接 import openai——
测试时注入一个 fake 函数即可，无需真实 API key。

openai SDK 延迟 import（在 build_deepseek_generator 内部），
保证不装 openai 时其余代码（含测试）也能正常 import。
"""
from __future__ import annotations

import re
import time
from typing import Callable

import config
from logging_setup import get_logger

logger = get_logger("agent.llm")

# LLM 函数签名：问题 + 表结构 + 上次错误 → 返回 SQL
SqlGenerator = Callable[[str, str, str], str]

_SYSTEM_PROMPT = """你是数据分析 SQL 专家。根据给定的表结构，把用户问题翻译成一条 DuckDB 只读 SQL。
硬性要求：
1. 只输出 SQL 本身，禁止输出任何解释、注释或 markdown 代码块标记。
2. 只允许 SELECT / WITH 查询，禁止任何写操作（INSERT/UPDATE/DELETE/DROP 等）。
3. 表名和列名必须与提供的表结构完全一致，必要时加双引号。
4. 如果提供了"指标口径字典"，用户问题涉及该指标时，必须直接套用给出的口径 SQL，不得自行改变聚合逻辑。
5. 如果提供了"上次 SQL 的错误信息"，请针对性修正后重新输出。
6. 不确定口径时，选择最符合常识的聚合方式，不要臆造不存在的列。"""


def build_deepseek_generator(
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> SqlGenerator:
    """构建真实的 DeepSeek SQL 生成函数（生产环境用）。"""
    from openai import APITimeoutError, OpenAI

    client = OpenAI(
        api_key=api_key or config.DEEPSEEK_API_KEY,
        base_url=base_url or config.DEEPSEEK_BASE_URL,
    )
    chosen_model = model or config.DEEPSEEK_MODEL

    def generate(question: str, schema_text: str, error_hint: str = "") -> str:
        user_prompt = f"表结构:\n{schema_text}\n\n用户问题: {question}"
        if error_hint:
            user_prompt += f"\n\n上次生成的 SQL 有问题，请修正: {error_hint}"

        t0 = time.monotonic()
        try:
            resp = client.chat.completions.create(
                model=chosen_model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,  # 低温度：SQL 生成要稳定可复现
                timeout=config.LLM_TIMEOUT_SECONDS,
            )
        except APITimeoutError as exc:
            # 超时转成中文 RuntimeError：graph.py 只认识普通异常，
            # 统一走"LLM 调用失败"重试链路，不引入 openai 类型依赖
            raise RuntimeError(
                f"LLM 调用超时（>{config.LLM_TIMEOUT_SECONDS:g} 秒）"
            ) from exc
        sql = _extract_sql(resp.choices[0].message.content or "")
        logger.info("llm generate ok ms=%.1f sql_chars=%d",
                    (time.monotonic() - t0) * 1000, len(sql))
        return sql

    return generate


def _extract_sql(text: str) -> str:
    """从 LLM 输出里提取 SQL：容忍 markdown 代码块和少量废话。

    做两层防御：① 优先取 ```sql 代码块；② 否则取整段文本（去尾分号）。
    """
    if not text:
        return ""
    # 情况1：markdown 代码块包裹（LLM 最常见的输出格式）
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # 情况2：整段就是 SQL，去掉结尾分号（DuckDB execute 不需要分号）
    text = text.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    return text
