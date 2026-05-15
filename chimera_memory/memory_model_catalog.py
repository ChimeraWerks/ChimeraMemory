"""Model catalog helpers for memory-enhancement provider selection.

The catalog source is models.dev. This module keeps the integration narrow:
only provider/model metadata needed by memory enhancement is parsed, and all
network/cache failures fall back to a bundled snapshot.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .memory_enhancement_google import GOOGLE_CLOUDCODE_MEMORY_MODEL_CANDIDATES

MODELS_DEV_URL = "https://models.dev/api.json"
MEMORY_ENHANCEMENT_PROVIDER_IDS = {"openai", "anthropic", "google", "openrouter", "lmstudio"}
DISK_CACHE_TTL_SECONDS = 24 * 60 * 60
REFRESH_INTERVAL_SECONDS = 60 * 60
FETCH_TIMEOUT_SECONDS = 15

_MODEL_CATALOG_CACHE: dict[str, Any] | None = None
_MODEL_CATALOG_CACHE_TIME = 0.0

_PREFERRED_MEMORY_MODELS: dict[str, tuple[str, ...]] = {
    "openai": (
        "gpt-4o-mini",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "gpt-5-mini",
        "gpt-5-nano",
    ),
    "anthropic": (
        "claude-haiku-4-5",
        "claude-3-haiku-20240307",
    ),
    "google": (
        *GOOGLE_CLOUDCODE_MEMORY_MODEL_CANDIDATES,
    ),
    "openrouter": (
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
        "anthropic/claude-haiku-4.5",
        "google/gemini-2.5-flash-lite",
        "google/gemini-2.5-flash",
    ),
    "lmstudio": (
        "openai/gpt-oss-20b",
        "qwen/qwen3-coder-30b",
        "qwen/qwen3-30b-a3b-2507",
    ),
}


@dataclass(frozen=True)
class ModelInfo:
    """Small provider-model metadata record parsed from models.dev."""

    provider_id: str
    model_id: str
    name: str
    family: str = ""
    status: str = ""
    context_window: int = 0
    max_output_tokens: int = 0
    cost_input_per_million: float | None = None
    cost_output_per_million: float | None = None
    supports_tool_call: bool = False
    supports_structured_output: bool = False
    supports_reasoning: bool = False
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()

    @property
    def is_deprecated(self) -> bool:
        return self.status.lower() == "deprecated"

    @property
    def is_text_output(self) -> bool:
        return "text" in self.output_modalities

    @property
    def has_cost_data(self) -> bool:
        return self.cost_input_per_million is not None or self.cost_output_per_million is not None

    @property
    def estimated_memory_job_cost(self) -> float | None:
        """Estimated USD for the default 500-in/200-out enhancement budget."""
        if not self.has_cost_data:
            return None
        input_cost = self.cost_input_per_million or 0.0
        output_cost = self.cost_output_per_million or 0.0
        return (input_cost * 500 / 1_000_000) + (output_cost * 200 / 1_000_000)


@dataclass(frozen=True)
class ProviderInfo:
    """Small provider metadata record parsed from models.dev."""

    provider_id: str
    name: str
    api: str = ""
    env_vars: tuple[str, ...] = ()
    doc_url: str = ""
    model_count: int = 0


def default_model_catalog_cache_path() -> Path:
    configured = os.environ.get("CHIMERA_MEMORY_MODEL_CATALOG_CACHE", "").strip()
    if configured:
        return Path(configured)
    return Path.home() / ".chimera-memory" / "cache" / "models-dev.json"


def reset_model_catalog_cache() -> None:
    """Clear the in-process catalog cache. Used by tests."""
    global _MODEL_CATALOG_CACHE, _MODEL_CATALOG_CACHE_TIME
    _MODEL_CATALOG_CACHE = None
    _MODEL_CATALOG_CACHE_TIME = 0.0


def load_model_catalog(
    *,
    cache_path: str | Path | None = None,
    force_refresh: bool = False,
    fetcher: Callable[[], Mapping[str, Any] | None] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Load models.dev data with memory cache, disk cache, network, snapshot fallback."""
    global _MODEL_CATALOG_CACHE, _MODEL_CATALOG_CACHE_TIME

    current_time = time.time() if now is None else now
    if (
        not force_refresh
        and _MODEL_CATALOG_CACHE is not None
        and (current_time - _MODEL_CATALOG_CACHE_TIME) < REFRESH_INTERVAL_SECONDS
    ):
        return _MODEL_CATALOG_CACHE

    path = Path(cache_path) if cache_path is not None else default_model_catalog_cache_path()
    disk_data, disk_mtime = _read_catalog_file(path)
    disk_age = current_time - disk_mtime if disk_data is not None and disk_mtime > 0 else None
    disk_within_refresh = disk_age is not None and disk_age < REFRESH_INTERVAL_SECONDS
    disk_within_ttl = disk_age is not None and disk_age < DISK_CACHE_TTL_SECONDS
    if not force_refresh and disk_data is not None and disk_within_refresh:
        _MODEL_CATALOG_CACHE = disk_data
        _MODEL_CATALOG_CACHE_TIME = current_time
        return disk_data

    fetched = (fetcher or _fetch_models_dev)()
    if fetched is not None and _validate_catalog(fetched):
        data = dict(fetched)
        _write_catalog_file(path, data)
        _MODEL_CATALOG_CACHE = data
        _MODEL_CATALOG_CACHE_TIME = current_time
        return data

    if disk_data is not None and disk_within_ttl:
        _MODEL_CATALOG_CACHE = disk_data
        _MODEL_CATALOG_CACHE_TIME = current_time
        return disk_data

    if disk_data is not None:
        _MODEL_CATALOG_CACHE = disk_data
        _MODEL_CATALOG_CACHE_TIME = current_time
        return disk_data

    snapshot = _load_bundled_snapshot()
    _MODEL_CATALOG_CACHE = snapshot
    _MODEL_CATALOG_CACHE_TIME = current_time
    return snapshot


def provider_info(provider_id: str, *, catalog: Mapping[str, Any] | None = None) -> ProviderInfo | None:
    provider = _provider_block(provider_id, catalog=catalog)
    if provider is None:
        return None
    models = provider.get("models") if isinstance(provider.get("models"), Mapping) else {}
    return ProviderInfo(
        provider_id=str(provider.get("id") or provider_id),
        name=str(provider.get("name") or provider_id),
        api=str(provider.get("api") or ""),
        env_vars=tuple(str(value) for value in provider.get("env", ()) if str(value).strip())
        if isinstance(provider.get("env"), list)
        else (),
        doc_url=str(provider.get("doc") or ""),
        model_count=len(models),
    )


def provider_model_infos(
    provider_id: str,
    *,
    catalog: Mapping[str, Any] | None = None,
    include_deprecated: bool = False,
) -> tuple[ModelInfo, ...]:
    provider = _provider_block(provider_id, catalog=catalog)
    if provider is None:
        return ()
    models = provider.get("models")
    if not isinstance(models, Mapping):
        return ()

    out: list[ModelInfo] = []
    for model_id, raw_model in models.items():
        if not isinstance(raw_model, Mapping):
            continue
        model = _model_info_from_raw(provider_id, str(model_id), raw_model)
        if model.is_deprecated and not include_deprecated:
            continue
        out.append(model)
    return tuple(out)


def recommended_memory_enhancement_models(
    provider_id: str,
    *,
    catalog: Mapping[str, Any] | None = None,
    limit: int = 5,
) -> tuple[ModelInfo, ...]:
    """Return cheap text-output models suitable for structured memory extraction."""
    models = [
        model
        for model in provider_model_infos(provider_id, catalog=catalog)
        if model.is_text_output and model.context_window >= 4_000
    ]
    if not models:
        return ()

    by_id = {model.model_id: model for model in models}
    preferred = [by_id[model_id] for model_id in _PREFERRED_MEMORY_MODELS.get(provider_id, ()) if model_id in by_id]
    preferred_ids = {model.model_id for model in preferred}
    remaining = [model for model in models if model.model_id not in preferred_ids]
    remaining.sort(key=_memory_model_sort_key)
    return tuple((preferred + remaining)[: max(1, limit)])


def default_memory_enhancement_model(provider_id: str, *, catalog: Mapping[str, Any] | None = None) -> str:
    recommended = recommended_memory_enhancement_models(provider_id, catalog=catalog, limit=1)
    if not recommended:
        return ""
    return recommended[0].model_id


def _provider_block(provider_id: str, *, catalog: Mapping[str, Any] | None = None) -> Mapping[str, Any] | None:
    data = catalog if catalog is not None else load_model_catalog()
    provider = data.get(provider_id) if isinstance(data, Mapping) else None
    return provider if isinstance(provider, Mapping) else None


def _model_info_from_raw(provider_id: str, model_id: str, raw_model: Mapping[str, Any]) -> ModelInfo:
    limit = raw_model.get("limit") if isinstance(raw_model.get("limit"), Mapping) else {}
    cost = raw_model.get("cost") if isinstance(raw_model.get("cost"), Mapping) else {}
    modalities = raw_model.get("modalities") if isinstance(raw_model.get("modalities"), Mapping) else {}
    return ModelInfo(
        provider_id=provider_id,
        model_id=str(raw_model.get("id") or model_id),
        name=str(raw_model.get("name") or raw_model.get("id") or model_id),
        family=str(raw_model.get("family") or ""),
        status=str(raw_model.get("status") or ""),
        context_window=_positive_int(limit.get("context")),
        max_output_tokens=_positive_int(limit.get("output")),
        cost_input_per_million=_number_or_none(cost.get("input")),
        cost_output_per_million=_number_or_none(cost.get("output")),
        supports_tool_call=bool(raw_model.get("tool_call")),
        supports_structured_output=bool(raw_model.get("structured_output")),
        supports_reasoning=bool(raw_model.get("reasoning")),
        input_modalities=tuple(str(value) for value in modalities.get("input", ()) if str(value).strip())
        if isinstance(modalities.get("input"), list)
        else (),
        output_modalities=tuple(str(value) for value in modalities.get("output", ()) if str(value).strip())
        if isinstance(modalities.get("output"), list)
        else (),
    )


def _memory_model_sort_key(model: ModelInfo) -> tuple[float, int, str]:
    estimated = model.estimated_memory_job_cost
    cost_key = estimated if estimated is not None and estimated > 0 else 999_999.0
    capability_penalty = 0 if (model.supports_tool_call or model.supports_structured_output) else 1
    return (cost_key + capability_penalty, -model.context_window, model.model_id)


def _fetch_models_dev() -> Mapping[str, Any] | None:
    try:
        request = urllib.request.Request(
            MODELS_DEV_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "chimera-memory/0.1",
            },
        )
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _validate_catalog(data: Mapping[str, Any] | None) -> bool:
    if not isinstance(data, Mapping) or not data:
        return False
    for provider_id, provider in data.items():
        if not isinstance(provider_id, str) or not isinstance(provider, Mapping):
            return False
        models = provider.get("models")
        if not isinstance(models, Mapping):
            return False
        for model_id, model in models.items():
            if not isinstance(model_id, str) or not isinstance(model, Mapping):
                return False
    return True


def _read_catalog_file(path: Path) -> tuple[dict[str, Any] | None, float]:
    try:
        mtime = path.stat().st_mtime
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, 0.0
    if not _validate_catalog(data):
        return None, 0.0
    return data, mtime


def _write_catalog_file(path: Path, data: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(data, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)
    except OSError:
        return


def _load_bundled_snapshot() -> dict[str, Any]:
    try:
        text = resources.files("chimera_memory.data").joinpath("models_dev_snapshot.json").read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ModuleNotFoundError, json.JSONDecodeError):
        data = {}
    return data if _validate_catalog(data) else _minimal_catalog_snapshot()


def _minimal_catalog_snapshot() -> dict[str, Any]:
    return {
        "openai": {
            "id": "openai",
            "name": "OpenAI",
            "models": {
                "gpt-4o-mini": {
                    "id": "gpt-4o-mini",
                    "name": "GPT-4o mini",
                    "tool_call": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 128000, "output": 4096},
                    "cost": {"input": 0.15, "output": 0.60},
                }
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
                    "cost": {"input": 1.00, "output": 5.00},
                }
            },
        },
        "google": {
            "id": "google",
            "name": "Google",
            "models": {
                "gemini-3-flash-preview": {
                    "id": "gemini-3-flash-preview",
                    "name": "Gemini 3 Flash Preview",
                    "tool_call": True,
                    "structured_output": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 1048576, "output": 8192},
                    "cost": {"input": 0.30, "output": 2.50},
                },
                "gemini-3.1-pro-preview": {
                    "id": "gemini-3.1-pro-preview",
                    "name": "Gemini 3.1 Pro Preview",
                    "tool_call": True,
                    "structured_output": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 1048576, "output": 8192},
                    "cost": {"input": 1.25, "output": 10.00},
                },
                "gemini-3-pro-preview": {
                    "id": "gemini-3-pro-preview",
                    "name": "Gemini 3 Pro Preview",
                    "tool_call": True,
                    "structured_output": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 1048576, "output": 8192},
                    "cost": {"input": 1.25, "output": 10.00},
                }
            },
        },
        "openrouter": {
            "id": "openrouter",
            "name": "OpenRouter",
            "api": "https://openrouter.ai/api/v1",
            "models": {
                "openai/gpt-4o-mini": {
                    "id": "openai/gpt-4o-mini",
                    "name": "OpenAI GPT-4o mini",
                    "tool_call": True,
                    "structured_output": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 128000, "output": 4096},
                    "cost": {"input": 0.15, "output": 0.60},
                }
            },
        },
        "lmstudio": {
            "id": "lmstudio",
            "name": "LM Studio",
            "models": {},
        },
    }


def _positive_int(value: object) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _number_or_none(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
