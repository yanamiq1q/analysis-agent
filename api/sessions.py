"""会话历史存储（P0-3）：每会话一把锁。

原来的写法是模块级 ``dict`` + 请求里直接读改写。两个问题：
  1. 同一会话并发两次请求（用户连点/重连重发）时，"读历史 → 跑图 →
     写历史"不是原子的，两次都会读到旧历史、后写的覆盖先写的 → 丢一轮；
  2. 没有锁，dict 在多线程下读改写本身也不安全（FastAPI 现在会把同步
     生成器丢进线程池执行，并发是真实存在的）。

锁的粒度是"每会话一把"而不是全局一把：不同会话应当能并行分析，
只有同一会话需要排队（否则同一会话会出现两条并行回答）。
"""
from __future__ import annotations

import threading


class SessionStore:
    """会话历史（内存态）+ 每会话串行锁。"""

    def __init__(self) -> None:
        self._histories: dict[str, list[dict]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()  # 只保护上面两个 dict 自身

    def lock(self, session_id: str) -> threading.Lock:
        """拿某会话的锁：整轮"读历史→跑图→写历史"都在锁内。"""
        with self._guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def history(self, session_id: str) -> list[dict]:
        """返回副本：调用方会切片/追加，不能让它改到已存的历史。"""
        with self._guard:
            return list(self._histories.get(session_id, []))

    def commit(self, session_id: str, history: list[dict]) -> None:
        with self._guard:
            self._histories[session_id] = list(history)

    def clear(self) -> None:
        with self._guard:
            self._histories.clear()
            self._locks.clear()

    def __len__(self) -> int:
        with self._guard:
            return len(self._histories)
