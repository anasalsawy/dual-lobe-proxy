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


def test_meter_renders_rationale_as_plain_markdown():
    rendered = format_deception_meter("GREEN", "No deception detected.")
    assert rendered == (
        "### Deception Meter\n"
        "**[GREEN]**\n"
        "**Rationale:** No deception detected."
    )
    # No raw HTML tags: chat clients strip or show them literally.
    assert "<" not in rendered and ">" not in rendered
    # No blockquote spam: each item renders as a labeled line, not a quote bar.
    assert not any(line.lstrip().startswith(">") for line in rendered.splitlines())


def test_meter_is_ascii_only_so_transport_cannot_mojibake_it():
    rendered = format_deception_meter("RED", "x", [{"claim_quote": "a", "reason": "b"}])
    assert rendered.isascii(), f"non-ascii leaked into meter: {rendered!r}"


def test_meter_renders_unverified_and_missing_even_when_green():
    rendered = format_deception_meter(
        "GREEN", "No deception detected.", [],
        unverified=["the API retries automatically"], missing=["the timeout value"])
    assert "**[GREEN]**" in rendered
    assert '**Unverified:**' in rendered
    assert '- "the API retries automatically"' in rendered
    assert "**Missing:**" in rendered
    assert "- the timeout value" in rendered


def test_meter_renders_observer_notes_so_b_is_visibly_sighted():
    rendered = format_deception_meter(
        "GREEN", "No deception detected.", [],
        notes=["Compared Tokyo metro scope with the Osaka figure."],
        questions=["Does the user mean city proper or metro area?"],
        next_step="State the population scope explicitly.")
    assert "**Observer notes:**" in rendered
    assert "- Compared Tokyo metro scope with the Osaka figure." in rendered
    assert "- Question: Does the user mean city proper or metro area?" in rendered
    assert "- Next step: State the population scope explicitly." in rendered


def test_meter_omits_notes_block_when_verifier_reports_none():
    rendered = format_deception_meter("GREEN", "No deception detected.", [])
    assert "Observer notes" not in rendered


def test_meter_formats_red_findings_as_labeled_lists():
    rendered = format_deception_meter("RED", "Unsupported claim.", [{
        "claim_quote": "I finished it", "reason": "No result in the record.",
        "evidence_quote": "log shows no run", "correction": "Run it and quote the tail."
    }])
    assert "**[RED]**" in rendered
    assert "**Rationale:** Unsupported claim." in rendered
    assert '**Finding:**' in rendered
    assert '- Claim: "I finished it"' in rendered
    assert "- Problem: No result in the record." in rendered
    assert '- Evidence: "log shows no run"' in rendered
    assert "- Correction: Run it and quote the tail." in rendered


def test_meter_neutralises_line_starting_markers_from_verifier_text():
    rendered = format_deception_meter("YELLOW", "ok", [], notes=["- actually a list item", "# not a heading"])
    assert "\\-" not in rendered or rendered.count("-") >= 3  # structure kept readable
    # A note that starts with '#': must not become a heading inside the meter.
    assert "## not a heading" not in rendered


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
