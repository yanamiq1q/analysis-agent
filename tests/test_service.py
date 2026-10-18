"""DataService 集成测试：文件 → 引擎 → 预览/查询 全链路。"""
import pandas as pd
import pytest

from data import AnalysisEngine, DataService
from data.engine import QueryError


@pytest.fixture()
def service():
    eng = AnalysisEngine(db_path=None)
    yield DataService(engine=eng)
    eng.close()


@pytest.fixture()
def csv_path(tmp_path):
    path = tmp_path / "sales.csv"
    pd.DataFrame(
        {"region": ["east", "west", "east"], "amount": [100, 200, 300]}
    ).to_csv(path, index=False)
    return str(path)


class TestLoadQuery:
    def test_load_then_query(self, service, csv_path):
        tables = service.load_file(csv_path)
        assert tables == ["sales"]
        df = service.query("SELECT region, sum(amount) AS s FROM sales GROUP BY region")
        assert len(df) == 2
        assert df[df.region == "east"].iloc[0]["s"] == 400

    def test_preview(self, service, csv_path):
        service.load_file(csv_path)
        pv = service.preview("sales", max_rows=2)
        assert pv["table"] == "sales"
        assert len(pv["rows"]) == 2
        assert {c["name"] for c in pv["columns"]} == {"region", "amount"}

    def test_describe(self, service, csv_path):
        service.load_file(csv_path)
        desc = service.describe("sales")
        assert desc["row_count"] == 3
        assert desc["columns"]["region"]["unique"] == 2
        assert desc["columns"]["amount"]["nulls"] == 0

    def test_preview_missing_table(self, service):
        with pytest.raises(QueryError, match="表不存在"):
            service.preview("nope")


class TestSourceAliasMapping:
    def test_multi_sheet_alias_keeps_all_tables(self, service, tmp_path):
        """P0 修复：多 sheet Excel 的别名映射必须保留全部表，而不是只有最后一张。"""
        path = tmp_path / "book.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            pd.DataFrame({"a": [1]}).to_excel(writer, sheet_name="S1", index=False)
            pd.DataFrame({"b": [2]}).to_excel(writer, sheet_name="S2", index=False)

        tables = service.load_file(str(path), source_name="book")
        assert set(tables) == {"book_S1", "book_S2"}
        assert service._sources["book"] == ["book_S1", "book_S2"]
