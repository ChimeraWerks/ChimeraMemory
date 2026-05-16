"""CM credential shim for the Hermes Gemini Cloud Code adapter.

The copied Hermes adapter calls ``agent.google_oauth`` for token/project state.
CM resolves the OAuth credential before invoking the provider, so this module
exposes the Hermes-facing API backed by a per-call context variable and the CM
OAuth store instead of Hermes's on-disk credential file.
"""

from __future__ import annotations

import contextlib
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from .memory_enhancement_oauth import (
    MemoryEnhancementOAuthCredential,
    MemoryEnhancementOAuthStore,
    refresh_memory_enhancement_oauth_credential,
)


REFRESH_SKEW_SECONDS = 60


@dataclass(frozen=True)
class _GoogleCredentials:
    access_token: str
    refresh_token: str = ""
    expires_ms: int = 0
    email: str = ""
    project_id: str = ""
    managed_project_id: str = ""

    def expires_unix_seconds(self) -> float:
        return self.expires_ms / 1000.0

    def access_token_expired(self, skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
        if not self.access_token or not self.expires_ms:
            return True
        return (time.time() + max(0, skew_seconds)) * 1000 >= self.expires_ms


_credential_var: ContextVar[MemoryEnhancementOAuthCredential | None] = ContextVar(
    "cm_hermes_google_credential",
    default=None,
)
_store_var: ContextVar[MemoryEnhancementOAuthStore | None] = ContextVar(
    "cm_hermes_google_store",
    default=None,
)


@contextlib.contextmanager
def bind_credential(
    credential: MemoryEnhancementOAuthCredential,
    *,
    store: MemoryEnhancementOAuthStore | None = None,
) -> Iterator[None]:
    credential_token = _credential_var.set(credential)
    store_token = _store_var.set(store)
    try:
        yield
    finally:
        _credential_var.reset(credential_token)
        _store_var.reset(store_token)


def get_valid_access_token(*, force_refresh: bool = False) -> str:
    credential = _current_credential()
    refresh_skew_ms = REFRESH_SKEW_SECONDS * 1000
    if not force_refresh and not credential.access_token_expiring(skew_ms=refresh_skew_ms):
        return credential.access_token
    store = _store_var.get()
    if store is None:
        raise RuntimeError("memory enhancement google oauth refresh store unavailable")
    refreshed = store.get_valid(
        credential.name,
        provider_id=credential.provider_id,
        force_refresh=force_refresh,
        refresh_skew_ms=refresh_skew_ms,
        refresher=refresh_memory_enhancement_oauth_credential,
    )
    _credential_var.set(refreshed)
    credential = refreshed
    return credential.access_token


def load_credentials() -> _GoogleCredentials | None:
    credential = _credential_var.get()
    if credential is None:
        return None
    return _GoogleCredentials(
        access_token=credential.access_token,
        refresh_token=credential.refresh_token,
        expires_ms=int(credential.expires_at_ms or 0),
        email=credential.account_label,
        project_id=credential.project_id,
        managed_project_id=str(credential.extra.get("managed_project_id") or ""),
    )


def update_project_ids(*, project_id: str = "", managed_project_id: str = "") -> None:
    credential = _credential_var.get()
    store = _store_var.get()
    if credential is None or store is None:
        return
    extra = dict(credential.extra)
    if managed_project_id:
        extra["managed_project_id"] = managed_project_id
    updated = MemoryEnhancementOAuthCredential(
        name=credential.name,
        provider_id=credential.provider_id,
        source=credential.source,
        access_token=credential.access_token,
        refresh_token=credential.refresh_token,
        expires_at_ms=credential.expires_at_ms,
        transport=credential.transport,
        base_url=credential.base_url,
        project_id=project_id or credential.project_id,
        account_label=credential.account_label,
        extra=extra,
    )
    store.upsert(updated)
    _credential_var.set(updated)


def resolve_project_id_from_env() -> str:
    for key in (
        "HERMES_GEMINI_PROJECT_ID",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_PROJECT_ID",
    ):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _current_credential() -> MemoryEnhancementOAuthCredential:
    credential = _credential_var.get()
    if credential is None:
        raise RuntimeError("memory enhancement google oauth credential unavailable")
    if not credential.access_token:
        raise RuntimeError("memory enhancement google oauth credential unavailable")
    return credential
