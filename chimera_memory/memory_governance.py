"""Governance metadata normalization for curated memory files."""

from __future__ import annotations


PROVENANCE_STATUSES = {
    "observed", "inferred", "user_confirmed", "imported",
    "generated", "superseded", "disputed",
}
LIFECYCLE_STATUSES = {"active", "stale", "archived", "superseded", "disputed", "rejected"}
REVIEW_STATUSES = {
    "pending", "confirmed", "evidence_only", "restricted",
    "rejected", "stale", "merged", "superseded", "disputed",
}
SENSITIVITY_TIERS = {"standard", "restricted", "unknown"}
INSTRUCTION_GRADE_PROVENANCE = {"user_confirmed", "imported"}


def _choice(value: object, allowed: set[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, parsed))


def _bool_int(value: object, default: bool) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if value is None:
        return 1 if default else 0
    if isinstance(value, (int, float)):
        return 1 if value else 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return 1
    if text in {"0", "false", "no", "n", "off"}:
        return 0
    return 1 if default else 0


def governance_from_frontmatter(fm: dict) -> dict:
    """Normalize OB1-inspired governance metadata from YAML frontmatter."""
    provenance = _choice(fm.get("provenance_status"), PROVENANCE_STATUSES, "imported")
    lifecycle = _choice(
        fm.get("lifecycle_status"),
        LIFECYCLE_STATUSES,
        _choice(fm.get("status"), LIFECYCLE_STATUSES, "active"),
    )
    review_default = "confirmed" if provenance in INSTRUCTION_GRADE_PROVENANCE else "pending"
    review = _choice(fm.get("review_status"), REVIEW_STATUSES, review_default)
    sensitivity = _choice(fm.get("sensitivity_tier"), SENSITIVITY_TIERS, "standard")

    instruction_default = provenance in INSTRUCTION_GRADE_PROVENANCE
    can_use_as_instruction = _bool_int(fm.get("can_use_as_instruction"), instruction_default)
    if provenance not in INSTRUCTION_GRADE_PROVENANCE:
        can_use_as_instruction = 0

    requires_default = provenance not in INSTRUCTION_GRADE_PROVENANCE
    return {
        "provenance_status": provenance,
        "confidence": _optional_float(fm.get("confidence")),
        "lifecycle_status": lifecycle,
        "review_status": review,
        "sensitivity_tier": sensitivity,
        "can_use_as_instruction": can_use_as_instruction,
        "can_use_as_evidence": _bool_int(fm.get("can_use_as_evidence"), True),
        "requires_user_confirmation": _bool_int(
            fm.get("requires_user_confirmation"),
            requires_default,
        ),
    }
