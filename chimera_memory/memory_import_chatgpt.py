"""ChatGPT export import planning and file writing."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_auto_capture import resolve_persona_root
from .memory_observability import record_memory_audit_event
from .sanitizer import sanitize_content, scan_for_injection

CHATGPT_IMPORT_SCHEMA_VERSION = "chimera-memory.chatgpt-import.v1"
CHATGPT_IMPORT_TAGS = ["import", "chatgpt"]

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_BLOCKING_FINDING_TYPES = {"credential"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(value: str | None) -> str:
    sanitized = sanitize_content(value or "") or ""
    return sanitized.replace("\r\n", "\n").replace("\r", "\n").strip()


def _yaml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=True)


def _slugify(value: str, fallback: str = "chatgpt-conversation") -> str:
    text = _SLUG_RE.sub("-", value.lower()).strip("-")
    return (text or fallback)[:72].strip("-") or fallback


def _iso_from_timestamp(value: object, fallback: str | None = None) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback or _utc_now()
    return datetime.fromtimestamp(parsed, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def _load_conversations_json(export_path: Path) -> list[dict]:
    path = Path(export_path)
    if path.is_dir():
        path = path / "conversations.json"
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.replace("\\", "/").endswith("conversations.json")]
            if not names:
                raise ValueError("zip export does not contain conversations.json")
            with archive.open(names[0]) as handle:
                payload = json.loads(handle.read().decode("utf-8"))
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("conversations.json must contain a list")
    return [item for item in payload if isinstance(item, dict)]


def _message_text(message: dict) -> str:
    content = message.get("content") or {}
    parts = content.get("parts")
    if isinstance(parts, list):
        text_parts = []
        for part in parts:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    text_parts.append(text)
        return _clean_text("\n".join(text_parts))
    text = content.get("text") or message.get("text") or ""
    if isinstance(text, str):
        return _clean_text(text)
    return ""


def _flatten_messages(conversation: dict) -> list[dict]:
    mapping = conversation.get("mapping") or {}
    messages: list[dict] = []
    if isinstance(mapping, dict):
        for node in mapping.values():
            if not isinstance(node, dict):
                continue
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            text = _message_text(message)
            if not text:
                continue
            author = message.get("author") or {}
            role = str(author.get("role") or "unknown")
            messages.append(
                {
                    "role": role,
                    "created": _iso_from_timestamp(message.get("create_time"), ""),
                    "timestamp": message.get("create_time") or 0,
                    "content": text,
                }
            )
    messages.sort(key=lambda item: (float(item.get("timestamp") or 0), item.get("role") or ""))
    return messages


def _conversation_identity(conversation: dict, ordinal: int) -> tuple[str, str, str]:
    title = _clean_text(str(conversation.get("title") or "")) or "ChatGPT conversation"
    conversation_id = _clean_text(str(conversation.get("id") or conversation.get("conversation_id") or "")) or str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{title}:{conversation.get('create_time') or ''}:{ordinal}",
    ))
    created = _iso_from_timestamp(conversation.get("create_time"), fallback=_utc_now())
    return conversation_id, title, created


def build_chatgpt_import_plans(
    export_path: Path,
    *,
    persona: str,
    limit: int = 50,
) -> dict:
    """Build governed markdown import plans from a ChatGPT conversations export."""
    persona = persona.strip()
    if not persona:
        return {"ok": False, "error": "persona required"}
    try:
        conversations = _load_conversations_json(Path(export_path))
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        return {"ok": False, "error": f"failed to load ChatGPT export: {exc}"}

    plans = []
    for ordinal, conversation in enumerate(conversations[: max(0, min(int(limit), 1000))]):
        conversation_id, title, created = _conversation_identity(conversation, ordinal)
        messages = _flatten_messages(conversation)
        if not messages:
            continue
        body = render_chatgpt_import_markdown(
            title=title,
            conversation_id=conversation_id,
            created=created,
            messages=messages,
        )
        findings, blocking_findings = _safe_findings(body)
        slug = _slugify(title)
        date_prefix = created[:10].replace("-", "")
        id_suffix = uuid.uuid5(uuid.NAMESPACE_URL, conversation_id).hex[:10]
        relative_path = f"memory/imports/chatgpt/{date_prefix}-{slug}-{id_suffix}.md"
        plans.append(
            {
                "ok": True,
                "schema_version": CHATGPT_IMPORT_SCHEMA_VERSION,
                "source": "chatgpt",
                "source_id": conversation_id,
                "title": title,
                "created": created,
                "relative_path": relative_path,
                "message_count": len(messages),
                "guard_findings": findings,
                "blocking_findings": blocking_findings,
                "body": body,
            }
        )
    return {
        "ok": True,
        "schema_version": CHATGPT_IMPORT_SCHEMA_VERSION,
        "source": "chatgpt",
        "persona": persona,
        "export_path": str(export_path),
        "conversation_count": len(conversations),
        "plan_count": len(plans),
        "plans": plans,
    }


def render_chatgpt_import_markdown(
    *,
    title: str,
    conversation_id: str,
    created: str,
    messages: list[dict],
) -> str:
    frontmatter = {
        "type": "episodic",
        "importance": 5,
        "created": created,
        "status": "active",
        "about": title,
        "tags": CHATGPT_IMPORT_TAGS,
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
            f"- source: chatgpt",
            f"- source_id: {conversation_id}",
            f"- schema: {CHATGPT_IMPORT_SCHEMA_VERSION}",
            f"- message_count: {len(messages)}",
            "",
            "## Conversation",
        ]
    )
    for message in messages:
        role = _clean_text(str(message.get("role") or "unknown")).upper()
        created_at = _clean_text(str(message.get("created") or ""))
        lines.extend(["", f"### {role} {created_at}".rstrip(), "", _clean_text(str(message.get("content") or ""))])
    lines.append("")
    return "\n".join(lines)


def write_chatgpt_import_file(personas_dir: Path, persona: str, plan: dict, *, force: bool = False) -> dict:
    """Write one planned ChatGPT import memory under the persona folder."""
    if not plan.get("ok"):
        return plan
    if plan.get("blocking_findings"):
        return {
            "ok": False,
            "error": "ChatGPT import content failed safety scan",
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


def memory_import_chatgpt_export(
    conn: sqlite3.Connection,
    personas_dir: Path,
    *,
    export_path: str,
    persona: str,
    index_file_func,
    pyramid_summary_builder,
    limit: int = 50,
    write: bool = False,
    force: bool = False,
    build_pyramid: bool = True,
    actor: str = "agent",
) -> dict:
    """Plan or write governed memories from a ChatGPT conversations export."""
    plans = build_chatgpt_import_plans(Path(export_path), persona=persona, limit=limit)
    if not plans.get("ok"):
        return plans

    preview = [
        {
            "source_id": plan.get("source_id"),
            "title": plan.get("title"),
            "relative_path": plan.get("relative_path"),
            "message_count": plan.get("message_count"),
            "guard_findings": plan.get("guard_findings", []),
            "blocking_findings": plan.get("blocking_findings", []),
        }
        for plan in plans.get("plans", [])
    ]
    audit_payload = {
        "schema_version": plans["schema_version"],
        "source": "chatgpt",
        "export_path": str(export_path),
        "conversation_count": plans.get("conversation_count", 0),
        "plan_count": len(preview),
        "write": bool(write),
        "build_pyramid": bool(build_pyramid),
    }
    if not write:
        record_memory_audit_event(
            conn,
            "memory_import_chatgpt_planned",
            persona=persona,
            target_kind="chatgpt_import",
            target_id=str(export_path),
            payload=audit_payload,
            actor=actor,
        )
        return {"ok": True, "written": False, "plans": preview, "summary": audit_payload}

    written = []
    skipped = []
    failed = []
    pyramid_built = 0
    for plan in plans.get("plans", []):
        result = write_chatgpt_import_file(personas_dir, persona, plan, force=force)
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
            "memory_import_chatgpt_conversation",
            persona=persona,
            target_kind="memory_file",
            target_id=str(file_id or relative_path),
            payload={
                "source": "chatgpt",
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
        "memory_import_chatgpt_completed",
        persona=persona,
        target_kind="chatgpt_import",
        target_id=str(export_path),
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
