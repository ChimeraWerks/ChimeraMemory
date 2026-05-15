from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .memory_enhancement_credentials import MemoryEnhancementCredentialResolutionError, ProtocolValidationError
from .memory_enhancement_oauth import (
    ANTHROPIC_OAUTH_CLIENT_ID,
    GOOGLE_OAUTH_TOKEN_ENDPOINT,
    MemoryEnhancementOAuthCredential,
    MemoryEnhancementOAuthStore,
    OPENAI_CODEX_OAUTH_CLIENT_ID,
    OPENAI_CODEX_OAUTH_TOKEN_ENDPOINT,
    _atomic_write_secret_text,
    _chmod_owner_only,
    _credential_from_refresh_payload,
    _expires_at_ms_from_refresh_payload,
    _google_oauth_client_credentials,
    _post_form_json,
    require_valid_oauth_name,
    require_valid_oauth_provider_id,
)


OAUTH_FLOW_TTL_SECONDS = 15 * 60

ANTHROPIC_OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
ANTHROPIC_OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
ANTHROPIC_OAUTH_SCOPE = "org:create_api_key user:profile user:inference"
ANTHROPIC_OAUTH_TOKEN_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"

GOOGLE_OAUTH_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_REDIRECT_URI = "http://127.0.0.1:8085/oauth2callback"
GOOGLE_OAUTH_SCOPE = " ".join(
    (
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    )
)

OPENAI_DEVICE_USERCODE_ENDPOINT = "https://auth.openai.com/api/accounts/deviceauth/usercode"
OPENAI_DEVICE_TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/deviceauth/token"
OPENAI_DEVICE_VERIFICATION_URI = "https://auth.openai.com/codex/device"
OPENAI_DEVICE_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
OPENAI_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

_DEFAULT_OAUTH_NAMES = {
    "openai": "openai-memory",
    "anthropic": "anthropic-memory",
    "google": "google-memory",
}


def start_memory_enhancement_oauth_flow(
    provider_id: str,
    *,
    name: str = "",
    store: MemoryEnhancementOAuthStore | None = None,
    project_id: str = "",
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Create a first-time OAuth setup flow without exposing credential material."""
    provider_id = _provider_id(provider_id)
    credential_name = _credential_name(provider_id, name)
    target_store = store or MemoryEnhancementOAuthStore()
    flow_id = _new_flow_id()
    now_ms = int(time.time() * 1000)

    if provider_id == "anthropic":
        verifier, challenge = _pkce_pair()
        state = verifier
        authorization_url = ANTHROPIC_OAUTH_AUTHORIZE_URL + "?" + urllib.parse.urlencode(
            {
                "code": "true",
                "client_id": ANTHROPIC_OAUTH_CLIENT_ID,
                "response_type": "code",
                "redirect_uri": ANTHROPIC_OAUTH_REDIRECT_URI,
                "scope": ANTHROPIC_OAUTH_SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
        _save_flow_state(
            target_store,
            flow_id,
            {
                "provider_id": provider_id,
                "name": credential_name,
                "flow": "pkce",
                "created_at_ms": now_ms,
                "expires_at_ms": now_ms + OAUTH_FLOW_TTL_SECONDS * 1000,
                "code_verifier": verifier,
                "state": state,
                "redirect_uri": ANTHROPIC_OAUTH_REDIRECT_URI,
            },
        )
        return _start_result(
            provider_id=provider_id,
            name=credential_name,
            flow_id=flow_id,
            authorization_url=authorization_url,
            submit_mode="paste_code",
            expires_at_ms=now_ms + OAUTH_FLOW_TTL_SECONDS * 1000,
        )

    if provider_id == "google":
        client_id, client_secret = _google_oauth_client_credentials({})
        if not client_id:
            raise MemoryEnhancementCredentialResolutionError("memory enhancement google oauth client unavailable")
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(16)
        resolved_project_id = project_id.strip() or _google_project_id_from_env()
        authorization_url = GOOGLE_OAUTH_AUTHORIZE_URL + "?" + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
                "response_type": "code",
                "scope": GOOGLE_OAUTH_SCOPE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        _save_flow_state(
            target_store,
            flow_id,
            {
                "provider_id": provider_id,
                "name": credential_name,
                "flow": "pkce",
                "created_at_ms": now_ms,
                "expires_at_ms": now_ms + OAUTH_FLOW_TTL_SECONDS * 1000,
                "code_verifier": verifier,
                "state": state,
                "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
                "project_id": resolved_project_id,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        return _start_result(
            provider_id=provider_id,
            name=credential_name,
            flow_id=flow_id,
            authorization_url=authorization_url,
            submit_mode="paste_redirect_url",
            expires_at_ms=now_ms + OAUTH_FLOW_TTL_SECONDS * 1000,
        )

    device_payload = _post_json(
        OPENAI_DEVICE_USERCODE_ENDPOINT,
        {"client_id": OPENAI_CODEX_OAUTH_CLIENT_ID},
        opener=opener,
        timeout_seconds=15,
    )
    user_code = _payload_text(device_payload, "user_code")
    device_auth_id = _payload_text(device_payload, "device_auth_id")
    if not user_code or not device_auth_id:
        raise MemoryEnhancementCredentialResolutionError("memory enhancement openai oauth device flow unavailable")
    interval = _payload_int(device_payload, "interval", 5)
    expires_in = max(60, _payload_int(device_payload, "expires_in", OAUTH_FLOW_TTL_SECONDS))
    expires_at_ms = now_ms + expires_in * 1000
    _save_flow_state(
        target_store,
        flow_id,
        {
            "provider_id": provider_id,
            "name": credential_name,
            "flow": "device_code",
            "created_at_ms": now_ms,
            "expires_at_ms": expires_at_ms,
            "device_auth_id": device_auth_id,
            "user_code": user_code,
            "interval": max(3, interval),
        },
    )
    return _start_result(
        provider_id=provider_id,
        name=credential_name,
        flow_id=flow_id,
        authorization_url=OPENAI_DEVICE_VERIFICATION_URI,
        submit_mode="poll",
        expires_at_ms=expires_at_ms,
        user_code=user_code,
        poll_after_seconds=max(3, interval),
    )


def submit_memory_enhancement_oauth_flow(
    flow_id: str,
    callback_value: str,
    *,
    store: MemoryEnhancementOAuthStore | None = None,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    target_store = store or MemoryEnhancementOAuthStore()
    state = _load_flow_state(target_store, flow_id)
    _require_unexpired_flow(state)
    provider_id = _provider_id(str(state.get("provider_id") or ""))
    if state.get("flow") != "pkce":
        raise ProtocolValidationError("memory enhancement oauth flow must be polled")
    code, callback_state = _parse_callback_code(callback_value)
    if not code:
        raise ProtocolValidationError("memory enhancement oauth callback code is required")
    expected_state = str(state.get("state") or "")
    if callback_state and callback_state != expected_state:
        raise ProtocolValidationError("memory enhancement oauth callback state mismatch")

    if provider_id == "anthropic":
        payload = _post_json(
            ANTHROPIC_OAUTH_TOKEN_ENDPOINT,
            {
                "grant_type": "authorization_code",
                "client_id": ANTHROPIC_OAUTH_CLIENT_ID,
                "code": code,
                "state": callback_state or expected_state,
                "redirect_uri": ANTHROPIC_OAUTH_REDIRECT_URI,
                "code_verifier": str(state.get("code_verifier") or ""),
            },
            headers={"User-Agent": "chimera-memory-oauth/1.0"},
            opener=opener,
            timeout_seconds=20,
        )
        credential = _credential_from_authorization_payload(
            state,
            payload,
            source="browser:anthropic_pkce",
            transport="anthropic_oauth",
        )
    elif provider_id == "google":
        token_request = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
            "client_id": str(state.get("client_id") or ""),
            "code_verifier": str(state.get("code_verifier") or ""),
        }
        client_secret = str(state.get("client_secret") or "")
        if client_secret:
            token_request["client_secret"] = client_secret
        payload = _post_form_json(
            GOOGLE_OAUTH_TOKEN_ENDPOINT,
            token_request,
            headers={"Accept": "application/json"},
            opener=opener,
            timeout_seconds=20,
        )
        credential = _credential_from_authorization_payload(
            state,
            payload,
            source="browser:google_pkce",
            transport="google_cloudcode",
            project_id=str(state.get("project_id") or ""),
            extra={
                "client_id": str(state.get("client_id") or ""),
                "client_secret": str(state.get("client_secret") or ""),
            },
        )
    else:
        raise ProtocolValidationError("memory enhancement oauth provider unsupported")

    target_store.upsert(credential)
    _delete_flow_state(target_store, flow_id)
    return _approved_result(credential)


def poll_memory_enhancement_oauth_flow(
    flow_id: str,
    *,
    store: MemoryEnhancementOAuthStore | None = None,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    target_store = store or MemoryEnhancementOAuthStore()
    state = _load_flow_state(target_store, flow_id)
    _require_unexpired_flow(state)
    provider_id = _provider_id(str(state.get("provider_id") or ""))
    if provider_id != "openai" or state.get("flow") != "device_code":
        raise ProtocolValidationError("memory enhancement oauth flow does not support polling")

    poll_payload = _post_json(
        OPENAI_DEVICE_TOKEN_ENDPOINT,
        {
            "device_auth_id": str(state.get("device_auth_id") or ""),
            "user_code": str(state.get("user_code") or ""),
        },
        opener=opener,
        timeout_seconds=15,
        pending_statuses={403, 404},
    )
    if poll_payload.get("_pending"):
        return {
            "status": "pending",
            "provider_id": provider_id,
            "flow_id": flow_id,
            "poll_after_seconds": _payload_int(state, "interval", 5),
        }

    authorization_code = _payload_text(poll_payload, "authorization_code")
    code_verifier = _payload_text(poll_payload, "code_verifier")
    if not authorization_code or not code_verifier:
        raise MemoryEnhancementCredentialResolutionError("memory enhancement openai oauth device flow unavailable")

    token_payload = _post_form_json(
        OPENAI_CODEX_OAUTH_TOKEN_ENDPOINT,
        {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": OPENAI_DEVICE_REDIRECT_URI,
            "client_id": OPENAI_CODEX_OAUTH_CLIENT_ID,
            "code_verifier": code_verifier,
        },
        headers={"Accept": "application/json"},
        opener=opener,
        timeout_seconds=20,
    )
    credential = _credential_from_authorization_payload(
        state,
        token_payload,
        source="browser:openai_device",
        transport="openai_codex",
        base_url=OPENAI_CODEX_BASE_URL,
    )
    target_store.upsert(credential)
    _delete_flow_state(target_store, flow_id)
    return _approved_result(credential)


def _credential_from_authorization_payload(
    state: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    source: str,
    transport: str,
    base_url: str = "",
    project_id: str = "",
    extra: Mapping[str, Any] | None = None,
) -> MemoryEnhancementOAuthCredential:
    provider_id = _provider_id(str(state.get("provider_id") or ""))
    name = _credential_name(provider_id, str(state.get("name") or ""))
    access_token = _payload_text(payload, "access_token")
    refresh_token = _payload_text(payload, "refresh_token")
    if not access_token or not refresh_token:
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth token response unavailable")
    seed = MemoryEnhancementOAuthCredential(
        name=name,
        provider_id=provider_id,
        source=source,
        access_token="pending",
        refresh_token=refresh_token,
        transport=transport,
        base_url=base_url,
        project_id=project_id,
        extra=dict(extra or {}),
    )
    return _credential_from_refresh_payload(
        seed,
        {
            **dict(payload),
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at_ms": _expires_at_ms_from_refresh_payload(payload, access_token=access_token),
        },
    )


def _post_json(
    url: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    opener: Callable[..., Any] | None,
    timeout_seconds: int,
    pending_statuses: set[int] | None = None,
) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(dict(payload)).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **dict(headers or {}),
        },
        method="POST",
    )
    target = opener or urllib.request.urlopen
    try:
        response = target(request, timeout=timeout_seconds)
        if hasattr(response, "__enter__"):
            with response as handle:
                raw = handle.read()
        else:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if pending_statuses and exc.code in pending_statuses:
            return {"_pending": True}
        if exc.code in {400, 401, 403}:
            raise MemoryEnhancementCredentialResolutionError(
                "memory enhancement oauth authorization rejected; re-run setup"
            ) from exc
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth authorization unavailable") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth authorization unavailable") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth response unavailable") from exc
    if not isinstance(parsed, Mapping):
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth response unavailable")
    return parsed


def _parse_callback_code(value: str) -> tuple[str, str]:
    text = value.strip()
    if not text:
        return "", ""
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urllib.parse.urlparse(text)
        query = urllib.parse.parse_qs(parsed.query)
        fragment = urllib.parse.parse_qs(parsed.fragment)
        return (query.get("code") or fragment.get("code") or [""])[0], (
            query.get("state") or fragment.get("state") or [""]
        )[0]
    if text.startswith("?"):
        query = urllib.parse.parse_qs(text[1:])
        return (query.get("code") or [""])[0], (query.get("state") or [""])[0]
    if "#" in text:
        code, state = text.split("#", 1)
        return code.strip(), state.strip()
    return text, ""


def _start_result(
    *,
    provider_id: str,
    name: str,
    flow_id: str,
    authorization_url: str,
    submit_mode: str,
    expires_at_ms: int,
    user_code: str = "",
    poll_after_seconds: int = 0,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "started",
        "provider_id": provider_id,
        "name": name,
        "flow_id": flow_id,
        "authorization_url": authorization_url,
        "submit_mode": submit_mode,
        "expires_at_ms": expires_at_ms,
    }
    if user_code:
        result["user_code"] = user_code
    if poll_after_seconds:
        result["poll_after_seconds"] = poll_after_seconds
    return result


def _approved_result(credential: MemoryEnhancementOAuthCredential) -> dict[str, Any]:
    return {
        "status": "approved",
        "provider_id": credential.provider_id,
        "name": credential.name,
        "credential": credential.to_safe_dict(),
    }


def _provider_id(provider_id: str) -> str:
    text = provider_id.strip().lower()
    require_valid_oauth_provider_id(text)
    return text


def _credential_name(provider_id: str, name: str) -> str:
    credential_name = name.strip() or _DEFAULT_OAUTH_NAMES[provider_id]
    require_valid_oauth_name(credential_name)
    return credential_name


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).decode("ascii").rstrip("=")
    return verifier, challenge


def _new_flow_id() -> str:
    return secrets.token_urlsafe(18)


def _flow_state_path(store: MemoryEnhancementOAuthStore, flow_id: str) -> Path:
    if not flow_id or not all(ch.isalnum() or ch in {"-", "_"} for ch in flow_id):
        raise ProtocolValidationError("memory enhancement oauth flow id is invalid")
    return store.path.parent / "oauth-flows" / f"{flow_id}.json"


def _save_flow_state(store: MemoryEnhancementOAuthStore, flow_id: str, payload: Mapping[str, Any]) -> None:
    path = _flow_state_path(store, flow_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_owner_only(path.parent, directory=True)
    _atomic_write_secret_text(path, json.dumps(dict(payload), indent=2, sort_keys=True) + "\n")


def _load_flow_state(store: MemoryEnhancementOAuthStore, flow_id: str) -> Mapping[str, Any]:
    path = _flow_state_path(store, flow_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth flow unavailable") from exc
    if not isinstance(payload, Mapping):
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth flow unavailable")
    return payload


def _delete_flow_state(store: MemoryEnhancementOAuthStore, flow_id: str) -> None:
    try:
        _flow_state_path(store, flow_id).unlink()
    except OSError:
        return


def _require_unexpired_flow(state: Mapping[str, Any]) -> None:
    expires_at_ms = _payload_int(state, "expires_at_ms", 0)
    if expires_at_ms <= int(time.time() * 1000):
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth flow expired")


def _payload_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _payload_int(payload: Mapping[str, Any], key: str, default: int) -> int:
    try:
        return int(payload.get(key, default))
    except (TypeError, ValueError):
        return default


def _google_project_id_from_env() -> str:
    for name in (
        "CHIMERA_MEMORY_GOOGLE_CLOUD_PROJECT",
        "PERSONIFYAGENTS_GOOGLE_CLOUD_PROJECT",
        "HERMES_GEMINI_PROJECT_ID",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_PROJECT_ID",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""
