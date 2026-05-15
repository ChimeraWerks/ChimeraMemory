"""Portable context profile exports from reviewed curated memory."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_frontmatter import parse_frontmatter
from .memory_observability import record_memory_audit_event
from .sanitizer import sanitize_content

PROFILE_EXPORT_SCHEMA_VERSION = "chimera-memory.profile-export.v1"
PROFILE_EXPORT_FILES = ("USER.md", "SOUL.md", "HEARTBEAT.md", "memory-profile.json")

_WHITESPACE_RE = re.compile(r"\s+")
_USER_TYPES = {"semantic", "procedural", "entity"}
_SOUL_TYPES = {"reflection", "social", "entity", "semantic"}
_HEARTBEAT_TYPES = {"episodic", "procedural", "semantic", "reflection"}
_ALLOWED_REVIEW_STATUS = {"confirmed", "evidence_only"}
_EXCLUDED_LIFECYCLE = {"rejected", "disputed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_text(value: object) -> str:
    return json.dumps(value if value is not None else {}, indent=2, sort_keys=True, default=str)


def _json_list(text: str | None) -> list:
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _clean_text(value: str | None) -> str:
    sanitized = sanitize_content(value or "") or ""
    return sanitized.replace("\r\n", "\n").replace("\r", "\n").strip()


def _collapse(value: str | None, limit: int = 520) -> str:
    text = _WHITESPACE_RE.sub(" ", _clean_text(value)).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _read_memory_excerpt(path: str, limit: int = 520) -> str:
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    _, body = parse_frontmatter(content)
    return _collapse(body, limit=limit)


def _row_to_record(row: sqlite3.Row | tuple[Any, ...]) -> dict:
    if not isinstance(row, sqlite3.Row):
        keys = [
            "id", "path", "persona", "relative_path", "content_hash", "updated_at",
            "type", "importance", "created", "last_accessed", "status", "about",
            "tags", "entity", "provenance_status", "confidence", "lifecycle_status",
            "review_status", "sensitivity_tier", "can_use_as_instruction",
            "can_use_as_evidence", "requires_user_confirmation",
        ]
        row = dict(zip(keys, row))
    else:
        row = dict(row)
    source_path = str(row["path"])
    record = {
        "id": row["id"],
        "persona": row["persona"],
        "relative_path": row["relative_path"],
        "content_hash": row["content_hash"],
        "updated_at": row["updated_at"],
        "type": row["type"] or "",
        "importance": row["importance"] if row["importance"] is not None else 0,
        "created": row["created"] or "",
        "last_accessed": row["last_accessed"] or "",
        "status": row["status"] or "",
        "about": row["about"] or "",
        "tags": _json_list(row["tags"]),
        "entity": row["entity"] or "",
        "provenance_status": row["provenance_status"] or "",
        "confidence": row["confidence"],
        "lifecycle_status": row["lifecycle_status"] or "",
        "review_status": row["review_status"] or "",
        "sensitivity_tier": row["sensitivity_tier"] or "",
        "can_use_as_instruction": bool(row["can_use_as_instruction"]),
        "can_use_as_evidence": bool(row["can_use_as_evidence"]),
        "requires_user_confirmation": bool(row["requires_user_confirmation"]),
    }
    record["excerpt"] = _read_memory_excerpt(source_path)
    return record


def _selected_memory_records(
    conn: sqlite3.Connection,
    *,
    persona: str | None = None,
    include_restricted: bool = False,
    include_archived: bool = False,
    limit: int = 120,
) -> list[dict]:
    conditions = ["COALESCE(fm_can_use_as_evidence, 1) = 1"]
    params: list[object] = []
    if persona:
        conditions.append("persona = ?")
        params.append(persona)
    review_statuses = sorted(_ALLOWED_REVIEW_STATUS | ({"restricted"} if include_restricted else set()))
    conditions.append(f"COALESCE(fm_review_status, 'confirmed') IN ({','.join('?' for _ in review_statuses)})")
    params.extend(review_statuses)
    conditions.append("COALESCE(fm_lifecycle_status, fm_status, 'active') NOT IN (?, ?)")
    params.extend(sorted(_EXCLUDED_LIFECYCLE))
    if not include_restricted:
        conditions.append("COALESCE(fm_sensitivity_tier, 'standard') <> 'restricted'")
    if not include_archived:
        conditions.append("COALESCE(fm_status, 'active') <> 'archived'")
        conditions.append("COALESCE(fm_lifecycle_status, 'active') <> 'archived'")
    where = " AND ".join(conditions)
    rows = conn.execute(
        f"""
        SELECT id, path, persona, relative_path, content_hash, updated_at,
               fm_type AS type, fm_importance AS importance, fm_created AS created,
               fm_last_accessed AS last_accessed, fm_status AS status,
               fm_about AS about, fm_tags AS tags, fm_entity AS entity,
               fm_provenance_status AS provenance_status, fm_confidence AS confidence,
               fm_lifecycle_status AS lifecycle_status, fm_review_status AS review_status,
               fm_sensitivity_tier AS sensitivity_tier,
               fm_can_use_as_instruction AS can_use_as_instruction,
               fm_can_use_as_evidence AS can_use_as_evidence,
               fm_requires_user_confirmation AS requires_user_confirmation
        FROM memory_files
        WHERE {where}
        ORDER BY COALESCE(fm_can_use_as_instruction, 0) DESC,
                 COALESCE(fm_importance, 0) DESC,
                 updated_at DESC,
                 relative_path ASC
        LIMIT ?
        """,
        params + [max(0, min(int(limit), 1000))],
    ).fetchall()
    return [_row_to_record(row) for row in rows]


def _record_title(record: dict) -> str:
    title = str(record.get("about") or "").strip()
    if title:
        return title
    return str(record.get("relative_path") or record.get("path") or "memory")


def _record_line(record: dict, *, include_policy: bool = False) -> str:
    title = _record_title(record)
    source = record.get("relative_path") or ""
    detail = record.get("excerpt") or title
    policy = ""
    if include_policy:
        policy = (
            f"; review={record.get('review_status')}; "
            f"instruction={str(record.get('can_use_as_instruction')).lower()}"
        )
    return (
        f"- {title}: {detail} "
        f"[source: {source}; type={record.get('type')}; importance={record.get('importance')}{policy}]"
    )


def _render_section(title: str, records: list[dict], *, include_policy: bool = False) -> list[str]:
    lines = [f"## {title}", ""]
    if not records:
        lines.extend(["No reviewed memories selected.", ""])
        return lines
    for record in records:
        lines.append(_record_line(record, include_policy=include_policy))
    lines.append("")
    return lines


def _artifact_header(title: str, *, generated_at: str, persona: str | None, records: list[dict]) -> list[str]:
    selected_persona = persona or "all indexed personas"
    return [
        f"# {title}",
        "",
        f"Generated: {generated_at}",
        f"Persona scope: {selected_persona}",
        f"Reviewed memory records: {len(records)}",
        "",
        "Generated from reviewed ChimeraMemory records. Treat as portable context, not as source of truth.",
        "",
    ]


def _render_user_md(records: list[dict], *, generated_at: str, persona: str | None) -> str:
    instruction = [
        row for row in records
        if row.get("can_use_as_instruction") and row.get("review_status") == "confirmed"
    ]
    evidence = [
        row for row in records
        if row.get("type") in _USER_TYPES and row not in instruction
    ]
    lines = _artifact_header("USER.md", generated_at=generated_at, persona=persona, records=records)
    lines.extend(_render_section("Instruction-Grade Context", instruction, include_policy=True))
    lines.extend(_render_section("Reviewed Evidence Context", evidence, include_policy=True))
    return "\n".join(lines).rstrip() + "\n"


def _render_soul_md(records: list[dict], *, generated_at: str, persona: str | None) -> str:
    soul = [row for row in records if row.get("type") in _SOUL_TYPES]
    reflections = [row for row in soul if row.get("type") == "reflection"]
    relational = [row for row in soul if row.get("type") in {"social", "entity"}]
    other = [row for row in soul if row not in reflections and row not in relational]
    lines = _artifact_header("SOUL.md", generated_at=generated_at, persona=persona, records=records)
    lines.extend(_render_section("Reflections", reflections, include_policy=True))
    lines.extend(_render_section("People And Relationship Context", relational, include_policy=True))
    lines.extend(_render_section("Other Reviewed Context", other, include_policy=True))
    return "\n".join(lines).rstrip() + "\n"


def _render_heartbeat_md(records: list[dict], *, generated_at: str, persona: str | None) -> str:
    active = [
        row for row in records
        if row.get("type") in _HEARTBEAT_TYPES and row.get("lifecycle_status") in {"", "active", "stale"}
    ]
    active.sort(key=lambda row: (int(row.get("importance") or 0), str(row.get("updated_at") or "")), reverse=True)
    lines = _artifact_header("HEARTBEAT.md", generated_at=generated_at, persona=persona, records=records)
    lines.extend(_render_section("Current High-Signal Context", active[:30], include_policy=True))
    lines.extend(
        [
            "## Export Notes",
            "",
            "- Pending-review, rejected, disputed, and restricted memories are excluded by default.",
            "- Re-run the export after confirming review-queue items to promote them into this profile.",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _build_artifacts(records: list[dict], *, generated_at: str, persona: str | None, filters: dict) -> dict:
    artifacts = {
        "USER.md": _render_user_md(records, generated_at=generated_at, persona=persona),
        "SOUL.md": _render_soul_md(records, generated_at=generated_at, persona=persona),
        "HEARTBEAT.md": _render_heartbeat_md(records, generated_at=generated_at, persona=persona),
    }
    profile = {
        "schema_version": PROFILE_EXPORT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "persona": persona or "",
        "filters": filters,
        "counts": {
            "selected": len(records),
            "instruction_grade": sum(1 for row in records if row.get("can_use_as_instruction")),
            "evidence_only": sum(1 for row in records if not row.get("can_use_as_instruction")),
        },
        "records": records,
        "artifact_names": list(artifacts),
    }
    artifacts["memory-profile.json"] = _json_text(profile) + "\n"
    return artifacts


def memory_profile_export(
    conn: sqlite3.Connection,
    *,
    output_dir: str | Path | None = None,
    persona: str | None = None,
    limit: int = 120,
    include_restricted: bool = False,
    include_archived: bool = False,
    write: bool = False,
    actor: str = "system",
) -> dict:
    """Plan or write portable USER/SOUL/HEARTBEAT context artifacts."""
    selected_persona = (persona or "").strip() or None
    filters = {
        "limit": max(0, min(int(limit), 1000)),
        "include_restricted": bool(include_restricted),
        "include_archived": bool(include_archived),
        "review_statuses": sorted(_ALLOWED_REVIEW_STATUS | ({"restricted"} if include_restricted else set())),
    }
    records = _selected_memory_records(
        conn,
        persona=selected_persona,
        include_restricted=include_restricted,
        include_archived=include_archived,
        limit=filters["limit"],
    )
    generated_at = _utc_now()
    artifacts = _build_artifacts(records, generated_at=generated_at, persona=selected_persona, filters=filters)
    payload = {
        "schema_version": PROFILE_EXPORT_SCHEMA_VERSION,
        "persona": selected_persona or "",
        "selected_count": len(records),
        "write": bool(write),
        "output_dir": str(output_dir or ""),
        "artifact_names": list(PROFILE_EXPORT_FILES),
        "filters": filters,
    }
    if not write:
        record_memory_audit_event(
            conn,
            "memory_profile_export_planned",
            persona=selected_persona,
            target_kind="profile_export",
            target_id=str(output_dir or ""),
            payload=payload,
            actor=actor,
        )
        return {
            "ok": True,
            "written": False,
            "summary": payload,
            "records": [
                {
                    "id": row["id"],
                    "persona": row["persona"],
                    "relative_path": row["relative_path"],
                    "type": row["type"],
                    "review_status": row["review_status"],
                    "can_use_as_instruction": row["can_use_as_instruction"],
                }
                for row in records
            ],
            "artifacts": artifacts,
        }

    if not output_dir:
        return {"ok": False, "error": "output_dir required when write=true"}
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    written_files = []
    for name in PROFILE_EXPORT_FILES:
        target = target_dir / name
        target.write_text(str(artifacts[name]), encoding="utf-8", newline="\n")
        written_files.append(str(target))
    record_memory_audit_event(
        conn,
        "memory_profile_export_written",
        persona=selected_persona,
        target_kind="profile_export",
        target_id=str(target_dir),
        payload={**payload, "written_files": written_files},
        actor=actor,
    )
    return {
        "ok": True,
        "written": True,
        "output_dir": str(target_dir),
        "written_files": written_files,
        "summary": {**payload, "written_count": len(written_files)},
    }
