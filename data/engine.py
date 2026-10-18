"""DuckDB 统一执行引擎。

设计思想（从 Data-Analysis-Agent 借鉴）：
- 所有数据源（CSV/Excel/MySQL/...）最终都注册成 DuckDB 视图/表
- 分析只面对一个引擎，跑 SQL 只调 execute_query
- 返回 pandas DataFrame，方便后续图表/统计

查询护栏（生产级）：
- 行数上限：SQL 外包裹 LIMIT n+1，超限即报错——防止 LLM 生成的
  无筛选查询把整张表拉进内存
- 执行超时：ThreadPoolExecutor + future.result(timeout)。DuckDB 本身
  没有 statement_timeout（实测 1.5.6 不支持），只能线程池软超时：
  超时后底层查询线程仍在跑（无法真正 kill），但新一轮查询会在
  连接锁上排队，调用方不会无限等待
- 连接锁：DuckDB 单连接非线程安全，所有触达 _conn 的操作串行化
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from typing import Any

import duckdb
import pandas as pd

import config
from data.validate import validate_sql
from logging_setup import get_logger

logger = get_logger("data.engine")


class QueryError(Exception):
    """SQL 执行失败，message 面向用户（前端可展示）。"""


class AnalysisEngine:
    """进程内 DuckDB 引擎。"""

    def __init__(self, db_path: str | None = config.DUCKDB_PATH):
        # db_path=None 时是纯内存模式；否则持久化到文件
        self._conn = duckdb.connect(db_path or ":memory:")
        # ── 连接级封锁（第二道物理闸）────────────────────────────
        # 即使 AST 校验漏掉了某个未知函数/语法，DuckDB 本身也无法
        # 触碰文件系统 / 网络 / 外部库（统一抛 PermissionException）。
        # 我们的数据加载走 pandas（loaders.py），不进 DuckDB 文件系统，
        # 因此"封锁外部访问"不影响正常功能，只有 read_csv/http 等被禁。
        self._conn.execute("SET enable_external_access = false")
        # 连接锁：DuckDB 单连接非线程安全，并发 execute 必须串行化
        # 锁由正在执行的查询持有（在 worker 线程内 acquire/release）：
        # 超时的慢查询仍在持锁跑，后续请求在锁上排队而不会 corruption
        self._conn_lock = threading.Lock()
        # _loaded 从库内现有表恢复：文件模式下重启后表还在（register_table
        # 的 CREATE TABLE 持久化在 duckdb 文件里），这个内存集合必须跟着
        # 恢复，否则 cache_status 会报空与实际表清单不符
        self._loaded: set[str] = set(self.list_tables())
        self._large_tables: set[str] = set()  # 超阈值、留在远端的大表（M3 用）

    # ---------- 注册 ----------

    def register_table(self, name: str, df: pd.DataFrame) -> str:
        """把一个 DataFrame 注册成 DuckDB 表。返回规范化后的表名。"""
        name = self._normalize_name(name)
        # 注册 → 落表 → 解注册：pandas → DuckDB 的标准搬运手法。
        # 临时名用 uuid 随机生成：并发注册时不会互相覆盖同名引用
        # （uuid hex 只含 [0-9a-f]，拼进 SQL 无需再引号/转义）。
        tmp_name = f"_tmp_{uuid.uuid4().hex}"
        with self._conn_lock:
            self._conn.register(tmp_name, df)
            try:
                self._conn.execute(
                    f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM "{tmp_name}"'
                )
            finally:
                # 无论落表成败都解注册，避免给连接留下垃圾引用
                self._conn.unregister(tmp_name)
        self._loaded.add(name)
        logger.info("register_table name=%s rows=%d cols=%d",
                    name, len(df), len(df.columns))
        return name

    def register_view(self, name: str, df: pd.DataFrame) -> str:
        """注册成视图（不拷贝数据，DuckDB 直接读 pandas 内存）。"""
        name = self._normalize_name(name)
        with self._conn_lock:
            self._conn.register(name, df)
        self._loaded.add(name)
        return name

    # ---------- 查询 ----------

    def execute_query(
        self,
        sql: str,
        max_rows: int | None = None,
        timeout_s: float | None = None,
    ) -> pd.DataFrame:
        """执行只读 SELECT，返回 DataFrame。非只读查询抛 QueryError。

        安全链路（两道闸）+ 两道执行护栏：
          ① validate_sql：sqlglot AST 校验（多语句/危险函数/路径白名单）
          ② DuckDB 执行（带行数上限 + 超时护栏）

        max_rows / timeout_s 默认取 config，测试可传入小值验证护栏。
        """
        err = validate_sql(sql)
        if err:
            raise QueryError(err)
        limit = max_rows or config.MAX_QUERY_ROWS
        timeout = config.QUERY_TIMEOUT_SECONDS if timeout_s is None else timeout_s

        guarded_sql = self._with_row_guard(sql, limit)
        t0 = time.monotonic()
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            future = ex.submit(self._execute_raw, guarded_sql)
            try:
                df = future.result(timeout=timeout)
            except FutTimeout:
                logger.warning("query timeout %.1fs sql=%.120s", timeout, sql)
                # 注意：下层查询线程仍在跑（DuckDB 无法 kill），它继续
                # 持有连接锁直到跑完；后续请求在锁上排队而非并发崩溃
                raise QueryError(
                    f"查询超过 {timeout:g} 秒未完成，请简化查询（加筛选/聚合条件）"
                ) from None
        finally:
            ex.shutdown(wait=False)  # 不能 wait=True：那会让超时形同虚设

        if len(df) > limit:
            logger.warning("row guard triggered limit=%d rows=%d", limit, len(df))
            raise QueryError(
                f"查询结果超过 {limit} 行上限，请加筛选条件或聚合后重试"
            )
        logger.info("query ok rows=%d ms=%.1f", len(df), (time.monotonic() - t0) * 1000)
        return df

    def _execute_raw(self, sql: str) -> pd.DataFrame:
        """在 worker 线程内持锁执行（锁随查询走，见 __init__ 注释）。"""
        with self._conn_lock:
            try:
                return self._conn.execute(sql).df()
            except Exception as exc:
                raise QueryError(f"SQL 执行失败: {exc}") from exc

    def list_tables(self) -> list[str]:
        """列出当前引擎里所有可见的表/视图。"""
        with self._conn_lock:
            rows = self._conn.execute(
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_schema='main' ORDER BY table_name"
            ).fetchall()
        return [r[0] for r in rows]

    # ---------- 元信息 ----------

    def table_schema(self, table_name: str) -> list[dict[str, Any]]:
        """返回一张表的列信息（列名/类型），用于喂给 LLM。"""
        with self._conn_lock:
            rows = self._conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                'WHERE table_name = ? ORDER BY ordinal_position',
                [table_name],
            ).fetchall()
        return [{"name": c, "type": t} for c, t in rows]

    def cache_status(self) -> dict[str, Any]:
        """可观测接口：排查"为什么查不到表/为什么慢"时用。"""
        return {
            "loaded_tables": sorted(self._loaded),
            "large_tables": sorted(self._large_tables),
            "total_tables": len(self.list_tables()),
        }

    # ---------- 内部 ----------

    @staticmethod
    def _with_row_guard(sql: str, max_rows: int) -> str:
        """行数护栏：把 SQL 包成 LIMIT n+1 的子查询。

        查出 n+1 行 = 真实结果超限；恰好 n 行则不受影响。
        剥掉尾部分号再包裹（SELECT 1; 会破坏子查询语法）。
        """
        inner = sql.strip().rstrip(";").strip()
        return f"SELECT * FROM ({inner}) AS __qa_guard LIMIT {int(max_rows) + 1}"

    @staticmethod
    def _normalize_name(name: str) -> str:
        """表名规范化：去空格/非法字符，避免注入和歧义。"""
        cleaned = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", "_", name).strip("_")
        if not cleaned:
            raise QueryError(f"非法表名: {name!r}")
        return cleaned

    def close(self) -> None:
        with self._conn_lock:
            self._conn.close()
