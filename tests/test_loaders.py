"""loader 测试：CSV / Excel / JSON / Parquet 加载 + 校验失败分支。"""
from pathlib import Path

import pandas as pd
import pytest

from data.sources.loaders import load_file

SAMPLES = Path(__file__).parent / "samples"


def _write_csv(path: Path, df: pd.DataFrame) -> Path:
    df.to_csv(path, index=False)
    return path


@pytest.fixture(scope="module")
def sample_csv(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("samples")
    return _write_csv(
        tmp / "orders.csv",
        pd.DataFrame({"order_id": [1, 2, 3], "amount": [10.5, 20, 30]}),
    )


class TestCsv:
    def test_load_csv(self, sample_csv):
        result = load_file(sample_csv)
        assert result.table_names == ["orders"]
        df = result.frames["orders"]
        assert len(df) == 3
        assert list(df.columns) == ["order_id", "amount"]

    def test_custom_source_name(self, sample_csv):
        result = load_file(sample_csv, source_name="我的订单")
        assert result.source_name == "我的订单"

    def test_gb18030_encoding_fallback(self, tmp_path):
        # P1 修复：GBK 导出的中文 CSV（utf-8 解不开）应自动降级 gb18030
        path = tmp_path / "gbk.csv"
        text = "姓名,金额\n张三,100\n李四,200\n"
        path.write_bytes(text.encode("gb18030"))
        result = load_file(path)
        df = result.frames["gbk"]
        assert list(df.columns) == ["姓名", "金额"]
        assert df.iloc[0]["姓名"] == "张三"


class TestExcel:
    def test_load_multi_sheet(self, tmp_path):
        path = tmp_path / "multi.xlsx"
        with pd.ExcelWriter(path) as writer:
            pd.DataFrame({"a": [1, 2]}).to_excel(writer, sheet_name="S1", index=False)
            pd.DataFrame({"b": [3]}).to_excel(writer, sheet_name="S2", index=False)
        result = load_file(path)
        assert set(result.table_names) == {"multi_S1", "multi_S2"}

    def test_load_single_sheet_no_suffix(self, tmp_path):
        path = tmp_path / "one.xlsx"
        pd.DataFrame({"x": [1]}).to_excel(path, index=False)
        result = load_file(path)
        assert result.table_names == ["one"]

    def test_too_many_sheets_rejected(self, tmp_path, monkeypatch):
        # P1 修复：sheet 数超过 MAX_SOURCE_TABLES 必须上游拒绝
        import config

        monkeypatch.setattr(config, "MAX_SOURCE_TABLES", 2)
        path = tmp_path / "many.xlsx"
        with pd.ExcelWriter(path) as writer:
            for i in range(3):
                pd.DataFrame({"v": [i]}).to_excel(writer, sheet_name=f"S{i}", index=False)
        with pytest.raises(ValueError, match="超过上限"):
            load_file(path)


class TestJsonParquet:
    def test_load_json(self, tmp_path):
        path = tmp_path / "d.json"
        pd.DataFrame({"k": ["a", "b"]}).to_json(path, orient="records")
        result = load_file(path)
        assert len(result.frames["d"]) == 2

    def test_load_parquet(self, tmp_path):
        path = tmp_path / "p.parquet"
        pd.DataFrame({"n": [1, 2, 3]}).to_parquet(path)
        result = load_file(path)
        assert len(result.frames["p"]) == 3


class TestValidation:
    def test_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_file("no_such_file.csv")

    def test_bad_extension(self, tmp_path):
        path = tmp_path / "x.txt"
        path.write_text("hello")
        with pytest.raises(ValueError, match="不支持的文件类型"):
            load_file(path)
