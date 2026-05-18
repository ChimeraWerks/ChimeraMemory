from __future__ import annotations

import json
import urllib.error
from collections.abc import Mapping

import pytest

from chimera_memory.memory_enhancement import (
    build_authored_memory_enrichment_request,
    build_memory_enhancement_request,
)
from chimera_memory.memory_enhancement_model_client import (
    ANTHROPIC_MESSAGES_ENDPOINT,
    GOOGLE_GENERATE_CONTENT_ENDPOINT,
    MemoryEnhancementCostCapError,
    OPENAI_CHAT_COMPLETIONS_ENDPOINT,
    OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
    ProviderModelMemoryEnhancementClient,
    _metadata_from_model_text,
)
from chimera_memory.memory_enhancement_provider import (
    build_enhancement_invocation,
    classify_enhancement_failure,
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


def _authored_invocation(provider_order: str, **env: str) -> dict[str, object]:
    plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": provider_order,
            "CHIMERA_MEMORY_ENHANCEMENT_OPENAI_CREDENTIAL_REF": "oauth:openai-memory",
            "CHIMERA_MEMORY_ENHANCEMENT_ENABLE_LOCAL_MODEL": "true",
            **env,
        }
    )
    request = build_authored_memory_enrichment_request(
        memory_payload={
            "memory_type": "procedural",
            "lessons": ["Preserve reference UX behavior."],
            "entities": {"people": ["Charles"], "projects": ["Hermes"]},
            "body": "Day 60 slice 2 covered Google OAuth loopback behavior.",
        },
        persona="developer/asa",
        request_id="authored-request-1",
    )
    return build_enhancement_invocation(request, plan)


def test_metadata_from_model_text_extracts_json_from_wrapped_text() -> None:
    metadata = _metadata_from_model_text(
        'Here is the metadata:\n```json\n{"summary":"ok","topics":["oauth"]}\n```'
    )

    assert metadata["summary"] == "ok"
    assert metadata["topics"] == ["oauth"]


def test_openai_provider_client_builds_json_mode_chat_request_without_leaking_token() -> None:
    ProviderModelMemoryEnhancementClient.reset_call_count()
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
    assert body["model"] == "gpt-5.3-codex-spark"
    assert body["temperature"] == 0
    assert metadata["memory_type"] == "lesson"
    assert metadata["can_use_as_instruction"] is False
    assert fake_token not in str(metadata)


def test_provider_client_raw_json_uses_prompt_overrides() -> None:
    ProviderModelMemoryEnhancementClient.reset_call_count()
    fake_token = "TEST_ONLY_OPENAI_TOKEN"
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
                                    "relation": "supports",
                                    "direction": "A_to_B",
                                    "confidence": 0.91,
                                }
                            )
                        }
                    }
                ]
            }
        )

    invocation = _invocation("openai,dry_run")
    invocation["system_prompt"] = "classify edges only"
    invocation["user_prompt"] = "A supports B?"
    invocation["raw_json"] = True
    metadata = ProviderModelMemoryEnhancementClient(bearer_token=fake_token, opener=opener).invoke(invocation)

    body = json.loads(captured["request"].data.decode("utf-8"))
    assert body["messages"][0]["content"] == "classify edges only"
    assert body["messages"][1]["content"] == "A supports B?"
    assert metadata == {"relation": "supports", "direction": "A_to_B", "confidence": 0.91}


def test_cost_cap_blocks_invocation_before_network(monkeypatch) -> None:
    ProviderModelMemoryEnhancementClient.reset_call_count()
    monkeypatch.setenv("CHIMERA_MEMORY_ENHANCEMENT_MAX_CALLS", "0")
    called = False

    def opener(_request, *, timeout):
        nonlocal called
        called = True
        raise AssertionError("network should not be called")

    with pytest.raises(MemoryEnhancementCostCapError):
        ProviderModelMemoryEnhancementClient(
            bearer_token="TEST_ONLY_OPENAI_TOKEN",
            opener=opener,
        ).invoke(_invocation("openai,dry_run"))

    assert called is False


def test_cost_cap_counter_increments_on_failed_calls(monkeypatch) -> None:
    ProviderModelMemoryEnhancementClient.reset_call_count()
    monkeypatch.setenv("CHIMERA_MEMORY_ENHANCEMENT_MAX_CALLS", "1")

    def opener(_request, *, timeout):
        raise TimeoutError("simulated timeout")

    client = ProviderModelMemoryEnhancementClient(
        bearer_token="TEST_ONLY_OPENAI_TOKEN",
        opener=opener,
    )

    with pytest.raises(RuntimeError):
        client.invoke(_invocation("openai,dry_run"))
    with pytest.raises(MemoryEnhancementCostCapError):
        client.invoke(_invocation("openai,dry_run"))


def test_cost_cap_error_classifies_as_quota_exceeded() -> None:
    assert classify_enhancement_failure(MemoryEnhancementCostCapError("memory enhancement cost cap reached")) == "quota_exceeded"


def test_anthropic_provider_client_builds_messages_request() -> None:
    ProviderModelMemoryEnhancementClient.reset_call_count()
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
    ProviderModelMemoryEnhancementClient.reset_call_count()
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
    ProviderModelMemoryEnhancementClient.reset_call_count()
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
    ProviderModelMemoryEnhancementClient.reset_call_count()
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
    ProviderModelMemoryEnhancementClient.reset_call_count()
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


def test_koboldcpp_openai_compatible_omits_json_mode_and_uses_local_sampling_floor() -> None:
    ProviderModelMemoryEnhancementClient.reset_call_count()
    captured = {}

    def opener(request, *, timeout):
        captured["request"] = request
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "\n\n"
                            + json.dumps(
                                {
                                    "memory_type": "semantic",
                                    "summary": "KoboldCpp local adapter returns JSON without OpenAI grammar mode.",
                                    "tools": ["koboldcpp"],
                                }
                            )
                        }
                    }
                ]
            }
        )

    metadata = ProviderModelMemoryEnhancementClient(opener=opener).invoke(
        _invocation(
            "openai_compatible,dry_run",
            CHIMERA_MEMORY_ENHANCEMENT_OPENAI_COMPATIBLE_ENDPOINT="http://127.0.0.1:5001/v1",
            CHIMERA_MEMORY_ENHANCEMENT_OPENAI_COMPATIBLE_MODEL="koboldcpp/qwen3-local",
            CHIMERA_MEMORY_ENHANCEMENT_MAX_OUTPUT_TOKENS="200",
        )
    )

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == "http://127.0.0.1:5001/v1/chat/completions"
    assert "response_format" not in body
    assert body["max_tokens"] == 800
    assert body["temperature"] == 0
    assert body["top_p"] == 1.0
    assert body["top_k"] == 0
    assert body["min_p"] == 0.0
    assert metadata["tools"] == ["koboldcpp"]


def test_authored_enrichment_request_uses_narrow_prompt_surface() -> None:
    ProviderModelMemoryEnhancementClient.reset_call_count()
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
                                    "entities": [{"name": "Charles", "type": "person", "confidence": 0.9}],
                                    "topics": ["ux-parity"],
                                    "dates": ["Day 60", "slice 2"],
                                    "confidence": 0.9,
                                    "sensitivity_tier": "standard",
                                }
                            )
                        }
                    }
                ]
            }
        )

    ProviderModelMemoryEnhancementClient(opener=opener).invoke(
        _authored_invocation(
            "openai_compatible,dry_run",
            CHIMERA_MEMORY_ENHANCEMENT_OPENAI_COMPATIBLE_ENDPOINT="http://127.0.0.1:5001/v1",
            CHIMERA_MEMORY_ENHANCEMENT_OPENAI_COMPATIBLE_MODEL="koboldcpp/qwen3-local",
        )
    )

    body = json.loads(captured["request"].data.decode("utf-8"))
    system_prompt = body["messages"][0]["content"]
    assert "Allowed top-level keys: entities, relationships, topics, dates, confidence, sensitivity_tier" in system_prompt
    assert "action_items" in system_prompt
    assert "Do not output memory_type, summary, action_items" in system_prompt
    assert "Action items should be stable imperative directives" not in system_prompt


def test_metadata_from_model_text_ignores_leading_think_block() -> None:
    metadata = _metadata_from_model_text(
        "<think>this may mention {not json}</think>\n"
        + json.dumps(
            {
                "memory_type": "semantic",
                "summary": "JSON after a thinking block is parsed.",
                "topics": ["local-model"],
            }
        )
    )

    assert metadata["summary"] == "JSON after a thinking block is parsed."
    assert metadata["topics"] == ["local-model"]


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


def test_provider_client_dry_run_raw_json_trace_analysis_uses_trace_contract() -> None:
    plan = resolve_enhancement_provider_plan({"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"})
    invocation = build_enhancement_invocation(
        {
            "schema_version": "chimera-memory.retrieval-trace-analysis.v1",
            "task": "analyze_memory_retrieval_trace",
            "trace": {
                "tool_name": "memory_recall",
                "query_text": "What ProjectChimera work shipped today?",
                "requested_limit": 10,
                "returned_count": 10,
                "items": [
                    {
                        "rank": 1,
                        "relative_path": "memory/episodes/day62-projectchimera-phase5-merged.md",
                    }
                ],
            },
        },
        plan,
    )
    invocation["raw_json"] = True

    analysis = ProviderModelMemoryEnhancementClient().invoke(invocation)

    assert analysis["category"] == "ok"
    assert analysis["severity"] == "info"
    assert analysis["suggested_tool_route"] == "memory_recall"
    assert "memory_type" not in analysis


def test_provider_client_dry_run_raw_json_trace_analysis_flags_noise_paths() -> None:
    plan = resolve_enhancement_provider_plan({"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"})
    invocation = build_enhancement_invocation(
        {
            "schema_version": "chimera-memory.retrieval-trace-analysis.v1",
            "task": "analyze_memory_retrieval_trace",
            "trace": {
                "tool_name": "memory_search",
                "query_text": "synthesis dossier pollutes search results",
                "requested_limit": 10,
                "returned_count": 2,
                "items": [
                    {
                        "rank": 1,
                        "relative_path": "diagnostics/generated-summary.md",
                    }
                ],
            },
        },
        plan,
    )
    invocation["raw_json"] = True

    analysis = ProviderModelMemoryEnhancementClient().invoke(invocation)

    assert analysis["category"] == "diagnostics_noise_pollution"
    assert analysis["severity"] == "high"
    assert analysis["confidence"] == 0.9


def test_provider_client_failures_are_bounded_and_do_not_include_tokens_or_content() -> None:
    ProviderModelMemoryEnhancementClient.reset_call_count()
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
