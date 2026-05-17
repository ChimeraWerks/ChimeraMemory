"""LLM-assisted classifier for typed reasoning edges between memory files."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from .memory_file_edges import MEMORY_FILE_EDGE_RELATION_TYPES, memory_file_edge_upsert
from .memory_frontmatter import parse_frontmatter
from .memory_observability import record_memory_audit_event
from .memory_enhancement_provider import (
    build_enhancement_invocation,
    resolve_enhancement_provider_plan,
    safe_provider_receipt,
)

CLASSIFIER_VERSION = "memory-file-edge-classifier.v1"
EDGE_DIRECTIONS = {"A_to_B", "B_to_A", "symmetric"}
MAX_PAIR_BODY_CHARS = 800


class MemoryFileEdgeClassifierClient(Protocol):
    """Client boundary for edge-classification LLM calls."""

    def invoke(self, invocation: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return one strict JSON object for a filter or classify invocation."""


def sample_memory_file_edge_candidates(
    conn: sqlite3.Connection,
    *,
    persona: str | None = None,
    min_support: int = 2,
    limit: int = 20,
    include_related: bool = False,
) -> list[dict[str, Any]]:
    """Return memory-file pairs sharing enough entities to merit classification."""
    conditions = ["1 = 1"]
    params: list[object] = []
    if persona:
        conditions.append("mf.persona = ?")
        params.append(persona)
    rows = conn.execute(
        f"""
        SELECT mfe.file_id, mfe.entity_id, me.entity_type, me.canonical_name
        FROM memory_file_entities mfe
        JOIN memory_files mf ON mf.id = mfe.file_id
        JOIN memory_entities me ON me.id = mfe.entity_id
        WHERE {' AND '.join(conditions)}
        ORDER BY mfe.file_id ASC, me.entity_type ASC, me.canonical_name ASC
        """,
        params,
    ).fetchall()
    file_entities: dict[int, dict[int, dict[str, Any]]] = {}
    for row in rows:
        file_entities.setdefault(int(row[0]), {})[int(row[1])] = {
            "entity_id": int(row[1]),
            "entity_type": row[2],
            "canonical_name": row[3],
        }

    candidates: list[dict[str, Any]] = []
    file_ids = sorted(file_entities)
    for left_index, left_id in enumerate(file_ids):
        left_entities = set(file_entities[left_id])
        for right_id in file_ids[left_index + 1 :]:
            shared_ids = sorted(left_entities.intersection(file_entities[right_id]))
            if len(shared_ids) < max(1, int(min_support)):
                continue
            if _edge_pair_already_classified(conn, left_id, right_id, include_related=include_related):
                continue
            left = _memory_file_for_classifier(conn, left_id)
            right = _memory_file_for_classifier(conn, right_id)
            if left is None or right is None:
                continue
            candidates.append(
                {
                    "source_file_id": left_id,
                    "target_file_id": right_id,
                    "support": len(shared_ids),
                    "shared_entities": [file_entities[left_id][entity_id] for entity_id in shared_ids],
                    "source": left,
                    "target": right,
                }
            )
    candidates.sort(key=lambda item: (-int(item["support"]), item["source"]["relative_path"], item["target"]["relative_path"]))
    return candidates[: max(0, min(int(limit), 500))]


def run_memory_file_edge_classifier_batch(
    conn: sqlite3.Connection,
    *,
    client: MemoryFileEdgeClassifierClient,
    env: Mapping[str, str] | None = None,
    persona: str | None = None,
    limit: int = 20,
    min_support: int = 2,
    min_confidence: float = 0.75,
    dry_run: bool = True,
    hybrid: bool = True,
    actor: str = "edge-classifier",
) -> dict[str, Any]:
    """Classify candidate file pairs and optionally upsert typed reasoning edges."""
    plan = resolve_enhancement_provider_plan(os.environ if env is None else env)
    candidates = sample_memory_file_edge_candidates(
        conn,
        persona=persona,
        min_support=min_support,
        limit=limit,
    )
    results: list[dict[str, Any]] = []
    llm_call_count = 0
    for candidate in candidates:
        result, calls = classify_memory_file_edge_candidate(
            conn,
            candidate=candidate,
            client=client,
            plan=plan,
            min_confidence=min_confidence,
            dry_run=dry_run,
            hybrid=hybrid,
            actor=actor,
        )
        llm_call_count += calls
        results.append(result)
    counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    record_memory_audit_event(
        conn,
        "memory_file_edge_classifier_batch",
        persona=persona,
        target_kind="memory_file_edges",
        target_id=persona or "all",
        payload={
            "dry_run": dry_run,
            "candidate_count": len(candidates),
            "llm_call_count": llm_call_count,
            "status_counts": counts,
            "classifier_version": CLASSIFIER_VERSION,
        },
        actor=actor,
        commit=False,
    )
    conn.commit()
    return {
        "ok": True,
        "dry_run": dry_run,
        "provider": safe_provider_receipt(plan),
        "candidate_count": len(candidates),
        "llm_call_count": llm_call_count,
        "status_counts": counts,
        "results": results,
    }


def classify_memory_file_edge_candidate(
    conn: sqlite3.Connection,
    *,
    candidate: Mapping[str, Any],
    client: MemoryFileEdgeClassifierClient,
    plan: Any,
    min_confidence: float = 0.75,
    dry_run: bool = True,
    hybrid: bool = True,
    actor: str = "edge-classifier",
) -> tuple[dict[str, Any], int]:
    """Classify one sampled pair, returning the result and LLM-call count."""
    calls = 0
    if hybrid:
        calls += 1
        filter_response = dict(client.invoke(_edge_filter_invocation(candidate, plan)))
        if not bool(filter_response.get("worth_classifying")):
            return (_edge_result(candidate, "filter_rejected", hunch=str(filter_response.get("hunch") or "none")), calls)

    calls += 1
    response = _normalize_classification(dict(client.invoke(_edge_classify_invocation(candidate, plan))))
    status = _classification_status(response, min_confidence=min_confidence)
    if status != "would_insert":
        return (_edge_result(candidate, status, **response), calls)

    source, target = _directed_pair(candidate, response["direction"])
    label = (
        f"{source['relative_path']} -[{response['relation']}]-> "
        f"{target['relative_path']}"
    )
    if dry_run:
        return (_edge_result(candidate, "would_insert", label=label, **response), calls)

    upserted = memory_file_edge_upsert(
        conn,
        source_file_path=str(source["id"]),
        target_file_path=str(target["id"]),
        relation_type=response["relation"],
        confidence=response["confidence"],
        valid_from=response.get("valid_from") or None,
        valid_until=response.get("valid_until") or None,
        classifier_version=CLASSIFIER_VERSION,
        evidence=response.get("rationale", ""),
        metadata={
            "classifier": CLASSIFIER_VERSION,
            "direction": response["direction"],
            "shared_entities": candidate.get("shared_entities", []),
        },
        actor=actor,
    )
    if not upserted.get("ok"):
        return (_edge_result(candidate, "insert_failed", reason=str(upserted.get("error") or "unknown"), **response), calls)
    return (_edge_result(candidate, "inserted", edge_id=upserted["edge"]["edge_id"], label=label, **response), calls)


def _edge_filter_invocation(candidate: Mapping[str, Any], plan: Any) -> dict[str, Any]:
    return _edge_invocation(
        candidate,
        plan,
        task="filter_memory_file_edge_candidate",
        system_prompt=(
            "You are a fast pre-filter for a reasoning-edge classifier. "
            "Given two memory files, answer whether there is any meaningful semantic relation "
            "beyond simple co-mention. Meaningful relations include supports, contradicts, "
            "evolved_into, supersedes, and depends_on. Reply with strict JSON only: "
            '{"worth_classifying": true|false, "hunch": "<one-word relation or none>"}'
        ),
        user_prompt=_pair_prompt(candidate, max_chars=400, suffix="Is there a meaningful relation?"),
        max_output_tokens=128,
    )


def _edge_classify_invocation(candidate: Mapping[str, Any], plan: Any) -> dict[str, Any]:
    return _edge_invocation(
        candidate,
        plan,
        task="classify_memory_file_edge",
        system_prompt=(
            "Classify the semantic relationship between two memory files. "
            "Allowed relation types: supports, contradicts, evolved_into, supersedes, depends_on, related_to, none. "
            "Use supports when A strengthens or provides evidence for B. "
            "Use contradicts only when A directly disagrees with or disproves B. "
            "Use evolved_into when A was replaced by a refined or updated B over time. "
            "Use supersedes when A is the newer surviving replacement for B. "
            "Use depends_on when A is conditional on B being true or complete. "
            "Use related_to sparingly for real association without a stronger label. "
            "Return none for mere co-mention or ambiguity. "
            "Direction must be A_to_B, B_to_A, or symmetric. "
            "Return strict JSON only: "
            '{"relation":"<type|none>","direction":"A_to_B|B_to_A|symmetric",'
            '"confidence":0.0,"rationale":"...","valid_from":null,"valid_until":null}'
        ),
        user_prompt=_pair_prompt(candidate, max_chars=MAX_PAIR_BODY_CHARS, suffix="Classify the relationship."),
        max_output_tokens=512,
    )


def _edge_invocation(
    candidate: Mapping[str, Any],
    plan: Any,
    *,
    task: str,
    system_prompt: str,
    user_prompt: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    request = {
        "task": task,
        "source_file": _safe_file_descriptor(candidate.get("source")),
        "target_file": _safe_file_descriptor(candidate.get("target")),
        "shared_entities": candidate.get("shared_entities", []),
    }
    invocation = build_enhancement_invocation(request, plan)
    invocation["system_prompt"] = system_prompt
    invocation["user_prompt"] = user_prompt
    invocation["raw_json"] = True
    invocation["budget"] = dict(invocation.get("budget") or {})
    invocation["budget"]["max_output_tokens"] = max_output_tokens
    return invocation


def _pair_prompt(candidate: Mapping[str, Any], *, max_chars: int, suffix: str) -> str:
    source = _safe_file_descriptor(candidate.get("source"))
    target = _safe_file_descriptor(candidate.get("target"))
    shared = ", ".join(
        f"{item.get('canonical_name')} ({item.get('entity_type')})"
        for item in candidate.get("shared_entities", [])
        if isinstance(item, Mapping)
    )
    return (
        f"Memory A (id={source['id']}, path={source['relative_path']}, type={source['type']}):\n"
        f"about: {source['about']}\n"
        f"{source['body'][:max_chars]}\n\n"
        f"Memory B (id={target['id']}, path={target['relative_path']}, type={target['type']}):\n"
        f"about: {target['about']}\n"
        f"{target['body'][:max_chars]}\n\n"
        f"Shared entities: {shared or 'none'}\n\n"
        f"{suffix} Return strict JSON."
    )


def _normalize_classification(payload: Mapping[str, Any]) -> dict[str, Any]:
    relation = str(payload.get("relation") or "none").strip().lower()
    if relation not in MEMORY_FILE_EDGE_RELATION_TYPES and relation != "none":
        relation = "none"
    direction = str(payload.get("direction") or "A_to_B").strip()
    if direction not in EDGE_DIRECTIONS:
        direction = "A_to_B"
    confidence = _confidence(payload.get("confidence"))
    return {
        "relation": relation,
        "direction": direction,
        "confidence": confidence,
        "rationale": str(payload.get("rationale") or "")[:500],
        "valid_from": _nullable_date(payload.get("valid_from")),
        "valid_until": _nullable_date(payload.get("valid_until")),
    }


def _classification_status(payload: Mapping[str, Any], *, min_confidence: float) -> str:
    relation = str(payload.get("relation") or "none")
    if relation == "none" or relation not in MEMORY_FILE_EDGE_RELATION_TYPES:
        return "none"
    if float(payload.get("confidence") or 0.0) < max(0.0, min(1.0, float(min_confidence))):
        return "below_confidence"
    return "would_insert"


def _directed_pair(candidate: Mapping[str, Any], direction: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    source = _safe_file_descriptor(candidate.get("source"))
    target = _safe_file_descriptor(candidate.get("target"))
    if direction == "B_to_A":
        return target, source
    if direction == "symmetric":
        return tuple(sorted((source, target), key=lambda item: str(item["relative_path"])))  # type: ignore[return-value]
    return source, target


def _edge_pair_already_classified(
    conn: sqlite3.Connection,
    left_id: int,
    right_id: int,
    *,
    include_related: bool,
) -> bool:
    relation_clause = "" if include_related else "AND relation_type != 'related_to'"
    row = conn.execute(
        f"""
        SELECT 1
        FROM memory_file_edges
        WHERE ((source_file_id = ? AND target_file_id = ?)
            OR (source_file_id = ? AND target_file_id = ?))
          {relation_clause}
        LIMIT 1
        """,
        (left_id, right_id, right_id, left_id),
    ).fetchone()
    return row is not None


def _memory_file_for_classifier(conn: sqlite3.Connection, file_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, path, persona, relative_path, fm_type, fm_about, fm_created
        FROM memory_files
        WHERE id = ?
        """,
        (file_id,),
    ).fetchone()
    if row is None:
        return None
    body = ""
    try:
        _, body = parse_frontmatter(Path(row[1]).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        body = ""
    return {
        "id": int(row[0]),
        "path": row[1],
        "persona": row[2],
        "relative_path": row[3],
        "type": row[4],
        "about": row[5],
        "created": row[6],
        "body": body[:MAX_PAIR_BODY_CHARS],
    }


def _safe_file_descriptor(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"id": "", "relative_path": "", "type": "", "about": "", "body": ""}
    return {
        "id": value.get("id"),
        "relative_path": str(value.get("relative_path") or ""),
        "type": str(value.get("type") or ""),
        "about": str(value.get("about") or ""),
        "body": str(value.get("body") or ""),
    }


def _edge_result(candidate: Mapping[str, Any], status: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "source_file_id": candidate.get("source_file_id"),
        "target_file_id": candidate.get("target_file_id"),
        "support": candidate.get("support"),
        "source_path": _safe_file_descriptor(candidate.get("source")).get("relative_path"),
        "target_path": _safe_file_descriptor(candidate.get("target")).get("relative_path"),
        **extra,
    }


def _confidence(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, parsed))


def _nullable_date(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() == "null":
        return None
    return text[:32]


class StaticMemoryFileEdgeClassifierClient:
    """Deterministic test client for edge-classifier wiring."""

    def __init__(self, responses: Sequence[Mapping[str, Any]]):
        self._responses = list(responses)
        self.invocations: list[Mapping[str, Any]] = []

    def invoke(self, invocation: Mapping[str, Any]) -> Mapping[str, Any]:
        self.invocations.append(invocation)
        if not self._responses:
            return {}
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
