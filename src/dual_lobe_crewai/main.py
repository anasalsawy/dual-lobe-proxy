from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dotenv import load_dotenv

from .engines import GatedEngine, NonSplitEngine, SplitEngine


def build_engine(mode: str):
    if mode == "gated":
        return GatedEngine()
    if mode in {"non-split", "nonsplit", "non_split"}:
        return NonSplitEngine()
    if mode == "split":
        return SplitEngine()
    raise ValueError(f"Unknown mode: {mode}")


async def _amain(mode: str, task: str, show_meta: bool):
    engine = build_engine(mode)
    result = await engine.run(task)
    print(result.visible_text())
    if show_meta:
        print("\n--- internal test metadata ---")
        print(json.dumps({
            "mode": result.mode,
            "route": result.route.model_dump() if result.route else None,
            "verdict": result.verdict.model_dump(),
            "timings_ms": result.timings_ms,
            "logical_model_calls": result.logical_model_calls,
            "route_source": result.route_source,
        }, indent=2))


def main():
    load_dotenv()
    # Windows redirected stdout commonly defaults to cp1252; keep Unicode output safe.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["gated", "non-split", "split"], default="gated")
    p.add_argument("--task", required=True)
    p.add_argument("--show-meta", action="store_true")
    args = p.parse_args()
    asyncio.run(_amain(args.mode, args.task, args.show_meta))


if __name__ == "__main__":
    main()
