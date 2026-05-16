from __future__ import annotations

import json
import io
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from chimera_memory.memory_enhancement_credentials import (
    MemoryEnhancementCredentialRef,
    MemoryEnhancementCredentialResolutionError,
    ProtocolValidationError,
)
from chimera_memory.memory_enhancement_oauth import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_OAUTH,
    MemoryEnhancementOAuthCredential,
    MemoryEnhancementOAuthStore,
    MemoryEnhancementPooledCredential,
    OAuthMemoryEnhancementCredentialResolver,
    _google_oauth_client_credentials,
    refresh_memory_enhancement_oauth_credential,
    resolve_oauth_store_path,
)


def test_oauth_store_round_trips_provider_credentials_without_safe_token_echo(tmp_path: Path):
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    credential = MemoryEnhancementOAuthCredential(
        name="anthropic-memory",
        provider_id="anthropic",
        source="manual:hermes_pkce",
        access_token="TEST_ONLY_ACCESS_TOKEN",
        refresh_token="TEST_ONLY_REFRESH_TOKEN",
        expires_at_ms=1_800_000_000_000,
        transport="anthropic_oauth",
        account_label="test@example.invalid",
    )

    store.upsert(credential)
    loaded = store.get("anthropic-memory", provider_id="anthropic")
    safe_json = json.dumps(loaded.to_safe_dict(), sort_keys=True)

    assert loaded.provider_id == "anthropic"
    assert loaded.transport == "anthropic_oauth"
    assert loaded.access_token == "TEST_ONLY_ACCESS_TOKEN"
    assert "TEST_ONLY_ACCESS_TOKEN" not in safe_json
    assert "TEST_ONLY_REFRESH_TOKEN" not in safe_json
    assert "anthropic-memory" not in safe_json
    assert safe_json.count("anthropic") >= 1


def test_oauth_store_writes_hermes_pool_and_legacy_provider_mirror(tmp_path: Path):
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    credential = MemoryEnhancementOAuthCredential(
        name="openai-memory",
        provider_id="openai",
        source="browser:openai_device",
        access_token="TEST_ONLY_OPENAI_ACCESS",
        refresh_token="TEST_ONLY_OPENAI_REFRESH",
        transport="openai_codex",
        account_label="primary@example.invalid",
    )

    store.upsert(credential)
    payload = store.read()

    pool_entry = payload["credential_pool"]["openai"][0]
    assert pool_entry["id"] == "openai-memory"
    assert pool_entry["auth_type"] == AUTH_TYPE_OAUTH
    assert pool_entry["label"] == "primary@example.invalid"
    assert payload["providers"]["openai"]["openai-memory"]["transport"] == "openai_codex"
    assert store.get_pooled("openai-memory", provider_id="openai").auth_type == AUTH_TYPE_OAUTH


def test_pooled_api_key_credentials_round_trip_and_select_without_safe_token_echo(tmp_path: Path):
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    store.upsert_pooled(
        MemoryEnhancementPooledCredential(
            provider_id="openrouter",
            id="openrouter-primary",
            label="Primary OpenRouter",
            auth_type=AUTH_TYPE_API_KEY,
            priority=10,
            source="manual",
            access_token="TEST_ONLY_OPENROUTER_KEY",
        )
    )

    loaded = store.get_active_pooled("openrouter")
    safe_json = json.dumps(loaded.to_safe_dict(), sort_keys=True)
    payload = store.read()

    assert loaded.ref.raw_ref == "secret:openrouter-primary"
    assert loaded.auth_type == AUTH_TYPE_API_KEY
    assert payload["credential_pool"]["openrouter"][0]["auth_type"] == AUTH_TYPE_API_KEY
    assert payload["providers"] == {}
    assert "TEST_ONLY_OPENROUTER_KEY" not in safe_json
    assert "openrouter-primary" not in safe_json


def test_oauth_store_migrates_legacy_provider_entries_into_pool(tmp_path: Path):
    path = tmp_path / "memory-oauth.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_provider": "google",
                "active_credentials": {"google": "google-memory"},
                "providers": {
                    "google": {
                        "google-memory": {
                            "provider_id": "google",
                            "source": "browser:google_pkce",
                            "access_token": "TEST_ONLY_GOOGLE_ACCESS",
                            "refresh_token": "TEST_ONLY_GOOGLE_REFRESH",
                            "transport": "google_cloudcode",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    payload = MemoryEnhancementOAuthStore(path).read()

    assert payload["credential_pool"]["google"][0]["id"] == "google-memory"
    assert payload["credential_pool"]["google"][0]["auth_type"] == AUTH_TYPE_OAUTH
    assert payload["providers"]["google"]["google-memory"]["transport"] == "google_cloudcode"


def test_oauth_resolver_returns_legacy_value_for_current_static_boundaries(tmp_path: Path):
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

    resolver = OAuthMemoryEnhancementCredentialResolver(store)
    ref = MemoryEnhancementCredentialRef(scheme="oauth", name="google-memory")
    resolved = resolver.resolve(ref)
    provider_resolved = resolver.resolve_oauth(ref, provider_id="google")

    assert resolved.value == "TEST_ONLY_GOOGLE_ACCESS"
    assert resolved.source == "oauth:google:manual:google_pkce"
    assert provider_resolved.project_id == "project-test"
    assert provider_resolved.transport == "google_cloudcode"


def test_oauth_resolver_refreshes_expiring_token_and_persists_rotation(tmp_path: Path):
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    store.upsert(
        MemoryEnhancementOAuthCredential(
            name="anthropic-memory",
            provider_id="anthropic",
            source="manual:hermes_pkce",
            access_token="TEST_ONLY_OLD_ACCESS",
            refresh_token="TEST_ONLY_OLD_REFRESH",
            expires_at_ms=1,
            transport="anthropic_oauth",
        )
    )
    seen_refresh_tokens: list[str] = []

    def refresher(credential: MemoryEnhancementOAuthCredential) -> MemoryEnhancementOAuthCredential:
        seen_refresh_tokens.append(credential.refresh_token)
        return MemoryEnhancementOAuthCredential(
            name=credential.name,
            provider_id=credential.provider_id,
            source=credential.source,
            access_token="TEST_ONLY_NEW_ACCESS",
            refresh_token="TEST_ONLY_NEW_REFRESH",
            expires_at_ms=4_200_000_000_000,
            transport=credential.transport,
        )

    resolver = OAuthMemoryEnhancementCredentialResolver(store, refresher=refresher)
    resolved = resolver.resolve_oauth(MemoryEnhancementCredentialRef(scheme="oauth", name="anthropic-memory"))
    persisted = store.get("anthropic-memory", provider_id="anthropic")

    assert resolved.access_token == "TEST_ONLY_NEW_ACCESS"
    assert resolved.refresh_token == "TEST_ONLY_NEW_REFRESH"
    assert persisted.access_token == "TEST_ONLY_NEW_ACCESS"
    assert persisted.refresh_token == "TEST_ONLY_NEW_REFRESH"
    assert seen_refresh_tokens == ["TEST_ONLY_OLD_REFRESH"]


def test_oauth_resolver_keeps_fresh_token_without_refresh(tmp_path: Path):
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    store.upsert(
        MemoryEnhancementOAuthCredential(
            name="google-memory",
            provider_id="google",
            source="manual:google_pkce",
            access_token="TEST_ONLY_GOOGLE_ACCESS",
            refresh_token="TEST_ONLY_GOOGLE_REFRESH",
            expires_at_ms=4_200_000_000_000,
            transport="google_cloudcode",
            project_id="project-test",
        )
    )

    def fail_refresh(_credential: MemoryEnhancementOAuthCredential) -> MemoryEnhancementOAuthCredential:
        raise AssertionError("fresh token should not refresh")

    resolver = OAuthMemoryEnhancementCredentialResolver(store, refresher=fail_refresh)
    resolved = resolver.resolve_oauth(MemoryEnhancementCredentialRef(scheme="oauth", name="google-memory"))

    assert resolved.access_token == "TEST_ONLY_GOOGLE_ACCESS"


def test_oauth_store_tracks_active_credentials_per_provider(tmp_path: Path):
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    credentials = [
        MemoryEnhancementOAuthCredential(
            name="openai-primary",
            provider_id="openai",
            source="browser:openai_device",
            access_token="TEST_ONLY_OPENAI_PRIMARY",
            refresh_token="TEST_ONLY_OPENAI_REFRESH_PRIMARY",
            transport="openai_codex",
            account_label="primary@example.invalid",
        ),
        MemoryEnhancementOAuthCredential(
            name="openai-secondary",
            provider_id="openai",
            source="browser:openai_device",
            access_token="TEST_ONLY_OPENAI_SECONDARY",
            refresh_token="TEST_ONLY_OPENAI_REFRESH_SECONDARY",
            transport="openai_codex",
            account_label="secondary@example.invalid",
        ),
        MemoryEnhancementOAuthCredential(
            name="google-memory",
            provider_id="google",
            source="browser:google_pkce",
            access_token="TEST_ONLY_GOOGLE_ACCESS",
            refresh_token="TEST_ONLY_GOOGLE_REFRESH",
            transport="google_cloudcode",
            project_id="project-test",
        ),
    ]
    for credential in credentials:
        store.upsert(credential)

    assert [credential.name for credential in store.list_credentials(provider_id="openai")] == [
        "openai-primary",
        "openai-secondary",
    ]
    assert store.active_name("openai") == "openai-secondary"
    assert store.active_name("google") == "google-memory"
    assert store.get_active("openai").access_token == "TEST_ONLY_OPENAI_SECONDARY"

    selected = store.set_active("openai-primary", provider_id="openai")
    payload = store.read()

    assert selected.name == "openai-primary"
    assert store.get_active("openai").access_token == "TEST_ONLY_OPENAI_PRIMARY"
    assert payload["active_provider"] == "openai"
    assert payload["active_credentials"] == {
        "openai": "openai-primary",
        "google": "google-memory",
    }


def test_oauth_store_normalizes_stale_active_credentials(tmp_path: Path):
    path = tmp_path / "memory-oauth.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_provider": "openai",
                "active_credentials": {
                    "openai": "missing-openai",
                    "google": "google-memory",
                    "unsupported": "ignored",
                },
                "providers": {
                    "google": {
                        "google-memory": {
                            "provider_id": "google",
                            "source": "browser:google_pkce",
                            "access_token": "TEST_ONLY_GOOGLE_ACCESS",
                            "refresh_token": "TEST_ONLY_GOOGLE_REFRESH",
                            "transport": "google_cloudcode",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = MemoryEnhancementOAuthStore(path)

    payload = store.read()

    assert payload["active_provider"] == ""
    assert payload["active_credentials"] == {"google": "google-memory"}
    assert store.get_active("google").name == "google-memory"
    with pytest.raises(MemoryEnhancementCredentialResolutionError, match="active credential unavailable"):
        store.get_active("openai")


def test_refresh_anthropic_oauth_posts_form_and_preserves_unrotated_refresh_token():
    captured: dict[str, object] = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = urllib.parse.parse_qs(request.data.decode("utf-8"))
        return _json_response({"access_token": "TEST_ONLY_NEW_ACCESS", "expires_in": 3600})

    credential = MemoryEnhancementOAuthCredential(
        name="anthropic-memory",
        provider_id="anthropic",
        source="manual:hermes_pkce",
        access_token="TEST_ONLY_OLD_ACCESS",
        refresh_token="TEST_ONLY_OLD_REFRESH",
        expires_at_ms=1,
        transport="anthropic_oauth",
    )

    refreshed = refresh_memory_enhancement_oauth_credential(credential, opener=opener)

    payload = captured["payload"]
    assert "platform.claude.com" in captured["url"]
    assert captured["timeout"] == 20
    assert payload["grant_type"] == ["refresh_token"]
    assert payload["refresh_token"] == ["TEST_ONLY_OLD_REFRESH"]
    assert refreshed.access_token == "TEST_ONLY_NEW_ACCESS"
    assert refreshed.refresh_token == "TEST_ONLY_OLD_REFRESH"
    assert refreshed.expires_at_ms is not None


def test_refresh_google_oauth_uses_credential_client_metadata_and_preserves_project():
    captured: dict[str, object] = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = urllib.parse.parse_qs(request.data.decode("utf-8"))
        return _json_response(
            {
                "access_token": "TEST_ONLY_NEW_GOOGLE_ACCESS",
                "refresh_token": "TEST_ONLY_NEW_GOOGLE_REFRESH",
                "expires_in": 3600,
            }
        )

    credential = MemoryEnhancementOAuthCredential(
        name="google-memory",
        provider_id="google",
        source="manual:google_pkce",
        access_token="TEST_ONLY_OLD_GOOGLE_ACCESS",
        refresh_token="TEST_ONLY_OLD_GOOGLE_REFRESH",
        expires_at_ms=1,
        transport="google_cloudcode",
        project_id="project-test",
        extra={
            "client_id": "TEST_ONLY_CLIENT_ID",
            "client_secret": "TEST_ONLY_CLIENT_SECRET",
        },
    )

    refreshed = refresh_memory_enhancement_oauth_credential(credential, opener=opener)
    payload = captured["payload"]

    assert captured["url"] == "https://oauth2.googleapis.com/token"
    assert captured["timeout"] == 20
    assert payload["grant_type"] == ["refresh_token"]
    assert payload["refresh_token"] == ["TEST_ONLY_OLD_GOOGLE_REFRESH"]
    assert payload["client_id"] == ["TEST_ONLY_CLIENT_ID"]
    assert payload["client_secret"] == ["TEST_ONLY_CLIENT_SECRET"]
    assert refreshed.access_token == "TEST_ONLY_NEW_GOOGLE_ACCESS"
    assert refreshed.refresh_token == "TEST_ONLY_NEW_GOOGLE_REFRESH"
    assert refreshed.project_id == "project-test"


def test_google_oauth_client_credentials_have_hermes_public_defaults(monkeypatch):
    monkeypatch.delenv("CHIMERA_MEMORY_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("CHIMERA_MEMORY_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("PERSONIFYAGENTS_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("PERSONIFYAGENTS_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("HERMES_GEMINI_CLIENT_ID", raising=False)
    monkeypatch.delenv("HERMES_GEMINI_CLIENT_SECRET", raising=False)

    client_id, client_secret = _google_oauth_client_credentials({})

    assert client_id.endswith(".apps.googleusercontent.com")
    assert client_secret.startswith("GOCSPX-")


def test_refresh_error_mapping_distinguishes_reused_refresh_token():
    def opener(request, *, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "bad request",
            hdrs={},
            fp=io.BytesIO(json.dumps({"error": "refresh_token_reused"}).encode("utf-8")),
        )

    credential = MemoryEnhancementOAuthCredential(
        name="openai-memory",
        provider_id="openai",
        source="browser:openai_device",
        access_token="TEST_ONLY_OLD_OPENAI_ACCESS",
        refresh_token="TEST_ONLY_OLD_OPENAI_REFRESH",
        expires_at_ms=1,
        transport="openai_codex",
    )

    with pytest.raises(MemoryEnhancementCredentialResolutionError, match="token reused"):
        refresh_memory_enhancement_oauth_credential(credential, opener=opener)


def test_oauth_store_rejects_ambiguous_refs(tmp_path: Path):
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")
    for provider_id, transport in (("anthropic", "anthropic_oauth"), ("google", "google_cloudcode")):
        store.upsert(
            MemoryEnhancementOAuthCredential(
                name="shared-memory",
                provider_id=provider_id,
                source="manual:test",
                access_token=f"TEST_ONLY_{provider_id.upper()}",
                transport=transport,
            )
        )

    with pytest.raises(ProtocolValidationError, match="ambiguous"):
        store.get("shared-memory")

    assert store.get("shared-memory", provider_id="google").provider_id == "google"


def test_oauth_store_path_prefers_pwa_state_root(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("CHIMERA_MEMORY_OAUTH_STORE", raising=False)
    monkeypatch.delenv("PERSONIFYAGENTS_MEMORY_OAUTH_STORE", raising=False)
    monkeypatch.setenv("CHIMERA_MEMORY_STATE_ROOT", str(tmp_path / "state"))

    assert resolve_oauth_store_path().name == "auth.json"
    assert resolve_oauth_store_path().parent == (tmp_path / "state").resolve()


def test_oauth_resolver_rejects_missing_or_wrong_scheme(tmp_path: Path):
    resolver = OAuthMemoryEnhancementCredentialResolver(MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json"))

    with pytest.raises(MemoryEnhancementCredentialResolutionError, match="unsupported"):
        resolver.resolve(MemoryEnhancementCredentialRef(scheme="env", name="OPENAI_API_KEY"))
    with pytest.raises(MemoryEnhancementCredentialResolutionError, match="unavailable"):
        resolver.resolve(MemoryEnhancementCredentialRef(scheme="oauth", name="missing-memory"))


class _json_response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")
