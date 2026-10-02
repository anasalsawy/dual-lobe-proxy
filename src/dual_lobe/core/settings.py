"""Central configuration for the dual-lobe inference proxy.

Reads from environment or a .env file. ``DATABASE_URL`` is the admin/owner
connection (used by migrations, the auth key lookup and the B worker); the
tenant-scoped ``RLS_DATABASE_URL`` is used for request data-plane access under
Postgres Row-Level Security.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

RolloutStage = Literal[
    "observation", "context", "integrity-observe", "integrity-intervene", "enforcement"
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = (
        "postgresql+asyncpg://dual_lobe:dual_lobe_dev@localhost:5432/dual_lobe"
    )
    rls_database_url: str | None = None

    host: str = Field(default="0.0.0.0", validation_alias="DUAL_LOBE_HOST")
    port: int = Field(default=8801, validation_alias="DUAL_LOBE_PORT")
    log_level: str = Field(default="INFO", validation_alias="DUAL_LOBE_LOG_LEVEL")
    otel_enabled: bool = Field(default=False, validation_alias="DUAL_LOBE_OTEL_ENABLED")

    bootstrap_keys: str = Field(default="", validation_alias="DUAL_LOBE_BOOTSTRAP_KEYS")
    seed_tenant_slug: str = Field(default="default", validation_alias="DUAL_LOBE_SEED_TENANT_SLUG")
    seed_tenant_name: str = Field(default="Default Tenant", validation_alias="DUAL_LOBE_SEED_TENANT_NAME")

    a_model: str = Field(default="zai-org/GLM-5.3-Flash", validation_alias="DUAL_LOBE_A_MODEL")
    a_base_url: str = Field(default="https://api.featherless.ai/v1", validation_alias="DUAL_LOBE_A_BASE_URL")
    a_api_key: str = Field(default="", validation_alias="DUAL_LOBE_A_API_KEY")
    a_dialect: str = Field(default="chat_completions", validation_alias="DUAL_LOBE_A_DIALECT")
    b_model: str | None = Field(default=None, validation_alias="DUAL_LOBE_B_MODEL")
    b_base_url: str | None = Field(default=None, validation_alias="DUAL_LOBE_B_BASE_URL")
    b_api_key: str | None = Field(default=None, validation_alias="DUAL_LOBE_B_API_KEY")
    b_dialect: str = Field(default="chat_completions", validation_alias="DUAL_LOBE_B_DIALECT")

    # The secure public model ID selects this local B privacy gate. It is not a
    # service-wide engine mode; both public variants share the same runtime.
    secure_b_model: str | None = Field(default=None, validation_alias="DUAL_LOBE_SECURE_B_MODEL")
    secure_b_base_url: str | None = Field(default=None, validation_alias="DUAL_LOBE_SECURE_B_BASE_URL")
    secure_b_api_key: str | None = Field(default=None, validation_alias="DUAL_LOBE_SECURE_B_API_KEY")
    testing_mode: bool = Field(default=False, validation_alias="DUAL_LOBE_TESTING_MODE")
    secure_b_local_only: bool = Field(default=True, validation_alias="DUAL_LOBE_SECURE_B_LOCAL_ONLY")

    rollout_stage: RolloutStage = Field(default="context", validation_alias="DUAL_LOBE_ROLLOUT_STAGE")
    pulse_every: int = Field(default=1, ge=1, validation_alias="DUAL_LOBE_PULSE_EVERY")
    b_enabled: bool = Field(default=True, validation_alias="DUAL_LOBE_B_ENABLED")
    observation_reminder: bool = Field(default=True, validation_alias="DUAL_LOBE_OBSERVATION_REMINDER")
    monitoring_role: Literal["system", "developer"] = Field(default="system", validation_alias="DUAL_LOBE_MONITORING_ROLE")
    context_memory_enabled: bool = Field(default=True, validation_alias="DUAL_LOBE_CONTEXT_MEMORY_ENABLED")
    claim_checks_enabled: bool = Field(default=True, validation_alias="DUAL_LOBE_CLAIM_CHECKS_ENABLED")
    deception_meter_enabled: bool = Field(default=True, validation_alias="DUAL_LOBE_DECEPTION_METER_ENABLED")
    context_memory_ttl_seconds: float = Field(default=86400, gt=0, validation_alias="DUAL_LOBE_CONTEXT_MEMORY_TTL_SECONDS")
    max_memory_chars: int = Field(default=1600, ge=600, le=6000, validation_alias="DUAL_LOBE_MAX_MEMORY_CHARS")
    b_cooldown_seconds: float = Field(default=0, ge=0, validation_alias="DUAL_LOBE_B_COOLDOWN_SECONDS")
    b_state_ttl_seconds: float = Field(default=180, gt=0, validation_alias="DUAL_LOBE_B_STATE_TTL_SECONDS")
    b_state_read_timeout: float = Field(default=0.025, gt=0, le=0.25, validation_alias="DUAL_LOBE_B_STATE_READ_TIMEOUT")
    b_max_output_tokens: int = Field(default=1400, ge=128, le=4000, validation_alias="DUAL_LOBE_B_MAX_OUTPUT_TOKENS")
    b_rpm_limit: int = Field(default=20, ge=1, validation_alias="DUAL_LOBE_B_RPM_LIMIT")
    max_injection_chars: int = Field(default=1200, ge=300, le=4000, validation_alias="DUAL_LOBE_MAX_INJECTION_CHARS")
    max_shadow_input_chars: int = Field(default=18000, ge=4000, le=60000, validation_alias="DUAL_LOBE_MAX_SHADOW_INPUT_CHARS")

    # Gateway-side evidence sensing: B stays inference-only; code executes bounded
    # tools on its behalf, strictly off the request critical path (worker side).
    evidence_sensing_enabled: bool = Field(default=True, validation_alias="DUAL_LOBE_EVIDENCE_SENSING_ENABLED")
    b_tool_max_rounds: int = Field(default=2, ge=1, le=4, validation_alias="DUAL_LOBE_B_TOOL_MAX_ROUNDS")
    b_evidence_ops_per_run: int = Field(default=3, ge=1, le=20, validation_alias="DUAL_LOBE_B_EVIDENCE_OPS_PER_RUN")
    fetch_timeout: float = Field(default=10.0, gt=0, le=60, validation_alias="DUAL_LOBE_FETCH_TIMEOUT")
    fetch_max_bytes: int = Field(default=32000, ge=2000, le=200000, validation_alias="DUAL_LOBE_FETCH_MAX_BYTES")
    artifact_root: str = Field(default="", validation_alias="DUAL_LOBE_ARTIFACT_ROOT")
    observer_memory_space: str = Field(default="observer", pattern=r"^([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})?$",
                                       validation_alias="DUAL_LOBE_OBSERVER_MEMORY_SPACE")

    rpm_limit: int = Field(default=600, validation_alias="DUAL_LOBE_RPM_LIMIT")
    tpm_limit: int = Field(default=120000, validation_alias="DUAL_LOBE_TPM_LIMIT")

    a_retries: int = Field(default=1, ge=1, le=3, validation_alias="DUAL_LOBE_A_RETRIES")
    gateway_max_request_bytes: int = Field(default=4 * 1024 * 1024, validation_alias="DUAL_LOBE_MAX_REQUEST_BYTES")
    a_timeout: float = Field(default=180.0, gt=0, validation_alias="DUAL_LOBE_A_TIMEOUT")
    b_timeout: float = Field(default=20.0, gt=0, le=60, validation_alias="DUAL_LOBE_B_TIMEOUT")

    # Upstream pacing: detect the connected provider, read its rate-limit
    # headers on every response, and throttle both lobes so the provider budget
    # is never exceeded (no avoidable429s).
    upstream_rate_enabled: bool = Field(default=True, validation_alias="DUAL_LOBE_UPSTREAM_RATE_ENABLED")
    upstream_rate_max_wait: float = Field(default=60.0, ge=0, le=600,
                                          validation_alias="DUAL_LOBE_UPSTREAM_RATE_MAX_WAIT")
    upstream_rate_overrides: str = Field(default="", validation_alias="DUAL_LOBE_UPSTREAM_RATE_OVERRIDES")

    shared_memory_enabled: bool = Field(default=True, validation_alias="DUAL_LOBE_SHARED_MEMORY_ENABLED")
    default_memory_id: str = Field(default="main", pattern=r"^([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})?$", validation_alias="DUAL_LOBE_DEFAULT_MEMORY_ID")
    shared_memory_max_chars: int = Field(default=10000, ge=6000, le=40000, validation_alias="DUAL_LOBE_SHARED_MEMORY_MAX_CHARS")
    shared_memory_timeout: float = Field(default=5, gt=0, le=30, validation_alias="DUAL_LOBE_SHARED_MEMORY_TIMEOUT")
    episodic_memory_enabled: bool = Field(default=True, validation_alias="DUAL_LOBE_EPISODIC_MEMORY")
    episodic_notes_max: int = Field(default=5, ge=1, le=10, validation_alias="DUAL_LOBE_EPISODIC_NOTES_MAX")

    worker_poll_seconds: float = Field(default=1.0, gt=0, validation_alias="DUAL_LOBE_WORKER_POLL_SECONDS")
    worker_max_concurrency: int = Field(default=2, ge=1, le=16, validation_alias="DUAL_LOBE_WORKER_MAX_CONCURRENCY")
    worker_lock_seconds: int = Field(default=90, validation_alias="DUAL_LOBE_WORKER_LOCK_SECONDS")

    # Multi-agent recipient routing: detect if a message is for THIS agent or another
    recipient_routing_enabled: bool = Field(default=False, validation_alias="DUAL_LOBE_RECIPIENT_ROUTING_ENABLED")
    agent_name: str = Field(default="agent", validation_alias="DUAL_LOBE_AGENT_NAME")
    recipient_routing_confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0, validation_alias="DUAL_LOBE_RECIPIENT_ROUTING_CONFIDENCE_THRESHOLD")

    # Routing mode: "off" (single agent), "flat" (group chat), or "hierarchy" (ranked roles)
    routing_mode: str = Field(default="off", validation_alias="DUAL_LOBE_ROUTING_MODE")
    hierarchy_roles: str = Field(default="", validation_alias="DUAL_LOBE_HIERARCHY_ROLES")

    # Gated mode: flip-back on YELLOW/RED (default off — meter shown in headers only)
    gated_flip_back: bool = Field(default=False, validation_alias="DUAL_LOBE_GATED_FLIP_BACK")

    # Gated mode: the same downstream B call also returns handoff material for
    # A's next call (unverified claims, tool-call review, next step, missing
    # facts). No extra B call, no extra latency hop.
    gated_b_handoff: bool = Field(default=True, validation_alias="DUAL_LOBE_GATED_B_HANDOFF")

    # Meter presentation: "markdown" (default, portable) or "html" (styled —
    # small text, level colour + tinted background, right-aligned). The HTML
    # form is a client-rendered fragment; flip only when the client renders HTML.
    meter_style: str = Field(default="markdown", validation_alias="DUAL_LOBE_METER_STYLE")
    # With meter_style=html: pin the meter as a bottom-right fixed badge
    # (position:fixed) instead of an inline right-aligned block.
    meter_fixed: bool = Field(default=False, validation_alias="DUAL_LOBE_METER_FIXED")

    # Proxy-owned server-side tools for A (gated path only): inline execution,
    # one same-turn continuation, never visible to the client.
    proxy_tools_enabled: bool = Field(default=True, validation_alias="DUAL_LOBE_PROXY_TOOLS")
    proxy_memory_search_cap: int = Field(default=2, ge=0, le=8,
                                         validation_alias="DUAL_LOBE_PROXY_MEMORY_SEARCH_CAP")
    proxy_delegate_cap: int = Field(default=1, ge=0, le=4,
                                    validation_alias="DUAL_LOBE_PROXY_DELEGATE_CAP")
    proxy_consult_cap: int = Field(default=1, ge=0, le=4,
                                   validation_alias="DUAL_LOBE_PROXY_CONSULT_CAP")

    # Implementation-auditor: static checks that catch UI/mock substitution, simulated
    # progress, missing backends, deleted integrations, and unsubstantiated completion.
    implementation_audit_enabled: bool = Field(default=True, validation_alias="DUAL_LOBE_IMPLEMENTATION_AUDIT_ENABLED")
    implementation_audit_workspace: str | None = Field(default=None, validation_alias="DUAL_LOBE_IMPLEMENTATION_AUDIT_WORKSPACE")
    implementation_audit_baseline_paths: list[str] | None = Field(default=None, validation_alias="DUAL_LOBE_IMPLEMENTATION_AUDIT_BASELINE_PATHS")

    @property
    def rls_url(self) -> str:
        if self.rls_database_url:
            return self.rls_database_url
        raise ValueError("RLS_DATABASE_URL is required; do not use the owner role for tenant requests")

    @property
    def resolved_b_model(self) -> str:
        return self.b_model or self.a_model

    @property
    def resolved_b_base_url(self) -> str:
        return self.b_base_url or self.a_base_url

    @property
    def resolved_b_api_key(self) -> str:
        return self.b_api_key if self.b_api_key is not None else self.a_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
