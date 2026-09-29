from __future__ import annotations

import json
import os
from dataclasses import replace
from urllib.parse import urlparse
from crewai import LLM

from .provider_control import ProviderSpec


def _role_defaults(role: str) -> tuple[str, int, str]:
    role = role.upper()
    a_default = os.getenv("DUAL_LOBE_A_MODEL", "openrouter/nvidia/nemotron-3-super-120b-a12b:free")
    b_default = os.getenv("DUAL_LOBE_B_MODEL", a_default)
    if role == "A":
        return a_default, int(os.getenv("DUAL_LOBE_A_MAX_TOKENS", "8000")), "A"
    if role == "A_CHILD":
        return os.getenv("DUAL_LOBE_CHILD_MODEL", a_default), int(os.getenv("DUAL_LOBE_CHILD_MAX_TOKENS", "6000")), "A"
    if role == "A_MERGE":
        return os.getenv("DUAL_LOBE_A_MERGE_MODEL", a_default), int(os.getenv("DUAL_LOBE_A_MERGE_MAX_TOKENS", "12000")), "A"
    if role == "B_VERIFY":
        return os.getenv("DUAL_LOBE_B_VERIFY_MODEL", b_default), int(os.getenv("DUAL_LOBE_B_VERIFY_MAX_TOKENS", "6000")), "B"
    if role == "B_WORKER":
        return os.getenv("DUAL_LOBE_B_WORKER_MODEL", b_default), int(os.getenv("DUAL_LOBE_B_WORKER_MAX_TOKENS", "6000")), "B"
    raise ValueError(f"Unknown LLM role: {role}")


def primary_spec(role: str) -> ProviderSpec:
    role = role.upper()
    model, max_tokens, key_role = _role_defaults(role)
    api_key = os.getenv(f"DUAL_LOBE_{role}_API_KEY") or os.getenv(f"DUAL_LOBE_{key_role}_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv(f"DUAL_LOBE_{role}_BASE_URL") or os.getenv(f"DUAL_LOBE_{key_role}_BASE_URL") or os.getenv("OPENAI_API_BASE")
    tier = (os.getenv(f"DUAL_LOBE_{role}_TIER") or os.getenv(f"DUAL_LOBE_{key_role}_TIER") or os.getenv("DUAL_LOBE_PROVIDER_TIER", "auto")).lower()
    rpm = _opt_int(os.getenv(f"DUAL_LOBE_{role}_RPM") or os.getenv(f"DUAL_LOBE_{key_role}_RPM"))
    tpm = _opt_int(os.getenv(f"DUAL_LOBE_{role}_TPM") or os.getenv(f"DUAL_LOBE_{key_role}_TPM"))
    return ProviderSpec(model=model, max_tokens=max_tokens, api_key=api_key, base_url=base_url, tier=tier, rpm=rpm, tpm=tpm, label=f"{role}:primary")


def _provider_env_names(model: str, base_url: str | None) -> tuple[list[str], list[str]]:
    """Return provider-wide credential and endpoint variable names."""
    model_l = (model or "").lower()
    host = ""
    if base_url:
        try:
            host = (urlparse(base_url).hostname or "").lower()
        except Exception:
            pass
    identity = f"{model_l} {host}"
    providers = [
        (("grok", "xai", "x.ai"), ("GROK_API_KEY", "XAI_API_KEY"), ("GROK_BASE_URL", "XAI_BASE_URL")),
        (("groq", "groq.com"), ("GROQ_API_KEY",), ("GROQ_BASE_URL",)),
        (("openrouter", "openrouter.ai"), ("OPENROUTER_API_KEY",), ("OPENROUTER_BASE_URL",)),
        (("deepinfra", "deepinfra.com"), ("DEEPINFRA_API_KEY",), ("DEEPINFRA_BASE_URL",)),
        (("anthropic", "claude", "anthropic.com"), ("ANTHROPIC_API_KEY",), ("ANTHROPIC_BASE_URL",)),
        (("gemini", "google", "generativelanguage.googleapis.com"), ("GEMINI_API_KEY", "GOOGLE_API_KEY"), ("GEMINI_BASE_URL", "GOOGLE_API_BASE")),
        (("cerebras", "cerebras.ai"), ("CEREBRAS_API_KEY",), ("CEREBRAS_BASE_URL",)),
        (("together", "together.ai"), ("TOGETHER_API_KEY", "TOGETHERAI_API_KEY"), ("TOGETHER_BASE_URL",)),
        (("fireworks", "fireworks.ai"), ("FIREWORKS_API_KEY",), ("FIREWORKS_BASE_URL",)),
        (("mistral", "mistral.ai"), ("MISTRAL_API_KEY",), ("MISTRAL_BASE_URL",)),
        (("cohere", "cohere.ai"), ("COHERE_API_KEY",), ("COHERE_BASE_URL",)),
        (("sambanova", "sambanova.ai"), ("SAMBANOVA_API_KEY",), ("SAMBANOVA_BASE_URL",)),
        (("openai", "api.openai.com"), ("OPENAI_API_KEY",), ("OPENAI_API_BASE",)),
    ]
    for markers, key_names, base_names in providers:
        if any(marker in identity for marker in markers):
            return list(key_names), list(base_names)
    return ["OPENAI_API_KEY"], ["OPENAI_API_BASE"]


def _first_env(names: list[str]) -> str | None:
    return next((os.getenv(name) for name in names if os.getenv(name)), None)


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
        model = str(row["model"])
        key = row.get("api_key")
        if not key and row.get("api_key_env"):
            key = os.getenv(str(row["api_key_env"]))
        base_url = row.get("base_url")
        # A fallback lives at its own provider, never at the primary's endpoint.
        provider_keys, provider_bases = _provider_env_names(model, base_url)
        key = key or _first_env(provider_keys)
        base_url = base_url or _first_env(provider_bases)
        out.append(ProviderSpec(
            model=model,
            max_tokens=int(row.get("max_tokens") or primary.max_tokens),
            api_key=key,
            base_url=base_url,
            tier=str(row.get("tier") or "auto").lower(),
            rpm=_opt_int(row.get("rpm")),
            tpm=_opt_int(row.get("tpm")),
            label=str(row.get("label") or f"{role}:fallback:{i}"),
        ))

    if os.getenv("DUAL_LOBE_CROSS_ROLE_FAILOVER", "true").lower() in {"1", "true", "yes", "on"}:
        for other in ["A", "A_CHILD", "B_VERIFY"]:
            if other == role or (role == "A_MERGE" and other == "A"):
                continue
            alt = primary_spec(other)
            alt = replace(alt, max_tokens=primary.max_tokens, label=f"{role}:cross:{other}")
            out.append(alt)

    dedup, seen = [], set()
    for s in out:
        k = (s.model, s.base_url, s.api_key)
        if k not in seen:
            dedup.append(s)
            seen.add(k)
    return dedup


def make_llm(role: str, spec: ProviderSpec | None = None) -> LLM:
    spec = spec or primary_spec(role)
    kwargs = {"model": spec.model, "max_tokens": spec.max_tokens}
    if spec.api_key:
        kwargs["api_key"] = spec.api_key
    if spec.base_url:
        kwargs["base_url"] = spec.base_url
    return LLM(**kwargs)
