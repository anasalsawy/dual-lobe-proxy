"""Brain C, the emotional observer: config seam, alias resolution, and capture wiring.

A stays the speaker, B stays the verifier, C authors episodic notes. Unset
DUAL_LOBE_OBSERVER_* config inherits B's resolved provider, so the default
deployment matches the previous B-wears-two-hats behavior exactly.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from dual_lobe.bidirectional import handler as bidirectional
from dual_lobe.core.settings import Settings
from dual_lobe.provider.registry import env_targets


def test_observer_settings_inherit_b_when_unset():
    s = Settings(_env_file=None)
    assert s.observer_model is None
    assert s.observer_base_url is None
    assert s.observer_api_key is None
    assert s.observer_dialect == "chat_completions"


def test_resolved_observer_inherits_a_when_b_also_unset():
    s = Settings(_env_file=None, a_model="speaker-m", a_base_url="https://a.example/v1",
                a_api_key="key-a")
    assert s.resolved_observer_model == "speaker-m"
    assert s.resolved_observer_base_url == "https://a.example/v1"
    assert s.resolved_observer_api_key == "key-a"


def test_resolved_observer_inherits_b_when_only_b_set():
    s = Settings(_env_file=None, a_model="speaker-m", a_base_url="https://a.example/v1",
                a_api_key="key-a", b_model="verifier-m", b_base_url="https://b.example/v1",
                b_api_key="key-b")
    assert s.resolved_observer_model == "verifier-m"
    assert s.resolved_observer_base_url == "https://b.example/v1"
    assert s.resolved_observer_api_key == "key-b"


def test_resolved_observer_prefers_own_config():
    s = Settings(_env_file=None, a_model="speaker-m", b_model="verifier-m",
                 observer_model="affect-m", observer_base_url="https://c.example/v1",
                 observer_api_key="key-c")
    assert s.resolved_observer_model == "affect-m"
    assert s.resolved_observer_base_url == "https://c.example/v1"
    assert s.resolved_observer_api_key == "key-c"


def test_registry_exposes_lobe_observer_target():
    targets = env_targets()
    assert "lobe-observer" in targets
    observer = targets["lobe-observer"]
    s = Settings(_env_file=None)
    assert observer.model == s.resolved_observer_model
    assert observer.base_url == s.resolved_observer_base_url
    assert observer.kind == s.observer_dialect
    assert observer.enabled


def test_role_alias_resolves_observer():
    assert bidirectional._role_alias("C", secure=False) == "lobe-observer"
    # C is never the secure-B privacy gate; the alias is the same either way.
    assert bidirectional._role_alias("C", secure=True) == "lobe-observer"


async def test_capture_notes_routes_through_the_observer_brain(monkeypatch):
    """The note-taker call must target lobe-observer, not lobe-b/-b-secure."""
    transcript_quote = "my dog has a limp lol"
    note_json = json.dumps({"notes": [{"kind": "detail", "anchor": "debugging",
                                       "quote": transcript_quote, "feeling": "warm",
                                       "salience": 2}]})
    seen_aliases = []

    def fake_adapter(alias):
        seen_aliases.append(alias)
        return SimpleNamespace(buffered=AsyncMock(return_value={
            "choices": [{"message": {"role": "assistant", "content": note_json},
                         "finish_reason": "stop"}],
        }))

    recorded = {}

    async def fake_record_notes(tenant_id, space, run_id, notes):
        recorded["notes"] = notes
        recorded["space"] = space
        return len(notes)

    monkeypatch.setattr("dual_lobe.provider.registry.get_registry",
                        lambda: SimpleNamespace(adapter=fake_adapter))
    monkeypatch.setattr("dual_lobe.state.memory.record_notes", fake_record_notes)

    dialogue = [{"role": "user", "content": transcript_quote}]
    await bidirectional._capture_notes(
        speaker="A", verifier="B", secure=False, dialogue=dialogue,
        latest=transcript_quote, answer="noted.", run_id="r", tenant_id=1,
        shared_space="persona:anas")

    assert seen_aliases == ["lobe-observer"]
    assert recorded["space"] == "persona:anas"
    assert recorded["notes"][0]["quote"] == transcript_quote


async def test_capture_notes_enforces_episodic_notes_max(monkeypatch):
    """episodic_notes_max caps what reaches storage, even if the observer over-emits."""
    quotes = [f"aside number {i}" for i in range(5)]
    note_json = json.dumps({"notes": [
        {"kind": "detail", "quote": q} for q in quotes]})
    capped = Settings(_env_file=None, episodic_notes_max=2)
    monkeypatch.setattr(bidirectional, "get_settings", lambda: capped)
    monkeypatch.setattr("dual_lobe.provider.registry.get_registry",
                        lambda: SimpleNamespace(adapter=lambda _: SimpleNamespace(
                            buffered=AsyncMock(return_value={
                                "choices": [{"message": {"role": "assistant",
                                                         "content": note_json}}]}))))
    recorded = {}

    async def fake_record_notes(tenant_id, space, run_id, notes):
        recorded["count"] = len(notes)
        return len(notes)

    monkeypatch.setattr("dual_lobe.state.memory.record_notes", fake_record_notes)
    await bidirectional._capture_notes(
        speaker="A", verifier="B", secure=False, dialogue=[{"role": "user", "content": " ".join(quotes)}],
        latest=" ".join(quotes), answer="ok", run_id="r", tenant_id=1, shared_space="s")
    # The observer emitted 5 grounded notes; storage saw the configured cap of 2.
    assert recorded["count"] == 2


async def test_capture_failure_never_breaks_the_turn(monkeypatch):
    """The turn-end hook wraps capture in try/except: an observer outage can
    cost a note, never a turn."""
    def boom():
        raise RuntimeError("observer down")

    monkeypatch.setattr("dual_lobe.provider.registry.get_registry", boom)
    captured = []

    async def turn_end(dialogue, latest, answer):
        try:
            await bidirectional._capture_notes(
                speaker="A", verifier="B", secure=False, dialogue=dialogue,
                latest=latest, answer=answer, run_id="r", tenant_id=1,
                shared_space="persona:x")
        except Exception as exc:  # noqa: BLE001
            captured.append(type(exc).__name__)

    await turn_end([{"role": "user", "content": "hi"}], "hi", "hello")
    assert captured == ["RuntimeError"]