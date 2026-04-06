"""Local embedding generation and vector storage/search."""

import json
import struct
import logging
import sqlite3
from typing import Generator

log = logging.getLogger(__name__)

# Embedding model config
MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

# Lazy-loaded model singleton
_model = None


def _get_model():
    """Lazy-load the embedding model (23MB ONNX, cached after first download)."""
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(MODEL_NAME)
        log.info("Loaded embedding model: %s (%d dims)", MODEL_NAME, EMBEDDING_DIM)
    return _model


def embed_text(text: str) -> list[float]:
    """Embed a single text string. Returns a list of floats."""
    model = _get_model()
    results = list(model.embed([text]))
    return results[0].tolist()


def embed_batch(texts: list[str], batch_size: int = 64) -> Generator[list[float], None, None]:
    """Embed a batch of texts. Yields one embedding per text."""
    model = _get_model()
    for embedding in model.embed(texts, batch_size=batch_size):
        yield embedding.tolist()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors. Pure Python, no numpy."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def pack_embedding(embedding: list[float]) -> bytes:
    """Pack a float list into bytes for SQLite BLOB storage."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def unpack_embedding(blob: bytes) -> list[float]:
    """Unpack bytes back into a float list."""
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


# Schema for embedding storage
EMBEDDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript_embeddings (
    transcript_id INTEGER PRIMARY KEY,
    embedding BLOB NOT NULL,
    FOREIGN KEY (transcript_id) REFERENCES transcript(id) ON DELETE CASCADE
);
"""


def init_embedding_table(conn: sqlite3.Connection):
    """Create the embeddings table if it doesn't exist."""
    conn.execute(EMBEDDING_SCHEMA)
    conn.commit()


def store_embeddings(conn: sqlite3.Connection, entries: list[tuple[int, list[float]]]):
    """Batch store embeddings. entries = [(transcript_id, embedding_vector), ...]"""
    data = [(tid, pack_embedding(emb)) for tid, emb in entries]
    conn.executemany(
        "INSERT OR IGNORE INTO transcript_embeddings (transcript_id, embedding) VALUES (?, ?)",
        data,
    )
    conn.commit()


def vector_search(conn: sqlite3.Connection, query_embedding: list[float],
                   limit: int = 50, entry_types: list[str] | None = None) -> list[tuple[int, float]]:
    """Search for similar entries by cosine similarity.

    Returns list of (transcript_id, similarity_score) sorted by similarity descending.
    This is a brute-force scan — fine for <1M entries. For larger scale, use sqlite-vec.
    """
    # Build query with optional type filter
    if entry_types:
        placeholders = ",".join("?" * len(entry_types))
        sql = f"""
            SELECT e.transcript_id, e.embedding
            FROM transcript_embeddings e
            JOIN transcript t ON t.id = e.transcript_id
            WHERE t.entry_type IN ({placeholders})
        """
        rows = conn.execute(sql, entry_types).fetchall()
    else:
        rows = conn.execute(
            "SELECT transcript_id, embedding FROM transcript_embeddings"
        ).fetchall()

    # Compute similarities
    results = []
    for row in rows:
        tid = row[0]
        stored_emb = unpack_embedding(row[1])
        sim = cosine_similarity(query_embedding, stored_emb)
        results.append((tid, sim))

    # Sort by similarity descending, return top N
    results.sort(key=lambda x: -x[1])
    return results[:limit]


def embed_transcript_entries(db, conn: sqlite3.Connection, batch_size: int = 100):
    """Embed all transcript entries that don't have embeddings yet.

    Only embeds entries with content (skips tool_result, system, etc.).
    """
    init_embedding_table(conn)

    # Find entries needing embeddings
    rows = conn.execute("""
        SELECT t.id, t.content
        FROM transcript t
        LEFT JOIN transcript_embeddings e ON e.transcript_id = t.id
        WHERE e.transcript_id IS NULL
          AND t.content IS NOT NULL
          AND t.content != ''
          AND t.entry_type IN ('user_message', 'assistant_message', 'discord_inbound', 'discord_outbound')
    """).fetchall()

    if not rows:
        return 0

    log.info("Embedding %d transcript entries...", len(rows))

    # Process in batches
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        ids = [r[0] for r in batch]
        texts = [r[1] for r in batch]

        embeddings = list(embed_batch(texts, batch_size=batch_size))
        entries = list(zip(ids, embeddings))
        store_embeddings(conn, entries)
        total += len(entries)

        if total % 500 == 0:
            log.info("  Embedded %d / %d entries", total, len(rows))

    log.info("Embedding complete: %d entries", total)
    return total
