"""Atom / Blogger export import planning and file writing."""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from .memory_auto_capture import resolve_persona_root
from .memory_observability import record_memory_audit_event
from .sanitizer import sanitize_content, scan_for_injection

ATOM_BLOGGER_IMPORT_SCHEMA_VERSION = "chimera-memory.atom-blogger-import.v1"
ATOM_BLOGGER_IMPORT_TAGS = ["import", "atom", "blogger"]
ATOM_BLOGGER_TEXT_CHAR_LIMIT = 30000

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BLOCKING_FINDING_TYPES = {"credential"}
_SUPPORTED_SUFFIXES = {".xml", ".atom"}
_SKIP_DIRS = {".git", "__pycache__", "node_modules"}
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(value: str | None) -> str:
    sanitized = sanitize_content(value or "") or ""
    return sanitized.replace("\r\n", "\n").replace("\r", "\n").strip()


def _yaml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=True)


def _slugify(value: str, fallback: str = "atom-entry") -> str:
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


def _html_to_text(raw: str) -> str:
    text = raw.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = _HTML_TAG_RE.sub(" ", text)
    return _clean_text(html.unescape(text))


def _child_text(element: ElementTree.Element, tag: str) -> str:
    found = element.find(f"atom:{tag}", _ATOM_NS)
    if found is None:
        found = element.find(tag)
    if found is None:
        return ""
    return _clean_text("".join(found.itertext()))


def _entry_content(element: ElementTree.Element) -> str:
    for tag in ("content", "summary"):
        found = element.find(f"atom:{tag}", _ATOM_NS)
        if found is None:
            found = element.find(tag)
        if found is None:
            continue
        text = "".join(found.itertext())
        if (found.attrib.get("type") or "").lower() in {"html", "xhtml"}:
            text = _html_to_text(text)
        else:
            text = _clean_text(text)
        if text:
            return text
    return ""


def _entry_author(element: ElementTree.Element) -> str:
    author = element.find("atom:author", _ATOM_NS)
    if author is None:
        author = element.find("author")
    if author is None:
        return ""
    return _child_text(author, "name") or _clean_text("".join(author.itertext()))


def _entry_categories(element: ElementTree.Element) -> list[str]:
    categories = []
    for category in list(element.findall("atom:category", _ATOM_NS)) + list(element.findall("category")):
        term = category.attrib.get("term") or category.attrib.get("label")
        if term:
            categories.append(_clean_text(term))
    return categories


def _entry_link(element: ElementTree.Element) -> str:
    for link in list(element.findall("atom:link", _ATOM_NS)) + list(element.findall("link")):
        rel = (link.attrib.get("rel") or "alternate").lower()
        href = link.attrib.get("href")
        if href and rel in {"alternate", "self"}:
            return _clean_text(href)
    return ""


def _entry_to_document(source_rel: str, entry: ElementTree.Element, ordinal: int, created: str) -> dict | None:
    title = _child_text(entry, "title") or "Blogger entry"
    body = _entry_content(entry)
    if not body:
        return None
    if len(body) > ATOM_BLOGGER_TEXT_CHAR_LIMIT:
        body = body[:ATOM_BLOGGER_TEXT_CHAR_LIMIT].rstrip() + "\n\n[Truncated by ChimeraMemory Atom/Blogger import.]"
    source_id = _child_text(entry, "id") or _hash_text(f"{source_rel}\n{ordinal}\n{title}\n{body[:1000]}")
    published = _child_text(entry, "published") or _child_text(entry, "updated") or created or _utc_now()
    return {
        "source_path": source_rel,
        "source_id": source_id,
        "title": title,
        "created": published,
        "body": body,
        "author": _entry_author(entry),
        "categories": _entry_categories(entry),
        "link": _entry_link(entry),
    }


def _documents_from_raw(source_rel: str, raw: str, created: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return []
    entries = root.findall("atom:entry", _ATOM_NS)
    if not entries:
        entries = root.findall("entry")
    if not entries and root.tag.endswith("entry"):
        entries = [root]
    documents = []
    for ordinal, entry in enumerate(entries):
        document = _entry_to_document(source_rel, entry, ordinal, created)
        if document:
            documents.append(document)
    return documents


def _iter_atom_blogger_documents(import_path: Path) -> list[dict]:
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
        raise ValueError("Atom/Blogger import path must be an XML/Atom file, directory, or zip export")
    return documents


def render_atom_blogger_import_markdown(document: dict) -> str:
    """Render one governed Atom/Blogger import memory."""
    title = _clean_text(str(document.get("title") or "Blogger entry"))
    frontmatter = {
        "type": "semantic",
        "importance": 5,
        "created": document.get("created") or _utc_now(),
        "status": "active",
        "about": title,
        "tags": ATOM_BLOGGER_IMPORT_TAGS,
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
            "- source: atom-blogger",
            f"- source_path: {document.get('source_path') or ''}",
            f"- source_id: {document.get('source_id') or ''}",
            f"- author: {document.get('author') or ''}",
            f"- categories: {', '.join(document.get('categories') or [])}",
            f"- link: {document.get('link') or ''}",
            f"- schema: {ATOM_BLOGGER_IMPORT_SCHEMA_VERSION}",
            "",
            "## Source Content",
            "",
            _clean_text(str(document.get("body") or "")),
            "",
        ]
    )
    return "\n".join(lines)


def build_atom_blogger_import_plans(
    import_path: Path,
    *,
    persona: str,
    limit: int = 200,
) -> dict:
    """Build governed markdown import plans from Atom/Blogger exports."""
    persona = persona.strip()
    if not persona:
        return {"ok": False, "error": "persona required"}
    try:
        documents = _iter_atom_blogger_documents(Path(import_path))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"ok": False, "error": f"failed to load Atom/Blogger export: {exc}"}

    plans = []
    for document in documents[: max(0, min(int(limit), 5000))]:
        rendered = render_atom_blogger_import_markdown(document)
        findings, blocking_findings = _safe_findings(rendered)
        source_hash = _hash_text(f"{document.get('source_path')}\n{document.get('source_id')}\n{document.get('body')}")
        date_prefix = str(document.get("created") or _utc_now())[:10].replace("-", "")
        slug = _slugify(str(document.get("title") or document.get("source_id") or "atom-entry"))
        relative_path = f"memory/imports/atom-blogger/{date_prefix}-{slug}-{source_hash[:10]}.md"
        plans.append(
            {
                "ok": True,
                "schema_version": ATOM_BLOGGER_IMPORT_SCHEMA_VERSION,
                "source": "atom-blogger",
                "source_path": document.get("source_path"),
                "source_id": document.get("source_id"),
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
        "schema_version": ATOM_BLOGGER_IMPORT_SCHEMA_VERSION,
        "source": "atom-blogger",
        "persona": persona,
        "import_path": str(import_path),
        "document_count": len(documents),
        "plan_count": len(plans),
        "plans": plans,
    }


def write_atom_blogger_import_file(personas_dir: Path, persona: str, plan: dict, *, force: bool = False) -> dict:
    """Write one planned Atom/Blogger import memory under the persona folder."""
    if not plan.get("ok"):
        return plan
    if plan.get("blocking_findings"):
        return {
            "ok": False,
            "error": "Atom/Blogger import content failed safety scan",
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


def memory_import_atom_blogger_export(
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
    """Plan or write governed memories from Atom/Blogger exports."""
    plans = build_atom_blogger_import_plans(Path(import_path), persona=persona, limit=limit)
    if not plans.get("ok"):
        return plans

    preview = [
        {
            "source_path": plan.get("source_path"),
            "source_id": plan.get("source_id"),
            "title": plan.get("title"),
            "relative_path": plan.get("relative_path"),
            "guard_findings": plan.get("guard_findings", []),
            "blocking_findings": plan.get("blocking_findings", []),
        }
        for plan in plans.get("plans", [])
    ]
    audit_payload = {
        "schema_version": plans["schema_version"],
        "source": "atom-blogger",
        "import_path": str(import_path),
        "document_count": plans.get("document_count", 0),
        "plan_count": len(preview),
        "write": bool(write),
        "build_pyramid": bool(build_pyramid),
    }
    if not write:
        record_memory_audit_event(
            conn,
            "memory_import_atom_blogger_planned",
            persona=persona,
            target_kind="atom_blogger_import",
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
        result = write_atom_blogger_import_file(personas_dir, persona, plan, force=force)
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
            "memory_import_atom_blogger_document",
            persona=persona,
            target_kind="memory_file",
            target_id=str(file_id or relative_path),
            payload={
                "source": "atom-blogger",
                "source_path": plan.get("source_path"),
                "source_id": plan.get("source_id"),
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
        "memory_import_atom_blogger_completed",
        persona=persona,
        target_kind="atom_blogger_import",
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
