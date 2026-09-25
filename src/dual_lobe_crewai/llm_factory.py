from __future__ import annotations

import json
import os
from dataclasses import replace
from crewai import LLM

from .provider_control import ProviderSpec


def _role_defaults(role: str) -> tuple[str, int, str]:
    role = role.upper()
    a_default = os.getenv("DUAL_LOBE_A_MODEL", "openrouter/nvidia/nemotron-3-super-120b-a12b:free")
    b_default = os.getenv("DUAL_LOBE_B_MODEL", a_default)
    splitter_default = os.getenv("DUAL_LOBE_SPLITTER_MODEL", "openrouter/google/gemini-2.5-flash-lite")
    if role == "A":
        return a_default, int(os.getenv("DUAL_LOBE_A_MAX_TOKENS", "8000")), "A"
    if role == "A_MERGE":
        return os.getenv("DUAL_LOBE_A_MERGE_MODEL", a_default), int(os.getenv("DUAL_LOBE_A_MERGE_MAX_TOKENS", "12000")), "A"
    if role == "B_VERIFY":
        return os.getenv("DUAL_LOBE_B_VERIFY_MODEL", b_default), int(os.getenv("DUAL_LOBE_B_VERIFY_MAX_TOKENS", "6000")), "B"
    if role == "B_WORKER":
        return os.getenv("DUAL_LOBE_B_WORKER_MODEL", b_default), int(os.getenv("DUAL_LOBE_B_WORKER_MAX_TOKENS", "6000")), "B"
    return splitter_default, int(os.getenv("DUAL_LOBE_SPLITTER_MAX_TOKENS", "6000")), "SPLITTER"


def primary_spec(role: str) -> ProviderSpec:
    role = role.upper()
    model, max_tokens, key_role = _role_defaults(role)
    api_key = os.getenv(f"DUAL_LOBE_{role}_API_KEY") or os.getenv(f"DUAL_LOBE_{key_role}_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv(f"DUAL_LOBE_{role}_BASE_URL") or os.getenv(f"DUAL_LOBE_{key_role}_BASE_URL") or os.getenv("OPENAI_API_BASE")
    tier = (os.getenv(f"DUAL_LOBE_{role}_TIER") or os.getenv(f"DUAL_LOBE_{key_role}_TIER") or os.getenv("DUAL_LOBE_PROVIDER_TIER", "auto")).lower()
    rpm = _opt_int(os.getenv(f"DUAL_LOBE_{role}_RPM") or os.getenv(f"DUAL_LOBE_{key_role}_RPM"))
    tpm = _opt_int(os.getenv(f"DUAL_LOBE_{role}_TPM") or os.getenv(f"DUAL_LOBE_{key_role}_TPM"))
    return ProviderSpec(model=model, max_tokens=max_tokens, api_key=api_key, base_url=base_url, tier=tier, rpm=rpm, tpm=tpm, label=f"{role}:primary")


def _opt_int(v):
    try:
        return int(v) if v not in (None, "") else None
    except Exception:
        return None


def _fallback_json(role: str) -> list[dict]:
    raw = os.getenv(f"DUAL_LOBE_{role.upper()}_FALLBACKS") or os.getenv("DUAL_LOBE_FALLBACKS") or "[]"
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def resolve_role_specs(role: str) -> list[ProviderSpec]:
    role = role.upper()
    primary = primary_spec(role)
    out = [primary]
    for i, row in enumerate(_fallback_json(role), 1):
        if not isinstance(row, dict) or not row.get("model"):
            continue
        key = row.get("api_key")
        if not key and row.get("api_key_env"):
            key = os.getenv(str(row["api_key_env"]))
        out.append(ProviderSpec(
            model=str(row["model"]),
            max_tokens=int(row.get("max_tokens") or primary.max_tokens),
            api_key=key or primary.api_key,
            base_url=row.get("base_url") or primary.base_url,
            tier=str(row.get("tier") or "auto").lower(),
            rpm=_opt_int(row.get("rpm")),
            tpm=_opt_int(row.get("tpm")),
            label=str(row.get("label") or f"{role}:fallback:{i}"),
        ))

    if os.getenv("DUAL_LOBE_CROSS_ROLE_FAILOVER", "true").lower() in {"1", "true", "yes", "on"}:
        for other in ["A", "B_VERIFY", "B_WORKER", "SPLITTER"]:
            if other == role or (role == "A_MERGE" and other == "A"):
                continue
            alt = primary_spec(other)
            alt = replace(alt, max_tokens=primary.max_tokens, label=f"{role}:cross:{other}")
            out.append(alt)

    dedup, seen = [], set()
    for s in out:
        k = (s.model, s.base_url, s.api_key)
        if k not in seen:
            dedup.append(s); seen.add(k)
    return dedup


def make_llm(role: str, spec: ProviderSpec | None = None) -> LLM:
    spec = spec or primary_spec(role)
    kwargs = {"model": spec.model, "max_tokens": spec.max_tokens}
    if spec.api_key:
        kwargs["api_key"] = spec.api_key
    if spec.base_url:
        kwargs["base_url"] = spec.base_url
    return LLM(**kwargs)
