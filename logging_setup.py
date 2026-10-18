"""结构化日志（生产级缺口 #2：可观测性）。

所有模块统一从这里拿 logger，服务入口调用 setup_logging() 初始化。
格式：时间 | 级别 | 模块名 | 消息（含耗时/行数等关键指标，可直接进
日志采集系统做检索/聚合）。

用法：
    from logging_setup import get_logger
    logger = get_logger("data.engine")
    logger.info("query done rows=%d ms=%.1f", n, ms)   # % 风格，惰性求值
"""
from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(level: int = logging.INFO) -> None:
    """初始化根日志（幂等，force=True 保证重复调用也生效）。"""
    logging.basicConfig(level=level, format=_FORMAT, stream=sys.stdout, force=True)


def get_logger(name: str) -> logging.Logger:
    """按模块名拿 logger（如 "data.engine" / "agent.graph" / "api.server"）。"""
    return logging.getLogger(name)