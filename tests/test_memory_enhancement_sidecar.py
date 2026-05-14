import threading

import pytest

from chimera_memory.memory_enhancement import ENHANCEMENT_SCHEMA_VERSION
from chimera_memory.memory_enhancement_http_client import MemoryEnhancementHttpClient
from chimera_memory.memory_enhancement_provider import build_enhancement_invocation, resolve_enhancement_provider_plan
from chimera_memory.memory_enhancement_sidecar import (
    build_dry_run_sidecar_error,
    build_dry_run_sidecar_response,
    build_provider_sidecar_response,
    create_dry_run_sidecar_server,
    create_provider_sidecar_server,
)


def test_dry_run_sidecar_response_matches_contract() -> None:
    response = build_dry_run_sidecar_response(
        {
            "request_id": "request-1",
            "request": {
                "existing_frontmatter": {"type": "procedural", "tags": ["sidecar"]},
                "wrapped_content": "\n".join(
                    [
                        "----- BEGIN UNTRUSTED MEMORY CONTENT -----",
                        "Dry-run sidecar should produce metadata on 2026-05-14.",
                        "TODO: keep OAuth outside CM.",
                        "----- END UNTRUSTED MEMORY CONTENT -----",
                    ]
                ),
            },
        }
    )

    assert response["schema_version"] == ENHANCEMENT_SCHEMA_VERSION
    assert response["request_id"] == "request-1"
    assert response["status"] == "ok"
    assert response["metadata"]["memory_type"] == "procedural"
    assert "2026-05-14" in response["metadata"]["dates"]
    assert response["metadata"]["can_use_as_instruction"] is False
    assert response["diagnostics"]["model"] == "dry-run/deterministic-local"


def test_dry_run_sidecar_error_omits_message_detail() -> None:
    response = build_dry_run_sidecar_error("auth_error")

    assert response["error"] == {"code": "auth_error", "message": ""}
    assert response["metadata"] == {}


class FakeProviderClient:
    def __init__(self, response=None, error=None):
        self.response = response or {
            "memory_type": "lesson",
            "summary": "Provider sidecar returned metadata.",
            "topics": ["provider"],
            "confidence": 0.9,
        }
        self.error = error
        self.invocations = []

    def invoke(self, invocation):
        self.invocations.append(invocation)
        if self.error:
            raise self.error
        return self.response


def test_provider_sidecar_response_uses_injected_client() -> None:
    client = FakeProviderClient()
    invocation = {
        "request_id": "request-1",
        "provider": {"provider_id": "openai", "model": "gpt-4o-mini"},
        "request": {
            "wrapped_content": "----- BEGIN UNTRUSTED MEMORY CONTENT -----\ncontent\n----- END UNTRUSTED MEMORY CONTENT -----"
        },
    }

    response = build_provider_sidecar_response(invocation, client)

    assert response["schema_version"] == ENHANCEMENT_SCHEMA_VERSION
    assert response["request_id"] == "request-1"
    assert response["status"] == "ok"
    assert response["metadata"]["summary"] == "Provider sidecar returned metadata."
    assert response["metadata"]["can_use_as_instruction"] is False
    assert response["diagnostics"]["model"] == "openai/gpt-4o-mini"
    assert client.invocations == [invocation]


def test_http_client_can_call_dry_run_sidecar() -> None:
    fake_token = "TEST_ONLY_SIDE_TOKEN"
    server = create_dry_run_sidecar_server("127.0.0.1", 0, bearer_token=fake_token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/enhance"
        client = MemoryEnhancementHttpClient(endpoint, bearer_token=fake_token)
        invocation = build_enhancement_invocation(
            {
                "existing_frontmatter": {"type": "semantic"},
                "wrapped_content": "\n".join(
                    [
                        "----- BEGIN UNTRUSTED MEMORY CONTENT -----",
                        "HTTP sidecar integration test.",
                        "----- END UNTRUSTED MEMORY CONTENT -----",
                    ]
                ),
            },
            resolve_enhancement_provider_plan({"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"}),
        )

        metadata = client.invoke(invocation)

        assert metadata["memory_type"] == "semantic"
        assert metadata["summary"] == "HTTP sidecar integration test."
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_client_sidecar_auth_failure_is_sanitized() -> None:
    server = create_dry_run_sidecar_server("127.0.0.1", 0, bearer_token="TEST_ONLY_SIDE_TOKEN")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/enhance"
        client = MemoryEnhancementHttpClient(endpoint, bearer_token="TEST_ONLY_WRONG_TOKEN")
        with pytest.raises(RuntimeError) as exc_info:
            client.invoke({"request": {"wrapped_content": "captured content must not leak"}})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    message = str(exc_info.value)
    assert "401" in message
    assert "TEST_ONLY_SIDE_TOKEN" not in message
    assert "TEST_ONLY_WRONG_TOKEN" not in message
    assert "captured content" not in message


def test_http_client_can_call_provider_sidecar_with_separate_sidecar_auth() -> None:
    fake_sidecar_token = "TEST_ONLY_SIDECAR_TOKEN"
    client = FakeProviderClient()
    server = create_provider_sidecar_server("127.0.0.1", 0, client=client, bearer_token=fake_sidecar_token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/enhance"
        http_client = MemoryEnhancementHttpClient(endpoint, bearer_token=fake_sidecar_token)
        invocation = build_enhancement_invocation(
            {
                "existing_frontmatter": {"type": "semantic"},
                "wrapped_content": "Provider sidecar integration test.",
            },
            resolve_enhancement_provider_plan({"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"}),
        )

        metadata = http_client.invoke(invocation)

        assert metadata["summary"] == "Provider sidecar returned metadata."
        assert client.invocations[0]["request"]["wrapped_content"] == "Provider sidecar integration test."
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_provider_sidecar_failure_is_sanitized() -> None:
    server = create_provider_sidecar_server(
        "127.0.0.1",
        0,
        client=FakeProviderClient(error=RuntimeError("unauthorized raw-token-value captured content")),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/enhance"
        http_client = MemoryEnhancementHttpClient(endpoint)
        with pytest.raises(RuntimeError) as exc_info:
            http_client.invoke({"request": {"wrapped_content": "captured content must not leak"}})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    message = str(exc_info.value)
    assert "502" in message
    assert "raw-token-value" not in message
    assert "captured content" not in message
