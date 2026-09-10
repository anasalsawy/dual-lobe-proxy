"""Run labeled B cases using the configured provider; no automatic efficacy verdict.

Default is a local prompt preview. --live explicitly invokes B and consumes quota.
Use tools.ux_probe separately to assess whether A acts on the resulting guidance.
"""
import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

from dual_lobe.b import context_shadow, prompts
from dual_lobe.core.redact import redact_payload
from dual_lobe.core.settings import get_settings
from dual_lobe.provider.adapters import close_http_client


async def run(args):
    settings = get_settings()
    raw = Path(args.cases).read_text()
    cases = json.loads(raw)
    system = prompts.observer_instructions(settings)
    metadata = {
        "kind": "setup", "live": args.live, "cases_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "b_model": settings.resolved_b_model, "recorded_at": time.time(),
        "settings": {k: getattr(settings, k) for k in (
            "context_memory_enabled", "context_enrichment_enabled", "claim_checks_enabled",
            "b_timeout", "b_max_output_tokens", "b_rpm_limit", "max_shadow_input_chars")},
    }
    print(json.dumps(redact_payload(metadata)), flush=True)
    failures = 0
    try:
        for case in cases:
            prompt = prompts.build_cycle_prompt(case["context"], case["output"], "", "",
                                                max_chars=settings.max_shadow_input_chars,
                                                latest_request=case["latest_request"])
            record = {"case": case["id"], "rubric": case["rubric"],
                      "expected_signals": case["expected_signals"], "prompt": prompt, "system": system}
            if args.live:
                started = time.monotonic()
                try:
                    review = await context_shadow._obtain_review("lobe-b", prompt)
                    actual = sorted({c.signal for c in review.concerns})
                    record.update(ok=True, review=review.model_dump(), color=review.deception_level or "GREEN",
                                  color_reason=review.deception_reason,
                                  signals_match=actual == sorted(case["expected_signals"]))
                except Exception as exc:
                    failures += 1
                    record.update(ok=False, error_type=type(exc).__name__)
                record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            else:
                record.update(live=False, system=system)
            print(json.dumps(redact_payload(record), ensure_ascii=False), flush=True)
    finally:
        if args.live:
            await close_http_client()
    if failures:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="tools/evals/cases.json")
    parser.add_argument("--live", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
