import sqlite3
import time
from pathlib import Path

from chimera_memory.memory import (
    index_file,
    init_memory_tables,
    memory_content_duplicate_groups,
    memory_query,
    memory_search,
    memory_source_ref_query,
    normalized_content_fingerprint,
)


LEGACY_MEMORY_FILES_SQL = """
CREATE TABLE memory_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    persona TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    indexed_at REAL NOT NULL,
    fm_type TEXT,
    fm_importance INTEGER,
    fm_created TEXT,
    fm_last_accessed TEXT,
    fm_access_count INTEGER DEFAULT 0,
    fm_status TEXT DEFAULT 'active',
    fm_about TEXT,
    fm_tags TEXT,
    fm_entity TEXT,
    fm_relationship_temperature REAL,
    fm_trust_level REAL,
    fm_trend TEXT,
    fm_failure_count INTEGER DEFAULT 0
)
"""


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(memory_files)").fetchall()}


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA index_list(memory_files)").fetchall()}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def test_normalized_content_fingerprint_collapses_cosmetic_differences() -> None:
    assert normalized_content_fingerprint(" Hello\n\nWORLD ") == normalized_content_fingerprint("hello world")
    assert normalized_content_fingerprint("hello world") != normalized_content_fingerprint("hello there")


def test_init_memory_tables_migrates_legacy_memory_files_schema() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(LEGACY_MEMORY_FILES_SQL)
    conn.execute(
        """
        INSERT INTO memory_files (
            path, persona, relative_path, content_hash, indexed_at, fm_status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("C:/tmp/memory.md", "asa", "memory.md", "oldhash", 1.0, "active"),
    )

    init_memory_tables(conn)

    columns = _columns(conn)
    assert {
        "idempotency_key",
        "content_fingerprint",
        "updated_at",
        "fm_provenance_status",
        "fm_confidence",
        "fm_lifecycle_status",
        "fm_review_status",
        "fm_sensitivity_tier",
        "fm_can_use_as_instruction",
        "fm_can_use_as_evidence",
        "fm_requires_user_confirmation",
        "fm_exclude_from_default_search",
    } <= columns

    row = conn.execute("SELECT updated_at FROM memory_files WHERE path = ?", ("C:/tmp/memory.md",)).fetchone()
    assert row is not None
    assert row[0]

    indexes = _indexes(conn)
    assert "idx_mf_idempotency_key" in indexes
    assert "idx_mf_content_fingerprint" in indexes
    assert "idx_mf_active_persona_importance" in indexes
    assert "idx_mf_active_type_importance" in indexes
    assert "idx_mf_provenance_status" in indexes
    assert "idx_mf_review_status" in indexes
    assert "idx_mf_sensitivity_tier" in indexes
    assert "idx_mf_instruction_use" in indexes
    assert "idx_mf_default_search" in indexes
    assert "memory_file_edges" in _table_names(conn)
    assert "memory_file_source_refs" in _table_names(conn)


def test_index_file_writes_content_fingerprint_and_updates_timestamp(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("---\ntype: procedural\nimportance: 5\n---\nAlpha beta\n", encoding="utf-8")

    assert index_file(conn, "asa", "memory.md", memory_file)
    row = conn.execute(
        "SELECT content_fingerprint, updated_at FROM memory_files WHERE relative_path = ?",
        ("memory.md",),
    ).fetchone()
    assert row is not None
    assert row[0] == normalized_content_fingerprint(memory_file.read_text(encoding="utf-8"))
    assert row[1]

    first_updated_at = row[1]
    time.sleep(0.01)
    memory_file.write_text("---\ntype: procedural\nimportance: 6\n---\nAlpha beta changed\n", encoding="utf-8")
    assert index_file(conn, "asa", "memory.md", memory_file)
    row = conn.execute(
        "SELECT content_fingerprint, updated_at, fm_importance FROM memory_files WHERE relative_path = ?",
        ("memory.md",),
    ).fetchone()
    assert row is not None
    assert row[0] == normalized_content_fingerprint(memory_file.read_text(encoding="utf-8"))
    assert row[1] > first_updated_at
    assert row[2] == 6


def test_index_file_syncs_source_refs_and_query_filters(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    memory_file = tmp_path / "memory.md"
    memory_file.write_text(
        "---\n"
        "type: procedural\n"
        "importance: 7\n"
        "source_refs:\n"
        "  - kind: discord-msg\n"
        "    uri: '150'\n"
        "    title: source message\n"
        "    timestamp: '2026-05-17T00:00:00Z'\n"
        "    channel: active-development\n"
        "---\n"
        "Source indexed body.\n",
        encoding="utf-8",
    )

    assert index_file(conn, "asa", "memory.md", memory_file)
    refs = memory_source_ref_query(conn, persona="asa", source_kind="discord-msg")

    assert len(refs) == 1
    assert refs[0]["relative_path"] == "memory.md"
    assert refs[0]["uri"] == "150"
    assert refs[0]["metadata"] == {"channel": "active-development"}
    assert memory_query(conn, persona="asa", source_kind="discord-msg")[0]["relative_path"] == "memory.md"
    assert memory_query(conn, persona="asa", source_uri="150")[0]["relative_path"] == "memory.md"
    assert memory_search(conn, "Source", persona="asa", source_kind="discord-msg")[0]["relative_path"] == "memory.md"
    assert memory_search(conn, "Source", persona="asa", source_kind="gmail") == []

    assert not index_file(conn, "asa", "memory.md", memory_file)
    assert len(memory_source_ref_query(conn, persona="asa")) == 1

    memory_file.write_text(
        "---\ntype: procedural\nimportance: 7\n---\nSource indexed body.\n",
        encoding="utf-8",
    )
    assert index_file(conn, "asa", "memory.md", memory_file)
    assert memory_source_ref_query(conn, persona="asa") == []


def test_idempotency_key_is_partial_unique_and_content_fingerprint_accepts_duplicates() -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    conn.execute(
        """
        INSERT INTO memory_files (
            path, persona, relative_path, content_hash, indexed_at,
            idempotency_key, content_fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("C:/tmp/a.md", "asa", "a.md", "hash-a", 1.0, "key-1", "fingerprint-1"),
    )

    try:
        conn.execute(
            """
            INSERT INTO memory_files (
                path, persona, relative_path, content_hash, indexed_at,
                idempotency_key, content_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("C:/tmp/b.md", "asa", "b.md", "hash-b", 1.0, "key-1", "fingerprint-2"),
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("duplicate idempotency_key should be rejected")

    conn.execute(
        """
        INSERT INTO memory_files (
            path, persona, relative_path, content_hash, indexed_at,
            idempotency_key, content_fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("C:/tmp/c.md", "asa", "c.md", "hash-c", 1.0, "key-2", "fingerprint-1"),
    )
    conn.execute(
        """
        INSERT INTO memory_files (
            path, persona, relative_path, content_hash, indexed_at,
            idempotency_key, content_fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("C:/tmp/d.md", "asa", "d.md", "hash-d", 1.0, None, None),
    )
    conn.execute(
        """
        INSERT INTO memory_files (
            path, persona, relative_path, content_hash, indexed_at,
            idempotency_key, content_fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("C:/tmp/e.md", "asa", "e.md", "hash-e", 1.0, None, None),
    )

    count = conn.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0]
    assert count == 4

    groups = memory_content_duplicate_groups(conn, persona="asa")
    assert len(groups) == 1
    assert groups[0]["content_fingerprint"] == "fingerprint-1"
    assert groups[0]["duplicate_count"] == 2
    assert [item["relative_path"] for item in groups[0]["files"]] == ["a.md", "c.md"]
