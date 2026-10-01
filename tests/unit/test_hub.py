import json

import httpx
import pytest

from dual_lobe.provider import adapters, hub, ratelimit
from dual_lobe.provider.adapters import NormalizedRequest, ProviderTarget

SLOTS = [
    {"label": "s2", "model": "m2", "base_url": "https://two.example/v1", "api_key_env": "HUB_K2"},
    {"label": "s3", "model": "m3", "base_url": "https://three.example/v1", "api_key_env": "HUB_K3"},
    {"label": "s4", "model": "m4", "base_url": "https://four.example/v1", "api_key_env": "HUB_K4",
     "roles": ["a"]},
]


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_HUB_SLOTS", json.dumps(SLOTS))
    for k in ("HUB_K2", "HUB_K3", "HUB_K4"):
        monkeypatch.setenv(k, k.lower())
    hub.reset_for_tests()
    ratelimit.reset_for_tests()
    calls: list[str] = []
    failing: dict[str, int] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        calls.append(host)
        body = json.loads(request.content)
        if host in failing:
            return httpx.Response(failing[host], json={"error": "nope"})
        if body.get("stream"):
            sse = (f'data: {{"choices":[{{"delta":{{"content":"{host}"}}}}]}}\n\n'
                   "data: [DONE]\n\n")
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": host}}], "model": body["model"]})

    monkeypatch.setattr(adapters, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    yield calls, failing
    hub.reset_for_tests()
    ratelimit.reset_for_tests()


def _target(host: str, model: str = "m1") -> ProviderTarget:
    return ProviderTarget(alias="lobe-a", base_url=f"https://{host}/v1", api_key="k1", model=model)


def _req():
    return NormalizedRequest(messages=[{"role": "user", "content": "hi"}])


async def test_global_rotation_one_slot_per_call(served):
    calls, _ = served
    a = hub.HubAdapter(_target("one.example"), "a", hub.configured_slots())
    for _ in range(8):
        await a.buffered(_req())
    assert calls == ["one.example", "two.example", "three.example", "four.example"] * 2


async def test_rotation_is_shared_between_lobes(served):
    calls, _ = served
    a = hub.HubAdapter(_target("one.example"), "a", hub.configured_slots())
    b = hub.HubAdapter(_target("one.example"), "b", hub.configured_slots())
    await a.buffered(_req())   # slot 1
    await b.buffered(_req())   # slot 2
    await a.buffered(_req())   # slot 3
    await b.buffered(_req())   # slot 4 is A-only, so B takes the next it has: slot 1
    assert calls == ["one.example", "two.example", "three.example", "one.example"]


async def test_error_switches_to_next_slot_and_skips_it_after(served):
    calls, failing = served
    failing["two.example"] = 503
    a = hub.HubAdapter(_target("one.example"), "a", hub.configured_slots())
    await a.buffered(_req())                 # slot 1
    data = await a.buffered(_req())          # slot 2 fails -> slot 3 serves the same call
    assert data["choices"][0]["message"]["content"] == "three.example"
    calls.clear()
    for _ in range(4):
        await a.buffered(_req())
    assert "two.example" not in calls        # cooling slot is not hit again


async def test_429_parks_slot_and_call_succeeds(served):
    calls, failing = served
    failing["one.example"] = 429
    a = hub.HubAdapter(_target("one.example"), "a", hub.configured_slots())
    data = await a.buffered(_req())
    assert data["choices"][0]["message"]["content"] == "two.example"
    calls.clear()
    for _ in range(3):
        await a.buffered(_req())
    assert "one.example" not in calls


async def test_all_slots_fail_raises_last_error(served):
    _, failing = served
    for host in ("one.example", "two.example", "three.example", "four.example"):
        failing[host] = 500
    a = hub.HubAdapter(_target("one.example"), "a", hub.configured_slots())
    with pytest.raises(httpx.HTTPStatusError):
        await a.buffered(_req())


async def test_stream_fails_over_before_first_chunk(served):
    _, failing = served
    failing["one.example"] = 500
    a = hub.HubAdapter(_target("one.example"), "a", hub.configured_slots())
    chunks = [c async for c in a.stream(NormalizedRequest(messages=[{"role": "user", "content": "x"}], stream=True))]
    assert chunks[0]["choices"][0]["delta"]["content"] == "two.example"


async def test_slots_with_same_host_but_different_keys_are_separate(served, monkeypatch):
    calls, _ = served
    monkeypatch.setenv("DUAL_LOBE_HUB_SLOTS", json.dumps([
        {"label": "or1", "model": "m", "base_url": "https://or.example/v1", "api_key_env": "HUB_K2"},
        {"label": "or2", "model": "m", "base_url": "https://or.example/v1", "api_key_env": "HUB_K3"},
    ]))
    hub.reset_for_tests()
    a = hub.HubAdapter(_target("one.example"), "a", hub.configured_slots())
    assert len(a._own) == 3
    assert [s["label"] for s in hub.snapshot()] == ["a-primary:m1", "or1", "or2"]


def test_registry_wraps_env_aliases_only_when_slots_configured(monkeypatch):
    from dual_lobe.provider.registry import Registry

    monkeypatch.delenv("DUAL_LOBE_HUB_SLOTS", raising=False)
    r = Registry()
    r.register(_target("one.example"))
    assert isinstance(r.adapter("lobe-a"), adapters.ChatCompletionsAdapter)
    monkeypatch.setenv("DUAL_LOBE_HUB_SLOTS", json.dumps(SLOTS[:1]))
    monkeypatch.setenv("HUB_K2", "x")
    hub.reset_for_tests()
    r.register(_target("one.example"))
    assert isinstance(r.adapter("lobe-a"), hub.HubAdapter)
    hub.reset_for_tests()


def test_registry_exposes_only_the_two_supported_public_models(monkeypatch):
    from dual_lobe.core.settings import Settings
    from dual_lobe.provider.registry import Registry, env_targets

    monkeypatch.delenv("DUAL_LOBE_HUB_SLOTS", raising=False)
    monkeypatch.setattr("dual_lobe.core.settings.get_settings", lambda: Settings(_env_file=None))
    targets = env_targets()
    registry = Registry()
    registry.refresh(targets)
    assert {item["id"] for item in registry.models()} == {
        "sawii/dl-bidirectional", "sawii/dl-secure"
    }
    assert "sawii/dl-bidirectional" not in targets
    assert registry.target().alias == "sawii/dl-bidirectional"


async def test_daily_quota_429_sidelines_slot_until_reset(served, monkeypatch):
    calls, _ = served
    import time as _time

    reset_ms = int((_time.time() + 3600) * 1000)
    orig = adapters._client._transport.handler

    def handle(request):
        if request.url.host == "two.example":
            calls.append("two.example")
            return httpx.Response(429, json={"error": {"message": "Rate limit exceeded: free-models-per-day",
                                                       "metadata": {"headers": {"X-RateLimit-Reset": str(reset_ms)}}}})
        return orig(request)

    adapters._client._transport.handler = handle
    a = hub.HubAdapter(_target("one.example"), "a", hub.configured_slots())
    for _ in range(8):
        await a.buffered(_req())
    assert calls.count("two.example") == 1
    row = next(r for r in hub.snapshot() if r["label"] == "s2")
    assert 3500 < row["cooling_seconds"] <= 3600
    assert row["last_error"] == "429 daily quota used up"


def test_b_sees_full_long_answer():
    from dual_lobe.gated.handler import _for_b

    text = "step " * 2000  # 10k chars, past the old 4000 cut
    assert _for_b(text) == text


async def test_fastest_strategy_prefers_quickest_healthy_slot(served, monkeypatch):
    calls, _ = served
    monkeypatch.setenv("DUAL_LOBE_HUB_STRATEGY", "fastest")
    a = hub.HubAdapter(_target("one.example"), "a", hub.configured_slots())
    for ident, speed in zip(a._own, (5.0, 0.5, 3.0, 4.0)):
        hub._HEALTH[ident]["speed"] = speed
    for _ in range(3):
        await a.buffered(_req())
    assert calls == ["two.example"] * 3
