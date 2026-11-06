"""黄金集评测入口。

    # 1) 先自检题目（不需要 API key）：参考 SQL 能不能跑通
    python scripts/eval_golden.py --mode oracle

    # 2) 真跑 Agent（需要 DEEPSEEK_API_KEY）
    python scripts/eval_golden.py --mode agent

    # 只看某一类 / 前 N 题
    python scripts/eval_golden.py --mode agent --category metric --limit 5

结果写到 data/warehouse/eval/golden-<时间>.json，同时打印一张表。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from agent.llm import build_deepseek_generator  # noqa: E402
from agent.skill_loader import build_metric_registry, parse_skill_dir  # noqa: E402
from data.quality.golden import evaluate, load_cases, oracle_check  # noqa: E402
from data.service import DataService  # noqa: E402

QUESTIONS = ROOT / "tests" / "golden" / "questions.yaml"


def _print_table(rows: list[dict]) -> None:
    for item in rows:
        mark = "PASS" if item.get("ok") else "FAIL"
        print(f"  [{mark}] {item['id']:<20} {item.get('detail', '')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="黄金集评测")
    parser.add_argument("--mode", choices=("oracle", "agent"), default="oracle")
    parser.add_argument("--questions", default=str(QUESTIONS))
    parser.add_argument("--category", default="", help="只跑某一类")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default="", help="结果 JSON 路径")
    args = parser.parse_args(argv)

    cases = load_cases(args.questions)
    if args.category:
        cases = [c for c in cases if c.category == args.category]
    if args.limit > 0:
        cases = cases[: args.limit]
    if not cases:
        print("[失败] 没有匹配的题目", file=sys.stderr)
        return 1

    service = DataService()

    if args.mode == "oracle":
        report = oracle_check(service, cases)
        _print_table(report["rows"])
        print(f"\n参考集自检: {report['passed']}/{report['total']}")
        return 0 if report["passed"] == report["total"] else 1

    # agent 模式
    llm = build_deepseek_generator()
    # 生产链路里 query_sql 是带口径注册表跑的；评测必须一致，
    # 否则口径题会"不公平地"失败（第一版就踩了这个）
    skills, _diagnostics = parse_skill_dir(config.PROJECT_ROOT / "skills")
    registry = build_metric_registry(skills)
    print(f"跑 {len(cases)} 题（model={config.DEEPSEEK_MODEL}，口径 {len(registry)} 条）…")
    started = time.monotonic()
    report = evaluate(service, llm, cases, metric_registry=registry,
                      progress=lambda r: print(f"  {'PASS' if r['ok'] else 'FAIL'} {r['id']:<20} {r['detail']}"))
    report["mode"] = "agent"
    report["model"] = config.DEEPSEEK_MODEL
    report["elapsed_s"] = round(time.monotonic() - started, 1)

    print("\n=== 汇总 ===")
    print(f"总题数 {report['total']}，通过 {report['passed']}，准确率 {report['accuracy']:.1%}")
    print(f"数值题 {report['numeric_passed']}/{report['numeric_total']}，"
          f"拦截题 {report['reject_passed']}/{report['reject_total']}")
    for category, bucket in sorted(report["by_category"].items()):
        print(f"  {category:<10} {bucket['passed']}/{bucket['total']}")

    out = Path(args.out) if args.out else (
        config.DATA_DIR / "eval" / f"golden-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
