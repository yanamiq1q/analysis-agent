"""M5a 测试：MySQL 直连 + 冷热分层 + 执行路由。

用 SQLite 内存引擎模拟 MySQL（Engine 注入设计的目的：不依赖
真 MySQL 就能验证路由逻辑）。StaticPool 保证所有 connect() 共享
同一个内存库（否则 sqlite:// 每次连接都是全新空库）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import config
import data.sources.mysql_source as mysql_source_mod
from agent.graph import build_graph
from data.engine import AnalysisEngine, QueryError
from data.service import DataService
from data.sources.mysql_source import MySQLSource, build_mysql_engine


@pytest.fixture
def sqlite_engine():
    """一个含 small(3 行) + big(5000 行) 两张表的内存库。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE small (id INTEGER PRIMARY KEY, v INTEGER)"))
        conn.execute(text("CREATE TABLE big (id INTEGER PRIMARY KEY, v INTEGER)"))
        conn.execute(text("INSERT INTO small VALUES (1,10),(2,20),(3,30)"))
        # 递归 CTE 造 5000 行"大表"
        conn.exec_driver_sql(
            "INSERT INTO big "
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<5000) "
            "SELECT x, x*2 FROM c"
        )
        conn.commit()
    return engine


@pytest.fixture
def svc(sqlite_engine):
    """已连接远端库的 service：small 搬进本地，big 留远端。"""
    service = DataService(AnalysisEngine(db_path=None))
    result = service.connect_mysql(sqlite_engine, lazy_limit=100)
    assert result["moved"] == ["small"]   # 热表搬运
    assert result["large"] == ["big"]     # 冷表留远端
    return service


class TestColdHotSplit:
    def test_list_tables_sees_both(self, svc):
        assert set(svc.list_tables()) == {"small", "big"}

    def test_local_table_has_schema(self, svc):
        cols = svc.table_schema("small")
        assert {c["name"] for c in cols} == {"id", "v"}

    def test_remote_table_has_schema(self, svc):
        # 远端大表的列信息从 information_schema 拿（SQLite 由 inspector 提供）
        cols = svc.table_schema("big")
        assert {c["name"] for c in cols} == {"id", "v"}

    def test_big_table_not_in_engine(self, svc):
        # 大表必须留在远端，不能进 DuckDB（冷热分层的本质）
        assert "big" not in svc.engine.list_tables()


class TestExecuteRouting:
    def test_local_table_goes_duckdb(self, svc):
        df = svc.execute("SELECT SUM(v) AS s FROM small")
        assert int(df.iloc[0]["s"]) == 60

    def test_remote_table_pushed_down(self, svc):
        # 远端聚合整条下推，只回传结果
        df = svc.execute('SELECT COUNT(*) AS c FROM "big"')
        assert int(df.iloc[0]["c"]) == 5000

    def test_remote_quoted_table_name(self, svc):
        # LLM 习惯给表名加双引号（DuckDB 风格），下推前由 sqlglot 转方言
        df = svc.execute('SELECT MAX(v) AS m FROM "big"')
        assert int(df.iloc[0]["m"]) == 10000

    def test_mixed_across_engine_rejected(self, svc):
        # 一条 SQL 同时碰本地表和远端表：明确报错而不是静默错算
        with pytest.raises(QueryError, match="跨引擎"):
            svc.execute(
                "SELECT a.id FROM small a JOIN big b ON a.id = b.id"
            )

    def test_unknown_table_rejected(self, svc):
        with pytest.raises(QueryError, match="未知的表"):
            svc.execute("SELECT * FROM nope")

    def test_garbage_sql_rejected(self, svc):
        # 非查询语句必然被拒：解析失败或引擎层"只允许 SELECT"，
        # 两条防线走哪条都行，但必须包装成 QueryError
        with pytest.raises(QueryError):
            svc.execute("NOT SQL AT ALL")


class TestPreviewRemote:
    def test_preview_big_table(self, svc):
        # 远端大表 preview：下推 LIMIT，不能把 5000 行全拉回来
        preview = svc.preview("big", max_rows=5)
        assert len(preview["rows"]) == 5

    def test_describe_remote_rejected(self, svc):
        # describe 全列统计不支持下推（会拖垮源库），明确拒绝
        with pytest.raises(QueryError, match="仅支持本地表"):
            svc.describe("big")


class _FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, question, schema_text, error_hint=""):
        self.calls.append((question, schema_text, error_hint))
        return self.answers.pop(0) if self.answers else "SELECT 1"


class TestGraphIntegration:
    def test_agent_queries_remote_big_table(self, svc):
        # 全链路：schema 收集（含远端表）→ LLM 生成 → 校验 → 路由下推
        llm = _FakeLLM('SELECT COUNT(*) AS c FROM "big"')
        graph = build_graph(svc, llm)
        state = graph.invoke({"question": "big 表有多少行"})

        assert state["attempts"] == 1
        assert "5000" in state["result_text"]

    def test_agent_queries_mixed_rejected_then_fixed(self, svc):
        # LLM 第一次跨引擎 JOIN → 被路由拒绝 → 错误回喂 → 第二次只用远端表
        llm = _FakeLLM(
            "SELECT a.id FROM small a JOIN big b ON a.id = b.id",
            'SELECT COUNT(*) AS c FROM "big"',
        )
        graph = build_graph(svc, llm)
        state = graph.invoke({"question": "统计 big 行数"})

        assert state["attempts"] == 2
        assert "跨引擎" in llm.calls[1][2]   # 错误信息回喂给 LLM
        assert "5000" in state["result_text"]


class TestPushdownGuards:
    """下推查询护栏：与 DuckDB 引擎同一套语义（行数上限/软超时）。"""

    def test_row_limit_exceeded(self, sqlite_engine):
        src = MySQLSource(sqlite_engine)
        with pytest.raises(QueryError, match="超过 5 行上限"):
            src.execute("SELECT * FROM big", max_rows=5)

    def test_row_limit_exactly_fits(self, sqlite_engine):
        # big 表 5000 行 → 只查前 5 行恰好放行
        src = MySQLSource(sqlite_engine)
        df = src.execute("SELECT * FROM big LIMIT 5", max_rows=5)
        assert len(df) == 5

    def test_aggregate_unaffected(self, sqlite_engine):
        src = MySQLSource(sqlite_engine)
        df = src.execute("SELECT COUNT(*) AS c FROM big", max_rows=5)
        assert int(df.iloc[0]["c"]) == 5000

    def test_timeout_raises(self, sqlite_engine):
        src = MySQLSource(sqlite_engine)
        # timeout=0：result(0) 不等待，确定性触发（同 test_engine 的踩坑修复）
        with pytest.raises(QueryError, match="未完成"):
            src.execute("SELECT 1", timeout_s=0)

    def test_failure_wrapped_as_query_error(self, sqlite_engine):
        src = MySQLSource(sqlite_engine)
        with pytest.raises(QueryError, match="SQL 执行失败"):
            src.execute("SELECT * FROM no_such_table")


class TestTranspileMysqlDialect:
    """M6：DuckDB → MySQL 方言转译行为。

    create_engine 是惰性的：只取 dialect 做字符串转换，不建立连接，
    因此测试里可以放心拿 "mysql+pymysql://root:x@..." 的假连接串。
    断言基线来自 sqlglot 实测输出（见 Temp 探针），锁定其"保持语义"
    的关键行为。
    """

    @pytest.fixture
    def mysql_src(self):
        return MySQLSource(create_engine("mysql+pymysql://root:x@localhost:3306/x"))

    def test_double_quote_identifiers_to_backticks(self, mysql_src):
        out = mysql_src._transpile(
            'SELECT "user_id", SUM("amount") AS gmv '
            'FROM "ods_order_info" GROUP BY "user_id"'
        )
        assert "`user_id`" in out
        assert "`ods_order_info`" in out
        assert '"' not in out

    def test_colon_colon_cast_to_cast(self, mysql_src):
        assert "CAST(x AS SIGNED)" in mysql_src._transpile("SELECT x::INT FROM t")

    def test_concat_operator_to_concat_func(self, mysql_src):
        assert "CONCAT('a', 'b')" in mysql_src._transpile(
            "SELECT 'a' || 'b' AS c FROM t"
        )

    def test_ilike_keeps_case_insensitive(self, mysql_src):
        # ILIKE 不能退化成语义不同的 LIKE：sqlglot 用 LOWER 套 LIKE 保持等价
        out = mysql_src._transpile("SELECT * FROM t WHERE name ILIKE '%a%'")
        assert "LOWER(name) LIKE LOWER('%a%')" in out

    def test_common_semantics_pass_through(self, mysql_src):
        out = mysql_src._transpile("SELECT CURRENT_DATE, now() FROM t")
        assert "CURRENT_DATE" in out and "NOW()" in out

    def test_sqlite_dialect_keeps_double_quotes(self):
        # _dialect_name 分支：sqlite（测试路径）保留双引号，不走反引号
        src = MySQLSource(create_engine("sqlite://"))
        out = src._transpile('SELECT * FROM "big"')
        assert '"big"' in out
        assert "`big`" not in out

    def test_parse_error_falls_back_to_original(self, mysql_src):
        # 转译失败回退原样发送：错误暴露给远端库 → QueryError → LLM 重试
        assert mysql_src._transpile("SELECT FROM") == "SELECT FROM"


class TestRowcountSampling:
    """count 采样：估算值落在阈值模糊区时用精确 COUNT 复核，
    防 information_schema 估算偏差把边界表错分冷热。"""

    def test_no_estimate_falls_back_to_exact(self, sqlite_engine):
        # SQLite 无 information_schema → est=None → 精确计数（原回退路径）
        src = MySQLSource(sqlite_engine)
        assert src.table_rowcount("big") == 5000

    def test_fuzzy_zone_reevaluated_by_exact(self, sqlite_engine):
        # 300000 落在默认阈值 500000 的模糊区 [250000, 1000000] → 复核
        src = MySQLSource(sqlite_engine)
        src._estimated_rowcount = lambda t: 300000
        assert src.table_rowcount("big") == 5000

    def test_estimate_far_below_zone_trusted(self, sqlite_engine):
        # 远低于模糊区：估得再偏也跨不过阈值，不复核直接采信
        src = MySQLSource(sqlite_engine)
        src._estimated_rowcount = lambda t: 100
        src._exact_rowcount = lambda t: pytest.fail("远离阈值时不应精确计数")
        assert src.table_rowcount("big") == 100

    def test_estimate_far_above_zone_trusted(self, sqlite_engine):
        # 远高于模糊区：必然判冷，不复核（对真大表 COUNT 很贵）
        src = MySQLSource(sqlite_engine)
        src._estimated_rowcount = lambda t: 5_000_000
        src._exact_rowcount = lambda t: pytest.fail("远离阈值时不应精确计数")
        assert src.table_rowcount("big") == 5_000_000

    def test_exact_count_timeout_treated_as_cold(self, sqlite_engine):
        # 精确计数超时 = 表大到连 COUNT 都跑不动 → 安全方向判冷表
        src = MySQLSource(sqlite_engine)
        src._estimated_rowcount = lambda t: 300000  # 模糊区内

        def boom(t):
            raise QueryError("查询超过 30 秒未完成")

        src._exact_rowcount = boom
        assert src.table_rowcount("big") == config.LAZY_LOAD_ROW_LIMIT + 1

    def test_fuzzy_limit_follows_caller(self, sqlite_engine):
        # 自定义 lazy_limit=100：模糊区变为 [50, 200]，est=150 触发复核
        src = MySQLSource(sqlite_engine)
        src._estimated_rowcount = lambda t: 150
        assert src.table_rowcount("big", fuzzy_limit=100) == 5000

    def test_connect_mysql_fuzzy_split(self, sqlite_engine, monkeypatch):
        # 端到端：估算 small/big 都是 150（对 lazy_limit=100 在模糊区），
        # 精确计数决定真实冷热——small(3 行)搬、big(5000 行)留
        monkeypatch.setattr(
            MySQLSource, "_estimated_rowcount", lambda self, t: 150
        )
        svc = DataService(AnalysisEngine(db_path=None))
        res = svc.connect_mysql(sqlite_engine, lazy_limit=100)
        assert sorted(res["moved"]) == ["small"]
        assert sorted(res["large"]) == ["big"]


class TestReconnectPool:
    """断线重连：Engine 必须带 pool_pre_ping（取连接前探活，失效自动
    换新连接）+ pool_recycle（连接寿命主动短于服务端 wait_timeout）。"""

    def test_build_engine_reconnect_options(self, monkeypatch):
        captured = {}

        def fake_create_engine(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return create_engine(url, **kwargs)

        monkeypatch.setattr(
            mysql_source_mod, "create_engine", fake_create_engine
        )
        build_mysql_engine("mysql+pymysql://root:x@localhost:3306/x")

        assert captured["url"] == "mysql+pymysql://root:x@localhost:3306/x"
        assert captured["pool_pre_ping"] is True
        assert captured["pool_recycle"] == config.MYSQL_POOL_RECYCLE_SECONDS