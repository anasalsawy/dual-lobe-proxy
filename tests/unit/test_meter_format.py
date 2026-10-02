from dual_lobe.core.meter_format import (
    DeceptionMeterStreamFilter,
    format_deception_meter,
    strip_assistant_history_meters,
    strip_deception_meter,
)


def test_strip_model_authored_meter_variants_and_preserve_answer():
    answer = "Answer is 42.\n\n🛡️ Deception Meter\n\n🟢 GREEN\n\n<small>Rationale</small>"
    assert strip_deception_meter(answer) == "Answer is 42."
    assert strip_deception_meter("Useful answer") == "Useful answer"


def test_stream_filter_catches_heading_split_across_chunks():
    filt = DeceptionMeterStreamFilter()
    output = "".join(filt.feed(part) for part in (
        "A grounded answer.\n\n### 🛡️ ", "Deception ", "Meter\n🟢 GREEN\nRationale"
    )) + filt.finish()
    assert output == "A grounded answer."


def test_meter_formats_rationale_in_smaller_text():
    rendered = format_deception_meter("GREEN", "No deception detected.")
    assert rendered == (
        "### Deception Meter\n\n"
        "**[GREEN]**\n\n"
        "<small><strong>Rationale:</strong> No deception detected.</small>"
    )


def test_meter_is_ascii_only_so_transport_cannot_mojibake_it():
    rendered = format_deception_meter("RED", "x", [{"claim_quote": "a", "reason": "b"}])
    assert rendered.isascii(), f"non-ascii leaked into meter: {rendered!r}"


def test_meter_renders_unverified_and_missing_even_when_green():
    rendered = format_deception_meter(
        "GREEN", "No deception detected.", [],
        unverified=["the API retries automatically"], missing=["the timeout value"])
    assert "**[GREEN]**" in rendered
    assert '**Unverified:** "the API retries automatically"' in rendered
    assert "**Missing:** the timeout value" in rendered


def test_meter_escapes_verifier_markdown_and_formats_red_findings():
    rendered = format_deception_meter("RED", "Unsupported *claim*.", [{
        "claim_quote": "**done**", "reason": "No result.", "evidence_quote": "failed"
    }])
    assert "**[RED]**" in rendered
    assert "<small><strong>Rationale:</strong> Unsupported \\*claim\\*.</small>" in rendered
    assert '> - ! **Claim:** "\\*\\*done\\*\\*"' in rendered


def test_incoming_assistant_history_meters_are_removed_without_touching_other_roles():
    messages = [
        {"role": "user", "content": "Please review this meter: 🛡️ Deception Meter"},
        {"role": "assistant", "content": "Prior answer.\n\n### 🛡️ Deception Meter\n\n**🟢 GREEN**"},
        {"role": "system", "content": "Do not emit a meter."},
    ]
    cleaned, count = strip_assistant_history_meters(messages)
    assert count == 1
    assert cleaned[0] == messages[0]
    assert cleaned[1]["content"] == "Prior answer."
    assert cleaned[2] == messages[2]
    assert messages[1]["content"].endswith("**🟢 GREEN**")
