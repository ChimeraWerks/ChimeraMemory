"""Typed grading harness for memory-enhancement model runs."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

from .memory_enhancement import normalize_memory_enhancement_response

GRADE_SCHEMA_VERSION = "chimera-memory.enhancement.typed-grade.v1"
GRADE_THRESHOLD = 0.8
NON_TOPIC_ENTITY_TYPES = ("person", "project", "tool", "organization", "place", "date")
ENTITY_TYPES = (*NON_TOPIC_ENTITY_TYPES, "topic")
DEFAULT_ACTION_TEACHINGS = ("grep-before", "live-call-diff", "wire-level-validation")
_BUILTIN_TEACHING_PATTERNS = {
    "grep-before": ("grep.*before", "grep"),
    "grep-before-implementation": ("grep.*before", "grep", "look at the source", "read the reference"),
    "live-call-diff": ("live-call", "live call", "live request", "request.*response", "runtime behavior"),
    "ar-is-live-call-diff": ("live-call", "live call", "wire-level", "wire level", "accept/reject", "not.*constants"),
    "wire-level-validation": ("wire-level", "wire level", "accept/reject", "axis"),
    "wire-level-axis-independence": ("axis", "each.*independently", "headers", "endpoint", "scope", "request shape"),
}


def load_grade_records(paths: Sequence[str | Path]) -> list[dict[str, Any]]:
    """Load model-run records from JSONL files or JSON arrays."""
    records: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.suffix.lower() == ".jsonl":
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    records.append(parsed)
            continue
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, list):
            records.extend(item for item in parsed if isinstance(item, dict))
        elif isinstance(parsed, dict):
            raw_records = parsed.get("records") or parsed.get("runs")
            if isinstance(raw_records, list):
                records.extend(item for item in raw_records if isinstance(item, dict))
            else:
                records.append(parsed)
    return records


def load_action_teachings(path: str | Path) -> list[dict[str, Any]]:
    """Load expected action teachings from a Sarah-owned YAML fixture file."""
    import yaml

    parsed = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(parsed, Mapping):
        return []
    teachings = parsed.get("teachings")
    if not isinstance(teachings, list):
        return []
    return [dict(item) for item in teachings if isinstance(item, Mapping)]


def grade_memory_enhancement_records(
    records: Sequence[Mapping[str, Any]],
    *,
    expected_action_teachings: Sequence[str | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Grade records grouped by model label."""
    action_teachings = _normalize_teachings(expected_action_teachings or DEFAULT_ACTION_TEACHINGS)
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        groups[_model_label(record)].append(record)
    model_results = [
        grade_memory_enhancement_model_runs(
            model_label,
            model_records,
            expected_action_teachings=action_teachings,
        )
        for model_label, model_records in sorted(groups.items())
    ]
    passing = [result["model_label"] for result in model_results if result["gate"]["pass"]]
    return {
        "schema": GRADE_SCHEMA_VERSION,
        "threshold": GRADE_THRESHOLD,
        "action_teachings": [teaching["id"] for teaching in action_teachings],
        "model_count": len(model_results),
        "passing_models": passing,
        "models": model_results,
    }


def grade_memory_enhancement_model_runs(
    model_label: str,
    records: Sequence[Mapping[str, Any]],
    *,
    expected_action_teachings: Sequence[str | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Grade repeated runs for one model."""
    action_teachings = _normalize_teachings(expected_action_teachings or DEFAULT_ACTION_TEACHINGS)
    normalized_records = [_normalized_record(record, index=index) for index, record in enumerate(records, start=1)]
    successful = [record for record in normalized_records if record["ok"]]
    contracts = {tuple(sorted(record["contract"].items())) for record in successful}
    topics = [record["entity_sets"]["topic"] for record in successful]
    typed_entities = [
        set().union(*(record["entity_sets"][entity_type] for entity_type in NON_TOPIC_ENTITY_TYPES))
        for record in successful
    ]
    action_items = [record["action_items"] for record in successful]
    action_texts = [record["action_item_texts"] for record in successful]

    topic_score = _set_score(topics)
    entity_flat_score = _set_score(typed_entities)
    entity_per_type_score = _per_type_entity_score(successful)
    action_jaccard_score = _set_score(action_items)
    action_teaching_score = _action_teaching_score(action_texts, action_teachings)
    per_type_scores = {
        entity_type: _set_score([record["entity_sets"][entity_type] for record in successful])
        for entity_type in NON_TOPIC_ENTITY_TYPES
    }
    coverage = {
        "all_runs_ok": len(successful) == len(normalized_records),
        "runs_with_topics": sum(1 for value in topics if value),
        "runs_with_typed_entities": sum(1 for value in typed_entities if value),
        "runs_with_action_items": sum(1 for value in action_items if value),
    }
    run_count = len(normalized_records)
    success_count = len(successful)
    gate = {
        "run_count_min": run_count >= 2,
        "all_runs_ok": coverage["all_runs_ok"],
        "contract_stable": len(contracts) == 1 and success_count > 0,
        "topics_covered": coverage["runs_with_topics"] == success_count and success_count > 0,
        "typed_entities_covered": coverage["runs_with_typed_entities"] == success_count and success_count > 0,
        "action_items_covered": coverage["runs_with_action_items"] == success_count and success_count > 0,
        "topic_jaccard_pass": topic_score["pairwise_mean"] >= GRADE_THRESHOLD,
        "typed_entity_jaccard_pass": entity_per_type_score["pairwise_mean"] >= GRADE_THRESHOLD,
        "action_items_preserved": action_teaching_score["pass"],
    }
    gate["pass"] = all(gate.values())

    return {
        "schema": GRADE_SCHEMA_VERSION,
        "model_label": model_label,
        "provider": _first_nonempty(record.get("provider") for record in records),
        "model": _first_nonempty(record.get("model") for record in records),
        "run_count": run_count,
        "successful_count": success_count,
        "latency_mean_seconds": _rounded_mean(record["elapsed_seconds"] for record in successful),
        "contract_values": [dict(value) for value in sorted(contracts)],
        "coverage": coverage,
        "scores": {
            "topics": topic_score,
            "typed_entities": entity_per_type_score,
            "typed_entities_flat": entity_flat_score,
            "action_items": action_teaching_score,
            "action_items_jaccard": action_jaccard_score,
            "per_entity_type": per_type_scores,
        },
        "gate": gate,
        "runs": normalized_records,
    }


def _normalized_record(record: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    metadata = normalize_memory_enhancement_response(_metadata_payload(record))
    entity_sets = {entity_type: set() for entity_type in ENTITY_TYPES}
    for entity in metadata["entities"]:
        entity_type = str(entity.get("type") or "")
        if entity_type not in entity_sets:
            continue
        name_key = _grade_key(entity.get("name"))
        if name_key:
            entity_sets[entity_type].add(f"{entity_type}:{name_key}")
    return {
        "run": int(record.get("model_pass_number") or record.get("run") or index),
        "ok": bool(record.get("ok", True)),
        "elapsed_seconds": _float(record.get("elapsed_seconds")),
        "contract": {
            "type": metadata["memory_type"],
            "sensitivity": metadata["sensitivity_tier"],
            "can_use_as_instruction": metadata["can_use_as_instruction"],
        },
        "entity_sets": {key: sorted(value) for key, value in entity_sets.items()},
        "action_items": sorted({_grade_key(item) for item in metadata["action_items"] if _grade_key(item)}),
        "action_item_texts": sorted({str(item).strip() for item in metadata["action_items"] if str(item).strip()}),
    }


def _metadata_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    raw_metadata = record.get("metadata")
    if isinstance(raw_metadata, Mapping):
        return dict(raw_metadata)

    payload: dict[str, Any] = {
        "summary": record.get("summary"),
        "topics": record.get("topics"),
        "action_items": record.get("action_items"),
        "confidence": record.get("confidence"),
    }
    contract = record.get("contract") if isinstance(record.get("contract"), Mapping) else {}
    payload["memory_type"] = contract.get("type") or record.get("memory_type") or record.get("type")
    payload["sensitivity_tier"] = (
        contract.get("sensitivity") or record.get("sensitivity_tier") or record.get("sensitivity")
    )
    categories = record.get("entity_categories")
    if isinstance(categories, Mapping):
        for field in ("people", "projects", "tools", "dates", "organizations", "places"):
            payload[field] = categories.get(field)
    else:
        for field in ("people", "projects", "tools", "dates", "organizations", "places"):
            payload[field] = record.get(field)
        raw_entities = record.get("entities")
        if isinstance(raw_entities, list) and all(isinstance(item, Mapping) for item in raw_entities):
            payload["entities"] = raw_entities
    return payload


def _set_score(values: Sequence[set[str] | list[str]]) -> dict[str, Any]:
    sets = [set(value) for value in values]
    if len(sets) < 2:
        return {"pairwise_mean": 0.0, "pairwise_min": 0.0, "pair_count": 0}
    if not any(sets):
        return {"pairwise_mean": 0.0, "pairwise_min": 0.0, "pair_count": len(list(combinations(sets, 2)))}
    scores = [_jaccard(left, right) for left, right in combinations(sets, 2)]
    return {
        "pairwise_mean": round(mean(scores), 3),
        "pairwise_min": round(min(scores), 3),
        "pair_count": len(scores),
    }


def _per_type_entity_score(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(records) < 2:
        return {"pairwise_mean": 0.0, "pairwise_min": 0.0, "pair_count": 0, "type_count_mean": 0.0}
    scores: list[float] = []
    type_counts: list[int] = []
    for left, right in combinations(records, 2):
        type_scores = []
        for entity_type in NON_TOPIC_ENTITY_TYPES:
            left_values = set(left["entity_sets"][entity_type])
            right_values = set(right["entity_sets"][entity_type])
            if not left_values and not right_values:
                continue
            type_scores.append(_jaccard(left_values, right_values))
        if type_scores:
            scores.append(mean(type_scores))
            type_counts.append(len(type_scores))
        else:
            scores.append(0.0)
            type_counts.append(0)
    return {
        "pairwise_mean": round(mean(scores), 3),
        "pairwise_min": round(min(scores), 3),
        "pair_count": len(scores),
        "type_count_mean": round(mean(type_counts), 3) if type_counts else 0.0,
    }


def _action_teaching_score(
    action_items_by_run: Sequence[Sequence[str]],
    teachings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    run_results: list[dict[str, Any]] = []
    for index, action_items in enumerate(action_items_by_run, start=1):
        present = [teaching["id"] for teaching in teachings if _teaching_present(teaching, action_items)]
        missing = [teaching["id"] for teaching in teachings if teaching["id"] not in present]
        run_results.append(
            {
                "run": index,
                "present": present,
                "missing": missing,
                "pass": not missing,
            }
        )
    passed_runs = sum(1 for result in run_results if result["pass"])
    return {
        "mode": "core-teaching",
        "teachings": [teaching["id"] for teaching in teachings],
        "passed_runs": passed_runs,
        "run_count": len(run_results),
        "pass": bool(run_results) and passed_runs == len(run_results),
        "runs": run_results,
    }


def _teaching_present(teaching: Mapping[str, Any], action_items: Sequence[str]) -> bool:
    patterns = teaching.get("match_patterns")
    if not isinstance(patterns, list) or not patterns:
        patterns = list(_BUILTIN_TEACHING_PATTERNS.get(str(teaching.get("id") or ""), (str(teaching.get("id") or ""),)))
    search_texts = []
    for item in action_items:
        text = f"{str(item)}\n{_grade_key(item)}"
        search_texts.append(text)
        search_texts.append(text.replace("axes", "axis"))
    for pattern in patterns:
        pattern_text = str(pattern or "").strip()
        if not pattern_text:
            continue
        for action in search_texts:
            try:
                if re.search(pattern_text, action, flags=re.IGNORECASE):
                    return True
            except re.error:
                if pattern_text.lower() in action.lower():
                    return True
    return False


def _normalize_teachings(
    teachings: Sequence[str | Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    normalized = []
    for teaching in teachings:
        if isinstance(teaching, Mapping):
            teaching_id = str(teaching.get("id") or "").strip()
            patterns = teaching.get("match_patterns")
            if not teaching_id:
                continue
            normalized.append(
                {
                    "id": teaching_id,
                    "match_patterns": list(patterns) if isinstance(patterns, list) else [],
                }
            )
            continue
        teaching_id = str(teaching or "").strip()
        if not teaching_id:
            continue
        normalized.append(
            {
                "id": teaching_id,
                "match_patterns": list(_BUILTIN_TEACHING_PATTERNS.get(teaching_id, (teaching_id,))),
            }
        )
    return tuple(normalized)


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _grade_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").split())


def _model_label(record: Mapping[str, Any]) -> str:
    return str(record.get("model_label") or record.get("model") or "unknown").strip() or "unknown"


def _first_nonempty(values: Iterable[Any]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rounded_mean(values: Sequence[float]) -> float:
    numbers = [float(value) for value in values]
    if not numbers:
        return 0.0
    return round(mean(numbers), 3)
