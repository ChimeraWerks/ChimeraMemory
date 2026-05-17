from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from chimera_memory import hermes_gemini_oauth
from chimera_memory.memory_enhancement_oauth import MemoryEnhancementOAuthStore
from chimera_memory.memory_enhancement_hermes_oauth import run_hermes_memory_enhancement_oauth_login
from chimera_memory.memory_enhancement_oauth_flow import (
    poll_memory_enhancement_oauth_flow,
    start_memory_enhancement_oauth_flow,
    submit_memory_enhancement_oauth_flow,
)


def test_anthropic_browser_oauth_flow_persists_refreshable_credential(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_oauth_flow._get_claude_code_version",
        lambda: "2.1.74",
    )
    store = MemoryEnhancementOAuthStore(tmp_path / "auth.json")
    started = start_memory_enhancement_oauth_flow("anthropic", store=store)
    flow_state = _flow_state(tmp_path, started["flow_id"])
    captured: dict[str, object] = {}

    def opener(request, *, timeout):
        captured["request"] = request
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["raw_body"] = request.data.decode("utf-8")
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _json_response(
            {
                "access_token": "TEST_ONLY_ANTHROPIC_ACCESS",
                "refresh_token": "TEST_ONLY_ANTHROPIC_REFRESH",
                "expires_in": 3600,
            }
        )

    result = submit_memory_enhancement_oauth_flow(
        started["flow_id"],
        f"TEST_ONLY_AUTHORIZATION_CODE#{flow_state['state']}",
        store=store,
        opener=opener,
    )
    credential = store.get("anthropic-memory", provider_id="anthropic")

    assert result["status"] == "approved"
    assert "claude.ai/oauth/authorize" in started["authorization_url"]
    assert captured["url"] == "https://console.anthropic.com/v1/oauth/token"
    assert request_header(captured["request"], "User-Agent") == "claude-cli/2.1.74 (external, cli)"
    assert request_header(captured["request"], "Content-Type") == "application/json"
    assert str(captured["raw_body"]).lstrip().startswith("{")
    assert captured["payload"]["grant_type"] == "authorization_code"
    assert captured["payload"]["code"] == "TEST_ONLY_AUTHORIZATION_CODE"
    assert credential.access_token == "TEST_ONLY_ANTHROPIC_ACCESS"
    assert credential.refresh_token == "TEST_ONLY_ANTHROPIC_REFRESH"
    assert credential.transport == "anthropic_oauth"


def test_google_browser_oauth_flow_uses_pkce_and_stores_client_metadata(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHIMERA_MEMORY_GOOGLE_OAUTH_CLIENT_ID", "123456789012-testclientidvalue.apps.googleusercontent.com")
    monkeypatch.setenv("CHIMERA_MEMORY_GOOGLE_OAUTH_CLIENT_SECRET", "TEST_ONLY_GOOGLE_CLIENT_SECRET")
    monkeypatch.setenv("CHIMERA_MEMORY_GOOGLE_CLOUD_PROJECT", "project-test")
    store = MemoryEnhancementOAuthStore(tmp_path / "auth.json")
    started = start_memory_enhancement_oauth_flow("google", store=store, start_callback_server=False)
    flow_state = _flow_state(tmp_path, started["flow_id"])
    captured: dict[str, object] = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = urllib.parse.parse_qs(request.data.decode("utf-8"))
        return _json_response(
            {
                "access_token": "TEST_ONLY_GOOGLE_ACCESS",
                "refresh_token": "TEST_ONLY_GOOGLE_REFRESH",
                "expires_in": 3600,
            }
        )

    callback = f"http://127.0.0.1:8085/oauth2callback?code=TEST_ONLY_GOOGLE_CODE&state={flow_state['state']}"
    result = submit_memory_enhancement_oauth_flow(started["flow_id"], callback, store=store, opener=opener)
    credential = store.get("google-memory", provider_id="google")

    assert result["status"] == "approved"
    assert "accounts.google.com" in started["authorization_url"]
    assert captured["payload"]["code"] == ["TEST_ONLY_GOOGLE_CODE"]
    assert captured["payload"]["code_verifier"] == [flow_state["code_verifier"]]
    assert credential.access_token == "TEST_ONLY_GOOGLE_ACCESS"
    assert credential.refresh_token == "TEST_ONLY_GOOGLE_REFRESH"
    assert credential.project_id == "project-test"
    assert credential.extra["client_id"] == "123456789012-testclientidvalue.apps.googleusercontent.com"


def test_google_oauth_loopback_callback_can_be_polled(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHIMERA_MEMORY_GOOGLE_OAUTH_CLIENT_ID", "123456789012-testclientidvalue.apps.googleusercontent.com")
    monkeypatch.setenv("CHIMERA_MEMORY_GOOGLE_OAUTH_CLIENT_SECRET", "TEST_ONLY_GOOGLE_CLIENT_SECRET")
    monkeypatch.setenv("CHIMERA_MEMORY_GOOGLE_OAUTH_CALLBACK_PORT", "0")
    store = MemoryEnhancementOAuthStore(tmp_path / "auth.json")
    started = start_memory_enhancement_oauth_flow("google", store=store)
    flow_state = _flow_state(tmp_path, started["flow_id"])
    captured: dict[str, object] = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["payload"] = urllib.parse.parse_qs(request.data.decode("utf-8"))
        return _json_response(
            {
                "access_token": "TEST_ONLY_GOOGLE_ACCESS",
                "refresh_token": "TEST_ONLY_GOOGLE_REFRESH",
                "expires_in": 3600,
            }
        )

    callback = (
        f"{flow_state['redirect_uri']}?code=TEST_ONLY_GOOGLE_CODE&state={flow_state['state']}"
    )
    with urllib.request.urlopen(callback, timeout=5) as response:
        assert response.status == 200

    result = poll_memory_enhancement_oauth_flow(started["flow_id"], store=store, opener=opener)
    credential = store.get("google-memory", provider_id="google")

    assert started["submit_mode"] == "poll"
    assert result["status"] == "approved"
    assert captured["payload"]["code"] == ["TEST_ONLY_GOOGLE_CODE"]
    assert captured["payload"]["redirect_uri"] == [flow_state["redirect_uri"]]
    assert credential.access_token == "TEST_ONLY_GOOGLE_ACCESS"


def test_openai_device_oauth_flow_polls_and_persists_codex_credential(tmp_path: Path):
    store = MemoryEnhancementOAuthStore(tmp_path / "auth.json")
    calls: list[str] = []

    def opener(request, *, timeout):
        calls.append(request.full_url)
        if request.full_url.endswith("/api/accounts/deviceauth/usercode"):
            return _json_response(
                {
                    "device_auth_id": "TEST_ONLY_DEVICE_AUTH",
                    "user_code": "TEST-CODE",
                    "interval": 3,
                    "expires_in": 900,
                }
            )
        if request.full_url.endswith("/api/accounts/deviceauth/token"):
            return _json_response(
                {
                    "authorization_code": "TEST_ONLY_OPENAI_AUTHORIZATION_CODE",
                    "code_verifier": "TEST_ONLY_OPENAI_CODE_VERIFIER",
                }
            )
        return _json_response(
            {
                "access_token": "TEST_ONLY_OPENAI_ACCESS",
                "refresh_token": "TEST_ONLY_OPENAI_REFRESH",
                "expires_in": 3600,
            }
        )

    started = start_memory_enhancement_oauth_flow("openai", store=store, opener=opener)
    result = poll_memory_enhancement_oauth_flow(started["flow_id"], store=store, opener=opener)
    credential = store.get("openai-memory", provider_id="openai")

    assert started["submit_mode"] == "poll"
    assert started["authorization_url"] == "https://auth.openai.com/codex/device"
    assert started["user_code"] == "TEST-CODE"
    assert result["status"] == "approved"
    assert credential.access_token == "TEST_ONLY_OPENAI_ACCESS"
    assert credential.refresh_token == "TEST_ONLY_OPENAI_REFRESH"
    assert credential.transport == "openai_codex"
    assert len(calls) == 3


def test_hermes_anthropic_login_persists_to_memory_pool(monkeypatch, tmp_path: Path):
    store = MemoryEnhancementOAuthStore(tmp_path / "auth.json")

    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_hermes_oauth._run_hermes_anthropic_oauth_login_pure",
        lambda: {
            "access_token": "TEST_ONLY_ANTHROPIC_ACCESS",
            "refresh_token": "TEST_ONLY_ANTHROPIC_REFRESH",
            "expires_at_ms": 1_800_000_000_000,
        },
    )

    result = run_hermes_memory_enhancement_oauth_login("anthropic", store=store)
    credential = store.get("anthropic-memory", provider_id="anthropic")

    assert result["status"] == "approved"
    assert credential.access_token == "TEST_ONLY_ANTHROPIC_ACCESS"
    assert credential.refresh_token == "TEST_ONLY_ANTHROPIC_REFRESH"
    assert credential.source == "manual:hermes_pkce"
    assert credential.transport == "anthropic_oauth"


def test_hermes_openai_login_persists_to_memory_pool(monkeypatch, tmp_path: Path):
    store = MemoryEnhancementOAuthStore(tmp_path / "auth.json")

    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_hermes_oauth._run_hermes_openai_codex_device_code_login",
        lambda: {
            "tokens": {
                "access_token": "TEST_ONLY_OPENAI_ACCESS",
                "refresh_token": "TEST_ONLY_OPENAI_REFRESH",
            },
            "base_url": "https://chatgpt.com/backend-api/codex",
            "last_refresh": "2026-05-17T00:00:00Z",
            "auth_mode": "chatgpt",
        },
    )

    result = run_hermes_memory_enhancement_oauth_login("openai", store=store)
    credential = store.get("openai-memory", provider_id="openai")

    assert result["status"] == "approved"
    assert credential.access_token == "TEST_ONLY_OPENAI_ACCESS"
    assert credential.refresh_token == "TEST_ONLY_OPENAI_REFRESH"
    assert credential.source == "device_code"
    assert credential.transport == "openai_codex"
    assert credential.base_url == "https://chatgpt.com/backend-api/codex"
    assert credential.extra["auth_mode"] == "chatgpt"


def test_hermes_google_login_persists_to_memory_pool(monkeypatch, tmp_path: Path):
    store = MemoryEnhancementOAuthStore(tmp_path / "auth.json")

    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_hermes_oauth._run_hermes_gemini_oauth_login_pure",
        lambda **_kwargs: {
            "access_token": "TEST_ONLY_GOOGLE_ACCESS",
            "refresh_token": "TEST_ONLY_GOOGLE_REFRESH",
            "expires_at_ms": 1_800_000_000_000,
            "email": "tester@example.invalid",
            "project_id": "project-test",
            "managed_project_id": "managed-project-test",
        },
    )

    result = run_hermes_memory_enhancement_oauth_login("google", store=store)
    credential = store.get("google-memory", provider_id="google")

    assert result["status"] == "approved"
    assert credential.access_token == "TEST_ONLY_GOOGLE_ACCESS"
    assert credential.refresh_token == "TEST_ONLY_GOOGLE_REFRESH"
    assert credential.source == "manual:hermes_google_pkce"
    assert credential.transport == "google_cloudcode"
    assert credential.project_id == "project-test"
    assert credential.account_label == "tester@example.invalid"
    assert credential.extra["managed_project_id"] == "managed-project-test"


def test_hermes_google_policy_decline_stops_before_oauth(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    monkeypatch.setattr(
        hermes_gemini_oauth,
        "start_oauth_flow",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("OAuth should not start")),
    )

    with pytest.raises(hermes_gemini_oauth.GoogleOAuthError) as exc:
        hermes_gemini_oauth.run_gemini_oauth_login_pure()

    assert exc.value.code == "google_oauth_cancelled"


def test_hermes_google_policy_accept_returns_pool_shape(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    monkeypatch.setattr(
        hermes_gemini_oauth,
        "start_oauth_flow",
        lambda **_kwargs: hermes_gemini_oauth.GoogleCredentials(
            access_token="TEST_ONLY_GOOGLE_ACCESS",
            refresh_token="TEST_ONLY_GOOGLE_REFRESH",
            expires_ms=1_800_000_000_000,
            email="tester@example.invalid",
            project_id="project-test",
        ),
    )

    result = hermes_gemini_oauth.run_gemini_oauth_login_pure()

    assert result == {
        "access_token": "TEST_ONLY_GOOGLE_ACCESS",
        "refresh_token": "TEST_ONLY_GOOGLE_REFRESH",
        "expires_at_ms": 1_800_000_000_000,
        "email": "tester@example.invalid",
        "project_id": "project-test",
    }


def test_hermes_google_login_accepts_project_hint(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    def fake_start_oauth_flow(**kwargs):
        captured.update(kwargs)
        return hermes_gemini_oauth.GoogleCredentials(
            access_token="TEST_ONLY_GOOGLE_ACCESS",
            refresh_token="TEST_ONLY_GOOGLE_REFRESH",
            expires_ms=1_800_000_000_000,
            project_id=str(kwargs.get("project_id") or ""),
        )

    monkeypatch.setattr(hermes_gemini_oauth, "start_oauth_flow", fake_start_oauth_flow)

    result = hermes_gemini_oauth.run_gemini_oauth_login_pure(project_id="project-explicit")

    assert captured["project_id"] == "project-explicit"
    assert result["project_id"] == "project-explicit"


def test_hermes_google_oauth_uses_loopback_before_paste_for_non_headless(monkeypatch):
    calls: list[str] = []

    class FakeServer:
        server_address = ("127.0.0.1", 18085)

        def serve_forever(self):
            calls.append("serve")

        def shutdown(self):
            calls.append("shutdown")

        def server_close(self):
            calls.append("close")

    class FakeReady:
        def wait(self, *, timeout):
            calls.append(f"wait:{timeout}")
            hermes_gemini_oauth._OAuthCallbackHandler.captured_code = "TEST_ONLY_GOOGLE_CODE"
            hermes_gemini_oauth._OAuthCallbackHandler.captured_error = None
            return True

    monkeypatch.setattr(hermes_gemini_oauth, "_is_headless", lambda: False)
    monkeypatch.setattr(hermes_gemini_oauth, "_require_client_id", lambda: "test-client.apps.googleusercontent.com")
    monkeypatch.setattr(hermes_gemini_oauth, "_get_client_secret", lambda: "")
    monkeypatch.setattr(hermes_gemini_oauth, "_generate_pkce_pair", lambda: ("verifier", "challenge"))
    monkeypatch.setattr(hermes_gemini_oauth.secrets, "token_urlsafe", lambda _n: "state-test")
    monkeypatch.setattr(hermes_gemini_oauth, "_bind_callback_server", lambda _port: (FakeServer(), 18085))
    monkeypatch.setattr(hermes_gemini_oauth.threading, "Event", lambda: FakeReady())
    monkeypatch.setattr(hermes_gemini_oauth.threading, "Thread", lambda **_kwargs: type("FakeThread", (), {"start": lambda self: None, "join": lambda self, timeout=None: None})())
    monkeypatch.setattr(
        hermes_gemini_oauth,
        "exchange_code",
        lambda code, verifier, redirect_uri, **_kwargs: {
            "access_token": "TEST_ONLY_GOOGLE_ACCESS",
            "refresh_token": "TEST_ONLY_GOOGLE_REFRESH",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(hermes_gemini_oauth, "_fetch_user_email", lambda _token: "tester@example.invalid")
    monkeypatch.setattr(hermes_gemini_oauth, "save_credentials", lambda creds: Path("unused"))

    creds = hermes_gemini_oauth.start_oauth_flow(force_relogin=True, open_browser=False)

    assert creds.access_token == "TEST_ONLY_GOOGLE_ACCESS"
    assert any(call.startswith("wait:") for call in calls)


def _flow_state(tmp_path: Path, flow_id: str):
    return json.loads((tmp_path / "oauth-flows" / f"{flow_id}.json").read_text(encoding="utf-8"))


def request_header(request: urllib.request.Request, name: str) -> str:
    for mapping in (request.headers, request.unredirected_hdrs):
        for key, value in mapping.items():
            if key.lower() == name.lower():
                return str(value)
    direct = request.get_header(name) or request.get_header(name.lower()) or request.get_header(name.title())
    if direct:
        return str(direct)
    return ""


class _json_response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")
