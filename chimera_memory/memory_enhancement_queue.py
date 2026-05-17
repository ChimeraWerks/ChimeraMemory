"""SQLite job queue helpers for memory-enhancement sidecar work."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .memory_enhancement import (
    build_authored_memory_enrichment_request,
    build_memory_enhancement_request,
    normalize_authored_memory_writeback,
    normalize_memory_enhancement_response,
)
from .memory_entities import apply_enhancement_entities
from .memory_frontmatter import parse_frontmatter
from .memory_observability import _json_object, _json_text, record_memory_audit_event

ENHANCEMENT_JOB_STATUSES = {"pending", "running", "succeeded", "failed", "skipped"}


def _find_memory_file_for_enhancement(conn: sqlite3.Connection, file_path: str):
    path = file_path.replace("\\", "/").strip()
    return conn.execute(
        """
        SELECT id, path, persona, relative_path, content_fingerprint
        FROM memory_files
        WHERE path = ? OR relative_path = ? OR path LIKE ?
        ORDER BY CASE
            WHEN path = ? THEN 0
            WHEN relative_path = ? THEN 1
            ELSE 2
        END
        LIMIT 1
        """,
        (path, path, f"%{path}%", path, path),
    ).fetchone()


def _enhancement_job_to_dict(row: sqlite3.Row | tuple | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row[0],
        "job_id": row[1],
        "created_at": row[2],
        "updated_at": row[3],
        "status": row[4],
        "persona": row[5],
        "file_id": row[6],
        "path": row[7],
        "content_fingerprint": row[8],
        "requested_provider": row[9],
        "requested_model": row[10],
        "request_payload": _json_object(row[11]),
        "result_payload": _json_object(row[12]),
        "error": row[13],
        "attempt_count": row[14],
        "locked_at": row[15],
    }


def _select_enhancement_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT id, job_id, created_at, updated_at, status, persona, file_id,
               path, content_fingerprint, requested_provider, requested_model,
               request_payload, result_payload, error, attempt_count, locked_at
        FROM memory_enhancement_jobs
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    return _enhancement_job_to_dict(row)


def memory_enhancement_enqueue(
    conn: sqlite3.Connection,
    *,
    file_path: str,
    requested_provider: str = "",
    requested_model: str = "",
    force: bool = False,
) -> dict:
    """Queue a memory file for sidecar metadata enhancement."""
    memory_row = _find_memory_file_for_enhancement(conn, file_path)
    if memory_row is None:
        return {"ok": False, "error": "memory file not found", "file_path": file_path}

    existing = conn.execute(
        """
        SELECT job_id FROM memory_enhancement_jobs
        WHERE file_id = ? AND status IN ('pending', 'running')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (memory_row[0],),
    ).fetchone()
    if existing and not force:
        return {"ok": True, "enqueued": False, "job": _select_enhancement_job(conn, existing[0])}
    if existing and force:
        conn.execute(
            """
            UPDATE memory_enhancement_jobs
               SET status = 'skipped',
                   error = 'superseded by forced enqueue',
                   locked_at = NULL
             WHERE job_id = ?
            """,
            (existing[0],),
        )

    disk_path = Path(memory_row[1])
    try:
        raw_content = disk_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"ok": False, "error": "memory file not readable", "file_path": str(memory_row[1])}

    frontmatter, body = parse_frontmatter(raw_content)
    request_payload = build_memory_enhancement_request(
        content=body,
        persona=str(memory_row[2] or ""),
        source_path=str(memory_row[3] or memory_row[1]),
        existing_frontmatter=frontmatter,
    )
    job_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO memory_enhancement_jobs (
            job_id, status, persona, file_id, path, content_fingerprint,
            requested_provider, requested_model, request_payload
        ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            memory_row[2],
            memory_row[0],
            memory_row[1],
            memory_row[4],
            requested_provider or "",
            requested_model or "",
            _json_text(request_payload),
        ),
    )
    record_memory_audit_event(
        conn,
        "memory_enhancement_enqueued",
        persona=memory_row[2],
        target_kind="memory_file",
        target_id=str(memory_row[0]),
        payload={"job_id": job_id, "path": memory_row[1]},
        commit=False,
    )
    conn.commit()
    return {"ok": True, "enqueued": True, "job": _select_enhancement_job(conn, job_id)}


def _authored_payload_fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def memory_enhancement_enqueue_authored(
    conn: sqlite3.Connection,
    *,
    persona: str,
    memory_payload: dict,
    provenance: dict | None = None,
    source_ref: str = "",
    file_id: int | None = None,
    requested_provider: str = "",
    requested_model: str = "",
) -> dict:
    """Queue enrichment for a caller-authored structured memory payload."""
    if file_id:
        existing = conn.execute(
            """
            SELECT job_id FROM memory_enhancement_jobs
            WHERE file_id = ? AND status IN ('pending', 'running')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (file_id,),
        ).fetchone()
        if existing:
            return {"ok": True, "enqueued": False, "job": _select_enhancement_job(conn, existing[0])}
    try:
        request_payload = build_authored_memory_enrichment_request(
            memory_payload=memory_payload,
            persona=persona,
            source_ref=source_ref,
            provenance=provenance,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "source_ref": source_ref}

    job_id = str(uuid.uuid4())
    fingerprint = _authored_payload_fingerprint(
        {
            "memory_payload": request_payload.get("memory_payload") or {},
            "provenance": request_payload.get("provenance") or {},
        }
    )
    path = source_ref or str(request_payload["request_id"])
    conn.execute(
        """
        INSERT INTO memory_enhancement_jobs (
            job_id, status, persona, file_id, path, content_fingerprint,
            requested_provider, requested_model, request_payload
        ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            request_payload.get("persona"),
            file_id,
            path,
            fingerprint,
            requested_provider or "",
            requested_model or "",
            _json_text(request_payload),
        ),
    )
    record_memory_audit_event(
        conn,
        "memory_enhancement_authored_enqueued",
        persona=request_payload.get("persona"),
        target_kind="authored_memory_payload",
        target_id=job_id,
        payload={
            "job_id": job_id,
            "source_ref": source_ref,
            "schema_version": request_payload["schema_version"],
        },
        commit=False,
    )
    conn.commit()
    return {"ok": True, "enqueued": True, "job": _select_enhancement_job(conn, job_id)}


def memory_enhancement_claim_next(
    conn: sqlite3.Connection,
    *,
    persona: str | None = None,
) -> dict | None:
    """Claim the next pending sidecar enhancement job."""
    conditions = ["status = 'pending'"]
    params: list[object] = []
    if persona:
        conditions.append("persona = ?")
        params.append(persona)
    row = conn.execute(
        f"""
        SELECT job_id, persona FROM memory_enhancement_jobs
        WHERE {' AND '.join(conditions)}
        ORDER BY created_at ASC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        return None
    job_id = row[0]
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    conn.execute(
        """
        UPDATE memory_enhancement_jobs
           SET status = 'running',
               attempt_count = attempt_count + 1,
               locked_at = ?
         WHERE job_id = ? AND status = 'pending'
        """,
        (now, job_id),
    )
    record_memory_audit_event(
        conn,
        "memory_enhancement_started",
        persona=row[1],
        target_kind="enhancement_job",
        target_id=job_id,
        payload={},
        commit=False,
    )
    conn.commit()
    return _select_enhancement_job(conn, job_id)


def memory_enhancement_complete(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    status: str,
    response_payload: object | None = None,
    error: str = "",
) -> dict:
    """Finish a sidecar enhancement job without mutating memory files."""
    status = status.strip()
    if status not in {"succeeded", "failed", "skipped"}:
        raise ValueError("status must be succeeded, failed, or skipped")
    job = _select_enhancement_job(conn, job_id)
    if job is None:
        return {"ok": False, "error": "enhancement job not found", "job_id": job_id}

    if status == "succeeded":
        response_mapping = response_payload if isinstance(response_payload, dict) else {}
        request_payload = job.get("request_payload") if isinstance(job.get("request_payload"), dict) else {}
        if request_payload.get("task") == "enrich_authored_memory_payload":
            result_payload = normalize_authored_memory_writeback(
                request_payload,
                enrichment_payload=response_mapping,
            )
        else:
            result_payload = normalize_memory_enhancement_response(response_mapping)
        entity_result = apply_enhancement_entities(
            conn,
            file_id=job.get("file_id"),
            metadata=result_payload,
            source="enhancement",
        )
        event_type = "memory_enhancement_completed"
        error_text = ""
    else:
        result_payload = response_payload if isinstance(response_payload, dict) else {}
        entity_result = {"link_count": 0, "edge_count": 0}
        event_type = "memory_enhancement_failed" if status == "failed" else "memory_enhancement_skipped"
        error_text = error or ""

    conn.execute(
        """
        UPDATE memory_enhancement_jobs
           SET status = ?,
               result_payload = ?,
               error = ?,
               locked_at = NULL
         WHERE job_id = ?
        """,
        (status, _json_text(result_payload), error_text, job_id),
    )
    record_memory_audit_event(
        conn,
        event_type,
        persona=job.get("persona"),
        target_kind="enhancement_job",
        target_id=job_id,
        payload={"status": status, "file_id": job.get("file_id"), "entities": entity_result},
        commit=False,
    )
    conn.commit()
    return {"ok": True, "job": _select_enhancement_job(conn, job_id)}
