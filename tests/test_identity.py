from __future__ import annotations

from pathlib import Path

from chimera_memory.identity import load_identity_from_env


def test_load_identity_from_env_reads_persona_metadata(tmp_path: Path, monkeypatch) -> None:
    persona_root = tmp_path / "personas" / "developer" / "asa"
    shared_root = tmp_path / "shared"
    persona_root.mkdir(parents=True)
    shared_root.mkdir()

    monkeypatch.setenv("TRANSCRIPT_PERSONA", "asa")
    monkeypatch.setenv("CHIMERA_PERSONA_ID", "developer/asa")
    monkeypatch.setenv("CHIMERA_PERSONA_NAME", "asa")
    monkeypatch.setenv("CHIMERA_PERSONA_ROOT", str(persona_root))
    monkeypatch.setenv("CHIMERA_PERSONAS_DIR", str(tmp_path / "personas"))
    monkeypatch.setenv("CHIMERA_SHARED_ROOT", str(shared_root))
    monkeypatch.setenv("CHIMERA_CLIENT", "codex")

    identity = load_identity_from_env()

    assert identity.persona == "asa"
    assert identity.persona_id == "developer/asa"
    assert identity.persona_name == "asa"
    assert identity.persona_root == persona_root
    assert identity.personas_dir == tmp_path / "personas"
    assert identity.shared_root == shared_root
    assert identity.client == "codex"
    assert identity.warnings() == []


def test_identity_warns_on_mismatched_persona(monkeypatch) -> None:
    monkeypatch.setenv("TRANSCRIPT_PERSONA", "asa")
    monkeypatch.setenv("CHIMERA_PERSONA_NAME", "sarah")
    monkeypatch.setenv("CHIMERA_PERSONA_ID", "sarah")

    warnings = load_identity_from_env().warnings()

    assert "TRANSCRIPT_PERSONA differs from CHIMERA_PERSONA_NAME" in warnings
    assert "CHIMERA_PERSONA_ID should use role/name shape" in warnings
