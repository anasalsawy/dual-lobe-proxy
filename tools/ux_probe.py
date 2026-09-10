"""A/B UX probe for the dual-lobe proxy.

Sends the same multi-turn script to either the live proxy gateway or a provider
directly, and reports per-turn latency/shape/flow metrics plus the proxy's
observer delivery headers and stored B state. Repro harness for the assessment
in docs/UX_ASSESSMENT_WITH_VS_WITHOUT.md.

Usage:
    python -m tools.ux_probe --url http://127.0.0.1:8801 --key <proxy-key> --path /v1/chat/completions --state 1 --observe-delay 12 --questions q.txt
    python -m tools.ux_probe --url https://generativelanguage.googleapis.com/v1beta/openai --key <gemini-key> --path /chat/completions --questions q.txt
"""
from __future__ import annotations

import argparse
import json
import time

import httpx


async def stream_turn(client: httpx.AsyncClient, url: str, messages: list[dict],
                      model: str) -> tuple[dict, dict]:
    started = time.monotonic()
    content = ""
    first_token_ms = None
    tokens_in = tokens_out = None
    receipts = {}
    async with client.stream("POST", url,
                             json={"model": model, "messages": messages, "stream": True}) as r:
        for k, v in r.headers.items():
            if k.startswith("x-dual-lobe"):
                receipts[k] = v
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if "usage" in chunk:
                tokens_in = chunk["usage"].get("prompt_tokens") or tokens_in
                tokens_out = chunk["usage"].get("completion_tokens") or tokens_out
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                text = delta.get("content") or ""
                if text and first_token_ms is None:
                    first_token_ms = (time.monotonic() - started) * 1000
                content += text
    total_ms = (time.monotonic() - started) * 1000
    return ({"ttft_ms": round(first_token_ms or -1, 1), "total_ms": round(total_ms, 1),
             "tokens_in": tokens_in, "tokens_out": tokens_out,
             "words": len(content.split()), "text": content.strip()}, receipts)


# usage is optional; tokens stay None when the provider omits it
async def run(args: argparse.Namespace) -> None:
    questions = [ln for ln in (open(args.questions).read().splitlines()) if ln.strip()]
    messages: list[dict] = []
    base = args.url.rstrip("/")
    headers = {"Authorization": "Bearer " + args.key}
    if args.run:
        headers["X-DL-Run-ID"] = args.run
    async with httpx.AsyncClient(base_url=base, headers=headers,
                                 timeout=httpx.Timeout(240, connect=10)) as client:
        for i, q in enumerate(questions, 1):
            messages.append({"role": "user", "content": q})
            result, receipts = await stream_turn(client, args.path, messages, args.model)
            messages.append({"role": "assistant", "content": result["text"]})
            record = {"turn": i, "kind": "question"} | {"q": q} | result
            if receipts:
                record["receipts"] = {"memory": receipts.get("x-dual-lobe-memory"),
                                      "claims": receipts.get("x-dual-lobe-claims"),
                                      "monitoring": receipts.get("x-dual-lobe-monitoring")}
            print(json.dumps(record, ensure_ascii=False))
            if args.state and args.run:
                resp = await client.get("/v1/dual-lobe/state/" + args.run)
                if resp.status_code == 200:
                    s = resp.json()
                    payload = s.get("payload") or {}
                    mem = (payload.get("context_memory") or {}).get("content") or {}
                    claims = (payload.get("claim_review") or {}).get("concerns", [])
                    print(json.dumps({"turn": i, "kind": "state",
                                      "revision": s.get("revision"),
                                      "oversight_status": payload.get("oversight_status"),
                                      "memory_version": (payload.get("context_memory") or {}).get("version"),
                                      "goal": mem.get("goal", ""),
                                      "next_step": mem.get("next_step", ""),
                                      "n_claims": len(claims)}, ensure_ascii=False))
                else:
                    print(json.dumps({"turn": i, "kind": "state", "error": resp.status_code},
                                     ensure_ascii=False))
            if args.observe_delay and i < len(questions):
                time.sleep(args.observe_delay)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--path", default="/chat/completions")
    p.add_argument("--model", default="gemini-3.5-flash-lite")
    p.add_argument("--questions", required=True)
    p.add_argument("--run", default=None)
    p.add_argument("--state", action="store_true")
    p.add_argument("--observe-delay", type=float, default=0.0)
    args = p.parse_args()
    import asyncio
    asyncio.run(run(args))


if __name__ == "__main__":
    main()