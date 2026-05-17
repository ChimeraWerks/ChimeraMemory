from pathlib import Path

from chimera_memory.memory_legacy_migration import (
    memory_legacy_frontmatter_review_action,
    memory_legacy_frontmatter_retrofit,
    memory_legacy_migration_plan,
)


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


def test_legacy_frontmatter_retrofit_previews_without_writing_and_preserves_body(tmp_path: Path) -> None:
    personas_dir = tmp_path / "personas"
    target = personas_dir / "researcher" / "sarah" / "memory" / "procedural" / "rule.md"
    body = "# Rule\n\nOriginal prose stays exactly.\n"
    original = "---\ntype: procedural\nimportance: 9\ntags:\n- old\n---" + body
    _write(target, original)

    result = memory_legacy_frontmatter_retrofit(
        personas_dir,
        persona="sarah",
        relative_path="memory/procedural/rule.md",
        memory_payload={
            "lessons": [{"teaching": "Preserve prose while adding structure."}],
            "constraints": [{"rule": "Do not rewrite the body."}],
        },
        migrated_at="2026-05-17T00:00:00Z",
    )

    assert result["ok"] is True
    assert result["written"] is False
    assert result["body_preserved"] is True
    assert result["review_status"] == "pending"
    assert target.read_text(encoding="utf-8") == original
    frontmatter = result["preview_frontmatter"]
    assert frontmatter["type"] == "procedural"
    assert frontmatter["importance"] == 9
    assert frontmatter["tags"] == ["old"]
    assert frontmatter["memory_payload"]["lessons"][0]["teaching"] == (
        "Preserve prose while adding structure."
    )
    assert frontmatter["legacy_migration"]["mode"] == "body_preserving_frontmatter_retrofit"


def test_legacy_frontmatter_retrofit_writes_and_keeps_body_hash(tmp_path: Path) -> None:
    personas_dir = tmp_path / "personas"
    target = personas_dir / "researcher" / "sarah" / "memory" / "episodes" / "moment.md"
    original_body = "Line one.\n\nLine two with spacing.\n"
    _write(target, "---\ntype: episode\nimportance: 4\n---" + original_body)

    result = memory_legacy_frontmatter_retrofit(
        personas_dir,
        persona="sarah",
        relative_path="memory/episodes/moment.md",
        memory_payload={"decisions": [{"decision": "Keep the body canonical."}]},
        write=True,
        actor="test",
        migrated_at="2026-05-17T00:00:00Z",
    )

    assert result["ok"] is True
    assert result["written"] is True
    updated = target.read_text(encoding="utf-8")
    assert updated.endswith(original_body)
    assert "memory_payload:" in updated
    assert "review_status: pending" in updated
    assert "migrated_by: test" in updated


def test_legacy_frontmatter_retrofit_rejects_escape_and_existing_payload(tmp_path: Path) -> None:
    personas_dir = tmp_path / "personas"
    target = personas_dir / "researcher" / "sarah" / "memory" / "procedural" / "structured.md"
    _write(
        target,
        "---\ntype: procedural\nmemory_payload:\n  lessons:\n  - old\n---\nBody.\n",
    )

    escaped = memory_legacy_frontmatter_retrofit(
        personas_dir,
        persona="sarah",
        relative_path="../outside.md",
        memory_payload={"lessons": [{"teaching": "Nope."}]},
    )
    assert escaped["ok"] is False
    assert "escapes" in escaped["error"]

    existing = memory_legacy_frontmatter_retrofit(
        personas_dir,
        persona="sarah",
        relative_path="memory/procedural/structured.md",
        memory_payload={"lessons": [{"teaching": "New."}]},
    )
    assert existing["ok"] is False
    assert existing["error"] == "memory_payload already exists"


def test_legacy_frontmatter_review_action_confirms_durably_and_preserves_body(tmp_path: Path) -> None:
    personas_dir = tmp_path / "personas"
    target = personas_dir / "researcher" / "sarah" / "memory" / "procedural" / "rule.md"
    original_body = "Rule body stays untouched.\n"
    _write(target, "---\ntype: procedural\nimportance: 9\n---" + original_body)
    migrated = memory_legacy_frontmatter_retrofit(
        personas_dir,
        persona="sarah",
        relative_path="memory/procedural/rule.md",
        memory_payload={"lessons": [{"teaching": "Review confirms instruction use."}]},
        write=True,
        migrated_at="2026-05-17T00:00:00Z",
    )
    assert migrated["ok"] is True

    preview = memory_legacy_frontmatter_review_action(
        personas_dir,
        persona="sarah",
        relative_path="memory/procedural/rule.md",
        action="confirm",
        reviewer="sarah",
        reviewed_at="2026-05-17T01:00:00Z",
    )
    assert preview["ok"] is True
    assert preview["written"] is False
    assert preview["body_preserved"] is True
    assert preview["after"]["review_status"] == "confirmed"
    assert preview["after"]["can_use_as_instruction"] is True

    written = memory_legacy_frontmatter_review_action(
        personas_dir,
        persona="sarah",
        relative_path="memory/procedural/rule.md",
        action="confirm",
        reviewer="sarah",
        notes="approved",
        write=True,
        reviewed_at="2026-05-17T01:00:00Z",
    )

    assert written["ok"] is True
    assert written["written"] is True
    updated = target.read_text(encoding="utf-8")
    assert updated.endswith(original_body)
    assert "review_status: confirmed" in updated
    assert "provenance_status: user_confirmed" in updated
    assert "can_use_as_instruction: true" in updated
    assert "requires_user_confirmation: false" in updated
    assert "payload_review_status: confirmed" in updated
    assert "reviewed_by: sarah" in updated


def test_legacy_frontmatter_review_action_rejects_unmigrated_file(tmp_path: Path) -> None:
    personas_dir = tmp_path / "personas"
    target = personas_dir / "researcher" / "sarah" / "memory" / "procedural" / "plain.md"
    _write(target, "---\ntype: procedural\nimportance: 9\n---\nBody.\n")

    result = memory_legacy_frontmatter_review_action(
        personas_dir,
        persona="sarah",
        relative_path="memory/procedural/plain.md",
        action="confirm",
    )

    assert result["ok"] is False
    assert result["error"] == "memory_payload required"
