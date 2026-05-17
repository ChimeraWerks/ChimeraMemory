"""Persona-facing writer for structured authored memory payloads."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .memory_auto_capture import resolve_persona_root
from .memory_enhancement import (
    AUTHORED_WRITEBACK_SCHEMA_VERSION,
    build_authored_memory_enrichment_request,
    normalize_authored_memory_writeback,
)
from .sanitizer import scan_for_injection

AUTHORED_MEMORY_WRITE_SCHEMA_VERSION = "chimera-memory.authored-memory-write.v1"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MEMORY_TYPE_DIRS = {
    "procedural": "memory/procedural",
    "episodic": "memory/episodes",
    "episode": "memory/episodes",
    "semantic": "memory/semantic",
    "entity": "memory/entities",
    "reflection": "memory/reflections",
    "social": "memory/social",
    "decision": "memory/procedural",
    "lesson": "memory/procedural",
    "constraint": "memory/procedural",
    "failure": "memory/episodes",
    "artifact_reference": "memory/procedural",
    "work_log": "memory/episodes",
    "output": "memory/episodes",
    "open_question": "memory/procedural",
}
_STRUCTURED_FIELDS = (
    "decisions",
    "outputs",
    "lessons",
    "constraints",
    "unresolved_questions",
    "next_steps",
    "failures",
    "artifacts",
)


class _NoAliasSafeDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: object) -> bool:
        return True


def load_authored_memory_payload(path: str | Path) -> dict[str, Any]:
    """Load a structured authored memory payload from YAML or JSON-compatible YAML."""
    try:
        parsed = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("authored memory payload file is invalid") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError("authored memory payload must be a mapping")
    return dict(parsed)


def build_authored_memory_write_plan(
    *,
    payload: Mapping[str, Any],
    persona: str,
    relative_path: str = "",
) -> dict[str, Any]:
    """Build a write plan for a structured authored memory file."""
    persona = str(persona or "").strip()
    if not persona:
        return {"ok": False, "error": "persona required"}
    try:
        request = build_authored_memory_enrichment_request(
            memory_payload=payload,
            persona=persona,
            source_ref=relative_path,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    normalized = normalize_authored_memory_writeback(request)
    memory_payload = request["memory_payload"]
    memory_type = str(normalized.get("memory_type") or "procedural")
    memory_id = _slugify(memory_payload.get("memory_id") or normalized.get("summary") or "authored-memory")
    target_relative_path = _relative_path_for(memory_type, memory_id, relative_path)
    frontmatter = _frontmatter_from_payload(
        persona=persona,
        memory_id=memory_id,
        memory_payload=memory_payload,
        normalized=normalized,
    )
    body = render_authored_memory_markdown(
        title=str(frontmatter.get("about") or memory_id),
        memory_payload=memory_payload,
        source_refs=normalized.get("source_refs") if isinstance(normalized.get("source_refs"), list) else [],
    )
    content = _render_frontmatter_markdown(frontmatter, body)
    guard_findings = _safe_findings(content)
    blocking_findings = list(guard_findings)
    return {
        "ok": True,
        "schema_version": AUTHORED_MEMORY_WRITE_SCHEMA_VERSION,
        "persona": persona,
        "relative_path": target_relative_path,
        "frontmatter": frontmatter,
        "memory_payload": memory_payload,
        "source_refs": normalized.get("source_refs", []),
        "models_used": normalized.get("models_used", []),
        "retention": normalized.get("retention", {}),
        "request_payload": request,
        "guard_findings": guard_findings,
        "blocking_findings": blocking_findings,
        "body": content,
    }


def write_authored_memory_file(
    personas_dir: Path,
    plan: Mapping[str, Any],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write a planned structured memory file under the persona memory folder."""
    if not plan.get("ok"):
        return dict(plan)
    if plan.get("blocking_findings"):
        return {
            "ok": False,
            "error": "authored memory content failed safety scan",
            "blocking_findings": plan["blocking_findings"],
        }
    persona_root = resolve_persona_root(personas_dir, str(plan.get("persona") or ""))
    if persona_root is None:
        return {"ok": False, "error": "persona root not found", "persona": plan.get("persona")}

    relative_text = str(plan["relative_path"]).replace("\\", "/").lstrip("/")
    relative_path = Path(relative_text)
    if relative_path.is_absolute() or any(part == ".." for part in relative_path.parts):
        return {
            "ok": False,
            "error": "authored memory relative path escapes persona root",
            "relative_path": relative_text,
        }
    root = persona_root.resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return {
            "ok": False,
            "error": "authored memory relative path escapes persona root",
            "relative_path": relative_text,
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        return {
            "ok": False,
            "error": "authored memory file already exists",
            "relative_path": str(relative_path).replace("\\", "/"),
        }
    target.write_text(str(plan["body"]), encoding="utf-8", newline="\n")
    return {
        "ok": True,
        "path": str(target),
        "relative_path": str(relative_path).replace("\\", "/"),
        "persona_root": str(persona_root),
    }


def render_authored_memory_markdown(
    *,
    title: str,
    memory_payload: Mapping[str, Any],
    source_refs: list[Mapping[str, Any]],
) -> str:
    """Render a human-readable body for a structured authored memory."""
    lines = [f"# {title}", "", "## Structured Payload"]
    for field in _STRUCTURED_FIELDS:
        values = memory_payload.get(field)
        if not values:
            continue
        lines.extend(["", f"### {field.replace('_', ' ').title()}"])
        if isinstance(values, list):
            for item in values:
                lines.append(f"- {_display_item(item)}")
        else:
            lines.append(f"- {_display_item(values)}")

    entities = memory_payload.get("entities")
    if isinstance(entities, Mapping) and entities:
        lines.extend(["", "### Entities"])
        for field, values in entities.items():
            if not values:
                continue
            joined = ", ".join(str(value) for value in values)
            lines.append(f"- {field}: {joined}")

    if source_refs:
        lines.extend(["", "## Source References"])
        for ref in source_refs:
            lines.append(f"- {_display_item(ref)}")

    body = str(memory_payload.get("body") or "").strip()
    if body:
        lines.extend(["", "## Body", "", body])

    lines.extend(["", "## Writeback Metadata", f"- schema: {AUTHORED_MEMORY_WRITE_SCHEMA_VERSION}"])
    return "\n".join(lines).rstrip() + "\n"


def _frontmatter_from_payload(
    *,
    persona: str,
    memory_id: str,
    memory_payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
) -> dict[str, Any]:
    provenance_status = str(normalized.get("provenance_status") or "generated")
    review_status = str(normalized.get("review_status") or "pending")
    can_use_as_instruction = bool(normalized.get("can_use_as_instruction"))
    source_refs = normalized.get("source_refs") if isinstance(normalized.get("source_refs"), list) else []
    models_used = normalized.get("models_used") if isinstance(normalized.get("models_used"), list) else []
    retention = normalized.get("retention") if isinstance(normalized.get("retention"), Mapping) else {}
    frontmatter: dict[str, Any] = {
        "type": normalized.get("memory_type") or memory_payload.get("memory_type") or "procedural",
        "importance": _importance(memory_payload.get("importance")),
        "created": str(memory_payload.get("created") or ""),
        "last_accessed": str(memory_payload.get("last_accessed") or ""),
        "status": str(memory_payload.get("status") or "active"),
        "about": normalized.get("summary") or memory_id,
        "tags": _tags_from_topics(normalized.get("topics")),
        "provenance_status": provenance_status,
        "confidence": normalized.get("confidence"),
        "lifecycle_status": "active",
        "review_status": review_status,
        "sensitivity_tier": normalized.get("sensitivity_tier") or "standard",
        "can_use_as_instruction": can_use_as_instruction,
        "can_use_as_evidence": bool(normalized.get("can_use_as_evidence", True)),
        "requires_user_confirmation": bool(normalized.get("requires_user_confirmation", True)),
        "structured_write_schema_version": AUTHORED_MEMORY_WRITE_SCHEMA_VERSION,
        "authored_writeback_schema_version": AUTHORED_WRITEBACK_SCHEMA_VERSION,
        "payload_schema_version": memory_payload.get("payload_schema_version") or "",
        "memory_id": memory_id,
        "author": memory_payload.get("author") or persona,
        "source_refs": source_refs,
        "models_used": models_used,
        "retention": dict(retention),
        "memory_payload": dict(memory_payload),
        "enrichment": {
            "entities": [],
            "topics": [],
            "sensitivity_tier": normalized.get("sensitivity_tier") or "standard",
            "enriched_at": None,
            "enriched_by": None,
            "review_status": "pending",
        },
    }
    return {key: value for key, value in frontmatter.items() if value not in ("", None)}


def _render_frontmatter_markdown(frontmatter: Mapping[str, Any], body: str) -> str:
    dumped = yaml.dump(
        dict(frontmatter),
        Dumper=_NoAliasSafeDumper,
        sort_keys=False,
        allow_unicode=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{dumped}\n---\n\n{body}"


def _safe_findings(content: str) -> list[dict[str, Any]]:
    return [
        {
            "type": finding.get("type", "unknown"),
            "match_count": finding.get("match_count", 1),
        }
        for finding in scan_for_injection(content)
    ]


def _relative_path_for(memory_type: str, memory_id: str, explicit: str) -> str:
    if explicit:
        return explicit.replace("\\", "/").lstrip("/")
    directory = _MEMORY_TYPE_DIRS.get(memory_type, "memory/procedural")
    return f"{directory}/{memory_id}.md"


def _slugify(value: Any, fallback: str = "authored-memory") -> str:
    text = _SLUG_RE.sub("-", str(value or "").lower()).strip("-")
    return (text or fallback)[:80].strip("-") or fallback


def _importance(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 6
    return max(1, min(10, parsed))


def _tags_from_topics(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["structured-writeback"]
    tags = ["structured-writeback"]
    seen = set(tags)
    for item in value:
        tag = str(item or "").strip()
        if not tag or tag in seen:
            continue
        tags.append(tag)
        seen.add(tag)
    return tags


def _display_item(value: Any) -> str:
    if isinstance(value, Mapping):
        parts = [f"{key}: {item}" for key, item in value.items() if item not in ("", None, [], {})]
        return "; ".join(parts)
    return str(value)
