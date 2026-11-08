"""对比两次黄金集结果：谁退化了、谁修好了。

    python scripts/eval_diff.py                      # 自动取最近两次
    python scripts/eval_diff.py a.json b.json        # 指定

有退化（PASS → FAIL）时退出码 1，方便挂进 CI 或 pre-push。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _by_id(report: dict) -> dict[str, bool]:
    return {r["id"]: bool(r["ok"]) for r in report.get("results", [])}


def diff(old: dict, new: dict) -> dict:
    a, b = _by_id(old), _by_id(new)
    common = sorted(set(a) & set(b))
    regressed = [k for k in common if a[k] and not b[k]]
    fixed = [k for k in common if not a[k] and b[k]]
    only_old = sorted(set(a) - set(b))
    only_new = sorted(set(b) - set(a))
    return {
        "old_accuracy": old.get("accuracy", 0.0),
        "new_accuracy": new.get("accuracy", 0.0),
        "regressed": regressed,
        "fixed": fixed,
        "only_in_old": only_old,
        "only_in_new": only_new,
        "comparable": len(common),
    }


def main(argv: list[str] | None = None) -> int:
    args = (argv if argv is not None else sys.argv[1:])
    files = [Path(p) for p in args]
    if not files:
        files = sorted((ROOT / "data" / "warehouse" / "eval").glob("golden-*.json"))[-2:]
    if len(files) < 2:
        print("需要至少两份结果（先跑两次 eval_golden.py）", file=sys.stderr)
        return 2

    old, new = _load(files[0]), _load(files[1])
    result = diff(old, new)
    print(f"旧: {files[0].name}  准确率 {result['old_accuracy']:.1%}")
    print(f"新: {files[1].name}  准确率 {result['new_accuracy']:.1%}")
    print(f"可对比题数 {result['comparable']}")
    print(f"修好 {len(result['fixed'])}: {result['fixed'] or '无'}")
    print(f"退化 {len(result['regressed'])}: {result['regressed'] or '无'}")
    if result["only_in_old"] or result["only_in_new"]:
        print(f"仅旧有 {result['only_in_old']}，仅新有 {result['only_in_new']}")
    return 1 if result["regressed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
