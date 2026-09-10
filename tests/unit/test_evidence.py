"""Offline contract tests for gateway-side evidence sensing (no network, no model)."""
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from dual_lobe.b import artifacts, context_shadow, fetch
from dual_lobe.b.channels import prepare_context, reviewed_state
from dual_lobe.b.protocol import EvidenceRequest, Review
from dual_lobe.core.settings import Settings


def review(**kwargs):
    base = {"goal": "Fix tests", "questions": [], "next_step": "", "context_notes": [],
            "concerns": []}
    base.update(kwargs)
    return base


def test_evidence_request_tool_whitelist_and_shape():
    assert EvidenceRequest(tool="fetch_web", arguments={"url": "https://x/y"}).reason() == "https://x/y"
    assert EvidenceRequest(tool="read_artifact", arguments={"path": "a.py"}).reason() == "a.py"
    with pytest.raises(ValidationError):
        EvidenceRequest(tool="delete_server")
    with pytest.raises(ValidationError):
        EvidenceRequest(tool="fetch_file", arguments={"url": "file:///etc/passwd"})
    assert EvidenceRequest(tool="fetch_web", arguments={"url": "https://x", "command": "ls"}).reason() == "https://x"


def test_review_carries_meter_rationale_and_evidence_request():
    parsed = Review.model_validate(review(
        deception_level="YELLOW", meter_rationale="Outcome contradicts the tool result.",
        evidence_request={"tool": "fetch_web", "arguments": {"url": "https://docs.example.com/flag"}}))
    assert parsed.meter_rationale == "Outcome contradicts the tool result."
    assert parsed.evidence_request.tool == "fetch_web"
    with pytest.raises(ValidationError):
        Review.model_validate(review(meter_rationale="x" * 201))
    with pytest.raises(ValidationError):
        Review.model_validate(review(evidence_request={"tool": "fetch_web", "arguments": {"url": 5}}))


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://host/a", "ftp://host/x", "javascript:alert(1)"])
def test_fetch_rejects_non_http_schemes_without_network(url):
    result = asyncio_run(fetch.fetch_url(url))
    assert result["ok"] is False
    assert "unsupported scheme" in result["error"]


def test_fetch_rejects_empty_or_missing_url_argument():
    result = asyncio_run(fetch.fetch_url(""))
    assert result["ok"] is False and result["error"]


def test_resolve_artifact_containment_and_blocklist(tmp_path: Path):
    root = str(tmp_path)
    assert artifacts.resolve_artifact("guide.md", root) is not None
    assert artifacts.resolve_artifact("../escape.md", root) is None
    assert artifacts.resolve_artifact(".env", root) is None
    assert artifacts.resolve_artifact("keys/id_rsa", root) is None
    secret = tmp_path / "service.pem"
    secret.write_text("x")
    assert artifacts.resolve_artifact("service.pem", root) is None
    (tmp_path / "outer").mkdir()
    (tmp_path / "outer" / "link").symlink_to("/etc/hostname")
    assert artifacts.resolve_artifact("outer/link", root) is None
    assert artifacts.resolve_artifact("guide.md", "") is None


async def _read_with(monkeypatch, root, ref):
    monkeypatch.setattr(artifacts, "get_settings", lambda: Settings(_env_file=None, artifact_root=root))
    return await artifacts.read_artifact(ref)

def _binary_read(monkeypatch, root, ref):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_read_with(monkeypatch, root, ref))
    finally:
        loop.close()


def test_read_artifact_requires_configured_root(monkeypatch):
    read = _binary_read(monkeypatch, "", "a.py")
    assert read["ok"] is False and "not readable" in read["error"]


def test_read_artifact_rejects_directory(monkeypatch, tmp_path: Path):
    read = _binary_read(monkeypatch, str(tmp_path), ".")
    assert read["ok"] is False and "not a file" in read["error"]


def test_read_artifact_excerpts_and_sensed_text(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(artifacts, "get_settings", lambda: Settings(_env_file=None, artifact_root=str(tmp_path)))
    (tmp_path / "notes.md").write_text("line one.\n" * 5000)
    result = asyncio_run(artifacts.read_artifact("notes.md"))
    assert result["ok"] is True and result["text"]
    snippet = artifacts.sensed_text([result])
    assert snippet.startswith("SENSED_TOOL_RESULTS:")
    assert "notes.md" in snippet
    assert artifacts.sensed_text([]) == ""


def test_review_with_sensing_reruns_b_after_gateway_fetch(monkeypatch):
    monkeypatch.setattr(context_shadow, "get_settings", lambda: Settings(
        _env_file=None, evidence_sensing_enabled=True, b_tool_max_rounds=2, b_evidence_ops_per_run=5))
    first = Review.model_validate(review(
        evidence_request={"tool": "fetch_web", "arguments": {"url": "https://docs.example.com/flag"}}))
    second = Review.model_validate(review())
    calls = []
    async def fake_obtain(run, prompt):
        calls.append(prompt)
        return first if len(calls) == 1 else second
    monkeypatch.setattr(context_shadow, "_obtain_review", fake_obtain)
    monkeypatch.setattr(context_shadow, "_execute_evidence", AsyncMock(
        return_value={"ok": True, "tool": "fetch_web", "label": "fetch_web",
                      "url": "https://docs.example.com/flag", "status": 200,
                      "text": "The documented flag is --dry-run."}))
    review_out, results, used = asyncio_run(context_shadow._review_with_sensing(
        "base", lambda sensed: "base\n\n" + sensed, {}))
    assert review_out is second and used == 1 and len(results) == 1
    assert len(calls) == 2
    assert "SENSED_TOOL_RESULTS" in calls[1]


def test_review_with_sensing_respects_ops_budget(monkeypatch):
    monkeypatch.setattr(context_shadow, "get_settings", lambda: Settings(
        _env_file=None, evidence_sensing_enabled=True, b_tool_max_rounds=3, b_evidence_ops_per_run=1))
    scripted = [Review.model_validate(review(
        evidence_request={"tool": "read_artifact", "arguments": {"path": "a.py"}})),
        Review.model_validate(review(
            evidence_request={"tool": "read_artifact", "arguments": {"path": "b.py"}})),
        Review.model_validate(review())]
    calls = []
    async def fake_obtain(run, prompt):
        calls.append(prompt)
        return scripted[len(calls) - 1]
    monkeypatch.setattr(context_shadow, "_obtain_review", fake_obtain)
    executed = []
    async def fake_exec(request, settings):
        executed.append(request.tool)
        return {"ok": False, "tool": request.tool, "label": request.tool, "error": "nope"}
    monkeypatch.setattr(context_shadow, "_execute_evidence", fake_exec)
    previous = {"evidence_ops_used": 0}
    review_out, results, used = asyncio_run(context_shadow._review_with_sensing(
        "base", lambda sensed: "base", previous))
    assert used == 1 and len(executed) == 1 and len(results) == 1
    assert review_out.deception_level == "GREEN"


def test_review_with_sensing_disabled_is_cheap(monkeypatch):
    monkeypatch.setattr(context_shadow, "get_settings", lambda: Settings(
        _env_file=None, evidence_sensing_enabled=False, b_tool_max_rounds=2, b_evidence_ops_per_run=5))
    monkeypatch.setattr(context_shadow, "_obtain_review", AsyncMock(return_value=Review.model_validate(review())))
    monkeypatch.setattr(context_shadow, "_execute_evidence", AsyncMock())
    review_out, results, used = asyncio_run(context_shadow._review_with_sensing(
        "base", lambda sensed: "base", {}))
    assert used == 0 and results == [] and review_out.deception_level == "GREEN"
    context_shadow._execute_evidence.assert_not_awaited()


def test_evidence_delivery_stale_when_snapshot_old(monkeypatch):
    state = reviewed_state({}, Review.model_validate(review()), {
        "run_id": "run", "observed_at": time.time() - 100000}, evidence_used=1,
        evidence_snapshot=[{"ok": True, "tool": "fetch_web", "label": "fetch_web",
                            "url": "https://x/y", "status": 200, "text": "z"}])
    context = prepare_context(state, "", 1, Settings(_env_file=None))
    assert context.evidence_status == "none"  # snapshot freshness is tied to state TTL


def asyncio_run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()