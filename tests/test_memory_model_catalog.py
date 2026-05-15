from __future__ import annotations

import json
import os
from pathlib import Path

from chimera_memory.memory_enhancement_provider import resolve_enhancement_provider_plan
from chimera_memory.memory_model_catalog import (
    default_memory_enhancement_model,
    load_model_catalog,
    provider_info,
    provider_model_infos,
    recommended_memory_enhancement_models,
    reset_model_catalog_cache,
)


def _catalog() -> dict:
    return {
        "openai": {
            "id": "openai",
            "name": "OpenAI",
            "env": ["OPENAI_API_KEY"],
            "models": {
                "expensive": {
                    "id": "expensive",
                    "name": "Expensive",
                    "tool_call": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 128000, "output": 4096},
                    "cost": {"input": 10, "output": 30},
                },
                "gpt-4o-mini": {
                    "id": "gpt-4o-mini",
                    "name": "GPT-4o mini",
                    "tool_call": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 128000, "output": 4096},
                    "cost": {"input": 0.15, "output": 0.6},
                },
                "image-only": {
                    "id": "image-only",
                    "name": "Image only",
                    "modalities": {"input": ["text"], "output": ["image"]},
                    "limit": {"context": 128000, "output": 0},
                },
                "old": {
                    "id": "old",
                    "name": "Old",
                    "status": "deprecated",
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 128000, "output": 4096},
                },
            },
        },
        "anthropic": {
            "id": "anthropic",
            "name": "Anthropic",
            "models": {
                "claude-haiku-4-5": {
                    "id": "claude-haiku-4-5",
                    "name": "Claude Haiku 4.5",
                    "tool_call": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 200000, "output": 8192},
                    "cost": {"input": 1, "output": 5},
                }
            },
        },
        "google": {
            "id": "google",
            "name": "Google",
            "models": {
                "gemini-2.5-flash": {
                    "id": "gemini-2.5-flash",
                    "name": "Gemini 2.5 Flash",
                    "tool_call": True,
                    "structured_output": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 1048576, "output": 8192},
                    "cost": {"input": 0.3, "output": 2.5},
                },
                "gemini-flash-latest": {
                    "id": "gemini-flash-latest",
                    "name": "Gemini Flash Latest",
                    "tool_call": True,
                    "structured_output": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 1048576, "output": 8192},
                    "cost": {"input": 0.3, "output": 2.5},
                }
            },
        },
        "openrouter": {
            "id": "openrouter",
            "name": "OpenRouter",
            "models": {
                "openai/gpt-4o-mini": {
                    "id": "openai/gpt-4o-mini",
                    "name": "OpenAI GPT-4o mini",
                    "tool_call": True,
                    "structured_output": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 128000, "output": 4096},
                    "cost": {"input": 0.15, "output": 0.6},
                }
            },
        },
    }


def test_load_model_catalog_fetches_and_writes_disk_cache(tmp_path: Path) -> None:
    reset_model_catalog_cache()
    cache_path = tmp_path / "models-dev.json"

    data = load_model_catalog(cache_path=cache_path, fetcher=_catalog, now=1000.0)

    assert data["openai"]["name"] == "OpenAI"
    assert cache_path.exists()
    assert json.loads(cache_path.read_text(encoding="utf-8"))["anthropic"]["name"] == "Anthropic"


def test_load_model_catalog_uses_fresh_disk_without_network(tmp_path: Path) -> None:
    reset_model_catalog_cache()
    cache_path = tmp_path / "models-dev.json"
    cache_path.write_text(json.dumps(_catalog()), encoding="utf-8")
    os.utime(cache_path, (1000.0, 1000.0))

    data = load_model_catalog(
        cache_path=cache_path,
        fetcher=lambda: (_ for _ in ()).throw(AssertionError("network should not run")),
        now=1100.0,
    )

    assert provider_info("openai", catalog=data).model_count == 4


def test_recommended_models_filter_for_memory_enhancement() -> None:
    reset_model_catalog_cache()
    recommendations = recommended_memory_enhancement_models("openai", catalog=_catalog(), limit=5)

    assert [model.model_id for model in recommendations] == ["gpt-4o-mini", "expensive"]
    assert recommendations[0].estimated_memory_job_cost is not None
    assert default_memory_enhancement_model("anthropic", catalog=_catalog()) == "claude-haiku-4-5"
    assert default_memory_enhancement_model("google", catalog=_catalog()) == "gemini-2.5-flash"
    assert default_memory_enhancement_model("openrouter", catalog=_catalog()) == "openai/gpt-4o-mini"


def test_provider_models_hide_deprecated_by_default() -> None:
    models = provider_model_infos("openai", catalog=_catalog())

    assert "old" not in {model.model_id for model in models}
    assert "old" in {model.model_id for model in provider_model_infos("openai", catalog=_catalog(), include_deprecated=True)}


def test_provider_plan_can_use_models_dev_default_from_cache(tmp_path: Path, monkeypatch) -> None:
    reset_model_catalog_cache()
    cache_path = tmp_path / "models-dev.json"
    cache_path.write_text(json.dumps(_catalog()), encoding="utf-8")
    monkeypatch.setenv("CHIMERA_MEMORY_MODEL_CATALOG_CACHE", str(cache_path))

    plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_USE_MODELS_DEV_CATALOG": "true",
            "CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "anthropic,dry_run",
            "CHIMERA_MEMORY_ENHANCEMENT_ANTHROPIC_CREDENTIAL_REF": "oauth:anthropic-memory",
        }
    )

    assert plan.selected.provider_id == "anthropic"
    assert plan.selected.model == "claude-haiku-4-5"


def test_provider_plan_supports_gemini_and_openrouter_aliases(tmp_path: Path, monkeypatch) -> None:
    reset_model_catalog_cache()
    cache_path = tmp_path / "models-dev.json"
    cache_path.write_text(json.dumps(_catalog()), encoding="utf-8")
    monkeypatch.setenv("CHIMERA_MEMORY_MODEL_CATALOG_CACHE", str(cache_path))

    gemini_plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_USE_MODELS_DEV_CATALOG": "true",
            "CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "gemini,dry_run",
            "CHIMERA_MEMORY_ENHANCEMENT_GOOGLE_CREDENTIAL_REF": "oauth:gemini-memory",
        }
    )
    openrouter_plan = resolve_enhancement_provider_plan(
        {
            "CHIMERA_MEMORY_ENHANCEMENT_USE_MODELS_DEV_CATALOG": "true",
            "CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "openrouter,dry_run",
            "CHIMERA_MEMORY_ENHANCEMENT_OPENROUTER_CREDENTIAL_REF": "secret:openrouter-memory",
        }
    )

    assert gemini_plan.selected.provider_id == "google"
    assert gemini_plan.selected.model == "gemini-2.5-flash"
    assert openrouter_plan.selected.provider_id == "openrouter"
    assert openrouter_plan.selected.model == "openai/gpt-4o-mini"
