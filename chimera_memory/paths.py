"""Path helpers for Chimera Memory storage."""

from __future__ import annotations

import os
import re
from pathlib import Path


def persona_db_root() -> Path:
    override = os.environ.get("CHIMERA_MEMORY_PERSONA_DB_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".chimera-memory" / "personas"


def _safe_segment(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    clean = clean.strip(".-")
    return clean or "unknown"


def persona_transcript_db_path(
    persona_name: str,
    *,
    persona_id: str | None = None,
    root: Path | str | None = None,
) -> Path:
    """Return the default per-persona transcript DB path.

    `persona_id` uses the Chimera role/name shape, for example `developer/asa`.
    When unavailable, fall back to the persona name alone.
    """
    base = Path(root).expanduser() if root is not None else persona_db_root()
    if persona_id:
        parts = [_safe_segment(part) for part in persona_id.replace("\\", "/").split("/") if part.strip()]
    else:
        parts = [_safe_segment(persona_name)]
    if not parts:
        parts = [_safe_segment(persona_name)]
    return base.joinpath(*parts) / "transcript.db"

