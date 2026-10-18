"""DataService：数据层对外统一入口。

职责：加载文件 / 连接远端库 → 注册进引擎 → 统一查询执行。
Agent（M3）只依赖这个服务，不直接碰 DuckDB 或 MySQL。
M5a 起支持远端库：小表搬进 DuckDB，大表留远端下推（冷热分层）。
"""
from __future__ import annotations

import pandas as pd
import sqlglot
from sqlalchemy import Engine

import config
from data.engine import AnalysisEngine, QueryError
from data.sources.loaders import LoadResult, load_file
from data.sources.mysql_source import MySQLSource
from data.validate import collect_referenced_tables


def _fold(s: str) -> str:
    """大小写不敏感比较用（对齐 DuckDB 表名语义）。"""
    return s.casefold()


class DataService:
    def __init__(self, engine: AnalysisEngine | None = None):
        self.engine = engine or AnalysisEngine()
        # 用户别名 → 已注册表名列表（一个 Excel 多 sheet 就是多张表，
        # 必须存列表而不是单值，否则别名只记住最后一张 sheet）
        self._sources: dict[str, list[str]] = {}
        self._mysql: MySQLSource | None = None
        self._remote_tables: set[str] = set()

    # ---------- 加载：文件 ----------

    def load_file(self, path: str, source_name: str = "") -> list[str]:
        """加载一个文件并注册进引擎，返回注册后的表名列表。"""
        result: LoadResult = load_file(path, source_name)
        registered: list[str] = []
        for table_name, df in result.frames.items():
            reg = self.engine.register_table(table_name, df)
            registered.append(reg)
        # 别名映射到全部注册表（多 sheet 时全部保留，而不是覆盖）
        self._sources.setdefault(result.source_name, []).extend(registered)
        return registered

    # ---------- 加载：远端库（M5a） ----------

    def connect_mysql(
        self, engine: Engine, lazy_limit: int | None = None
    ) -> dict:
        """连接远端库：小表搬进 DuckDB，大表留远端下推。

        冷热分层：行数 <= lazy_limit 的表整表搬进本地（查询快）；
        大表留远端，SQL 整条下推执行（省内存、省搬运）。
        lazy_limit=None 时用 config.LAZY_LOAD_ROW_LIMIT。
        返回 {"moved": [...], "large": [...]} 便于感知分流结果。
        """
        limit = config.LAZY_LOAD_ROW_LIMIT if lazy_limit is None else lazy_limit
        src = MySQLSource(engine)
        moved: list[str] = []
        large: list[str] = []
        for t in src.list_tables():
            if src.table_rowcount(t, fuzzy_limit=limit) <= limit:
                self.engine.register_table(t, src.read_table(t))
                moved.append(t)
            else:
                large.append(t)
        self._mysql = src
        # 远端判定集合 = 冷表（大表）。注意不能用 src.list_tables()：
        # 那会包含已搬走的表，把"本地+远端混合"SQL 误判成全远端下推
        self._remote_tables = set(large)
        return {"moved": moved, "large": large}

    @property
    def remote_tables(self) -> list[str]:
        """留在远端未搬运的大表（冷表）。"""
        return sorted(self._remote_tables)

    # ---------- 执行（M5a 路由） ----------

    def execute(self, sql: str) -> pd.DataFrame:
        """执行路由：全部引用本地表 → DuckDB；含远端表 → 下推 MySQL；
        混合（本地+远端）或完全未知 → 明确报错（不静默猜）。

        路由判定复用 validate.collect_referenced_tables：与白名单
        校验用同一套"这条 SQL 碰了哪些表"的逻辑，不会两处漂移。
        """
        try:
            stmt = sqlglot.parse_one(sql, dialect="duckdb")
        except sqlglot.errors.ParseError as exc:
            raise QueryError(f"SQL 解析失败: {exc}") from exc

        referenced = collect_referenced_tables(stmt)
        local_folded = {_fold(t) for t in self.engine.list_tables()}
        ref_folded = {_fold(t) for t in referenced}

        if ref_folded.issubset(local_folded):
            return self.engine.execute_query(sql)

        if self._mysql:
            remote_folded = {_fold(t) for t in self._remote_tables}
            if ref_folded.issubset(remote_folded):
                # 下推：整条 SQL 发远端（含方言转译），只回传结果
                return self._mysql.execute(sql)
            unknown = ref_folded - local_folded - remote_folded
            if not unknown:
                raise QueryError(
                    "跨引擎查询暂不支持：一条 SQL 同时引用本地表和远端大表，"
                    "请分成两次查询"
                )
        raise QueryError(f"引用了未知的表: {sorted(ref_folded)}")

    query = execute  # query 是 execute 的历史别名，统一走路由

    # ---------- 预览 / 概况 ----------

    def preview(self, table_name: str, max_rows: int = 10) -> dict:
        """预览一张表：前几行 + 列信息。远端大表走路由下推 LIMIT。"""
        if table_name not in self.list_tables():
            raise QueryError(f"表不存在: {table_name}（可用: {self.list_tables()}）")
        df = self.execute(f'SELECT * FROM "{table_name}" LIMIT {int(max_rows)}')
        return {
            "table": table_name,
            "columns": self.table_schema(table_name),
            "rows": df.to_dict(orient="records"),
        }

    def describe(self, table_name: str) -> dict:
        """表概况：行数、每列的缺失值/唯一值。只支持本地表（远端的
        全列统计要全表扫描，不经筛选直接算会拖垮源库，明确拒绝）。"""
        if table_name not in self.engine.list_tables():
            raise QueryError(
                f"describe 仅支持本地表: {table_name}（远端大表请先用 SQL 筛选）"
            )
        df = self.engine.execute_query(f'SELECT * FROM "{table_name}"')
        return {
            "table": table_name,
            "row_count": len(df),
            "columns": {
                col: {
                    "type": str(df[col].dtype),
                    "nulls": int(df[col].isna().sum()),
                    "unique": int(df[col].nunique()),
                }
                for col in df.columns
            },
        }

    # ---------- 元数据（M3 Agent 的 schema 收集用） ----------

    def list_tables(self) -> list[str]:
        """所有可用表：本地 DuckDB 表 + 远端大表（对 Agent 一视同仁）。"""
        tables = self.engine.list_tables()
        if self._mysql:
            for t in self._mysql.list_tables():
                if t not in tables:
                    tables.append(t)
        return tables

    def table_schema(self, table_name: str) -> list[dict]:
        """单表列信息（列名/类型）。本地查 DuckDB，远端查 information_schema。"""
        if table_name in self.engine.list_tables():
            return self.engine.table_schema(table_name)
        if self._mysql and table_name in self._mysql.list_tables():
            return self._mysql.schema(table_name)
        raise QueryError(f"表不存在: {table_name}")