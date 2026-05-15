"""Perplexity export import planning and file writing."""

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

PERPLEXITY_IMPORT_SCHEMA_VERSION = "chimera-memory.perplexity-import.v1"
PERPLEXITY_IMPORT_TAGS = ["import", "perplexity"]
PERPLEXITY_TEXT_CHAR_LIMIT = 30000

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_HEADING_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
_BLOCKING_FINDING_TYPES = {"credential"}
_SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".json"}
_SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(value: str | None) -> str:
    sanitized = sanitize_content(value or "") or ""
    return sanitized.replace("\r\n", "\n").replace("\r", "\n").strip()


def _yaml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=True)


def _slugify(value: str, fallback: str = "perplexity-thread") -> str:
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


def _title_from_text(source_rel: str, text: str, metadata: dict | None = None) -> str:
    metadata = metadata or {}
    for key in ("title", "name", "query", "question", "prompt"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return _clean_text(value)
    match = _HEADING_RE.search(text)
    if match:
        return _clean_text(match.group(1))
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return _clean_text(first_line[:120]) or Path(source_rel).stem.replace("-", " ").replace("_", " ")


def _json_to_text(value: object) -> tuple[str, dict]:
    metadata: dict = {}
    if isinstance(value, dict):
        metadata = {key: value.get(key) for key in ("title", "name", "query", "question", "prompt") if value.get(key)}
        for key in ("markdown", "content", "answer", "text", "transcript"):
            content = value.get(key)
            if isinstance(content, str) and content.strip():
                return content, metadata
        messages = value.get("messages") or value.get("conversation") or value.get("turns")
        if isinstance(messages, list):
            lines = []
            for item in messages:
                if isinstance(item, dict):
                    role = str(item.get("role") or item.get("author") or "entry").upper()
                    content = item.get("content") or item.get("text") or item.get("message")
                    if isinstance(content, str) and content.strip():
                        lines.extend([f"### {role}", "", content.strip(), ""])
                elif isinstance(item, str) and item.strip():
                    lines.extend([item.strip(), ""])
            return "\n".join(lines).strip(), metadata
    if isinstance(value, list):
        lines = []
        for item in value:
            text, _ = _json_to_text(item)
            if text:
                lines.append(text)
        return "\n\n".join(lines), metadata
    if isinstance(value, str):
        return value, metadata
    return "", metadata


def _document_from_raw(source_rel: str, raw: str, created: str) -> dict | None:
    suffix = Path(source_rel).suffix.lower()
    metadata: dict = {}
    if suffix == ".json":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        text, metadata = _json_to_text(parsed)
    else:
        frontmatter, body = parse_frontmatter(raw)
        metadata = frontmatter
        text = body
    text = _clean_text(text)
    if not text:
        return None
    if len(text) > PERPLEXITY_TEXT_CHAR_LIMIT:
        text = text[:PERPLEXITY_TEXT_CHAR_LIMIT].rstrip() + "\n\n[Truncated by ChimeraMemory Perplexity import.]"
    return {
        "source_path": source_rel,
        "title": _title_from_text(source_rel, text, metadata),
        "created": str(metadata.get("created") or metadata.get("date") or created or _utc_now()),
        "body": text,
    }


def _iter_perplexity_documents(import_path: Path) -> list[dict]:
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
                doc = _document_from_raw(name, raw, _created_from_zip(info))
                if doc:
                    documents.append(doc)
    elif path.is_dir():
        for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
            rel = file_path.relative_to(path).as_posix()
            if Path(rel).suffix.lower() not in _SUPPORTED_SUFFIXES:
                continue
            if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.split("/")):
                continue
            doc = _document_from_raw(rel, file_path.read_text(encoding="utf-8", errors="replace"), _created_from_mtime(file_path))
            if doc:
                documents.append(doc)
    elif path.is_file() and path.suffix.lower() in _SUPPORTED_SUFFIXES:
        doc = _document_from_raw(path.name, path.read_text(encoding="utf-8", errors="replace"), _created_from_mtime(path))
        if doc:
            documents.append(doc)
    else:
        raise ValueError("Perplexity import path must be a supported file, directory, or zip export")
    return documents


def render_perplexity_import_markdown(document: dict) -> str:
    """Render one governed Perplexity import memory."""
    title = _clean_text(str(document.get("title") or "Perplexity thread"))
    frontmatter = {
        "type": "semantic",
        "importance": 5,
        "created": document.get("created") or _utc_now(),
        "status": "active",
        "about": title,
        "tags": PERPLEXITY_IMPORT_TAGS,
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
            "- source: perplexity",
            f"- source_path: {document.get('source_path') or ''}",
            f"- schema: {PERPLEXITY_IMPORT_SCHEMA_VERSION}",
            "",
            "## Source Content",
            "",
            _clean_text(str(document.get("body") or "")),
            "",
        ]
    )
    return "\n".join(lines)


def build_perplexity_import_plans(
    import_path: Path,
    *,
    persona: str,
    limit: int = 200,
) -> dict:
    """Build governed markdown import plans from Perplexity exports."""
    persona = persona.strip()
    if not persona:
        return {"ok": False, "error": "persona required"}
    try:
        documents = _iter_perplexity_documents(Path(import_path))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"ok": False, "error": f"failed to load Perplexity export: {exc}"}

    plans = []
    for document in documents[: max(0, min(int(limit), 5000))]:
        rendered = render_perplexity_import_markdown(document)
        findings, blocking_findings = _safe_findings(rendered)
        source_hash = _hash_text(f"{document.get('source_path')}\n{document.get('body')}")
        slug = _slugify(str(document.get("title") or document.get("source_path") or "perplexity-thread"))
        relative_path = f"memory/imports/perplexity/{slug}-{source_hash[:10]}.md"
        plans.append(
            {
                "ok": True,
                "schema_version": PERPLEXITY_IMPORT_SCHEMA_VERSION,
                "source": "perplexity",
                "source_path": document.get("source_path"),
                "source_id": source_hash,
                "title": document.get("title"),
                "created": document.get("created"),
                "relative_path": relative_path,
                "guard_findings": findings,
                "blocking_findings": blocking_findings,
                "body": rendered,
            }
        )
    return {
        "ok": True,
        "schema_version": PERPLEXITY_IMPORT_SCHEMA_VERSION,
        "source": "perplexity",
        "persona": persona,
        "import_path": str(import_path),
        "document_count": len(documents),
        "plan_count": len(plans),
        "plans": plans,
    }


def write_perplexity_import_file(personas_dir: Path, persona: str, plan: dict, *, force: bool = False) -> dict:
    """Write one planned Perplexity import memory under the persona folder."""
    if not plan.get("ok"):
        return plan
    if plan.get("blocking_findings"):
        return {
            "ok": False,
            "error": "Perplexity import content failed safety scan",
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


def memory_import_perplexity_export(
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
    """Plan or write governed memories from Perplexity exports."""
    plans = build_perplexity_import_plans(Path(import_path), persona=persona, limit=limit)
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
        "source": "perplexity",
        "import_path": str(import_path),
        "document_count": plans.get("document_count", 0),
        "plan_count": len(preview),
        "write": bool(write),
        "build_pyramid": bool(build_pyramid),
    }
    if not write:
        record_memory_audit_event(
            conn,
            "memory_import_perplexity_planned",
            persona=persona,
            target_kind="perplexity_import",
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
        result = write_perplexity_import_file(personas_dir, persona, plan, force=force)
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
            "memory_import_perplexity_document",
            persona=persona,
            target_kind="memory_file",
            target_id=str(file_id or relative_path),
            payload={
                "source": "perplexity",
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
        "memory_import_perplexity_completed",
        persona=persona,
        target_kind="perplexity_import",
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
