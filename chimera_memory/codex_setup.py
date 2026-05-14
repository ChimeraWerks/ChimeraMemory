"""Codex setup diagnostics for Chimera Memory."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any


CODEX_MCP_SERVER_NAMES = ("chimera-memory", "chimera_memory")
REQUIRED_CODEX_ENV = ("TRANSCRIPT_JSONL_DIR", "TRANSCRIPT_PERSONA", "CHIMERA_CLIENT")
IDENTITY_ENV = (
    "CHIMERA_PERSONA_ID",
    "CHIMERA_PERSONA_NAME",
    "CHIMERA_PERSONA_ROOT",
    "CHIMERA_PERSONAS_DIR",
    "CHIMERA_SHARED_ROOT",
)


def default_codex_mcp_config_path() -> Path:
    return Path.home() / ".codex" / "mcp_servers.json"


def build_codex_mcp_config(
    *,
    persona: str,
    jsonl_dir: str = "~/.codex/sessions/",
    command: str = "chimera-memory",
    server_name: str = "chimera-memory",
    persona_id: str = "",
    persona_name: str = "",
    persona_root: str = "",
    personas_dir: str = "",
    shared_root: str = "",
) -> dict[str, Any]:
    """Build a safe Codex MCP config template.

    The template only contains paths and non-secret identity fields. It never
    reads the user's current config and never emits raw credentials.
    """
    persona = persona.strip()
    if not persona:
        raise ValueError("persona is required")
    command = command.strip()
    if not command:
        raise ValueError("command is required")
    server_name = server_name.strip()
    if not server_name:
        raise ValueError("server_name is required")

    env: dict[str, str] = {
        "TRANSCRIPT_JSONL_DIR": jsonl_dir.strip() or "~/.codex/sessions/",
        "TRANSCRIPT_PERSONA": persona,
        "CHIMERA_CLIENT": "codex",
    }
    optional_env = {
        "CHIMERA_PERSONA_ID": persona_id,
        "CHIMERA_PERSONA_NAME": persona_name,
        "CHIMERA_PERSONA_ROOT": persona_root,
        "CHIMERA_PERSONAS_DIR": personas_dir,
        "CHIMERA_SHARED_ROOT": shared_root,
    }
    for key, value in optional_env.items():
        cleaned = value.strip()
        if cleaned:
            env[key] = cleaned

    return {
        "mcpServers": {
            server_name: {
                "command": command,
                "args": ["serve"],
                "env": env,
            }
        }
    }


def inspect_codex_mcp_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Inspect Codex MCP config without exposing raw environment values."""
    path = Path(config_path).expanduser() if config_path is not None else default_codex_mcp_config_path()
    checks: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "config_path": str(path),
        "config_exists": path.is_file(),
        "parse_ok": False,
        "server_name": "",
        "server_configured": False,
        "env_keys": [],
        "checks": checks,
    }

    if not path.is_file():
        _check(checks, "config_exists", "error", "Codex MCP config file does not exist.")
        return _finalize(result)
    _check(checks, "config_exists", "ok", "Codex MCP config file exists.")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _check(checks, "parse_json", "error", f"Codex MCP config is not valid JSON: {exc.msg}.")
        return _finalize(result)
    if not isinstance(data, Mapping):
        _check(checks, "parse_json", "error", "Codex MCP config root must be an object.")
        return _finalize(result)
    result["parse_ok"] = True
    _check(checks, "parse_json", "ok", "Codex MCP config parses as JSON.")

    servers = data.get("mcpServers") or data.get("mcp_servers")
    if not isinstance(servers, Mapping):
        _check(checks, "mcp_servers", "error", "Config must contain an mcpServers object.")
        return _finalize(result)

    server_name = _configured_server_name(servers)
    if not server_name:
        _check(checks, "chimera_server", "error", "No chimera-memory MCP server entry found.")
        return _finalize(result)
    result["server_name"] = server_name
    result["server_configured"] = True
    _check(checks, "chimera_server", "ok", f"Found MCP server entry: {server_name}.")

    server = servers.get(server_name)
    if not isinstance(server, Mapping):
        _check(checks, "server_shape", "error", "chimera-memory server entry must be an object.")
        return _finalize(result)

    command = str(server.get("command") or "").strip()
    if not command:
        _check(checks, "command", "error", "Server command is missing.")
    elif _command_resolves(command):
        _check(checks, "command", "ok", f"Server command resolves: {Path(command).name}.")
    else:
        _check(checks, "command", "warning", f"Server command does not resolve on PATH: {Path(command).name}.")

    args = server.get("args")
    if isinstance(args, list) and "serve" in [str(item) for item in args]:
        _check(checks, "args", "ok", "Server args include serve.")
    else:
        _check(checks, "args", "warning", "Server args should include serve.")

    env = server.get("env")
    if not isinstance(env, Mapping):
        _check(checks, "env", "error", "Server env must be an object.")
        return _finalize(result)
    result["env_keys"] = sorted(str(key) for key in env.keys())
    _check(checks, "env", "ok", "Server env is present. Values are intentionally not reported.")

    for key in REQUIRED_CODEX_ENV:
        value = str(env.get(key) or "").strip()
        if not value:
            _check(checks, f"env:{key}", "error", f"{key} is required for Codex setup.")
        elif key == "CHIMERA_CLIENT" and value != "codex":
            _check(checks, f"env:{key}", "error", "CHIMERA_CLIENT must be codex for Codex transcripts.")
        elif key == "CHIMERA_CLIENT":
            _check(checks, f"env:{key}", "ok", "CHIMERA_CLIENT selects the Codex parser.")
        elif key == "TRANSCRIPT_JSONL_DIR":
            status = "ok" if _path_exists(value) else "warning"
            message = "TRANSCRIPT_JSONL_DIR exists." if status == "ok" else "TRANSCRIPT_JSONL_DIR does not exist yet."
            _check(checks, f"env:{key}", status, message)
        else:
            _check(checks, f"env:{key}", "ok", f"{key} is set.")

    missing_identity = [key for key in IDENTITY_ENV if not str(env.get(key) or "").strip()]
    if missing_identity:
        _check(
            checks,
            "identity_env",
            "warning",
            "Persona identity env is incomplete.",
            {"missing_keys": missing_identity},
        )
    else:
        _check(checks, "identity_env", "ok", "Persona identity env is complete.")

    return _finalize(result)


def format_codex_doctor_report(result: Mapping[str, Any]) -> str:
    """Render a human-readable report without raw env values."""
    status = str(result.get("status") or "unknown").upper()
    lines = [
        f"Codex ChimeraMemory setup: {status}",
        f"Config: {result.get('config_path')}",
        f"Server: {result.get('server_name') or 'not configured'}",
    ]
    env_keys = result.get("env_keys")
    if isinstance(env_keys, list) and env_keys:
        lines.append("Env keys: " + ", ".join(str(key) for key in env_keys))
    lines.append("")
    for check in result.get("checks", []):
        if not isinstance(check, Mapping):
            continue
        state = str(check.get("status") or "?").upper()
        lines.append(f"[{state}] {check.get('name')}: {check.get('message')}")
        details = check.get("details")
        if isinstance(details, Mapping) and details.get("missing_keys"):
            lines.append("  missing: " + ", ".join(str(key) for key in details["missing_keys"]))
    return "\n".join(lines)


def _configured_server_name(servers: Mapping[str, Any]) -> str:
    for name in CODEX_MCP_SERVER_NAMES:
        if name in servers:
            return name
    return ""


def _command_resolves(command: str) -> bool:
    expanded = Path(os.path.expandvars(os.path.expanduser(command)))
    if expanded.is_absolute() or expanded.parent != Path("."):
        return expanded.exists()
    return shutil.which(command) is not None


def _path_exists(value: str) -> bool:
    return Path(os.path.expandvars(os.path.expanduser(value))).exists()


def _check(
    checks: list[dict[str, Any]],
    name: str,
    status: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    item: dict[str, Any] = {"name": name, "status": status, "message": message}
    if details:
        item["details"] = dict(details)
    checks.append(item)


def _finalize(result: dict[str, Any]) -> dict[str, Any]:
    statuses = [str(check.get("status") or "") for check in result["checks"]]
    if "error" in statuses:
        result["status"] = "error"
    elif "warning" in statuses:
        result["status"] = "warning"
    else:
        result["status"] = "ok"
    return result
