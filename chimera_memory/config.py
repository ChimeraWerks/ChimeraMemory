"""Configuration management: auto-generated YAML config with commented defaults."""

import os
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".chimera-memory"
CONFIG_PATH = CONFIG_DIR / "config.yaml"

DEFAULT_CONFIG_TEMPLATE = """\
# ─── Chimera Memory Configuration ───
#
# This file controls how your session history is stored and searched.
# Everything below is set to its default value.
# Uncomment and change any line to customize.
#
# Location: ~/.chimera-memory/config.yaml
# Docs: https://github.com/ChimeraWerks/ChimeraMemory

# ─── Storage ─────────────────────────

# How many days of full transcripts to keep. After this,
# entries are compressed into permanent session summaries
# and the raw transcript is pruned. Set to 0 to keep
# everything forever (no compression, no pruning).
# retention_days: 90

# Maximum database size in megabytes. When exceeded,
# the oldest raw entries are compressed and pruned first.
# max_db_size_mb: 1024

# ─── What Gets Indexed ───────────────

# Index tool calls (Read, Bash, Write, etc.)
# Stores tool name and parameters, not full output.
# index_tool_calls: true

# Index tool results (output from tools).
# Usually very large and noisy. Most users leave this off.
# index_tool_results: false

# Index system messages (reminders, notifications).
# Low value for search. Most users leave this off.
# index_system: false

# ─── Search ──────────────────────────

# Return compact summaries first instead of full content.
# Saves tokens on large result sets. Use discord_detail()
# to fetch full content for specific entries.
# progressive_disclosure: true

# ─── Identity ────────────────────────

# Tag all entries with this name. Useful if multiple
# agents share one database.
# persona:

# Client transcript format. Supported values include claude and codex.
# client:

# ─── Session Files ───────────────────

# Where the client stores session files.
# Auto-detected from your working directory if not set.
# jsonl_dir:

# ─── Features ────────────────────────

# Detect conversation branches from Claude Code rewinds.
# When enabled, only the active branch is indexed by default.
# branch_detection: false
"""

# Mapping of config keys to their defaults
DEFAULTS = {
    "retention_days": 90,
    "max_db_size_mb": 1024,
    "index_tool_calls": True,
    "index_tool_results": False,
    "index_system": False,
    "progressive_disclosure": True,
    "persona": None,
    "client": None,
    "jsonl_dir": None,
    "branch_detection": False,
}


def ensure_config_exists():
    """Create the config file with defaults if it doesn't exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
        log.info("Created default config at %s", CONFIG_PATH)


def load_config() -> dict[str, Any]:
    """Load configuration with priority: env vars > config file > defaults.

    Returns a dict with all config values resolved.
    """
    config = dict(DEFAULTS)

    # Layer 1: Read config file (if exists)
    if CONFIG_PATH.exists():
        file_config = _parse_yaml_simple(CONFIG_PATH.read_text(encoding="utf-8"))
        for key, value in file_config.items():
            if key in DEFAULTS:
                config[key] = _cast_value(key, value)

    # Layer 2: Environment variables override everything
    env_map = {
        "TRANSCRIPT_RETENTION_DAYS": "retention_days",
        "TRANSCRIPT_MAX_DB_SIZE_MB": "max_db_size_mb",
        "TRANSCRIPT_INDEX_TOOL_CALLS": "index_tool_calls",
        "TRANSCRIPT_INDEX_TOOL_RESULTS": "index_tool_results",
        "TRANSCRIPT_INDEX_SYSTEM": "index_system",
        "TRANSCRIPT_PROGRESSIVE_DISCLOSURE": "progressive_disclosure",
        "TRANSCRIPT_PERSONA": "persona",
        "CHIMERA_CLIENT": "client",
        "TRANSCRIPT_JSONL_DIR": "jsonl_dir",
        "TRANSCRIPT_BRANCH_DETECTION": "branch_detection",
    }

    for env_key, config_key in env_map.items():
        env_val = os.environ.get(env_key)
        if env_val is not None:
            config[config_key] = _cast_value(config_key, env_val)

    return config


def _parse_yaml_simple(text: str) -> dict:
    """Parse a simple YAML file (key: value pairs, no nesting).

    Handles comments, empty lines, and quoted strings.
    Avoids requiring PyYAML as a dependency.
    """
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Strip inline comments
        if " #" in value:
            value = value[:value.index(" #")].strip()
        # Strip quotes
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        if value:
            result[key] = value
    return result


def _cast_value(key: str, value: Any) -> Any:
    """Cast a config value to the appropriate type based on the key's default."""
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes", "on")
    elif isinstance(default, int):
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
    return value if value else default
