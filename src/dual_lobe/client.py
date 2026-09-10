"""Interactive text test against a running proxy. No mock models or tool execution.

python -m dual_lobe.client --url http://localhost:8801 --run my-test
Set DUAL_LOBE_PROXY_KEY or enter the proxy key at the hidden prompt.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import uuid
from urllib.parse import quote

import httpx


def delivery(headers) -> str:
    return (f"Prepared for A: memory={headers.get('x-dual-lobe-memory', 'unknown')}, "
            f"claims={headers.get('x-dual-lobe-claims', 'unknown')}, "
            f"monitoring={headers.get('x-dual-lobe-monitoring', 'unknown')}")


async def reply(client: httpx.AsyncClient, messages: list[dict], run: str) -> dict:
    content = ""
    finished = False
    pending_tools = []
    async with client.stream("POST", "/v1/chat/completions", headers={"X-DL-Run-ID": run},
                             json={"model": "lobe-a", "messages": messages, "stream": True}) as response:
        if response.status_code != 200:
            await response.aread()
            raise RuntimeError(f"Proxy returned HTTP {response.status_code}; check configuration/server logs.")
        print(delivery(response.headers))
        print("A: ", end="", flush=True)
        try:
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if "error" in chunk:
                    raise RuntimeError("Upstream stream interrupted; the text above is incomplete.")
                for choice in chunk.get("choices") or []:
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta") or {}
                    text = delta.get("content") or ""
                    if delta.get("tool_calls"):
                        pending_tools.extend(delta["tool_calls"])
                    content += text
                    print(text, end="", flush=True)
                    if choice.get("finish_reason") is not None:
                        finished = True
        finally:
            print()
    if pending_tools:
        raise RuntimeError("A requested a tool, but this chat test has no executor; no tool was run.")
    if not finished:
        raise RuntimeError("The stream ended without a terminal result; do not treat it as complete.")
    return {"role": "assistant", "content": content}


async def show_state(client: httpx.AsyncClient, run: str) -> None:
    response = await client.get("/v1/dual-lobe/state/" + quote(run, safe=""))
    if response.status_code == 404:
        print("No run yet. Send your first message.")
        return
    if response.status_code != 200:
        print(f"State unavailable (HTTP {response.status_code}); the key needs state:read.")
        return
    state = response.json()
    payload = state.get("payload") or {}
    print("Latest stored B state (may be newer than the last delivery receipt):")
    print(json.dumps({"revision": state.get("revision"),
                      "oversight_status": payload.get("oversight_status", "pending"),
                      "memory_status": payload.get("context_memory_status", "none"),
                      "context_memory": payload.get("context_memory"),
                      "claim_review": payload.get("claim_review")}, indent=2, ensure_ascii=False))


async def conversation(url: str, key: str, run: str) -> None:
    messages = []
    async with httpx.AsyncClient(base_url=url.rstrip("/"),
                                 headers={"Authorization": "Bearer " + key},
                                 timeout=httpx.Timeout(240, connect=10)) as client:
        ready = await client.get("/readyz")
        if ready.status_code != 200:
            raise RuntimeError(f"Proxy is not ready (HTTP {ready.status_code}).")
        print(f"Live proxy conversation: {run}")
        print("/state inspects B's stored memory and claims; /quit exits. Only A speaks in the chat.")
        print("This client holds chat history for this session and does not execute tools.")
        while True:
            try:
                text = await asyncio.to_thread(input, "You: ")
            except EOFError:
                return
            if text.strip() == "/quit":
                return
            if text.strip() == "/state":
                await show_state(client, run)
                continue
            if not text.strip():
                continue
            messages.append({"role": "user", "content": text})
            try:
                answer = await reply(client, messages, run)
            except (RuntimeError, ValueError, httpx.HTTPError) as exc:
                # Do not silently store a partial answer as a completed turn or retry it.
                messages.pop()
                message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                print(f"Incomplete turn: {message}")
                continue
            messages.append(answer)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("DUAL_LOBE_PROXY_URL", "http://localhost:8801"))
    parser.add_argument("--run", default="conversation-test-" + uuid.uuid4().hex[:12])
    args = parser.parse_args()
    key = os.environ.get("DUAL_LOBE_PROXY_KEY", "").strip()
    if not key:
        key = getpass.getpass("Proxy API key (hidden): ").strip()
    if not key:
        parser.error("A proxy key is required. Do not enter your provider key here.")
    try:
        asyncio.run(conversation(args.url, key, args.run))
    except (KeyboardInterrupt, EOFError):
        pass
    except (RuntimeError, ValueError, httpx.HTTPError) as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        raise SystemExit(f"Test could not start: {message}") from None


if __name__ == "__main__":
    main()
