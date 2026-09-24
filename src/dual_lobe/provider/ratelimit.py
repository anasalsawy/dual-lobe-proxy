"""Upstream provider rate-limit discovery and adaptive pacing.

Every upstream LLM call in this proxy (lobe A, lobe B, co-author, gated,
director, router) flows through :class:`~dual_lobe.provider.adapters.ChatCompletionsAdapter`,
which uses this module as its single pacing function:

1. ``detect_provider`` identifies which upstream a target is talking to from its
   base URL / model.
2. A known provider starts from its published free-tier budget (or an explicit
   ``DUAL_LOBE_UPSTREAM_RATE_OVERRIDES`` entry); an unknown provider starts
   unpaced and is learned from traffic.
3. Every response header set is parsed: ``X-RateLimit-Limit-*``,
   ``X-RateLimit-Remaining-*``, ``X-RateLimit-Reset-*``, ``Retry-After``.
   That is the "fetch": the provider itself reports the live budget.
4. ``RateGate.acquire`` blocks the caller until the request fits inside the
   current RPM/TPM/daily window, so the upstream is never asked for more than
   it allows.
5. A 429 still shrinks the budget and parks the gate for ``Retry-After``
   (exponential fallback when absent); sustained success grows it back toward
   the ceiling reported by the provider.

The gate is process-local (like ``api/limits.py``) and keyed by provider host +
API key, because upstream budgets are per credential, not per lobe alias.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

LOG = logging.getLogger("dual_lobe.ratelimit")

WINDOW = 60.0
DAY = 86400.0
MIN_RPM = 1.0
MIN_TPM = 500.0
MAX_BACKOFF = 300.0


class UpstreamRateLimited(Exception):
    """Raised when a provider budget cannot be satisfied inside max wait."""

    status_code = 429
    retry_after = 0.0

    def __init__(self, provider: str, retry_after: float, detail: str = "") -> None:
        self.provider = provider
        self.retry_after = max(1.0, float(retry_after))
        super().__init__(
            f"{provider} upstream rate budget exhausted; retry in {self.retry_after:.0f}s"
            + (f" ({detail})" if detail else "")
        )


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    hosts: tuple[str, ...]
    rpm: float | None = None
    tpm: float | None = None
    rpd: float | None = None


# Published free-tier budgets, deliberately conservative: under-shooting a free
# budget is cheap, a429 costs a full retry. Header discovery overrides these
# upward/downward on the first response.
PROFILES: tuple[ProviderProfile, ...] = (
    ProviderProfile("gemini", ("generativelanguage.googleapis.com", "aiplatform.googleapis.com"),
                    rpm=15, tpm=150_000, rpd=200),
    ProviderProfile("openrouter", ("openrouter.ai",), rpm=20, tpm=100_000, rpd=50),
    ProviderProfile("nvidia", ("integrate.api.nvidia.com",), rpm=20, tpm=40_000),
    ProviderProfile("groq", ("api.groq.com",), rpm=30, tpm=100_000),
    ProviderProfile("cerebras", ("api.cerebras.ai",), rpm=30, tpm=100_000),
    ProviderProfile("openai", ("api.openai.com",), rpm=500, tpm=200_000),
    ProviderProfile("mistral", ("api.mistral.ai",), rpm=100, tpm=200_000),
    ProviderProfile("deepinfra", ("api.deepinfra.com",), rpm=30, tpm=200_000),
    ProviderProfile("featherless", ("api.featherless.ai",), rpm=60, tpm=200_000),
)


def detect_provider(base_url: str, model: str = "") -> str:
    """Best-effort upstream identification from the target's base URL."""
    host = (urlparse(base_url or "").netloc or (base_url or "")).lower().split("@")[-1]
    host = host.split(":")[0]
    for profile in PROFILES:
        if any(host == h or host.endswith("." + h) for h in profile.hosts):
            return profile.name
    if model.startswith(("gemini/", "models/gemini")):
        return "gemini"
    if model.startswith(("openai/", "gpt-")):
        return "openai"
    return host or "unknown"


def _profile(provider: str) -> ProviderProfile | None:
    for profile in PROFILES:
        if profile.name == provider:
            return profile
    return None


def _overrides() -> dict[str, dict[str, float]]:
    """``DUAL_LOBE_UPSTREAM_RATE_OVERRIDES``: {"gemini": {"rpm": 10}, ...}."""
    from ..core.settings import get_settings

    raw = (get_settings().upstream_rate_overrides or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        LOG.warning("upstream rate overrides are not valid JSON; ignoring")
        return {}


def _header(headers: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = headers.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _seconds(value: str | None) -> float | None:
    """Parse Retry-After / X-RateLimit-Reset styles into seconds from now."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        numeric = float(text)
        # Large plain numbers are absolute unix timestamps, not deltas.
        return max(0.0, numeric - time.time()) if numeric > 1e6 else max(0.0, numeric)
    except ValueError:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            stamp = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return max(0.0, stamp.timestamp() - time.time())
        except ValueError:
            continue
    if text[:1].isdigit() and ("T" in text or "-" in text):
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0.0, stamp.timestamp() - time.time())
        except ValueError:
            pass
    compact = re.sub(r"^(?:PT|P)", "", text, flags=re.I)
    hours = [g for g in re.findall(r"(\d+(?:\.\d+)?)\s*h", compact, flags=re.I)]
    minutes = [g for g in re.findall(r"(\d+(?:\.\d+)?)\s*m(?!s)", compact, flags=re.I)]
    seconds = [g for g in re.findall(r"(\d+(?:\.\d+)?)\s*s", compact, flags=re.I)]
    if hours or minutes or seconds:
        total = (float(hours[-1]) * 3600 if hours else 0.0) \
            + (float(minutes[-1]) * 60 if minutes else 0.0) \
            + (float(seconds[-1]) if seconds else 0.0)
        return max(0.0, total)
    return None


class RateGate:
    """Paces one upstream credential so its published budget is never exceeded."""

    def __init__(self, key: str, provider: str, *, rpm: float | None, tpm: float | None,
                 rpd: float | None, max_wait: float) -> None:
        self.key = key
        self.provider = provider
        self.max_wait = max_wait
        self._lock = asyncio.Lock()
        self._reqs: deque[float] = deque()
        self._toks: deque[tuple[float, int]] = deque()
        self._tok_total = 0
        self._rpm = rpm
        self._tpm = tpm
        self._rpd = rpd
        self._ceil_rpm = rpm
        self._ceil_tpm = tpm
        self._pause_until = 0.0
        self._day_index = int(time.time() // DAY)
        self._day_used = 0
        self._consecutive_ok = 0
        self.total_requests = 0
        self.total_paced_waits = 0
        self.last_status: int | None = None
        self.last_429_at: float | None = None
        self.last_header_at: float | None = None
        self.last_wait = 0.0

    # -- windows ---------------------------------------------------------
    def _prune(self, now: float) -> None:
        while self._reqs and now - self._reqs[0] >= WINDOW:
            self._reqs.popleft()
        while self._toks and now - self._toks[0][0] >= WINDOW:
            self._tok_total -= self._toks[0][1]
            self._toks.popleft()

    def _roll_day(self) -> None:
        index = int(time.time() // DAY)
        if index != self._day_index:
            self._day_index = index
            self._day_used = 0

    def _day_end(self) -> float:
        return (self._day_index + 1) * DAY - time.time()

    # -- pacing ----------------------------------------------------------
    async def acquire(self, tokens: int) -> None:
        from ..core.settings import get_settings

        if not get_settings().upstream_rate_enabled:
            return
        tokens = max(1, int(tokens))
        async with self._lock:
            for _ in range(64):
                now = time.monotonic()
                self._prune(now)
                self._roll_day()
                wait = 0.0
                if self._pause_until > now:
                    wait = self._pause_until - now
                if self._rpm and len(self._reqs) + 1 > self._rpm:
                    wait = max(wait, self._reqs[0] + WINDOW - now)
                if self._tpm:
                    projected = self._tok_total + tokens
                    if projected > self._tpm:
                        deficit = projected - self._tpm
                        released = 0
                        reset_at = now
                        for stamp, size in self._toks:
                            released += size
                            reset_at = stamp + WINDOW
                            if released >= deficit:
                                break
                        wait = max(wait, reset_at - now)
                if self._rpd and self._day_used + 1 > self._rpd:
                    wait = max(wait, self._day_end())
                if wait <= 0:
                    stamp = time.monotonic()
                    self._reqs.append(stamp)
                    self._toks.append((stamp, tokens))
                    self._tok_total += tokens
                    self._day_used += 1
                    self.total_requests += 1
                    return
                if wait > self.max_wait:
                    self.total_paced_waits += 1
                    raise UpstreamRateLimited(self.provider, wait, detail=self._why(wait))
                self.last_wait = wait
                self.total_paced_waits += 1
                await asyncio.sleep(wait)
            raise UpstreamRateLimited(self.provider, 1.0, detail="pacing loop did not converge")

    def _why(self, wait: float) -> str:
        now = time.monotonic()
        if self._pause_until > now:
            return "provider backoff window"
        if self._rpd and self._day_used + 1 > self._rpd:
            return f"daily budget {self._day_used}/{self._rpd:.0f}"
        if self._rpm and len(self._reqs) + 1 > self._rpm:
            return f"rpm {len(self._reqs)}/{self._rpm:.0f}"
        if wait > 0 and self._tpm:
            return f"tpm {self._tok_total}/{self._tpm:.0f}"
        return "upstream budget"

    def record_usage(self, usage: dict[str, Any] | None) -> None:
        """Feed real prompt/completion tokens back into the TPM window."""
        if not isinstance(usage, dict):
            return
        total = usage.get("total_tokens")
        if total is None:
            total = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
        try:
            total = int(total or 0)
        except (TypeError, ValueError):
            return
        if total <= 0:
            return
        self._toks.append((time.monotonic(), total))
        self._tok_total += total
        self._prune(time.monotonic())

    # -- header discovery ------------------------------------------------
    def observe(self, headers: Any, status: int | None = None) -> None:
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        self.last_status = status
        self.last_header_at = time.time()
        try:
            lowered = {str(k).lower(): str(v) for k, v in dict(headers).items()}
        except Exception:
            lowered = {}
        limit_rpm = _number(_header(lowered, "x-ratelimit-limit-requests",
                                    "ratelimit-limit-requests", "x-ratelimit-requests-limit"))
        limit_tpm = _number(_header(lowered, "x-ratelimit-limit-tokens",
                                    "ratelimit-limit-tokens", "x-ratelimit-tokens-limit"))
        if limit_rpm and limit_rpm > 0:
            self._ceil_rpm = limit_rpm
            if self._rpm is None or self._rpm > limit_rpm:
                # Adopt a lower ceiling immediately; recovery grows back to it.
                self._rpm = limit_rpm
        if limit_tpm and limit_tpm > 0:
            self._ceil_tpm = limit_tpm
            if self._tpm is None or self._tpm > limit_tpm:
                self._tpm = limit_tpm

        remaining_rpm = _number(_header(lowered, "x-ratelimit-remaining-requests",
                                        "ratelimit-remaining-requests"))
        remaining_tpm = _number(_header(lowered, "x-ratelimit-remaining-tokens",
                                        "ratelimit-remaining-tokens"))
        reset = _seconds(_header(lowered, "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
                                 "x-ratelimit-reset", "ratelimit-reset"))
        now_mono = time.monotonic()
        if remaining_rpm is not None and remaining_rpm <= 0:
            self._pause_until = max(self._pause_until, now_mono + (reset or 5.0))
        elif remaining_tpm is not None and remaining_tpm <= 0:
            self._pause_until = max(self._pause_until, now_mono + (reset or 5.0))

        if status == 429:
            self.note_rejected(lowered)
        elif status is not None and 200 <= status < 300:
            self.note_success()

    def note_rejected(self, headers: dict[str, str]) -> None:
        retry = _seconds(_header(headers, "retry-after"))
        if retry is None:
            retry_ms = _number(_header(headers, "retry-after-ms"))
            retry = (retry_ms / 1000.0) if retry_ms else None
        if retry is None:
            retry = min(MAX_BACKOFF, 2.0 ** min(6, self._consecutive_ok + 1))
        retry = max(1.0, min(MAX_BACKOFF, retry))
        self._pause_until = max(self._pause_until, time.monotonic() + retry)
        if self._rpm:
            self._rpm = max(MIN_RPM, round(self._rpm * 0.7, 2))
        elif self._reqs:
            self._rpm = max(MIN_RPM, round(len(self._reqs) * 0.7, 2))
            self._ceil_rpm = self._ceil_rpm or self._rpm
        if self._tpm:
            self._tpm = max(MIN_TPM, round(self._tpm * 0.7, 2))
        self._consecutive_ok = 0
        self.last_429_at = time.time()
        LOG.warning("upstream429 provider=%s backing_off=%.1fs rpm=%s tpm=%s",
                    self.provider, retry, self._rpm, self._tpm)

    def note_success(self) -> None:
        self._consecutive_ok += 1
        if self._consecutive_ok < 10:
            return
        self._consecutive_ok = 0
        if self._ceil_rpm and self._rpm and self._rpm < self._ceil_rpm:
            self._rpm = min(self._ceil_rpm, round(self._rpm * 1.1, 2))
        if self._ceil_tpm and self._tpm and self._tpm < self._ceil_tpm:
            self._tpm = min(self._ceil_tpm, round(self._tpm * 1.1, 2))
        if self._pause_until and self._pause_until <= time.monotonic():
            self._pause_until = 0.0

    # -- introspection ---------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        self._prune(now)
        self._roll_day()
        return {
            "provider": self.provider,
            "credential": self.key.split("|", 1)[0],
            "rpm": self._rpm,
            "rpm_ceiling": self._ceil_rpm,
            "rpm_in_window": len(self._reqs),
            "tpm": self._tpm,
            "tpm_ceiling": self._ceil_tpm,
            "tpm_in_window": self._tok_total,
            "requests_per_day": self._rpd,
            "requests_today": self._day_used,
            "paused_for_seconds": round(max(0.0, self._pause_until - now), 3),
            "max_wait_seconds": self.max_wait,
            "total_requests": self.total_requests,
            "paced_waits": self.total_paced_waits,
            "last_status": self.last_status,
            "last_429_at": self.last_429_at,
            "last_header_at": self.last_header_at,
        }


_gates: dict[str, RateGate] = {}


def _gate_key(base_url: str, api_key: str) -> str:
    host = (urlparse(base_url or "").netloc or (base_url or "")).lower()
    digest = hashlib.sha256((api_key or "").encode()).hexdigest()[:8]
    return f"{host}|{digest}"


def gate_for(target: Any) -> RateGate:
    """Return (creating once) the pacing gate for this provider credential."""
    from ..core.settings import get_settings

    key = _gate_key(getattr(target, "base_url", ""), getattr(target, "api_key", ""))
    gate = _gates.get(key)
    if gate is not None:
        return gate
    provider = detect_provider(getattr(target, "base_url", ""), getattr(target, "model", ""))
    profile = _profile(provider)
    rpm = profile.rpm if profile else None
    tpm = profile.tpm if profile else None
    rpd = profile.rpd if profile else None
    override = _overrides().get(provider) or {}
    if isinstance(override, dict):
        rpm = float(override["rpm"]) if override.get("rpm") else rpm
        tpm = float(override["tpm"]) if override.get("tpm") else tpm
        rpd = float(override["rpd"]) if override.get("rpd") else rpd
    gate = RateGate(f"{provider}|{key.split('|', 1)[1]}", provider, rpm=rpm, tpm=tpm, rpd=rpd,
                    max_wait=get_settings().upstream_rate_max_wait)
    _gates[key] = gate
    LOG.info("upstream rate gate ready provider=%s base_url=%s rpm=%s tpm=%s rpd=%s",
             provider, getattr(target, "base_url", ""), rpm, tpm, rpd)
    return gate


def estimate_request_tokens(req: Any) -> int:
    """Cheap token estimate used to reserve TPM headroom before the call."""
    try:
        blob = json.dumps({"messages": getattr(req, "messages", None),
                           "tools": getattr(req, "tools", None)}, default=str)
        size = len(blob)
    except Exception:
        size = 4000
    return max(16, size // 4)


def snapshot() -> list[dict[str, Any]]:
    return [gate.snapshot() for gate in _gates.values()]


def reset_for_tests() -> None:
    _gates.clear()
