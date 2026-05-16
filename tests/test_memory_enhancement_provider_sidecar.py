from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

from chimera_memory.memory_enhancement_credentials import EnvMemoryEnhancementCredentialResolver
from chimera_memory.memory_enhancement_oauth import (
    AUTH_TYPE_API_KEY,
    MemoryEnhancementOAuthCredential,
    MemoryEnhancementOAuthStore,
    MemoryEnhancementPooledCredential,
    OAuthMemoryEnhancementCredentialResolver,
)
from chimera_memory.memory_enhancement_provider_sidecar import (
    ResolvingMemoryEnhancementProviderClient,
    _google_cloudcode_endpoint,
    _openai_codex_stream_text,
)


def test_resolving_provider_client_resolves_api_key_ref_per_invocation():
    captured: list[str] = []

    class FakeApiKeyClient:
        def __init__(self, token: str) -> None:
            self.token = token

        def invoke(self, _invocation):
            return {"summary": self.token}

    client = ResolvingMemoryEnhancementProviderClient(
        credential_resolver=EnvMemoryEnhancementCredentialResolver({"PA_PROVIDER_TOKEN": "TEST_ONLY_API_KEY"}),
        api_key_client_factory=lambda token: captured.append(token) or FakeApiKeyClient(token),
    )

    result = client.invoke(_invocation("openai", "gpt-4o-mini", "env:PA_PROVIDER_TOKEN"))

    assert result == {"summary": "TEST_ONLY_API_KEY"}
    assert captured == ["TEST_ONLY_API_KEY"]


def test_resolving_provider_client_resolves_pooled_api_key_ref_per_invocation(tmp_path: Path):
    captured: list[str] = []

    class FakeApiKeyClient:
        def __init__(self, token: str) -> None:
            self.token = token

        def invoke(self, _invocation):
            return {"summary": self.token}

    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
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
    client = ResolvingMemoryEnhancementProviderClient(
        oauth_resolver=OAuthMemoryEnhancementCredentialResolver(store),
        api_key_client_factory=lambda token: captured.append(token) or FakeApiKeyClient(token),
    )

    result = client.invoke(_invocation("openrouter", "openai/gpt-4o-mini", "secret:openrouter-primary"))

    assert result == {"summary": "TEST_ONLY_OPENROUTER_KEY"}
    assert captured == ["TEST_ONLY_OPENROUTER_KEY"]


def test_resolving_provider_client_uses_anthropic_oauth_transport(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    def fake_post_json(endpoint, payload, headers, *, opener, timeout_seconds):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["headers"] = headers
        captured["timeout_seconds"] = timeout_seconds
        return {"content": [{"text": json.dumps({"summary": "ok"})}]}

    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_provider_sidecar._memory_model_client_module",
        lambda: _fake_model_client(fake_post_json),
    )
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    store.upsert(
        MemoryEnhancementOAuthCredential(
            name="anthropic-memory",
            provider_id="anthropic",
            source="manual:hermes_pkce",
            access_token="TEST_ONLY_ANTHROPIC_ACCESS",
            refresh_token="TEST_ONLY_ANTHROPIC_REFRESH",
            transport="anthropic_oauth",
        )
    )
    client = ResolvingMemoryEnhancementProviderClient(
        oauth_resolver=OAuthMemoryEnhancementCredentialResolver(store),
        opener=lambda *_args, **_kwargs: None,
    )

    result = client.invoke(_invocation("anthropic", "claude-haiku-4-5", "oauth:anthropic-memory"))

    headers = captured["headers"]
    assert result["summary"] == "ok"
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer TEST_ONLY_ANTHROPIC_ACCESS"
    assert "anthropic-beta" in headers
    assert headers["anthropic-beta"] == (
        "interleaved-thinking-2025-05-14,"
        "fine-grained-tool-streaming-2025-05-14,"
        "claude-code-20250219,"
        "oauth-2025-04-20"
    )
    assert headers["user-agent"] == "claude-cli/2.1.74 (external, cli)"
    assert captured["payload"]["model"] == "claude-haiku-4-5"


def test_resolving_provider_client_uses_openai_codex_oauth_transport(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    def fake_post_openai_codex_stream(endpoint, payload, headers, *, opener, timeout_seconds, model_client):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["headers"] = headers
        captured["timeout_seconds"] = timeout_seconds
        return json.dumps({"summary": "ok"})

    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_provider_sidecar._memory_model_client_module",
        lambda: _fake_model_client(lambda *_args, **_kwargs: {}),
    )
    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_provider_sidecar._post_openai_codex_stream",
        fake_post_openai_codex_stream,
    )
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    store.upsert(
        MemoryEnhancementOAuthCredential(
            name="openai-memory",
            provider_id="openai",
            source="browser:openai_device",
            access_token=_jwt_with_chatgpt_account("acct_test"),
            refresh_token="TEST_ONLY_OPENAI_REFRESH",
            transport="openai_codex",
            base_url="https://chatgpt.com/backend-api/codex",
        )
    )
    client = ResolvingMemoryEnhancementProviderClient(
        oauth_resolver=OAuthMemoryEnhancementCredentialResolver(store),
        opener=lambda *_args, **_kwargs: None,
    )

    result = client.invoke(_invocation("openai", "gpt-5.4", "oauth:openai-memory"))

    headers = captured["headers"]
    payload = captured["payload"]
    assert result["summary"] == "ok"
    assert captured["endpoint"] == "https://chatgpt.com/backend-api/codex/responses"
    assert isinstance(headers, dict)
    assert headers["Authorization"].startswith("Bearer ")
    assert headers["originator"] == "codex_cli_rs"
    assert headers["ChatGPT-Account-ID"] == "acct_test"
    assert "session_id" in headers
    assert headers["session_id"] == "cm-memory-enhancement-openai-gpt-5.4"
    assert headers["x-client-request-id"] == "req-test"
    assert isinstance(payload, dict)
    assert payload["model"] == "gpt-5.4"
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["input"][0]["role"] == "user"
    assert payload["input"][0]["content"][0]["type"] == "input_text"
    assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["prompt_cache_key"] == "cm-memory-enhancement-openai-gpt-5.4"


def test_openai_codex_stream_text_reads_completed_response():
    raw = "\n".join(
        (
            'data: {"type":"response.output_text.delta","delta":"partial"}',
            'data: {"type":"response.completed","response":{"output_text":"{\\"summary\\":\\"ok\\"}"}}',
            "data: [DONE]",
        )
    ).encode("utf-8")

    assert _openai_codex_stream_text(raw) == '{"summary":"ok"}'


def test_resolving_provider_client_refreshes_expiring_oauth_before_model_call(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    def fake_post_json(endpoint, payload, headers, *, opener, timeout_seconds):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["headers"] = headers
        captured["timeout_seconds"] = timeout_seconds
        return {"content": [{"text": json.dumps({"summary": "ok"})}]}

    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_provider_sidecar._memory_model_client_module",
        lambda: _fake_model_client(fake_post_json),
    )
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    store.upsert(
        MemoryEnhancementOAuthCredential(
            name="anthropic-memory",
            provider_id="anthropic",
            source="manual:hermes_pkce",
            access_token="TEST_ONLY_OLD_ANTHROPIC_ACCESS",
            refresh_token="TEST_ONLY_OLD_ANTHROPIC_REFRESH",
            expires_at_ms=1,
            transport="anthropic_oauth",
        )
    )

    def refresher(credential: MemoryEnhancementOAuthCredential) -> MemoryEnhancementOAuthCredential:
        return MemoryEnhancementOAuthCredential(
            name=credential.name,
            provider_id=credential.provider_id,
            source=credential.source,
            access_token="TEST_ONLY_NEW_ANTHROPIC_ACCESS",
            refresh_token="TEST_ONLY_NEW_ANTHROPIC_REFRESH",
            expires_at_ms=4_200_000_000_000,
            transport=credential.transport,
        )

    client = ResolvingMemoryEnhancementProviderClient(
        oauth_resolver=OAuthMemoryEnhancementCredentialResolver(store, refresher=refresher),
        opener=lambda *_args, **_kwargs: None,
    )

    result = client.invoke(_invocation("anthropic", "claude-haiku-4-5", "oauth:anthropic-memory"))

    headers = captured["headers"]
    persisted = store.get("anthropic-memory", provider_id="anthropic")
    assert result["summary"] == "ok"
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer TEST_ONLY_NEW_ANTHROPIC_ACCESS"
    assert persisted.access_token == "TEST_ONLY_NEW_ANTHROPIC_ACCESS"
    assert persisted.refresh_token == "TEST_ONLY_NEW_ANTHROPIC_REFRESH"


def test_anthropic_oauth_retries_once_after_auth_failure(monkeypatch, tmp_path: Path):
    captured_headers: list[dict[str, str]] = []

    def fake_post_json(_endpoint, _payload, headers, *, opener, timeout_seconds):
        captured_headers.append(headers)
        if len(captured_headers) == 1:
            raise RuntimeError("memory enhancement provider auth failed")
        return {"content": [{"text": json.dumps({"summary": "ok"})}]}

    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_provider_sidecar._memory_model_client_module",
        lambda: _fake_model_client(fake_post_json),
    )
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    store.upsert(
        MemoryEnhancementOAuthCredential(
            name="anthropic-memory",
            provider_id="anthropic",
            source="manual:hermes_pkce",
            access_token="TEST_ONLY_OLD_ANTHROPIC_ACCESS",
            refresh_token="TEST_ONLY_OLD_ANTHROPIC_REFRESH",
            expires_at_ms=4_200_000_000_000,
            transport="anthropic_oauth",
        )
    )

    def refresher(credential: MemoryEnhancementOAuthCredential) -> MemoryEnhancementOAuthCredential:
        return MemoryEnhancementOAuthCredential(
            name=credential.name,
            provider_id=credential.provider_id,
            source=credential.source,
            access_token="TEST_ONLY_NEW_ANTHROPIC_ACCESS",
            refresh_token=credential.refresh_token,
            expires_at_ms=4_200_000_000_000,
            transport=credential.transport,
        )

    client = ResolvingMemoryEnhancementProviderClient(
        oauth_resolver=OAuthMemoryEnhancementCredentialResolver(store, refresher=refresher),
        opener=lambda *_args, **_kwargs: None,
    )

    result = client.invoke(_invocation("anthropic", "claude-haiku-4-5", "oauth:anthropic-memory"))

    assert result["summary"] == "ok"
    assert len(captured_headers) == 2
    assert captured_headers[0]["Authorization"] == "Bearer TEST_ONLY_OLD_ANTHROPIC_ACCESS"
    assert captured_headers[1]["Authorization"] == "Bearer TEST_ONLY_NEW_ANTHROPIC_ACCESS"


def test_resolving_provider_client_uses_google_cloudcode_transport(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    def fake_post_json(endpoint, payload, headers, *, opener, timeout_seconds):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["headers"] = headers
        captured["timeout_seconds"] = timeout_seconds
        return {"candidates": [{"content": {"parts": [{"text": json.dumps({"summary": "ok"})}]}}]}

    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_provider_sidecar._memory_model_client_module",
        lambda: _fake_model_client(fake_post_json),
    )
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    store.upsert(
        MemoryEnhancementOAuthCredential(
            name="google-memory",
            provider_id="google",
            source="manual:google_pkce",
            access_token="TEST_ONLY_GOOGLE_ACCESS",
            refresh_token="TEST_ONLY_GOOGLE_REFRESH",
            transport="google_cloudcode",
            project_id="project-test",
        )
    )
    client = ResolvingMemoryEnhancementProviderClient(
        oauth_resolver=OAuthMemoryEnhancementCredentialResolver(store),
        opener=lambda *_args, **_kwargs: None,
    )

    result = client.invoke(_invocation("google", "gemini-3-flash-preview", "oauth:google-memory"))

    headers = captured["headers"]
    payload = captured["payload"]
    assert result["summary"] == "ok"
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer TEST_ONLY_GOOGLE_ACCESS"
    assert isinstance(payload, dict)
    assert payload["project"] == "project-test"
    assert payload["model"] == "gemini-3-flash-preview"
    assert "request" in payload


def test_google_cloudcode_falls_back_to_hermes_oauth_model_list(monkeypatch, tmp_path: Path):
    captured_models: list[str] = []

    def fake_post_json(endpoint, payload, headers, *, opener, timeout_seconds):
        if endpoint.endswith(":generateContent"):
            captured_models.append(payload["model"])
            if payload["model"] == "gemini-2.5-flash":
                raise RuntimeError("memory enhancement provider unavailable")
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"summary": "ok"})}]}}]}
        return {"cloudaicompanionProject": "project-test"}

    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_provider_sidecar._memory_model_client_module",
        lambda: _fake_model_client(fake_post_json),
    )
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    store.upsert(
        MemoryEnhancementOAuthCredential(
            name="google-memory",
            provider_id="google",
            source="manual:google_pkce",
            access_token="TEST_ONLY_GOOGLE_ACCESS",
            refresh_token="TEST_ONLY_GOOGLE_REFRESH",
            transport="google_cloudcode",
            project_id="project-test",
        )
    )
    client = ResolvingMemoryEnhancementProviderClient(
        oauth_resolver=OAuthMemoryEnhancementCredentialResolver(store),
        opener=lambda *_args, **_kwargs: None,
    )

    result = client.invoke(_invocation("google", "gemini-2.5-flash", "oauth:google-memory"))

    assert result["summary"] == "ok"
    assert captured_models == ["gemini-2.5-flash", "gemini-3-flash-preview"]


def test_google_cloudcode_discovers_project_when_hermes_pool_credential_has_none(monkeypatch, tmp_path: Path):
    captured: list[dict[str, object]] = []

    def fake_post_json(endpoint, payload, headers, *, opener, timeout_seconds):
        captured.append(
            {
                "endpoint": endpoint,
                "payload": payload,
                "headers": headers,
                "timeout_seconds": timeout_seconds,
            }
        )
        if endpoint.endswith(":loadCodeAssist"):
            return {"cloudaicompanionProject": "project-discovered"}
        return {"response": {"candidates": [{"content": {"parts": [{"text": json.dumps({"summary": "ok"})}]}}]}}

    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_provider_sidecar._memory_model_client_module",
        lambda: _fake_model_client(fake_post_json),
    )
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    store.upsert(
        MemoryEnhancementOAuthCredential(
            name="google-memory",
            provider_id="google",
            source="hermes_auth_pool",
            access_token="TEST_ONLY_GOOGLE_ACCESS",
            transport="google_cloudcode",
            base_url="https://cloudcode-pa.googleapis.com",
        )
    )
    client = ResolvingMemoryEnhancementProviderClient(
        oauth_resolver=OAuthMemoryEnhancementCredentialResolver(store),
        opener=lambda *_args, **_kwargs: None,
    )

    result = client.invoke(_invocation("google", "gemini-3-flash-preview", "oauth:google-memory"))

    assert result["summary"] == "ok"
    assert captured[0]["endpoint"] == "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
    assert captured[0]["payload"] == {
        "metadata": {
            "duetProject": "",
            "ideType": "IDE_UNSPECIFIED",
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI",
        }
    }
    assert captured[0]["headers"]["User-Agent"] == "google-api-nodejs-client/9.15.1 (gzip) model/gemini-3-flash-preview"
    assert captured[0]["headers"]["X-Goog-Api-Client"] == "gl-node/24.0.0"
    assert captured[1]["endpoint"] == "https://cloudcode-pa.googleapis.com/v1internal:generateContent"
    assert captured[1]["headers"]["User-Agent"] == "hermes-agent (gemini-cli-compat)"
    assert captured[1]["headers"]["X-Goog-Api-Client"] == "gl-python/hermes"
    assert captured[1]["payload"]["project"] == "project-discovered"


def test_google_cloudcode_onboards_free_tier_when_project_missing(monkeypatch, tmp_path: Path):
    captured: list[dict[str, object]] = []

    def fake_post_json(endpoint, payload, headers, *, opener, timeout_seconds):
        captured.append({"endpoint": endpoint, "payload": payload, "headers": headers})
        if endpoint.endswith(":loadCodeAssist"):
            return {"allowedTiers": [{"id": "free-tier", "isDefault": True}]}
        if endpoint.endswith(":onboardUser"):
            return {"done": True, "response": {"cloudaicompanionProject": "project-onboarded"}}
        return {"response": {"candidates": [{"content": {"parts": [{"text": json.dumps({"summary": "ok"})}]}}]}}

    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_provider_sidecar._memory_model_client_module",
        lambda: _fake_model_client(fake_post_json),
    )
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    store.upsert(
        MemoryEnhancementOAuthCredential(
            name="google-memory",
            provider_id="google",
            source="browser:google_pkce",
            access_token="TEST_ONLY_GOOGLE_ACCESS",
            refresh_token="TEST_ONLY_GOOGLE_REFRESH",
            transport="google_cloudcode",
        )
    )
    client = ResolvingMemoryEnhancementProviderClient(
        oauth_resolver=OAuthMemoryEnhancementCredentialResolver(store),
        opener=lambda *_args, **_kwargs: None,
    )

    result = client.invoke(_invocation("google", "gemini-3-flash-preview", "oauth:google-memory"))

    assert result["summary"] == "ok"
    assert captured[1]["endpoint"] == "https://cloudcode-pa.googleapis.com/v1internal:onboardUser"
    assert captured[1]["payload"] == {
        "tierId": "free-tier",
        "metadata": {
            "ideType": "IDE_UNSPECIFIED",
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI",
        },
    }
    assert captured[2]["payload"]["project"] == "project-onboarded"


def test_google_cloudcode_endpoint_ignores_public_gemini_base_url():
    credential = MemoryEnhancementOAuthCredential(
        name="google-memory",
        provider_id="google",
        source="hermes_auth_pool",
        access_token="TEST_ONLY_GOOGLE_ACCESS",
        transport="google_cloudcode",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )

    endpoint = _google_cloudcode_endpoint({"endpoint": ""}, credential, "loadCodeAssist")

    assert endpoint == "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"


def _invocation(provider_id: str, model: str, credential_ref: str):
    return {
        "request_id": "req-test",
        "provider": {
            "provider_id": provider_id,
            "model": model,
            "credential_ref": credential_ref,
            "endpoint": "",
        },
        "budget": {
            "max_output_tokens": 128,
            "timeout_seconds": 12,
        },
        "request": {
            "wrapped_content": "remember this",
        },
    }


def _fake_model_client(fake_post_json):
    return SimpleNamespace(
        _budget=lambda _invocation: SimpleNamespace(max_output_tokens=128, timeout_seconds=12),
        _system_prompt=lambda: "system",
        _user_prompt=lambda invocation: json.dumps(invocation["request"]),
        _post_json=fake_post_json,
        _metadata_from_model_text=lambda text: json.loads(text),
    )


def _jwt_with_chatgpt_account(account_id: str) -> str:
    header = _b64({"alg": "none", "typ": "JWT"})
    payload = _b64({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}})
    return f"{header}.{payload}."


def _b64(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
