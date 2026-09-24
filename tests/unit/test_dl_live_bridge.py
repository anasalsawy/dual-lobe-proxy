import asyncio

import pytest

from dual_lobe.dl import handler


@pytest.mark.asyncio
async def test_dual_lobe_event_stream_preserves_order_and_final_result(monkeypatch):
    async def fake_response(payload, run_id, tenant_id, public_model, task="", event_sink=None):
        assert payload["dual_lobe_inline_exchange"] is False
        await event_sink({"type": "exchange_start", "run_id": run_id})
        await event_sink({"type": "exchange_message", "actor": "B", "content": "challenge", "round": 0})
        await event_sink({"type": "exchange_message", "actor": "A", "content": "revision", "round": 0})
        return (
            {"choices": [{"message": {"role": "assistant", "content": "final"}, "finish_reason": "stop"}]},
            {"X-Dual-Lobe-Mode": "pre-final"},
            None,
        )

    monkeypatch.setattr(handler, "dual_lobe_response", fake_response)
    items = []
    async for item in handler.dual_lobe_event_stream(
        {"messages": []}, "run-1", 7, "sawii/dual-lobe"
    ):
        items.append(item)

    assert [item["kind"] for item in items] == ["event", "event", "event", "result"]
    assert [item["event"].get("actor") for item in items[1:3]] == ["B", "A"]
    assert items[-1]["data"]["choices"][0]["message"]["content"] == "final"


@pytest.mark.asyncio
async def test_dual_lobe_event_stream_cancels_inference_on_client_close(monkeypatch):
    cancelled = asyncio.Event()

    async def fake_response(payload, run_id, tenant_id, public_model, task="", event_sink=None):
        try:
            await event_sink({"type": "exchange_start", "run_id": run_id})
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(handler, "dual_lobe_response", fake_response)
    stream = handler.dual_lobe_event_stream(
        {"messages": []}, "run-cancel", 7, "sawii/dual-lobe"
    )

    first = await stream.__anext__()
    assert first["kind"] == "event"
    await stream.aclose()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
