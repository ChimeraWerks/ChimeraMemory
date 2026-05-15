from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from chimera_memory.memory_enhancement_oauth import MemoryEnhancementOAuthStore
from chimera_memory.memory_enhancement_oauth_flow import (
    poll_memory_enhancement_oauth_flow,
    start_memory_enhancement_oauth_flow,
    submit_memory_enhancement_oauth_flow,
)


def test_anthropic_browser_oauth_flow_persists_refreshable_credential(tmp_path: Path):
    store = MemoryEnhancementOAuthStore(tmp_path / "auth.json")
    started = start_memory_enhancement_oauth_flow("anthropic", store=store)
    flow_state = _flow_state(tmp_path, started["flow_id"])
    captured: dict[str, object] = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
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
    started = start_memory_enhancement_oauth_flow("google", store=store)
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


def _flow_state(tmp_path: Path, flow_id: str):
    return json.loads((tmp_path / "oauth-flows" / f"{flow_id}.json").read_text(encoding="utf-8"))


class _json_response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")
