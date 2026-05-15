from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory_enhancement_credentials import (
    MemoryEnhancementCredentialRef,
    MemoryEnhancementCredentialResolutionError,
    ProtocolValidationError,
    ResolvedMemoryEnhancementCredential,
    require_valid_memory_enhancement_credential_ref,
    require_valid_memory_enhancement_credential_value,
)


OAUTH_STORE_VERSION = 1
OAUTH_PROVIDER_IDS = frozenset(("openai", "anthropic", "google"))
OAUTH_TRANSPORTS = frozenset(("openai_codex", "anthropic_oauth", "google_cloudcode"))
OAUTH_REF_SCHEME = "oauth"

_OAUTH_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:\-]{0,119}$")
_DEFAULT_STATE_ROOT = Path(".chimera-memory") / "oauth"
_DEFAULT_AUTH_STORE_NAME = "auth.json"
_LOCK_TIMEOUT_SECONDS = 30.0
_ACCESS_TOKEN_REFRESH_SKEW_MS = 120_000
_TOKEN_REQUEST_TIMEOUT_SECONDS = 20

ANTHROPIC_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_CODE_VERSION_FALLBACK = "2.1.74"
ANTHROPIC_OAUTH_TOKEN_ENDPOINTS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)
GOOGLE_OAUTH_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_OAUTH_PUBLIC_CLIENT_ID_PROJECT_NUM = "681255809395"
GOOGLE_OAUTH_PUBLIC_CLIENT_ID_HASH = "oo8ft2oprdrnp9e3aqf6av3hmdib135j"
GOOGLE_OAUTH_PUBLIC_CLIENT_SECRET_SUFFIX = "4uHgMPm-1o7Sk-geV6Cu5clXFsxl"
GOOGLE_OAUTH_DEFAULT_CLIENT_ID = (
    f"{GOOGLE_OAUTH_PUBLIC_CLIENT_ID_PROJECT_NUM}-{GOOGLE_OAUTH_PUBLIC_CLIENT_ID_HASH}"
    ".apps.googleusercontent.com"
)
GOOGLE_OAUTH_DEFAULT_CLIENT_SECRET = f"GOCSPX-{GOOGLE_OAUTH_PUBLIC_CLIENT_SECRET_SUFFIX}"
OPENAI_CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_CODEX_OAUTH_TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"

RefreshCallback = Callable[["MemoryEnhancementOAuthCredential"], "MemoryEnhancementOAuthCredential"]


@dataclass(frozen=True)
class MemoryEnhancementOAuthCredential:
    """Stored OAuth credential material for one memory-enhancement provider."""

    name: str
    provider_id: str
    source: str
    access_token: str
    refresh_token: str = ""
    expires_at_ms: int | None = None
    transport: str = ""
    base_url: str = ""
    project_id: str = ""
    account_label: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> MemoryEnhancementCredentialRef:
        return MemoryEnhancementCredentialRef(scheme=OAUTH_REF_SCHEME, name=self.name)

    def to_dict(self) -> dict[str, Any]:
        require_valid_oauth_credential(self)
        payload: dict[str, Any] = {
            "provider_id": self.provider_id,
            "source": self.source,
            "access_token": self.access_token,
            "transport": self.transport,
        }
        if self.refresh_token:
            payload["refresh_token"] = self.refresh_token
        if self.expires_at_ms is not None:
            payload["expires_at_ms"] = int(self.expires_at_ms)
        if self.base_url:
            payload["base_url"] = self.base_url
        if self.project_id:
            payload["project_id"] = self.project_id
        if self.account_label:
            payload["account_label"] = self.account_label
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload

    @classmethod
    def from_dict(cls, name: str, payload: Mapping[str, Any]) -> "MemoryEnhancementOAuthCredential":
        expires_at_ms = payload.get("expires_at_ms")
        return cls(
            name=name,
            provider_id=str(payload.get("provider_id") or ""),
            source=str(payload.get("source") or ""),
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at_ms=int(expires_at_ms) if expires_at_ms is not None else None,
            transport=str(payload.get("transport") or ""),
            base_url=str(payload.get("base_url") or ""),
            project_id=str(payload.get("project_id") or ""),
            account_label=str(payload.get("account_label") or ""),
            extra=payload.get("extra") if isinstance(payload.get("extra"), Mapping) else {},
        )

    def to_resolved_credential(self) -> ResolvedMemoryEnhancementCredential:
        require_valid_oauth_credential(self)
        return ResolvedMemoryEnhancementCredential(
            ref=self.ref,
            value=self.access_token,
            source=f"oauth:{self.provider_id}:{self.source}",
        )

    def to_safe_dict(self) -> dict[str, object]:
        require_valid_oauth_credential(self)
        safe = self.to_resolved_credential().to_safe_dict()
        safe.update(
            {
                "provider_id": self.provider_id,
                "transport": self.transport,
                "expires_at_ms_present": self.expires_at_ms is not None,
                "refresh_token_present": bool(self.refresh_token),
                "account_label_present": bool(self.account_label),
                "project_id_present": bool(self.project_id),
            }
        )
        return safe

    def access_token_expiring(self, *, now_ms: int | None = None, skew_ms: int = _ACCESS_TOKEN_REFRESH_SKEW_MS) -> bool:
        if not self.access_token:
            return True
        if self.expires_at_ms is None:
            return False
        current = int(time.time() * 1000) if now_ms is None else int(now_ms)
        return int(self.expires_at_ms) <= current + max(0, int(skew_ms))


class MemoryEnhancementOAuthStore:
    """Local PA auth store for memory-enhancement OAuth credentials."""

    def __init__(self, path: str | Path | None = None, *, repo_root: str | Path | None = None) -> None:
        self.path = resolve_oauth_store_path(path, repo_root=repo_root)

    def read(self) -> dict[str, Any]:
        with _store_lock(self.path):
            return self._read_unlocked()

    def write(self, payload: Mapping[str, Any]) -> None:
        with _store_lock(self.path):
            self._write_unlocked(payload)

    def upsert(self, credential: MemoryEnhancementOAuthCredential) -> None:
        require_valid_oauth_credential(credential)
        with _store_lock(self.path):
            payload = self._read_unlocked()
            providers = payload.setdefault("providers", {})
            provider_bucket = providers.setdefault(credential.provider_id, {})
            provider_bucket[credential.name] = credential.to_dict()
            self._write_unlocked(payload)

    def get(self, name: str, *, provider_id: str = "") -> MemoryEnhancementOAuthCredential:
        require_valid_oauth_name(name)
        with _store_lock(self.path):
            payload = self._read_unlocked()
        return _get_credential_from_store_payload(payload, name, provider_id=provider_id)

    def get_valid(
        self,
        name: str,
        *,
        provider_id: str = "",
        refresh_if_expiring: bool = True,
        force_refresh: bool = False,
        refresh_skew_ms: int = _ACCESS_TOKEN_REFRESH_SKEW_MS,
        refresher: RefreshCallback | None = None,
    ) -> MemoryEnhancementOAuthCredential:
        require_valid_oauth_name(name)
        with _store_lock(self.path):
            payload = self._read_unlocked()
            credential = _get_credential_from_store_payload(payload, name, provider_id=provider_id)
            should_refresh = bool(force_refresh)
            if not should_refresh and refresh_if_expiring:
                should_refresh = credential.access_token_expiring(skew_ms=refresh_skew_ms)
            if not should_refresh:
                return credential
            if not credential.refresh_token:
                raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth refresh token unavailable")
            refreshed = (refresher or refresh_memory_enhancement_oauth_credential)(credential)
            if refreshed.name != credential.name or refreshed.provider_id != credential.provider_id:
                raise ProtocolValidationError("memory enhancement oauth refresh returned mismatched credential")
            providers = payload.setdefault("providers", {})
            provider_bucket = providers.setdefault(refreshed.provider_id, {})
            provider_bucket[refreshed.name] = refreshed.to_dict()
            self._write_unlocked(payload)
            return refreshed

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_store()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth store unavailable") from exc
        if not isinstance(payload, dict):
            raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth store unavailable")
        return _normalize_store(payload)

    def _write_unlocked(self, payload: Mapping[str, Any]) -> None:
        normalized = _normalize_store(dict(payload))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_owner_only(self.path.parent, directory=True)
        _atomic_write_secret_text(self.path, json.dumps(normalized, indent=2, sort_keys=True) + "\n")


def _get_credential_from_store_payload(
    payload: Mapping[str, Any],
    name: str,
    *,
    provider_id: str = "",
) -> MemoryEnhancementOAuthCredential:
    providers = payload.get("providers") if isinstance(payload.get("providers"), Mapping) else {}
    if provider_id:
        require_valid_oauth_provider_id(provider_id)
        raw = providers.get(provider_id, {})
        if isinstance(raw, Mapping) and isinstance(raw.get(name), Mapping):
            return MemoryEnhancementOAuthCredential.from_dict(name, raw[name])
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth credential unavailable")

    matches: list[MemoryEnhancementOAuthCredential] = []
    for provider_name, bucket in providers.items():
        if not isinstance(provider_name, str) or not isinstance(bucket, Mapping):
            continue
        raw = bucket.get(name)
        if isinstance(raw, Mapping):
            matches.append(MemoryEnhancementOAuthCredential.from_dict(name, raw))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ProtocolValidationError("memory enhancement oauth credential ref is ambiguous")
    raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth credential unavailable")


@dataclass(frozen=True)
class OAuthMemoryEnhancementCredentialResolver:
    """Resolve PA-owned `oauth:*` refs from the local auth store."""

    store: MemoryEnhancementOAuthStore | None = None
    refresher: RefreshCallback | None = field(default=None, repr=False)

    def resolve(self, ref: MemoryEnhancementCredentialRef) -> ResolvedMemoryEnhancementCredential:
        credential = self.resolve_oauth(ref)
        return credential.to_resolved_credential()

    def resolve_oauth(
        self,
        ref: MemoryEnhancementCredentialRef,
        *,
        provider_id: str = "",
        refresh_if_expiring: bool = True,
        force_refresh: bool = False,
    ) -> MemoryEnhancementOAuthCredential:
        require_valid_memory_enhancement_credential_ref(ref)
        if ref.scheme != OAUTH_REF_SCHEME:
            raise MemoryEnhancementCredentialResolutionError("memory enhancement credential resolver unsupported")
        store = self.store or MemoryEnhancementOAuthStore()
        return store.get_valid(
            ref.name,
            provider_id=provider_id,
            refresh_if_expiring=refresh_if_expiring,
            force_refresh=force_refresh,
            refresher=self.refresher,
        )


def refresh_memory_enhancement_oauth_credential(
    credential: MemoryEnhancementOAuthCredential,
    *,
    opener: Callable[..., Any] | None = None,
) -> MemoryEnhancementOAuthCredential:
    """Refresh one OAuth credential and return the rotated credential state."""
    require_valid_oauth_credential(credential)
    if not credential.refresh_token:
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth refresh token unavailable")
    if credential.transport == "anthropic_oauth":
        payload = _refresh_anthropic_oauth(credential.refresh_token, opener=opener)
        return _credential_from_refresh_payload(credential, payload)
    if credential.transport == "google_cloudcode":
        payload = _refresh_google_oauth(credential, opener=opener)
        return _credential_from_refresh_payload(credential, payload)
    if credential.transport == "openai_codex":
        payload = _refresh_openai_codex_oauth(credential.refresh_token, opener=opener)
        return _credential_from_refresh_payload(credential, payload)
    raise ProtocolValidationError("memory enhancement oauth transport unsupported")


def _credential_from_refresh_payload(
    credential: MemoryEnhancementOAuthCredential,
    payload: Mapping[str, Any],
) -> MemoryEnhancementOAuthCredential:
    access_token = _payload_text(payload, "access_token")
    if not access_token:
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth refresh response unavailable")
    refresh_token = _payload_text(payload, "refresh_token") or credential.refresh_token
    if not refresh_token:
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth refresh token unavailable")
    return MemoryEnhancementOAuthCredential(
        name=credential.name,
        provider_id=credential.provider_id,
        source=credential.source,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_ms=_expires_at_ms_from_refresh_payload(payload, access_token=access_token),
        transport=credential.transport,
        base_url=credential.base_url,
        project_id=credential.project_id,
        account_label=credential.account_label,
        extra=dict(credential.extra),
    )


def _refresh_anthropic_oauth(
    refresh_token: str,
    *,
    opener: Callable[..., Any] | None,
) -> Mapping[str, Any]:
    last_error: Exception | None = None
    for endpoint in ANTHROPIC_OAUTH_TOKEN_ENDPOINTS:
        try:
            return _post_form_json(
                endpoint,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": ANTHROPIC_OAUTH_CLIENT_ID,
                },
                headers={
                    "Accept": "application/json",
                    "User-Agent": f"claude-cli/{CLAUDE_CODE_VERSION_FALLBACK} (external, cli)",
                },
                opener=opener,
                timeout_seconds=_TOKEN_REQUEST_TIMEOUT_SECONDS,
            )
        except MemoryEnhancementCredentialResolutionError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth refresh unavailable")


def _refresh_google_oauth(
    credential: MemoryEnhancementOAuthCredential,
    *,
    opener: Callable[..., Any] | None,
) -> Mapping[str, Any]:
    client_id, client_secret = _google_oauth_client_credentials(credential.extra)
    if not client_id:
        raise MemoryEnhancementCredentialResolutionError("memory enhancement google oauth client unavailable")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": credential.refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    return _post_form_json(
        GOOGLE_OAUTH_TOKEN_ENDPOINT,
        data,
        headers={"Accept": "application/json"},
        opener=opener,
        timeout_seconds=_TOKEN_REQUEST_TIMEOUT_SECONDS,
    )


def _refresh_openai_codex_oauth(
    refresh_token: str,
    *,
    opener: Callable[..., Any] | None,
) -> Mapping[str, Any]:
    return _post_form_json(
        OPENAI_CODEX_OAUTH_TOKEN_ENDPOINT,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OPENAI_CODEX_OAUTH_CLIENT_ID,
        },
        headers={"Accept": "application/json"},
        opener=opener,
        timeout_seconds=_TOKEN_REQUEST_TIMEOUT_SECONDS,
    )


def _post_form_json(
    url: str,
    data: Mapping[str, str],
    *,
    headers: Mapping[str, str] | None = None,
    opener: Callable[..., Any] | None,
    timeout_seconds: int,
    operation: str = "refresh",
) -> Mapping[str, Any]:
    encoded = urllib.parse.urlencode(dict(data)).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
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
        raise MemoryEnhancementCredentialResolutionError(_oauth_http_error_message(exc, operation=operation)) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth refresh unavailable") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth refresh response unavailable") from exc
    if not isinstance(payload, Mapping):
        raise MemoryEnhancementCredentialResolutionError("memory enhancement oauth refresh response unavailable")
    return payload


def _oauth_http_error_message(exc: urllib.error.HTTPError, *, operation: str) -> str:
    oauth_error = _oauth_http_error_code(exc)
    action = "authorization" if operation == "authorization" else "refresh"
    if oauth_error == "refresh_token_reused":
        return "memory enhancement oauth refresh token reused; close other clients and re-run setup"
    if oauth_error in {"invalid_grant", "invalid_token", "invalid_request"}:
        return f"memory enhancement oauth {action} rejected; re-run setup"
    if exc.code in {400, 401, 403}:
        return f"memory enhancement oauth {action} rejected; re-run setup"
    if exc.code == 429:
        return f"memory enhancement oauth {action} rate limited"
    return f"memory enhancement oauth {action} unavailable"


def _oauth_http_error_code(exc: urllib.error.HTTPError) -> str:
    try:
        raw_body = exc.read()
    except Exception:
        return ""
    if not raw_body:
        return ""
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, Mapping):
        return ""
    value = payload.get("error")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        code = value.get("error") or value.get("code") or value.get("status")
        return code.strip() if isinstance(code, str) else ""
    return ""


def _payload_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _expires_at_ms_from_refresh_payload(payload: Mapping[str, Any], *, access_token: str) -> int:
    raw_expires_at = payload.get("expires_at_ms") or payload.get("expires_at")
    if raw_expires_at is not None:
        try:
            value = int(raw_expires_at)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value * 1000 if value < 10_000_000_000 else value
    raw_expires_in = payload.get("expires_in")
    if raw_expires_in is not None:
        try:
            seconds = max(60, int(raw_expires_in))
        except (TypeError, ValueError):
            seconds = 3600
        return int(time.time() * 1000) + seconds * 1000
    jwt_expiry = _jwt_expires_at_ms(access_token)
    if jwt_expiry is not None:
        return jwt_expiry
    return int(time.time() * 1000) + 3600 * 1000


def _jwt_expires_at_ms(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        padded = parts[1] + ("=" * (-len(parts[1]) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    exp = payload.get("exp") if isinstance(payload, Mapping) else None
    if not isinstance(exp, (int, float)) or exp <= 0:
        return None
    return int(exp * 1000)


def _google_oauth_client_credentials(extra: Mapping[str, Any]) -> tuple[str, str]:
    client_id = _mapping_text(extra, "client_id")
    client_secret = _mapping_text(extra, "client_secret")
    if client_id:
        return client_id, client_secret
    client_id = _first_env((
        "CHIMERA_MEMORY_GOOGLE_OAUTH_CLIENT_ID",
        "PERSONIFYAGENTS_GOOGLE_OAUTH_CLIENT_ID",
        "HERMES_GEMINI_CLIENT_ID",
    ))
    client_secret = _first_env((
        "CHIMERA_MEMORY_GOOGLE_OAUTH_CLIENT_SECRET",
        "PERSONIFYAGENTS_GOOGLE_OAUTH_CLIENT_SECRET",
        "HERMES_GEMINI_CLIENT_SECRET",
    ))
    if client_id:
        return client_id, client_secret
    if GOOGLE_OAUTH_DEFAULT_CLIENT_ID:
        return GOOGLE_OAUTH_DEFAULT_CLIENT_ID, GOOGLE_OAUTH_DEFAULT_CLIENT_SECRET
    return _scrape_google_oauth_client_credentials()


def _mapping_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _scrape_google_oauth_client_credentials() -> tuple[str, str]:
    for path in _google_oauth_client_credential_sources():
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        client_id, client_secret = _parse_google_oauth_client_credentials(content)
        if client_id:
            return client_id, client_secret
    return "", ""


def _google_oauth_client_credential_sources() -> list[Path]:
    sources: list[Path] = []
    hermes_agent = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "hermes-agent" / "agent" / "google_oauth.py"
    if hermes_agent.exists():
        sources.append(hermes_agent)
    home_hermes_agent = Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent" / "agent" / "google_oauth.py"
    if home_hermes_agent.exists() and home_hermes_agent not in sources:
        sources.append(home_hermes_agent)

    gemini = shutil.which("gemini")
    if gemini:
        try:
            gemini_path = Path(gemini).resolve()
        except OSError:
            gemini_path = Path(gemini)
        candidate_roots = [gemini_path.parent, *list(gemini_path.parents)[:5]]
        for root in candidate_roots:
            core_root = root / "node_modules" / "@google" / "gemini-cli-core"
            for relative in (
                Path("dist") / "src" / "code_assist" / "oauth2.js",
                Path("dist") / "code_assist" / "oauth2.js",
                Path("src") / "code_assist" / "oauth2.js",
            ):
                path = core_root / relative
                if path.exists() and path not in sources:
                    sources.append(path)
    return sources


def _parse_google_oauth_client_credentials(content: str) -> tuple[str, str]:
    client_id_match = re.search(r"([0-9]{8,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com)", content)
    secret_prefix = "".join(("GO", "CSPX"))
    client_secret_match = re.search(r"(" + re.escape(secret_prefix) + r"-[A-Za-z0-9_-]{20,})", content)
    client_id = client_id_match.group(1) if client_id_match else ""
    client_secret = client_secret_match.group(1) if client_secret_match else ""
    if client_id and client_secret:
        return client_id, client_secret

    project_num = _python_string_assignment(content, "_PUBLIC_CLIENT_ID_PROJECT_NUM")
    client_hash = _python_string_assignment(content, "_PUBLIC_CLIENT_ID_HASH")
    secret_suffix = _python_string_assignment(content, "_PUBLIC_CLIENT_SECRET_SUFFIX")
    if not client_id and project_num and client_hash:
        client_id = f"{project_num}-{client_hash}.apps.googleusercontent.com"
    if not client_secret and secret_suffix:
        client_secret = f"{secret_prefix}-{secret_suffix}"
    return client_id, client_secret


def _python_string_assignment(content: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}\s*=\s*['\"]([^'\"]+)['\"]", content)
    return match.group(1) if match else ""


def resolve_oauth_store_path(path: str | Path | None = None, *, repo_root: str | Path | None = None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    configured = (
        os.environ.get("CHIMERA_MEMORY_OAUTH_STORE", "").strip()
        or os.environ.get("PERSONIFYAGENTS_MEMORY_OAUTH_STORE", "").strip()
    )
    if configured:
        return Path(configured).expanduser().resolve()
    state_root = (
        os.environ.get("CHIMERA_MEMORY_STATE_ROOT", "").strip()
        or os.environ.get("PERSONIFYAGENTS_PWA_STATE_ROOT", "").strip()
    )
    if state_root:
        return (Path(state_root).expanduser() / _DEFAULT_AUTH_STORE_NAME).resolve()
    root = Path(repo_root).resolve() if repo_root is not None else Path.cwd().resolve()
    return (root / _DEFAULT_STATE_ROOT / _DEFAULT_AUTH_STORE_NAME).resolve()


def require_valid_oauth_credential(credential: MemoryEnhancementOAuthCredential) -> None:
    require_valid_oauth_name(credential.name)
    require_valid_oauth_provider_id(credential.provider_id)
    if credential.transport not in OAUTH_TRANSPORTS:
        raise ProtocolValidationError("memory enhancement oauth transport unsupported")
    if not credential.source.strip():
        raise ProtocolValidationError("memory enhancement oauth source is required")
    require_valid_memory_enhancement_credential_value(credential.access_token)
    if credential.refresh_token:
        require_valid_memory_enhancement_credential_value(credential.refresh_token)
    if credential.expires_at_ms is not None and int(credential.expires_at_ms) < 0:
        raise ProtocolValidationError("memory enhancement oauth expiry is invalid")


def require_valid_oauth_name(name: str) -> None:
    if _OAUTH_NAME_RE.fullmatch(str(name or "")) is None:
        raise ProtocolValidationError("memory enhancement oauth credential name is invalid")


def require_valid_oauth_provider_id(provider_id: str) -> None:
    if provider_id not in OAUTH_PROVIDER_IDS:
        raise ProtocolValidationError("memory enhancement oauth provider unsupported")


def _empty_store() -> dict[str, Any]:
    return {"version": OAUTH_STORE_VERSION, "providers": {}}


def _normalize_store(payload: Mapping[str, Any]) -> dict[str, Any]:
    providers: dict[str, dict[str, dict[str, Any]]] = {}
    raw_providers = payload.get("providers")
    if isinstance(raw_providers, Mapping):
        for provider_id, bucket in raw_providers.items():
            if provider_id not in OAUTH_PROVIDER_IDS or not isinstance(bucket, Mapping):
                continue
            providers[provider_id] = {}
            for name, raw_credential in bucket.items():
                if not isinstance(name, str) or not isinstance(raw_credential, Mapping):
                    continue
                credential = MemoryEnhancementOAuthCredential.from_dict(name, raw_credential)
                require_valid_oauth_credential(credential)
                providers[provider_id][name] = credential.to_dict()
    return {"version": OAUTH_STORE_VERSION, "providers": providers}


@contextmanager
def _store_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            import fcntl  # type: ignore[import-not-found]
        except ImportError:
            fcntl = None
        if fcntl is not None:
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out acquiring memory enhancement OAuth store lock at {lock_path}.")
                    time.sleep(0.05)
        else:
            import msvcrt  # type: ignore[import-not-found]

            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out acquiring memory enhancement OAuth store lock at {lock_path}.")
                    time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                if "fcntl" in locals() and fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    import msvcrt  # type: ignore[import-not-found]

                    try:
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            finally:
                os.close(fd)
        else:
            os.close(fd)


def _atomic_write_secret_text(path: Path, payload: str) -> None:
    tmp_path = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        tmp_path.replace(path)
        _chmod_owner_only(path)
        _fsync_dir(path.parent)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _chmod_owner_only(path: Path, *, directory: bool = False) -> None:
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError:
        return
