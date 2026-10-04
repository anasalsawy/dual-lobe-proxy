"""In-perimeter text embeddings: a candidate generator, never a decider.

The chord is how recall decides; embeddings only widen the candidate pool
beyond the boolean word-overlap tsquery (near forms, subsets, reworded
asides). The default embedder is a pure-Python feature hash - no model, no
network, no new dependency, deterministic across processes. It is honest
about being lexical-ish: it never claims semantic understanding.

Provider seam: `set_embedder()` accepts any object with
`embed(text) -> list[float]` (a local sentence-transformer later, say).
External embedding APIs are excluded by policy - raw user text must not
leave the A/B/C perimeter. Vectors are stored per note; a stored vector
whose length differs from the active embedder's is treated as absent, so
swapping embedders degrades recall to the old behavior instead of
comparing incompatible geometries.
"""
from __future__ import annotations

import hashlib
import math
import re

DIMENSIONS = 384
_WORD = re.compile(r"[^\W_]{3,64}", re.UNICODE)


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine of two vectors (already L2-normalized: just a dot product)."""
    if len(a) != len(b) or not a:
        return 0.0
    return sum(x * y for x, y in zip(a, b))


class HashEmbedder:
    """Signed feature hashing over words + character trigrams, L2-normalized.

    Words carry weight 1.0, trigrams 0.4: enough overlap signal for near
    forms without drowning the content words. Sublinear term frequency
    keeps one repeated word from dominating a long text.
    """

    dimensions = DIMENSIONS

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        words = _WORD.findall((text or "").lower())
        weights: dict[str, float] = {}
        for word in words:
            weights[word] = weights.get(word, 0.0) + 1.0
            for i in range(max(0, len(word) - 2)):
                tri = word[i:i + 3]
                weights[tri] = weights.get(tri, 0.0) + 0.4
        for token, count in weights.items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=5).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign * math.log1p(count)
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]


_embedder = HashEmbedder()


def get_embedder():
    return _embedder


def set_embedder(embedder) -> None:
    """Swap the embedder (local models only; external APIs are out of policy).

    Stored vectors from the previous embedder are skipped on length
    mismatch, so this degrades to pre-embedding recall rather than
    scoring garbage.
    """
    global _embedder
    _embedder = embedder


def embed(text: str) -> list[float]:
    try:
        return _embedder.embed(text)
    except Exception:
        # Recall must never fail the request path; empty skips the lane.
        return []