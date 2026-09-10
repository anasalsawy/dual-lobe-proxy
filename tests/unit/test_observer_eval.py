"""The evaluation driver exercises the production B prompt/validation path."""
import argparse
import json
from unittest.mock import AsyncMock

from dual_lobe.b import context_shadow, prompts
from tests.helpers import FakeAdapter, FakeRegistry
from tests.unit.test_observer_repairs import BASE, NOTE
from tools import observer_eval


async def test_labeled_evaluation_runs_actual_review_path_with_scripted_provider(monkeypatch, capsys):
    b = FakeAdapter(json.dumps({**BASE, "knowledge_notes": [NOTE]}))
    monkeypatch.setattr(context_shadow, "get_registry", lambda: FakeRegistry(b=b))
    admission = AsyncMock(return_value=(True, 0))
    monkeypatch.setattr(context_shadow, "_local_sliding", admission)
    await observer_eval.run(argparse.Namespace(cases="tools/evals/cases.json", live=True))
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[0]["live"] and len(records) == 9 and admission.await_count == 8
    assert all(r["ok"] and r["review"]["knowledge_notes"] for r in records[1:])
    # Fixed negative controls must not be hidden by an always-empty concerns result.
    assert not next(r for r in records if r.get("case") == "contradicted_execution")["signals_match"]
    assert prompts.ENRICHMENT_POLICY in b.last_request.messages[0]["content"]
