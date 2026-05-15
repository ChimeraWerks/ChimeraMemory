"""Typed reasoning relations between curated memory files."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from .memory_observability import record_memory_audit_event

MEMORY_FILE_EDGE_RELATION_TYPES = {
    "supports",
    "contradicts",
    "evolved_into",
    "supersedes",
    "depends_on",
    "related_to",
}

_WHITESPACE_RE = re.compile(r"\s+")
_STALE_FILE_STATUSES = {"stale", "archived"}
_STALE_LIFECYCLE_STATUSES = {"stale", "superseded", "disputed", "rejected", "archived"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_text(value: object) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str)


def _json_object(text: str | None) -> object:
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _normalize_relation_type(relation_type: str | None) -> str:
    clean = _WHITESPACE_RE.sub("_", str(relation_type or "related_to").strip().lower())
    return clean


def _confidence(value: float | int | None) -> float:
    if value is None:
        return 1.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, parsed))


def _decay_weight(value: float | int | None) -> float:
    if value is None:
        return 1.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, parsed)


def _find_memory_file(conn: sqlite3.Connection, file_path: str):
    path = str(file_path or "").replace("\\", "/").strip()
    if not path:
        return None
    if path.isdigit():
        by_id = conn.execute(
            """
            SELECT id, path, persona, relative_path, fm_type, fm_about
            FROM memory_files
            WHERE id = ?
            """,
            (int(path),),
        ).fetchone()
        if by_id is not None:
            return by_id
    return conn.execute(
        """
        SELECT id, path, persona, relative_path, fm_type, fm_about
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


def _file_to_dict(row) -> dict | None:
    if row is None:
        return None
    return {
        "id": row[0],
        "path": row[1],
        "persona": row[2],
        "relative_path": row[3],
        "type": row[4],
        "about": row[5],
    }


def _edge_to_dict(row) -> dict:
    return {
        "id": row[0],
        "edge_id": row[1],
        "relation_type": row[2],
        "confidence": row[3],
        "support_count": row[4],
        "valid_from": row[5],
        "valid_until": row[6],
        "decay_weight": row[7],
        "classifier_version": row[8],
        "evidence": row[9],
        "metadata": _json_object(row[10]),
        "created_at": row[11],
        "updated_at": row[12],
        "source": {
            "id": row[13],
            "path": row[14],
            "persona": row[15],
            "relative_path": row[16],
            "type": row[17],
            "about": row[18],
        },
        "target": {
            "id": row[19],
            "path": row[20],
            "persona": row[21],
            "relative_path": row[22],
            "type": row[23],
            "about": row[24],
        },
    }


def memory_file_edge_upsert(
    conn: sqlite3.Connection,
    *,
    source_file_path: str,
    target_file_path: str,
    relation_type: str = "related_to",
    confidence: float | int | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    decay_weight: float | int | None = None,
    classifier_version: str = "",
    evidence: str = "",
    metadata: object | None = None,
    actor: str = "system",
) -> dict:
    """Create or reinforce a typed reasoning relation between two memory files."""
    source = _find_memory_file(conn, source_file_path)
    if source is None:
        return {"ok": False, "error": "source memory file not found", "source_file_path": source_file_path}
    target = _find_memory_file(conn, target_file_path)
    if target is None:
        return {"ok": False, "error": "target memory file not found", "target_file_path": target_file_path}
    if int(source[0]) == int(target[0]):
        return {"ok": False, "error": "source and target memory files must differ"}

    clean_relation = _normalize_relation_type(relation_type)
    if clean_relation not in MEMORY_FILE_EDGE_RELATION_TYPES:
        return {
            "ok": False,
            "error": "unsupported relation type",
            "relation_type": relation_type,
            "allowed": sorted(MEMORY_FILE_EDGE_RELATION_TYPES),
        }
    edge_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO memory_file_edges (
            edge_id, source_file_id, target_file_id, relation_type,
            confidence, support_count, valid_from, valid_until,
            decay_weight, classifier_version, evidence, metadata
        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_file_id, target_file_id, relation_type) DO UPDATE SET
            confidence = MAX(COALESCE(confidence, 0), excluded.confidence),
            support_count = support_count + 1,
            valid_from = COALESCE(valid_from, excluded.valid_from),
            valid_until = CASE
                WHEN valid_until IS NULL OR valid_until = '' THEN valid_until
                WHEN excluded.valid_until IS NULL OR excluded.valid_until = '' THEN NULL
                WHEN excluded.valid_until > valid_until THEN excluded.valid_until
                ELSE valid_until
            END,
            decay_weight = excluded.decay_weight,
            classifier_version = excluded.classifier_version,
            evidence = excluded.evidence,
            metadata = excluded.metadata
        """,
        (
            edge_id,
            int(source[0]),
            int(target[0]),
            clean_relation,
            _confidence(confidence),
            valid_from or None,
            valid_until or None,
            _decay_weight(decay_weight),
            classifier_version or "",
            str(evidence or ""),
            _json_text(metadata),
        ),
    )
    edge = memory_file_edge_query(
        conn,
        source_file_path=str(source[0]),
        target_file_path=str(target[0]),
        relation_type=clean_relation,
        current_only=False,
        limit=1,
    )[0]
    record_memory_audit_event(
        conn,
        "memory_file_edge_upserted",
        persona=source[2],
        target_kind="memory_file_edge",
        target_id=edge["edge_id"],
        payload={
            "source_file_id": source[0],
            "target_file_id": target[0],
            "relation_type": clean_relation,
            "support_count": edge["support_count"],
        },
        actor=actor,
        commit=False,
    )
    conn.commit()
    return {"ok": True, "edge": edge}


def memory_file_edge_query(
    conn: sqlite3.Connection,
    *,
    source_file_path: str | None = None,
    target_file_path: str | None = None,
    file_path: str | None = None,
    relation_type: str | None = None,
    persona: str | None = None,
    current_only: bool = True,
    current_at: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query typed reasoning relations between memory files."""
    conditions: list[str] = []
    params: list[object] = []
    if source_file_path:
        source = _find_memory_file(conn, source_file_path)
        if source is None:
            return []
        conditions.append("edge.source_file_id = ?")
        params.append(int(source[0]))
    if target_file_path:
        target = _find_memory_file(conn, target_file_path)
        if target is None:
            return []
        conditions.append("edge.target_file_id = ?")
        params.append(int(target[0]))
    if file_path:
        memory_file = _find_memory_file(conn, file_path)
        if memory_file is None:
            return []
        conditions.append("(edge.source_file_id = ? OR edge.target_file_id = ?)")
        params.extend([int(memory_file[0]), int(memory_file[0])])
    if relation_type:
        normalized_relation = _normalize_relation_type(relation_type)
        if normalized_relation not in MEMORY_FILE_EDGE_RELATION_TYPES:
            return []
        conditions.append("edge.relation_type = ?")
        params.append(normalized_relation)
    if persona:
        conditions.append("(source.persona = ? OR target.persona = ?)")
        params.extend([persona, persona])
    if current_only:
        as_of = current_at or _utc_now()
        conditions.append("(edge.valid_from IS NULL OR edge.valid_from = '' OR edge.valid_from <= ?)")
        params.append(as_of)
        conditions.append("(edge.valid_until IS NULL OR edge.valid_until = '' OR edge.valid_until > ?)")
        params.append(as_of)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT edge.id, edge.edge_id, edge.relation_type, edge.confidence,
               edge.support_count, edge.valid_from, edge.valid_until,
               edge.decay_weight, edge.classifier_version, edge.evidence,
               edge.metadata, edge.created_at, edge.updated_at,
               source.id, source.path, source.persona, source.relative_path,
               source.fm_type, source.fm_about,
               target.id, target.path, target.persona, target.relative_path,
               target.fm_type, target.fm_about
        FROM memory_file_edges edge
        JOIN memory_files source ON source.id = edge.source_file_id
        JOIN memory_files target ON target.id = edge.target_file_id
        {where}
        ORDER BY edge.support_count DESC, edge.confidence DESC, edge.created_at DESC
        LIMIT ?
        """,
        params + [max(0, min(int(limit), 500))],
    ).fetchall()
    return [_edge_to_dict(row) for row in rows]


def memory_file_edge_temporal_sweep(
    conn: sqlite3.Connection,
    *,
    persona: str | None = None,
    now: str | None = None,
    dry_run: bool = True,
    expire_stale_files: bool = True,
    expire_zero_decay: bool = True,
    actor: str = "system",
) -> dict:
    """Expire current memory-file edges whose temporal or lifecycle inputs are stale."""
    now = now or _utc_now()
    conditions = [
        "(edge.valid_from IS NULL OR edge.valid_from = '' OR edge.valid_from <= ?)",
        "(edge.valid_until IS NULL OR edge.valid_until = '' OR edge.valid_until > ?)",
    ]
    params: list[object] = [now, now]
    stale_reasons: list[str] = []
    if expire_stale_files:
        status_placeholders = ",".join("?" * len(_STALE_FILE_STATUSES))
        lifecycle_placeholders = ",".join("?" * len(_STALE_LIFECYCLE_STATUSES))
        stale_reasons.append(
            f"source.fm_status IN ({status_placeholders}) "
            f"OR target.fm_status IN ({status_placeholders}) "
            f"OR source.fm_lifecycle_status IN ({lifecycle_placeholders}) "
            f"OR target.fm_lifecycle_status IN ({lifecycle_placeholders})"
        )
        params.extend(sorted(_STALE_FILE_STATUSES))
        params.extend(sorted(_STALE_FILE_STATUSES))
        params.extend(sorted(_STALE_LIFECYCLE_STATUSES))
        params.extend(sorted(_STALE_LIFECYCLE_STATUSES))
    if expire_zero_decay:
        stale_reasons.append("edge.decay_weight <= 0")
    if not stale_reasons:
        return {"ok": False, "error": "no sweep criteria enabled"}
    conditions.append("(" + " OR ".join(stale_reasons) + ")")
    if persona:
        conditions.append("(source.persona = ? OR target.persona = ?)")
        params.extend([persona, persona])

    rows = conn.execute(
        f"""
        SELECT edge.id, edge.edge_id, edge.relation_type, edge.confidence,
               edge.support_count, edge.valid_from, edge.valid_until,
               edge.decay_weight, edge.classifier_version, edge.evidence,
               edge.metadata, edge.created_at, edge.updated_at,
               source.id, source.path, source.persona, source.relative_path,
               source.fm_type, source.fm_about,
               target.id, target.path, target.persona, target.relative_path,
               target.fm_type, target.fm_about
        FROM memory_file_edges edge
        JOIN memory_files source ON source.id = edge.source_file_id
        JOIN memory_files target ON target.id = edge.target_file_id
        WHERE {' AND '.join(conditions)}
        ORDER BY edge.created_at ASC
        """,
        params,
    ).fetchall()
    candidates = [_edge_to_dict(row) for row in rows]
    if not dry_run and candidates:
        edge_ids = [edge["id"] for edge in candidates]
        placeholders = ",".join("?" * len(edge_ids))
        conn.execute(
            f"""
            UPDATE memory_file_edges
               SET valid_until = ?
             WHERE id IN ({placeholders})
            """,
            [now, *edge_ids],
        )
    record_memory_audit_event(
        conn,
        "memory_file_edges_temporal_sweep",
        persona=persona,
        target_kind="memory_file_edges",
        target_id=persona or "all",
        payload={
            "dry_run": dry_run,
            "expired_count": 0 if dry_run else len(candidates),
            "candidate_count": len(candidates),
            "now": now,
            "expire_stale_files": expire_stale_files,
            "expire_zero_decay": expire_zero_decay,
        },
        actor=actor,
        commit=False,
    )
    conn.commit()
    return {
        "ok": True,
        "dry_run": dry_run,
        "candidate_count": len(candidates),
        "expired_count": 0 if dry_run else len(candidates),
        "now": now,
        "candidates": candidates,
    }
