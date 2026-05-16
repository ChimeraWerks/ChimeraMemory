from chimera_memory.memory_enhancement import build_memory_enhancement_request
from chimera_memory.memory_enhancement_oauth import (
    AUTH_TYPE_API_KEY,
    MemoryEnhancementOAuthCredential,
    MemoryEnhancementOAuthStore,
    MemoryEnhancementPooledCredential,
)
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
    assert plan.budget.max_output_tokens == 1200


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


def test_resolve_provider_plan_uses_active_oauth_when_ref_missing(tmp_path) -> None:
    store = MemoryEnhancementOAuthStore(tmp_path / "auth.json")
    store.upsert(
        MemoryEnhancementOAuthCredential(
            name="openai-primary",
            provider_id="openai",
            source="browser:openai_device",
            access_token="TEST_ONLY_OPENAI_PRIMARY",
            refresh_token="TEST_ONLY_OPENAI_REFRESH_PRIMARY",
            transport="openai_codex",
        )
    )
    store.upsert(
        MemoryEnhancementOAuthCredential(
            name="openai-secondary",
            provider_id="openai",
            source="browser:openai_device",
            access_token="TEST_ONLY_OPENAI_SECONDARY",
            refresh_token="TEST_ONLY_OPENAI_REFRESH_SECONDARY",
            transport="openai_codex",
        )
    )
    store.set_active("openai-primary", provider_id="openai")

    plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "openai,dry_run",
            "CHIMERA_MEMORY_OAUTH_STORE": str(store.path),
        }
    )

    assert plan.selected.provider_id == "openai"
    assert plan.selected.credential_ref == "oauth:openai-primary"
    assert plan.selected.uses_user_oauth is True


def test_resolve_provider_plan_uses_active_pooled_api_key_when_ref_missing(tmp_path) -> None:
    store = MemoryEnhancementOAuthStore(tmp_path / "auth.json")
    store.upsert_pooled(
        MemoryEnhancementPooledCredential(
            provider_id="openrouter",
            id="openrouter-primary",
            label="Primary OpenRouter",
            auth_type=AUTH_TYPE_API_KEY,
            priority=0,
            source="manual",
            access_token="TEST_ONLY_OPENROUTER_KEY",
        )
    )

    plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "openrouter,dry_run",
            "CHIMERA_MEMORY_OAUTH_STORE": str(store.path),
        }
    )

    assert plan.selected.provider_id == "openrouter"
    assert plan.selected.credential_ref == "secret:openrouter-primary"
    assert plan.selected.uses_user_oauth is False


def test_resolve_provider_plan_fails_over_from_exhausted_active_pool_credential(tmp_path) -> None:
    store = MemoryEnhancementOAuthStore(tmp_path / "auth.json")
    for name, token, priority in (
        ("openrouter-primary", "TEST_ONLY_OPENROUTER_PRIMARY", 0),
        ("openrouter-secondary", "TEST_ONLY_OPENROUTER_SECONDARY", 1),
    ):
        store.upsert_pooled(
            MemoryEnhancementPooledCredential(
                provider_id="openrouter",
                id=name,
                label=name,
                auth_type=AUTH_TYPE_API_KEY,
                priority=priority,
                source="manual",
                access_token=token,
            )
        )
    store.set_active_pooled("openrouter-primary", provider_id="openrouter")
    store.mark_pooled_exhausted(
        "openrouter-primary",
        provider_id="openrouter",
        status_code=429,
        reason="rate_limit",
        message="rate limited",
    )

    plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "openrouter,dry_run",
            "CHIMERA_MEMORY_OAUTH_STORE": str(store.path),
        }
    )

    assert plan.selected.provider_id == "openrouter"
    assert plan.selected.credential_ref == "secret:openrouter-secondary"
    assert plan.selected.uses_user_oauth is False


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
        "monthly quota exceeded": "billing",
        "request timed out": "timeout",
        "invalid JSON schema": "parse_error",
        "content filter blocked": "content_filter",
        "this model is not available at your current rate tier": "model_unavailable",
        "operate separately without provider throttling": "unknown_error",
        "something odd": "unknown_error",
    }

    for message, expected in cases.items():
        assert classify_enhancement_failure(message) == expected


def test_classify_enhancement_failure_uses_hermes_status_and_body_refinement() -> None:
    assert (
        classify_enhancement_failure(
            "Error",
            provider="anthropic",
            model="claude-sonnet-4-6",
            status_code=400,
            body={
                "error": {
                    "message": "The long context beta is not yet available for this subscription.",
                    "type": "invalid_request_error",
                }
            },
        )
        == "oauth_long_context_beta_forbidden"
    )
    assert (
        classify_enhancement_failure(
            "Error",
            provider="anthropic",
            model="claude-sonnet-4-6",
            status_code=429,
            body={"error": {"message": "extra usage long context is not enabled"}},
        )
        == "long_context_tier"
    )
