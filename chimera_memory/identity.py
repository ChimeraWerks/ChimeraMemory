"""Persona identity metadata for Chimera Memory runtimes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PersonaIdentity:
    persona: str | None
    persona_id: str | None
    persona_name: str | None
    persona_root: Path | None
    personas_dir: Path | None
    shared_root: Path | None
    client: str | None

    @property
    def display_name(self) -> str:
        return self.persona_name or self.persona or "unscoped"

    def warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.persona and self.persona_name and self.persona != self.persona_name:
            warnings.append("TRANSCRIPT_PERSONA differs from CHIMERA_PERSONA_NAME")
        if self.persona_id and "/" not in self.persona_id:
            warnings.append("CHIMERA_PERSONA_ID should use role/name shape")
        if self.persona_root and not self.persona_root.exists():
            warnings.append("CHIMERA_PERSONA_ROOT does not exist")
        if self.personas_dir and not self.personas_dir.exists():
            warnings.append("CHIMERA_PERSONAS_DIR does not exist")
        if self.shared_root and not self.shared_root.exists():
            warnings.append("CHIMERA_SHARED_ROOT does not exist")
        return warnings


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def load_identity_from_env() -> PersonaIdentity:
    """Read non-secret persona identity metadata from environment variables."""
    return PersonaIdentity(
        persona=os.environ.get("TRANSCRIPT_PERSONA", "").strip() or None,
        persona_id=os.environ.get("CHIMERA_PERSONA_ID", "").strip() or None,
        persona_name=os.environ.get("CHIMERA_PERSONA_NAME", "").strip() or None,
        persona_root=_env_path("CHIMERA_PERSONA_ROOT"),
        personas_dir=_env_path("CHIMERA_PERSONAS_DIR"),
        shared_root=_env_path("CHIMERA_SHARED_ROOT"),
        client=os.environ.get("CHIMERA_CLIENT", "").strip() or None,
    )
