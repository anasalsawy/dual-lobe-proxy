import asyncio
import time

import pytest

from dual_lobe.provider import ratelimit
from dual_lobe.provider.ratelimit import RateGate, UpstreamRateLimited, _seconds, detect_provider


def test_detect_provider_by_base_url():
    assert detect_provider("https://generativelanguage.googleapis.com/v1beta/openai") == "gemini"
    assert detect_provider("https://openrouter.ai/api/v1") == "openrouter"
    assert detect_provider("https://api.deepinfra.com/v1/openai") == "deepinfra"
    assert detect_provider("https://llm.example.com/v1") == "llm.example.com"


def test_seconds_parses_provider_reset_styles():
    assert _seconds("10") == 10
    assert _seconds("10s") == 10
    assert _seconds("PT15S") == 15
    assert _seconds("1h30m") == 5400
    assert _seconds("Mon, 01 Jan 2024 00:00:00 GMT") == 0
    assert _seconds(str(int(time.time()) + 30)) == pytest.approx(30, abs=2)
    assert _seconds(None) is None


def test_header_discovery_sets_ceiling():
    gate = RateGate("gemini|test", "gemini", rpm=None, tpm=None, rpd=None, max_wait=1)
    gate.observe({"X-RateLimit-Limit-Requests": "12", "X-RateLimit-Limit-Tokens": "500000"}, 200)
    assert gate._rpm == 12
    assert gate._ceil_rpm == 12
    assert gate._tpm == 500000


def test_remaining_zero_parks_the_gate():
    gate = RateGate("gemini|test", "gemini", rpm=10, tpm=None, rpd=None, max_wait=0.05)
    gate.observe({"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "10s"}, 200)
    with pytest.raises(UpstreamRateLimited):
        asyncio.run(gate.acquire(10))


def test_rpm_budget_blocks_before_exceeding():
    gate = RateGate("gemini|test", "gemini", rpm=1, tpm=None, rpd=None, max_wait=0.05)
    asyncio.run(gate.acquire(10))
    with pytest.raises(UpstreamRateLimited) as exc:
        asyncio.run(gate.acquire(10))
    assert exc.value.status_code == 429
    assert "rpm" in str(exc.value)


def test_429_backs_off_and_shrinks_budget():
    gate = RateGate("openrouter|test", "openrouter", rpm=10, tpm=100000, rpd=None, max_wait=0.05)
    gate.note_rejected({"retry-after": "7"})
    assert gate._pause_until > time.monotonic()
    assert gate._rpm < 10
    with pytest.raises(UpstreamRateLimited):
        asyncio.run(gate.acquire(10))
    assert gate.last_429_at is not None


def test_record_usage_feeds_tpm_window():
    gate = RateGate("gemini|test", "gemini", rpm=None, tpm=100, rpd=None, max_wait=0.05)
    gate.record_usage({"prompt_tokens": 40, "completion_tokens": 40, "total_tokens": 80})
    assert gate._tok_total == 80
    with pytest.raises(UpstreamRateLimited):
        asyncio.run(gate.acquire(50))


def test_snapshot_exposes_provider_view():
    gate = RateGate("gemini|abc12345", "gemini", rpm=15, tpm=1000, rpd=200, max_wait=60)
    snap = gate.snapshot()
    assert snap["provider"] == "gemini"
    assert snap["rpm"] == 15
    assert snap["requests_per_day"] == 200
    assert snap["max_wait_seconds"] == 60


def test_gate_for_is_cached_per_credential():
    class Target:
        base_url = "https://openrouter.ai/api/v1"
        api_key = "sk-or-v1-demo"
        model = "qwen/qwen3.8-27b:free"

    try:
        first = ratelimit.gate_for(Target())
        second = ratelimit.gate_for(Target())
        assert first is second
        assert first.provider == "openrouter"
        assert first._rpd == 50
    finally:
        ratelimit.reset_for_tests()
