"""Gated inference: B sits inline between Hermes and A.

Unlike the async shadow-cycle design (sawii/dual-lobe), this module runs
B's LLM inline in the request path:

  Upstream:   B enriches context (observation + broadening + last-turn meter)
              BEFORE A generates.
  Downstream: B compares A's response against evidence in the conversation
              and produces a deception meter.
              GREEN/YELLOW → forward to user. RED → flip back to A for
              reconsideration (one cycle, then forward regardless).

B is always an LLM step, never a deterministic shortcut.  The meter is
injected into A's context on the NEXT upstream, not shown to the user.
"""
