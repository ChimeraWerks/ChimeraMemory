from __future__ import annotations

import sqlite3
from pathlib import Path

from chimera_memory.db import TranscriptDB
from chimera_memory.db_split import discover_personas, split_db
from chimera_memory.embeddings import init_embedding_table


def _seed_source(path: Path) -> None:
    db = TranscriptDB(path)
    with db.connection() as conn:
        init_embedding_table(conn)
        conn.execute(
            """
            INSERT INTO sessions (session_id, persona, title, cwd, started_at)
            VALUES ('sarah-session', 'sarah', 'Sarah', '/repo/sarah', '2026-05-01T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO sessions (session_id, persona, title, cwd, started_at)
            VALUES ('asa-session', 'asa', 'Asa', '/repo/asa', '2026-05-01T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO transcript
            (id, session_id, entry_type, timestamp, content, persona, source)
            VALUES
            (10, 'sarah-session', 'discord_inbound', '2026-05-01T00:00:00Z', 'sarah marker', 'sarah', 'discord'),
            (20, 'asa-session', 'discord_inbound', '2026-05-01T00:00:01Z', 'asa marker', 'asa', 'discord')
            """
        )
        conn.execute(
            "INSERT INTO transcript_embeddings (transcript_id, embedding) VALUES (10, ?), (20, ?)",
            (b"sarah-embedding", b"asa-embedding"),
        )
        conn.execute(
            """
            INSERT INTO import_log (file_path, file_hash, file_size, last_position, entries_imported)
            VALUES
            (?, 'h1', 100, 100, 1),
            (?, 'h2', 100, 100, 1)
            """,
            (
                str(Path("C:/Users/charl/.claude/projects/C--sarah/sarah-session.jsonl")),
                str(Path("C:/Users/charl/.claude/projects/C--asa/asa-session.jsonl")),
            ),
        )
        conn.commit()


def test_discover_personas(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _seed_source(source)

    assert discover_personas(source) == ["asa", "sarah"]


def test_split_db_dry_run_counts_rows(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _seed_source(source)

    results = split_db(
        source,
        output_root=tmp_path / "personas",
        personas=["sarah"],
        persona_ids={"sarah": "researcher/sarah"},
        dry_run=True,
    )

    assert len(results) == 1
    result = results[0]
    assert result.dry_run is True
    assert result.session_rows == 1
    assert result.transcript_rows == 1
    assert result.import_log_rows == 1
    assert result.embedding_rows == 1
    assert result.target_db.endswith("researcher\\sarah\\transcript.db") or result.target_db.endswith("researcher/sarah/transcript.db")


def test_split_db_writes_persona_db_with_fts_and_embeddings(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _seed_source(source)

    results = split_db(
        source,
        output_root=tmp_path / "personas",
        personas=["sarah"],
        persona_ids={"sarah": "researcher/sarah"},
        dry_run=False,
        replace=True,
    )

    result = results[0]
    target = Path(result.target_db)
    assert result.integrity_check == "ok"
    assert target.exists()

    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM transcript").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transcript WHERE persona = 'sarah'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transcript WHERE persona = 'asa'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM import_log").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transcript_embeddings").fetchone()[0] == 1
        fts = conn.execute("SELECT rowid FROM transcript_fts WHERE transcript_fts MATCH 'sarah'").fetchall()
        assert len(fts) == 1
    finally:
        conn.close()
