"""报告命令行入口（PR1 的自测/演示通道）。

    python -m data.report.run --list-templates
    python -m data.report.run --template monthly_ecom --period 2026-10
    python -m data.report.run --template monthly_ecom --period 2026-10-01:2026-10-31 --force

产物在 data/warehouse/report_jobs/{job_id}/ 下：report.json / assets/ / status.json。
不需要 DEEPSEEK_API_KEY——报表的 SQL 来自模板，不调 LLM。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import config
from data.report.pipeline import (
    TEMPLATES_DIR,
    ReportError,
    ReportRequest,
    parse_period,
    run_report,
)
from data.report.spec import summarize
from data.service import DataService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m data.report.run", description="生成结构化报告包（ReportSpec）"
    )
    parser.add_argument("--template", default="monthly_ecom", help="模板名（见 --list-templates）")
    parser.add_argument("--period", help="2026-10 或 2026-10-01:2026-10-31")
    parser.add_argument("--focus", default="", help="可选：本期关注点（写进 meta，影响 job_id）")
    parser.add_argument("--out", default="", help="产物根目录（默认 config.REPORT_JOBS_DIR）")
    parser.add_argument("--force", action="store_true", help="忽略已完成缓存，强制重跑")
    parser.add_argument("--list-templates", action="store_true", help="列出可用模板")
    args = parser.parse_args(argv)

    if args.list_templates:
        for path in sorted(TEMPLATES_DIR.glob("*.yaml")):
            print(path.stem)
        return 0

    if not args.period:
        parser.error("--period 必填（如 --period 2026-10）")

    try:
        start, end = parse_period(args.period)
        service = DataService()
        spec = run_report(
            service,
            ReportRequest(
                template=args.template,
                period_start=start,
                period_end=end,
                focus=args.focus,
                force=args.force,
            ),
            out_root=Path(args.out) if args.out else None,
        )
    except ReportError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # 库被占用/依赖坏了：也给人话，而不是堆栈
        print(f"[失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    out_dir = (Path(args.out) if args.out else config.REPORT_JOBS_DIR) / spec.job_id
    print(f"报告: {out_dir / 'report.json'}")
    print(summarize(spec))
    for section in spec.sections:
        detail = f"[跳过] {section.skipped}" if section.skipped else (
            f"{len(section.metrics)} 指标 / {len(section.charts)} 图 / "
            f"{len(section.tables)} 表"
        )
        print(f"  - {section.heading}: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
