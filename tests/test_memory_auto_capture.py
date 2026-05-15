import sqlite3
from pathlib import Path

from chimera_memory.memory import (
    init_memory_tables,
    memory_audit_query,
    memory_auto_capture_session_close,
    memory_review_pending,
)
from chimera_memory.memory_auto_capture import parse_action_items


def _personas_dir(tmp_path: Path) -> Path:
    personas = tmp_path / "personas"
    (personas / "developer" / "asa").mkdir(parents=True)
    return personas


def test_parse_action_items_from_session_text() -> None:
    items = parse_action_items(
        "\n".join(
            [
                "Normal note.",
                "ACT NOW: ship the dashboard review surface",
                "- [ ] verify provider smoke harness",
                "TODO update module docs",
            ]
        )
    )

    assert items == [
        "ship the dashboard review surface",
        "verify provider smoke harness",
        "update module docs",
    ]


def test_auto_capture_preview_audits_without_writing(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    personas = _personas_dir(tmp_path)

    result = memory_auto_capture_session_close(
        conn,
        personas,
        persona="asa",
        title="Day 58 wrap",
        summary="Phase 5e dashboard landed and needs review.",
        act_now_text="ACT NOW: run the provider smoke once credentials exist",
        write=False,
    )

    assert result["ok"] is True
    assert result["written"] is False
    assert result["plan"]["persona"] == "asa"
    assert result["plan"]["action_items"] == ["run the provider smoke once credentials exist"]
    assert not list((personas / "developer" / "asa" / "memory" / "episodes").glob("*.md"))

    events = memory_audit_query(conn, event_type="memory_auto_capture_planned", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["action_item_count"] == 1


def test_auto_capture_write_creates_review_gated_memory(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    personas = _personas_dir(tmp_path)

    result = memory_auto_capture_session_close(
        conn,
        personas,
        persona="asa",
        title="Auto capture ship",
        summary="Auto-capture writes an evidence-only session close memory.",
        act_now_text="- [ ] confirm the memory in review queue",
        source_session_id="session-123",
        write=True,
    )

    assert result["ok"] is True
    assert result["written"] is True
    assert result["relative_path"].startswith("memory/episodes/")
    memory_file = Path(result["path"])
    assert memory_file.exists()
    content = memory_file.read_text(encoding="utf-8")
    assert 'provenance_status: "generated"' in content
    assert 'review_status: "pending"' in content
    assert 'can_use_as_instruction: false' in content
    assert "- confirm the memory in review queue" in content

    row = conn.execute(
        """
        SELECT fm_provenance_status, fm_review_status, fm_can_use_as_instruction,
               fm_can_use_as_evidence, fm_requires_user_confirmation
        FROM memory_files
        WHERE relative_path = ?
        """,
        (result["relative_path"],),
    ).fetchone()
    assert row == ("generated", "pending", 0, 1, 1)
    assert memory_review_pending(conn, persona="asa")[0]["relative_path"] == result["relative_path"]

    events = memory_audit_query(conn, event_type="memory_auto_capture_written", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["file_id"] == result["file_id"]
