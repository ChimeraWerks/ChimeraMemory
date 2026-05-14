from chimera_memory.memory_enhancement import build_memory_enhancement_request
from chimera_memory.memory_enhancement_provider import (
    DEFAULT_PROVIDER_ORDER,
    build_enhancement_invocation,
    classify_enhancement_failure,
    parse_provider_order,
    resolve_enhancement_provider_plan,
    safe_provider_receipt,
)


def test_parse_provider_order_keeps_known_unique_order() -> None:
    assert parse_provider_order("ollama,openai,unknown,openai,dry-run") == (
        "ollama",
        "openai",
        "dry_run",
    )
    assert parse_provider_order("") == DEFAULT_PROVIDER_ORDER


def test_resolve_provider_plan_selects_first_configured_credential_ref() -> None:
    plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_OPENAI_CREDENTIAL_REF": "oauth:openai-memory",
            "CHIMERA_MEMORY_ENHANCEMENT_ANTHROPIC_CREDENTIAL_REF": "oauth:anthropic-memory",
        }
    )

    assert plan.selected.provider_id == "openai"
    assert plan.selected.model == "gpt-4o-mini"
    assert plan.selected.uses_user_oauth is True
    assert plan.budget.max_input_tokens == 500
    assert plan.budget.max_output_tokens == 200


def test_resolve_provider_plan_rejects_raw_looking_credential_ref() -> None:
    plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "openai,dry_run",
            "CHIMERA_MEMORY_ENHANCEMENT_OPENAI_CREDENTIAL_REF": "raw-token-material",
        }
    )

    assert plan.selected.provider_id == "dry_run"
    openai = plan.candidates[0]
    assert openai.available is False
    assert openai.reason == "invalid_credential_ref"
    receipt = safe_provider_receipt(plan)
    assert receipt["candidates"][0]["credential_ref_present"] is False
    assert "raw-token-material" not in str(receipt)


def test_resolve_provider_plan_uses_local_model_when_enabled() -> None:
    plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "ollama,dry_run",
            "CHIMERA_MEMORY_ENHANCEMENT_ENABLE_LOCAL_MODEL": "true",
            "CHIMERA_MEMORY_ENHANCEMENT_OLLAMA_MODEL": "qwen2.5:3b",
        }
    )

    assert plan.selected.provider_id == "ollama"
    assert plan.selected.model == "qwen2.5:3b"
    assert plan.selected.requires_network is False


def test_build_invocation_clamps_content_and_never_includes_raw_secret() -> None:
    request = build_memory_enhancement_request(
        content="x" * 10_000,
        persona="developer/asa",
        request_id="request-1",
    )
    plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_OPENAI_CREDENTIAL_REF": "oauth:openai-memory",
            "CHIMERA_MEMORY_ENHANCEMENT_MAX_INPUT_TOKENS": "100",
            "CHIMERA_MEMORY_ENHANCEMENT_MAX_OUTPUT_TOKENS": "64",
        }
    )

    invocation = build_enhancement_invocation(request, plan)

    assert invocation["provider"]["provider_id"] == "openai"
    assert invocation["provider"]["credential_ref"] == "oauth:openai-memory"
    assert invocation["budget"]["max_input_tokens"] == 100
    assert invocation["budget"]["max_input_chars"] == 400
    assert len(invocation["request"]["wrapped_content"]) == 400
    assert invocation["request"]["policy"]["truncated_by_budget"] is True


def test_safe_provider_receipt_hides_credential_ref_value() -> None:
    plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_OPENAI_CREDENTIAL_REF": "oauth:openai-memory",
        }
    )

    receipt = safe_provider_receipt(plan)

    assert receipt["selected_provider"] == "openai"
    assert receipt["candidates"][0]["credential_ref_present"] is True
    assert "oauth:openai-memory" not in str(receipt)


def test_classify_enhancement_failure_uses_bounded_categories() -> None:
    cases = {
        "unauthorized credential": "auth_error",
        "model deprecated or unavailable": "model_unavailable",
        "provider returned 429 rate limit": "rate_limit",
        "monthly quota exceeded": "quota_exceeded",
        "request timed out": "timeout",
        "invalid JSON schema": "parse_error",
        "content filter blocked": "content_filter",
        "something odd": "unknown_error",
    }

    for message, expected in cases.items():
        assert classify_enhancement_failure(message) == expected
