"""agent 包：LangGraph 主链路（M3）。

分层：
  agent/state.py  — 状态定义（节点间共享的数据）
  agent/llm.py    — LLM 接入（DeepSeek，可注入 fake 测试）
  agent/graph.py  — StateGraph 四节点：schema → sql_gen → validate → exec
"""
from agent.graph import build_graph
from agent.llm import build_deepseek_generator
from agent.state import AgentState

__all__ = ["build_graph", "build_deepseek_generator", "AgentState"]
