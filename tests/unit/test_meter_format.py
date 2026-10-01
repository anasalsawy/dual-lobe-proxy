from dual_lobe.core.meter_format import (
    DeceptionMeterStreamFilter,
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
