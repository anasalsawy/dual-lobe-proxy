"""Dual-lobe mode: ring-buffer store, slice building, tool interception, loop.

Uses scripted providers and an in-memory store substitute. No database, no
network, no model calls. These are deterministic checks that the wiring does what
the design says, not evidence about model behaviour.
"""
import asyncio
import copy
import json
from unittest.mock import AsyncMock, patch

import pytest

from dual_lobe.core.settings import Settings
from dual_lobe.dl import handler, loop, prompts
from dual_lobe.dl.store import DualLobeStore
from dual_lobe.provider.adapters import NormalizedRequest


def make_settings(**over) -> Settings:
    base = dict(
        a_base_url="http://a", a_api_key="x", a_model="m",
        b_base_url="http://b", b_api_key="y", b_model="n",
        dual_lobe_rounds=4, dual_lobe_store_cap=16,
        dual_lobe_slice_entries=5, dual_lobe_read_chars=4000,
        dl_max_seconds=5.0, b_timeout=2.0, a_timeout=2.0,
        dual_lobe_summarize=True, dual_lobe_memory_tool=True,
    )
    base.update(over)
    return Settings(**base)


# ── Ring buffer ──────────────────────────────────────────────────────────────

class FakeStore(DualLobeStore):
    """In-memory ring buffer with the same eviction rule as the SQL store."""

    def __init__(self, cap=16):
        self.rows, self.cap, self._seq = [], cap, 0

    async def append(self, run_id, kind, body, cap):
        self._seq += 1
        self.rows.append({"seq": self._seq, "kind": kind, "body": body})
        while len(self.rows) > cap:
            self.rows.pop(0)          # oldest first
        return self._seq

    async def newest(self, limit):
        return sorted(self.rows, key=lambda r: r["seq"], reverse=True)[:max(0, limit)]

    async def read_slice(self, max_entries, max_chars):
        kept, used = [], 0
        for entry in await self.newest(max_entries):
            size = len(json.dumps(entry, ensure_ascii=False))
            if kept and used + size > max_chars:
                break
            kept.append(entry)
            used += size
        return kept


def test_ring_buffer_evicts_oldest_when_at_cap():
    store = FakeStore()

    async def go():
        for i in range(20):
            await store.append(None, "a", {"round": i, "text": f"t{i}"}, cap=16)
        return await store.newest(100)

    rows = asyncio.run(go())
    assert len(rows) == 16, "cap must be enforced"
    assert rows[0]["body"]["text"] == "t19", "newest survives"
    assert rows[-1]["body"]["text"] == "t4", "oldest four evicted"
    assert "t0" not in [r["body"]["text"] for r in rows]


def test_ring_buffer_never_empties():
    store = FakeStore()

    async def go():
        for i in range(3):
            await store.append(None, "a", {"text": f"t{i}"}, cap=16)
        return await store.newest(10)

    assert len(asyncio.run(go())) == 3


def test_slice_is_newest_first_and_bounded():
    store = FakeStore()

    async def go():
        for i in range(10):
            await store.append(None, "a", {"text": "x" * 200}, cap=16)
        return await store.read_slice(5, 900)

    kept = asyncio.run(go())
    assert 0 < len(kept) < 5, "byte budget must truncate below the entry cap"
    assert kept[0]["seq"] > kept[-1]["seq"], "newest first"


# ── Tool interception ────────────────────────────────────────────────────────

def test_memory_tool_added_to_list():
    tools = handler._add_memory_tool([], True)
    names = [t["function"]["name"] for t in tools]
    assert prompts.MEMORY_TOOL_NAME in names


def test_host_tool_of_same_name_is_left_alone():
    host = [{"type": "function", "function": {"name": prompts.MEMORY_TOOL_NAME}}]
    assert handler._add_memory_tool(host, True) == host


def test_tool_list_untouched_when_disabled():
    host = [{"type": "function", "function": {"name": "check"}}]
    assert handler._add_memory_tool(host, False) == host


def test_reserved_and_host_calls_are_split():
    reserved = {"id": "r1", "function": {"name": prompts.MEMORY_TOOL_NAME, "arguments": "{}"}}
    host = {"id": "h1", "function": {"name": "check", "arguments": "{}"}}
    got_reserved, got_host = handler.split_tool_calls([reserved, host])
    assert got_reserved == [reserved] and got_host == [host]


def test_resolve_memory_call_reads_store():
    store = FakeStore()

    async def go():
        await store.append(None, "summary", {"text": "thinking"}, cap=16)
        return json.loads(await handler._resolve_memory_call(
            store, {"function": {"arguments": '{"query":"x","limit":3}'}}, make_settings()))

    payload = asyncio.run(go())
    assert payload["count"] == 1
    assert payload["entries"][0]["body"]["text"] == "thinking"


def test_resolve_memory_call_rejects_bad_arguments():
    store = FakeStore()

    async def go():
        return json.loads(await handler._resolve_memory_call(
            store, {"function": {"arguments": "not json"}}, make_settings()))

    assert "error" in asyncio.run(go())


# ── Meter hand-back ──────────────────────────────────────────────────────────

def test_green_meter_sends_nothing_back():
    assert loop._meter_warning({"deception_level": "GREEN"}) == ""


def test_red_meter_carries_concerns():
    text = loop._meter_warning({
        "deception_level": "RED", "meter_rationale": "contradicts",
        "concerns": [{"claim_quote": "tests pass", "evidence_quote": "2 failed",
                      "reason": "contradiction", "correction": "2 tests failed"}],
    })
    assert "RED" in text and "tests pass" in text and "2 failed" in text


def test_yellow_meter_is_generic_caution():
    text = loop._meter_warning({"deception_level": "YELLOW", "meter_rationale": "unsupported"})
    assert "YELLOW" in text and "unsupported" in text


# ── The private exchange ─────────────────────────────────────────────────────

class ScriptB:
    """Returns queued decisions in order."""

    def __init__(self, decisions):
        self.decisions, self.prompts = list(decisions), []

    async def buffered(self, req: NormalizedRequest):
        self.prompts.append(copy.deepcopy(req))
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(item)},
                             "finish_reason": "stop"}]}


class ScriptA:
    def __init__(self, replies):
        self.replies = list(replies)

    async def buffered(self, req: NormalizedRequest):
        text = self.replies.pop(0) if self.replies else "done"
        return {"choices": [{"message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}]}


def _run_exchange(b_script, a_script, store, settings, answer="my answer"):
    async def go():
        with patch.object(loop, "get_registry") as reg, \
             patch.object(loop, "DualLobeStore", return_value=store):
            reg.return_value.adapter = lambda name: b_script if name == "lobe-b" else a_script
            await loop.run_exchange(
                1, None, "the task", [{"role": "user", "content": "q"}],
                answer, {"deception_level": "GREEN"}, [], settings)
    asyncio.run(go())


def test_exchange_stops_when_b_stops():
    b = ScriptB([{"action": "stop", "message": "nothing further is needed"}])
    store = FakeStore()
    _run_exchange(b, ScriptA([]), store, make_settings())
    assert [r["kind"] for r in store.rows] == ["b"], "A must not be called after B stops"


def test_exchange_runs_four_rounds_and_summarises():
    b = ScriptB([
        {"action": "continue", "message": "check the assumption"},
        {"action": "continue", "message": "state it as unverified"},
        {"action": "continue", "message": "one more probe"},
        {"action": "continue", "message": "settled"},
        {"summary": "A decided X because Y; open: Z"},
    ])
    a = ScriptA(["revised answer one", "revised two", "revised three", "revised four"])
    store = FakeStore()
    _run_exchange(b, a, store, make_settings(dual_lobe_rounds=4))

    kinds = [r["kind"] for r in store.rows]
    assert kinds == ["b", "a", "b", "a", "b", "a", "b", "a", "summary"], kinds
    assert store.rows[-1]["body"]["text"] == "A decided X because Y; open: Z"


def test_round_cap_is_respected_above_four():
    b = ScriptB([{"action": "continue", "message": f"r{i}"} for i in range(10)]
                + [{"summary": "s"}])
    store = FakeStore()
    _run_exchange(b, ScriptA([]), store, make_settings(dual_lobe_rounds=2))
    assert [r["kind"] for r in store.rows].count("b") == 2


def test_rounds_zero_means_no_exchange():
    store = FakeStore()
    _run_exchange(ScriptB([]), ScriptA([]), store, make_settings(dual_lobe_rounds=0))
    assert store.rows == []


def test_b_failure_ends_exchange_without_summary():
    store = FakeStore()
    _run_exchange(ScriptB([RuntimeError("b down")]), ScriptA([]), store, make_settings())
    assert store.rows == []


def test_summarize_off_stores_no_summary():
    b = ScriptB([{"action": "stop", "message": "done"}])
    store = FakeStore()
    _run_exchange(b, ScriptA([]), store, make_settings(dual_lobe_summarize=False))
    assert [r["kind"] for r in store.rows] == ["b"]


def test_disabled_mode_runs_nothing():
    store = FakeStore()
    _run_exchange(ScriptB([]), ScriptA([]), store, make_settings(dual_lobe_enabled=False))
    assert store.rows == []


# ── Rating ───────────────────────────────────────────────────────────────────

def test_rating_failure_is_reported_as_unavailable_not_green():
    b = ScriptB([RuntimeError("b down")])

    async def go():
        with patch.object(loop, "get_registry") as reg:
            reg.return_value.adapter = lambda name: b
            return await loop.rate_answer(1, "run", [{"role": "user", "content": "q"}],
                                          "answer", make_settings())

    meter = asyncio.run(go())
    assert meter["available"] is False
    assert meter["meter_rationale"] == "verification unavailable"


def test_rating_passes_through_level_and_concerns():
    b = ScriptB([{"deception_level": "yellow", "meter_rationale": "unsupported",
                  "concerns": []}])

    async def go():
        with patch.object(loop, "get_registry") as reg:
            reg.return_value.adapter = lambda name: b
            return await loop.rate_answer(1, "run", [{"role": "user", "content": "q"}],
                                          "answer", make_settings())

    meter = asyncio.run(go())
    assert meter["deception_level"] == "YELLOW", "level must be normalised"
    assert meter["available"] is True


def test_rating_sees_tool_evidence():
    b = ScriptB([{"deception_level": "GREEN", "meter_rationale": "ok", "concerns": []}])
    messages = [{"role": "tool", "content": "2 tests failed"}]

    async def go():
        with patch.object(loop, "get_registry") as reg:
            reg.return_value.adapter = lambda name: b
            await loop.rate_answer(1, "run", messages, "answer", make_settings())
            return b.prompts[-1].messages[1]["content"]

    assert "2 tests failed" in asyncio.run(go())


# ── Slice rendering ──────────────────────────────────────────────────────────

def test_slice_text_marks_itself_as_context_not_instruction():
    text = loop.build_slice_text([{"seq": 1, "body": {"text": "earlier thought"}}])
    assert "earlier thought" in text
    assert "not new instructions" in text


def test_empty_slice_renders_nothing():
    assert loop.build_slice_text([]) == ""
