"""引擎测试：注册 / 查询 / 只读防护 / 查询护栏 / 并发 / 元信息。"""
import logging
import threading

import pandas as pd
import pytest

from data.engine import AnalysisEngine, QueryError


@pytest.fixture()
def engine():
    eng = AnalysisEngine(db_path=None)  # 内存模式，测试互不污染
    yield eng
    eng.close()


@pytest.fixture()
def sample_df():
    return pd.DataFrame(
        {"name": ["apple", "banana", "cherry"], "price": [3, 5, 8]}
    )


class TestRegisterAndQuery:
    def test_register_then_query(self, engine, sample_df):
        engine.register_table("fruit", sample_df)
        df = engine.execute_query("SELECT * FROM fruit")
        assert len(df) == 3
        assert df["price"].sum() == 16

    def test_register_view_does_not_copy(self, engine, sample_df):
        engine.register_view("v", sample_df)
        df = engine.execute_query("SELECT count(*) AS n FROM v")
        assert df.iloc[0]["n"] == 3

    def test_table_name_normalized(self, engine, sample_df):
        # 中文表名 + 空格，应被规范化且可查询
        name = engine.register_table(" 订单 数据! ", sample_df)
        assert name == "订单_数据"
        assert engine.execute_query(f'SELECT * FROM "{name}"').shape[0] == 3


class TestReadOnlyGuard:
    def test_reject_update(self, engine, sample_df):
        engine.register_table("fruit", sample_df)
        with pytest.raises(QueryError, match="只允许 SELECT / WITH"):
            engine.execute_query("DELETE FROM fruit")

    def test_reject_drop(self, engine, sample_df):
        engine.register_table("fruit", sample_df)
        with pytest.raises(QueryError, match="只允许 SELECT / WITH"):
            engine.execute_query("DROP TABLE fruit")

    def test_allow_cte(self, engine, sample_df):
        engine.register_table("fruit", sample_df)
        df = engine.execute_query("WITH t AS (SELECT * FROM fruit) SELECT sum(price) AS s FROM t")
        assert df.iloc[0]["s"] == 16


class TestMeta:
    def test_list_tables(self, engine, sample_df):
        engine.register_table("fruit", sample_df)
        assert "fruit" in engine.list_tables()

    def test_table_schema(self, engine, sample_df):
        engine.register_table("fruit", sample_df)
        cols = {c["name"]: c["type"] for c in engine.table_schema("fruit")}
        assert cols["price"] == "BIGINT"

    def test_query_error_message(self, engine):
        with pytest.raises(QueryError, match="SQL 执行失败"):
            engine.execute_query("SELECT * FROM no_such_table")


class TestConnectionLockdown:
    """第二道物理闸：绕过 AST，直接打 DuckDB，验证外部访问被物理禁止。"""

    def test_read_local_file_blocked(self, engine):
        # 即使路径合法（validate 放行），DuckDB 文件系统操作也被禁用
        with pytest.raises(Exception, match="disabled by configuration"):
            engine._conn.execute(
                "SELECT * FROM read_csv('data/warehouse/sample.csv')"
            )

    def test_http_blocked(self, engine):
        with pytest.raises(Exception, match="disabled by configuration"):
            engine._conn.execute("SELECT * FROM 'http://example.com/x.csv'")

    def test_attach_external_db_blocked(self, engine):
        with pytest.raises(Exception, match="disabled by configuration"):
            engine._conn.execute("ATTACH 'sneaky.db'")

    def test_in_memory_query_still_works(self, engine, sample_df):
        # 封锁不影响内存表查询（我们的数据全在内存注册表里）
        engine.register_table("fruit", sample_df)
        df = engine.execute_query("SELECT count(*) AS n FROM fruit")
        assert df.iloc[0]["n"] == 3


class TestQueryGuards:
    """行数上限 + 软超时护栏：production 硬伤 #1 的验证。"""

    def test_row_limit_exceeded(self, engine):
        engine.register_table("t", pd.DataFrame({"x": range(10)}))
        with pytest.raises(QueryError, match="超过 3 行上限"):
            engine.execute_query("SELECT * FROM t", max_rows=3)

    def test_row_limit_exactly_fits(self, engine):
        # LIMIT n+1 包裹的边界：恰好 n 行必须正常放行
        engine.register_table("t", pd.DataFrame({"x": range(3)}))
        df = engine.execute_query("SELECT * FROM t", max_rows=3)
        assert len(df) == 3

    def test_aggregate_unaffected_by_guard(self, engine, sample_df):
        # 聚合结果只有 1 行，护栏包裹不影响正确性
        engine.register_table("fruit", sample_df)
        df = engine.execute_query("SELECT sum(price) AS s FROM fruit", max_rows=1)
        assert df.iloc[0]["s"] == 16

    def test_timeout_raises(self, engine):
        # timeout=0 让 result(0) 完全不等待，确定性触发软超时。
        # 不用 1e-9 这类极小值：Windows 的 Condition.wait 会把亚毫秒
        # 超时钳到毫秒级，快查询会赶在超时前完成（已踩坑）。
        with pytest.raises(QueryError, match="未完成"):
            engine.execute_query("SELECT 1", timeout_s=0)

    def test_timeout_logged_warning(self, engine, caplog):
        with caplog.at_level(logging.INFO):
            with pytest.raises(QueryError):
                engine.execute_query("SELECT 1", timeout_s=0)
        assert any("query timeout" in r.message for r in caplog.records)

    def test_query_ok_logged(self, engine, sample_df, caplog):
        engine.register_table("fruit", sample_df)
        with caplog.at_level(logging.INFO):
            engine.execute_query("SELECT * FROM fruit")
        assert any("query ok" in r.message for r in caplog.records)

    def test_register_logged(self, engine, sample_df, caplog):
        with caplog.at_level(logging.INFO):
            engine.register_table("fruit", sample_df)
        assert any("register_table" in r.message for r in caplog.records)


class TestConcurrentAccess:
    """连接锁验证：单连接非线程安全的 DuckDB 必须串行化。"""

    def test_concurrent_register(self):
        eng = AnalysisEngine(db_path=None)
        dfs = {f"t{i}": pd.DataFrame({"x": [i]}) for i in range(8)}
        errors: list[Exception] = []

        def work(name, df):
            try:
                eng.register_table(name, df)
            except Exception as exc:  # 收集异常：任何一次失败都不该发生
                errors.append(exc)

        threads = [threading.Thread(target=work, args=item) for item in dfs.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert set(dfs) <= eng._loaded
        assert all(n in eng.list_tables() for n in dfs)
        eng.close()

    def test_concurrent_query(self, engine, sample_df):
        engine.register_table("fruit", sample_df)
        results: list[int] = []

        def q():
            results.append(
                int(engine.execute_query("SELECT sum(price) AS s FROM fruit").iloc[0]["s"])
            )

        threads = [threading.Thread(target=q) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == [16, 16, 16, 16]


class TestPersistence:
    """数据落盘：文件模式重启（close + 重开）后表必须还在。"""

    def test_tables_survive_reopen(self, tmp_path):
        path = str(tmp_path / "persist.duckdb")
        eng = AnalysisEngine(db_path=path)
        eng.register_table("t", pd.DataFrame({"x": [1, 2, 3]}))
        eng.close()

        # 模拟服务重启：同一个文件重新打开
        eng2 = AnalysisEngine(db_path=path)
        try:
            assert "t" in eng2.list_tables()
            df = eng2.execute_query('SELECT sum(x) AS s FROM "t"')
            assert df.iloc[0]["s"] == 6
            # _loaded 也要跟着恢复，否则 cache_status 与实际表清单不符
            assert "t" in eng2._loaded
        finally:
            eng2.close()

    def test_reopened_table_replaceable(self, tmp_path):
        # 重启后同名上传：CREATE OR REPLACE 语义覆盖旧数据而不是并存
        path = str(tmp_path / "persist.duckdb")
        eng = AnalysisEngine(db_path=path)
        eng.register_table("t", pd.DataFrame({"x": [1]}))
        eng.close()

        eng2 = AnalysisEngine(db_path=path)
        try:
            eng2.register_table("t", pd.DataFrame({"x": [9, 9]}))
            df = eng2.execute_query("SELECT count(*) AS n FROM t")
            assert df.iloc[0]["n"] == 2
        finally:
            eng2.close()

    def test_memory_mode_stays_isolated(self):
        # 回归保障：db_path=None 依旧不落盘，重开同名连接表已消失
        eng = AnalysisEngine(db_path=None)
        eng.register_table("t", pd.DataFrame({"x": [1]}))
        eng.close()
        eng2 = AnalysisEngine(db_path=None)
        try:
            assert eng2.list_tables() == []
        finally:
            eng2.close()
