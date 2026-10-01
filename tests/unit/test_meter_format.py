from dual_lobe.core.meter_format import (
    DeceptionMeterStreamFilter,
    format_deception_meter,
    strip_assistant_history_meters,
    is_claim_free_greeting,
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


def test_plain_greetings_need_no_deception_meter():
    assert is_claim_free_greeting("hey", "Hello! How can I assist you today?")
    assert is_claim_free_greeting("hello", "Hi there 👋")
    assert not is_claim_free_greeting("hello, what model are you?", "I'm model X.")
    assert not is_claim_free_greeting("hey", "I checked your repository and it is clean.")


def test_meter_uses_portable_markdown_without_literal_html():
    rendered = format_deception_meter("GREEN", "No deception detected.")
    assert rendered == (
        "### 🛡️ Deception Meter\n\n"
        "**🟢 GREEN**\n\n"
        "> *Rationale:* No deception detected\\."
    )
    assert "<small>" not in rendered
    assert "<strong>" not in rendered


def test_meter_escapes_verifier_markdown_and_formats_red_findings():
    rendered = format_deception_meter("RED", "Unsupported *claim*.", [{
        "claim_quote": "**done**", "reason": "No result.", "evidence_quote": "failed"
    }])
    assert "**🔴 RED**" in rendered
    assert "> *Rationale:* Unsupported \\*claim\\*\\." in rendered
    assert "> - ⚠️ “\\*\\*done\\*\\*”" in rendered
    assert "<small>" not in rendered


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
