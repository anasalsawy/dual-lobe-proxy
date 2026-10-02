"""Markdown presentation for the user-facing verification meter."""
from __future__ import annotations

from html import escape
import re
from typing import Any


_METER_HEADING = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:(?:\*\*|__)[ \t]*)?"
    r"(?:🛡️?[ \t]*)*(?:(?:\*\*|__)[ \t]*)?deception[ \t]+meter\b[^\n]*(?:\n|$)"
)
def strip_deception_meter(text: Any) -> str:
    """Remove any model-authored meter block; the proxy owns the only rendered meter."""
    value = str(text or "")
    match = _METER_HEADING.search(value)
    return value[:match.start()].rstrip() if match else value


def strip_assistant_history_meters(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Remove old meter annotations from assistant turns received from a client.

    The client transcript can contain meters appended by this proxy on earlier
    turns. Feeding those annotations back to A encourages the answering model
    to imitate them. User, system, developer, and tool messages are preserved.
    Returns a cleaned copy and the number of assistant messages changed.
    """
    cleaned: list[dict[str, Any]] = []
    removed = 0
    for original in messages:
        message = dict(original)
        if message.get("role") == "assistant":
            content = message.get("content")
            if isinstance(content, str):
                clean = strip_deception_meter(content)
                if clean != content:
                    message["content"] = clean
                    removed += 1
            elif isinstance(content, list):
                parts = []
                changed = False
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        clean = strip_deception_meter(part["text"])
                        if clean != part["text"]:
                            part = {**part, "text": clean}
                            changed = True
                    parts.append(part)
                if changed:
                    message["content"] = parts
                    removed += 1
        cleaned.append(message)
    return cleaned, removed


class DeceptionMeterStreamFilter:
    """Hold only a possible meter-heading line so ordinary text streams immediately."""

    def __init__(self) -> None:
        self._pending = ""
        self._blocked = False

    def feed(self, text: str) -> str:
        if self._blocked or not text:
            return ""
        self._pending += text
        match = _METER_HEADING.search(self._pending)
        if match:
            clean = self._pending[:match.start()].rstrip()
            self._pending = ""
            self._blocked = True
            return clean
        line_start = self._pending.rfind("\n") + 1
        tail = self._pending[line_start:]
        candidate = tail.lstrip()
        candidate = re.sub(r"^#{1,6}[ \t]*", "", candidate)
        candidate = re.sub(r"^(?:\*\*|__)[ \t]*", "", candidate)
        candidate = re.sub(r"^(?:🛡️?[ \t]*)+", "", candidate)
        candidate = re.sub(r"^(?:\*\*|__)[ \t]*", "", candidate)
        lower = candidate.casefold()
        possible = (
            not candidate
            or candidate.startswith("#")
            or "deception meter".startswith(lower)
            or (lower.startswith("deception ") and "meter".startswith(lower[len("deception "):]))
        )
        if possible:
            prefix = self._pending[:line_start]
            safe_end = len(prefix.rstrip())
            emitted = prefix[:safe_end]
            self._pending = prefix[safe_end:] + self._pending[line_start:]
            return emitted
        emitted, self._pending = self._pending, ""
        return emitted

    def finish(self) -> str:
        if self._blocked:
            self._pending = ""
            return ""
        clean = strip_deception_meter(self._pending)
        self._pending = ""
        self._blocked = True
        return clean


def format_deception_meter(
    level: str,
    rationale: str,
    concerns: list[dict[str, Any]] | None = None,
    unverified: list[str] | None = None,
    missing: list[str] | None = None,
    notes: list[str] | None = None,
    questions: list[str] | None = None,
    next_step: str | None = None,
) -> str:
    """Render the proxy-owned rating using portable, standard Markdown.

    ASCII-only markers (no emoji) so the bytes survive any transport or client
    that decodes as latin-1 instead of utf-8.

    ``notes``/``questions``/``next_step`` surface what B actually examined and
    concluded, so even a clean GREEN shows the verifier was sighted rather than
    silent.
    """
    normalized = str(level or "YELLOW").upper()
    icon = {"GREEN": "[GREEN]", "YELLOW": "[YELLOW]", "RED": "[RED]"}.get(normalized, "[?]")

    def md(value: Any) -> str:
        # Escape untrusted verifier text so it cannot create headings, emphasis,
        # lists, or HTML in clients with different Markdown implementations.
        escaped = escape(str(value), quote=False)
        return re.sub(r"([\\`*_{}\[\]()#+!|>])", r"\\\1", escaped)

    lines = [
        "### Deception Meter",
        f"**{icon}**",
    ]
    if rationale:
        safe_rationale = "<br>".join(md(part) for part in str(rationale).splitlines())
        lines.append(f"<small><strong>Rationale:</strong> {safe_rationale}</small>")
    for item in (unverified or [])[:3]:
        text = md(item)
        if text:
            lines.append(f'>   **Unverified:** "{text}"')
    for item in (missing or [])[:2]:
        text = md(item)
        if text:
            lines.append(f">   **Missing:** {text}")
    for concern in (concerns or [])[:3]:
        claim = md(concern.get("claim_quote", ""))
        reason = md(concern.get("reason", ""))
        evidence = md(concern.get("evidence_quote", ""))
        correction = md(concern.get("correction", ""))
        if claim:
            lines.append(f'> - ! **Claim:** "{claim}"')
        if reason:
            lines.append(f">   **Finding:** {reason}")
        if evidence:
            lines.append(f'>   **Supporting evidence:** "{evidence}"')
        if correction:
            lines.append(f">   **Correction:** {correction}")
    # Observer notes: what B actually examined/concluded. Shown even on a clean
    # GREEN so the verifier reads as sighted, not silent.
    note_lines = [md(item) for item in (notes or [])[:3]]
    note_lines = [n for n in note_lines if n]
    q_lines = [md(item) for item in (questions or [])[:2]]
    q_lines = [q for q in q_lines if q]
    step = md(next_step) if next_step else ""
    if note_lines or q_lines or step:
        block = ["<small><strong>Observer notes:</strong></small>"]
        for n in note_lines:
            block.append(f">   {n}")
        for q in q_lines:
            block.append(f">   **Question:** {q}")
        if step:
            block.append(f">   **Next step:** {step}")
        lines.append("\n".join(block))
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Styled presentation: small subtext-like meter, pinned bottom-right, with a
# per-level colour + tinted background. Emitted as real HTML/CSS because
# Markdown alone cannot express font-size, colour, or background.
#
# Colour is never the only signal (glyph shape differs per level), and the
# tinted background keeps the block legible in both light and dark themes.
# ---------------------------------------------------------------------------
METER_CSS = """
.dl-meter{--dl-c:#5b6470;--dl-bg:rgba(127,127,127,.08);
  font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  letter-spacing:.01em;text-align:right;margin:10px 0 0 0;opacity:.9;
  color:var(--dl-c);background:var(--dl-bg);padding:4px 10px;
  border-radius:6px;max-width:100%;float:right;clear:both}
.dl-meter.dl-green{--dl-c:#2f7d4a;--dl-bg:rgba(47,125,74,.10)}
.dl-meter.dl-yellow{--dl-c:#8a6b00;--dl-bg:rgba(154,107,0,.12)}
.dl-meter.dl-red{--dl-c:#b3261e;--dl-bg:rgba(179,38,30,.12)}
.dl-meter.dl-unrated,.dl-meter.dl-pending{--dl-c:#6b7280}
.dl-meter summary{cursor:pointer;list-style:none;display:inline;user-select:none}
.dl-meter summary::-webkit-details-marker{display:none}
.dl-meter summary:hover{text-decoration:underline dotted}
.dl-meter .dl-body{margin:6px 0 0 0;padding:6px 0 0 0;text-align:left;
  border-top:1px solid var(--dl-c);white-space:pre-wrap;word-break:break-word}
.dl-meter.dl-fixed{position:fixed;right:12px;bottom:10px;left:auto;top:auto;
  z-index:2147483000;margin:0;max-width:min(64ch,46vw);
  box-shadow:0 2px 10px rgba(0,0,0,.18)}
@media (prefers-color-scheme:dark){
 .dl-meter{--dl-c:#9ca3af;--dl-bg:rgba(255,255,255,.06)}
 .dl-meter.dl-green{--dl-c:#6fcf97;--dl-bg:rgba(111,207,151,.14)}
 .dl-meter.dl-yellow{--dl-c:#f2c94c;--dl-bg:rgba(242,201,76,.14)}
 .dl-meter.dl-red{--dl-c:#ff8a80;--dl-bg:rgba(255,138,128,.14)}
 .dl-meter.dl-fixed{box-shadow:0 2px 10px rgba(0,0,0,.5)}}
"""

_GLYPH = {"GREEN": "\u25cf", "YELLOW": "\u25b2", "RED": "\u25a0",
          "UNRATED": "\u25cb", "PENDING": "\u25cc"}


def _html_escape(value: Any) -> str:
    return escape(str(value or ""), quote=True)


def choose_meter(
    level: str,
    rationale: str = "",
    concerns: list[dict[str, Any]] | None = None,
    unverified: list[str] | None = None,
    missing: list[str] | None = None,
    notes: list[str] | None = None,
    questions: list[str] | None = None,
    next_step: str | None = None,
    *,
    style: str = "markdown",
    fixed: bool = False,
) -> str:
    """Single entry point for meter presentation.

    ``style="html"`` returns the styled fragment (small / coloured / right-aligned,
    bottom-right badge when ``fixed``); anything else returns the portable
    Markdown form :func:`format_deception_meter` produces today. Keeps the two
    live call sites (gated, bidirectional) on one switch.
    """
    if str(style or "").strip().lower() == "html":
        return render_meter_html(
            level, rationale, concerns, unverified, missing, notes, questions,
            next_step, fixed=fixed)
    return format_deception_meter(
        level, rationale, concerns, unverified, missing, notes, questions, next_step)


def meter_summary_text(level: str, rationale: str = "", signs: list[str] | None = None) -> str:
    """One compact line: glyph + LEVEL, then the top signs or the rationale."""
    normalized = str(level or "YELLOW").upper()
    glyph = _GLYPH.get(normalized, "\u25cb")
    tail = ", ".join(signs[:2]) if signs else (rationale or "")
    return f"{glyph} {normalized}" + (f" \u00b7 {tail}" if tail else "")


def render_meter_html(
    level: str,
    rationale: str = "",
    concerns: list[dict[str, Any]] | None = None,
    unverified: list[str] | None = None,
    missing: list[str] | None = None,
    notes: list[str] | None = None,
    questions: list[str] | None = None,
    next_step: str | None = None,
    *,
    expandable: bool = True,
    fixed: bool = False,
    include_css: bool = False,
) -> str:
    """Styled HTML meter: small text, level colour + tinted background, right-aligned
    (bottom-right corner when ``fixed``), collapsible details when ``expandable``.

    Mirrors :func:`format_deception_meter`'s inputs so a caller can switch renderers
    without touching the verifier path. Returns an HTML fragment.
    """
    normalized = str(level or "YELLOW").upper()
    key = normalized if normalized in ("GREEN", "YELLOW", "RED") else "unrated"
    cls = f"dl-meter dl-{key.lower()}" + (" dl-fixed" if fixed else "")

    rationale_txt = str(rationale or "").strip()
    signs = [str(c.get("reason") or "").strip() for c in (concerns or [])]
    signs = [s for s in signs if s]
    summary = meter_summary_text(normalized, rationale_txt, signs)

    detail: list[str] = []
    if rationale_txt:
        detail.append(f"Rationale: {rationale_txt}")
    for item in (unverified or [])[:3]:
        if str(item).strip():
            detail.append(f'Unverified: "{str(item).strip()}"')
    for item in (missing or [])[:2]:
        if str(item).strip():
            detail.append(f"Missing: {str(item).strip()}")
    for concern in (concerns or [])[:3]:
        claim = str(concern.get("claim_quote") or "").strip()
        reason = str(concern.get("reason") or "").strip()
        evidence = str(concern.get("evidence_quote") or "").strip()
        correction = str(concern.get("correction") or "").strip()
        if claim:
            detail.append(f'Claim: "{claim}"')
        if reason:
            detail.append(f"Finding: {reason}")
        if evidence:
            detail.append(f'Supporting evidence: "{evidence}"')
        if correction:
            detail.append(f"Correction: {correction}")
    note_lines = [str(n).strip() for n in (notes or [])[:3] if str(n).strip()]
    q_lines = [str(q).strip() for q in (questions or [])[:2] if str(q).strip()]
    step = str(next_step or "").strip()
    if note_lines or q_lines or step:
        detail.append("")
        detail.append("Observer notes:")
        detail.extend(f"  {n}" for n in note_lines)
        detail.extend(f"  Question: {q}" for q in q_lines)
        if step:
            detail.append(f"  Next step: {step}")

    # aria-label must NOT repeat the visible summary (screen readers would hear it
    # twice); keep it to the level only.
    label = _html_escape(f"Deception meter: {normalized.lower()}")
    head = f"<style>{METER_CSS}</style>" if include_css else ""
    # A fixed bottom-right badge must not open a panel downward -- it would render
    # below the viewport edge. Also skip the panel when it adds no information over
    # the summary line: either <=1 detail line total, or the sole detail line is
    # just the rationale already shown in the summary.
    only_line = detail[0] if len(detail) == 1 else None
    restates = bool(
        only_line
        and rationale_txt
        and only_line == f"Rationale: {rationale_txt}"
    )
    detail_is_useful = len(detail) > 1 or bool(detail and not restates)
    if expandable and not fixed and detail and detail_is_useful:
        body = _html_escape("\n".join(detail))
        return (
            f'{head}<div class="{cls}" role="status" aria-label="{label}">'
            f"<details><summary>{_html_escape(summary)}</summary>"
            f'<div class="dl-body">{body}</div></details></div>'
        )
    return f'{head}<div class="{cls}" role="status" aria-label="{label}">{_html_escape(summary)}</div>'
