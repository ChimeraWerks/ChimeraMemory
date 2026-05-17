"""Human review queue helpers for governed memory metadata."""

from __future__ import annotations

import sqlite3
import uuid

from .memory_observability import _json_text, record_memory_audit_event

REVIEW_ACTIONS = {
    "confirm",
    "edit",
    "evidence_only",
    "restrict_scope",
    "mark_stale",
    "merge",
    "reject",
    "dispute",
    "supersede",
}


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
    if action == "edit":
        return {
            "fm_review_status": "pending",
            "fm_can_use_as_instruction": 0,
            "fm_can_use_as_evidence": 1,
            "fm_requires_user_confirmation": 1,
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
    if action == "merge":
        return {
            "fm_lifecycle_status": "superseded",
            "fm_review_status": "merged",
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
        "edit": "memory_review_edit_requested",
        "evidence_only": "memory_evidence_only",
        "restrict_scope": "memory_restricted",
        "mark_stale": "memory_marked_stale",
        "merge": "memory_merged",
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
