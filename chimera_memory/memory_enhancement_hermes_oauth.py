from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import time
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import httpx

from .memory_enhancement_credentials import MemoryEnhancementCredentialResolutionError, ProtocolValidationError
from .memory_enhancement_oauth import (
    MemoryEnhancementOAuthCredential,
    MemoryEnhancementOAuthStore,
    _credential_from_refresh_payload,
    _expires_at_ms_from_refresh_payload,
    require_valid_oauth_name,
    require_valid_oauth_provider_id,
)


# Vendored from Hermes OAuth login paths. Keep provider protocol constants and
# flow shape aligned with Hermes; CM owns only persistence into its credential pool.
ANTHROPIC_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
ANTHROPIC_OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
ANTHROPIC_OAUTH_SCOPES = "org:create_api_key user:profile user:inference"
CLAUDE_CODE_VERSION_FALLBACK = "2.1.74"

CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

_DEFAULT_OAUTH_NAMES = {
    "openai": "openai-memory",
    "anthropic": "anthropic-memory",
}

_claude_code_version_cache: str | None = None


class HermesMemoryOAuthError(RuntimeError):
    def __init__(self, message: str, *, provider: str = "", code: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code


def run_hermes_memory_enhancement_oauth_login(
    provider_id: str,
    *,
    name: str = "",
    store: MemoryEnhancementOAuthStore | None = None,
) -> dict[str, Any]:
    provider_id = _provider_id(provider_id)
    credential_name = _credential_name(provider_id, name)
    target_store = store or MemoryEnhancementOAuthStore()

    if provider_id == "anthropic":
        creds = _run_hermes_anthropic_oauth_login_pure()
        if not creds:
            raise MemoryEnhancementCredentialResolutionError("memory enhancement anthropic oauth login returned no credentials")
        credential = _credential_from_hermes_payload(
            provider_id=provider_id,
            name=credential_name,
            source="manual:hermes_pkce",
            transport="anthropic_oauth",
            payload=creds,
        )
    elif provider_id == "openai":
        creds = _run_hermes_openai_codex_device_code_login()
        tokens = creds.get("tokens") if isinstance(creds, Mapping) else None
        if not isinstance(tokens, Mapping):
            raise MemoryEnhancementCredentialResolutionError("memory enhancement openai oauth login returned no credentials")
        credential = _credential_from_hermes_payload(
            provider_id=provider_id,
            name=credential_name,
            source="device_code",
            transport="openai_codex",
            payload=tokens,
            base_url=str(creds.get("base_url") or OPENAI_CODEX_BASE_URL),
            extra={"last_refresh": str(creds.get("last_refresh") or ""), "auth_mode": str(creds.get("auth_mode") or "chatgpt")},
        )
    else:
        raise ProtocolValidationError("memory enhancement hermes oauth provider unsupported")

    target_store.upsert(credential)
    return {
        "status": "approved",
        "provider_id": credential.provider_id,
        "name": credential.name,
        "credential": credential.to_safe_dict(),
    }


def _run_hermes_anthropic_oauth_login_pure() -> Mapping[str, Any] | None:
    """Hermes-native Anthropic OAuth PKCE flow, vendored for CM standalone use."""
    verifier, challenge = _generate_pkce()

    params = {
        "code": "true",
        "client_id": ANTHROPIC_OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": ANTHROPIC_OAUTH_REDIRECT_URI,
        "scope": ANTHROPIC_OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    }

    auth_url = f"https://claude.ai/oauth/authorize?{urllib.parse.urlencode(params)}"

    print()
    print("Authorize Hermes with your Claude Pro/Max subscription.")
    print()
    print("Open this link in your browser:")
    print()
    print(f"  {auth_url}")
    print()

    try:
        webbrowser.open(auth_url)
        print("  (Browser opened automatically)")
    except Exception:
        pass

    print()
    print("After authorizing, you'll see a code. Paste it below.")
    print()
    try:
        auth_code = input("Authorization code: ").strip()
    except (KeyboardInterrupt, EOFError):
        return None

    if not auth_code:
        print("No code entered.")
        return None

    splits = auth_code.split("#")
    code = splits[0]
    state = splits[1] if len(splits) > 1 else ""

    try:
        exchange_data = json.dumps(
            {
                "grant_type": "authorization_code",
                "client_id": ANTHROPIC_OAUTH_CLIENT_ID,
                "code": code,
                "state": state,
                "redirect_uri": ANTHROPIC_OAUTH_REDIRECT_URI,
                "code_verifier": verifier,
            }
        ).encode()

        req = urllib.request.Request(
            ANTHROPIC_OAUTH_TOKEN_URL,
            data=exchange_data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"claude-cli/{_get_claude_code_version()} (external, cli)",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
    except Exception as exc:
        print(f"Token exchange failed: {exc}")
        return None

    access_token = result.get("access_token", "")
    refresh_token = result.get("refresh_token", "")
    expires_in = result.get("expires_in", 3600)

    if not access_token:
        print("No access token in response.")
        return None

    expires_at_ms = int(time.time() * 1000) + (expires_in * 1000)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at_ms": expires_at_ms,
    }


def _run_hermes_openai_codex_device_code_login() -> Mapping[str, Any]:
    """Hermes OpenAI Codex device-code flow, vendored for CM standalone use."""
    issuer = "https://auth.openai.com"
    client_id = CODEX_OAUTH_CLIENT_ID

    try:
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.post(
                f"{issuer}/api/accounts/deviceauth/usercode",
                json={"client_id": client_id},
                headers={"Content-Type": "application/json"},
            )
    except Exception as exc:
        raise HermesMemoryOAuthError(
            f"Failed to request device code: {exc}",
            provider="openai-codex",
            code="device_code_request_failed",
        ) from exc

    if resp.status_code != 200:
        raise HermesMemoryOAuthError(
            f"Device code request returned status {resp.status_code}.",
            provider="openai-codex",
            code="device_code_request_error",
        )

    device_data = resp.json()
    user_code = device_data.get("user_code", "")
    device_auth_id = device_data.get("device_auth_id", "")
    poll_interval = max(3, int(device_data.get("interval", "5")))

    if not user_code or not device_auth_id:
        raise HermesMemoryOAuthError(
            "Device code response missing required fields.",
            provider="openai-codex",
            code="device_code_incomplete",
        )

    print("To continue, follow these steps:\n")
    print("  1. Open this URL in your browser:")
    print(f"     \033[94m{issuer}/codex/device\033[0m\n")
    print("  2. Enter this code:")
    print(f"     \033[94m{user_code}\033[0m\n")
    print("Waiting for sign-in... (press Ctrl+C to cancel)")

    max_wait = 15 * 60
    start = time.monotonic()
    code_resp = None

    try:
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            while time.monotonic() - start < max_wait:
                time.sleep(poll_interval)
                poll_resp = client.post(
                    f"{issuer}/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )

                if poll_resp.status_code == 200:
                    code_resp = poll_resp.json()
                    break
                if poll_resp.status_code in (403, 404):
                    continue
                raise HermesMemoryOAuthError(
                    f"Device auth polling returned status {poll_resp.status_code}.",
                    provider="openai-codex",
                    code="device_code_poll_error",
                )
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
        raise SystemExit(130) from None

    if code_resp is None:
        raise HermesMemoryOAuthError(
            "Login timed out after 15 minutes.",
            provider="openai-codex",
            code="device_code_timeout",
        )

    authorization_code = code_resp.get("authorization_code", "")
    code_verifier = code_resp.get("code_verifier", "")
    redirect_uri = f"{issuer}/deviceauth/callback"

    if not authorization_code or not code_verifier:
        raise HermesMemoryOAuthError(
            "Device auth response missing authorization_code or code_verifier.",
            provider="openai-codex",
            code="device_code_incomplete_exchange",
        )

    try:
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            token_resp = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:
        raise HermesMemoryOAuthError(
            f"Token exchange failed: {exc}",
            provider="openai-codex",
            code="token_exchange_failed",
        ) from exc

    if token_resp.status_code != 200:
        raise HermesMemoryOAuthError(
            f"Token exchange returned status {token_resp.status_code}.",
            provider="openai-codex",
            code="token_exchange_error",
        )

    tokens = token_resp.json()
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")

    if not access_token:
        raise HermesMemoryOAuthError(
            "Token exchange did not return an access_token.",
            provider="openai-codex",
            code="token_exchange_no_access_token",
        )

    base_url = os.getenv("HERMES_CODEX_BASE_URL", "").strip().rstrip("/") or OPENAI_CODEX_BASE_URL

    return {
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
        },
        "base_url": base_url,
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "auth_mode": "chatgpt",
        "source": "device-code",
    }


def _generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _get_claude_code_version() -> str:
    global _claude_code_version_cache
    if _claude_code_version_cache is None:
        _claude_code_version_cache = _detect_claude_code_version()
    return _claude_code_version_cache


def _detect_claude_code_version() -> str:
    for cmd in ("claude", "claude-code"):
        try:
            result = subprocess.run(
                [cmd, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                version = result.stdout.strip().split()[0]
                if version and version[0].isdigit():
                    return version
        except Exception:
            pass
    return CLAUDE_CODE_VERSION_FALLBACK


def _credential_from_hermes_payload(
    *,
    provider_id: str,
    name: str,
    source: str,
    transport: str,
    payload: Mapping[str, Any],
    base_url: str = "",
    extra: Mapping[str, Any] | None = None,
) -> MemoryEnhancementOAuthCredential:
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


def _provider_id(provider_id: str) -> str:
    text = provider_id.strip().lower()
    if text == "openai-codex":
        text = "openai"
    require_valid_oauth_provider_id(text)
    if text not in _DEFAULT_OAUTH_NAMES:
        raise ProtocolValidationError("memory enhancement hermes oauth provider unsupported")
    return text


def _credential_name(provider_id: str, name: str) -> str:
    credential_name = name.strip() or _DEFAULT_OAUTH_NAMES[provider_id]
    require_valid_oauth_name(credential_name)
    return credential_name


def _payload_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""
