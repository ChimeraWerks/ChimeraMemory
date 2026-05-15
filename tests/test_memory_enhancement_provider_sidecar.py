from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

from chimera_memory.memory_enhancement_credentials import EnvMemoryEnhancementCredentialResolver
from chimera_memory.memory_enhancement_oauth import (
    MemoryEnhancementOAuthCredential,
    MemoryEnhancementOAuthStore,
    OAuthMemoryEnhancementCredentialResolver,
)
from chimera_memory.memory_enhancement_provider_sidecar import (
    ResolvingMemoryEnhancementProviderClient,
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
    assert captured["payload"]["model"] == "claude-haiku-4-5"


def test_resolving_provider_client_uses_openai_codex_oauth_transport(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    def fake_post_json(endpoint, payload, headers, *, opener, timeout_seconds):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["headers"] = headers
        captured["timeout_seconds"] = timeout_seconds
        return {"output_text": json.dumps({"summary": "ok"})}

    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_provider_sidecar._memory_model_client_module",
        lambda: _fake_model_client(fake_post_json),
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
    assert payload["input"][0]["role"] == "user"
    assert payload["input"][0]["content"][0]["type"] == "input_text"
    assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["prompt_cache_key"] == "cm-memory-enhancement-openai-gpt-5.4"


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

    result = client.invoke(_invocation("google", "gemini-2.5-flash", "oauth:google-memory"))

    headers = captured["headers"]
    payload = captured["payload"]
    assert result["summary"] == "ok"
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer TEST_ONLY_GOOGLE_ACCESS"
    assert isinstance(payload, dict)
    assert payload["project"] == "project-test"
    assert payload["model"] == "gemini-2.5-flash"
    assert "request" in payload


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

    result = client.invoke(_invocation("google", "gemini-2.5-flash", "oauth:google-memory"))

    assert result["summary"] == "ok"
    assert captured[0]["endpoint"] == "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
    assert captured[1]["endpoint"] == "https://cloudcode-pa.googleapis.com/v1internal:generateContent"
    assert captured[1]["payload"]["project"] == "project-discovered"


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
