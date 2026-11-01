"""查询执行护栏（P0-5）：可中断的超时执行器。

背景：旧实现用 ``ThreadPoolExecutor + future.result(timeout)``，超时后
``shutdown(wait=False)`` 只是"调用方不等了"——工作线程还在跑，而且它
握着 DuckDB 的连接锁。于是"超时"变成了"后续所有查询无限排队"，比不
超时更糟；同时 ``ThreadPoolExecutor`` 的工作线程是非 daemon 的，进程
退出时还会被它拖住。

现在的做法：
  1. daemon 线程执行（进程可以退出）；
  2. 超时先用 ``on_timeout``（DuckDB 就是 ``conn.interrupt()``）真把查询
     打断，再给 grace 秒让它退栈释放锁；
  3. 只有"确认还在跑"才调用中断——避免查询其实已经结束、中断标志却
     留给下一条查询（in-flight 判断用 ``finished`` 事件做闸门）。
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from logging_setup import get_logger

logger = get_logger("data.guards")


class SoftTimeout(TimeoutError):
    """软超时：调用方转成面向用户的 QueryError（中文提示 + 建议）。"""


def run_with_timeout(
    fn: Callable[[], Any],
    timeout_s: float,
    *,
    on_timeout: Optional[Callable[[], None]] = None,
    grace_s: float = 5.0,
    label: str = "query",
) -> Any:
    """在 daemon 线程里跑 fn，超过 timeout_s 就中断并抛 SoftTimeout。

    on_timeout：尽力取消底层操作（DuckDB 用 conn.interrupt()）。
    grace_s：中断后等待线程退栈的时间——它必须释放连接锁，否则下一条
             查询会一直排队（这正是旧实现的病根）。
    """
    box: dict[str, Any] = {}
    finished = threading.Event()

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 线程内异常必须回传给调用方
            box["error"] = exc
        finally:
            finished.set()

    worker = threading.Thread(target=target, daemon=True, name=f"dsh-{label}")
    worker.start()
    worker.join(timeout_s)

    if worker.is_alive():
        # 只有"确实还没结束"才打断，避免把中断标志留给下一条查询
        if on_timeout is not None and not finished.is_set():
            try:
                on_timeout()
            except Exception as exc:  # noqa: BLE001 - 中断失败也要走超时分支
                logger.warning("timeout handler failed label=%s error=%s", label, exc)
        worker.join(grace_s)
        if worker.is_alive():
            # 极端情况：底层不可中断（如阻塞在 C 扩展里）。此时只能放弃，
            # 但 daemon 线程不会阻止进程退出，日志留证便于排查。
            logger.error("query still alive after interrupt label=%s grace=%.1fs",
                         label, grace_s)
        raise SoftTimeout(f"超过 {timeout_s:g} 秒未完成")

    if "error" in box:
        raise box["error"]
    return box.get("value")
