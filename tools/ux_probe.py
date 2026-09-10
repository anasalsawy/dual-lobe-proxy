"""Reproducible text-only UX probe; failures never become successful samples.

Examples (keys in environment, not command history):
    python -m tools.ux_probe --url http://127.0.0.1:8801 --path /v1/chat/completions --model lobe-a --run probe-1 --state --observe-delay 12 --questions tools/evals/questions.txt
    python -m tools.ux_probe --url https://provider.example/v1 --model actual-model-id --key-env PROVIDER_API_KEY --questions tools/evals/questions.txt
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import httpx

from dual_lobe.core.redact import redact_payload


async def stream_turn(client: httpx.AsyncClient, url: str, messages: list[dict],
                      model: str, *, temperature: float = 0) -> tuple[dict, dict]:
    started = time.monotonic()
    content, first_token_ms = "", None
    tokens_in = tokens_out = None
    receipts, finished, done = {}, False, False
    async with client.stream("POST", url, json={"model": model, "messages": messages,
                                               "temperature": temperature, "stream": True}) as r:
        r.raise_for_status()
        receipts = {k: v for k, v in r.headers.items() if k.startswith("x-dual-lobe")}
        lines, size = [], 0
        async for line in r.aiter_lines():
            if line.startswith("data:"):
                value = line[5:].lstrip()
                size += len(value)
                if size > 2 * 1024 * 1024:
                    raise ValueError("oversize SSE event")
                lines.append(value)
                continue
            if line or not lines:
                continue
            data = "\n".join(lines)
            lines, size = [], 0
            if data.strip() == "[DONE]":
                done = True
                break
            chunk = json.loads(data)
            if not isinstance(chunk, dict) or "error" in chunk:
                raise ValueError("upstream SSE error or invalid event")
            usage = chunk.get("usage") or {}
            tokens_in = usage.get("prompt_tokens", tokens_in)
            tokens_out = usage.get("completion_tokens", tokens_out)
            for choice in chunk.get("choices") or []:
                if choice.get("index", 0) != 0 or finished:
                    raise ValueError("unexpected choice or content after finish")
                delta = choice.get("delta") or {}
                if delta.get("tool_calls") or delta.get("refusal"):
                    raise ValueError("text probe cannot complete a tool handoff or refusal")
                text = delta.get("content") or ""
                if text and first_token_ms is None:
                    first_token_ms = (time.monotonic() - started) * 1000
                content += text
                finish = choice.get("finish_reason")
                if finish is not None:
                    if finish != "stop":
                        raise ValueError("truncated or non-text completion")
                    finished = True
        if lines or not done or not finished or not content.strip():
            raise ValueError("incomplete or empty completion")
    return ({"ok": True, "ttft_ms": round(first_token_ms, 1),
             "total_ms": round((time.monotonic() - started) * 1000, 1),
             "tokens_in": tokens_in, "tokens_out": tokens_out,
             "words": len(content.split()), "text": content.strip()}, receipts)


async def run(args: argparse.Namespace) -> None:
    questions = [line for line in Path(args.questions).read_text().splitlines() if line.strip()]
    if not questions:
        raise ValueError("question set is empty")
    messages = []
    headers = {"Authorization": "Bearer " + args.key}
    if args.run:
        headers["X-DL-Run-ID"] = args.run
    if args.memory_id is not None:
        headers["X-DL-Memory-ID"] = args.memory_id

    def emit(record):
        encoded = json.dumps(redact_payload(record), ensure_ascii=False)
        # CLI-provided keys need not be present in the service's environment.
        print(encoded.replace(args.key, "[REDACTED]"), flush=True)

    emit({"kind": "setup", "model": args.model, "run": args.run, "memory_id": args.memory_id,
          "temperature": args.temperature, "observe_delay": args.observe_delay,
          "questions": questions, "recorded_at": time.time(),
          "metadata": json.loads(Path(args.metadata).read_text()) if args.metadata else {}})
    async with httpx.AsyncClient(base_url=args.url.rstrip("/"), headers=headers,
                                 timeout=httpx.Timeout(240, connect=10)) as client:
        for i, question in enumerate(questions, 1):
            messages.append({"role": "user", "content": question})
            try:
                result, receipts = await stream_turn(client, args.path, messages, args.model,
                                                      temperature=args.temperature)
            except Exception as exc:
                emit({"turn": i, "kind": "question", "q": question, "ok": False,
                      "error_type": type(exc).__name__})
                raise SystemExit(1) from None
            messages.append({"role": "assistant", "content": result["text"]})
            emit({"turn": i, "kind": "question", "q": question, **result, "receipts": receipts})
            # Include the final turn; inspect AFTER the optional observer pause.
            if args.observe_delay:
                await asyncio.sleep(args.observe_delay)
            if args.state:
                try:
                    response = await client.get("/v1/dual-lobe/state/" + args.run)
                    response.raise_for_status()
                    state = response.json()
                except Exception as exc:
                    emit({"turn": i, "kind": "state", "ok": False, "error_type": type(exc).__name__})
                    continue
                payload = state.get("payload") or {}
                source = payload.get("source_call")
                emit({"turn": i, "kind": "state", "ok": True, "revision": state.get("revision"),
                      "review_matches_turn": bool(source and source == receipts.get("x-dual-lobe-call-id")),
                      "payload": payload})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--key", help="Prefer --key-env to avoid shell-history exposure")
    parser.add_argument("--key-env", default="DUAL_LOBE_PROXY_KEY")
    parser.add_argument("--path", default="/chat/completions")
    parser.add_argument("--model", required=True)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--metadata", help="JSON file containing revision, arm label and non-secret server settings")
    parser.add_argument("--run")
    parser.add_argument("--memory-id")
    parser.add_argument("--state", action="store_true")
    parser.add_argument("--observe-delay", type=float, default=0)
    parser.add_argument("--temperature", type=float, default=0)
    args = parser.parse_args()
    args.key = args.key or os.environ.get(args.key_env)
    if not args.key:
        parser.error("set the requested key environment variable or supply --key")
    if args.state and not args.run:
        parser.error("--state requires --run")
    if not 0 <= args.observe_delay <= 60:
        parser.error("--observe-delay must be between 0 and 60 seconds")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
