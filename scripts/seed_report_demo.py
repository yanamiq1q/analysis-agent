"""给报告演示补两个月的日汇总数据（幂等、可回滚）。

    # 1) 先停掉 data-agent（DuckDB 单进程独占，服务在跑时打不开）
    # 2) 补数
    .venv\\Scripts\\python scripts\\seed_report_demo.py
    # 3) 回滚
    .venv\\Scripts\\python scripts\\seed_report_demo.py --clean
    # 4) 重新启动服务，生成 2026-10 报告就能看到环比

只动 ads_order_daily_report 的 2026-09 与 2026-10-17~31，原表前 16 天不动。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from data.report.demo_data import DemoDataError, seed  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补/清报告演示用的日汇总数据")
    parser.add_argument("--db", default="", help="仓库路径（默认 config.DUCKDB_PATH）")
    parser.add_argument("--clean", action="store_true", help="只删除演示数据，回到原始状态")
    args = parser.parse_args(argv)

    db_path = args.db or config.DUCKDB_PATH
    if not db_path:
        print("[失败] config.DUCKDB_PATH 为空（内存模式），无法补数", file=sys.stderr)
        return 1
    try:
        result = seed(db_path, clean=args.clean)
    except DemoDataError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 脚本入口统一兜底
        print(f"[失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if result.get("cleaned"):
        print(f"已回滚：删除 {result['removed']} 行演示数据")
    else:
        print(f"已补数：新增 {result['inserted']} 行（删除旧演示行 {result['removed']}）")
        print(f"表 {db_path} 现有 {result.get('total_rows')} 行")
        print("重新启动服务后，生成 2026-10 报告即可看到环比。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
