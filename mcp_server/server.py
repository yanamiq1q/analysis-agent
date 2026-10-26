"""data-agent MCP Server 实现（M9）。

职责边界（对齐"纯套壳复用现有能力"的约束）：
  - 不重写校验/执行逻辑：query_sql 直接走 data.validate.validate_sql
    + data.service.DataService.execute；ask_data 直接走
    agent.graph.run_analysis 四节点链路。
  - 只读铁律：三个工具全部标注 readOnlyHint，且 SQL 走 AST 白名单校验。
  - 错误人话回喂：校验/执行失败一律抛 ValueError（中文描述），
    FastMCP 会把它转成 isError 结果回给宿主，而不是裸异常。

与 api/server.py 的关系：同样维护模块级单例（service / 指标口径），
但 MCP 是独立进程，不 import FastAPI app。LLM 延迟构建——
openai 客户端构造需要 API key，进程启动时环境变量可能没配好，
必须等真正要调 ask_data 时才构造（与"测试里 monkeypatch 替换"兼容）。

关键约束（MCP stdio 协议）：stdout 是协议通道，任何日志/print 都会
破坏 JSON-RPC 帧。因此本模块不调用 logging_setup.setup_logging()
（它写 stdout），而是在 main() 里把根日志显式重定向到 stderr。
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

import pandas as pd
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import config
from agent.graph import run_analysis
from agent.llm import SqlGenerator, build_deepseek_generator
from agent.skill_loader import build_metric_registry, parse_skill_dir
from data.service import DataService
from data.validate import validate_sql

mcp = FastMCP("data-agent")

# 模块级单例（与 api/server.py 同构；测试里用 monkeypatch 替换 service/_get_llm）
service = DataService()
_skills, _skill_diagnostics = parse_skill_dir(config.PROJECT_ROOT / "skills")
for _diag in _skill_diagnostics:
    print(f"[skill load failed] {_diag.path}: {_diag.error}", file=sys.stderr)
_metric_registry = build_metric_registry(_skills)

# LLM 延迟构建：进程启动时可能没有 DEEPSEEK_API_KEY，等到第一次
# ask_data 真正需要时才构造；测试直接 monkeypatch _get_llm。
_llm: Optional[SqlGenerator] = None


def _get_llm() -> SqlGenerator:
    """按需构建内层 SQL 生成 LLM（单例，只构建一次）。"""
    global _llm
    if _llm is None:
        _llm = build_deepseek_generator()
    return _llm


# 只读标注：三个工具全部是"读数据、不改变外部世界状态"，宿主看到
# readOnlyHint 可以做并发/缓存优化；openWorldHint=False 表示不访问外部世界。
_RO_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

# 结果返回上限：防止宿主无脑拉全表，超出的行数用提示注明总数
_DEFAULT_ROW_LIMIT = 200


@mcp.tool(title="列出当前所有可查询的数据表", annotations=_RO_ANNOTATIONS)
def list_tables() -> str:
    """当用户想知道"有哪些表 / 有什么数据 / 现在能查什么"时调用。
    返回当前已注册的所有数据表名，每行一个。"""
    tables = service.list_tables()
    if not tables:
        return "（当前没有已加载的数据表，请先通过 web 服务上传文件。）"
    return "\n".join(tables)


@mcp.tool(title="执行只读 SQL 查询", annotations=_RO_ANNOTATIONS)
def query_sql(sql: str, limit: int = _DEFAULT_ROW_LIMIT) -> str:
    """当用户给出明确 SQL 查询语句时调用。执行一条只读 SQL 并把结果以 CSV 文本返回。
    仅支持 SELECT / WITH / UNION 查询：写操作（INSERT/UPDATE/DELETE/DROP 等）、
    多语句（分号注入）、文件写入与网络访问都会被安全校验拦截并返回中文错误。
    - sql: 完整的只读查询语句，如 "SELECT name, price FROM fruit ORDER BY price DESC LIMIT 5"
    - limit: 最多返回的行数（默认 200；查询结果超出时会注明总行数并截断返回）"""
    if not sql or not sql.strip():
        raise ValueError("SQL 不能为空。")
    if limit < 1:
        raise ValueError(f"limit 必须大于等于 1，收到: {limit}")
    # 安全闸（纯套壳）：AST 级校验 + 当前已注册表的白名单
    err = validate_sql(sql, allowed_tables=service.list_tables())
    if err:
        raise ValueError(err)
    try:
        df: pd.DataFrame = service.execute(sql)
    except Exception as exc:
        raise ValueError(f"SQL 执行失败: {exc}") from exc

    total = len(df)
    head = df.head(int(limit))
    csv_text = head.to_csv(index=False)
    if total > len(head):
        return f"共 {total} 行，已截断返回前 {len(head)} 行：\n{csv_text}"
    return f"共 {total} 行：\n{csv_text}"


@mcp.tool(title="用自然语言提问并自动分析数据", annotations=_RO_ANNOTATIONS)
def ask_data(question: str) -> str:
    """当用户用自然语言描述数据分析需求（而非明确给出 SQL）时调用。
    内部自动完成"收集表结构 → LLM 生成 SQL → 安全校验 → 执行"全链路，
    返回可直接阅读的分析结果文本。
    - question: 用户的自然语言问题，如 "价格最高的前 3 种水果" """
    if not question or not question.strip():
        raise ValueError("问题不能为空。")
    try:
        result = run_analysis(
            service,
            _get_llm(),
            question,
            metric_registry=_metric_registry,
        )
    except Exception as exc:
        raise ValueError(f"分析失败: {exc}") from exc
    return result["result_text"]


def main() -> None:
    """入口：把根日志重定向到 stderr（stdio 模式下 stdout 是协议通道，
    任何日志都会破坏 MCP 帧），然后启动 stdio 传输。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stderr,
        force=True,
    )
    mcp.run()  # 默认 stdio 传输，阻塞直到宿主断开


if __name__ == "__main__":
    main()
