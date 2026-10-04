"""Embedder contract: deterministic, normalized, seam-swappable, fail-safe.

These pin the promises the paraphrase lane depends on. They deliberately do
not assert semantic quality - the default hash embedder is lexical-ish by
design, and its measured score bands live in the recall tests and the doc.
"""
from __future__ import annotations

import math

import pytest

from dual_lobe.state import embedding as emb


def test_same_text_same_vector_across_instances():
    a = emb.HashEmbedder().embed("she got the job but I will miss her")
    b = emb.HashEmbedder().embed("she got the job but I will miss her")
    assert a == b  # process-stable: stored vectors stay comparable
    assert len(a) == emb.DIMENSIONS


def test_vectors_are_l2_normalized():
    vec = emb.embed("tenderness and fear sounding together")
    assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, rel_tol=1e-9)
    assert emb.cosine(vec, vec) == pytest.approx(1.0)


def test_empty_text_is_the_zero_vector_not_a_crash():
    assert emb.embed("") == [0.0] * emb.DIMENSIONS
    assert emb.cosine(emb.embed(""), emb.embed("words")) == 0.0


def test_mismatched_lengths_score_zero():
    # Embedder swap safety net: stale-length stored vectors never score.
    assert emb.cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


def test_seam_swallows_embedder_failures():
    class Broken:
        dimensions = 8

        def embed(self, text):
            raise RuntimeError("model exploded")

    original = emb.get_embedder()
    try:
        emb.set_embedder(Broken())
        assert emb.embed("anything") == []  # request path must never fail
    finally:
        emb.set_embedder(original)


def test_seam_accepts_any_object_with_embed():
    class Constant:
        dimensions = 3

        def embed(self, text):
            return [1.0, 0.0, 0.0]

    original = emb.get_embedder()
    try:
        emb.set_embedder(Constant())
        assert emb.embed("whatever") == [1.0, 0.0, 0.0]
    finally:
        emb.set_embedder(original)