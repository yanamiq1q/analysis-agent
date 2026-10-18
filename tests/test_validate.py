"""M2 测试：sqlglot AST 安全校验。"""
import pytest

import config
from data.validate import validate_sql


class TestQueryAccept:
    def test_select_passes(self):
        assert validate_sql("SELECT 1") is None

    def test_select_from_table_passes(self):
        assert validate_sql("SELECT * FROM orders WHERE amount > 100") is None

    def test_with_cte_passes(self):
        sql = "WITH top AS (SELECT * FROM orders) SELECT * FROM top"
        assert validate_sql(sql) is None

    def test_union_passes(self):
        sql = "SELECT a FROM t1 UNION ALL SELECT b FROM t2"
        assert validate_sql(sql) is None

    def test_subquery_passes(self):
        sql = "SELECT * FROM (SELECT id FROM orders) sub WHERE id > 10"
        assert validate_sql(sql) is None

    def test_case_when_passes(self):
        sql = "SELECT CASE WHEN amount > 100 THEN 'big' ELSE 'small' END FROM orders"
        assert validate_sql(sql) is None


class TestMultiStatement:
    def test_semicolon_injection_rejected(self):
        # M1 快闸唯一漏洞：SELECT 开头 + 分号注入
        sql = "SELECT 1; DROP TABLE orders"
        err = validate_sql(sql)
        assert err and "多语句" in err

    def test_three_statements_rejected(self):
        sql = "SELECT 1; SELECT 2; SELECT 3"
        assert validate_sql(sql) is not None

    def test_trailing_semicolon_ok(self):
        # 结尾一个分号是单语句，sqlglot 正常解析
        assert validate_sql("SELECT 1;") is None


class TestTopLevelRejected:
    @pytest.mark.parametrize(
        "sql,stmt_type",
        [
            ("UPDATE orders SET amount=0", "Update"),
            ("DELETE FROM orders", "Delete"),
            ("INSERT INTO orders VALUES (1)", "Insert"),
            ("DROP TABLE orders", "Drop"),
            ("CREATE TABLE t (id INT)", "Create"),
            ("ALTER TABLE orders ADD COLUMN x INT", "Alter"),
            ("TRUNCATE orders", "TruncateTable"),
        ],
    )
    def test_non_query_rejected(self, sql, stmt_type):
        err = validate_sql(sql)
        assert err and stmt_type in err


class TestBannedFunctions:
    def test_banned_anonymous_rejected(self):
        # copy_to 是 Anonymous 节点
        err = validate_sql("SELECT copy_to('t', 'out.csv') FROM orders")
        assert err and "copy_to" in err

    def test_pragma_statement_rejected(self):
        # PRAGMA 是元命令（可改连接设置），顶层语句必须拒绝
        err = validate_sql("PRAGMA enable_external_access=false")
        assert err and "Pragma" in err

    def test_select_pragma_function_passes(self):
        # pragma_version() 只是只读元数据函数，不该误杀（AST 校验的优势）
        assert validate_sql("SELECT pragma_version()") is None

    def test_banned_in_subquery_rejected(self):
        # 危险函数藏在子查询里也必须拦截（walk 是递归的）
        sql = "SELECT * FROM (SELECT load('http_ext') ) x"
        assert validate_sql(sql) is not None

    def test_copy_node_rejected(self):
        # COPY ... TO 顶层语句直接拒绝
        err = validate_sql("COPY orders TO 'out.csv'")
        assert err and "只允许" in err


class TestStringLiteralNotFlagged:
    """AST 校验相对关键字黑名单的核心优势：字符串里的危险词不误杀。"""

    def test_column_named_update_passes(self):
        assert validate_sql("SELECT update_time FROM orders") is None

    def test_literal_contains_danger_word_passes(self):
        sql = "SELECT * FROM orders WHERE note = 'please DELETE this row'"
        assert validate_sql(sql) is None


class TestFileReadPathWhitelist:
    def test_read_csv_inside_data_dir_passes(self, tmp_path):
        # 白名单根 = config.DATA_DIR；tmp_path 不在白名单 → 拒绝
        # 这里构造"允许"场景：用 config.DATA_DIR 下的真实路径
        target = config.DATA_DIR / "sample.csv"
        target.write_text("a,b\n1,2\n")
        sql = f"SELECT * FROM read_csv('{target.as_posix()}')"
        assert validate_sql(sql) is None
        target.unlink()

    def test_read_csv_outside_whitelist_rejected(self, tmp_path):
        # 临时目录下的文件不在白名单根内 → 拒绝
        f = tmp_path / "secret.csv"
        f.write_text("x\n1\n")
        sql = f"SELECT * FROM read_csv('{f.as_posix()}')"
        err = validate_sql(sql)
        assert err and "目录" in err

    def test_read_csv_network_url_rejected(self):
        err = validate_sql("SELECT * FROM read_csv('http://evil.com/x.csv')")
        assert err and "网络路径" in err

    def test_read_csv_non_literal_rejected(self):
        # 变量拼接路径（read_csv(@var)）必须拒绝
        err = validate_sql("SELECT * FROM read_csv(@var)")
        assert err and "字面量" in err

    def test_quoted_table_file_scan_rejected(self):
        # SELECT * FROM 'C:/windows/system.ini' —— DuckDB 文件扫描绕过点
        err = validate_sql("SELECT * FROM 'C:/windows/system.ini'")
        assert err and "目录" in err

    def test_plain_table_name_passes(self):
        # 非引用的普通表名不是文件路径，正常放行
        assert validate_sql("SELECT * FROM orders") is None


class TestParseError:
    def test_garbage_sql_rejected(self):
        assert validate_sql("THIS IS NOT SQL !!!") is not None


class TestTableWhitelist:
    """M4：allowed_tables 表名白名单。"""

    def test_allowed_table_passes(self):
        assert validate_sql("SELECT * FROM orders", allowed_tables=["orders"]) is None

    def test_unknown_table_rejected(self):
        err = validate_sql(
            "SELECT * FROM secret_table", allowed_tables=["orders"]
        )
        assert err and "secret_table" in err
        # 错误信息要带上可用表清单（会作为 error_hint 回喂 LLM 修正）
        assert "orders" in err

    def test_join_both_must_be_allowed(self):
        err = validate_sql(
            "SELECT * FROM a JOIN b ON a.id = b.id", allowed_tables=["a"]
        )
        assert err and "b" in err

    def test_cte_alias_not_flagged(self):
        # CTE 别名不算物理表：WITH x AS (...) 里的 x 不应被白名单拦截
        sql = "WITH x AS (SELECT * FROM orders) SELECT * FROM x"
        assert validate_sql(sql, allowed_tables=["orders"]) is None

    def test_unrestricted_when_whitelist_none(self):
        # allowed_tables 缺省 = 不限制（向后兼容 M2 行为）
        assert validate_sql("SELECT * FROM anything") is None

    def test_case_insensitive(self):
        # DuckDB 表名大小写不敏感，白名单比较也要一致
        assert validate_sql("SELECT * FROM ORDERS", allowed_tables=["orders"]) is None
