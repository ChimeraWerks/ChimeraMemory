import json
import urllib.error
from typing import Any

import pytest

from chimera_memory.memory_enhancement import ENHANCEMENT_SCHEMA_VERSION
from chimera_memory.memory_enhancement_http_client import MemoryEnhancementHttpClient


class _FakeResponse:
    def __init__(self, payload: object):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def read(self) -> bytes:
        return self._body


class _FakeOpener:
    def __init__(self, payload: object):
        self.payload = payload
        self.requests: list[Any] = []
        self.timeouts: list[int] = []

    def __call__(self, request, *, timeout: int):
        self.requests.append(request)
        self.timeouts.append(timeout)
        return _FakeResponse(self.payload)


def test_http_client_posts_invocation_and_returns_metadata() -> None:
    fake_token = "TEST_ONLY_SIDE_TOKEN"
    opener = _FakeOpener(
        {
            "schema_version": ENHANCEMENT_SCHEMA_VERSION,
            "status": "ok",
            "metadata": {
                "memory_type": "semantic",
                "summary": "HTTP sidecar returned metadata.",
                "topics": ["sidecar"],
                "confidence": 0.91,
            },
        }
    )
    client = MemoryEnhancementHttpClient(
        "http://127.0.0.1:8944/enhance",
        bearer_token=fake_token,
        timeout_seconds=12,
        opener=opener,
    )

    metadata = client.invoke({"request": {"wrapped_content": "untrusted text"}})

    assert metadata["summary"] == "HTTP sidecar returned metadata."
    assert opener.timeouts == [12]
    request = opener.requests[0]
    assert request.full_url == "http://127.0.0.1:8944/enhance"
    assert request.get_method() == "POST"
    assert request.headers["Content-type"] == "application/json"
    assert request.headers["Authorization"] == f"Bearer {fake_token}"
    assert b"untrusted text" in request.data


def test_http_client_rejects_invalid_endpoint_and_timeout() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        MemoryEnhancementHttpClient("file:///tmp/sidecar.sock")
    with pytest.raises(ValueError, match="timeout"):
        MemoryEnhancementHttpClient("http://127.0.0.1:8944/enhance", timeout_seconds=0)


def test_http_client_errors_do_not_echo_token_or_response_body() -> None:
    fake_token = "TEST_ONLY_SIDE_TOKEN"

    def opener(_request, *, timeout: int):
        raise urllib.error.HTTPError(
            url="http://127.0.0.1:8944/enhance",
            code=401,
            msg="unauthorized",
            hdrs=None,
            fp=None,
        )

    client = MemoryEnhancementHttpClient(
        "http://127.0.0.1:8944/enhance",
        bearer_token=fake_token,
        opener=opener,
    )

    with pytest.raises(RuntimeError) as exc_info:
        client.invoke({"request": {"wrapped_content": "do not echo this body"}})

    message = str(exc_info.value)
    assert "401" in message
    assert fake_token not in message
    assert "do not echo this body" not in message


def test_http_client_rejects_non_ok_response_with_code_only() -> None:
    opener = _FakeOpener(
        {
            "schema_version": ENHANCEMENT_SCHEMA_VERSION,
            "status": "error",
            "metadata": {},
            "error": {"code": "content_filter", "message": "raw provider detail must not escape"},
        }
    )
    client = MemoryEnhancementHttpClient("http://127.0.0.1:8944/enhance", opener=opener)

    with pytest.raises(RuntimeError) as exc_info:
        client.invoke({"request": {"wrapped_content": "captured content"}})

    message = str(exc_info.value)
    assert "content_filter" in message
    assert "raw provider detail" not in message
    assert "captured content" not in message


def test_http_client_rejects_missing_metadata() -> None:
    client = MemoryEnhancementHttpClient(
        "http://127.0.0.1:8944/enhance",
        opener=_FakeOpener({"schema_version": ENHANCEMENT_SCHEMA_VERSION, "status": "ok"}),
    )

    with pytest.raises(RuntimeError, match="metadata"):
        client.invoke({"request": {}})
