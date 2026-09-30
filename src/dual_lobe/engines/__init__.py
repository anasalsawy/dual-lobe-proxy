"""Per-service engines ported from the CrewAI versions onto the gated base.

``DUAL_LOBE_ENGINE`` selects what ``/v1/chat/completions`` runs:
``split`` (A delegates, live B + adversarial B review), ``clinical``
(A plans, local B executes, A reviews), or ``gated`` (default).
"""
