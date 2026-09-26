from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dotenv import load_dotenv

from .engines import DualLobeEngine


async def _amain(task: str, show_meta: bool, loop_cycles: int):
    engine = DualLobeEngine()

    if loop_cycles > 1:
        results = await engine.run_loop(task, cycles=loop_cycles)
        result = results[-1]
    else:
        results = None
        result = await engine.run(task)

    print(result.visible_text())
    if show_meta:
        payload = {
            "architecture": "dual-lobe",
            "cycle_index": result.cycle_index,
            "canonical_state": result.canonical_state,
            "verdict": result.verdict.model_dump(),
            "challenges": result.challenges,
            "intent_risks": result.intent_risks,
            "overlooked_context": result.overlooked_context,
            "delegation_note": result.delegation_note,
            "timings_ms": result.timings_ms,
            "logical_model_calls": result.logical_model_calls,
        }
        if results is not None:
            payload["loop"] = [
                {
                    "cycle_index": r.cycle_index,
                    "verdict": r.verdict.model_dump(),
                    "logical_model_calls": r.logical_model_calls,
                    "timings_ms": r.timings_ms,
                }
                for r in results
            ]
        print("\n--- internal test metadata ---")
        print(json.dumps(payload, indent=2, ensure_ascii=False))


def main():
    load_dotenv()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    p = argparse.ArgumentParser(
        description="Dual-Lobe: delegation-first A plus persistent adversarial/verification B."
    )
    p.add_argument("--task", required=True)
    p.add_argument("--show-meta", action="store_true")
    p.add_argument("--loop-cycles", type=int, default=1)
    args = p.parse_args()
    asyncio.run(_amain(args.task, args.show_meta, args.loop_cycles))


if __name__ == "__main__":
    main()
