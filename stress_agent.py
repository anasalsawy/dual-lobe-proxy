"""Stress-test agent for the dual-lobe proxy.

Usage:
    python stress_agent.py --model lobe-a --level 1 --count 3
    python stress_agent.py --model lobe-a-flat --level 3 --count 10
    python stress_agent.py --model lobe-a-hierarchy --level 5 --count 20

Levels:
    1 = simple greeting/identity
    2 = short reasoning (math, logic)
    3 = multi-step planning
    4 = tool-use style structured request
    5 = long context + reasoning + output format
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = "http://localhost:8801/v1"
API_KEY = "dl_357a343ae5107707f9a6b708f0c5f39592519ef282e21d24"


PROMPTS = {
    1: [
        "Say hello and identify yourself in one sentence.",
        "What is your name and purpose?",
        "Greet the user briefly.",
    ],
    2: [
        "What is 17 * 34 + 91? Show your reasoning.",
        "If Alice is older than Bob and Bob is older than Charlie, who is the youngest? Explain.",
        "A train travels 120 km in 2 hours. What is its average speed?",
    ],
    3: [
        "Plan a 3-day trip to Tokyo. Include transportation, accommodation, and daily activities.",
        "Design a simple task queue system with retry logic. List components and their responsibilities.",
        "Write a step-by-step guide to onboard a new junior developer to a Python project.",
    ],
    4: [
        """You are a coding assistant. Respond in JSON with keys: "language", "code", "explanation".
Write a function that reverses a string without using built-in reverse methods.""",
        """You are a coding assistant. Respond in JSON with keys: "language", "code", "explanation".
Write a Python function that finds the longest word in a list.""",
        """You are a coding assistant. Respond in JSON with keys: "language", "code", "explanation".
Write a SQL query that returns the top 5 customers by total spend.""",
    ],
    5: [
        """You are an architecture reviewer. Read the following system description and produce:
1. A list of 5 strengths
2. A list of 5 risks
3. A prioritized action plan

System: A startup runs a monolithic Python Django app on a single VPS with a PostgreSQL database. Traffic is growing 50% month-over-month. They deploy manually via SSH and have no automated tests. They want to scale to 1M daily active users within 12 months.""",
        """You are a product strategist. Analyze the following scenario and output:
1. Target user segments
2. Key value propositions
3. Go-to-market channels
4. Top 3 success metrics
5. Biggest risks

Scenario: A new AI voice assistant for doctors that listens to patient consultations and automatically generates structured clinical notes.""",
        """You are a security auditor. Review the following deployment and list:
1. Critical vulnerabilities
2. Mitigation steps
3. Monitoring recommendations

Deployment: A web app stores user passwords as SHA256 hashes, uses HTTP on public Wi-Fi, and exposes a debug endpoint at /debug that returns environment variables.""",
    ],
}


@dataclass
class Result:
    model: str
    level: int
    index: int
    latency_ms: float
    status: str
    tokens: int
    error: str | None
    content: str


async def call_once(client: httpx.AsyncClient, model: str, level: int, index: int, prompt: str) -> Result:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "temperature": 0.3,
    }
    started = time.perf_counter()
    try:
        resp = await client.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        if resp.status_code != 200:
            return Result(model, level, index, latency_ms, f"http_{resp.status_code}", 0, resp.text[:200], "")
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")
        usage = data.get("usage", {})
        tokens = usage.get("total_tokens", 0)
        return Result(model, level, index, latency_ms, "ok", tokens, None, content)
    except httpx.TimeoutException:
        latency_ms = (time.perf_counter() - started) * 1000
        return Result(model, level, index, latency_ms, "timeout", 0, "timeout", "")
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000
        return Result(model, level, index, latency_ms, "error", 0, str(exc)[:200], "")


async def run_level(model: str, level: int, count: int) -> list[Result]:
    prompts = PROMPTS[level]
    async with httpx.AsyncClient() as client:
        tasks = [
            call_once(client, model, level, i, prompts[i % len(prompts)])
            for i in range(count)
        ]
        return await asyncio.gather(*tasks)


def summarize(results: list[Result]) -> dict[str, Any]:
    total = len(results)
    ok = [r for r in results if r.status == "ok"]
    errors = [r for r in results if r.status != "ok"]
    latencies = [r.latency_ms for r in ok]
    tokens = [r.tokens for r in ok]
    return {
        "total": total,
        "success": len(ok),
        "errors": len(errors),
        "error_kinds": sorted(set(r.status for r in errors)),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "max_latency_ms": round(max(latencies), 1) if latencies else None,
        "min_latency_ms": round(min(latencies), 1) if latencies else None,
        "avg_tokens": round(sum(tokens) / len(tokens), 1) if tokens else None,
        "total_tokens": sum(tokens),
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="lobe-a", choices=["lobe-a", "lobe-a-flat", "lobe-a-hierarchy"])
    parser.add_argument("--level", type=int, default=1)
    parser.add_argument("--count", type=int, default=3)
    args = parser.parse_args()

    print(f"Stress test: model={args.model} level={args.level} count={args.count}")
    results = await run_level(args.model, args.level, args.count)
    summary = summarize(results)
    print(json.dumps(summary, indent=2))
    for r in results:
        status_icon = "OK" if r.status == "ok" else "ERR"
        snippet = r.content.replace("\n", " ")[:80]
        print(f"{status_icon} [{r.status}] call {r.index}: {r.latency_ms:.0f}ms | {snippet}")


if __name__ == "__main__":
    asyncio.run(main())
