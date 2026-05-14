"""Markdown frontmatter parsing helpers."""

from __future__ import annotations


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    try:
        import yaml

        fm = yaml.safe_load(text[3:end].strip()) or {}
    except Exception:
        fm = {}
    return fm, text[end + 4 :].strip()
