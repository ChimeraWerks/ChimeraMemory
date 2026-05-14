from __future__ import annotations

import json
import sys
from pathlib import Path

from chimera_memory.codex_setup import (
    format_codex_doctor_report,
    inspect_codex_mcp_config,
)


def _write_config(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _valid_codex_config(jsonl_dir: Path) -> dict:
    return {
        "mcpServers": {
            "chimera-memory": {
                "command": sys.executable,
                "args": ["serve"],
                "env": {
                    "TRANSCRIPT_JSONL_DIR": str(jsonl_dir),
                    "TRANSCRIPT_PERSONA": "asa",
                    "CHIMERA_CLIENT": "codex",
                    "CHIMERA_PERSONA_ID": "developer/asa",
                    "CHIMERA_PERSONA_NAME": "asa",
                    "CHIMERA_PERSONA_ROOT": "C:/Github/ChimeraAgency/personas/developer/asa",
                    "CHIMERA_PERSONAS_DIR": "C:/Github/ChimeraAgency/personas",
                    "CHIMERA_SHARED_ROOT": "C:/Github/ChimeraAgency/shared",
                },
            },
        },
    }


def test_codex_doctor_reports_valid_setup_without_env_values(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "sessions"
    jsonl_dir.mkdir()
    config_path = tmp_path / "mcp_servers.json"
    payload = _valid_codex_config(jsonl_dir)
    payload["mcpServers"]["chimera-memory"]["env"]["EXTRA_SECRET"] = "secret-token-value"
    _write_config(config_path, payload)

    report = inspect_codex_mcp_config(config_path)
    text = format_codex_doctor_report(report)
    serialized = json.dumps(report)

    assert report["status"] == "ok"
    assert report["server_configured"] is True
    assert "EXTRA_SECRET" in report["env_keys"]
    assert "secret-token-value" not in serialized
    assert "secret-token-value" not in text
    assert "TRANSCRIPT_JSONL_DIR exists." in text


def test_codex_doctor_reports_missing_config(tmp_path: Path) -> None:
    report = inspect_codex_mcp_config(tmp_path / "missing.json")

    assert report["status"] == "error"
    assert report["config_exists"] is False
    assert any(check["name"] == "config_exists" for check in report["checks"])


def test_codex_doctor_rejects_wrong_client_parser(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "sessions"
    jsonl_dir.mkdir()
    config_path = tmp_path / "mcp_servers.json"
    payload = _valid_codex_config(jsonl_dir)
    payload["mcpServers"]["chimera-memory"]["env"]["CHIMERA_CLIENT"] = "claude"
    _write_config(config_path, payload)

    report = inspect_codex_mcp_config(config_path)

    assert report["status"] == "error"
    assert any(
        check["name"] == "env:CHIMERA_CLIENT"
        and "must be codex" in check["message"]
        for check in report["checks"]
    )


def test_codex_doctor_warns_on_incomplete_identity(tmp_path: Path) -> None:
    jsonl_dir = tmp_path / "sessions"
    jsonl_dir.mkdir()
    config_path = tmp_path / "mcp_servers.json"
    payload = _valid_codex_config(jsonl_dir)
    for key in (
        "CHIMERA_PERSONA_ID",
        "CHIMERA_PERSONA_NAME",
        "CHIMERA_PERSONA_ROOT",
        "CHIMERA_PERSONAS_DIR",
        "CHIMERA_SHARED_ROOT",
    ):
        del payload["mcpServers"]["chimera-memory"]["env"][key]
    _write_config(config_path, payload)

    report = inspect_codex_mcp_config(config_path)
    text = format_codex_doctor_report(report)

    assert report["status"] == "warning"
    assert "Persona identity env is incomplete." in text
    assert "CHIMERA_PERSONA_ID" in text
