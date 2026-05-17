from pathlib import Path

from chimera_memory.memory_legacy_migration import memory_legacy_migration_plan


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_legacy_migration_plan_is_read_only_and_classifies_risk(tmp_path: Path) -> None:
    personas_dir = tmp_path / "personas"
    asa = personas_dir / "developer" / "asa"
    procedural = asa / "memory" / "procedural" / "oauth.md"
    episode = asa / "memory" / "episodes" / "quiet-night.md"
    structured = asa / "memory" / "procedural" / "structured.md"

    _write(
        procedural,
        "---\ntype: procedural\nimportance: 9\n---\nOAuth token handling rule.\n",
    )
    _write(
        episode,
        "---\ntype: episode\nimportance: 4\n---\nSmall ordinary episode.\n",
    )
    _write(
        structured,
        "---\ntype: procedural\nmemory_payload:\n  lessons:\n    - already structured\n---\nBody stays.\n",
    )
    before = {path: path.read_text(encoding="utf-8") for path in (procedural, episode, structured)}

    result = memory_legacy_migration_plan(personas_dir, persona="asa")

    assert result["ok"] is True
    assert result["total_files"] == 3
    assert result["counts_by_mode"]["manual_frontmatter_retrofit"] == 1
    assert result["counts_by_mode"]["llm_draft_then_review"] == 1
    assert result["counts_by_mode"]["skip"] == 1

    by_path = {item["relative_path"]: item for item in result["files"]}
    assert by_path["memory/procedural/oauth.md"]["risk"] == "high"
    assert "security_or_credential_language" in by_path["memory/procedural/oauth.md"]["reasons"]
    assert by_path["memory/episodes/quiet-night.md"]["migration_mode"] == "llm_draft_then_review"
    assert by_path["memory/procedural/structured.md"]["migration_mode"] == "skip"

    after = {path: path.read_text(encoding="utf-8") for path in (procedural, episode, structured)}
    assert after == before


def test_legacy_migration_plan_scans_all_personas_and_truncates(tmp_path: Path) -> None:
    personas_dir = tmp_path / "personas"
    _write(
        personas_dir / "developer" / "asa" / "memory" / "episodes" / "one.md",
        "---\ntype: episode\nimportance: 3\n---\nOne.\n",
    )
    _write(
        personas_dir / "researcher" / "sarah" / "memory" / "reflections" / "two.md",
        "---\ntype: reflection\nimportance: 6\n---\nTwo.\n",
    )

    result = memory_legacy_migration_plan(personas_dir, limit=1)

    assert result["personas_scanned"] == 2
    assert result["total_files"] == 2
    assert result["returned_files"] == 1
    assert result["truncated"] is True
    assert result["counts_by_persona"] == {"asa": 1, "sarah": 1}
