"""Shadow-mode glue for memory-enhancement jobs.

Shadow mode keeps the existing memory file + local index path authoritative.
When enabled for specific personas, changed memory files are queued for
enhancement beside that path so operators can compare metadata before cutover.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from typing import Any

from .memory_enhancement_queue import memory_enhancement_enqueue
from .memory_observability import _json_object, record_memory_audit_event


TRUE_VALUES = {"1", "true", "yes", "y", "on"}


def _split_csv(value: object) -> set[str]:
    return {item.strip().lower() for item in str(value or "").split(",") if item.strip()}


def memory_enhancement_shadow_enabled(
    *, persona: str | None = None, env: Mapping[str, str] | None = None
) -> bool:
    """Return True only when shadow mode is explicitly enabled and allowed."""
    source = env or os.environ
    enabled = str(source.get("CHIMERA_MEMORY_ENHANCEMENT_SHADOW_MODE", "")).strip().lower()
    if enabled not in TRUE_VALUES:
        return False
    allowlist = _split_csv(source.get("CHIMERA_MEMORY_ENHANCEMENT_SHADOW_PERSONAS", ""))
    if not allowlist:
        return False
    if "*" in allowlist:
        return True
    return str(persona or "").strip().lower() in allowlist


def memory_enhancement_shadow_enqueue(
    conn: sqlite3.Connection,
    *,
    file_path: str,
    persona: str | None = None,
    reason: str,
    env: Mapping[str, str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Opt-in shadow enqueue for a changed memory file.

    Returns a safe receipt. It never writes enhancement output back to the
    memory file, and it requires a persona allowlist so startup reindexes cannot
    accidentally queue every persona.
    """
    source = env or os.environ
    if not memory_enhancement_shadow_enabled(persona=persona, env=source):
        return {"ok": True, "enabled": False, "enqueued": False, "reason": "shadow_disabled"}

    requested_provider = str(
        source.get("CHIMERA_MEMORY_ENHANCEMENT_SHADOW_PROVIDER")
        or source.get("CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER")
        or ""
    ).split(",", 1)[0].strip()
    requested_model = str(source.get("CHIMERA_MEMORY_ENHANCEMENT_SHADOW_MODEL") or "").strip()

    result = memory_enhancement_enqueue(
        conn,
        file_path=file_path,
        requested_provider=requested_provider,
        requested_model=requested_model,
        force=force,
    )
    job = result.get("job") if isinstance(result, dict) else {}
    payload = {
        "reason": reason,
        "file_path": file_path,
        "enabled": True,
        "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
        "enqueued": bool(result.get("enqueued")) if isinstance(result, dict) else False,
        "job_id": job.get("job_id") if isinstance(job, dict) else "",
        "status": job.get("status") if isinstance(job, dict) else "",
        "requested_provider_present": bool(requested_provider),
        "requested_model_present": bool(requested_model),
    }
    record_memory_audit_event(
        conn,
        "memory_enhancement_shadow_enqueue",
        persona=persona,
        target_kind="memory_file",
        target_id=file_path,
        payload=payload,
        actor="shadow",
    )
    return {
        "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
        "enabled": True,
        "enqueued": payload["enqueued"],
        "reason": reason,
        "job_id": payload["job_id"],
        "status": payload["status"],
        "error": result.get("error", "") if isinstance(result, dict) else "enqueue_failed",
    }


def _json_list(value: object) -> list[str]:
    raw = value
    if isinstance(value, str):
        raw = _json_object(value)
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _normalized_text(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _entity_counts(result_payload: Mapping[str, Any]) -> dict[str, int]:
    return {
        "people": len(_json_list(result_payload.get("people"))),
        "projects": len(_json_list(result_payload.get("projects"))),
        "tools": len(_json_list(result_payload.get("tools"))),
        "action_items": len(_json_list(result_payload.get("action_items"))),
        "dates": len(_json_list(result_payload.get("dates"))),
    }


def _comparison(row: sqlite3.Row | tuple) -> dict[str, Any]:
    result_payload = _json_object(row[8])
    if not isinstance(result_payload, dict):
        result_payload = {}
    current_tags = set(tag.lower() for tag in _json_list(row[12]))
    enhanced_topics = set(tag.lower() for tag in _json_list(result_payload.get("topics")))
    current_type = str(row[10] or "")
    enhanced_type = str(result_payload.get("memory_type") or "")
    current_sensitivity = str(row[14] or "standard")
    enhanced_sensitivity = str(result_payload.get("sensitivity_tier") or "")
    current_about = _normalized_text(row[13])
    enhanced_summary = _normalized_text(result_payload.get("summary"))

    return {
        "frontmatter_type": current_type,
        "enhanced_type": enhanced_type,
        "type_match": bool(enhanced_type) and enhanced_type == current_type,
        "frontmatter_sensitivity": current_sensitivity,
        "enhanced_sensitivity": enhanced_sensitivity,
        "sensitivity_escalated": current_sensitivity != "restricted" and enhanced_sensitivity == "restricted",
        "sensitivity_match": bool(enhanced_sensitivity) and enhanced_sensitivity == current_sensitivity,
        "frontmatter_tag_count": len(current_tags),
        "enhanced_topic_count": len(enhanced_topics),
        "topic_overlap_count": len(current_tags & enhanced_topics),
        "new_topic_count": len(enhanced_topics - current_tags),
        "summary_matches_about": bool(current_about and enhanced_summary and current_about == enhanced_summary),
        "summary_present": bool(enhanced_summary),
        "confidence": result_payload.get("confidence"),
        "review_status": result_payload.get("review_status"),
        "can_use_as_instruction": result_payload.get("can_use_as_instruction"),
        "entity_counts": _entity_counts(result_payload),
    }


def memory_enhancement_shadow_report(
    conn: sqlite3.Connection,
    *,
    persona: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return safe comparison data for recent enhancement jobs."""
    conditions: list[str] = []
    params: list[object] = []
    if persona:
        conditions.append("j.persona = ?")
        params.append(persona)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT j.job_id, j.created_at, j.status, j.persona, j.path,
               j.requested_provider, j.requested_model, j.error,
               j.result_payload, j.attempt_count,
               f.fm_type, f.relative_path, f.fm_tags, f.fm_about,
               f.fm_sensitivity_tier, f.fm_review_status,
               f.fm_can_use_as_instruction
          FROM memory_enhancement_jobs j
          LEFT JOIN memory_files f ON f.id = j.file_id
          {where}
         ORDER BY j.created_at DESC, j.id DESC
         LIMIT ?
        """,
        params + [max(0, min(limit, 200))],
    ).fetchall()

    jobs: list[dict[str, Any]] = []
    totals = {
        "jobs": 0,
        "pending": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "type_mismatches": 0,
        "sensitivity_escalations": 0,
    }
    for row in rows:
        status = str(row[2] or "")
        comparison = _comparison(row) if status == "succeeded" else {}
        totals["jobs"] += 1
        if status in totals:
            totals[status] += 1
        if comparison and not comparison.get("type_match"):
            totals["type_mismatches"] += 1
        if comparison and comparison.get("sensitivity_escalated"):
            totals["sensitivity_escalations"] += 1
        jobs.append(
            {
                "job_id": row[0],
                "created_at": row[1],
                "status": status,
                "persona": row[3],
                "relative_path": row[11] or row[4],
                "requested_provider_present": bool(row[5]),
                "requested_model_present": bool(row[6]),
                "error": row[7],
                "attempt_count": row[9],
                "comparison": comparison,
            }
        )

    return {"totals": totals, "jobs": jobs}
