"""Read-only legacy memory migration planning."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .memory_auto_capture import resolve_persona_root
from .memory_frontmatter import parse_frontmatter

LEGACY_MIGRATION_PLAN_SCHEMA_VERSION = "chimera-memory.legacy-migration-plan.v1"

_SECURITY_RE = re.compile(
    r"\b(auth|oauth|credential|credentials|token|secret|password|api[-_ ]?key|"
    r"bearer|webhook|refresh[-_ ]?token|private[-_ ]?key)\b",
    re.IGNORECASE,
)
_MANUAL_TYPES = {"procedural", "entity", "social", "semantic"}
_MEDIUM_MANUAL_TYPES = {"reflection", "feedback", "scoping"}
_DRAFTABLE_TYPES = {"episode", "episodic", "reading-notes"}


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
