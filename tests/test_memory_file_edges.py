import sqlite3
from pathlib import Path

from chimera_memory.memory import (
    index_file,
    init_memory_tables,
    memory_audit_query,
    memory_file_edge_query,
    memory_file_edge_temporal_sweep,
    memory_file_edge_upsert,
)


def _write_memory(path: Path, frontmatter: list[str], body: str) -> None:
    path.write_text(
        "\n".join(["---", *frontmatter, "---", body]),
        encoding="utf-8",
    )


def _index_pair(conn: sqlite3.Connection, tmp_path: Path) -> None:
    first = tmp_path / "decision.md"
    _write_memory(
        first,
        ["type: semantic", "importance: 8", "about: CM stays core"],
        "CM stays the local-first memory core.",
    )
    second = tmp_path / "evidence.md"
    _write_memory(
        second,
        ["type: reflection", "importance: 6", "about: OB1 comparison evidence"],
        "OB1 patterns are lifted additively.",
    )
    assert index_file(conn, "asa", "memory/decision.md", first)
    assert index_file(conn, "asa", "memory/evidence.md", second)


def test_memory_file_edge_upsert_accumulates_support_and_audits(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_pair(conn, tmp_path)

    first = memory_file_edge_upsert(
        conn,
        source_file_path="memory/evidence.md",
        target_file_path="memory/decision.md",
        relation_type="supports",
        confidence=0.4,
        evidence="OB1 comparison notes",
        classifier_version="manual.v1",
        actor="test",
    )
    second = memory_file_edge_upsert(
        conn,
        source_file_path="memory/evidence.md",
        target_file_path="memory/decision.md",
        relation_type="supports",
        confidence=0.9,
        evidence="second review confirmed",
        classifier_version="manual.v2",
        actor="test",
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["edge"]["edge_id"] == second["edge"]["edge_id"]
    assert second["edge"]["support_count"] == 2
    assert second["edge"]["confidence"] == 0.9
    assert second["edge"]["classifier_version"] == "manual.v2"
    assert second["edge"]["source"]["relative_path"] == "memory/evidence.md"
    assert second["edge"]["target"]["relative_path"] == "memory/decision.md"

    events = memory_audit_query(conn, event_type="memory_file_edge_upserted", persona="asa")
    assert len(events) == 2
    assert {event["payload"]["relation_type"] for event in events} == {"supports"}
    assert max(event["payload"]["support_count"] for event in events) == 2


def test_memory_file_edge_query_filters_current_and_historical_edges(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_pair(conn, tmp_path)

    current = memory_file_edge_upsert(
        conn,
        source_file_path="memory/evidence.md",
        target_file_path="memory/decision.md",
        relation_type="supports",
    )
    historical = memory_file_edge_upsert(
        conn,
        source_file_path="memory/decision.md",
        target_file_path="memory/evidence.md",
        relation_type="evolved_into",
        valid_until="2026-01-01T00:00:00Z",
    )

    assert current["ok"] is True
    assert historical["ok"] is True

    current_edges = memory_file_edge_query(conn, file_path="memory/decision.md")
    assert [edge["relation_type"] for edge in current_edges] == ["supports"]

    all_edges = memory_file_edge_query(conn, file_path="memory/decision.md", current_only=False)
    assert {edge["relation_type"] for edge in all_edges} == {"supports", "evolved_into"}

    support_edges = memory_file_edge_query(conn, relation_type="supports", persona="asa")
    assert len(support_edges) == 1
    assert support_edges[0]["source"]["relative_path"] == "memory/evidence.md"


def test_memory_file_edge_current_query_respects_validity_window(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_pair(conn, tmp_path)

    result = memory_file_edge_upsert(
        conn,
        source_file_path="memory/evidence.md",
        target_file_path="memory/decision.md",
        relation_type="supports",
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2026-12-31T00:00:00Z",
    )

    assert result["ok"] is True
    assert memory_file_edge_query(
        conn,
        relation_type="supports",
        current_at="2025-12-31T00:00:00Z",
    ) == []
    assert len(memory_file_edge_query(
        conn,
        relation_type="supports",
        current_at="2026-06-01T00:00:00Z",
    )) == 1
    assert memory_file_edge_query(
        conn,
        relation_type="supports",
        current_at="2027-01-01T00:00:00Z",
    ) == []


def test_memory_file_edge_temporal_sweep_expires_edges_for_stale_files(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_pair(conn, tmp_path)
    memory_file_edge_upsert(
        conn,
        source_file_path="memory/evidence.md",
        target_file_path="memory/decision.md",
        relation_type="supports",
    )
    conn.execute(
        """
        UPDATE memory_files
           SET fm_lifecycle_status = 'superseded'
         WHERE relative_path = ?
        """,
        ("memory/decision.md",),
    )

    dry_run = memory_file_edge_temporal_sweep(
        conn,
        persona="asa",
        now="2026-05-15T00:00:00Z",
        dry_run=True,
    )
    applied = memory_file_edge_temporal_sweep(
        conn,
        persona="asa",
        now="2026-05-15T00:00:00Z",
        dry_run=False,
    )

    assert dry_run["candidate_count"] == 1
    assert dry_run["expired_count"] == 0
    assert applied["candidate_count"] == 1
    assert applied["expired_count"] == 1
    assert memory_file_edge_query(
        conn,
        file_path="memory/decision.md",
        current_at="2026-05-16T00:00:00Z",
    ) == []

    events = memory_audit_query(conn, event_type="memory_file_edges_temporal_sweep", persona="asa")
    assert len(events) == 2
    assert max(event["payload"]["expired_count"] for event in events) == 1


def test_memory_file_edge_upsert_reports_missing_or_same_file(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_pair(conn, tmp_path)

    missing = memory_file_edge_upsert(
        conn,
        source_file_path="missing.md",
        target_file_path="memory/decision.md",
    )
    same = memory_file_edge_upsert(
        conn,
        source_file_path="memory/decision.md",
        target_file_path="memory/decision.md",
    )

    assert missing == {
        "ok": False,
        "error": "source memory file not found",
        "source_file_path": "missing.md",
    }
    assert same == {"ok": False, "error": "source and target memory files must differ"}
