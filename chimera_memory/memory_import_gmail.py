"""Gmail / Google Takeout mbox import planning and file writing."""

from __future__ import annotations

import hashlib
import html
import json
import mailbox
import re
import sqlite3
import tempfile
import zipfile
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path

from .memory_auto_capture import resolve_persona_root
from .memory_observability import record_memory_audit_event
from .sanitizer import sanitize_content, scan_for_injection

GMAIL_IMPORT_SCHEMA_VERSION = "chimera-memory.gmail-import.v1"
GMAIL_IMPORT_TAGS = ["import", "gmail", "email"]
GMAIL_BODY_CHAR_LIMIT = 20000

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BLOCKING_FINDING_TYPES = {"credential"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(value: str | None) -> str:
    sanitized = sanitize_content(value or "") or ""
    return sanitized.replace("\r\n", "\n").replace("\r", "\n").strip()


def _yaml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=True)


def _slugify(value: str, fallback: str = "gmail-message") -> str:
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


def _header_text(value: object) -> str:
    return _clean_text(str(value or ""))


def _address_list(value: object) -> list[str]:
    addresses = []
    for name, address in getaddresses([str(value or "")]):
        display = " ".join(part for part in [name.strip(), f"<{address.strip()}>" if address else ""] if part)
        if display:
            addresses.append(_clean_text(display))
    return addresses


def _message_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return _utc_now()
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return _utc_now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _message_body(message) -> str:
    body = None
    if message.is_multipart():
        part = message.get_body(preferencelist=("plain", "html"))
        if part is not None:
            try:
                body = part.get_content()
            except (LookupError, UnicodeDecodeError):
                body = ""
            if part.get_content_subtype() == "html":
                body = html.unescape(_HTML_TAG_RE.sub(" ", str(body or "")))
    elif message.get_content_maintype() == "text":
        try:
            body = message.get_content()
        except (LookupError, UnicodeDecodeError):
            body = ""
        if message.get_content_subtype() == "html":
            body = html.unescape(_HTML_TAG_RE.sub(" ", str(body or "")))
    text = _clean_text(str(body or ""))
    if len(text) > GMAIL_BODY_CHAR_LIMIT:
        text = text[:GMAIL_BODY_CHAR_LIMIT].rstrip() + "\n\n[Truncated by ChimeraMemory Gmail import.]"
    return text


def _parse_mbox(path: Path, source_label: str) -> list[dict]:
    parser = BytesParser(policy=policy.default)
    box = mailbox.mbox(str(path), factory=lambda handle: parser.parse(handle))
    messages = []
    try:
        for ordinal, message in enumerate(box):
            body = _message_body(message)
            if not body:
                continue
            subject = _header_text(message.get("Subject")) or "(no subject)"
            message_id = _header_text(message.get("Message-ID"))
            date = _message_date(message.get("Date"))
            from_addresses = _address_list(message.get("From"))
            to_addresses = _address_list(message.get("To"))
            cc_addresses = _address_list(message.get("Cc"))
            source_id = message_id or _hash_text(
                "\n".join([source_label, str(ordinal), subject, date, body[:1000]])
            )
            messages.append(
                {
                    "source_path": source_label,
                    "ordinal": ordinal,
                    "source_id": source_id,
                    "subject": subject,
                    "date": date,
                    "from": from_addresses,
                    "to": to_addresses,
                    "cc": cc_addresses,
                    "body": body,
                }
            )
    finally:
        box.close()
    return messages


def _load_gmail_messages(import_path: Path) -> list[dict]:
    path = Path(import_path)
    if path.suffix.lower() == ".zip":
        messages: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="chimera-gmail-import-") as tmp:
            tmp_root = Path(tmp)
            with zipfile.ZipFile(path) as archive:
                mbox_infos = [
                    info for info in archive.infolist()
                    if not info.is_dir() and info.filename.replace("\\", "/").lower().endswith(".mbox")
                ]
                if not mbox_infos:
                    raise ValueError("zip export does not contain an mbox file")
                for idx, info in enumerate(mbox_infos):
                    extracted = tmp_root / f"{idx}-{Path(info.filename).name or 'mail.mbox'}"
                    extracted.write_bytes(archive.read(info))
                    messages.extend(_parse_mbox(extracted, info.filename.replace("\\", "/")))
        return messages
    if path.is_dir():
        mbox_paths = sorted(item for item in path.rglob("*.mbox") if item.is_file())
        if not mbox_paths:
            raise ValueError("directory does not contain any .mbox files")
        messages = []
        for mbox_path in mbox_paths:
            messages.extend(_parse_mbox(mbox_path, mbox_path.relative_to(path).as_posix()))
        return messages
    if path.is_file():
        return _parse_mbox(path, path.name)
    raise ValueError("Gmail import path must be an mbox file, directory, or zip export")


def render_gmail_import_markdown(message: dict) -> str:
    """Render one governed Gmail message import memory."""
    subject = _clean_text(str(message.get("subject") or "(no subject)"))
    frontmatter = {
        "type": "episodic",
        "importance": 5,
        "created": message.get("date") or _utc_now(),
        "status": "active",
        "about": subject,
        "tags": GMAIL_IMPORT_TAGS,
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
            f"# {subject}",
            "",
            "## Import Metadata",
            "- source: gmail",
            f"- source_path: {message.get('source_path') or ''}",
            f"- source_id: {message.get('source_id') or ''}",
            f"- schema: {GMAIL_IMPORT_SCHEMA_VERSION}",
            "",
            "## Email Headers",
            f"- from: {', '.join(message.get('from') or [])}",
            f"- to: {', '.join(message.get('to') or [])}",
            f"- cc: {', '.join(message.get('cc') or [])}",
            f"- date: {message.get('date') or ''}",
            "",
            "## Email Body",
            "",
            _clean_text(str(message.get("body") or "")),
            "",
        ]
    )
    return "\n".join(lines)


def build_gmail_import_plans(
    import_path: Path,
    *,
    persona: str,
    limit: int = 200,
) -> dict:
    """Build governed markdown import plans from Gmail mbox exports."""
    persona = persona.strip()
    if not persona:
        return {"ok": False, "error": "persona required"}
    try:
        messages = _load_gmail_messages(Path(import_path))
    except (OSError, ValueError, zipfile.BadZipFile, mailbox.Error) as exc:
        return {"ok": False, "error": f"failed to load Gmail export: {exc}"}

    plans = []
    for message in messages[: max(0, min(int(limit), 5000))]:
        rendered = render_gmail_import_markdown(message)
        findings, blocking_findings = _safe_findings(rendered)
        source_hash = _hash_text(f"{message.get('source_path')}\n{message.get('source_id')}\n{message.get('body')}")
        date_prefix = str(message.get("date") or _utc_now())[:10].replace("-", "")
        slug = _slugify(str(message.get("subject") or "gmail-message"))
        relative_path = f"memory/imports/gmail/{date_prefix}-{slug}-{source_hash[:10]}.md"
        plans.append(
            {
                "ok": True,
                "schema_version": GMAIL_IMPORT_SCHEMA_VERSION,
                "source": "gmail",
                "source_path": message.get("source_path"),
                "source_id": message.get("source_id"),
                "subject": message.get("subject"),
                "date": message.get("date"),
                "relative_path": relative_path,
                "guard_findings": findings,
                "blocking_findings": blocking_findings,
                "body": rendered,
            }
        )
    return {
        "ok": True,
        "schema_version": GMAIL_IMPORT_SCHEMA_VERSION,
        "source": "gmail",
        "persona": persona,
        "import_path": str(import_path),
        "message_count": len(messages),
        "plan_count": len(plans),
        "plans": plans,
    }


def write_gmail_import_file(personas_dir: Path, persona: str, plan: dict, *, force: bool = False) -> dict:
    """Write one planned Gmail import memory under the persona folder."""
    if not plan.get("ok"):
        return plan
    if plan.get("blocking_findings"):
        return {
            "ok": False,
            "error": "Gmail import content failed safety scan",
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


def memory_import_gmail_mbox(
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
    """Plan or write governed memories from Gmail mbox exports."""
    plans = build_gmail_import_plans(Path(import_path), persona=persona, limit=limit)
    if not plans.get("ok"):
        return plans

    preview = [
        {
            "source_path": plan.get("source_path"),
            "source_id": plan.get("source_id"),
            "subject": plan.get("subject"),
            "relative_path": plan.get("relative_path"),
            "guard_findings": plan.get("guard_findings", []),
            "blocking_findings": plan.get("blocking_findings", []),
        }
        for plan in plans.get("plans", [])
    ]
    audit_payload = {
        "schema_version": plans["schema_version"],
        "source": "gmail",
        "import_path": str(import_path),
        "message_count": plans.get("message_count", 0),
        "plan_count": len(preview),
        "write": bool(write),
        "build_pyramid": bool(build_pyramid),
    }
    if not write:
        record_memory_audit_event(
            conn,
            "memory_import_gmail_planned",
            persona=persona,
            target_kind="gmail_import",
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
        result = write_gmail_import_file(personas_dir, persona, plan, force=force)
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
            "memory_import_gmail_message",
            persona=persona,
            target_kind="memory_file",
            target_id=str(file_id or relative_path),
            payload={
                "source": "gmail",
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
        "memory_import_gmail_completed",
        persona=persona,
        target_kind="gmail_import",
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
