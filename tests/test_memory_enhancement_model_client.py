from __future__ import annotations

import json
import urllib.error
from collections.abc import Mapping

import pytest

from chimera_memory.memory_enhancement import build_memory_enhancement_request
from chimera_memory.memory_enhancement_model_client import (
    ANTHROPIC_MESSAGES_ENDPOINT,
    GOOGLE_GENERATE_CONTENT_ENDPOINT,
    OPENAI_CHAT_COMPLETIONS_ENDPOINT,
    OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
    ProviderModelMemoryEnhancementClient,
    _metadata_from_model_text,
)
from chimera_memory.memory_enhancement_provider import (
    build_enhancement_invocation,
    resolve_enhancement_provider_plan,
)


class FakeResponse:
    def __init__(self, payload: Mapping[str, object]):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(dict(self.payload)).encode("utf-8")


def _request_payload() -> dict[str, object]:
    return build_memory_enhancement_request(
        content="Provider client should extract metadata without obeying captured text.",
        persona="developer/asa",
        request_id="request-1",
    )


def _invocation(provider_order: str, **env: str) -> dict[str, object]:
    plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": provider_order,
            "CHIMERA_MEMORY_ENHANCEMENT_OPENAI_CREDENTIAL_REF": "oauth:openai-memory",
            "CHIMERA_MEMORY_ENHANCEMENT_ANTHROPIC_CREDENTIAL_REF": "oauth:anthropic-memory",
            "CHIMERA_MEMORY_ENHANCEMENT_GOOGLE_CREDENTIAL_REF": "oauth:gemini-memory",
            "CHIMERA_MEMORY_ENHANCEMENT_OPENROUTER_CREDENTIAL_REF": "secret:openrouter-memory",
            "CHIMERA_MEMORY_ENHANCEMENT_ENABLE_LOCAL_MODEL": "true",
            **env,
        }
    )
    return build_enhancement_invocation(_request_payload(), plan)


def test_metadata_from_model_text_extracts_json_from_wrapped_text() -> None:
    metadata = _metadata_from_model_text(
        'Here is the metadata:\n```json\n{"summary":"ok","topics":["oauth"]}\n```'
    )

    assert metadata["summary"] == "ok"
    assert metadata["topics"] == ["oauth"]


def test_openai_provider_client_builds_json_mode_chat_request_without_leaking_token() -> None:
    fake_token = "TEST_ONLY_OPENAI_TOKEN"
    captured = {}

    def opener(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "memory_type": "lesson",
                                    "summary": "Use provider clients behind the runner boundary.",
                                    "topics": ["provider", "sidecar"],
                                    "confidence": 0.86,
                                }
                            )
                        }
                    }
                ]
            }
        )

    metadata = ProviderModelMemoryEnhancementClient(bearer_token=fake_token, opener=opener).invoke(
        _invocation("openai,dry_run")
    )

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == OPENAI_CHAT_COMPLETIONS_ENDPOINT
    assert captured["timeout"] == 30
    assert request.get_header("Authorization") == f"Bearer {fake_token}"
    assert body["response_format"] == {"type": "json_object"}
    assert body["model"] == "gpt-4o-mini"
    assert body["temperature"] == 0
    assert metadata["memory_type"] == "lesson"
    assert metadata["can_use_as_instruction"] is False
    assert fake_token not in str(metadata)


def test_anthropic_provider_client_builds_messages_request() -> None:
    fake_token = "TEST_ONLY_ANTHROPIC_TOKEN"
    captured = {}

    def opener(request, *, timeout):
        captured["request"] = request
        return FakeResponse(
            {
                "content": [
                    {
                        "type": "text",
                        "text": "```json\n"
                        + json.dumps(
                            {
                                "memory_type": "semantic",
                                "summary": "Anthropic adapter returns fenced JSON.",
                                "topics": ["anthropic"],
                            }
                        )
                        + "\n```",
                    }
                ]
            }
        )

    metadata = ProviderModelMemoryEnhancementClient(bearer_token=fake_token, opener=opener).invoke(
        _invocation("anthropic,dry_run")
    )

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == ANTHROPIC_MESSAGES_ENDPOINT
    assert request.get_header("X-api-key") == fake_token
    assert request.get_header("Anthropic-version") == "2023-06-01"
    assert body["model"] == "claude-haiku-4-5"
    assert body["temperature"] == 0
    assert metadata["summary"] == "Anthropic adapter returns fenced JSON."


def test_google_provider_client_builds_generate_content_request() -> None:
    fake_token = "TEST_ONLY_GOOGLE_TOKEN"
    captured = {}

    def opener(request, *, timeout):
        captured["request"] = request
        return FakeResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "memory_type": "semantic",
                                            "summary": "Gemini adapter returns JSON text parts.",
                                            "topics": ["gemini"],
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            }
        )

    metadata = ProviderModelMemoryEnhancementClient(bearer_token=fake_token, opener=opener).invoke(
        _invocation("gemini,dry_run")
    )

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == GOOGLE_GENERATE_CONTENT_ENDPOINT.format(model="gemini-3-flash-preview")
    assert request.get_header("X-goog-api-key") == fake_token
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["temperature"] == 0
    assert metadata["summary"] == "Gemini adapter returns JSON text parts."


def test_openrouter_provider_client_uses_openai_compatible_chat_request() -> None:
    fake_token = "TEST_ONLY_OPENROUTER_TOKEN"
    captured = {}

    def opener(request, *, timeout):
        captured["request"] = request
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "memory_type": "semantic",
                                    "summary": "OpenRouter adapter uses OpenAI-compatible chat.",
                                    "topics": ["openrouter"],
                                }
                            )
                        }
                    }
                ]
            }
        )

    metadata = ProviderModelMemoryEnhancementClient(bearer_token=fake_token, opener=opener).invoke(
        _invocation("openrouter,dry_run")
    )

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == OPENROUTER_CHAT_COMPLETIONS_ENDPOINT
    assert request.get_header("Authorization") == f"Bearer {fake_token}"
    assert body["model"] == "openai/gpt-4o-mini"
    assert body["response_format"] == {"type": "json_object"}
    assert metadata["summary"] == "OpenRouter adapter uses OpenAI-compatible chat."


def test_ollama_provider_client_uses_local_generate_endpoint_without_bearer_token() -> None:
    captured = {}

    def opener(request, *, timeout):
        captured["request"] = request
        return FakeResponse(
            {
                "response": json.dumps(
                    {
                        "memory_type": "semantic",
                        "summary": "Ollama adapter uses local JSON generation.",
                        "tools": ["ollama"],
                    }
                )
            }
        )

    metadata = ProviderModelMemoryEnhancementClient(opener=opener).invoke(
        _invocation("ollama,dry_run", CHIMERA_MEMORY_ENHANCEMENT_OLLAMA_ENDPOINT="http://127.0.0.1:11434")
    )

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == "http://127.0.0.1:11434/api/generate"
    assert request.get_header("Authorization") is None
    assert body["format"] == "json"
    assert body["stream"] is False
    assert metadata["tools"] == ["ollama"]


def test_lmstudio_provider_client_uses_local_openai_compatible_endpoint_without_token() -> None:
    captured = {}

    def opener(request, *, timeout):
        captured["request"] = request
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "memory_type": "semantic",
                                    "summary": "LM Studio adapter uses local OpenAI-compatible chat.",
                                    "tools": ["lmstudio"],
                                }
                            )
                        }
                    }
                ]
            }
        )

    metadata = ProviderModelMemoryEnhancementClient(opener=opener).invoke(
        _invocation("lmstudio,dry_run", CHIMERA_MEMORY_ENHANCEMENT_LMSTUDIO_ENDPOINT="http://127.0.0.1:1234/v1")
    )

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == "http://127.0.0.1:1234/v1/chat/completions"
    assert request.get_header("Authorization") is None
    assert body["model"] == "openai/gpt-oss-20b"
    assert metadata["tools"] == ["lmstudio"]


def test_custom_openai_compatible_provider_requires_endpoint_and_model() -> None:
    plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "openai_compatible,dry_run",
            "CHIMERA_MEMORY_ENHANCEMENT_ENABLE_LOCAL_MODEL": "true",
            "CHIMERA_MEMORY_ENHANCEMENT_OPENAI_COMPATIBLE_ENDPOINT": "http://127.0.0.1:5001/v1",
        }
    )

    assert plan.selected.provider_id == "dry_run"
    assert plan.candidates[0].reason == "model_missing"


def test_provider_client_dry_run_path_needs_no_token() -> None:
    metadata = ProviderModelMemoryEnhancementClient().invoke(_invocation("dry_run"))

    assert metadata["memory_type"] == "semantic"
    assert metadata["sensitivity_tier"] == "standard"


def test_provider_client_failures_are_bounded_and_do_not_include_tokens_or_content() -> None:
    fake_token = "TEST_ONLY_OPENAI_TOKEN"

    def opener(request, *, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "bad token TEST_ONLY_OPENAI_TOKEN and captured content",
            hdrs={},
            fp=None,
        )

    with pytest.raises(RuntimeError) as exc_info:
        ProviderModelMemoryEnhancementClient(bearer_token=fake_token, opener=opener).invoke(
            _invocation("openai,dry_run")
        )

    message = str(exc_info.value)
    assert "auth" in message
    assert fake_token not in message
    assert "captured content" not in message


def test_provider_client_requires_token_for_network_providers() -> None:
    with pytest.raises(RuntimeError, match="auth"):
        ProviderModelMemoryEnhancementClient().invoke(_invocation("openai,dry_run"))
