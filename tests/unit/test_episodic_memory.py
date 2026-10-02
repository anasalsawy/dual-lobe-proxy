"""Episodic memory: B's notes contract, grounding, and callback rendering."""
from __future__ import annotations

from dual_lobe.b.notebook import build_notes_prompt, parse_notes
from dual_lobe.state.memory import LoadedMemory, compose_callbacks


def test_notes_prompt_carries_the_transcript_and_marker():
    prompt = build_notes_prompt(transcript="user: hello")
    assert "TRANSCRIPT FOR NOTES:" in prompt
    assert "user: hello" in prompt
    assert "verbatim" in prompt.lower()


def test_valid_note_with_verbatim_quote_is_kept():
    transcript = "user: my dog has a limp lol\nassistant: ok"
    content = ('{"notes":[{"kind":"detail","anchor":"debugging","quote":"my dog has a limp lol",'
               '"feeling":"warm","salience":2}]}')
    notes = parse_notes(content, known_texts=[transcript])
    assert notes == [{"kind": "detail", "anchor": "debugging",
                      "quote": "my dog has a limp lol", "feeling": "warm", "salience": 2}]


def test_hallucinated_quote_is_dropped():
    """A note whose quote never appears in the transcript must not survive."""
    transcript = "user: my dog has a limp"
    content = '{"notes":[{"kind":"detail","anchor":"x","quote":"my cat died yesterday"}]}'
    assert parse_notes(content, known_texts=[transcript]) == []


def test_quote_without_grounding_list_is_kept():
    content = '{"notes":[{"kind":"preference","quote":"i love tabs over spaces"}]}'
    notes = parse_notes(content)
    assert len(notes) == 1 and notes[0]["quote"] == "i love tabs over spaces"


def test_junk_and_fenced_json():
    assert parse_notes("no json here") == []
    assert parse_notes("") == []
    fenced = '```json\n{"notes":[{"kind":"callback","quote":"remember the joke"}]}\n```'
    assert parse_notes(fenced)[0]["kind"] == "callback"


def test_unknown_kind_falls_back_to_detail():
    content = '{"notes":[{"kind":"nonsense","quote":"hi there"}]}'
    assert parse_notes(content)[0]["kind"] == "detail"


def test_salience_is_clamped_by_the_model_schema():
    # salience above 3 fails strict validation -> whole payload dropped (fail safe).
    content = '{"notes":[{"kind":"detail","quote":"hi","salience":99}]}'
    assert parse_notes(content) == []


def test_callbacks_block_frames_quotes_as_past_user_words():
    text = compose_callbacks("main", [{"kind": "detail", "anchor": "debugging",
                                       "quote": "my dog has a limp lol",
                                       "feeling": "warm", "at": "2026-10-01T00:00:00"}], 10000)
    assert text is not None
    assert "the user's own words" in text
    assert "my dog has a limp lol" in text
    assert '"callbacks"' in text


def test_callbacks_none_when_empty():
    assert compose_callbacks("main", [], 10000) is None


def test_loaded_memory_defaults_have_no_callbacks():
    assert LoadedMemory().callbacks is None


def test_callbacks_shrink_when_over_budget():
    notes = [{"kind": "detail", "quote": "x" * 300} for _ in range(3)]
    text = compose_callbacks("main", notes, 600)
    assert text is not None and len(text) <= 600


def test_note_query_terms_drops_stopwords_and_short_words():
    """A generic message must not produce terms that spuriously match a note."""
    from dual_lobe.state.memory import note_query_terms
    terms = note_query_terms([{"role": "user", "content": "help me pick a hex color for the button"}])
    assert "the" not in terms and "for" not in terms and "help" not in terms
    assert "color" in terms and "button" in terms and "pick" in terms


def test_note_query_terms_keeps_meaningful_topic_words():
    from dual_lobe.state.memory import note_query_terms
    terms = note_query_terms([{"role": "user", "content": "back to the retry loop bug"}])
    assert "retry" in terms and "loop" in terms
    assert "back" not in terms and "the" not in terms


def test_note_query_terms_empty_for_pure_stopwords():
    from dual_lobe.state.memory import note_query_terms
    assert note_query_terms([{"role": "user", "content": "help me with that the back"}]) == []
