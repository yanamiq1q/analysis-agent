"""data 包：数据层（加载 / 引擎 / 服务统一入口）。"""
from data.engine import AnalysisEngine, QueryError
from data.service import DataService
from data.validate import validate_sql

__all__ = ["AnalysisEngine", "QueryError", "DataService", "validate_sql"]
