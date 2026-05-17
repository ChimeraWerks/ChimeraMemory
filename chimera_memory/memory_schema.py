"""SQLite schema and migrations for ChimeraMemory curated memory."""

from __future__ import annotations

import sqlite3


MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_files (
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
    fm_failure_count INTEGER DEFAULT 0,
    idempotency_key TEXT,
    content_fingerprint TEXT,
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    fm_provenance_status TEXT DEFAULT 'imported',
    fm_confidence REAL,
    fm_lifecycle_status TEXT DEFAULT 'active',
    fm_review_status TEXT DEFAULT 'confirmed',
    fm_sensitivity_tier TEXT DEFAULT 'standard',
    fm_can_use_as_instruction INTEGER DEFAULT 1,
    fm_can_use_as_evidence INTEGER DEFAULT 1,
    fm_requires_user_confirmation INTEGER DEFAULT 0,
    fm_exclude_from_default_search INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mf_persona ON memory_files(persona);
CREATE INDEX IF NOT EXISTS idx_mf_type ON memory_files(fm_type);
CREATE INDEX IF NOT EXISTS idx_mf_importance ON memory_files(fm_importance);
CREATE INDEX IF NOT EXISTS idx_mf_status ON memory_files(fm_status);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    file_id INTEGER PRIMARY KEY REFERENCES memory_files(id) ON DELETE CASCADE,
    embedding BLOB NOT NULL,
    embedded_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    path,
    persona,
    relative_path,
    content,
    fm_type,
    fm_tags,
    fm_about,
    tokenize='porter unicode61'
);
"""


MEMORY_POST_MIGRATION_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_mf_idempotency_key
ON memory_files(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_mf_content_fingerprint
ON memory_files(content_fingerprint)
WHERE content_fingerprint IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_mf_active_persona_importance
ON memory_files(persona, fm_importance DESC)
WHERE fm_status = 'active';

CREATE INDEX IF NOT EXISTS idx_mf_active_type_importance
ON memory_files(fm_type, fm_importance DESC)
WHERE fm_status = 'active';

CREATE INDEX IF NOT EXISTS idx_mf_provenance_status
ON memory_files(fm_provenance_status);

CREATE INDEX IF NOT EXISTS idx_mf_review_status
ON memory_files(fm_review_status);

CREATE INDEX IF NOT EXISTS idx_mf_sensitivity_tier
ON memory_files(fm_sensitivity_tier);

CREATE INDEX IF NOT EXISTS idx_mf_instruction_use
ON memory_files(fm_can_use_as_instruction)
WHERE fm_can_use_as_instruction = 1;

CREATE INDEX IF NOT EXISTS idx_mf_default_search
ON memory_files(persona, fm_importance DESC)
WHERE fm_exclude_from_default_search = 0 OR fm_exclude_from_default_search IS NULL;

CREATE TRIGGER IF NOT EXISTS memory_files_ai_updated_at
AFTER INSERT ON memory_files
WHEN NEW.updated_at IS NULL
BEGIN
    UPDATE memory_files
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS memory_files_au_updated_at
AFTER UPDATE OF
    path, persona, relative_path, content_hash, indexed_at,
    fm_type, fm_importance, fm_created, fm_last_accessed,
    fm_access_count, fm_status, fm_about, fm_tags, fm_entity,
    fm_relationship_temperature, fm_trust_level, fm_trend,
    fm_failure_count, idempotency_key, content_fingerprint,
    fm_provenance_status, fm_confidence, fm_lifecycle_status,
    fm_review_status, fm_sensitivity_tier, fm_can_use_as_instruction,
    fm_can_use_as_evidence, fm_requires_user_confirmation,
    fm_exclude_from_default_search
ON memory_files
BEGIN
    UPDATE memory_files
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS memory_recall_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    schema_version TEXT NOT NULL DEFAULT 'chimera-memory.recall-trace.v1',
    tool_name TEXT NOT NULL,
    persona TEXT,
    query_text TEXT NOT NULL,
    requested_limit INTEGER NOT NULL,
    result_count INTEGER DEFAULT 0,
    returned_count INTEGER DEFAULT 0,
    runtime_name TEXT,
    runtime_version TEXT,
    task_id TEXT,
    flow_id TEXT,
    channel_kind TEXT,
    channel_id TEXT,
    request_payload TEXT DEFAULT '{}',
    response_policy TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memory_recall_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL REFERENCES memory_recall_traces(trace_id) ON DELETE CASCADE,
    file_id INTEGER REFERENCES memory_files(id) ON DELETE SET NULL,
    rank INTEGER NOT NULL,
    similarity REAL,
    ranking_score REAL,
    returned INTEGER NOT NULL DEFAULT 1,
    used INTEGER NOT NULL DEFAULT 0,
    ignored_reason TEXT,
    path TEXT,
    persona TEXT,
    relative_path TEXT,
    fm_type TEXT,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memory_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    event_type TEXT NOT NULL,
    actor TEXT DEFAULT 'system',
    persona TEXT,
    target_kind TEXT,
    target_id TEXT,
    trace_id TEXT,
    payload TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_memory_recall_traces_created_at
ON memory_recall_traces(created_at);

CREATE INDEX IF NOT EXISTS idx_memory_recall_traces_persona
ON memory_recall_traces(persona);

CREATE INDEX IF NOT EXISTS idx_memory_recall_traces_tool
ON memory_recall_traces(tool_name);

CREATE INDEX IF NOT EXISTS idx_memory_recall_items_trace_rank
ON memory_recall_items(trace_id, rank);

CREATE INDEX IF NOT EXISTS idx_memory_audit_events_created_at
ON memory_audit_events(created_at);

CREATE INDEX IF NOT EXISTS idx_memory_audit_events_type
ON memory_audit_events(event_type);

CREATE INDEX IF NOT EXISTS idx_memory_audit_events_persona
ON memory_audit_events(persona);

CREATE TABLE IF NOT EXISTS memory_review_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    action TEXT NOT NULL,
    reviewer TEXT DEFAULT 'user',
    persona TEXT,
    file_id INTEGER REFERENCES memory_files(id) ON DELETE SET NULL,
    path TEXT,
    before_metadata TEXT DEFAULT '{}',
    after_metadata TEXT DEFAULT '{}',
    notes TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_memory_review_actions_file
ON memory_review_actions(file_id);

CREATE INDEX IF NOT EXISTS idx_memory_review_actions_action
ON memory_review_actions(action);

CREATE INDEX IF NOT EXISTS idx_memory_review_actions_created_at
ON memory_review_actions(created_at);

CREATE TABLE IF NOT EXISTS memory_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id TEXT UNIQUE NOT NULL,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    aliases TEXT DEFAULT '[]',
    confidence REAL DEFAULT 1.0,
    source TEXT DEFAULT 'frontmatter',
    metadata TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(entity_type, normalized_name)
);

CREATE TABLE IF NOT EXISTS memory_file_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES memory_files(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
    mention_role TEXT NOT NULL DEFAULT 'related',
    confidence REAL DEFAULT 1.0,
    source TEXT DEFAULT 'frontmatter',
    evidence TEXT DEFAULT '',
    metadata TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(file_id, entity_id, mention_role)
);

CREATE TABLE IF NOT EXISTS memory_entity_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id TEXT UNIQUE NOT NULL,
    source_entity_id INTEGER NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
    target_entity_id INTEGER NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL DEFAULT 'related_to',
    confidence REAL DEFAULT 1.0,
    support_count INTEGER NOT NULL DEFAULT 1,
    valid_from TEXT,
    valid_until TEXT,
    decay_weight REAL DEFAULT 1.0,
    classifier_version TEXT DEFAULT '',
    metadata TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK(source_entity_id <> target_entity_id),
    UNIQUE(source_entity_id, target_entity_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_memory_entities_type_name
ON memory_entities(entity_type, normalized_name);

CREATE INDEX IF NOT EXISTS idx_memory_entities_name
ON memory_entities(normalized_name);

CREATE INDEX IF NOT EXISTS idx_memory_file_entities_file
ON memory_file_entities(file_id);

CREATE INDEX IF NOT EXISTS idx_memory_file_entities_entity
ON memory_file_entities(entity_id);

CREATE INDEX IF NOT EXISTS idx_memory_entity_edges_source
ON memory_entity_edges(source_entity_id, relation_type);

CREATE INDEX IF NOT EXISTS idx_memory_entity_edges_target
ON memory_entity_edges(target_entity_id, relation_type);

CREATE INDEX IF NOT EXISTS idx_memory_entity_edges_current
ON memory_entity_edges(relation_type, source_entity_id)
WHERE valid_until IS NULL;

CREATE TRIGGER IF NOT EXISTS memory_entities_au_updated_at
AFTER UPDATE OF
    entity_type, canonical_name, normalized_name, aliases,
    confidence, source, metadata
ON memory_entities
BEGIN
    UPDATE memory_entities
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS memory_entity_edges_au_updated_at
AFTER UPDATE OF
    source_entity_id, target_entity_id, relation_type, confidence,
    support_count, valid_from, valid_until, decay_weight,
    classifier_version, metadata
ON memory_entity_edges
BEGIN
    UPDATE memory_entity_edges
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS memory_file_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id TEXT UNIQUE NOT NULL,
    source_file_id INTEGER NOT NULL REFERENCES memory_files(id) ON DELETE CASCADE,
    target_file_id INTEGER NOT NULL REFERENCES memory_files(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL DEFAULT 'related_to',
    confidence REAL DEFAULT 1.0,
    support_count INTEGER NOT NULL DEFAULT 1,
    valid_from TEXT,
    valid_until TEXT,
    decay_weight REAL DEFAULT 1.0,
    classifier_version TEXT DEFAULT '',
    evidence TEXT DEFAULT '',
    metadata TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK(source_file_id <> target_file_id),
    UNIQUE(source_file_id, target_file_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_memory_file_edges_source
ON memory_file_edges(source_file_id, relation_type);

CREATE INDEX IF NOT EXISTS idx_memory_file_edges_target
ON memory_file_edges(target_file_id, relation_type);

CREATE INDEX IF NOT EXISTS idx_memory_file_edges_relation
ON memory_file_edges(relation_type, confidence DESC);

CREATE INDEX IF NOT EXISTS idx_memory_file_edges_current
ON memory_file_edges(relation_type, source_file_id)
WHERE valid_until IS NULL OR valid_until = '';

CREATE TRIGGER IF NOT EXISTS memory_file_edges_au_updated_at
AFTER UPDATE OF
    source_file_id, target_file_id, relation_type, confidence,
    support_count, valid_from, valid_until, decay_weight,
    classifier_version, evidence, metadata
ON memory_file_edges
BEGIN
    UPDATE memory_file_edges
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS memory_pyramid_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_id TEXT UNIQUE NOT NULL,
    file_id INTEGER NOT NULL REFERENCES memory_files(id) ON DELETE CASCADE,
    persona TEXT,
    level INTEGER NOT NULL CHECK(level IN (0, 1, 2)),
    level_name TEXT NOT NULL CHECK(level_name IN ('chunk', 'section', 'document')),
    ordinal INTEGER NOT NULL,
    parent_summary_id TEXT,
    source_content_hash TEXT NOT NULL,
    source_start INTEGER NOT NULL DEFAULT 0,
    source_end INTEGER NOT NULL DEFAULT 0,
    summary_text TEXT NOT NULL,
    summary_hash TEXT NOT NULL,
    summarizer_version TEXT NOT NULL DEFAULT 'chimera-memory.pyramid-summary.v1',
    metadata TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(file_id, level, ordinal, source_content_hash, summarizer_version)
);

CREATE INDEX IF NOT EXISTS idx_memory_pyramid_summaries_file
ON memory_pyramid_summaries(file_id, level, ordinal);

CREATE INDEX IF NOT EXISTS idx_memory_pyramid_summaries_persona
ON memory_pyramid_summaries(persona, level_name);

CREATE INDEX IF NOT EXISTS idx_memory_pyramid_summaries_current
ON memory_pyramid_summaries(file_id, source_content_hash, summarizer_version);

CREATE TABLE IF NOT EXISTS memory_enhancement_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')),
    persona TEXT,
    file_id INTEGER REFERENCES memory_files(id) ON DELETE SET NULL,
    path TEXT,
    content_fingerprint TEXT,
    requested_provider TEXT DEFAULT '',
    requested_model TEXT DEFAULT '',
    request_payload TEXT DEFAULT '{}',
    result_payload TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    locked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_enhancement_jobs_status
ON memory_enhancement_jobs(status, created_at);

CREATE INDEX IF NOT EXISTS idx_memory_enhancement_jobs_persona_status
ON memory_enhancement_jobs(persona, status, created_at);

CREATE INDEX IF NOT EXISTS idx_memory_enhancement_jobs_file
ON memory_enhancement_jobs(file_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_enhancement_jobs_active_file
ON memory_enhancement_jobs(file_id)
WHERE status IN ('pending', 'running');

CREATE TRIGGER IF NOT EXISTS memory_enhancement_jobs_au_updated_at
AFTER UPDATE OF
    status, requested_provider, requested_model, request_payload,
    result_payload, error, attempt_count, locked_at
ON memory_enhancement_jobs
BEGIN
    UPDATE memory_enhancement_jobs
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;
"""


def init_memory_tables(conn: sqlite3.Connection):
    """Create memory tables if they don't exist."""
    _check_memory_schema_prereqs(conn)
    conn.executescript(MEMORY_SCHEMA)
    _migrate_memory_files_schema(conn)
    conn.executescript(MEMORY_POST_MIGRATION_SCHEMA)
    conn.commit()


def _check_memory_schema_prereqs(conn: sqlite3.Connection) -> None:
    if sqlite3.sqlite_version_info < (3, 9, 0):
        version = ".".join(str(part) for part in sqlite3.sqlite_version_info)
        raise RuntimeError(f"SQLite 3.9.0+ with FTS5 support is required, found {version}")
    try:
        conn.execute("DROP TABLE IF EXISTS temp.chimera_memory_fts5_check")
        conn.execute("CREATE VIRTUAL TABLE temp.chimera_memory_fts5_check USING fts5(content)")
        conn.execute("DROP TABLE IF EXISTS temp.chimera_memory_fts5_check")
    except sqlite3.Error as exc:
        raise RuntimeError("SQLite FTS5 support is required for ChimeraMemory") from exc


def _memory_file_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_files)").fetchall()}


def _ensure_memory_file_column(
    conn: sqlite3.Connection,
    columns: set[str],
    name: str,
    column_sql: str,
) -> None:
    if name in columns:
        return
    conn.execute(f"ALTER TABLE memory_files ADD COLUMN {column_sql}")
    columns.add(name)


def _migrate_memory_files_schema(conn: sqlite3.Connection) -> None:
    columns = _memory_file_columns(conn)
    _ensure_memory_file_column(conn, columns, "idempotency_key", "idempotency_key TEXT")
    _ensure_memory_file_column(conn, columns, "content_fingerprint", "content_fingerprint TEXT")
    _ensure_memory_file_column(conn, columns, "updated_at", "updated_at TEXT")
    _ensure_memory_file_column(conn, columns, "fm_provenance_status", "fm_provenance_status TEXT")
    _ensure_memory_file_column(conn, columns, "fm_confidence", "fm_confidence REAL")
    _ensure_memory_file_column(conn, columns, "fm_lifecycle_status", "fm_lifecycle_status TEXT")
    _ensure_memory_file_column(conn, columns, "fm_review_status", "fm_review_status TEXT")
    _ensure_memory_file_column(conn, columns, "fm_sensitivity_tier", "fm_sensitivity_tier TEXT")
    _ensure_memory_file_column(conn, columns, "fm_can_use_as_instruction", "fm_can_use_as_instruction INTEGER")
    _ensure_memory_file_column(conn, columns, "fm_can_use_as_evidence", "fm_can_use_as_evidence INTEGER")
    _ensure_memory_file_column(
        conn,
        columns,
        "fm_requires_user_confirmation",
        "fm_requires_user_confirmation INTEGER",
    )
    _ensure_memory_file_column(
        conn,
        columns,
        "fm_exclude_from_default_search",
        "fm_exclude_from_default_search INTEGER",
    )
    conn.execute(
        """
        UPDATE memory_files
           SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
         WHERE updated_at IS NULL
        """
    )
    conn.execute(
        """
        UPDATE memory_files
           SET fm_provenance_status = COALESCE(fm_provenance_status, 'imported'),
               fm_lifecycle_status = COALESCE(fm_lifecycle_status, COALESCE(fm_status, 'active')),
               fm_review_status = COALESCE(fm_review_status, 'confirmed'),
               fm_sensitivity_tier = COALESCE(fm_sensitivity_tier, 'standard'),
               fm_can_use_as_instruction = COALESCE(fm_can_use_as_instruction, 1),
               fm_can_use_as_evidence = COALESCE(fm_can_use_as_evidence, 1),
               fm_requires_user_confirmation = COALESCE(fm_requires_user_confirmation, 0),
               fm_exclude_from_default_search = COALESCE(fm_exclude_from_default_search, 0)
        """
    )
