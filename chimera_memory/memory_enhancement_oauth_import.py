from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from .memory_enhancement_credentials import MemoryEnhancementCredentialResolutionError, ProtocolValidationError
from .memory_enhancement_oauth import (
    MemoryEnhancementOAuthCredential,
    MemoryEnhancementOAuthStore,
)


ANTHROPIC_OAUTH_NAME = "anthropic-memory"
GOOGLE_OAUTH_NAME = "google-memory"
OPENAI_OAUTH_NAME = "openai-memory"
OPENAI_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"


def import_memory_enhancement_oauth_credential(
    *,
    provider_id: str,
    source: str = "auto",
    name: str = "",
    store: MemoryEnhancementOAuthStore | None = None,
    hermes_home: str | Path | None = None,
    claude_credentials_path: str | Path | None = None,
    codex_auth_path: str | Path | None = None,
) -> MemoryEnhancementOAuthCredential:
    provider = provider_id.strip().lower().replace("-", "_")
    selected_source = source.strip().lower()
    if provider == "openai":
        credential = _import_openai(
            selected_source,
            name=name or OPENAI_OAUTH_NAME,
            hermes_home=hermes_home,
            codex_auth_path=codex_auth_path,
        )
    elif provider == "anthropic":
        credential = _import_anthropic(
            selected_source,
            name=name or ANTHROPIC_OAUTH_NAME,
            hermes_home=hermes_home,
            claude_credentials_path=claude_credentials_path,
        )
    elif provider == "google":
        credential = _import_google(
            selected_source,
            name=name or GOOGLE_OAUTH_NAME,
            hermes_home=hermes_home,
        )
    else:
        raise ProtocolValidationError("memory enhancement oauth import provider unsupported")
    target_store = store or MemoryEnhancementOAuthStore()
    target_store.upsert(credential)
    return credential


def _import_openai(
    source: str,
    *,
    name: str,
    hermes_home: str | Path | None,
    codex_auth_path: str | Path | None,
) -> MemoryEnhancementOAuthCredential:
    attempts = ("codex_cli", "hermes_auth_pool") if source == "auto" else (source,)
    for attempt in attempts:
        if attempt == "codex_cli":
            raw = _read_json(_codex_auth_path(codex_auth_path))
            credential = _openai_from_payload(name, raw)
            if credential is not None:
                return credential
        elif attempt == "hermes_auth_pool":
            raw = _hermes_auth_pool_entry(hermes_home, "openai-codex")
            credential = _openai_from_hermes_auth_pool(name, raw)
            if credential is not None:
                return credential
        else:
            raise ProtocolValidationError("memory enhancement openai oauth import source unsupported")
    raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth credential unavailable")


def _import_anthropic(
    source: str,
    *,
    name: str,
    hermes_home: str | Path | None,
    claude_credentials_path: str | Path | None,
) -> MemoryEnhancementOAuthCredential:
    attempts = ("hermes_pkce", "claude_code") if source == "auto" else (source,)
    for attempt in attempts:
        if attempt == "hermes_pkce":
            raw = _read_json(_hermes_home(hermes_home) / ".anthropic_oauth.json")
            credential = _anthropic_from_payload(name, raw, source="hermes_pkce")
            if credential is not None:
                return credential
        elif attempt == "claude_code":
            raw = _read_json(_claude_credentials_path(claude_credentials_path))
            credential = _anthropic_from_payload(name, raw, source="claude_code")
            if credential is not None:
                return credential
        else:
            raise ProtocolValidationError("memory enhancement anthropic oauth import source unsupported")
    raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth credential unavailable")


def _import_google(
    source: str,
    *,
    name: str,
    hermes_home: str | Path | None,
) -> MemoryEnhancementOAuthCredential:
    attempts = ("hermes_google", "hermes_auth_pool") if source == "auto" else (source,)
    for attempt in attempts:
        if attempt in {"hermes_google", "google_pkce"}:
            raw = _read_json(_hermes_home(hermes_home) / "auth" / "google_oauth.json")
            credential = _google_from_payload(name, raw)
            if credential is not None:
                return credential
        elif attempt == "hermes_auth_pool":
            raw = _hermes_auth_pool_entry(hermes_home, "gemini")
            credential = _google_from_hermes_auth_pool(name, raw)
            if credential is not None:
                return credential
        else:
            raise ProtocolValidationError("memory enhancement google oauth import source unsupported")
    raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth credential unavailable")


def _openai_from_payload(name: str, payload: Mapping[str, object] | None) -> MemoryEnhancementOAuthCredential | None:
    if not payload:
        return None
    tokens = payload.get("tokens") if isinstance(payload.get("tokens"), Mapping) else {}
    access_token = _first_text(tokens, ("access_token", "accessToken"))
    if not access_token:
        return None
    return MemoryEnhancementOAuthCredential(
        name=name,
        provider_id="openai",
        source="codex_cli",
        access_token=access_token,
        refresh_token=_first_text(tokens, ("refresh_token", "refreshToken")),
        transport="openai_codex",
        base_url=OPENAI_CODEX_BASE_URL,
        account_label=_first_text(tokens, ("account_id", "accountId")),
    )


def _openai_from_hermes_auth_pool(
    name: str,
    payload: Mapping[str, object] | None,
) -> MemoryEnhancementOAuthCredential | None:
    if not payload:
        return None
    access_token = _first_text(payload, ("access_token", "accessToken"))
    if not access_token:
        return None
    return MemoryEnhancementOAuthCredential(
        name=name,
        provider_id="openai",
        source="hermes_auth_pool",
        access_token=access_token,
        refresh_token=_first_text(payload, ("refresh_token", "refreshToken")),
        transport="openai_codex",
        base_url=_first_text(payload, ("base_url", "baseUrl")) or OPENAI_CODEX_BASE_URL,
        account_label=_first_text(payload, ("label", "account_label", "accountLabel")),
    )


def _anthropic_from_payload(
    name: str,
    payload: Mapping[str, object] | None,
    *,
    source: str,
) -> MemoryEnhancementOAuthCredential | None:
    if not payload:
        return None
    access_token = _first_text(payload, ("accessToken", "access_token"))
    if not access_token:
        return None
    return MemoryEnhancementOAuthCredential(
        name=name,
        provider_id="anthropic",
        source=source,
        access_token=access_token,
        refresh_token=_first_text(payload, ("refreshToken", "refresh_token")),
        expires_at_ms=_optional_int(payload.get("expiresAt") or payload.get("expires_at_ms")),
        transport="anthropic_oauth",
    )


def _google_from_payload(name: str, payload: Mapping[str, object] | None) -> MemoryEnhancementOAuthCredential | None:
    if not payload:
        return None
    access_token = str(payload.get("access") or "").strip()
    refresh_packed = str(payload.get("refresh") or "").strip()
    if not access_token or not refresh_packed:
        return None
    refresh_token, project_id, managed_project_id = _parse_google_refresh(refresh_packed)
    return MemoryEnhancementOAuthCredential(
        name=name,
        provider_id="google",
        source="hermes_google",
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_ms=_optional_int(payload.get("expires")),
        transport="google_cloudcode",
        project_id=project_id or managed_project_id,
        account_label=str(payload.get("email") or "").strip(),
        extra={"managed_project_id": managed_project_id} if managed_project_id else {},
    )


def _google_from_hermes_auth_pool(
    name: str,
    payload: Mapping[str, object] | None,
) -> MemoryEnhancementOAuthCredential | None:
    if not payload:
        return None
    access_token = _first_text(payload, ("access_token", "accessToken"))
    if not access_token:
        return None
    return MemoryEnhancementOAuthCredential(
        name=name,
        provider_id="google",
        source="hermes_auth_pool",
        access_token=access_token,
        refresh_token=_first_text(payload, ("refresh_token", "refreshToken")),
        transport="google_cloudcode",
        project_id=_first_text(payload, ("project_id", "projectId", "project")),
        account_label=_first_text(payload, ("label", "account_label", "accountLabel")),
        base_url=_first_text(payload, ("base_url", "baseUrl")),
    )


def _parse_google_refresh(value: str) -> tuple[str, str, str]:
    parts = value.split("|", 2)
    return (
        parts[0] if len(parts) > 0 else "",
        parts[1] if len(parts) > 1 else "",
        parts[2] if len(parts) > 2 else "",
    )


def _read_json(path: Path) -> Mapping[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _hermes_auth_pool_entry(hermes_home: str | Path | None, pool_key: str) -> Mapping[str, object] | None:
    payload = _read_json(_hermes_home(hermes_home) / "auth.json")
    pool = payload.get("credential_pool") if isinstance(payload, Mapping) else {}
    raw_entries = pool.get(pool_key) if isinstance(pool, Mapping) else None
    entries: list[Mapping[str, object]]
    if isinstance(raw_entries, Mapping):
        entries = [raw_entries]
    elif isinstance(raw_entries, list):
        entries = [entry for entry in raw_entries if isinstance(entry, Mapping)]
    else:
        entries = []
    entries.sort(key=lambda entry: _optional_int(entry.get("priority")) or 0)
    for entry in entries:
        if _first_text(entry, ("access_token", "accessToken")):
            return entry
    return None


def _hermes_home(path: str | Path | None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    configured = (
        os.environ.get("CHIMERA_MEMORY_HERMES_HOME")
        or os.environ.get("PERSONIFYAGENTS_PWA_HERMES_HOME")
        or os.environ.get("HERMES_HOME")
    )
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".hermes"


def _claude_credentials_path(path: str | Path | None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    return Path.home() / ".claude" / ".credentials.json"


def _codex_auth_path(path: str | Path | None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    configured = os.environ.get("CHIMERA_MEMORY_CODEX_AUTH_PATH") or os.environ.get("CODEX_AUTH_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return root / "auth.json"


def _first_text(payload: Mapping[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
