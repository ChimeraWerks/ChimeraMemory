"""Read-only legacy memory migration planning."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .memory_auto_capture import resolve_persona_root
from .memory_frontmatter import parse_frontmatter

LEGACY_MIGRATION_PLAN_SCHEMA_VERSION = "chimera-memory.legacy-migration-plan.v1"
LEGACY_FRONTMATTER_RETROFIT_SCHEMA_VERSION = "chimera-memory.legacy-frontmatter-retrofit.v1"

_SECURITY_RE = re.compile(
    r"\b(auth|oauth|credential|credentials|token|secret|password|api[-_ ]?key|"
    r"bearer|webhook|refresh[-_ ]?token|private[-_ ]?key)\b",
    re.IGNORECASE,
)
_MANUAL_TYPES = {"procedural", "entity", "social", "semantic"}
_MEDIUM_MANUAL_TYPES = {"reflection", "feedback", "scoping"}
_DRAFTABLE_TYPES = {"episode", "episodic", "reading-notes"}


class _NoAliasSafeDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: object) -> bool:
        return True


def memory_legacy_migration_plan(
    personas_dir: str | Path,
    *,
    persona: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Plan legacy memory migration without writing or rewriting files."""
    root = Path(personas_dir).expanduser()
    persona_roots = _select_persona_roots(root, persona)
    files: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    risk_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    persona_counts: Counter[str] = Counter()

    max_items = max(0, int(limit))
    total_seen = 0
    for persona_name, persona_root in persona_roots:
        memory_root = persona_root / "memory"
        if not memory_root.exists():
            continue
        for path in sorted(memory_root.rglob("*.md"), key=lambda item: str(item).lower()):
            total_seen += 1
            item = _plan_file(persona_name, persona_root, path)
            counts[item["migration_mode"]] += 1
            risk_counts[item["risk"]] += 1
            type_counts[item["memory_type"]] += 1
            persona_counts[persona_name] += 1
            if len(files) < max_items:
                files.append(item)

    return {
        "ok": True,
        "schema_version": LEGACY_MIGRATION_PLAN_SCHEMA_VERSION,
        "personas_dir": str(root),
        "persona_filter": persona or "",
        "personas_scanned": len(persona_roots),
        "total_files": total_seen,
        "returned_files": len(files),
        "truncated": total_seen > len(files),
        "counts_by_mode": dict(sorted(counts.items())),
        "counts_by_risk": dict(sorted(risk_counts.items())),
        "counts_by_type": dict(sorted(type_counts.items())),
        "counts_by_persona": dict(sorted(persona_counts.items())),
        "files": files,
        "recommendation": (
            "Run inventory first. Prefer body-preserving frontmatter augmentation for manual "
            "retrofits; reject bulk LLM rewrite."
        ),
    }


def memory_legacy_frontmatter_retrofit(
    personas_dir: str | Path,
    *,
    persona: str,
    relative_path: str,
    memory_payload: Mapping[str, Any],
    write: bool = False,
    overwrite_payload: bool = False,
    actor: str = "agent",
    migrated_at: str | None = None,
) -> dict[str, Any]:
    """Preview or write an Option-B body-preserving frontmatter retrofit."""
    persona = str(persona or "").strip()
    if not persona:
        return {"ok": False, "error": "persona required"}
    if not isinstance(memory_payload, Mapping) or not memory_payload:
        return {"ok": False, "error": "memory_payload must be a non-empty mapping"}

    persona_root = resolve_persona_root(Path(personas_dir).expanduser(), persona)
    if persona_root is None:
        return {"ok": False, "error": "persona root not found", "persona": persona}

    relative_text = str(relative_path or "").replace("\\", "/").lstrip("/")
    if not relative_text:
        return {"ok": False, "error": "relative_path required"}
    relative = Path(relative_text)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        return {"ok": False, "error": "relative_path escapes persona root", "relative_path": relative_text}

    root = persona_root.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return {"ok": False, "error": "relative_path escapes persona root", "relative_path": relative_text}
    if target.suffix.lower() != ".md":
        return {"ok": False, "error": "legacy retrofit target must be a markdown file", "relative_path": relative_text}
    if not target.exists():
        return {"ok": False, "error": "legacy memory file not found", "relative_path": relative_text}

    original = target.read_text(encoding="utf-8")
    split = _split_frontmatter_preserving_body(original)
    if not split["ok"]:
        return {
            "ok": False,
            "error": split["error"],
            "relative_path": relative_text,
        }
    frontmatter = dict(split["frontmatter"])
    body = str(split["body"])
    if isinstance(frontmatter.get("memory_payload"), Mapping) and not overwrite_payload:
        return {
            "ok": False,
            "error": "memory_payload already exists",
            "relative_path": relative_text,
            "overwrite_required": True,
        }

    body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    frontmatter_sha256 = hashlib.sha256(str(split["frontmatter_text"]).encode("utf-8")).hexdigest()
    retrofitted = _retrofit_frontmatter(
        frontmatter,
        memory_payload=memory_payload,
        persona=persona,
        relative_path=relative_text,
        body_sha256=body_sha256,
        frontmatter_sha256=frontmatter_sha256,
        actor=actor,
        migrated_at=migrated_at or _utc_now(),
    )
    updated = _render_frontmatter_markdown(retrofitted, body)
    verify = _split_frontmatter_preserving_body(updated)
    if not verify["ok"]:
        return {"ok": False, "error": "rendered retrofit frontmatter is invalid", "relative_path": relative_text}
    body_sha256_after = hashlib.sha256(str(verify["body"]).encode("utf-8")).hexdigest()
    body_preserved = body_sha256_after == body_sha256 and str(verify["body"]) == body
    if not body_preserved:
        return {
            "ok": False,
            "error": "body preservation guard failed",
            "relative_path": relative_text,
            "body_sha256_before": body_sha256,
            "body_sha256_after": body_sha256_after,
        }

    result: dict[str, Any] = {
        "ok": True,
        "schema_version": LEGACY_FRONTMATTER_RETROFIT_SCHEMA_VERSION,
        "persona": persona,
        "relative_path": relative_text,
        "path": str(target),
        "written": False,
        "body_preserved": True,
        "body_sha256": body_sha256,
        "content_sha256_before": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "content_sha256_after": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
        "frontmatter_keys_added": sorted(set(retrofitted) - set(frontmatter)),
        "frontmatter_keys_updated": sorted(
            key for key in set(retrofitted).intersection(frontmatter) if retrofitted[key] != frontmatter[key]
        ),
        "review_status": retrofitted.get("review_status"),
        "provenance_status": retrofitted.get("provenance_status"),
        "legacy_migration": retrofitted.get("legacy_migration", {}),
    }
    if not write:
        result["preview_frontmatter"] = retrofitted
        return result

    target.write_text(updated, encoding="utf-8", newline="\n")
    result["written"] = True
    return result


def _select_persona_roots(root: Path, persona: str | None) -> list[tuple[str, Path]]:
    selected = str(persona or "").strip()
    if selected:
        persona_root = resolve_persona_root(root, selected)
        return [(selected, persona_root)] if persona_root is not None else []

    roots: list[tuple[str, Path]] = []
    if (root / "memory").is_dir():
        roots.append((root.name, root))
        return roots

    if not root.exists():
        return []
    for category in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not category.is_dir():
            continue
        if (category / "memory").is_dir():
            roots.append((category.name, category))
            continue
        for candidate in sorted(category.iterdir(), key=lambda item: item.name.lower()):
            if candidate.is_dir() and (candidate / "memory").is_dir():
                roots.append((candidate.name, candidate))
    return roots


def _split_frontmatter_preserving_body(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {"ok": True, "frontmatter": {}, "frontmatter_text": "", "body": text, "had_frontmatter": False}

    newline = "\r\n" if text.startswith("---\r\n") else "\n"
    if not text.startswith(f"---{newline}"):
        return {"ok": True, "frontmatter": {}, "frontmatter_text": "", "body": text, "had_frontmatter": False}

    marker = f"{newline}---"
    end = text.find(marker, len(f"---{newline}"))
    if end == -1:
        return {"ok": False, "error": "frontmatter closing marker not found"}
    frontmatter_text = text[len(f"---{newline}") : end]
    body_start = end + len(marker)
    if text.startswith(newline, body_start):
        body_start += len(newline)
    body = text[body_start:]
    try:
        parsed = yaml.safe_load(frontmatter_text.strip()) or {}
    except yaml.YAMLError as exc:
        return {"ok": False, "error": f"frontmatter yaml invalid: {exc.__class__.__name__}"}
    if not isinstance(parsed, Mapping):
        return {"ok": False, "error": "frontmatter must be a mapping"}
    return {
        "ok": True,
        "frontmatter": dict(parsed),
        "frontmatter_text": frontmatter_text,
        "body": body,
        "had_frontmatter": True,
    }


def _retrofit_frontmatter(
    frontmatter: Mapping[str, Any],
    *,
    memory_payload: Mapping[str, Any],
    persona: str,
    relative_path: str,
    body_sha256: str,
    frontmatter_sha256: str,
    actor: str,
    migrated_at: str,
) -> dict[str, Any]:
    retrofitted = dict(frontmatter)
    retrofitted.setdefault("provenance_status", "observed")
    retrofitted.setdefault("lifecycle_status", str(retrofitted.get("status") or "active"))
    retrofitted["review_status"] = "pending"
    retrofitted.setdefault("can_use_as_instruction", False)
    retrofitted.setdefault("can_use_as_evidence", True)
    retrofitted["requires_user_confirmation"] = True
    retrofitted["legacy_migration"] = {
        "schema_version": LEGACY_FRONTMATTER_RETROFIT_SCHEMA_VERSION,
        "mode": "body_preserving_frontmatter_retrofit",
        "persona": persona,
        "relative_path": relative_path,
        "body_sha256": body_sha256,
        "frontmatter_sha256_before": frontmatter_sha256,
        "migrated_at": migrated_at,
        "migrated_by": actor,
        "payload_review_status": "pending",
    }
    retrofitted["memory_payload"] = dict(memory_payload)
    return retrofitted


def _render_frontmatter_markdown(frontmatter: Mapping[str, Any], body: str) -> str:
    dumped = yaml.dump(
        dict(frontmatter),
        Dumper=_NoAliasSafeDumper,
        sort_keys=False,
        allow_unicode=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{dumped}\n---\n{body}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _plan_file(persona: str, persona_root: Path, path: Path) -> dict[str, Any]:
    content = path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(content)
    relative_path = path.relative_to(persona_root).as_posix()
    memory_type = _clean_type(frontmatter.get("type"))
    importance = _clean_importance(frontmatter.get("importance"))
    has_payload = isinstance(frontmatter.get("memory_payload"), dict)
    reasons: list[str] = []

    if has_payload:
        mode = "skip"
        risk = "low"
        reasons.append("already_structured")
    else:
        mode, risk = _migration_shape(memory_type, importance, content, body, reasons)

    return {
        "persona": persona,
        "relative_path": relative_path,
        "memory_type": memory_type,
        "importance": importance,
        "migration_mode": mode,
        "risk": risk,
        "reasons": reasons,
        "has_memory_payload": has_payload,
        "has_review_status": "review_status" in frontmatter,
        "has_exclude_from_default_search": "exclude_from_default_search" in frontmatter,
        "body_bytes": len(body.encode("utf-8")),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def _migration_shape(
    memory_type: str,
    importance: int | None,
    content: str,
    body: str,
    reasons: list[str],
) -> tuple[str, str]:
    if not memory_type or memory_type == "missing":
        reasons.append("missing_type")
        return "manual_frontmatter_retrofit", "medium"
    if _SECURITY_RE.search(content):
        reasons.append("security_or_credential_language")
        return "manual_frontmatter_retrofit", "high"
    if memory_type in _MANUAL_TYPES:
        reasons.append(f"{memory_type}_requires_authored_projection")
        return "manual_frontmatter_retrofit", "high"
    if importance is not None and importance >= 8:
        reasons.append("high_importance")
        return "manual_frontmatter_retrofit", "high"
    if len(body) > 6000:
        reasons.append("large_body")
        return "manual_frontmatter_retrofit", "medium"
    if memory_type in _MEDIUM_MANUAL_TYPES:
        reasons.append(f"{memory_type}_needs_persona_judgment")
        return "manual_frontmatter_retrofit", "medium"
    if memory_type in _DRAFTABLE_TYPES:
        reasons.append(f"{memory_type}_draftable_with_review")
        return "llm_draft_then_review", "medium"
    reasons.append("unknown_type_manual_default")
    return "manual_frontmatter_retrofit", "medium"


def _clean_type(value: object) -> str:
    text = str(value or "").strip().strip('"').strip("'").lower()
    return text or "missing"


def _clean_importance(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
