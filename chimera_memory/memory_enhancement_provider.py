"""Provider policy helpers for memory-enhancement sidecar calls.

This module does not perform network or model calls. It resolves safe provider
plans, budget limits, and invocation payloads for a future sidecar runner.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .memory_enhancement import ENHANCEMENT_SCHEMA_VERSION

PROVIDER_IDS = {"openai", "anthropic", "ollama", "dry_run"}
NETWORK_PROVIDERS = {"openai", "anthropic"}
LOCAL_PROVIDERS = {"ollama", "dry_run"}
DEFAULT_PROVIDER_ORDER = ("openai", "anthropic", "ollama", "dry_run")

PROVIDER_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "haiku-4.5",
    "ollama": "gemma2:2b",
    "dry_run": "deterministic-local",
}

FAILURE_CATEGORIES = {
    "auth_error",
    "content_filter",
    "model_unavailable",
    "parse_error",
    "quota_exceeded",
    "rate_limit",
    "timeout",
    "unknown_error",
}

_CREDENTIAL_REF_RE = re.compile(r"^(?:oauth|secret|env):[A-Za-z_][A-Za-z0-9_.:\\-]{0,119}$")


@dataclass(frozen=True)
class EnhancementBudget:
    """Hard limits for one memory-enhancement sidecar call."""

    max_input_tokens: int = 500
    max_input_chars: int = 2_000
    max_output_tokens: int = 200
    max_jobs_per_run: int = 10
    per_minute_call_cap: int = 30
    daily_soft_call_cap: int = 5_000
    monthly_hard_call_cap: int = 100_000
    timeout_seconds: int = 30


@dataclass(frozen=True)
class EnhancementProviderCandidate:
    """Safe provider candidate metadata.

    `credential_ref` is a reference name only. It must never contain raw token
    material.
    """

    provider_id: str
    model: str
    available: bool
    reason: str = ""
    credential_ref: str = ""
    endpoint: str = ""
    uses_user_oauth: bool = False
    requires_network: bool = False


@dataclass(frozen=True)
class EnhancementProviderPlan:
    """Resolved provider order plus selected candidate."""

    candidates: tuple[EnhancementProviderCandidate, ...]
    selected: EnhancementProviderCandidate
    budget: EnhancementBudget


def _env_bool(env: Mapping[str, str], key: str, *, default: bool = False) -> bool:
    value = str(env.get(key, "")).strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _env_int(env: Mapping[str, str], key: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(env.get(key, "")).strip())
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _clean_provider_id(value: object) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "dryrun": "dry_run",
        "dry": "dry_run",
        "local": "ollama",
        "anthropic_haiku": "anthropic",
        "openai_mini": "openai",
    }
    return aliases.get(text, text)


def parse_provider_order(raw: str | None) -> tuple[str, ...]:
    """Parse provider order and drop unknown or duplicate entries."""
    order: list[str] = []
    for item in str(raw or "").split(","):
        provider_id = _clean_provider_id(item)
        if provider_id in PROVIDER_IDS and provider_id not in order:
            order.append(provider_id)
    return tuple(order or DEFAULT_PROVIDER_ORDER)


def _credential_ref(env: Mapping[str, str], provider_id: str) -> tuple[str, str]:
    key = f"CHIMERA_MEMORY_ENHANCEMENT_{provider_id.upper()}_CREDENTIAL_REF"
    value = str(env.get(key, "")).strip()
    if not value:
        return "", "credential_missing"
    if not _CREDENTIAL_REF_RE.match(value):
        return "", "invalid_credential_ref"
    return value, ""


def load_enhancement_budget(env: Mapping[str, str]) -> EnhancementBudget:
    """Load safe sidecar budget caps from environment."""
    max_input_tokens = _env_int(
        env,
        "CHIMERA_MEMORY_ENHANCEMENT_MAX_INPUT_TOKENS",
        default=500,
        minimum=100,
        maximum=50_000,
    )
    max_input_chars = _env_int(
        env,
        "CHIMERA_MEMORY_ENHANCEMENT_MAX_INPUT_CHARS",
        default=max_input_tokens * 4,
        minimum=400,
        maximum=200_000,
    )
    return EnhancementBudget(
        max_input_tokens=max_input_tokens,
        max_input_chars=max_input_chars,
        max_output_tokens=_env_int(
            env,
            "CHIMERA_MEMORY_ENHANCEMENT_MAX_OUTPUT_TOKENS",
            default=200,
            minimum=64,
            maximum=8_000,
        ),
        max_jobs_per_run=_env_int(
            env,
            "CHIMERA_MEMORY_ENHANCEMENT_MAX_JOBS_PER_RUN",
            default=10,
            minimum=1,
            maximum=500,
        ),
        per_minute_call_cap=_env_int(
            env,
            "CHIMERA_MEMORY_ENHANCEMENT_PER_MINUTE_CALL_CAP",
            default=30,
            minimum=1,
            maximum=1_000,
        ),
        daily_soft_call_cap=_env_int(
            env,
            "CHIMERA_MEMORY_ENHANCEMENT_DAILY_SOFT_CALL_CAP",
            default=5_000,
            minimum=1,
            maximum=1_000_000,
        ),
        monthly_hard_call_cap=_env_int(
            env,
            "CHIMERA_MEMORY_ENHANCEMENT_MONTHLY_HARD_CALL_CAP",
            default=100_000,
            minimum=1,
            maximum=10_000_000,
        ),
        timeout_seconds=_env_int(
            env,
            "CHIMERA_MEMORY_ENHANCEMENT_TIMEOUT_SECONDS",
            default=30,
            minimum=1,
            maximum=300,
        ),
    )


def _provider_candidate(provider_id: str, env: Mapping[str, str]) -> EnhancementProviderCandidate:
    model_key = f"CHIMERA_MEMORY_ENHANCEMENT_{provider_id.upper()}_MODEL"
    model = str(env.get(model_key, "")).strip() or PROVIDER_DEFAULT_MODELS[provider_id]

    if provider_id == "dry_run":
        return EnhancementProviderCandidate(
            provider_id=provider_id,
            model=model,
            available=True,
            reason="available",
            requires_network=False,
            uses_user_oauth=False,
        )

    if provider_id == "ollama":
        endpoint = str(
            env.get("CHIMERA_MEMORY_ENHANCEMENT_OLLAMA_ENDPOINT", "http://127.0.0.1:11434")
        ).strip()
        enabled = _env_bool(
            env,
            "CHIMERA_MEMORY_ENHANCEMENT_ENABLE_LOCAL_MODEL",
            default=bool(env.get("CHIMERA_MEMORY_ENHANCEMENT_OLLAMA_ENDPOINT")),
        )
        return EnhancementProviderCandidate(
            provider_id=provider_id,
            model=model,
            available=enabled,
            reason="available" if enabled else "local_model_disabled",
            endpoint=endpoint,
            requires_network=False,
            uses_user_oauth=False,
        )

    credential_ref, credential_reason = _credential_ref(env, provider_id)
    available = bool(credential_ref)
    reason = "available"
    if credential_reason:
        reason = credential_reason
    return EnhancementProviderCandidate(
        provider_id=provider_id,
        model=model,
        available=available,
        reason=reason,
        credential_ref=credential_ref,
        requires_network=True,
        uses_user_oauth=True,
    )


def resolve_enhancement_provider_plan(env: Mapping[str, str]) -> EnhancementProviderPlan:
    """Resolve provider candidates and select the first available candidate."""
    order = parse_provider_order(env.get("CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER"))
    candidates = tuple(_provider_candidate(provider_id, env) for provider_id in order)
    selected = next((candidate for candidate in candidates if candidate.available), None)
    if selected is None:
        selected = EnhancementProviderCandidate(
            provider_id="dry_run",
            model=PROVIDER_DEFAULT_MODELS["dry_run"],
            available=True,
            reason="fallback",
        )
    return EnhancementProviderPlan(
        candidates=candidates,
        selected=selected,
        budget=load_enhancement_budget(env),
    )


def clamp_request_to_budget(
    request_payload: Mapping[str, Any],
    budget: EnhancementBudget,
) -> dict[str, Any]:
    """Return a request copy clamped to budget limits."""
    request = dict(request_payload)
    wrapped = str(request.get("wrapped_content") or "")
    truncated = len(wrapped) > budget.max_input_chars
    if truncated:
        request["wrapped_content"] = wrapped[: budget.max_input_chars]
    policy = dict(request.get("policy") or {})
    policy.update(
        {
            "max_input_chars": budget.max_input_chars,
            "max_output_tokens": budget.max_output_tokens,
            "truncated_by_budget": truncated,
        }
    )
    request["policy"] = policy
    return request


def build_enhancement_invocation(
    request_payload: Mapping[str, Any],
    plan: EnhancementProviderPlan,
) -> dict[str, Any]:
    """Build a safe invocation envelope for a future sidecar runner."""
    selected = plan.selected
    return {
        "schema_version": ENHANCEMENT_SCHEMA_VERSION,
        "provider": {
            "provider_id": selected.provider_id,
            "model": selected.model,
            "credential_ref": selected.credential_ref,
            "endpoint": selected.endpoint,
            "uses_user_oauth": selected.uses_user_oauth,
            "requires_network": selected.requires_network,
        },
        "budget": {
            "max_input_tokens": plan.budget.max_input_tokens,
            "max_input_chars": plan.budget.max_input_chars,
            "max_output_tokens": plan.budget.max_output_tokens,
            "max_jobs_per_run": plan.budget.max_jobs_per_run,
            "per_minute_call_cap": plan.budget.per_minute_call_cap,
            "daily_soft_call_cap": plan.budget.daily_soft_call_cap,
            "monthly_hard_call_cap": plan.budget.monthly_hard_call_cap,
            "timeout_seconds": plan.budget.timeout_seconds,
        },
        "request": clamp_request_to_budget(request_payload, plan.budget),
    }


def safe_provider_receipt(plan: EnhancementProviderPlan) -> dict[str, Any]:
    """Return provider-resolution diagnostics without credential material."""
    return {
        "selected_provider": plan.selected.provider_id,
        "selected_model": plan.selected.model,
        "budget": {
            "max_input_tokens": plan.budget.max_input_tokens,
            "max_input_chars": plan.budget.max_input_chars,
            "max_output_tokens": plan.budget.max_output_tokens,
            "max_jobs_per_run": plan.budget.max_jobs_per_run,
            "per_minute_call_cap": plan.budget.per_minute_call_cap,
            "daily_soft_call_cap": plan.budget.daily_soft_call_cap,
            "monthly_hard_call_cap": plan.budget.monthly_hard_call_cap,
            "timeout_seconds": plan.budget.timeout_seconds,
        },
        "candidates": [
            {
                "provider_id": candidate.provider_id,
                "model": candidate.model,
                "available": candidate.available,
                "reason": candidate.reason,
                "credential_ref_present": bool(candidate.credential_ref),
                "requires_network": candidate.requires_network,
                "uses_user_oauth": candidate.uses_user_oauth,
            }
            for candidate in plan.candidates
        ],
    }


def classify_enhancement_failure(message: object) -> str:
    """Classify provider failure text into bounded, non-secret categories."""
    text = str(message or "").lower()
    if not text:
        return "unknown_error"
    if "credential" in text or "unauthorized" in text or "forbidden" in text or "auth" in text:
        return "auth_error"
    if "deprecated" in text or "unavailable" in text or "not found" in text or "503" in text:
        return "model_unavailable"
    if "rate" in text or "429" in text:
        return "rate_limit"
    if "quota" in text or "cap" in text or "budget" in text:
        return "quota_exceeded"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "json" in text or "schema" in text or "invalid response" in text:
        return "parse_error"
    if "filter" in text or "policy" in text or "blocked" in text or "refused" in text:
        return "content_filter"
    return "unknown_error"
