"""HTTP client for a memory-enhancement sidecar endpoint.

This client speaks CM's own sidecar contract. It does not know how to call
OpenAI, Anthropic, or Ollama directly; the sidecar process owns those details
and any scoped credentials it needs.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from .memory_enhancement import ENHANCEMENT_SCHEMA_VERSION

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STATUS_OK = {"ok", "partial"}


class MemoryEnhancementHttpClient:
    """Invoke a memory-enhancement sidecar over HTTP."""

    def __init__(
        self,
        endpoint: str,
        *,
        bearer_token: str = "",
        timeout_seconds: int = 30,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.endpoint = _validate_endpoint(endpoint)
        self.bearer_token = _validate_bearer_token(bearer_token)
        self.timeout_seconds = _validate_timeout(timeout_seconds)
        self._opener = opener or urllib.request.urlopen

    def invoke(self, invocation: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.dumps(dict(invocation), separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")

        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw_body = response.read()
        except urllib.error.HTTPError as exc:
            code = _error_code_from_http_error(exc)
            if code:
                raise RuntimeError(f"memory enhancement sidecar rejected request: {code}") from exc
            raise RuntimeError(f"memory enhancement sidecar HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("memory enhancement sidecar unavailable") from exc
        except TimeoutError as exc:
            raise RuntimeError("memory enhancement sidecar timeout") from exc

        return _metadata_from_response(raw_body)


def _validate_endpoint(endpoint: object) -> str:
    value = str(endpoint or "").strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("memory enhancement sidecar endpoint must be http or https")
    return value


def _validate_bearer_token(value: object) -> str:
    token = str(value or "")
    if not token:
        return ""
    if len(token) > 16_384:
        raise ValueError("memory enhancement sidecar bearer token is too large")
    if _CONTROL_RE.search(token):
        raise ValueError("memory enhancement sidecar bearer token contains control characters")
    return token


def _validate_timeout(value: object) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        raise ValueError("memory enhancement sidecar timeout must be an integer") from None
    if timeout < 1 or timeout > 300:
        raise ValueError("memory enhancement sidecar timeout must be between 1 and 300 seconds")
    return timeout


def _metadata_from_response(raw_body: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("memory enhancement sidecar returned invalid JSON") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("memory enhancement sidecar response must be a JSON object")
    if payload.get("schema_version") not in {"", None, ENHANCEMENT_SCHEMA_VERSION}:
        raise RuntimeError("memory enhancement sidecar schema unsupported")

    status = str(payload.get("status") or "").strip().lower()
    if status not in _STATUS_OK:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = str(error.get("code") or "unknown_error").strip()[:80]
        raise RuntimeError(f"memory enhancement sidecar rejected request: {code}")

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError("memory enhancement sidecar metadata missing")
    return metadata


def _error_code_from_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        raw_body = exc.read()
    except Exception:
        return ""
    return _error_code_from_response_body(raw_body)


def _error_code_from_response_body(raw_body: bytes) -> str:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, Mapping):
        return ""
    error = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
    code = str(error.get("code") or "").strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
        return code
    return ""
