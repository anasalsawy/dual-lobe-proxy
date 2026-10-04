"""ND-SMC Phases 1-2: high-fidelity ingestion and the juice filter.

Two things must never happen in this module. The stored transcript is the
turn exactly as it arrived - stutters, slang, casing, typos, nothing
normalized; a rewrite would destroy the only asset the buffer exists to
hold. And nothing is stored unless its variance clears the floor: the
buffer is a lock on what is unusual, not a archive of everything.

Variance is measured, not guessed. It combines what C already computes for
the chord (loud voices, rare kinds, high salience), what style analysis sees
(hesitation, emphasis, register shift, rare vocabulary), and geometry -
distance from the space's running baseline of ordinary interaction. The
baseline lives in `MemorySpace.subconscious`, updated as an exponential
moving average, so "boring" is whatever this user's ordinary turns look like
rather than a universal prior.

The same embedding doubles as the sensory gut vector (the subconscious
vector): it never retrieves anything, it travels beside a turn so patterns
can be learned from it later.
"""
from __future__ import annotations

import json
import math
import re

from ..state.embedding import cosine, embed

# Style analysis is a deterministic read of the text, not a model: these
# markers describe surface shape only. They are stored alongside the raw
# text as labels, never in place of it.
_STUTTER = re.compile(r"(\w+)[ -]+\1", re.IGNORECASE)
_REPEATED_PUNCT = re.compile(r"[!?.]{2,}")
_ALLCAPS = re.compile(r"\b[A-Z]{3,}\b")
_ELLIPSIS = re.compile(r"[.][.][.]|[—–]{1,}")
_FILLER = {"like", "you", "kinda", "gotta", "just", "literally", "basically",
           "honestly", "whatever", "man", "dude", "yeah", "nah", "lol", "idk"}
_HEDGING = {"maybe", "perhaps", "possibly", "guess", "kinda", "sorta",
            "maybe", "not", "never", "nothing", "always"}
_STRONG = {"never", "always", "hate", "love", "must", "cant", "cannot",
           "absolutely", "completely", "totally", "everything", "everyone",
           "nobody", "nothing", "forever", "obsessed", "terrified",
           "ecstatic", "furious", "devastated"}
_SWEARS = {"damn", "hell", "shit", "fuck", "ass", "bastard", "crap"}


def style_markers(text: str) -> list[str]:
    """Surface-shape labels for a raw turn. Detection only; text untouched."""
    if not text or not text.strip():
        return []
    markers: list[str] = []
    words = re.findall(r"[A-Za-z']+", text)
    if len(words) < 2:
        return ["terse"]
    lowered = [w.lower() for w in words]
    if _STUTTER.search(text) or sum(1 for w in lowered if w.endswith("...")):
        markers.append("hesitant")
    if any(c in text for c in (",", ";")) and _ELLIPSIS.search(text):
        markers.append("halting")
    if _REPEATED_PUNCT.search(text) or text.rstrip().endswith(("!", "?")):
        markers.append("emphatic")
    if _ALLCAPS.search(text):
        markers.append("shouting")
    if text.isupper() and len(text) > 8:
        markers.append("shouting")
    filler = sum(1 for w in lowered if w in _FILLER) / max(1, len(lowered))
    if filler > 0.22:
        markers.append("informal")
    hedging = sum(1 for w in lowered if w in _HEDGING) / max(1, len(lowered))
    if hedging > 0.14:
        markers.append("guarded")
    strong = sum(1 for w in lowered if w in _STRONG) / max(1, len(lowered))
    if strong > 0.12:
        markers.append("absolute")
    if any(w in lowered for w in _SWEARS):
        markers.append("coarse")
    if not _ELLIPSIS.search(text) and not any(m in markers for m in
                                              ("hesitant", "halting")):
        if len(words) >= 14 and filler < 0.08:
            markers.append("composed")
    rare = sum(1 for w in lowered if len(w) >= 12) / max(1, len(lowered))
    if rare > 0.08:
        markers.append("unusual_vocabulary")
    if len(text) > 320:
        markers.append("long")
    return markers


def _marker_intensity(markers: list[str]) -> float:
    """How much these markers depart from a plain declarative turn."""
    if not markers:
        return 0.0
    loud = {"shouting", "emphatic", "coarse", "absolute", "unusual_vocabulary"}
    soft = {"hesitant", "halting", "guarded", "informal"}
    score = 0.55 * len(set(markers) & loud) + 0.4 * len(set(markers) & soft)
    if "composed" in markers and len(markers) == 1:
        return 0.0
    return min(1.0, score)


def _chord_intensity(chord: dict | None) -> float:
    if not chord:
        return 0.0
    weights = [float(v) for v in chord.values() if isinstance(v, (int, float))]
    if not weights:
        return 0.0
    loud = sum(1 for w in weights if w >= 0.7)
    mixed = 1.0 if len(weights) >= 2 and min(weights) >= 0.3 else 0.0
    return min(1.0, 0.45 * loud + 0.5 * mixed)


def emotional_variance(text: str, *, markers: list[str] | None = None,
                       chord: dict | None = None, salience: int = 1,
                       kind: str = "detail", novelty: float = 0.5) -> float:
    """Score a turn 0.0-1.0 for how much of it is worth keeping raw.

    `novelty` is 1 - cosine against the space's baseline (0.5 default when
    no baseline exists yet, which keeps early turns judged on their own
    content rather than on an empty reference). Salience and kind come from
    C, so the filter is C's judgment plus arithmetic - no extra LLM call.
    """
    if not text or not text.strip():
        return 0.0
    style = _marker_intensity(markers if markers is not None else style_markers(text))
    affect = _chord_intensity(chord)
    weight = {3: 1.0, 2: 0.55, 1: 0.15}.get(int(salience or 1), 0.15)
    kind_bonus = 1.0 if kind in ("affect", "callback") else 0.25
    affect_term = min(1.0, affect * 0.6 + weight * 0.4 * kind_bonus)
    score = 0.45 * float(novelty) + 0.35 * style + 0.20 * affect_term
    return round(max(0.0, min(1.0, score)), 3)


def sensory_vector(text: str) -> list[float]:
    """The turn's gut vector: an embedding used as context, never as a query.

    It is deliberately not searchable. It exists so behavior can be learned
    from it (and so a later week can recognize a familiar situation without
    remembering it).
    """
    return embed(text)


def update_baseline(baseline: list[float] | None, vector: list[float],
                    *, alpha: float = 0.1) -> list[float]:
    """Exponential moving average of ordinary interaction, L2-normalized.

    High-variance turns are folded in weakly (alpha/4) so a single
    emotional peak does not redefine what counts as ordinary; low-variance
    turns train it normally. Empty/short vectors are ignored rather than
    collapsing the baseline.
    """
    if not vector or not any(vector):
        return list(baseline or [])
    if not baseline or len(baseline) != len(vector):
        return list(vector)
    rate = alpha if cosine(vector, baseline) > 0.6 else alpha / 4.0
    mixed = [(1 - rate) * b + rate * v for b, v in zip(baseline, vector)]
    norm = math.sqrt(sum(v * v for v in mixed))
    return [v / norm for v in mixed] if norm else list(baseline)


def parse_baseline(raw: str | None) -> list[float]:
    """Read the stored subconscious blob; anything malformed means no baseline.

    The column also holds future subconscious state, so a non-list payload is
    not an error to raise - it is simply no baseline yet.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("baseline")
    if not isinstance(data, list):
        return []
    return [float(v) for v in data if isinstance(v, (int, float))]


def render_baseline(vector: list[float] | None) -> str:
    return json.dumps([(round(float(v), 5) if isinstance(v, float) else 0.0)
                       for v in (vector or [])])