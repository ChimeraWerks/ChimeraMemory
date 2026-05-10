"""Split a shared Chimera transcript DB into per-persona DBs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .db import TranscriptDB
from .embeddings import init_embedding_table
from .paths import persona_transcript_db_path


@dataclass
class PersonaSplitResult:
    persona: str
    persona_id: str | None
    target_db: str
    dry_run: bool
    session_rows: int
    transcript_rows: int
    import_log_rows: int
    embedding_rows: int
    integrity_check: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in _columns(conn, table) if _table_exists(conn, table) else False


def discover_personas(source_db: Path) -> list[str]:
    """Discover personas present in the transcript DB."""
    source_db = Path(source_db).expanduser()
    with _connect(source_db, readonly=True) as conn:
        personas: set[str] = set()
        if _has_column(conn, "transcript", "persona"):
            personas.update(
                row["persona"]
                for row in conn.execute(
                    "SELECT DISTINCT persona FROM transcript WHERE persona IS NOT NULL AND persona != ''"
                )
            )
        if _has_column(conn, "sessions", "persona"):
            personas.update(
                row["persona"]
                for row in conn.execute(
                    "SELECT DISTINCT persona FROM sessions WHERE persona IS NOT NULL AND persona != ''"
                )
            )
        return sorted(personas)


def _row_count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _common_columns(source: sqlite3.Connection, target: sqlite3.Connection, table: str) -> list[str]:
    return [col for col in _columns(source, table) if col in _columns(target, table)]


def _insert_rows(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    where_sql: str,
    params: tuple,
) -> int:
    cols = _common_columns(source, target, table)
    if not cols:
        return 0
    select_sql = f"SELECT {', '.join(cols)} FROM {table} WHERE {where_sql}"
    rows = source.execute(select_sql, params).fetchall()
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in cols)
    insert_sql = f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    target.executemany(insert_sql, ([row[col] for col in cols] for row in rows))
    return len(rows)


def _normalize_path(value: str) -> str:
    return str(Path(value).expanduser()).replace("\\", "/").rstrip("/").lower()


def _matches_jsonl_dir(file_path: str, jsonl_dirs: Iterable[Path]) -> bool:
    normalized = _normalize_path(file_path)
    for jsonl_dir in jsonl_dirs:
        prefix = _normalize_path(str(jsonl_dir))
        if normalized.startswith(prefix + "/") or normalized == prefix:
            return True
    return False


def _copy_import_log(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    session_ids: set[str],
    jsonl_dirs: list[Path],
) -> int:
    if not _table_exists(source, "import_log") or not _table_exists(target, "import_log"):
        return 0
    cols = _common_columns(source, target, "import_log")
    if not cols:
        return 0

    rows = []
    for row in source.execute(f"SELECT {', '.join(cols)} FROM import_log"):
        file_path = row["file_path"]
        stem = Path(file_path).stem
        if stem in session_ids or _matches_jsonl_dir(file_path, jsonl_dirs):
            rows.append(row)

    if not rows:
        return 0

    placeholders = ", ".join("?" for _ in cols)
    target.executemany(
        f"INSERT OR IGNORE INTO import_log ({', '.join(cols)}) VALUES ({placeholders})",
        ([row[col] for col in cols] for row in rows),
    )
    return len(rows)


def _remove_sqlite_files(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()


def split_persona_db(
    source_db: Path,
    persona: str,
    *,
    persona_id: str | None = None,
    output_root: Path | str | None = None,
    jsonl_dirs: list[Path] | None = None,
    dry_run: bool = True,
    replace: bool = False,
) -> PersonaSplitResult:
    """Create or preview one per-persona transcript DB."""
    source_db = Path(source_db).expanduser()
    target_db = persona_transcript_db_path(persona, persona_id=persona_id, root=output_root)
    jsonl_dirs = jsonl_dirs or []

    with _connect(source_db, readonly=True) as source:
        if not _has_column(source, "transcript", "persona"):
            raise ValueError("source transcript table has no persona column")

        transcript_rows = _row_count(source, "SELECT COUNT(*) FROM transcript WHERE persona = ?", (persona,))
        session_ids = {
            row["session_id"]
            for row in source.execute(
                "SELECT DISTINCT session_id FROM transcript WHERE persona = ?",
                (persona,),
            )
            if row["session_id"]
        }
        session_rows = 0
        if _table_exists(source, "sessions"):
            session_rows = _row_count(
                source,
                """
                SELECT COUNT(*)
                FROM sessions
                WHERE persona = ?
                   OR session_id IN (SELECT DISTINCT session_id FROM transcript WHERE persona = ?)
                """,
                (persona, persona),
            )

        import_log_rows = 0
        if _table_exists(source, "import_log"):
            for row in source.execute("SELECT file_path FROM import_log"):
                file_path = row["file_path"]
                if Path(file_path).stem in session_ids or _matches_jsonl_dir(file_path, jsonl_dirs):
                    import_log_rows += 1

        embedding_rows = 0
        if _table_exists(source, "transcript_embeddings"):
            embedding_rows = _row_count(
                source,
                """
                SELECT COUNT(*)
                FROM transcript_embeddings e
                JOIN transcript t ON t.id = e.transcript_id
                WHERE t.persona = ?
                """,
                (persona,),
            )

        if dry_run:
            return PersonaSplitResult(
                persona=persona,
                persona_id=persona_id,
                target_db=str(target_db),
                dry_run=True,
                session_rows=session_rows,
                transcript_rows=transcript_rows,
                import_log_rows=import_log_rows,
                embedding_rows=embedding_rows,
            )

        if target_db.exists() and not replace:
            raise FileExistsError(f"target DB already exists: {target_db}")
        if replace:
            _remove_sqlite_files(target_db)

        target_db.parent.mkdir(parents=True, exist_ok=True)
        TranscriptDB(target_db)

        with _connect(target_db, readonly=False) as target:
            init_embedding_table(target)

            if _table_exists(source, "settings"):
                settings_cols = _common_columns(source, target, "settings")
                rows = source.execute(f"SELECT {', '.join(settings_cols)} FROM settings").fetchall()
                if rows:
                    placeholders = ", ".join("?" for _ in settings_cols)
                    target.executemany(
                        f"INSERT OR REPLACE INTO settings ({', '.join(settings_cols)}) VALUES ({placeholders})",
                        ([row[col] for col in settings_cols] for row in rows),
                    )

            _insert_rows(
                source,
                target,
                "sessions",
                """
                persona = ?
                OR session_id IN (SELECT DISTINCT session_id FROM transcript WHERE persona = ?)
                """,
                (persona, persona),
            )

            _insert_rows(source, target, "transcript", "persona = ?", (persona,))

            if _table_exists(source, "transcript_embeddings") and _table_exists(target, "transcript_embeddings"):
                embedding_cols = _common_columns(source, target, "transcript_embeddings")
                rows = source.execute(
                    f"""
                    SELECT {', '.join('e.' + col for col in embedding_cols)}
                    FROM transcript_embeddings e
                    JOIN transcript t ON t.id = e.transcript_id
                    WHERE t.persona = ?
                    """,
                    (persona,),
                ).fetchall()
                if rows:
                    placeholders = ", ".join("?" for _ in embedding_cols)
                    target.executemany(
                        f"INSERT OR IGNORE INTO transcript_embeddings ({', '.join(embedding_cols)}) VALUES ({placeholders})",
                        ([row[col] for col in embedding_cols] for row in rows),
                    )

            _copy_import_log(source, target, session_ids=session_ids, jsonl_dirs=jsonl_dirs)
            target.execute("INSERT INTO transcript_fts(transcript_fts) VALUES('rebuild')")
            target.commit()
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]

            actual_session_rows = _row_count(target, "SELECT COUNT(*) FROM sessions")
            actual_transcript_rows = _row_count(target, "SELECT COUNT(*) FROM transcript")
            actual_import_log_rows = _row_count(target, "SELECT COUNT(*) FROM import_log")
            actual_embedding_rows = (
                _row_count(target, "SELECT COUNT(*) FROM transcript_embeddings")
                if _table_exists(target, "transcript_embeddings")
                else 0
            )

    return PersonaSplitResult(
        persona=persona,
        persona_id=persona_id,
        target_db=str(target_db),
        dry_run=False,
        session_rows=actual_session_rows,
        transcript_rows=actual_transcript_rows,
        import_log_rows=actual_import_log_rows,
        embedding_rows=actual_embedding_rows,
        integrity_check=integrity,
    )


def parse_mapping(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"mapping must use persona=value form: {value}")
        key, raw = value.split("=", 1)
        key = key.strip()
        raw = raw.strip()
        if not key or not raw:
            raise ValueError(f"mapping must use persona=value form: {value}")
        result[key] = raw
    return result


def split_db(
    source_db: Path,
    *,
    output_root: Path | str | None = None,
    personas: list[str] | None = None,
    persona_ids: dict[str, str] | None = None,
    jsonl_dirs: dict[str, str] | None = None,
    dry_run: bool = True,
    replace: bool = False,
) -> list[PersonaSplitResult]:
    source_db = Path(source_db).expanduser()
    selected = personas or discover_personas(source_db)
    if not selected:
        raise ValueError("no personas found in source DB")

    persona_ids = persona_ids or {}
    jsonl_dirs = jsonl_dirs or {}
    results = []
    for persona in selected:
        dirs = [Path(jsonl_dirs[persona]).expanduser()] if persona in jsonl_dirs else []
        results.append(
            split_persona_db(
                source_db,
                persona,
                persona_id=persona_ids.get(persona),
                output_root=output_root,
                jsonl_dirs=dirs,
                dry_run=dry_run,
                replace=replace,
            )
        )
    return results


def results_to_json(results: list[PersonaSplitResult]) -> str:
    return json.dumps([result.to_dict() for result in results], indent=2)
