"""MySQL 数据源（M5a）：把远程库接入 DuckDB 统一引擎，冷热分层。

设计（对齐旧项目 data/sources/sql.py 的大表路由，砍掉后台 job 的简化版）：
  - 依赖注入 sqlalchemy Engine：生产传 mysql+pymysql 连接串，
    测试传 SQLite 内存引擎 —— 同一套 API 验证路由逻辑，不需要真 MySQL。
  - 冷热分层：行数 <= LAZY_LOAD_ROW_LIMIT 的表（热表）整表搬进 DuckDB，
    查询走本地引擎；大表（冷表）留在 MySQL，SQL 整条下推远端执行，
    只回传聚合后的结果 DataFrame。
  - 行数判定用 information_schema.tables.table_rows（InnoDB 的统计估算值，
    够用于阈值分流，避免连接后对每张表做 COUNT(*) 全表扫描）。
  - 方言转换：LLM 生成的是 DuckDB 方言 SQL（双引号表名），下推前用
    sqlglot 转成目标库方言（MySQL 用反引号）。这也让 sqlglot 在项目里
    多了"转译"用途，不只是安全校验。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

import pandas as pd
import sqlglot
from sqlalchemy import Engine, create_engine, inspect, text

import config
from data.engine import AnalysisEngine, QueryError
from logging_setup import get_logger

logger = get_logger("data.mysql")


def build_mysql_engine(url: str) -> Engine:
    """创建 Engine（断线重连）。示例: mysql+pymysql://root:pwd@localhost:3306/stdw

    - pool_pre_ping：每次从连接池取连接前先探活（SELECT 1），
      MySQL 重启或空闲被 wait_timeout 断开后，下一条查询会自动
      换新连接，而不是把失效连接错误直接抛给用户
    - pool_recycle：连接存活超过该秒数强制回收，主动把连接寿命
      压到小于服务端 wait_timeout，把"被动断开"变成"主动轮换"
    """
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=config.MYSQL_POOL_RECYCLE_SECONDS,
    )


class MySQLSource:
    """一个已连接的远端库。只做三件事：元数据、读表、下推执行。"""

    def __init__(self, engine: Engine):
        self._engine = engine

    # ---------- 元数据 ----------

    def list_tables(self) -> list[str]:
        """库内全部业务表（过滤掉引擎内部表）。"""
        inspector = inspect(self._engine)
        return [
            t for t in inspector.get_table_names() if not t.startswith("sqlite_")
        ]

    def table_rowcount(self, table: str, fuzzy_limit: int | None = None) -> int:
        """行数估算 + 模糊区精确复核（count 采样）。

        information_schema.tables.table_rows 是 InnoDB 统计估算值，
        偏差可达 ±50%：直接拿去和 LAZY_LOAD_ROW_LIMIT 比分流，会把
        阈值附近的表错分冷热。策略：
          - 估算值落在阈值模糊区 [limit/2, limit*2] → 精确 COUNT(*)
            复核（走 execute，吃 MAX_EXECUTION_TIME 超时护栏）
          - 远离阈值的估算直接采信：判错代价小（热表误搬顶多多占
            内存，冷表误留顶多查询慢点），不值得全表扫描
          - 精确计数本身超时 = 表大到连 COUNT 都跑不动，按冷表处理
        """
        limit = config.LAZY_LOAD_ROW_LIMIT if fuzzy_limit is None else fuzzy_limit
        est = self._estimated_rowcount(table)
        if est is None:
            return self._exact_rowcount(table)
        if limit / 2 <= est <= limit * 2:
            try:
                return self._exact_rowcount(table)
            except QueryError:
                logger.warning(
                    "exact rowcount failed, treat as cold table=%s", table
                )
                return limit + 1
        return est

    def _estimated_rowcount(self, table: str) -> int | None:
        """information_schema 估算行数；引擎无该系统表（如 SQLite）时
        返回 None，由调用方回退精确计数。"""
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT table_rows FROM information_schema.tables "
                        "WHERE table_name = :tbl"
                    ),
                    {"tbl": table},
                ).fetchone()
        except Exception:
            return None
        if row is not None and row[0] is not None:
            return int(row[0])
        return None

    def _exact_rowcount(self, table: str) -> int:
        """精确 COUNT(*)：走 execute 复用超时/行数护栏（聚合只回 1 行）。"""
        df = self.execute(f"SELECT COUNT(*) AS n FROM {self._quote(table)}")
        return int(df.iloc[0]["n"])

    def schema(self, table: str) -> list[dict]:
        """列信息：与 data.engine.table_schema 同格式 [{'name','type'}]。"""
        inspector = inspect(self._engine)
        return [
            {"name": col["name"], "type": str(col["type"])}
            for col in inspector.get_columns(table)
        ]

    # ---------- 数据 ----------

    def read_table(self, table: str) -> pd.DataFrame:
        """整表读成 DataFrame（仅用于小表搬运，大表禁止调这个）。"""
        with self._engine.connect() as conn:
            return pd.read_sql_query(f"SELECT * FROM {self._quote(table)}", conn)

    def execute(
        self,
        sql: str,
        max_rows: int | None = None,
        timeout_s: float | None = None,
    ) -> pd.DataFrame:
        """大表下推：把 DuckDB 方言 SQL 转成远端方言后整条发远端执行。

        护栏与本地 DuckDB 引擎对齐（同一套阈值语义）：
          - 行数上限：复用 engine 的 LIMIT n+1 包裹（超限即报错），
            减少对数仓源库的影响
          - 超时：MySQL 方言在 _execute_remote 里先 SET SESSION
            MAX_EXECUTION_TIME（8.0 服务端硬终止 SELECT）；其它方言
            （SQLite 测试路径）退化为线程池软超时。shutdown(wait=False)
            的原因与 engine 相同：wait=True 会让超时形同虚设。
        """
        limit = max_rows or config.MAX_QUERY_ROWS
        timeout = config.QUERY_TIMEOUT_SECONDS if timeout_s is None else timeout_s
        guarded = AnalysisEngine._with_row_guard(self._transpile(sql), limit)
        t0 = time.monotonic()
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            future = ex.submit(self._execute_remote, guarded)
            try:
                df = future.result(timeout=timeout)
            except FutTimeout:
                logger.warning("mysql query timeout %.1fs sql=%.120s", timeout, sql)
                raise QueryError(
                    f"查询超过 {timeout:g} 秒未完成，请简化查询（加筛选/聚合条件）"
                ) from None
        finally:
            ex.shutdown(wait=False)

        if len(df) > limit:
            logger.warning("mysql row guard triggered limit=%d rows=%d", limit, len(df))
            raise QueryError(
                f"查询结果超过 {limit} 行上限，请加筛选条件或聚合后重试"
            )
        logger.info("mysql query ok rows=%d ms=%.1f",
                    len(df), (time.monotonic() - t0) * 1000)
        return df

    def _execute_remote(self, sql: str) -> pd.DataFrame:
        """worker 线程内执行远端 SQL；mysql 方言先设服务端超时再跑。

        SET SESSION 只作用于当前连接，必须与查询放在同一个
        connect 上下文（连接池里每个 connect 是独立 session）。
        """
        with self._engine.connect() as conn:
            if self._engine.dialect.name == "mysql":
                # 8.0 特性：单位毫秒、仅作用于 SELECT；值为 config 中
                # 的 int，由环境变量控制，注入风险为零
                conn.execute(
                    text(
                        "SET SESSION MAX_EXECUTION_TIME"
                        f" = {config.MYSQL_MAX_EXECUTION_TIME_MS}"
                    )
                )
            try:
                return pd.read_sql_query(sql, conn)
            except Exception as exc:
                raise QueryError(f"SQL 执行失败: {exc}") from exc

    # ---------- 内部 ----------

    def _dialect_name(self) -> str:
        return "mysql" if self._engine.dialect.name == "mysql" else "sqlite"

    def _transpile(self, sql: str) -> str:
        """DuckDB 方言 → 远端方言。转译失败就原样发送，交给数据库报错。"""
        try:
            return sqlglot.transpile(
                sql, read="duckdb", write=self._dialect_name()
            )[0]
        except Exception:
            return sql

    def _quote(self, ident: str) -> str:
        """按引擎方言引用标识符：MySQL 反引号，SQLite 双引号。"""
        if self._engine.dialect.name == "mysql":
            return f"`{ident}`"
        return f'"{ident}"'