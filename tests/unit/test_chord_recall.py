"""DB-backed emotional chord round-trip: storage, resonance recall (emotion ->
text), chord traveling with matched quotes (text -> emotion), advisory fallback.
"""
from __future__ import annotations

import asyncio
import json

from dual_lobe.state import memory as mem


def _extract_callbacks(loaded: mem.LoadedMemory) -> list[dict]:
    assert loaded.callbacks is not None
    payload = json.loads(loaded.callbacks.split("\n", 1)[1])
    return payload["callbacks"]


def test_chord_state_and_resonance_recall_surface_the_mixed_memory(tenant):
    """A sad-leaning present recalls a bittersweet past with no word overlap."""
    tid, _ = tenant

    async def _run():
        store = mem.MemoryStore(tid, "chord-resonance")
        await store.annotate(None, [{
            "kind": "affect", "anchor": "her leaving",
            "quote": "she got the job but I will miss her every day",
            "feeling": "warm", "chord": {"joy": 0.6, "sadness": 0.7},
            "salience": 3}])
        await store.annotate(None, [{
            "kind": "affect", "anchor": "monday",
            "quote": "flat tire again, what a great start",
            "feeling": "tired", "chord": {"sadness": 1.0, "anger": 0.4},
            "salience": 2}])
        return await store.load(
            [{"role": "user", "content": "ugh everything keeps falling apart"}], 8000)

    loaded = asyncio.run(_run())
    assert loaded.chord_state and loaded.chord_state["sadness"] == 1.0
    items = _extract_callbacks(loaded)
    surfaced = [i for i in items if i.get("surfaced_by") == "emotional resonance"]
    assert len(surfaced) == 1
    assert surfaced[0]["quote"] == "she got the job but I will miss her every day"
    assert surfaced[0]["chord"] == {"joy": 0.6, "sadness": 0.7}
    # No unanimous tag run (warm/tired), so the chord renders as the advisory.
    assert loaded.mood is not None and "emotional chord" in loaded.mood


def test_lexical_match_carries_the_chord_of_the_memory(tenant):
    """Text -> emotion: recognizing the situation resurfaces how it felt."""
    tid, _ = tenant

    async def _run():
        store = mem.MemoryStore(tid, "chord-text2emo")
        await store.annotate(None, [{
            "kind": "affect", "anchor": "marathon",
            "quote": "crossed the finish line crying honestly",
            "feeling": "proud", "chord": {"pride": 1.0, "joy": 0.8, "sadness": 0.3},
            "salience": 3}])
        return await store.load(
            [{"role": "user", "content": "anyone else finish their marathon crying?"}], 8000)

    loaded = asyncio.run(_run())
    items = _extract_callbacks(loaded)
    assert any(i["quote"] == "crossed the finish line crying honestly"
               and i["chord"] == {"pride": 1.0, "joy": 0.8, "sadness": 0.3}
               for i in items)


def test_paraphrase_lane_surfaces_what_the_word_query_misses(tenant, monkeypatch):
    """The lane exists for what the boolean word query provably cannot do:
    recall with zero shared exact token ("weding" never matches "wedding" -
    websearch_to_tsquery('simple') does not stem).

    Measured behavior of the default hash embedder: genuine morphology
    (weding/wedding) lands ~0.27 while unrelated text tops out ~0.20 - a
    thin band. The default threshold (0.45) therefore keeps the lane near
    silent until a semantic model is swapped in through the seam; this
    test lowers the configured threshold to exercise the real code path
    (pool, ordering, dedup, chord-travels) with the real embedder.
    """
    tid, _ = tenant
    from dual_lobe.core.settings import get_settings
    monkeypatch.setattr(get_settings(), "episodic_paraphrase_threshold", 0.2)

    async def _run():
        store = mem.MemoryStore(tid, "chord-paraphrase")
        await store.annotate(None, [{
            "kind": "affect", "anchor": "the wedding",
            "quote": "we attended the wedding last week",
            "feeling": "warm", "chord": {"joy": 0.7, "tenderness": 0.5},
            "salience": 1}])
        return await store.load(
            [{"role": "user", "content": "all the weding jitters"}], 8000)

    loaded = asyncio.run(_run())
    items = _extract_callbacks(loaded)
    surfaced = [i for i in items if i.get("surfaced_by") == "near paraphrase"]
    assert len(surfaced) == 1
    assert surfaced[0]["quote"] == "we attended the wedding last week"
    assert surfaced[0]["chord"] == {"joy": 0.7, "tenderness": 0.5}  # chord travels


def test_unrelated_turn_does_not_trigger_the_paraphrase_lane(tenant):
    tid, _ = tenant

    async def _run():
        store = mem.MemoryStore(tid, "chord-paraphrase-neg")
        await store.annotate(None, [{
            "kind": "affect", "anchor": "errand",
            "quote": "hospital parking is impossible today",
            "feeling": "tired", "chord": {"anger": 0.6, "sadness": 0.4},
            "salience": 3}])
        return await store.load(
            [{"role": "user", "content": "can you summarize the quarterly numbers"}], 8000)

    loaded = asyncio.run(_run())
    items = _extract_callbacks(loaded)
    assert not [i for i in items if i.get("surfaced_by") == "near paraphrase"]


def test_notes_without_chords_keep_the_tag_fallback(tenant):
    """Pre-backfill rows (chord {}) still drive the mood path, never crash it."""
    tid, _ = tenant

    async def _run():
        store = mem.MemoryStore(tid, "chord-legacy")
        await store.annotate(None, [{
            "kind": "affect", "anchor": "old note",
            "quote": "grandma loved this old song",
            "feeling": "concerned", "salience": 3}])
        await store.annotate(None, [{
            "kind": "affect", "anchor": "newer",
            "quote": "hospital parking is impossible today",
            "feeling": "concerned", "salience": 2}])
        return await store.load(
            [{"role": "user", "content": "long drive with nothing on the radio"}], 8000)

    loaded = asyncio.run(_run())
    assert loaded.chord_state is None
    items = _extract_callbacks(loaded)
    surfaced = [i for i in items if i.get("surfaced_by") == "emotional resonance"]
    assert len(surfaced) == 1  # tag-equality path still fires