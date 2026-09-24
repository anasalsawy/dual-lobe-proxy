from dual_lobe.gated.handler import (
    _build_assist_injections,
    _extract_handoff,
    _render_handoff,
)
from dual_lobe.gated.prompts import (
    DOWNSTREAM_CONTRACT,
    DOWNSTREAM_CONTRACT_HANDOFF,
    HANDOFF_SYSTEM_ADDENDUM,
)


def test_extract_handoff_normalizes_fields():
    raw = {
        "unverified": ["claims tests pass", "", 5],
        "tool_review": {"verdict": "FIX", "issue": " wrong id "},
        "next_step": " run migrate ",
        "missing": ["a token"],
    }
    handoff = _extract_handoff(raw)
    assert handoff["unverified"] == ["claims tests pass", "5"]
    assert handoff["tool_review"] == {"verdict": "fix", "issue": "wrong id"}
    assert handoff["next_step"] == "run migrate"
    assert handoff["missing"] == ["a token"]


def test_extract_handoff_defaults_on_garbage():
    handoff = _extract_handoff({"tool_review": {"verdict": "explode"}, "unverified": "single"})
    assert handoff["tool_review"]["verdict"] == "none"
    assert handoff["unverified"] == ["single"]
    empty = _extract_handoff(None)
    assert empty["tool_review"]["verdict"] == "none"
    assert empty["next_step"] == ""


def test_render_handoff_combines_sections():
    meter = {
        "assist": "prereq: set the env var first",
        "unverified": ["tests pass"],
        "tool_review": {"verdict": "block", "issue": "this drops the table"},
        "next_step": "ask for confirmation before deleting",
        "missing": ["migration head"],
    }
    text = _render_handoff(meter)
    assert "prereq: set the env var first" in text
    assert "Unverified claims" in text and "tests pass" in text
    assert "Tool call review: BLOCK" in text
    assert "this drops the table" in text
    assert "Next step for this task: ask for confirmation before deleting" in text
    assert "Missing before the answer is solid" in text


def test_render_handoff_skips_safe_and_empty():
    assert _render_handoff({}) == ""
    assert _render_handoff({"tool_review": {"verdict": "safe", "issue": "args ok"}}) == ""
    assert _render_handoff({"tool_review": {"verdict": "none", "issue": ""}}) == ""


def test_render_handoff_is_length_capped():
    text = _render_handoff({"assist": "x" * 5000, "unverified": ["y" * 400]})
    assert len(text) <= 2000


def test_assist_injection_pair_uses_handoff():
    messages = _build_assist_injections({"next_step": "call the API with page=2"})
    assert len(messages) == 2
    assert messages[0]["role"] == "assistant"
    assert messages[0]["tool_calls"][0]["function"]["name"] == "observer_assist"
    assert messages[1]["role"] == "tool"
    assert "Next step for this task: call the API with page=2" in messages[1]["content"]
    assert _build_assist_injections({}) == []


def test_handoff_contract_is_additive():
    assert "tool_review" not in DOWNSTREAM_CONTRACT
    assert "unverified" not in DOWNSTREAM_CONTRACT
    for field in ("unverified", "tool_review", "next_step", "missing"):
        assert field in DOWNSTREAM_CONTRACT_HANDOFF
        assert field in HANDOFF_SYSTEM_ADDENDUM
    assert "never change deception_level" in DOWNSTREAM_CONTRACT_HANDOFF
