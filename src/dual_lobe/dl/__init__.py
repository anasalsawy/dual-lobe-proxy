"""Dual-lobe mode: isolated background thinking with a ring-buffer store.

Not part of the gated handler, the director engine, or the hierarchy tiers.
"""
from .handler import dual_lobe_event_stream, dual_lobe_response
from .store import DualLobeStore

__all__ = ["dual_lobe_response", "dual_lobe_event_stream", "DualLobeStore"]
