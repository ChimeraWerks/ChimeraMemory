"""Database connection, schema, and WAL mode setup."""

import sqlite3
import time
import logging
from pathlib import Path
from contextlib import contextmanager

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- Sessions: one row per JSONL session file
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE NOT NULL,
    persona TEXT,
    title TEXT,
    git_branch TEXT,
    cwd TEXT,
    started_at TEXT,
    ended_at TEXT,
    exchange_count INTEGER DEFAULT 0,
    disposition TEXT,                    -- COMPLETED / IN_PROGRESS / INTERRUPTED
    mood_snapshot TEXT,                  -- JSON blob of mood.json at session boundaries
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Transcript: every entry from every session
CREATE TABLE IF NOT EXISTS transcript (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    entry_type TEXT NOT NULL,            -- user_message, assistant_message, tool_call,
                                         -- tool_result, discord_inbound, discord_outbound, system
    timestamp TEXT NOT NULL,
    content TEXT,
    persona TEXT,
    source TEXT,                          -- discord, cli, system, tool
    channel TEXT,                         -- discord channel name if applicable
    chat_id TEXT,                         -- discord chat_id
    message_id TEXT,                      -- discord message_id
    author TEXT,
    author_id TEXT,
    tool_name TEXT,                       -- for tool_call / tool_result entries
    conversation_id TEXT,                 -- thread grouping (populated later)
    source_refs TEXT,                     -- JSON: links to curated memory entries
    metadata TEXT,                        -- JSON blob for anything else
    UNIQUE(session_id, timestamp, entry_type, content)
);

-- Import log: track which JSONL files have been processed
CREATE TABLE IF NOT EXISTS import_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT UNIQUE NOT NULL,
    file_hash TEXT,
    file_size INTEGER,
    last_position INTEGER DEFAULT 0,     -- byte offset for tail-read
    entries_imported INTEGER DEFAULT 0,
    imported_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Settings: configurable retention, tiers, etc.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_transcript_session ON transcript(session_id);
CREATE INDEX IF NOT EXISTS idx_transcript_ts ON transcript(timestamp);
CREATE INDEX IF NOT EXISTS idx_transcript_chat_ts ON transcript(chat_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_transcript_type ON transcript(entry_type);
CREATE INDEX IF NOT EXISTS idx_transcript_source ON transcript(source);
CREATE INDEX IF NOT EXISTS idx_transcript_tool ON transcript(tool_name);
CREATE INDEX IF NOT EXISTS idx_transcript_msg_id ON transcript(message_id);
CREATE INDEX IF NOT EXISTS idx_transcript_persona ON transcript(persona);
CREATE INDEX IF NOT EXISTS idx_transcript_conv ON transcript(conversation_id);

-- FTS5: full-text search on content (external content table to avoid duplication)
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
    content,
    content=transcript,
    content_rowid=id,
    tokenize='porter unicode61'
);

-- FTS5 sync triggers
CREATE TRIGGER IF NOT EXISTS transcript_ai AFTER INSERT ON transcript BEGIN
    INSERT INTO transcript_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS transcript_ad AFTER DELETE ON transcript BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS transcript_au AFTER UPDATE ON transcript BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO transcript_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

# Default settings
DEFAULT_SETTINGS = {
    "retention_days": "90",
    "max_db_size_mb": "1024",
    "index_tool_calls": "true",
    "index_tool_results": "false",
    "index_system": "false",
}


class TranscriptDB:
    """SQLite database for transcript storage with WAL mode and retry logic."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize database with schema and pragmas."""
        conn = self._connect()
        try:
            conn.executescript(SCHEMA_SQL)
            # Insert default settings (don't overwrite existing)
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                    (key, value),
                )
            # Set schema version
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        """Create a connection with WAL mode and safety pragmas."""
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA wal_autocheckpoint=100")
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def connection(self):
        """Context manager for a standard connection with retry on SQLITE_BUSY."""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def bulk_connection(self):
        """Context manager for bulk imports with relaxed sync settings."""
        conn = self._connect()
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-65536")  # 64MB cache
        try:
            yield conn
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            conn.close()

    def execute_with_retry(self, conn: sqlite3.Connection, sql: str, params=(),
                           max_retries: int = 3, base_delay: float = 0.5):
        """Execute SQL with exponential backoff retry on SQLITE_BUSY."""
        for attempt in range(max_retries):
            try:
                return conn.execute(sql, params)
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    log.warning(f"DB locked, retry {attempt + 1}/{max_retries} in {delay:.1f}s")
                    time.sleep(delay)
                else:
                    raise

    def executemany_with_retry(self, conn: sqlite3.Connection, sql: str, params_list,
                                max_retries: int = 3, base_delay: float = 0.5):
        """Execute many with exponential backoff retry on SQLITE_BUSY."""
        for attempt in range(max_retries):
            try:
                return conn.executemany(sql, params_list)
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    log.warning(f"DB locked, retry {attempt + 1}/{max_retries} in {delay:.1f}s")
                    time.sleep(delay)
                else:
                    raise

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        """Get a setting value."""
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        """Set a setting value."""
        with self.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                (key, value),
            )
            conn.commit()

    def insert_entries(self, entries: list[dict], conn: sqlite3.Connection | None = None):
        """Batch insert transcript entries. Uses INSERT OR IGNORE for dedup."""
        if not entries:
            return 0

        sql = """
            INSERT OR IGNORE INTO transcript
            (session_id, entry_type, timestamp, content, persona, source,
             channel, chat_id, message_id, author, author_id, tool_name,
             conversation_id, source_refs, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = [
            (
                e.get("session_id", ""),
                e.get("entry_type", "unknown"),
                e.get("timestamp", ""),
                e.get("content"),
                e.get("persona"),
                e.get("source"),
                e.get("channel"),
                e.get("chat_id"),
                e.get("message_id"),
                e.get("author"),
                e.get("author_id"),
                e.get("tool_name"),
                e.get("conversation_id"),
                e.get("source_refs"),
                e.get("metadata"),
            )
            for e in entries
        ]

        own_conn = conn is None
        if own_conn:
            conn = self._connect()

        try:
            self.executemany_with_retry(conn, sql, params)
            if own_conn:
                conn.commit()
            return len(params)
        finally:
            if own_conn:
                conn.close()

    def upsert_session(self, session: dict, conn: sqlite3.Connection | None = None):
        """Insert or update a session record."""
        sql = """
            INSERT INTO sessions (session_id, persona, title, git_branch, cwd, started_at, ended_at, exchange_count, disposition)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                title = COALESCE(excluded.title, sessions.title),
                ended_at = COALESCE(excluded.ended_at, sessions.ended_at),
                exchange_count = COALESCE(excluded.exchange_count, sessions.exchange_count),
                disposition = COALESCE(excluded.disposition, sessions.disposition)
        """
        params = (
            session.get("session_id"),
            session.get("persona"),
            session.get("title"),
            session.get("git_branch"),
            session.get("cwd"),
            session.get("started_at"),
            session.get("ended_at"),
            session.get("exchange_count", 0),
            session.get("disposition"),
        )

        own_conn = conn is None
        if own_conn:
            conn = self._connect()

        try:
            self.execute_with_retry(conn, sql, params)
            if own_conn:
                conn.commit()
        finally:
            if own_conn:
                conn.close()

    def stats(self) -> dict:
        """Return database statistics."""
        with self.connection() as conn:
            entry_count = conn.execute("SELECT COUNT(*) FROM transcript").fetchone()[0]
            session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            last_entry = conn.execute(
                "SELECT timestamp FROM transcript ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0

            return {
                "entry_count": entry_count,
                "session_count": session_count,
                "last_entry": last_entry["timestamp"] if last_entry else None,
                "db_size_bytes": db_size,
                "db_size_mb": round(db_size / (1024 * 1024), 2),
                "db_path": str(self.db_path),
            }

    def disable_fts_triggers(self, conn: sqlite3.Connection):
        """Drop FTS triggers for bulk import performance."""
        conn.execute("DROP TRIGGER IF EXISTS transcript_ai")
        conn.execute("DROP TRIGGER IF EXISTS transcript_ad")
        conn.execute("DROP TRIGGER IF EXISTS transcript_au")

    def rebuild_fts(self, conn: sqlite3.Connection):
        """Rebuild FTS index and re-create triggers. Call after bulk import."""
        conn.execute("INSERT INTO transcript_fts(transcript_fts) VALUES('rebuild')")
        # Re-create triggers
        trigger_sql = """
        CREATE TRIGGER IF NOT EXISTS transcript_ai AFTER INSERT ON transcript BEGIN
            INSERT INTO transcript_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS transcript_ad AFTER DELETE ON transcript BEGIN
            INSERT INTO transcript_fts(transcript_fts, rowid, content) VALUES('delete', old.id, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS transcript_au AFTER UPDATE ON transcript BEGIN
            INSERT INTO transcript_fts(transcript_fts, rowid, content) VALUES('delete', old.id, old.content);
            INSERT INTO transcript_fts(rowid, content) VALUES (new.id, new.content);
        END;
        """
        conn.executescript(trigger_sql)
        conn.commit()
