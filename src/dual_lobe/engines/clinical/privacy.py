from __future__ import annotations

import gc
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class PrivacyDecision(str, Enum):
    ALLOW = "allow"
    SANITIZE = "sanitize"
    BLOCK = "block"


class PrivacyAssurance(str, Enum):
    PROXY_MINIMIZED = "proxy_minimized"
    PROVIDER_ATTESTED = "provider_attested"
    LOCAL_ISOLATED = "local_isolated"
    UNVERIFIED_REMOTE = "unverified_remote"


_DIRECT_KEYS = {
    "name", "full_name", "patient_name", "first_name", "last_name",
    "address", "street_address", "email", "phone", "telephone",
    "ssn", "social_security_number", "mrn", "medical_record_number",
    "account_number", "member_id", "insurance_id", "license_number",
    "device_id", "ip_address", "url", "biometric_id", "photo",
    "date_of_birth", "dob",
}

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("phone", re.compile(r"(?<!\d)(?:\+?\d[\d .()\-]{7,}\d)(?!\d)")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
)


@dataclass(frozen=True)
class ProviderPrivacyPolicy:
    provider_name: str = "unspecified"
    allow_identifiable_phi: bool = False
    no_training_attested: bool = False
    zero_retention_attested: bool = False
    business_associate_or_equivalent: bool = False
    local_inference: bool = False

    @property
    def assurance(self) -> PrivacyAssurance:
        if self.local_inference:
            return PrivacyAssurance.LOCAL_ISOLATED
        if (
            self.no_training_attested
            and self.zero_retention_attested
            and self.business_associate_or_equivalent
        ):
            return PrivacyAssurance.PROVIDER_ATTESTED
        return PrivacyAssurance.UNVERIFIED_REMOTE


@dataclass(frozen=True)
class PrivacyReceipt:
    decision: PrivacyDecision
    assurance: PrivacyAssurance
    direct_identifier_types: tuple[str, ...]
    token_count: int
    raw_phi_forwarded: bool
    persisted_raw_locally: bool
    sanitized_query_fingerprint: str
    sanitized_context_fingerprint: str
    provider_name: str
    provider_zero_retention_attested: bool
    provider_no_training_attested: bool
    provider_business_associate_or_equivalent: bool
    encrypted_vault_used: bool
    vault_key_destroyed: bool
    local_discard_status: str
    limitations: tuple[str, ...] = ()


class EphemeralTokenVault:
    """Run-scoped local token vault.

    Raw identifier values are encrypted individually with AES-256-GCM under a
    random per-run key. The key never leaves this object. Models see only opaque
    random tokens. Destroying the run key makes remaining ciphertext unusable,
    subject to normal process/runtime memory limitations.
    """

    def __init__(self) -> None:
        self._key = bytearray(AESGCM.generate_key(bit_length=256))
        self._ciphertext: dict[str, tuple[bytes, bytes, str]] = {}
        self._raw_to_token: dict[tuple[str, str], str] = {}
        self._destroyed = False

    @property
    def destroyed(self) -> bool:
        return self._destroyed

    @property
    def token_count(self) -> int:
        return len(self._ciphertext)

    def _aes(self) -> AESGCM:
        if self._destroyed:
            raise RuntimeError("token vault key has been destroyed")
        return AESGCM(bytes(self._key))

    @staticmethod
    def _new_token(label: str) -> str:
        return f"<PHI:{label.upper()}:{secrets.token_hex(6).upper()}>"

    def tokenize(self, raw_value: str, *, label: str) -> str:
        raw = str(raw_value)
        lookup = (label, raw)
        existing = self._raw_to_token.get(lookup)
        if existing:
            return existing
        token = self._new_token(label)
        nonce = secrets.token_bytes(12)
        aad = token.encode("utf-8")
        encrypted = self._aes().encrypt(nonce, raw.encode("utf-8"), aad)
        self._ciphertext[token] = (nonce, encrypted, label)
        self._raw_to_token[lookup] = token
        return token

    def resolve(self, token: str) -> str:
        if token not in self._ciphertext:
            raise KeyError(token)
        nonce, encrypted, _label = self._ciphertext[token]
        raw = self._aes().decrypt(nonce, encrypted, token.encode("utf-8"))
        return raw.decode("utf-8")

    def replace_known(self, text: str) -> tuple[str, tuple[str, ...]]:
        """Replace identities already learned from structured local context."""
        out = str(text)
        labels: set[str] = set()
        for (label, raw), token in sorted(
            self._raw_to_token.items(),
            key=lambda item: len(item[0][1]),
            reverse=True,
        ):
            if raw and raw in out:
                out = out.replace(raw, token)
                labels.add(label)
        return out, tuple(sorted(labels))

    def rehydrate_text(self, text: str) -> str:
        out = str(text)
        for token in sorted(self._ciphertext, key=len, reverse=True):
            if token in out:
                out = out.replace(token, self.resolve(token))
        return out

    def encrypted_snapshot(self) -> dict[str, dict[str, str]]:
        """Audit/debug representation containing ciphertext only, never raw values."""
        return {
            token: {
                "nonce_sha256": hashlib.sha256(nonce).hexdigest(),
                "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
                "label": label,
            }
            for token, (nonce, ciphertext, label) in self._ciphertext.items()
        }

    def destroy_key(self) -> None:
        if self._destroyed:
            return
        for i in range(len(self._key)):
            self._key[i] = 0
        self._key.clear()
        self._raw_to_token.clear()
        self._destroyed = True
        gc.collect()


class PrivacyGuard:
    """Deterministic pre-inference privacy membrane with local encrypted vault."""

    def __init__(
        self,
        provider_policy: ProviderPrivacyPolicy | None = None,
        *,
        audit_key: bytes | None = None,
    ) -> None:
        self.provider_policy = provider_policy or ProviderPrivacyPolicy()
        configured = os.getenv("DUAL_LOBE_PRIVACY_AUDIT_KEY")
        self._audit_key = audit_key or (
            configured.encode("utf-8") if configured else secrets.token_bytes(32)
        )

    def new_vault(self) -> EphemeralTokenVault:
        return EphemeralTokenVault()

    def _fingerprint(self, text: str) -> str:
        return hmac.new(
            self._audit_key,
            text.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _sanitize_text(
        self,
        text: str,
        *,
        vault: EphemeralTokenVault,
        found: set[str],
    ) -> str:
        out, known_labels = vault.replace_known(str(text))
        found.update(known_labels)
        for label, pattern in _PATTERNS:
            def repl(match: re.Match[str], label=label) -> str:
                found.add(label)
                return vault.tokenize(match.group(0), label=label)
            out = pattern.sub(repl, out)
        return out

    def _sanitize_object(
        self,
        value: Any,
        *,
        vault: EphemeralTokenVault,
        found: set[str],
    ) -> Any:
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                normalized = str(key).strip().lower()
                if normalized in _DIRECT_KEYS and item not in (None, "", [], {}):
                    found.add(normalized)
                    if isinstance(item, (dict, list)):
                        raw = json.dumps(item, ensure_ascii=False, sort_keys=True)
                    else:
                        raw = str(item)
                    out[key] = vault.tokenize(raw, label=normalized)
                else:
                    out[key] = self._sanitize_object(item, vault=vault, found=found)
            return out
        if isinstance(value, list):
            return [self._sanitize_object(x, vault=vault, found=found) for x in value]
        if isinstance(value, str):
            return self._sanitize_text(value, vault=vault, found=found)
        return value

    def sanitize(
        self,
        text: str,
        *,
        vault: EphemeralTokenVault,
    ) -> tuple[str, tuple[str, ...]]:
        found: set[str] = set()
        try:
            parsed = json.loads(text)
        except Exception:
            sanitized = self._sanitize_text(text, vault=vault, found=found)
        else:
            sanitized_obj = self._sanitize_object(parsed, vault=vault, found=found)
            sanitized = json.dumps(sanitized_obj, ensure_ascii=False, sort_keys=True)
        return sanitized, tuple(sorted(found))

    def prepare(
        self,
        *,
        query: str,
        patient_context: str,
        vault: EphemeralTokenVault,
    ) -> tuple[str, str, PrivacyReceipt]:
        # Learn structured patient identifiers locally first. The query and
        # later trace sanitization can then reuse the same opaque tokens.
        sanitized_context, c_types = self.sanitize(patient_context, vault=vault)
        sanitized_query, q_types = self.sanitize(query, vault=vault)
        direct_types = tuple(sorted(set(q_types) | set(c_types)))

        decision = PrivacyDecision.SANITIZE if direct_types else PrivacyDecision.ALLOW

        limitations = [
            "Raw identifiers are not intentionally persisted by the guarded clinical runtime.",
            "Run-key overwrite is best-effort process-memory destruction, not a claim of physical memory zeroization.",
        ]
        if self.provider_policy.assurance is PrivacyAssurance.UNVERIFIED_REMOTE:
            limitations.append(
                "Remote-provider deletion/training behavior is not independently verified by the proxy."
            )

        receipt = PrivacyReceipt(
            decision=decision,
            assurance=self.provider_policy.assurance,
            direct_identifier_types=direct_types,
            token_count=vault.token_count,
            raw_phi_forwarded=False,
            persisted_raw_locally=False,
            sanitized_query_fingerprint=self._fingerprint(sanitized_query),
            sanitized_context_fingerprint=self._fingerprint(sanitized_context),
            provider_name=self.provider_policy.provider_name,
            provider_zero_retention_attested=self.provider_policy.zero_retention_attested,
            provider_no_training_attested=self.provider_policy.no_training_attested,
            provider_business_associate_or_equivalent=self.provider_policy.business_associate_or_equivalent,
            encrypted_vault_used=True,
            vault_key_destroyed=False,
            local_discard_status="pending_run_completion",
            limitations=tuple(limitations),
        )
        return sanitized_query, sanitized_context, receipt

    def sanitize_trace(
        self,
        trace_text: str,
        *,
        vault: EphemeralTokenVault,
    ) -> tuple[str, tuple[str, ...]]:
        return self.sanitize(trace_text, vault=vault)

    @staticmethod
    def finalized_receipt(receipt: PrivacyReceipt, vault: EphemeralTokenVault) -> PrivacyReceipt:
        return PrivacyReceipt(
            **{
                **receipt.__dict__,
                "token_count": vault.token_count,
                "vault_key_destroyed": vault.destroyed,
                "local_discard_status": (
                    "run_key_destroyed_best_effort"
                    if vault.destroyed
                    else "run_key_not_destroyed"
                ),
            }
        )


class EphemeralClinicalMemory:
    """No-op patient memory used by default in clinical runs.

    It satisfies the parent runtime's memory interface without writing patient
    or run content to disk. Long-lived non-patient knowledge belongs in the
    frozen evidence corpus, not patient memory.
    """

    path = type("_EphemeralPath", (), {"name": "ephemeral", "stem": "ephemeral", "suffix": ""})()

    def record(self, text: str, pinned: bool = False) -> None:
        return None

    def record_split_experience(self, text: str) -> None:
        return None

    def search(self, query: str, limit: int = 4, *, include_split_experience: bool = True) -> list[str]:
        return []

    def split_experience_slice(self, task: str, limit: int = 5, max_chars: int = 5000) -> str:
        return ""

    def auto_slice(self, task: str, max_chars: int = 10_000) -> str:
        return ""
