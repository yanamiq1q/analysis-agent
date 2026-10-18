"""数据源加载器：把各种格式的文件读成 pandas DataFrame。

M1 只实现 CSV / Excel。每个 loader 返回一个统一的加载结果，
方便上层(engine)直接注册。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

import config


@dataclass
class LoadResult:
    """一次加载的产物：每个 sheet/表 一个 DataFrame。"""
    source_name: str          # 用户给的别名（如"订单数据"），缺省用文件名
    frames: dict[str, pd.DataFrame] = field(default_factory=dict)  # {表名: df}

    @property
    def table_names(self) -> list[str]:
        return list(self.frames.keys())


def load_file(path: str | Path, source_name: str = "") -> LoadResult:
    """按扩展名分发到具体 loader。入口统一走这里。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    if path.stat().st_size > config.MAX_UPLOAD_BYTES:
        raise ValueError(f"文件超过大小限制 {config.MAX_UPLOAD_BYTES // (1024*1024)}MB")
    if path.suffix.lower() not in config.ALLOWED_EXTENSIONS:
        raise ValueError(f"不支持的文件类型: {path.suffix}（支持 {sorted(config.ALLOWED_EXTENSIONS)}）")

    name = source_name or path.stem
    suffix = path.suffix.lower()

    if suffix == ".csv":
        return _load_csv(path, name)
    if suffix in (".xlsx", ".xls"):
        return _load_excel(path, name)
    if suffix == ".json":
        return _load_json(path, name)
    if suffix == ".parquet":
        return _load_parquet(path, name)
    raise ValueError(f"未实现的加载器: {suffix}")


def _load_csv(path: Path, name: str) -> LoadResult:
    # 编码兜底：默认 utf-8，失败降级 gb18030（覆盖国内 Excel 导出的 GBK 文件）。
    # gb18030 是 GBK 的超集，能解的字符更多。
    try:
        df = pd.read_csv(path, encoding="utf-8")
    except UnicodeDecodeError:
        df = pd.read_csv(path, encoding="gb18030")
    return LoadResult(source_name=name, frames={name: df})


def _load_excel(path: Path, name: str) -> LoadResult:
    """Excel 可能有多个 sheet，全部读出来。

    sheet 数超过 MAX_SOURCE_TABLES 直接拒绝：一个 Excel 塞成百上千
    sheet 会把引擎灌满，必须在上游拦住。
    """
    frames: dict[str, pd.DataFrame] = {}
    xls = pd.ExcelFile(path)
    if len(xls.sheet_names) > config.MAX_SOURCE_TABLES:
        raise ValueError(
            f"sheet 数量 {len(xls.sheet_names)} 超过上限 {config.MAX_SOURCE_TABLES}"
        )
    for sheet in xls.sheet_names:
        df = xls.parse(sheet)
        frames[f"{name}_{sheet}" if len(xls.sheet_names) > 1 else name] = df
    return LoadResult(source_name=name, frames=frames)


def _load_json(path: Path, name: str) -> LoadResult:
    df = pd.read_json(path)
    return LoadResult(source_name=name, frames={name: df})


def _load_parquet(path: Path, name: str) -> LoadResult:
    df = pd.read_parquet(path)
    return LoadResult(source_name=name, frames={name: df})
