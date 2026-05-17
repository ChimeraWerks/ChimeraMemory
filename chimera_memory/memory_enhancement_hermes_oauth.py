from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .memory_enhancement_credentials import MemoryEnhancementCredentialResolutionError, ProtocolValidationError
from .memory_enhancement_oauth import (
    MemoryEnhancementOAuthCredential,
    MemoryEnhancementOAuthStore,
    _credential_from_refresh_payload,
    _expires_at_ms_from_refresh_payload,
    require_valid_oauth_name,
    require_valid_oauth_provider_id,
)


OPENAI_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

_DEFAULT_OAUTH_NAMES = {
    "openai": "openai-memory",
    "anthropic": "anthropic-memory",
}


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
    with _hermes_import_path():
        from agent import anthropic_adapter as anthropic_mod  # type: ignore[import-not-found]

        return anthropic_mod.run_hermes_oauth_login_pure()


def _run_hermes_openai_codex_device_code_login() -> Mapping[str, Any]:
    with _hermes_import_path():
        from hermes_cli import auth as auth_mod  # type: ignore[import-not-found]

        return auth_mod._codex_device_code_login()


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


@contextmanager
def _hermes_import_path():
    root = _hermes_agent_root()
    if root is None:
        raise MemoryEnhancementCredentialResolutionError("Hermes agent install not found for memory enhancement oauth")
    text_root = str(root)
    inserted = False
    if text_root not in sys.path:
        sys.path.insert(0, text_root)
        inserted = True
    try:
        yield
    finally:
        if inserted:
            try:
                sys.path.remove(text_root)
            except ValueError:
                pass


def _hermes_agent_root() -> Path | None:
    candidates: list[Path] = []
    explicit = os.environ.get("HERMES_AGENT_ROOT", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates.append(Path(local_app_data) / "hermes" / "hermes-agent")
    candidates.append(Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent")
    for candidate in candidates:
        if (candidate / "hermes_cli" / "auth.py").is_file() and (candidate / "agent" / "anthropic_adapter.py").is_file():
            return candidate.resolve()
    return None


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
