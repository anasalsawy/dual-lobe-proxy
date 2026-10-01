from dual_lobe.core.meter_format import DeceptionMeterStreamFilter, strip_deception_meter


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

