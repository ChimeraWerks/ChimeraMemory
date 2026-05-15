"""Obsidian vault import planning and file writing."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .memory_auto_capture import resolve_persona_root
from .memory_frontmatter import parse_frontmatter
from .memory_observability import record_memory_audit_event
from .sanitizer import sanitize_content, scan_for_injection

OBSIDIAN_IMPORT_SCHEMA_VERSION = "chimera-memory.obsidian-import.v1"
OBSIDIAN_IMPORT_TAGS = ["import", "obsidian"]
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_HEADING_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
_BLOCKING_FINDING_TYPES = {"credential"}
_SKIP_DIRS = {".git", ".obsidian", ".trash", "__pycache__", "node_modules"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(value: str | None) -> str:
    sanitized = sanitize_content(value or "") or ""
    return sanitized.replace("\r\n", "\n").replace("\r", "\n").strip()


def _yaml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=True)


def _slugify(value: str, fallback: str = "obsidian-note") -> str:
    text = _SLUG_RE.sub("-", value.lower()).strip("-")
    return (text or fallback)[:96].strip("-") or fallback


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_findings(content: str) -> tuple[list[dict], list[dict]]:
    findings = []
    blocking = []
    for finding in scan_for_injection(content):
        safe = {
            "type": finding.get("type", "unknown"),
            "match_count": finding.get("match_count", 1),
        }
        findings.append(safe)
        if safe["type"] in _BLOCKING_FINDING_TYPES:
            blocking.append(safe)
    return findings, blocking


def _json_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [part.strip() for part in re.split(r"[,;]", text) if part.strip()]
    return []


def _title_from_note(source_rel: str, frontmatter: dict, body: str) -> str:
    for key in ("title", "about", "name"):
        value = str(frontmatter.get(key) or "").strip()
        if value:
            return _clean_text(value)
    match = _HEADING_RE.search(body)
    if match:
        return _clean_text(match.group(1))
    return Path(source_rel).stem.replace("-", " ").replace("_", " ").strip() or "Obsidian note"


def _created_from_mtime(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return _utc_now()
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _created_from_zip(info: zipfile.ZipInfo) -> str:
    try:
        return datetime(*info.date_time, tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError):
        return _utc_now()


def _iter_vault_markdown(vault_path: Path) -> list[dict]:
    path = Path(vault_path)
    notes: list[dict] = []
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                if info.is_dir() or not name.lower().endswith(".md"):
                    continue
                parts = [part for part in name.split("/") if part]
                if any(part in _SKIP_DIRS or part.startswith(".") for part in parts):
                    continue
                content = archive.read(info).decode("utf-8", errors="replace")
                notes.append({"source_rel": name, "created": _created_from_zip(info), "content": content})
    else:
        if not path.is_dir():
            raise ValueError("Obsidian import path must be a vault directory or zip file")
        for note_path in sorted(path.rglob("*.md")):
            rel = note_path.relative_to(path).as_posix()
            if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.split("/")):
                continue
            content = note_path.read_text(encoding="utf-8", errors="replace")
            notes.append({"source_rel": rel, "created": _created_from_mtime(note_path), "content": content})
    return notes


def render_obsidian_import_markdown(
    *,
    title: str,
    source_rel: str,
    created: str,
    tags: list[str],
    body: str,
) -> str:
    frontmatter = {
        "type": "semantic",
        "importance": 5,
        "created": created,
        "status": "active",
        "about": title,
        "tags": sorted(set([*OBSIDIAN_IMPORT_TAGS, *tags])),
        "provenance_status": "imported",
        "confidence": 0.75,
        "lifecycle_status": "active",
        "review_status": "pending",
        "sensitivity_tier": "standard",
        "can_use_as_instruction": False,
        "can_use_as_evidence": True,
        "requires_user_confirmation": True,
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {_yaml_value(value)}")
    lines.extend(
        [
            "---",
            "",
            f"# {title}",
            "",
            "## Import Metadata",
            "- source: obsidian",
            f"- source_path: {source_rel}",
            f"- schema: {OBSIDIAN_IMPORT_SCHEMA_VERSION}",
            "",
            "## Source Content",
            "",
            _clean_text(body),
            "",
        ]
    )
    return "\n".join(lines)


def build_obsidian_import_plans(
    vault_path: Path,
    *,
    persona: str,
    limit: int = 200,
) -> dict:
    """Build governed markdown import plans from an Obsidian vault."""
    persona = persona.strip()
    if not persona:
        return {"ok": False, "error": "persona required"}
    try:
        notes = _iter_vault_markdown(Path(vault_path))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"ok": False, "error": f"failed to load Obsidian vault: {exc}"}

    plans = []
    for note in notes[: max(0, min(int(limit), 5000))]:
        source_rel = str(note["source_rel"]).replace("\\", "/")
        content = _clean_text(str(note.get("content") or ""))
        if not content:
            continue
        frontmatter, body = parse_frontmatter(content)
        body = _clean_text(body)
        if not body:
            continue
        title = _title_from_note(source_rel, frontmatter, body)
        tags = _json_list(frontmatter.get("tags"))
        rendered = render_obsidian_import_markdown(
            title=title,
            source_rel=source_rel,
            created=str(note.get("created") or _utc_now()),
            tags=tags,
            body=body,
        )
        findings, blocking_findings = _safe_findings(rendered)
        source_hash = _hash_text(f"{source_rel}\n{content}")
        slug = _slugify(Path(source_rel).with_suffix("").as_posix())
        relative_path = f"memory/imports/obsidian/{slug}-{source_hash[:10]}.md"
        plans.append(
            {
                "ok": True,
                "schema_version": OBSIDIAN_IMPORT_SCHEMA_VERSION,
                "source": "obsidian",
                "source_path": source_rel,
                "source_id": source_hash,
                "title": title,
                "created": str(note.get("created") or ""),
                "relative_path": relative_path,
                "tags": tags,
                "guard_findings": findings,
                "blocking_findings": blocking_findings,
                "body": rendered,
            }
        )
    return {
        "ok": True,
        "schema_version": OBSIDIAN_IMPORT_SCHEMA_VERSION,
        "source": "obsidian",
        "persona": persona,
        "vault_path": str(vault_path),
        "note_count": len(notes),
        "plan_count": len(plans),
        "plans": plans,
    }


def write_obsidian_import_file(personas_dir: Path, persona: str, plan: dict, *, force: bool = False) -> dict:
    """Write one planned Obsidian import memory under the persona folder."""
    if not plan.get("ok"):
        return plan
    if plan.get("blocking_findings"):
        return {
            "ok": False,
            "error": "Obsidian import content failed safety scan",
            "blocking_findings": plan["blocking_findings"],
            "source_path": plan.get("source_path"),
        }
    persona_root = resolve_persona_root(personas_dir, persona)
    if persona_root is None:
        return {"ok": False, "error": "persona root not found", "persona": persona}
    relative_path = Path(str(plan["relative_path"]))
    target = persona_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        return {
            "ok": True,
            "written": False,
            "skipped": True,
            "reason": "target exists",
            "path": str(target),
            "relative_path": str(relative_path).replace("\\", "/"),
            "source_path": plan.get("source_path"),
        }
    target.write_text(str(plan["body"]), encoding="utf-8", newline="\n")
    return {
        "ok": True,
        "written": True,
        "skipped": False,
        "path": str(target),
        "relative_path": str(relative_path).replace("\\", "/"),
        "source_path": plan.get("source_path"),
    }


def memory_import_obsidian_vault(
    conn: sqlite3.Connection,
    personas_dir: Path,
    *,
    vault_path: str,
    persona: str,
    index_file_func,
    pyramid_summary_builder,
    limit: int = 200,
    write: bool = False,
    force: bool = False,
    build_pyramid: bool = True,
    actor: str = "agent",
) -> dict:
    """Plan or write governed memories from an Obsidian vault."""
    plans = build_obsidian_import_plans(Path(vault_path), persona=persona, limit=limit)
    if not plans.get("ok"):
        return plans

    preview = [
        {
            "source_path": plan.get("source_path"),
            "title": plan.get("title"),
            "relative_path": plan.get("relative_path"),
            "guard_findings": plan.get("guard_findings", []),
            "blocking_findings": plan.get("blocking_findings", []),
        }
        for plan in plans.get("plans", [])
    ]
    audit_payload = {
        "schema_version": plans["schema_version"],
        "source": "obsidian",
        "vault_path": str(vault_path),
        "note_count": plans.get("note_count", 0),
        "plan_count": len(preview),
        "write": bool(write),
        "build_pyramid": bool(build_pyramid),
    }
    if not write:
        record_memory_audit_event(
            conn,
            "memory_import_obsidian_planned",
            persona=persona,
            target_kind="obsidian_import",
            target_id=str(vault_path),
            payload=audit_payload,
            actor=actor,
        )
        return {"ok": True, "written": False, "plans": preview, "summary": audit_payload}

    written = []
    skipped = []
    failed = []
    pyramid_built = 0
    for plan in plans.get("plans", []):
        result = write_obsidian_import_file(personas_dir, persona, plan, force=force)
        if not result.get("ok"):
            failed.append(result)
            continue
        if result.get("skipped"):
            skipped.append(result)
            continue
        full_path = Path(result["path"])
        relative_path = result["relative_path"]
        indexed = index_file_func(conn, persona, relative_path, full_path)
        row = conn.execute(
            "SELECT id FROM memory_files WHERE path = ?",
            (str(full_path).replace("\\", "/"),),
        ).fetchone()
        file_id = row[0] if row else None
        pyramid = None
        if build_pyramid and file_id is not None:
            pyramid = pyramid_summary_builder(
                conn,
                file_path=str(file_id),
                persona=persona,
                force=force,
                actor=actor,
            )
            if pyramid.get("ok") and pyramid.get("built"):
                pyramid_built += 1
        written.append(
            {
                **result,
                "file_id": file_id,
                "indexed": indexed,
                "pyramid_built": bool(pyramid and pyramid.get("built")),
            }
        )
        record_memory_audit_event(
            conn,
            "memory_import_obsidian_note",
            persona=persona,
            target_kind="memory_file",
            target_id=str(file_id or relative_path),
            payload={
                "source": "obsidian",
                "source_path": plan.get("source_path"),
                "relative_path": relative_path,
                "file_id": file_id,
                "indexed": indexed,
                "pyramid_built": bool(pyramid and pyramid.get("built")),
            },
            actor=actor,
            commit=False,
        )
    record_memory_audit_event(
        conn,
        "memory_import_obsidian_completed",
        persona=persona,
        target_kind="obsidian_import",
        target_id=str(vault_path),
        payload={
            **audit_payload,
            "written_count": len(written),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "pyramid_built_count": pyramid_built,
        },
        actor=actor,
        commit=False,
    )
    conn.commit()
    return {
        "ok": not failed,
        "written": True,
        "summary": {
            "written_count": len(written),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "pyramid_built_count": pyramid_built,
            "plan_count": len(preview),
        },
        "written_items": written,
        "skipped_items": skipped,
        "failed_items": failed,
    }
