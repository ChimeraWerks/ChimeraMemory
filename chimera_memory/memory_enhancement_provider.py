"""Provider policy helpers for memory-enhancement sidecar calls.

This module does not perform model calls. It resolves safe provider plans,
budget limits, and invocation payloads for a future sidecar runner. Optional
catalog-backed defaults are delegated to memory_model_catalog.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hermes_error_classifier import FailoverReason, classify_api_error
from .memory_enhancement import ENHANCEMENT_SCHEMA_VERSION
from .memory_enhancement_google import GOOGLE_CLOUDCODE_MEMORY_DEFAULT_MODEL

PROVIDER_IDS = {
    "openai",
    "anthropic",
    "google",
    "openrouter",
    "ollama",
    "lmstudio",
    "openai_compatible",
    "dry_run",
}
NETWORK_PROVIDERS = {"openai", "anthropic", "google", "openrouter"}
LOCAL_PROVIDERS = {"ollama", "lmstudio", "openai_compatible", "dry_run"}
DEFAULT_PROVIDER_ORDER = ("openai", "anthropic", "google", "openrouter", "ollama", "lmstudio", "dry_run")

PROVIDER_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    "google": GOOGLE_CLOUDCODE_MEMORY_DEFAULT_MODEL,
    "openrouter": "openai/gpt-4o-mini",
    "ollama": "gemma2:2b",
    "lmstudio": "openai/gpt-oss-20b",
    "openai_compatible": "local-model",
    "dry_run": "deterministic-local",
}

FAILURE_CATEGORIES = {
    "auth_error",
    "content_filter",
    "context_overflow",
    "billing",
    "format_error",
    "image_too_large",
    "long_context_tier",
    "model_unavailable",
    "oauth_long_context_beta_forbidden",
    "overloaded",
    "payload_too_large",
    "parse_error",
    "provider_policy_blocked",
    "quota_exceeded",
    "rate_limit",
    "server_error",
    "thinking_signature",
    "timeout",
    "unknown_error",
}

_CREDENTIAL_REF_RE = re.compile(r"^(?:oauth|secret|env):[A-Za-z_][A-Za-z0-9_.:\\-]{0,119}$")


@dataclass(frozen=True)
class EnhancementBudget:
    """Hard limits for one memory-enhancement sidecar call."""

    max_input_tokens: int = 500
    max_input_chars: int = 2_000
    max_output_tokens: int = 1200
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
        "local_ai": "ollama",
        "local-ai": "ollama",
        "anthropic_haiku": "anthropic",
        "openai_mini": "openai",
        "gemini": "google",
        "google_gemini": "google",
        "google-gemini": "google",
        "openrouter": "openrouter",
        "openai-compatible": "openai_compatible",
        "openai_compatible": "openai_compatible",
        "custom_openai": "openai_compatible",
        "custom-openai": "openai_compatible",
        "lm_studio": "lmstudio",
        "lm-studio": "lmstudio",
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


def _provider_default_model(provider_id: str, env: Mapping[str, str]) -> str:
    if provider_id == "google":
        return PROVIDER_DEFAULT_MODELS[provider_id]
    if not _env_bool(env, "CHIMERA_MEMORY_ENHANCEMENT_USE_MODELS_DEV_CATALOG", default=False):
        return PROVIDER_DEFAULT_MODELS[provider_id]
    if provider_id not in {"openai", "anthropic", "google", "openrouter"}:
        return PROVIDER_DEFAULT_MODELS[provider_id]
    try:
        from .memory_model_catalog import default_memory_enhancement_model

        catalog_model = default_memory_enhancement_model(provider_id)
    except Exception:
        catalog_model = ""
    return catalog_model or PROVIDER_DEFAULT_MODELS[provider_id]


def _credential_ref(env: Mapping[str, str], provider_id: str) -> tuple[str, str]:
    key = f"CHIMERA_MEMORY_ENHANCEMENT_{provider_id.upper()}_CREDENTIAL_REF"
    value = str(env.get(key, "")).strip()
    if not value:
        active_value = _active_pooled_credential_ref(env, provider_id)
        if active_value:
            return active_value, ""
        return "", "credential_missing"
    if not _CREDENTIAL_REF_RE.match(value):
        return "", "invalid_credential_ref"
    return value, ""


def _active_pooled_credential_ref(env: Mapping[str, str], provider_id: str) -> str:
    if provider_id not in NETWORK_PROVIDERS:
        return ""
    try:
        from .memory_enhancement_oauth import MemoryEnhancementOAuthStore

        store = MemoryEnhancementOAuthStore(_oauth_store_path_from_env(env))
        credential = store.select_pooled(provider_id)
    except Exception:
        return ""
    ref = credential.ref.raw_ref
    return ref if _CREDENTIAL_REF_RE.match(ref) else ""


def _oauth_store_path_from_env(env: Mapping[str, str]) -> str | None:
    explicit = str(env.get("CHIMERA_MEMORY_OAUTH_STORE") or env.get("PERSONIFYAGENTS_MEMORY_OAUTH_STORE") or "").strip()
    if explicit:
        return explicit
    state_root = str(env.get("CHIMERA_MEMORY_STATE_ROOT") or env.get("PERSONIFYAGENTS_PWA_STATE_ROOT") or "").strip()
    if state_root:
        return str((Path(state_root).expanduser() / "auth.json").resolve())
    return None


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
            default=1200,
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
    model = str(env.get(model_key, "")).strip() or _provider_default_model(provider_id, env)

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

    if provider_id == "lmstudio":
        endpoint = str(
            env.get("CHIMERA_MEMORY_ENHANCEMENT_LMSTUDIO_ENDPOINT", "http://127.0.0.1:1234/v1")
        ).strip()
        enabled = _env_bool(
            env,
            "CHIMERA_MEMORY_ENHANCEMENT_ENABLE_LOCAL_MODEL",
            default=bool(env.get("CHIMERA_MEMORY_ENHANCEMENT_LMSTUDIO_ENDPOINT")),
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

    if provider_id == "openai_compatible":
        endpoint = str(env.get("CHIMERA_MEMORY_ENHANCEMENT_OPENAI_COMPATIBLE_ENDPOINT", "")).strip()
        explicit_model = str(env.get(model_key, "")).strip()
        enabled = _env_bool(
            env,
            "CHIMERA_MEMORY_ENHANCEMENT_ENABLE_LOCAL_MODEL",
            default=bool(endpoint and explicit_model),
        )
        reason = "available"
        if not endpoint:
            reason = "endpoint_missing"
            enabled = False
        elif not explicit_model:
            reason = "model_missing"
            enabled = False
        credential_ref, credential_reason = _credential_ref(env, provider_id)
        if credential_reason == "invalid_credential_ref":
            reason = credential_reason
            enabled = False
        return EnhancementProviderCandidate(
            provider_id=provider_id,
            model=explicit_model or model,
            available=enabled,
            reason=reason if not enabled else "available",
            credential_ref=credential_ref,
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
        uses_user_oauth=credential_ref.startswith("oauth:"),
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


_HERMES_REASON_TO_FAILURE_CATEGORY = {
    FailoverReason.auth: "auth_error",
    FailoverReason.auth_permanent: "auth_error",
    FailoverReason.billing: "billing",
    FailoverReason.context_overflow: "context_overflow",
    FailoverReason.format_error: "format_error",
    FailoverReason.image_too_large: "image_too_large",
    FailoverReason.long_context_tier: "long_context_tier",
    FailoverReason.llama_cpp_grammar_pattern: "format_error",
    FailoverReason.model_not_found: "model_unavailable",
    FailoverReason.oauth_long_context_beta_forbidden: "oauth_long_context_beta_forbidden",
    FailoverReason.overloaded: "overloaded",
    FailoverReason.payload_too_large: "payload_too_large",
    FailoverReason.provider_policy_blocked: "provider_policy_blocked",
    FailoverReason.rate_limit: "rate_limit",
    FailoverReason.server_error: "server_error",
    FailoverReason.thinking_signature: "thinking_signature",
    FailoverReason.timeout: "timeout",
    FailoverReason.unknown: "unknown_error",
}


def classify_enhancement_failure(
    message: object,
    *,
    provider: str = "",
    model: str = "",
    status_code: int | None = None,
    body: Mapping[str, Any] | None = None,
) -> str:
    """Classify provider failure text into bounded, non-secret categories."""
    if isinstance(message, Exception):
        error = message
    else:
        error = RuntimeError(str(message or ""))
    if status_code is not None:
        setattr(error, "status_code", int(status_code))
    if body is not None:
        setattr(error, "body", dict(body))

    classified = classify_api_error(error, provider=provider, model=model)
    category = _HERMES_REASON_TO_FAILURE_CATEGORY.get(classified.reason, "unknown_error")
    if category != "unknown_error":
        return category

    text = str(message or "").lower()
    if not text:
        return "unknown_error"
    if "credential" in text or "unauthorized" in text or "forbidden" in text or "auth" in text:
        return "auth_error"
    if "deprecated" in text or "unavailable" in text or "not available" in text or "not found" in text or "503" in text:
        return "model_unavailable"
    if (
        "rate limit" in text
        or "rate_limit" in text
        or "too many requests" in text
        or "throttled" in text
        or "429" in text
    ):
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
