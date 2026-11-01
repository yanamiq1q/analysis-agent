"""演示数据：给报告补两个月的日汇总（幂等、可回滚）。

为什么单独一个模块
------------------
环比要能演示，数据里必须有"上一个月"。而真实仓库的
``ads_order_daily_report`` 只有 2026-10 的前 16 天——拿半个月去和整月比，
正是 PR4 要消灭的那种错误。

所以这里只做一件事：**把 2026-09 整月和 2026-10 后半月补齐**，
让"整月 vs 整月"的环比成立。只碰这一张表、只碰这两个区间：

  2026-09-01 ~ 2026-10-01   整月 30 行
  2026-10-17 ~ 2026-11-01   原表缺的后半月 15 行

原表的 2026-10-01 ~ 10-16 一行不动。``clean=True`` 只删上面两段，
删完就回到原始状态。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb

TABLE = "ads_order_daily_report"
SEED_RANGES = (
    ("2026-09-01", "2026-10-01"),
    ("2026-10-17", "2026-11-01"),
)


class DemoDataError(RuntimeError):
    """演示数据准备失败（消息面向用户）。"""


def _rows() -> list[tuple]:
    """确定性的造数：同一天永远得到同一个值，方便对比两次运行。"""
    rows: list[tuple] = []
    # 9 月：gmv 从 7 万起，逐日 +500
    for day in range(1, 31):
        rows.append((
            date(2026, 9, day),
            60 + day,
            round(70000 + day * 500.0, 2),
            round(0.68 + (day % 5) * 0.005, 4),
        ))
    # 10 月后半月：整体高一个档，让环比为正
    for day in range(17, 32):
        rows.append((
            date(2026, 10, day),
            70 + day,
            round(100000 + day * 500.0, 2),
            round(0.70 + (day % 5) * 0.005, 4),
        ))
    return rows


def seed(db_path: str | Path, clean: bool = False) -> dict:
    """补数（clean=True 时只回滚，不插入）。返回统计信息。"""
    path = str(db_path)
    try:
        con = duckdb.connect(path)
    except (duckdb.IOException, OSError) as exc:
        raise DemoDataError(
            "打不开数据仓库（DuckDB 单进程独占）：请先停掉 data-agent 服务再跑本脚本。"
        ) from exc

    try:
        exists = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [TABLE],
        ).fetchone()[0]
        if not exists:
            raise DemoDataError(
                f"表 {TABLE} 不存在：先导入演示库或上传一份日汇总数据。"
            )

        removed = 0
        for start, end in SEED_RANGES:
            removed += con.execute(
                f"DELETE FROM {TABLE} WHERE dt >= DATE '{start}' AND dt < DATE '{end}'"
            ).fetchone()[0]
        if clean:
            return {"cleaned": True, "removed": removed, "inserted": 0}

        rows = _rows()
        con.executemany(
            f"INSERT INTO {TABLE} (dt, order_cnt, gmv, pay_rate) VALUES (?, ?, ?, ?)",
            rows,
        )
        total = con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
        return {"cleaned": False, "removed": removed, "inserted": len(rows),
                "total_rows": total}
    finally:
        con.close()
