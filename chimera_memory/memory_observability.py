"""Recall trace and audit-event helpers for ChimeraMemory."""

from __future__ import annotations

import json
import sqlite3
import uuid


def _json_text(value: object) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str)


def _json_object(text: str | None) -> object:
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def record_memory_audit_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    persona: str | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    trace_id: str | None = None,
    payload: object | None = None,
    actor: str = "system",
    commit: bool = True,
) -> str:
    """Record a memory audit event and return its event id."""
    event_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO memory_audit_events (
            event_id, event_type, actor, persona, target_kind,
            target_id, trace_id, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            event_type,
            actor,
            persona,
            target_kind or "",
            target_id or "",
            trace_id or "",
            _json_text(payload),
        ),
    )
    if commit:
        conn.commit()
    return event_id


def record_memory_recall_trace(
    conn: sqlite3.Connection,
    *,
    tool_name: str,
    query_text: str,
    persona: str | None,
    requested_limit: int,
    results: list[dict],
    request_payload: object | None = None,
    response_policy: object | None = None,
    runtime_name: str | None = None,
    runtime_version: str | None = None,
    task_id: str | None = None,
    flow_id: str | None = None,
    channel_kind: str | None = None,
    channel_id: str | None = None,
) -> str:
    """Record a recall request and its returned items."""
    trace_id = str(uuid.uuid4())
    returned_count = len(results)
    conn.execute(
        """
        INSERT INTO memory_recall_traces (
            trace_id, tool_name, persona, query_text, requested_limit,
            result_count, returned_count, runtime_name, runtime_version,
            task_id, flow_id, channel_kind, channel_id, request_payload,
            response_policy
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trace_id,
            tool_name,
            persona,
            query_text,
            requested_limit,
            len(results),
            returned_count,
            runtime_name or "",
            runtime_version or "",
            task_id or "",
            flow_id or "",
            channel_kind or "",
            channel_id or "",
            _json_text(request_payload),
            _json_text(response_policy),
        ),
    )

    for rank, result in enumerate(results, start=1):
        metadata = {
            "importance": result.get("importance"),
            "status": result.get("status"),
            "about": result.get("about"),
            "snippet_chars": len(str(result.get("snippet") or "")),
        }
        file_id = result.get("id")
        conn.execute(
            """
            INSERT INTO memory_recall_items (
                trace_id, file_id, rank, similarity, ranking_score, returned,
                used, ignored_reason, path, persona, relative_path, fm_type,
                metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace_id,
                file_id if isinstance(file_id, int) else None,
                rank,
                result.get("similarity"),
                result.get("ranking_score") or result.get("similarity"),
                1,
                0,
                "",
                result.get("path") or "",
                result.get("persona") or "",
                result.get("relative_path") or "",
                result.get("type") or "",
                _json_text(metadata),
            ),
        )
        record_memory_audit_event(
            conn,
            "memory_returned",
            persona=result.get("persona") or persona,
            target_kind="memory_file",
            target_id=str(file_id or result.get("path") or ""),
            trace_id=trace_id,
            payload={"rank": rank, "tool_name": tool_name},
            commit=False,
        )

    record_memory_audit_event(
        conn,
        "recall_requested",
        persona=persona,
        target_kind="memory_recall",
        target_id=trace_id,
        trace_id=trace_id,
        payload={
            "tool_name": tool_name,
            "requested_limit": requested_limit,
            "result_count": len(results),
            "returned_count": returned_count,
        },
        commit=False,
    )
    conn.commit()
    return trace_id


def memory_recall_trace_query(
    conn: sqlite3.Connection,
    *,
    persona: str | None = None,
    tool_name: str | None = None,
    limit: int = 20,
    include_items: bool = False,
) -> list[dict]:
    """Query recent recall traces."""
    conditions, params = [], []
    if persona:
        conditions.append("persona = ?")
        params.append(persona)
    if tool_name:
        conditions.append("tool_name = ?")
        params.append(tool_name)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT trace_id, created_at, tool_name, persona, query_text,
               requested_limit, result_count, returned_count, request_payload,
               response_policy
        FROM memory_recall_traces
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        params + [max(0, min(limit, 200))],
    ).fetchall()

    traces = []
    for row in rows:
        trace = {
            "trace_id": row[0],
            "created_at": row[1],
            "tool_name": row[2],
            "persona": row[3],
            "query_text": row[4],
            "requested_limit": row[5],
            "result_count": row[6],
            "returned_count": row[7],
            "request_payload": _json_object(row[8]),
            "response_policy": _json_object(row[9]),
        }
        if include_items:
            item_rows = conn.execute(
                """
                SELECT rank, similarity, ranking_score, returned, used,
                       ignored_reason, path, persona, relative_path, fm_type,
                       metadata
                FROM memory_recall_items
                WHERE trace_id = ?
                ORDER BY rank ASC
                """,
                (row[0],),
            ).fetchall()
            trace["items"] = [
                {
                    "rank": item[0],
                    "similarity": item[1],
                    "ranking_score": item[2],
                    "returned": bool(item[3]),
                    "used": bool(item[4]),
                    "ignored_reason": item[5],
                    "path": item[6],
                    "persona": item[7],
                    "relative_path": item[8],
                    "type": item[9],
                    "metadata": _json_object(item[10]),
                }
                for item in item_rows
            ]
        traces.append(trace)
    return traces


def memory_audit_query(
    conn: sqlite3.Connection,
    *,
    event_type: str | None = None,
    persona: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query recent memory audit events."""
    conditions, params = [], []
    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)
    if persona:
        conditions.append("persona = ?")
        params.append(persona)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT event_id, created_at, event_type, actor, persona,
               target_kind, target_id, trace_id, payload
        FROM memory_audit_events
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        params + [max(0, min(limit, 500))],
    ).fetchall()
    return [
        {
            "event_id": row[0],
            "created_at": row[1],
            "event_type": row[2],
            "actor": row[3],
            "persona": row[4],
            "target_kind": row[5],
            "target_id": row[6],
            "trace_id": row[7],
            "payload": _json_object(row[8]),
        }
        for row in rows
    ]
