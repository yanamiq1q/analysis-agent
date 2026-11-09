"""打印数据链路每一步的 state 参数，用来观察"问题 → SQL"到底怎么走。

    python scripts/trace_pipeline.py "2026 年 10 月的 GMV 是多少？"
    python scripts/trace_pipeline.py "支付转化率是多少？" --schema-chars 200

输出的是 LangGraph 的 stream_mode="updates"：每个节点只吐自己改了的字段。
所以能直接看到 attempts 怎么涨、error 怎么被回喂、SQL 怎么变。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from agent.graph import build_graph  # noqa: E402
from agent.llm import build_deepseek_generator  # noqa: E402
from agent.skill_loader import build_metric_registry, parse_skill_dir  # noqa: E402
from data.service import DataService  # noqa: E402


def _clip(text: str, limit: int = 240) -> str:
    text = (text or "").replace("\n", " / ")
    return text if len(text) <= limit else text[:limit] + "..."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跟踪数据链路的 state 变化")
    parser.add_argument("question", nargs="?", default="2026 年 10 月的 GMV 是多少？")
    parser.add_argument("--schema-chars", type=int, default=180)
    args = parser.parse_args(argv)

    service = DataService()
    skills, _ = parse_skill_dir(config.PROJECT_ROOT / "skills")
    registry = build_metric_registry(skills)
    graph = build_graph(service, build_deepseek_generator(), metric_registry=registry)

    print(f"QUESTION: {args.question}")
    print("=" * 72)
    step = 0
    final = {}
    for chunk in graph.stream({"question": args.question, "history": []},
                              stream_mode="updates"):
        for node, delta in chunk.items():
            step += 1
            delta = delta or {}
            print(f"[{step:02d}] {node}")
            if "schema_text" in delta:
                schema = delta["schema_text"]
                print(f"     schema_text: {_clip(schema, args.schema_chars)}")
                print(f"     len={len(schema)}  含口径提示={'【口径提示】' in schema}")
            if "attempts" in delta:
                print(f"     attempts={delta['attempts']}")
            if "sql" in delta:
                print(f"     sql: {_clip(delta['sql'])}")
            if "last_sql" in delta:
                print(f"     last_sql: {_clip(delta['last_sql'], 120)}")
            if "error" in delta:
                print(f"     error: {_clip(delta['error'])}")
            if "df" in delta and delta["df"] is not None:
                df = delta["df"]
                print(f"     df.shape={df.shape}  columns={list(df.columns)[:6]}")
            if "result_text" in delta:
                print(f"     result_text: {_clip(delta['result_text'])}")
            final.update(delta)
            print("-" * 72)

    print(f"FINAL: {_clip(final.get('result_text') or final.get('error') or '', 300)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
