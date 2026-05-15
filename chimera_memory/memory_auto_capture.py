"""Session-close auto-capture planning and file writing."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .sanitizer import sanitize_content, scan_for_injection

AUTO_CAPTURE_SCHEMA_VERSION = "chimera-memory.auto-capture.v1"
AUTO_CAPTURE_TAGS = ["auto-capture", "session-close"]

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ACTION_PATTERNS = [
    re.compile(r"^(?:[-*]\s*)?\[ \]\s*(?P<item>.+)$", re.IGNORECASE),
    re.compile(r"^(?:[-*]\s*)?(?:act\s+now|action\s+item|todo)\s*[:\-]\s*(?P<item>.+)$", re.IGNORECASE),
    re.compile(r"^(?:[-*]\s*)?todo\s+(?P<item>.+)$", re.IGNORECASE),
]
_BLOCKING_FINDING_TYPES = {"credential"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(value: str | None) -> str:
    sanitized = sanitize_content(value or "") or ""
    return sanitized.replace("\r\n", "\n").replace("\r", "\n").strip()


def _yaml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=True)


def _slugify(value: str, fallback: str = "session-capture") -> str:
    text = _SLUG_RE.sub("-", value.lower()).strip("-")
    return (text or fallback)[:64].strip("-") or fallback


def _dedupe(items: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        cleaned = _clean_text(item)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
        if len(deduped) >= limit:
            break
    return deduped


def parse_action_items(text: str, *, limit: int = 20) -> list[str]:
    """Extract ACT NOW style action items from plain text."""
    items: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for pattern in _ACTION_PATTERNS:
            match = pattern.match(line)
            if match:
                items.append(match.group("item"))
                break
    return _dedupe(items, limit)


def _fallback_summary(session_text: str, *, max_chars: int = 1200) -> str:
    lines = [_clean_text(line) for line in session_text.splitlines()]
    non_empty = [line for line in lines if line]
    summary = "\n".join(non_empty[:12])
    if len(summary) > max_chars:
        return summary[: max_chars - 3].rstrip() + "..."
    return summary


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


def resolve_persona_root(personas_dir: Path, persona: str) -> Path | None:
    """Find a persona directory under the standard category/persona layout."""
    persona = persona.strip()
    if not persona:
        return None
    direct = personas_dir / persona
    if direct.is_dir():
        return direct
    if not personas_dir.exists():
        return None
    for category in sorted(personas_dir.iterdir(), key=lambda item: item.name.lower()):
        candidate = category / persona
        if category.is_dir() and candidate.is_dir():
            return candidate
    return None


def build_auto_capture_plan(
    *,
    persona: str,
    title: str = "",
    summary: str = "",
    session_text: str = "",
    act_now_text: str = "",
    source_session_id: str = "",
    created_at: str | None = None,
    importance: int = 6,
) -> dict:
    """Build an evidence-only session-close capture plan."""
    created_at = created_at or _utc_now()
    persona = persona.strip()
    safe_title = _clean_text(title) or "Session close capture"
    safe_summary = _clean_text(summary) or _fallback_summary(_clean_text(session_text))
    explicit_items: list[str] = []
    for raw_line in _clean_text(act_now_text).splitlines():
        parsed = parse_action_items(raw_line, limit=1)
        explicit_items.append(parsed[0] if parsed else raw_line.strip(" -*\t"))
    action_items = _dedupe(explicit_items, 20) or parse_action_items(session_text or summary)

    if not persona:
        return {"ok": False, "error": "persona required"}
    if not safe_summary and not action_items:
        return {"ok": False, "error": "summary or action items required"}

    capture_id = str(uuid.uuid4())
    filename = f"{created_at[:10].replace('-', '')}-{created_at[11:19].replace(':', '')}-{_slugify(safe_title)}.md"
    relative_path = f"memory/episodes/{filename}"
    frontmatter = {
        "type": "episodic",
        "importance": max(1, min(10, int(importance))),
        "created": created_at,
        "status": "active",
        "about": safe_title,
        "tags": AUTO_CAPTURE_TAGS,
        "provenance_status": "generated",
        "confidence": 0.7,
        "lifecycle_status": "active",
        "review_status": "pending",
        "sensitivity_tier": "standard",
        "can_use_as_instruction": False,
        "can_use_as_evidence": True,
        "requires_user_confirmation": True,
    }
    body = render_auto_capture_markdown(
        frontmatter=frontmatter,
        title=safe_title,
        summary=safe_summary,
        action_items=action_items,
        capture_id=capture_id,
        source_session_id=_clean_text(source_session_id),
    )
    findings, blocking_findings = _safe_findings(body)
    return {
        "ok": True,
        "schema_version": AUTO_CAPTURE_SCHEMA_VERSION,
        "capture_id": capture_id,
        "persona": persona,
        "relative_path": relative_path,
        "frontmatter": frontmatter,
        "summary": safe_summary,
        "action_items": action_items,
        "guard_findings": findings,
        "blocking_findings": blocking_findings,
        "body": body,
    }


def render_auto_capture_markdown(
    *,
    frontmatter: dict,
    title: str,
    summary: str,
    action_items: list[str],
    capture_id: str,
    source_session_id: str = "",
) -> str:
    """Render a session-close capture as a governed memory file."""
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {_yaml_value(value)}")
    lines.extend(["---", "", f"# {title}", "", "## Summary"])
    lines.append(summary or "No summary captured.")
    lines.extend(["", "## ACT NOW"])
    if action_items:
        lines.extend(f"- {item}" for item in action_items)
    else:
        lines.append("- None captured.")
    lines.extend(["", "## Capture Metadata", f"- capture_id: {capture_id}", f"- schema: {AUTO_CAPTURE_SCHEMA_VERSION}"])
    if source_session_id:
        lines.append(f"- source_session_id: {source_session_id}")
    lines.append("")
    return "\n".join(lines)


def write_auto_capture_file(personas_dir: Path, plan: dict) -> dict:
    """Write a planned session capture under the persona memory folder."""
    if not plan.get("ok"):
        return plan
    if plan.get("blocking_findings"):
        return {
            "ok": False,
            "error": "auto-capture content failed safety scan",
            "blocking_findings": plan["blocking_findings"],
        }
    persona_root = resolve_persona_root(personas_dir, str(plan["persona"]))
    if persona_root is None:
        return {"ok": False, "error": "persona root not found", "persona": plan["persona"]}

    relative_path = Path(str(plan["relative_path"]))
    target = persona_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stem = target.stem
        suffix = target.suffix
        target = target.with_name(f"{stem}-{uuid.uuid4().hex[:8]}{suffix}")
        relative_path = Path("memory/episodes") / target.name
    target.write_text(str(plan["body"]), encoding="utf-8", newline="\n")
    return {
        "ok": True,
        "path": str(target),
        "relative_path": str(relative_path).replace("\\", "/"),
        "persona_root": str(persona_root),
    }
