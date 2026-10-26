"""data-agent 的 MCP Server 包（M9）。

向 MCP 宿主（IDE / Claude Desktop 等）暴露数据查询能力：
  - list_tables：列出当前可查询的表
  - query_sql：执行只读 SQL
  - ask_data：自然语言提问，自动走"结构→SQL→校验→执行"链路

启动（stdio 模式）：
  python -m mcp_server
"""
from mcp_server.server import main

__all__ = ["main"]
