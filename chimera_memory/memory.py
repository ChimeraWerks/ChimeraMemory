"""Curated memory system: index, search, and manage persona memory files.

Ported from the original chimera-memory MCP server. Indexes markdown files
with YAML frontmatter, provides FTS5 + semantic search, gap detection,
and consolidation analysis.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────

MEMORY_DIRS = {"memory", "reading", "shared"}
INDEX_EXTENSIONS = {".md"}
SKIP_DIRS = {".git", ".obsidian", ".claude", "__pycache__", "node_modules", ".chimera"}

# Consolidation thresholds
IMPORTANCE_DECAY_RATE = 0.05
MIN_IMPORTANCE_ACTIVE = 3
MIN_IMPORTANCE_STALE = 1
CONSOLIDATION_AGE_DAYS = 7

PROVENANCE_STATUSES = {
    "observed", "inferred", "user_confirmed", "imported",
    "generated", "superseded", "disputed",
}
LIFECYCLE_STATUSES = {"active", "stale", "archived", "superseded", "disputed", "rejected"}
REVIEW_STATUSES = {
    "pending", "confirmed", "evidence_only", "restricted",
    "rejected", "stale", "merged",
}
SENSITIVITY_TIERS = {"standard", "restricted", "unknown"}
INSTRUCTION_GRADE_PROVENANCE = {"user_confirmed", "imported"}
REVIEW_ACTIONS = {
    "confirm",
    "evidence_only",
    "restrict_scope",
    "mark_stale",
    "reject",
    "dispute",
    "supersede",
}
ENHANCEMENT_JOB_STATUSES = {"pending", "running", "succeeded", "failed", "skipped"}

# ─── Schema ──────────────────────────────────────────────────────────

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    persona TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    indexed_at REAL NOT NULL,
    fm_type TEXT,
    fm_importance INTEGER,
    fm_created TEXT,
    fm_last_accessed TEXT,
    fm_access_count INTEGER DEFAULT 0,
    fm_status TEXT DEFAULT 'active',
    fm_about TEXT,
    fm_tags TEXT,
    fm_entity TEXT,
    fm_relationship_temperature REAL,
    fm_trust_level REAL,
    fm_trend TEXT,
    fm_failure_count INTEGER DEFAULT 0,
    idempotency_key TEXT,
    content_fingerprint TEXT,
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    fm_provenance_status TEXT DEFAULT 'imported',
    fm_confidence REAL,
    fm_lifecycle_status TEXT DEFAULT 'active',
    fm_review_status TEXT DEFAULT 'confirmed',
    fm_sensitivity_tier TEXT DEFAULT 'standard',
    fm_can_use_as_instruction INTEGER DEFAULT 1,
    fm_can_use_as_evidence INTEGER DEFAULT 1,
    fm_requires_user_confirmation INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mf_persona ON memory_files(persona);
CREATE INDEX IF NOT EXISTS idx_mf_type ON memory_files(fm_type);
CREATE INDEX IF NOT EXISTS idx_mf_importance ON memory_files(fm_importance);
CREATE INDEX IF NOT EXISTS idx_mf_status ON memory_files(fm_status);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    file_id INTEGER PRIMARY KEY REFERENCES memory_files(id) ON DELETE CASCADE,
    embedding BLOB NOT NULL,
    embedded_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    path,
    persona,
    relative_path,
    content,
    fm_type,
    fm_tags,
    fm_about,
    tokenize='porter unicode61'
);
"""

MEMORY_POST_MIGRATION_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_mf_idempotency_key
ON memory_files(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_mf_content_fingerprint
ON memory_files(content_fingerprint)
WHERE content_fingerprint IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_mf_active_persona_importance
ON memory_files(persona, fm_importance DESC)
WHERE fm_status = 'active';

CREATE INDEX IF NOT EXISTS idx_mf_active_type_importance
ON memory_files(fm_type, fm_importance DESC)
WHERE fm_status = 'active';

CREATE INDEX IF NOT EXISTS idx_mf_provenance_status
ON memory_files(fm_provenance_status);

CREATE INDEX IF NOT EXISTS idx_mf_review_status
ON memory_files(fm_review_status);

CREATE INDEX IF NOT EXISTS idx_mf_sensitivity_tier
ON memory_files(fm_sensitivity_tier);

CREATE INDEX IF NOT EXISTS idx_mf_instruction_use
ON memory_files(fm_can_use_as_instruction)
WHERE fm_can_use_as_instruction = 1;

CREATE TRIGGER IF NOT EXISTS memory_files_ai_updated_at
AFTER INSERT ON memory_files
WHEN NEW.updated_at IS NULL
BEGIN
    UPDATE memory_files
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS memory_files_au_updated_at
AFTER UPDATE OF
    path, persona, relative_path, content_hash, indexed_at,
    fm_type, fm_importance, fm_created, fm_last_accessed,
    fm_access_count, fm_status, fm_about, fm_tags, fm_entity,
    fm_relationship_temperature, fm_trust_level, fm_trend,
    fm_failure_count, idempotency_key, content_fingerprint,
    fm_provenance_status, fm_confidence, fm_lifecycle_status,
    fm_review_status, fm_sensitivity_tier, fm_can_use_as_instruction,
    fm_can_use_as_evidence, fm_requires_user_confirmation
ON memory_files
BEGIN
    UPDATE memory_files
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS memory_recall_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    schema_version TEXT NOT NULL DEFAULT 'chimera-memory.recall-trace.v1',
    tool_name TEXT NOT NULL,
    persona TEXT,
    query_text TEXT NOT NULL,
    requested_limit INTEGER NOT NULL,
    result_count INTEGER DEFAULT 0,
    returned_count INTEGER DEFAULT 0,
    runtime_name TEXT,
    runtime_version TEXT,
    task_id TEXT,
    flow_id TEXT,
    channel_kind TEXT,
    channel_id TEXT,
    request_payload TEXT DEFAULT '{}',
    response_policy TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memory_recall_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL REFERENCES memory_recall_traces(trace_id) ON DELETE CASCADE,
    file_id INTEGER REFERENCES memory_files(id) ON DELETE SET NULL,
    rank INTEGER NOT NULL,
    similarity REAL,
    ranking_score REAL,
    returned INTEGER NOT NULL DEFAULT 1,
    used INTEGER NOT NULL DEFAULT 0,
    ignored_reason TEXT,
    path TEXT,
    persona TEXT,
    relative_path TEXT,
    fm_type TEXT,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memory_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    event_type TEXT NOT NULL,
    actor TEXT DEFAULT 'system',
    persona TEXT,
    target_kind TEXT,
    target_id TEXT,
    trace_id TEXT,
    payload TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_memory_recall_traces_created_at
ON memory_recall_traces(created_at);

CREATE INDEX IF NOT EXISTS idx_memory_recall_traces_persona
ON memory_recall_traces(persona);

CREATE INDEX IF NOT EXISTS idx_memory_recall_traces_tool
ON memory_recall_traces(tool_name);

CREATE INDEX IF NOT EXISTS idx_memory_recall_items_trace_rank
ON memory_recall_items(trace_id, rank);

CREATE INDEX IF NOT EXISTS idx_memory_audit_events_created_at
ON memory_audit_events(created_at);

CREATE INDEX IF NOT EXISTS idx_memory_audit_events_type
ON memory_audit_events(event_type);

CREATE INDEX IF NOT EXISTS idx_memory_audit_events_persona
ON memory_audit_events(persona);

CREATE TABLE IF NOT EXISTS memory_review_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    action TEXT NOT NULL,
    reviewer TEXT DEFAULT 'user',
    persona TEXT,
    file_id INTEGER REFERENCES memory_files(id) ON DELETE SET NULL,
    path TEXT,
    before_metadata TEXT DEFAULT '{}',
    after_metadata TEXT DEFAULT '{}',
    notes TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_memory_review_actions_file
ON memory_review_actions(file_id);

CREATE INDEX IF NOT EXISTS idx_memory_review_actions_action
ON memory_review_actions(action);

CREATE INDEX IF NOT EXISTS idx_memory_review_actions_created_at
ON memory_review_actions(created_at);

CREATE TABLE IF NOT EXISTS memory_enhancement_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')),
    persona TEXT,
    file_id INTEGER REFERENCES memory_files(id) ON DELETE SET NULL,
    path TEXT,
    content_fingerprint TEXT,
    requested_provider TEXT DEFAULT '',
    requested_model TEXT DEFAULT '',
    request_payload TEXT DEFAULT '{}',
    result_payload TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    locked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_enhancement_jobs_status
ON memory_enhancement_jobs(status, created_at);

CREATE INDEX IF NOT EXISTS idx_memory_enhancement_jobs_persona_status
ON memory_enhancement_jobs(persona, status, created_at);

CREATE INDEX IF NOT EXISTS idx_memory_enhancement_jobs_file
ON memory_enhancement_jobs(file_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_enhancement_jobs_active_file
ON memory_enhancement_jobs(file_id)
WHERE status IN ('pending', 'running');

CREATE TRIGGER IF NOT EXISTS memory_enhancement_jobs_au_updated_at
AFTER UPDATE OF
    status, requested_provider, requested_model, request_payload,
    result_payload, error, attempt_count, locked_at
ON memory_enhancement_jobs
BEGIN
    UPDATE memory_enhancement_jobs
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;
"""


def init_memory_tables(conn: sqlite3.Connection):
    """Create memory tables if they don't exist."""
    _check_memory_schema_prereqs(conn)
    conn.executescript(MEMORY_SCHEMA)
    _migrate_memory_files_schema(conn)
    conn.executescript(MEMORY_POST_MIGRATION_SCHEMA)
    conn.commit()


def _check_memory_schema_prereqs(conn: sqlite3.Connection) -> None:
    if sqlite3.sqlite_version_info < (3, 9, 0):
        version = ".".join(str(part) for part in sqlite3.sqlite_version_info)
        raise RuntimeError(f"SQLite 3.9.0+ with FTS5 support is required, found {version}")
    try:
        conn.execute("DROP TABLE IF EXISTS temp.chimera_memory_fts5_check")
        conn.execute("CREATE VIRTUAL TABLE temp.chimera_memory_fts5_check USING fts5(content)")
        conn.execute("DROP TABLE IF EXISTS temp.chimera_memory_fts5_check")
    except sqlite3.Error as exc:
        raise RuntimeError("SQLite FTS5 support is required for ChimeraMemory") from exc


def _memory_file_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_files)").fetchall()}


def _ensure_memory_file_column(
    conn: sqlite3.Connection,
    columns: set[str],
    name: str,
    column_sql: str,
) -> None:
    if name in columns:
        return
    conn.execute(f"ALTER TABLE memory_files ADD COLUMN {column_sql}")
    columns.add(name)


def _migrate_memory_files_schema(conn: sqlite3.Connection) -> None:
    columns = _memory_file_columns(conn)
    _ensure_memory_file_column(conn, columns, "idempotency_key", "idempotency_key TEXT")
    _ensure_memory_file_column(conn, columns, "content_fingerprint", "content_fingerprint TEXT")
    _ensure_memory_file_column(conn, columns, "updated_at", "updated_at TEXT")
    _ensure_memory_file_column(conn, columns, "fm_provenance_status", "fm_provenance_status TEXT")
    _ensure_memory_file_column(conn, columns, "fm_confidence", "fm_confidence REAL")
    _ensure_memory_file_column(conn, columns, "fm_lifecycle_status", "fm_lifecycle_status TEXT")
    _ensure_memory_file_column(conn, columns, "fm_review_status", "fm_review_status TEXT")
    _ensure_memory_file_column(conn, columns, "fm_sensitivity_tier", "fm_sensitivity_tier TEXT")
    _ensure_memory_file_column(conn, columns, "fm_can_use_as_instruction", "fm_can_use_as_instruction INTEGER")
    _ensure_memory_file_column(conn, columns, "fm_can_use_as_evidence", "fm_can_use_as_evidence INTEGER")
    _ensure_memory_file_column(
        conn,
        columns,
        "fm_requires_user_confirmation",
        "fm_requires_user_confirmation INTEGER",
    )
    conn.execute(
        """
        UPDATE memory_files
           SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
         WHERE updated_at IS NULL
        """
    )
    conn.execute(
        """
        UPDATE memory_files
           SET fm_provenance_status = COALESCE(fm_provenance_status, 'imported'),
               fm_lifecycle_status = COALESCE(fm_lifecycle_status, COALESCE(fm_status, 'active')),
               fm_review_status = COALESCE(fm_review_status, 'confirmed'),
               fm_sensitivity_tier = COALESCE(fm_sensitivity_tier, 'standard'),
               fm_can_use_as_instruction = COALESCE(fm_can_use_as_instruction, 1),
               fm_can_use_as_evidence = COALESCE(fm_can_use_as_evidence, 1),
               fm_requires_user_confirmation = COALESCE(fm_requires_user_confirmation, 0)
        """
    )


_FINGERPRINT_WHITESPACE_RE = re.compile(r"\s+")


def normalized_content_fingerprint(text: str) -> str:
    """Return OB1-style normalized SHA256 for duplicate-content detection."""
    normalized = _FINGERPRINT_WHITESPACE_RE.sub(" ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _choice(value: object, allowed: set[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, parsed))


def _bool_int(value: object, default: bool) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if value is None:
        return 1 if default else 0
    if isinstance(value, (int, float)):
        return 1 if value else 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return 1
    if text in {"0", "false", "no", "n", "off"}:
        return 0
    return 1 if default else 0


def governance_from_frontmatter(fm: dict) -> dict:
    """Normalize OB1-inspired governance metadata from YAML frontmatter."""
    provenance = _choice(fm.get("provenance_status"), PROVENANCE_STATUSES, "imported")
    lifecycle = _choice(
        fm.get("lifecycle_status"),
        LIFECYCLE_STATUSES,
        _choice(fm.get("status"), LIFECYCLE_STATUSES, "active"),
    )
    review_default = "confirmed" if provenance in INSTRUCTION_GRADE_PROVENANCE else "pending"
    review = _choice(fm.get("review_status"), REVIEW_STATUSES, review_default)
    sensitivity = _choice(fm.get("sensitivity_tier"), SENSITIVITY_TIERS, "standard")

    instruction_default = provenance in INSTRUCTION_GRADE_PROVENANCE
    can_use_as_instruction = _bool_int(fm.get("can_use_as_instruction"), instruction_default)
    if provenance not in INSTRUCTION_GRADE_PROVENANCE:
        can_use_as_instruction = 0

    requires_default = provenance not in INSTRUCTION_GRADE_PROVENANCE
    return {
        "provenance_status": provenance,
        "confidence": _optional_float(fm.get("confidence")),
        "lifecycle_status": lifecycle,
        "review_status": review,
        "sensitivity_tier": sensitivity,
        "can_use_as_instruction": can_use_as_instruction,
        "can_use_as_evidence": _bool_int(fm.get("can_use_as_evidence"), True),
        "requires_user_confirmation": _bool_int(
            fm.get("requires_user_confirmation"),
            requires_default,
        ),
    }


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


def _memory_governance_snapshot(row: sqlite3.Row | tuple) -> dict:
    return {
        "provenance_status": row[4],
        "confidence": row[5],
        "lifecycle_status": row[6],
        "review_status": row[7],
        "sensitivity_tier": row[8],
        "can_use_as_instruction": bool(row[9]),
        "can_use_as_evidence": bool(row[10]),
        "requires_user_confirmation": bool(row[11]),
    }


def _find_memory_file_for_review(conn: sqlite3.Connection, file_path: str):
    path = file_path.replace("\\", "/").strip()
    return conn.execute(
        """
        SELECT id, path, persona, relative_path, fm_provenance_status,
               fm_confidence, fm_lifecycle_status, fm_review_status,
               fm_sensitivity_tier, fm_can_use_as_instruction,
               fm_can_use_as_evidence, fm_requires_user_confirmation
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


def _review_updates_for_action(action: str) -> dict[str, object]:
    if action == "confirm":
        return {
            "fm_provenance_status": "user_confirmed",
            "fm_review_status": "confirmed",
            "fm_can_use_as_instruction": 1,
            "fm_can_use_as_evidence": 1,
            "fm_requires_user_confirmation": 0,
        }
    if action == "evidence_only":
        return {
            "fm_review_status": "evidence_only",
            "fm_can_use_as_instruction": 0,
            "fm_can_use_as_evidence": 1,
            "fm_requires_user_confirmation": 0,
        }
    if action == "restrict_scope":
        return {
            "fm_review_status": "restricted",
            "fm_sensitivity_tier": "restricted",
            "fm_can_use_as_instruction": 0,
            "fm_requires_user_confirmation": 0,
        }
    if action == "mark_stale":
        return {
            "fm_status": "stale",
            "fm_lifecycle_status": "stale",
            "fm_review_status": "stale",
            "fm_can_use_as_instruction": 0,
            "fm_requires_user_confirmation": 0,
        }
    if action == "reject":
        return {
            "fm_lifecycle_status": "rejected",
            "fm_review_status": "rejected",
            "fm_can_use_as_instruction": 0,
            "fm_can_use_as_evidence": 0,
            "fm_requires_user_confirmation": 0,
        }
    if action == "dispute":
        return {
            "fm_provenance_status": "disputed",
            "fm_lifecycle_status": "disputed",
            "fm_review_status": "pending",
            "fm_can_use_as_instruction": 0,
            "fm_requires_user_confirmation": 1,
        }
    if action == "supersede":
        return {
            "fm_provenance_status": "superseded",
            "fm_lifecycle_status": "superseded",
            "fm_review_status": "stale",
            "fm_can_use_as_instruction": 0,
            "fm_requires_user_confirmation": 0,
        }
    raise ValueError(f"unsupported review action: {action}")


def memory_review_pending(
    conn: sqlite3.Connection,
    *,
    persona: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return memories that need human review before instructional use."""
    conditions = [
        """
        (
            fm_review_status = 'pending'
            OR fm_requires_user_confirmation = 1
        )
        """
    ]
    params: list[object] = []
    if persona:
        conditions.append("persona = ?")
        params.append(persona)
    where = " AND ".join(conditions)
    rows = conn.execute(
        f"""
        SELECT id, path, persona, relative_path, fm_type, fm_importance,
               fm_about, fm_provenance_status, fm_confidence,
               fm_lifecycle_status, fm_review_status, fm_sensitivity_tier,
               fm_can_use_as_instruction, fm_can_use_as_evidence,
               fm_requires_user_confirmation
        FROM memory_files
        WHERE {where}
        ORDER BY fm_importance DESC NULLS LAST, updated_at DESC
        LIMIT ?
        """,
        params + [max(0, min(limit, 200))],
    ).fetchall()
    return [
        {
            "id": row[0],
            "path": row[1],
            "persona": row[2],
            "relative_path": row[3],
            "type": row[4],
            "importance": row[5],
            "about": row[6],
            "provenance_status": row[7],
            "confidence": row[8],
            "lifecycle_status": row[9],
            "review_status": row[10],
            "sensitivity_tier": row[11],
            "can_use_as_instruction": bool(row[12]),
            "can_use_as_evidence": bool(row[13]),
            "requires_user_confirmation": bool(row[14]),
        }
        for row in rows
    ]


def memory_review_action(
    conn: sqlite3.Connection,
    *,
    file_path: str,
    action: str,
    reviewer: str = "user",
    notes: str = "",
) -> dict:
    """Apply a human review action to one memory file."""
    action = action.strip()
    if action not in REVIEW_ACTIONS:
        raise ValueError(f"unsupported review action: {action}")

    row = _find_memory_file_for_review(conn, file_path)
    if row is None:
        return {"ok": False, "error": "memory file not found", "file_path": file_path}

    before = _memory_governance_snapshot(row)
    updates = _review_updates_for_action(action)
    assignments = ", ".join(f"{column} = ?" for column in updates)
    params = list(updates.values()) + [row[0]]
    conn.execute(f"UPDATE memory_files SET {assignments} WHERE id = ?", params)

    refreshed = _find_memory_file_for_review(conn, str(row[1]))
    after = _memory_governance_snapshot(refreshed)
    action_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO memory_review_actions (
            action_id, action, reviewer, persona, file_id, path,
            before_metadata, after_metadata, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action_id,
            action,
            reviewer or "user",
            row[2],
            row[0],
            row[1],
            _json_text(before),
            _json_text(after),
            notes or "",
        ),
    )

    event_type = {
        "confirm": "memory_confirmed",
        "evidence_only": "memory_evidence_only",
        "restrict_scope": "memory_restricted",
        "mark_stale": "memory_marked_stale",
        "reject": "memory_rejected",
        "dispute": "memory_disputed",
        "supersede": "memory_superseded",
    }[action]
    record_memory_audit_event(
        conn,
        event_type,
        persona=row[2],
        target_kind="memory_file",
        target_id=str(row[0]),
        payload={"action_id": action_id, "path": row[1], "notes": notes or ""},
        actor=reviewer or "user",
        commit=False,
    )
    conn.commit()
    return {
        "ok": True,
        "action_id": action_id,
        "action": action,
        "file_id": row[0],
        "path": row[1],
        "persona": row[2],
        "before": before,
        "after": after,
    }


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
    from .memory_enhancement import build_memory_enhancement_request

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
    from .memory_enhancement import normalize_memory_enhancement_response

    status = status.strip()
    if status not in {"succeeded", "failed", "skipped"}:
        raise ValueError("status must be succeeded, failed, or skipped")
    job = _select_enhancement_job(conn, job_id)
    if job is None:
        return {"ok": False, "error": "enhancement job not found", "job_id": job_id}

    if status == "succeeded":
        result_payload = normalize_memory_enhancement_response(
            response_payload if isinstance(response_payload, dict) else {}
        )
        event_type = "memory_enhancement_completed"
        error_text = ""
    else:
        result_payload = response_payload if isinstance(response_payload, dict) else {}
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
        payload={"status": status, "file_id": job.get("file_id")},
        commit=False,
    )
    conn.commit()
    return {"ok": True, "job": _select_enhancement_job(conn, job_id)}


# ─── FTS Normalization ───────────────────────────────────────────────

def normalize_for_fts(text: str) -> str:
    """Expand text for better FTS5 matching.
    Splits CamelCase and file paths into separate tokens.
    """
    def expand_camel(match):
        word = match.group(0)
        parts = re.sub(r'(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', ' ', word)
        return f"{word} {parts}" if parts != word else word

    def expand_path(match):
        path = match.group(0)
        segments = re.split(r'[/\\]', path)
        segments = [s for s in segments if s and s not in ('', 'C:')]
        return f"{path} {' '.join(segments)}"

    result = re.sub(r'[A-Za-z]:[/\\][^\s,;)}\]]+', expand_path, text)
    result = re.sub(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b', expand_camel, result)
    return result


# ─── Frontmatter ─────────────────────────────────────────────────────

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    try:
        import yaml
        fm = yaml.safe_load(text[3:end].strip()) or {}
    except Exception:
        fm = {}
    return fm, text[end + 4:].strip()


# ─── File Discovery ─────────────────────────────────────────────────

def discover_files(personas_dir: Path) -> list[tuple[str, str, Path]]:
    """Discover indexable markdown files for the current persona only.

    When TRANSCRIPT_PERSONA env var is set, only files belonging to that persona
    (plus shared/) are indexed. This enforces per-persona privacy: each persona
    sees its own memory + shared content, never another persona's files.

    When TRANSCRIPT_PERSONA is unset, walks all personas (legacy / multi-persona
    aggregation use case). The MCP-server-per-persona deployment should always
    set the env var.

    Returns [(persona, relative_path, full_path)].
    """
    import os
    results = []
    if not personas_dir.exists():
        return results

    scope_persona = os.environ.get("TRANSCRIPT_PERSONA", "").strip()

    for persona_dir in personas_dir.iterdir():
        if not persona_dir.is_dir() or persona_dir.name.startswith("."):
            continue
        for sub in persona_dir.iterdir():
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            if scope_persona and sub.name != scope_persona:
                continue
            _walk_for_files(sub, sub.name, sub, results)

    shared_dir = personas_dir.parent / "shared"
    if shared_dir.exists():
        _walk_for_files(shared_dir, "shared", shared_dir, results)

    return results


def cleanup_other_personas(conn, scope_persona: str) -> dict:
    """Delete memory rows belonging to other personas.

    Used to enforce the privacy boundary on existing data when TRANSCRIPT_PERSONA
    scope changes. Removes from memory_files, memory_embeddings, memory_fts.
    The 'shared' persona is preserved.

    Returns {'memory_files': N, 'memory_embeddings': N, 'memory_fts': N} counts.
    """
    if not scope_persona:
        return {"error": "scope_persona required"}

    cur = conn.cursor()
    counts = {}

    # Find file IDs to delete (everything except scope_persona and shared)
    cur.execute(
        "SELECT id FROM memory_files WHERE persona NOT IN (?, 'shared')",
        (scope_persona,),
    )
    ids_to_delete = [row[0] for row in cur.fetchall()]

    if not ids_to_delete:
        return {"memory_files": 0, "memory_embeddings": 0, "memory_fts": 0}

    placeholders = ",".join("?" * len(ids_to_delete))

    cur.execute(
        f"DELETE FROM memory_embeddings WHERE file_id IN ({placeholders})",
        ids_to_delete,
    )
    counts["memory_embeddings"] = cur.rowcount

    cur.execute(
        f"DELETE FROM memory_fts WHERE rowid IN ({placeholders})",
        ids_to_delete,
    )
    counts["memory_fts"] = cur.rowcount

    cur.execute(
        f"DELETE FROM memory_files WHERE id IN ({placeholders})",
        ids_to_delete,
    )
    counts["memory_files"] = cur.rowcount

    conn.commit()
    return counts


def _walk_for_files(directory: Path, persona: str, base: Path, results: list):
    for item in directory.iterdir():
        if item.name in SKIP_DIRS:
            continue
        if item.is_dir():
            _walk_for_files(item, persona, base, results)
        elif item.is_file() and item.suffix in INDEX_EXTENSIONS:
            rel = str(item.relative_to(base)).replace("\\", "/")
            results.append((persona, rel, item))


# ─── Indexing ────────────────────────────────────────────────────────

def index_file(conn: sqlite3.Connection, persona: str, relative_path: str,
               full_path: Path, maintenance: bool = False) -> bool:
    """Index a single memory file. Returns True if new or updated.

    Args:
        maintenance: If True, don't bump access counters (anti-inflation).
    """
    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    content_fingerprint = normalized_content_fingerprint(content)
    path_str = str(full_path).replace("\\", "/")

    row = conn.execute(
        "SELECT id, content_hash FROM memory_files WHERE path = ?", (path_str,)
    ).fetchone()

    if row and row[1] == content_hash:
        return False

    fm, body = parse_frontmatter(content)
    tags_json = json.dumps(fm.get("tags", []))
    governance = governance_from_frontmatter(fm)
    now = time.time()

    if row:
        file_id = row[0]
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (file_id,))
        conn.execute("""
            UPDATE memory_files SET
                content_hash=?, indexed_at=?,
                fm_type=?, fm_importance=?, fm_created=?, fm_last_accessed=?,
                fm_access_count=?, fm_status=?, fm_about=?, fm_tags=?,
                fm_entity=?, fm_relationship_temperature=?, fm_trust_level=?,
                fm_trend=?, fm_failure_count=?, content_fingerprint=?,
                fm_provenance_status=?, fm_confidence=?, fm_lifecycle_status=?,
                fm_review_status=?, fm_sensitivity_tier=?,
                fm_can_use_as_instruction=?, fm_can_use_as_evidence=?,
                fm_requires_user_confirmation=?
            WHERE id=?
        """, (
            content_hash, now,
            fm.get("type"), fm.get("importance"), fm.get("created"),
            fm.get("last_accessed"), fm.get("access_count", 0),
            fm.get("status", "active"), fm.get("about"), tags_json,
            fm.get("entity"), fm.get("relationship_temperature"),
            fm.get("trust_level"), fm.get("trend"),
            fm.get("failure_count", 0), content_fingerprint,
            governance["provenance_status"], governance["confidence"],
            governance["lifecycle_status"], governance["review_status"],
            governance["sensitivity_tier"], governance["can_use_as_instruction"],
            governance["can_use_as_evidence"], governance["requires_user_confirmation"],
            file_id
        ))
    else:
        cursor = conn.execute("""
            INSERT INTO memory_files (
                path, persona, relative_path, content_hash, indexed_at,
                fm_type, fm_importance, fm_created, fm_last_accessed,
                fm_access_count, fm_status, fm_about, fm_tags,
                fm_entity, fm_relationship_temperature, fm_trust_level,
                fm_trend, fm_failure_count, content_fingerprint,
                fm_provenance_status, fm_confidence, fm_lifecycle_status,
                fm_review_status, fm_sensitivity_tier,
                fm_can_use_as_instruction, fm_can_use_as_evidence,
                fm_requires_user_confirmation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            path_str, persona, relative_path, content_hash, now,
            fm.get("type"), fm.get("importance"), fm.get("created"),
            fm.get("last_accessed"), fm.get("access_count", 0),
            fm.get("status", "active"), fm.get("about"), tags_json,
            fm.get("entity"), fm.get("relationship_temperature"),
            fm.get("trust_level"), fm.get("trend"),
            fm.get("failure_count", 0), content_fingerprint,
            governance["provenance_status"], governance["confidence"],
            governance["lifecycle_status"], governance["review_status"],
            governance["sensitivity_tier"], governance["can_use_as_instruction"],
            governance["can_use_as_evidence"], governance["requires_user_confirmation"],
        ))
        file_id = cursor.lastrowid

    fts_body = normalize_for_fts(body)
    conn.execute("""
        INSERT INTO memory_fts (rowid, path, persona, relative_path, content, fm_type, fm_tags, fm_about)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (file_id, path_str, persona, relative_path, fts_body, fm.get("type", ""), tags_json, fm.get("about", "")))

    return True


def full_reindex(conn: sqlite3.Connection, personas_dir: Path, embed: bool = True) -> int:
    """Full reindex of all persona memory files."""
    files = discover_files(personas_dir)
    updated = 0
    updated_ids = []

    for persona, rel, full_path in files:
        if index_file(conn, persona, rel, full_path, maintenance=True):
            updated += 1
            row = conn.execute("SELECT id FROM memory_files WHERE path = ?",
                               (str(full_path).replace("\\", "/"),)).fetchone()
            if row:
                updated_ids.append(row[0])
    conn.commit()

    # Clean up deleted files
    indexed_paths = {str(fp).replace("\\", "/") for _, _, fp in files}
    rows = conn.execute("SELECT id, path FROM memory_files").fetchall()
    for file_id, path in rows:
        if path not in indexed_paths:
            conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (file_id,))
            conn.execute("DELETE FROM memory_embeddings WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM memory_files WHERE id = ?", (file_id,))
    conn.commit()

    if embed and updated_ids:
        embed_memory_files(conn, updated_ids)
    if embed:
        missing = conn.execute("""
            SELECT f.id FROM memory_files f
            LEFT JOIN memory_embeddings e ON e.file_id = f.id
            WHERE e.file_id IS NULL
        """).fetchall()
        missing_ids = [r[0] for r in missing if r[0] not in updated_ids]
        if missing_ids:
            embed_memory_files(conn, missing_ids)

    return updated


def embed_memory_files(conn: sqlite3.Connection, file_ids: list[int]):
    """Generate and store embeddings for memory files using fastembed."""
    if not file_ids:
        return

    from .embeddings import embed_batch, pack_embedding

    placeholders = ",".join("?" * len(file_ids))
    rows = conn.execute(f"""
        SELECT id, path, persona, relative_path, fm_type, fm_about, fm_tags
        FROM memory_files WHERE id IN ({placeholders})
    """, file_ids).fetchall()

    texts = []
    ids = []
    for r in rows:
        path = Path(r[1])
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            _, body = parse_frontmatter(content)
        except OSError:
            body = ""

        text_parts = [f"persona:{r[2]}", f"file:{r[3]}"]
        if r[4]:
            text_parts.append(f"type:{r[4]}")
        if r[5]:
            text_parts.append(f"about:{r[5]}")
        if r[6]:
            tags = json.loads(r[6]) if r[6] else []
            if tags:
                text_parts.append(f"tags:{','.join(str(t) for t in tags)}")
        text_parts.append(body[:2000])
        texts.append(" ".join(text_parts))
        ids.append(r[0])

    if not texts:
        return

    log.info("Embedding %d memory files...", len(texts))
    now = time.time()

    for file_id, emb in zip(ids, embed_batch(texts)):
        conn.execute("""
            INSERT OR REPLACE INTO memory_embeddings (file_id, embedding, embedded_at)
            VALUES (?, ?, ?)
        """, (file_id, pack_embedding(emb), now))
    conn.commit()


# ─── Search Tools ────────────────────────────────────────────────────

def memory_search(conn: sqlite3.Connection, query: str, persona: Optional[str] = None, limit: int = 20) -> list[dict]:
    """Full-text search across memory files."""
    from .cognitive import reinforce_on_access

    if persona:
        rows = conn.execute("""
            SELECT f.id, f.path, f.persona, f.relative_path, f.fm_type, f.fm_importance,
                   f.fm_status, snippet(memory_fts, 3, '>>>', '<<<', '...', 40) as snippet
            FROM memory_fts
            JOIN memory_files f ON f.id = memory_fts.rowid
            WHERE memory_fts MATCH ? AND f.persona = ?
            ORDER BY rank LIMIT ?
        """, (query, persona, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT f.id, f.path, f.persona, f.relative_path, f.fm_type, f.fm_importance,
                   f.fm_status, snippet(memory_fts, 3, '>>>', '<<<', '...', 40) as snippet
            FROM memory_fts
            JOIN memory_files f ON f.id = memory_fts.rowid
            WHERE memory_fts MATCH ?
            ORDER BY rank LIMIT ?
        """, (query, limit)).fetchall()

    for r in rows:
        reinforce_on_access(conn, r[0])

    results = [
        {"id": r[0], "path": r[1], "persona": r[2], "relative_path": r[3], "type": r[4],
         "importance": r[5], "status": r[6], "snippet": r[7]}
        for r in rows
    ]
    record_memory_recall_trace(
        conn,
        tool_name="memory_search",
        query_text=query,
        persona=persona,
        requested_limit=limit,
        results=results,
        request_payload={"query": query, "persona": persona, "limit": limit},
        response_policy={"ranking": "fts5_rank", "returned": "all_results"},
    )
    return results


def memory_query(
    conn: sqlite3.Connection, persona: Optional[str] = None,
    fm_type: Optional[str] = None, min_importance: Optional[int] = None,
    max_importance: Optional[int] = None, status: Optional[str] = None,
    tag: Optional[str] = None, about: Optional[str] = None,
    sort_by: str = "importance", sort_order: str = "DESC", limit: int = 50,
) -> list[dict]:
    """Structured query against frontmatter fields."""
    conditions, params = [], []

    if persona:
        conditions.append("persona = ?"); params.append(persona)
    if fm_type:
        conditions.append("fm_type = ?"); params.append(fm_type)
    if min_importance is not None:
        conditions.append("fm_importance >= ?"); params.append(min_importance)
    if max_importance is not None:
        conditions.append("fm_importance <= ?"); params.append(max_importance)
    if status:
        conditions.append("fm_status = ?"); params.append(status)
    if tag:
        conditions.append("fm_tags LIKE ?"); params.append(f"%{tag}%")
    if about:
        conditions.append("fm_about LIKE ?"); params.append(f"%{about}%")

    where = " AND ".join(conditions) if conditions else "1=1"
    valid_sorts = {
        "importance": "fm_importance", "created": "fm_created",
        "last_accessed": "fm_last_accessed", "access_count": "fm_access_count",
        "trust_level": "fm_trust_level", "relationship_temperature": "fm_relationship_temperature",
    }
    sort_col = valid_sorts.get(sort_by, "fm_importance")
    order = "ASC" if sort_order.upper() == "ASC" else "DESC"

    rows = conn.execute(f"""
        SELECT path, persona, relative_path, fm_type, fm_importance,
               fm_created, fm_last_accessed, fm_access_count, fm_status,
               fm_about, fm_tags, fm_entity, fm_relationship_temperature,
               fm_trust_level, fm_trend, fm_failure_count,
               fm_provenance_status, fm_confidence, fm_lifecycle_status,
               fm_review_status, fm_sensitivity_tier,
               fm_can_use_as_instruction, fm_can_use_as_evidence,
               fm_requires_user_confirmation
        FROM memory_files WHERE {where}
        ORDER BY {sort_col} {order} NULLS LAST LIMIT ?
    """, params + [limit]).fetchall()

    return [
        {"path": r[0], "persona": r[1], "relative_path": r[2], "type": r[3],
         "importance": r[4], "created": r[5], "last_accessed": r[6],
         "access_count": r[7], "status": r[8], "about": r[9],
         "tags": json.loads(r[10]) if r[10] else [], "entity": r[11],
         "relationship_temperature": r[12], "trust_level": r[13],
         "trend": r[14], "failure_count": r[15],
         "provenance_status": r[16], "confidence": r[17],
         "lifecycle_status": r[18], "review_status": r[19],
         "sensitivity_tier": r[20], "can_use_as_instruction": bool(r[21]),
         "can_use_as_evidence": bool(r[22]),
         "requires_user_confirmation": bool(r[23])}
        for r in rows
    ]


def memory_recall(conn: sqlite3.Connection, concept: str, persona: Optional[str] = None, limit: int = 10) -> list[dict]:
    """Semantic recall: find memories most similar to a concept."""
    from .embeddings import embed_text, unpack_embedding, cosine_similarity

    query_emb = embed_text(concept)

    if persona:
        rows = conn.execute("""
            SELECT f.id, f.path, f.persona, f.relative_path, f.fm_type,
                   f.fm_importance, f.fm_status, f.fm_about, e.embedding
            FROM memory_files f
            JOIN memory_embeddings e ON e.file_id = f.id
            WHERE f.persona = ?
        """, (persona,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT f.id, f.path, f.persona, f.relative_path, f.fm_type,
                   f.fm_importance, f.fm_status, f.fm_about, e.embedding
            FROM memory_files f
            JOIN memory_embeddings e ON e.file_id = f.id
        """).fetchall()

    scored = []
    for r in rows:
        emb = unpack_embedding(r[8])
        sim = cosine_similarity(query_emb, emb)
        scored.append((sim, r))

    scored.sort(key=lambda x: -x[0])
    top = scored[:limit]

    from .cognitive import reinforce_on_access
    for _, r in top:
        reinforce_on_access(conn, r[0])

    results = [
        {"id": r[0], "path": r[1], "persona": r[2], "relative_path": r[3], "type": r[4],
         "importance": r[5], "status": r[6], "about": r[7], "similarity": round(sim, 4)}
        for sim, r in top
    ]
    record_memory_recall_trace(
        conn,
        tool_name="memory_recall",
        query_text=concept,
        persona=persona,
        requested_limit=limit,
        results=results,
        request_payload={"concept": concept, "persona": persona, "limit": limit},
        response_policy={"ranking": "embedding_cosine", "returned": "top_limit"},
    )
    return results


def memory_stats(conn: sqlite3.Connection, persona: Optional[str] = None) -> dict:
    """Get memory corpus statistics."""
    where = "WHERE persona = ?" if persona else ""
    params = [persona] if persona else []

    total = conn.execute(f"SELECT COUNT(*) FROM memory_files {where}", params).fetchone()[0]
    by_type = conn.execute(f"SELECT fm_type, COUNT(*) FROM memory_files {where} GROUP BY fm_type ORDER BY COUNT(*) DESC", params).fetchall()
    by_status = conn.execute(f"SELECT fm_status, COUNT(*) FROM memory_files {where} GROUP BY fm_status ORDER BY COUNT(*) DESC", params).fetchall()
    by_persona = conn.execute("SELECT persona, COUNT(*) FROM memory_files GROUP BY persona ORDER BY COUNT(*) DESC").fetchall()

    return {
        "total_files": total,
        "by_type": {r[0] or "unknown": r[1] for r in by_type},
        "by_status": {r[0] or "unknown": r[1] for r in by_status},
        "by_persona": {r[0]: r[1] for r in by_persona},
    }


def memory_gaps(conn: sqlite3.Connection, persona: Optional[str] = None) -> dict:
    """Detect knowledge gaps using graph analysis."""
    try:
        import networkx as nx
    except ImportError:
        return {"error": "networkx not installed. pip install networkx"}

    where = "WHERE persona = ?" if persona else ""
    params = [persona] if persona else []

    rows = conn.execute(f"""
        SELECT id, path, persona, relative_path, fm_type, fm_importance, fm_tags, fm_about
        FROM memory_files {where}
    """, params).fetchall()

    if not rows:
        return {"error": "No files found", "gaps": [], "clusters": [], "bridges": []}

    G = nx.Graph()
    file_concepts = {}

    for r in rows:
        file_id, rel_path = r[0], r[3]
        fm_type = r[4] or "unknown"
        tags = json.loads(r[6]) if r[6] else []
        about = str(r[7]) if r[7] else ""

        concepts = set()
        for tag in tags:
            concepts.add(str(tag).lower())
        if about:
            concepts.add(about.lower())
        concepts.add(fm_type.lower())
        stem = Path(rel_path).stem.replace("-", " ").replace("_", " ").lower()
        for word in stem.split():
            if len(word) > 3:
                concepts.add(word)

        file_concepts[file_id] = concepts
        G.add_node(file_id, path=rel_path, persona=r[2], type=fm_type,
                    importance=r[5], concepts=list(concepts))

    file_ids = list(file_concepts.keys())
    for i in range(len(file_ids)):
        for j in range(i + 1, len(file_ids)):
            shared = file_concepts[file_ids[i]] & file_concepts[file_ids[j]]
            if shared:
                G.add_edge(file_ids[i], file_ids[j], weight=len(shared))

    components = list(nx.connected_components(G))
    clusters = []
    for comp in sorted(components, key=len, reverse=True)[:5]:
        files_in = [{"path": G.nodes[n]["path"], "type": G.nodes[n]["type"]} for n in comp]
        all_concepts = set()
        for n in comp:
            all_concepts.update(G.nodes[n].get("concepts", []))
        clusters.append({"size": len(comp), "files": files_in[:10], "top_concepts": sorted(all_concepts)[:15]})

    isolated = [{"path": G.nodes[n]["path"], "type": G.nodes[n]["type"]} for n in nx.isolates(G)]

    return {
        "total_nodes": len(G.nodes), "total_edges": len(G.edges),
        "connected_components": len(components),
        "clusters": clusters, "isolated_files": isolated[:20],
    }


def consolidation_report(conn: sqlite3.Connection, persona: Optional[str] = None) -> dict:
    """Dry-run analysis of what consolidation would do. Does NOT modify anything."""
    where = "WHERE persona = ?" if persona else ""
    params = [persona] if persona else []
    now = datetime.now()

    rows = conn.execute(f"""
        SELECT id, path, persona, relative_path, fm_type, fm_importance,
               fm_created, fm_last_accessed, fm_access_count, fm_status
        FROM memory_files {where}
    """, params).fetchall()

    stale_candidates = []
    archive_candidates = []

    for r in rows:
        importance = r[5]
        if importance is None:
            continue

        last_accessed = r[7]
        days_since = 30  # default
        if last_accessed:
            try:
                days_since = (now - datetime.fromisoformat(str(last_accessed))).days
            except (ValueError, TypeError):
                pass
        elif r[6]:
            try:
                days_since = (now - datetime.fromisoformat(str(r[6]))).days
            except (ValueError, TypeError):
                pass

        decayed = max(0, importance - IMPORTANCE_DECAY_RATE * days_since)
        status = r[9] or "active"

        if status == "active" and decayed < MIN_IMPORTANCE_ACTIVE:
            stale_candidates.append({"path": r[3], "persona": r[2],
                                     "importance": importance, "decayed": round(decayed, 2), "type": r[4]})

        if status in ("active", "stale") and decayed < MIN_IMPORTANCE_STALE:
            archive_candidates.append({"path": r[3], "persona": r[2],
                                       "importance": importance, "decayed": round(decayed, 2), "type": r[4]})

    return {
        "total_analyzed": len(rows),
        "stale_candidates": stale_candidates,
        "archive_candidates": archive_candidates,
        "summary": {
            "would_mark_stale": len(stale_candidates),
            "would_archive": len(archive_candidates),
        }
    }


def mark_failure(conn: sqlite3.Connection, file_path: str) -> bool:
    """Increment failure_count for a memory file. Returns True if found."""
    path_str = file_path.replace("\\", "/")
    row = conn.execute("SELECT id, fm_failure_count FROM memory_files WHERE path LIKE ?",
                        (f"%{path_str}%",)).fetchone()
    if not row:
        return False
    new_count = (row[1] or 0) + 1
    conn.execute("UPDATE memory_files SET fm_failure_count = ? WHERE id = ?", (new_count, row[0]))
    conn.commit()
    return True


# ─── Live File Watcher ──────────────────────────────────────────────

def start_memory_watcher(db, personas_dir: Path):
    """Watch persona memory dirs for .md changes and incrementally reindex.

    Returns the watchdog Observer (caller can stop it) or None if watchdog
    is unavailable. The watcher opens its own SQLite connections per event,
    so it is safe to run alongside the cached memory_conn in the main thread.
    """
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        log.warning("watchdog not installed, memory file watcher disabled")
        return None

    personas_dir = Path(personas_dir)
    shared_dir = personas_dir.parent / "shared"

    try:
        personas_root = personas_dir.resolve()
    except OSError:
        personas_root = personas_dir
    try:
        shared_root = shared_dir.resolve()
    except OSError:
        shared_root = shared_dir

    import os as _os
    _scope_persona = _os.environ.get("TRANSCRIPT_PERSONA", "").strip()

    def _resolve(path: Path) -> tuple[str, str] | None:
        """Map an absolute path to (persona, relative_path) or None.

        Respects TRANSCRIPT_PERSONA env var: returns None for files belonging
        to other personas. Shared content is always allowed through.
        """
        if path.suffix not in INDEX_EXTENSIONS:
            return None
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if any(part in SKIP_DIRS for part in resolved.parts):
            return None

        # shared/** → persona="shared", rel relative to shared_root
        try:
            rel = resolved.relative_to(shared_root)
            return ("shared", str(rel).replace("\\", "/"))
        except ValueError:
            pass

        # personas/<persona>/<sub>/** → persona=<sub>, rel relative to <sub>
        try:
            rel_full = resolved.relative_to(personas_root)
        except ValueError:
            return None
        parts = rel_full.parts
        if len(parts) < 3:
            return None
        # Privacy boundary: skip files belonging to other personas
        if _scope_persona and parts[1] != _scope_persona:
            return None
        sub_root = personas_root / parts[0] / parts[1]
        try:
            rel = resolved.relative_to(sub_root)
        except ValueError:
            return None
        return (parts[1], str(rel).replace("\\", "/"))

    def _upsert(path: Path):
        resolved = _resolve(path)
        if not resolved:
            return
        persona, rel = resolved
        try:
            with db.connection() as conn:
                init_memory_tables(conn)
                changed = index_file(conn, persona, rel, path, maintenance=True)
                if changed:
                    row = conn.execute(
                        "SELECT id FROM memory_files WHERE path = ?",
                        (str(path).replace("\\", "/"),),
                    ).fetchone()
                    if row:
                        try:
                            embed_memory_files(conn, [row[0]])
                        except Exception:
                            log.exception("Embedding failed for %s", path)
                conn.commit()
        except Exception:
            log.exception("Error reindexing memory file %s", path)

    def _delete(path: Path):
        if path.suffix not in INDEX_EXTENSIONS:
            return
        path_str = str(path).replace("\\", "/")
        try:
            with db.connection() as conn:
                init_memory_tables(conn)
                row = conn.execute(
                    "SELECT id FROM memory_files WHERE path = ?", (path_str,)
                ).fetchone()
                if not row:
                    return
                file_id = row[0]
                conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (file_id,))
                conn.execute("DELETE FROM memory_embeddings WHERE file_id = ?", (file_id,))
                conn.execute("DELETE FROM memory_files WHERE id = ?", (file_id,))
                conn.commit()
        except Exception:
            log.exception("Error removing memory file from index %s", path)

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event):
            if not event.is_directory:
                _upsert(Path(event.src_path))

        def on_created(self, event):
            if not event.is_directory:
                _upsert(Path(event.src_path))

        def on_deleted(self, event):
            if not event.is_directory:
                _delete(Path(event.src_path))

        def on_moved(self, event):
            if event.is_directory:
                return
            _delete(Path(event.src_path))
            _upsert(Path(event.dest_path))

    observer = Observer()
    handler = _Handler()
    scheduled = []
    if personas_dir.exists():
        observer.schedule(handler, str(personas_dir), recursive=True)
        scheduled.append(str(personas_dir))
    if shared_dir.exists():
        observer.schedule(handler, str(shared_dir), recursive=True)
        scheduled.append(str(shared_dir))

    if not scheduled:
        log.warning("start_memory_watcher: no directories to watch")
        return None

    observer.daemon = True
    observer.start()
    log.info("Memory file watcher started on %s", ", ".join(scheduled))
    return observer
