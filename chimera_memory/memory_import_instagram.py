"""Instagram export import planning and file writing."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .memory_auto_capture import resolve_persona_root
from .memory_observability import record_memory_audit_event
from .sanitizer import sanitize_content, scan_for_injection

INSTAGRAM_IMPORT_SCHEMA_VERSION = "chimera-memory.instagram-import.v1"
INSTAGRAM_IMPORT_TAGS = ["import", "instagram", "social"]
INSTAGRAM_TEXT_CHAR_LIMIT = 30000

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_BLOCKING_FINDING_TYPES = {"credential"}
_SUPPORTED_SUFFIXES = {".json", ".txt"}
_SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(value: str | None) -> str:
    sanitized = sanitize_content(value or "") or ""
    return sanitized.replace("\r\n", "\n").replace("\r", "\n").strip()


def _yaml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=True)


def _slugify(value: str, fallback: str = "instagram-item") -> str:
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


def _timestamp_to_created(value: object, fallback: str) -> str:
    if value in (None, ""):
        return fallback or _utc_now()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number > 10_000_000_000:
        number = number / 1000
    return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _string_values(item: object, keys: tuple[str, ...]) -> list[str]:
    if not isinstance(item, dict):
        return []
    values = []
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value)
    return values


def _message_thread_document(source_rel: str, value: dict, created: str) -> dict | None:
    messages = value.get("messages")
    if not isinstance(messages, list):
        return None
    participants = []
    for participant in value.get("participants") or []:
        if isinstance(participant, dict) and participant.get("name"):
            participants.append(_clean_text(str(participant["name"])))
    lines = []
    first_created = created
    for message in messages:
        if not isinstance(message, dict):
            continue
        sender = _clean_text(str(message.get("sender_name") or "participant"))
        content = "\n".join(
            _string_values(
                message,
                ("content", "share", "story_share", "photos", "videos", "audio_files", "gifs", "text"),
            )
        )
        content = _clean_text(content)
        if not content:
            continue
        timestamp = _timestamp_to_created(message.get("timestamp_ms") or message.get("timestamp"), created)
        if first_created == created:
            first_created = timestamp
        lines.extend([f"### {sender} ({timestamp})", "", content, ""])
    body = _clean_text("\n".join(lines))
    if not body:
        return None
    if len(body) > INSTAGRAM_TEXT_CHAR_LIMIT:
        body = body[:INSTAGRAM_TEXT_CHAR_LIMIT].rstrip() + "\n\n[Truncated by ChimeraMemory Instagram import.]"
    title = "Instagram conversation"
    if participants:
        title = "Instagram conversation with " + ", ".join(participants[:3])
    return {
        "source_path": source_rel,
        "source_id": _hash_text(f"{source_rel}\n{body[:1000]}"),
        "title": title,
        "created": first_created,
        "body": body,
        "category": "messages",
    }


def _generic_item_documents(source_rel: str, value: object, created: str) -> list[dict]:
    items = value if isinstance(value, list) else [value]
    documents = []
    for index, item in enumerate(items):
        candidates: list[dict]
        if isinstance(item, dict) and isinstance(item.get("media"), list):
            candidates = [media for media in item["media"] if isinstance(media, dict)]
        elif isinstance(item, dict):
            candidates = [item]
        else:
            candidates = []
        for candidate_index, candidate in enumerate(candidates):
            pieces = _string_values(
                candidate,
                (
                    "title",
                    "caption",
                    "content",
                    "text",
                    "comment",
                    "description",
                    "uri",
                    "href",
                ),
            )
            body = _clean_text("\n".join(pieces))
            if not body:
                continue
            if len(body) > INSTAGRAM_TEXT_CHAR_LIMIT:
                body = body[:INSTAGRAM_TEXT_CHAR_LIMIT].rstrip() + "\n\n[Truncated by ChimeraMemory Instagram import.]"
            timestamp = (
                candidate.get("creation_timestamp")
                or candidate.get("timestamp")
                or candidate.get("timestamp_ms")
                or candidate.get("taken_at")
            )
            created_at = _timestamp_to_created(timestamp, created)
            title = _clean_text(str(candidate.get("title") or candidate.get("caption") or body.splitlines()[0][:120]))
            documents.append(
                {
                    "source_path": source_rel,
                    "source_id": _hash_text(f"{source_rel}\n{index}\n{candidate_index}\n{body}"),
                    "title": title or Path(source_rel).stem.replace("_", " "),
                    "created": created_at,
                    "body": body,
                    "category": "content",
                }
            )
    return documents


def _documents_from_raw(source_rel: str, raw: str, created: str) -> list[dict]:
    suffix = Path(source_rel).suffix.lower()
    if suffix == ".txt":
        text = _clean_text(raw)
        if not text:
            return []
        return [
            {
                "source_path": source_rel,
                "source_id": _hash_text(f"{source_rel}\n{text}"),
                "title": text.splitlines()[0][:120] or Path(source_rel).stem.replace("_", " "),
                "created": created or _utc_now(),
                "body": text[:INSTAGRAM_TEXT_CHAR_LIMIT],
                "category": "text",
            }
        ]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        thread = _message_thread_document(source_rel, parsed, created)
        if thread:
            return [thread]
        for key in ("media", "comments", "likes", "items", "data"):
            nested = parsed.get(key)
            if isinstance(nested, list):
                return _generic_item_documents(source_rel, nested, created)
    return _generic_item_documents(source_rel, parsed, created)


def _iter_instagram_documents(import_path: Path) -> list[dict]:
    path = Path(import_path)
    documents: list[dict] = []
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                if info.is_dir() or Path(name).suffix.lower() not in _SUPPORTED_SUFFIXES:
                    continue
                parts = [part for part in name.split("/") if part]
                if any(part in _SKIP_DIRS or part.startswith(".") for part in parts):
                    continue
                raw = archive.read(info).decode("utf-8", errors="replace")
                documents.extend(_documents_from_raw(name, raw, _created_from_zip(info)))
    elif path.is_dir():
        for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
            rel = file_path.relative_to(path).as_posix()
            if Path(rel).suffix.lower() not in _SUPPORTED_SUFFIXES:
                continue
            if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.split("/")):
                continue
            documents.extend(_documents_from_raw(rel, file_path.read_text(encoding="utf-8", errors="replace"), _created_from_mtime(file_path)))
    elif path.is_file() and path.suffix.lower() in _SUPPORTED_SUFFIXES:
        documents.extend(_documents_from_raw(path.name, path.read_text(encoding="utf-8", errors="replace"), _created_from_mtime(path)))
    else:
        raise ValueError("Instagram import path must be a supported file, directory, or zip export")
    return documents


def render_instagram_import_markdown(document: dict) -> str:
    """Render one governed Instagram import memory."""
    title = _clean_text(str(document.get("title") or "Instagram item"))
    frontmatter = {
        "type": "episodic",
        "importance": 4,
        "created": document.get("created") or _utc_now(),
        "status": "active",
        "about": title,
        "tags": INSTAGRAM_IMPORT_TAGS,
        "provenance_status": "imported",
        "confidence": 0.75,
        "lifecycle_status": "active",
        "review_status": "pending",
        "sensitivity_tier": "restricted",
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
            "- source: instagram",
            f"- source_path: {document.get('source_path') or ''}",
            f"- source_id: {document.get('source_id') or ''}",
            f"- category: {document.get('category') or ''}",
            f"- schema: {INSTAGRAM_IMPORT_SCHEMA_VERSION}",
            "",
            "## Source Content",
            "",
            _clean_text(str(document.get("body") or "")),
            "",
        ]
    )
    return "\n".join(lines)


def build_instagram_import_plans(
    import_path: Path,
    *,
    persona: str,
    limit: int = 200,
) -> dict:
    """Build governed markdown import plans from Instagram exports."""
    persona = persona.strip()
    if not persona:
        return {"ok": False, "error": "persona required"}
    try:
        documents = _iter_instagram_documents(Path(import_path))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"ok": False, "error": f"failed to load Instagram export: {exc}"}

    plans = []
    for document in documents[: max(0, min(int(limit), 5000))]:
        rendered = render_instagram_import_markdown(document)
        findings, blocking_findings = _safe_findings(rendered)
        source_hash = _hash_text(f"{document.get('source_path')}\n{document.get('source_id')}\n{document.get('body')}")
        date_prefix = str(document.get("created") or _utc_now())[:10].replace("-", "")
        slug = _slugify(str(document.get("title") or document.get("source_id") or "instagram-item"))
        relative_path = f"memory/imports/instagram/{date_prefix}-{slug}-{source_hash[:10]}.md"
        plans.append(
            {
                "ok": True,
                "schema_version": INSTAGRAM_IMPORT_SCHEMA_VERSION,
                "source": "instagram",
                "source_path": document.get("source_path"),
                "source_id": document.get("source_id"),
                "title": document.get("title"),
                "category": document.get("category"),
                "created": document.get("created"),
                "relative_path": relative_path,
                "guard_findings": findings,
                "blocking_findings": blocking_findings,
                "body": rendered,
            }
        )
    return {
        "ok": True,
        "schema_version": INSTAGRAM_IMPORT_SCHEMA_VERSION,
        "source": "instagram",
        "persona": persona,
        "import_path": str(import_path),
        "document_count": len(documents),
        "plan_count": len(plans),
        "plans": plans,
    }


def write_instagram_import_file(personas_dir: Path, persona: str, plan: dict, *, force: bool = False) -> dict:
    """Write one planned Instagram import memory under the persona folder."""
    if not plan.get("ok"):
        return plan
    if plan.get("blocking_findings"):
        return {
            "ok": False,
            "error": "Instagram import content failed safety scan",
            "blocking_findings": plan["blocking_findings"],
            "source_id": plan.get("source_id"),
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
            "source_id": plan.get("source_id"),
        }
    target.write_text(str(plan["body"]), encoding="utf-8", newline="\n")
    return {
        "ok": True,
        "written": True,
        "skipped": False,
        "path": str(target),
        "relative_path": str(relative_path).replace("\\", "/"),
        "source_id": plan.get("source_id"),
    }


def memory_import_instagram_export(
    conn: sqlite3.Connection,
    personas_dir: Path,
    *,
    import_path: str,
    persona: str,
    index_file_func,
    pyramid_summary_builder,
    limit: int = 200,
    write: bool = False,
    force: bool = False,
    build_pyramid: bool = True,
    actor: str = "agent",
) -> dict:
    """Plan or write governed memories from Instagram exports."""
    plans = build_instagram_import_plans(Path(import_path), persona=persona, limit=limit)
    if not plans.get("ok"):
        return plans

    preview = [
        {
            "source_path": plan.get("source_path"),
            "source_id": plan.get("source_id"),
            "title": plan.get("title"),
            "category": plan.get("category"),
            "relative_path": plan.get("relative_path"),
            "guard_findings": plan.get("guard_findings", []),
            "blocking_findings": plan.get("blocking_findings", []),
        }
        for plan in plans.get("plans", [])
    ]
    audit_payload = {
        "schema_version": plans["schema_version"],
        "source": "instagram",
        "import_path": str(import_path),
        "document_count": plans.get("document_count", 0),
        "plan_count": len(preview),
        "write": bool(write),
        "build_pyramid": bool(build_pyramid),
    }
    if not write:
        record_memory_audit_event(
            conn,
            "memory_import_instagram_planned",
            persona=persona,
            target_kind="instagram_import",
            target_id=str(import_path),
            payload=audit_payload,
            actor=actor,
        )
        return {"ok": True, "written": False, "plans": preview, "summary": audit_payload}

    written = []
    skipped = []
    failed = []
    pyramid_built = 0
    for plan in plans.get("plans", []):
        result = write_instagram_import_file(personas_dir, persona, plan, force=force)
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
            "memory_import_instagram_document",
            persona=persona,
            target_kind="memory_file",
            target_id=str(file_id or relative_path),
            payload={
                "source": "instagram",
                "source_path": plan.get("source_path"),
                "source_id": plan.get("source_id"),
                "category": plan.get("category"),
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
        "memory_import_instagram_completed",
        persona=persona,
        target_kind="instagram_import",
        target_id=str(import_path),
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
