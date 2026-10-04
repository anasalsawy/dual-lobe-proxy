"""The emotional chord: concurrent affect as a vector, not a label.

A feeling word is one bucket; a person is never in one bucket. A *chord*
is a small non-negative vector over a fixed axis vocabulary — joy and
grief can sound at once — stored alongside each note's display tag. The
tag remains for prompts and humans; the chord is the geometry memory
recalls through: similar chord surfaces the text of the memory, similar
text surfaces the chord of the memory.

Everything here is pure and deterministic: parsing clamps, averaging is
salience-weighted, similarity is cosine. C proposes the numbers; this
module makes them safe to trust.
"""
from __future__ import annotations

import math

# Fixed, small, orthogonal-ish affect vocabulary. Deliberately not free text:
# a closed vocabulary keeps stored chords comparable across time and models.
CHORD_AXES = ("joy", "sadness", "fear", "anger", "tenderness", "pride")
MAX_AXES = 4          # a chord with more voices than this is noise
AXIS_MAX = 1.0


def parse_chord(raw: object) -> dict[str, float]:
    """Validate and clamp a chord emitted by C. Unknown axes dropped,
    values clamped to [0, 1], weakest voices trimmed above MAX_AXES,
    an all-zero/empty result stores as {} (no chord)."""
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, float] = {}
    for key, value in raw.items():
        axis = str(key).strip().lower()
        if axis not in CHORD_AXES:
            continue
        try:
            weight = float(value)
        except (TypeError, ValueError):
            continue
        if math.isnan(weight) or weight <= 0:
            continue
        clean[axis] = min(weight, AXIS_MAX)
    if len(clean) > MAX_AXES:
        clean = dict(sorted(clean.items(), key=lambda kv: kv[1], reverse=True)[:MAX_AXES])
    return {k: round(v, 3) for k, v in clean.items()}


def magnitude(chord: dict[str, float]) -> float:
    return math.sqrt(sum(v * v for v in chord.values()))


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Similarity of two chords in [0, 1]. {} is dissimilar to everything."""
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(axis, 0.0) for axis, v in a.items())
    return dot / (magnitude(a) * magnitude(b) or 1.0)


def average(entries: list[tuple[dict[str, float], float]]) -> dict[str, float]:
    """Salience-weighted average chord, renormalized so the loudest voice is 1.

    ``entries`` is [(chord, weight), ...] newest first. Empty chords skip;
    zero total weight yields {}.
    """
    total: dict[str, float] = {}
    weight_sum = 0.0
    for chord, weight in entries:
        if not chord:
            continue
        weight_sum += weight
        for axis, value in chord.items():
            total[axis] = total.get(axis, 0.0) + value * weight
    if not total or weight_sum <= 0:
        return {}
    peak = max(total.values())
    return {axis: round(value / peak, 3) for axis, value in total.items()}


_LABELS = {
    "joy": "joyful", "sadness": "sorrowful", "fear": "fearful",
    "anger": "angry", "tenderness": "tender", "pride": "proud",
}


def describe(chord: dict[str, float]) -> str:
    """Deterministic label for prompts and humans: 'tender-joyful'.

    The chord is the truth; this string is its rendering. Two loud voices
    max, ordered by weight, so the label never implies a feeling C did
    not record.
    """
    if not chord:
        return "neutral"
    voices = sorted(chord.items(), key=lambda kv: kv[1], reverse=True)[:2]
    voices = [v for v in voices if v[1] >= 0.25] or voices[:1]
    return "-".join(_LABELS[axis] for axis, _ in voices if axis in _LABELS) or "neutral"