import sqlite3
from pathlib import Path

from chimera_memory.memory import (
    index_file,
    init_memory_tables,
    memory_audit_query,
    memory_review_action,
    memory_review_pending,
)
from chimera_memory.memory_legacy_migration import memory_legacy_frontmatter_retrofit


def _index_generated_memory(conn: sqlite3.Connection, tmp_path: Path, name: str = "generated.md") -> None:
    memory_file = tmp_path / name
    memory_file.write_text(
        "\n".join(
            [
                "---",
                "type: procedural",
                "importance: 8",
                "about: generated review target",
                "provenance_status: generated",
                "confidence: 0.42",
                "---",
                "Generated memory that needs review.",
            ]
        ),
        encoding="utf-8",
    )
    assert index_file(conn, "asa", name, memory_file)


def test_generated_memory_appears_in_review_queue(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_generated_memory(conn, tmp_path)

    pending = memory_review_pending(conn, persona="asa")

    assert len(pending) == 1
    assert pending[0]["relative_path"] == "generated.md"
    assert pending[0]["provenance_status"] == "generated"
    assert pending[0]["review_status"] == "pending"
    assert pending[0]["requires_user_confirmation"] is True


def test_confirm_review_promotes_memory_and_audits_action(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_generated_memory(conn, tmp_path)

    result = memory_review_action(
        conn,
        file_path="generated.md",
        action="confirm",
        reviewer="charles",
        notes="confirmed from manual review",
    )

    assert result["ok"] is True
    assert result["action"] == "confirm"
    assert result["before"]["provenance_status"] == "generated"
    assert result["after"]["provenance_status"] == "user_confirmed"
    assert result["after"]["review_status"] == "confirmed"
    assert result["after"]["can_use_as_instruction"] is True
    assert result["after"]["requires_user_confirmation"] is False
    assert memory_review_pending(conn) == []

    review_row = conn.execute(
        """
        SELECT action, reviewer, before_metadata, after_metadata
        FROM memory_review_actions
        WHERE action_id = ?
        """,
        (result["action_id"],),
    ).fetchone()
    assert review_row[0] == "confirm"
    assert review_row[1] == "charles"
    assert "generated" in review_row[2]
    assert "user_confirmed" in review_row[3]

    events = memory_audit_query(conn, event_type="memory_confirmed", persona="asa")
    assert len(events) == 1
    assert events[0]["target_id"] == str(result["file_id"])
    assert events[0]["payload"]["action_id"] == result["action_id"]


def test_restrict_scope_keeps_memory_evidence_only_and_out_of_pending_queue(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_generated_memory(conn, tmp_path, "restricted.md")

    result = memory_review_action(
        conn,
        file_path="restricted.md",
        action="restrict_scope",
        reviewer="charles",
    )

    assert result["ok"] is True
    assert result["after"]["review_status"] == "restricted"
    assert result["after"]["sensitivity_tier"] == "restricted"
    assert result["after"]["can_use_as_instruction"] is False
    assert result["after"]["can_use_as_evidence"] is True
    assert result["after"]["requires_user_confirmation"] is False
    assert memory_review_pending(conn) == []

    events = memory_audit_query(conn, event_type="memory_restricted", persona="asa")
    assert len(events) == 1


def test_merge_review_action_matches_ob_lifecycle(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_generated_memory(conn, tmp_path, "merged.md")

    result = memory_review_action(
        conn,
        file_path="merged.md",
        action="merge",
        reviewer="charles",
        notes="merged into canonical memory",
    )

    assert result["ok"] is True
    assert result["after"]["lifecycle_status"] == "superseded"
    assert result["after"]["review_status"] == "merged"
    assert result["after"]["can_use_as_instruction"] is False
    assert result["after"]["requires_user_confirmation"] is False
    events = memory_audit_query(conn, event_type="memory_merged", persona="asa")
    assert len(events) == 1


def test_edit_review_action_keeps_memory_pending(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_generated_memory(conn, tmp_path, "edit.md")

    result = memory_review_action(conn, file_path="edit.md", action="edit", reviewer="charles")

    assert result["ok"] is True
    assert result["after"]["review_status"] == "pending"
    assert result["after"]["requires_user_confirmation"] is True
    assert result["after"]["can_use_as_instruction"] is False
    events = memory_audit_query(conn, event_type="memory_review_edit_requested", persona="asa")
    assert len(events) == 1


def test_review_action_reports_missing_file(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    result = memory_review_action(conn, file_path="missing.md", action="confirm")

    assert result == {
        "ok": False,
        "error": "memory file not found",
        "file_path": "missing.md",
    }


def test_review_action_routes_migrated_memory_through_frontmatter(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    personas_dir = tmp_path / "personas"
    persona_root = personas_dir / "researcher" / "sarah"
    memory_file = persona_root / "memory" / "procedural" / "migrated.md"
    body = "Durable migrated body.\n"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("---\ntype: procedural\nimportance: 9\n---" + body, encoding="utf-8")
    migrated = memory_legacy_frontmatter_retrofit(
        personas_dir,
        persona="sarah",
        relative_path="memory/procedural/migrated.md",
        memory_payload={"lessons": ["frontmatter review must be durable"]},
        write=True,
        migrated_at="2026-05-17T00:00:00Z",
    )
    assert migrated["ok"] is True
    assert index_file(conn, "sarah", "memory/procedural/migrated.md", memory_file)

    result = memory_review_action(
        conn,
        file_path="memory/procedural/migrated.md",
        action="confirm",
        reviewer="sarah",
    )

    assert result["ok"] is True
    assert result["durable_frontmatter"] is True
    assert result["after"]["review_status"] == "confirmed"
    assert result["after"]["can_use_as_instruction"] is True
    updated = memory_file.read_text(encoding="utf-8")
    assert updated.endswith(body)
    assert "review_status: confirmed" in updated
    assert "provenance_status: user_confirmed" in updated
    assert "can_use_as_instruction: true" in updated
    assert "payload_review_status: confirmed" in updated

    row = conn.execute(
        """
        SELECT fm_provenance_status, fm_review_status, fm_can_use_as_instruction,
               fm_requires_user_confirmation
        FROM memory_files
        WHERE relative_path = ?
        """,
        ("memory/procedural/migrated.md",),
    ).fetchone()
    assert row == ("user_confirmed", "confirmed", 1, 0)
    assert memory_review_pending(conn, persona="sarah") == []

    review_row = conn.execute(
        "SELECT action, reviewer FROM memory_review_actions WHERE action_id = ?",
        (result["action_id"],),
    ).fetchone()
    assert review_row == ("confirm", "sarah")
