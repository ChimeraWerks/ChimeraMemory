"""Search and query functions for the transcript database."""

import json
import math
import logging
from datetime import datetime, timezone
from .db import TranscriptDB
from .sanitizer import build_fts_query

log = logging.getLogger(__name__)

# RRF constant (from Cormack et al. 2009)
RRF_K = 60

# Default entry types for Discord recall (skip tool noise)
DISCORD_TYPES = ("discord_inbound", "discord_outbound", "user_message", "assistant_message")
CONVERSATION_TYPES = ("user_message", "assistant_message", "discord_inbound", "discord_outbound")


def discord_recall(
    db: TranscriptDB,
    channel: str | None = None,
    limit: int = 50,
    search: str | None = None,
    after: str | None = None,
    before: str | None = None,
    direction: str | None = None,
    author: str | None = None,
    include_tool_calls: bool = False,
) -> list[dict]:
    """Query Discord conversation history from transcript.db.

    Args:
        db: TranscriptDB instance
        channel: Filter by chat_id
        limit: Max results (default 50)
        search: FTS5 search query
        after: Messages after this ISO timestamp
        before: Messages before this ISO timestamp
        direction: 'inbound', 'outbound', or None for both
        author: Filter by author name
        include_tool_calls: Include tool_call entries in results

    Returns:
        List of message dicts, chronologically ordered
    """
    if search is not None:
        search = search.strip()
        if search:
            return _fts_search(db, search, channel, limit, after, before, direction, author, include_tool_calls)
        else:
            return []  # Empty/whitespace-only search returns nothing
    return _chronological(db, channel, limit, after, before, direction, author, include_tool_calls)


def _chronological(
    db, channel, limit, after, before, direction, author, include_tool_calls
) -> list[dict]:
    """Get messages in chronological order with filters."""
    entry_types = list(CONVERSATION_TYPES)
    if include_tool_calls:
        entry_types.append("tool_call")

    conditions = []
    params = []

    # Entry type filter
    placeholders = ",".join("?" * len(entry_types))
    conditions.append(f"entry_type IN ({placeholders})")
    params.extend(entry_types)

    if channel:
        conditions.append("chat_id = ?")
        params.append(channel)

    if after:
        conditions.append("timestamp > ?")
        params.append(after)

    if before:
        conditions.append("timestamp < ?")
        params.append(before)

    if direction == "inbound":
        conditions.append("entry_type IN ('discord_inbound', 'user_message')")
    elif direction == "outbound":
        conditions.append("entry_type IN ('discord_outbound', 'assistant_message')")

    if author:
        conditions.append("author = ?")
        params.append(author)

    where = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)

    sql = f"""
        SELECT id, session_id, entry_type, timestamp, content, source,
               channel, chat_id, message_id, author, author_id, tool_name, metadata
        FROM transcript
        WHERE {where}
        ORDER BY timestamp DESC
        LIMIT ?
    """

    with db.connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    # Return in chronological order (query was DESC for LIMIT, flip it)
    results = [_row_to_dict(row) for row in reversed(rows)]
    return results


def _fts_search(
    db, query, channel, limit, after, before, direction, author, include_tool_calls
) -> list[dict]:
    """Full-text search across transcript content."""
    # Split query into terms and build safe FTS query
    terms = query.split()
    fts_query = build_fts_query(terms)
    if not fts_query:
        return []

    entry_types = list(CONVERSATION_TYPES)
    if include_tool_calls:
        entry_types.append("tool_call")

    conditions = []
    params = []

    placeholders = ",".join("?" * len(entry_types))
    conditions.append(f"t.entry_type IN ({placeholders})")
    params.extend(entry_types)

    if channel:
        conditions.append("t.chat_id = ?")
        params.append(channel)

    if after:
        conditions.append("t.timestamp > ?")
        params.append(after)

    if before:
        conditions.append("t.timestamp < ?")
        params.append(before)

    if direction == "inbound":
        conditions.append("t.entry_type IN ('discord_inbound', 'user_message')")
    elif direction == "outbound":
        conditions.append("t.entry_type IN ('discord_outbound', 'assistant_message')")

    if author:
        conditions.append("t.author = ?")
        params.append(author)

    where = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)

    sql = f"""
        SELECT t.id, t.session_id, t.entry_type, t.timestamp, t.content, t.source,
               t.channel, t.chat_id, t.message_id, t.author, t.author_id, t.tool_name, t.metadata
        FROM transcript t
        JOIN transcript_fts ON transcript_fts.rowid = t.id
        WHERE transcript_fts MATCH ?
          AND {where}
        ORDER BY rank
        LIMIT ?
    """

    # FTS query goes first in params
    all_params = [fts_query] + params

    with db.connection() as conn:
        rows = conn.execute(sql, all_params).fetchall()

    return [_row_to_dict(row) for row in rows]


def transcript_stats(db: TranscriptDB) -> dict:
    """Get comprehensive stats about the transcript database."""
    base_stats = db.stats()

    with db.connection() as conn:
        # Entry type breakdown
        type_counts = conn.execute(
            "SELECT entry_type, COUNT(*) as cnt FROM transcript GROUP BY entry_type ORDER BY cnt DESC"
        ).fetchall()

        # Source breakdown
        source_counts = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM transcript GROUP BY source ORDER BY cnt DESC"
        ).fetchall()

        # Import log
        import_count = conn.execute("SELECT COUNT(*) FROM import_log").fetchone()[0]
        last_import = conn.execute(
            "SELECT updated_at FROM import_log ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()

        # Session stats
        session_dispositions = conn.execute(
            "SELECT disposition, COUNT(*) as cnt FROM sessions GROUP BY disposition"
        ).fetchall()

    base_stats["entry_types"] = {row["entry_type"]: row["cnt"] for row in type_counts}
    base_stats["sources"] = {row["source"]: row["cnt"] for row in source_counts}
    base_stats["files_indexed"] = import_count
    base_stats["last_import"] = last_import["updated_at"] if last_import else None
    base_stats["session_dispositions"] = {
        row["disposition"] or "unknown": row["cnt"] for row in session_dispositions
    }

    return base_stats


def discord_recall_index(
    db: TranscriptDB,
    channel: str | None = None,
    limit: int = 50,
    search: str | None = None,
    after: str | None = None,
    before: str | None = None,
    direction: str | None = None,
    author: str | None = None,
) -> list[dict]:
    """Progressive disclosure Layer 1: return compact index only.

    Each result has: id, timestamp, entry_type, author, preview (first 80 chars),
    chat_id, message_id. ~50-100 tokens per result instead of ~500-1000.
    """
    results = discord_recall(
        db, channel=channel, limit=limit, search=search,
        after=after, before=before, direction=direction, author=author,
    )

    index = []
    for msg in results:
        content = msg.get("content", "") or ""
        index.append({
            "id": msg.get("id"),
            "timestamp": msg.get("timestamp", "")[:19],
            "entry_type": msg.get("entry_type"),
            "author": msg.get("author"),
            "preview": content[:80] + ("..." if len(content) > 80 else ""),
            "chat_id": msg.get("chat_id"),
            "message_id": msg.get("message_id"),
        })

    return index


def discord_detail(db: TranscriptDB, ids: list[int]) -> list[dict]:
    """Progressive disclosure Layer 2: fetch full content for specific entries.

    Call this after reviewing the index to get full content for entries you care about.
    """
    if not ids:
        return []

    placeholders = ",".join("?" * len(ids))
    sql = f"""
        SELECT id, session_id, entry_type, timestamp, content, source,
               channel, chat_id, message_id, author, author_id, tool_name, metadata
        FROM transcript
        WHERE id IN ({placeholders})
        ORDER BY timestamp ASC
    """

    with db.connection() as conn:
        rows = conn.execute(sql, ids).fetchall()

    return [_row_to_dict(row) for row in rows]


def session_list(
    db: TranscriptDB,
    limit: int = 20,
    after: str | None = None,
    before: str | None = None,
    persona: str | None = None,
    disposition: str | None = None,
) -> list[dict]:
    """List sessions with summaries, dispositions, and date ranges.

    Args:
        db: TranscriptDB instance
        limit: Max sessions to return (default 20, most recent first)
        after: Sessions started after this ISO timestamp
        before: Sessions started before this ISO timestamp
        persona: Filter by persona name
        disposition: Filter by COMPLETED/IN_PROGRESS/INTERRUPTED
    """
    conditions = []
    params = []

    if after:
        conditions.append("started_at > ?")
        params.append(after)
    if before:
        conditions.append("started_at < ?")
        params.append(before)
    if persona:
        conditions.append("persona = ?")
        params.append(persona)
    if disposition:
        conditions.append("disposition = ?")
        params.append(disposition)

    where = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)

    sql = f"""
        SELECT session_id, persona, title, git_branch, cwd,
               started_at, ended_at, exchange_count, disposition
        FROM sessions
        WHERE {where}
        ORDER BY started_at DESC
        LIMIT ?
    """

    with db.connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    return [dict(row) for row in rows]


def consolidate_old_entries(db: TranscriptDB, retention_days: int):
    """Compress old transcript entries into permanent session summaries, then prune raw entries.

    Entries older than retention_days are:
    1. Summarized (if their session doesn't have a summary yet)
    2. Deleted from the transcript table

    Session summaries persist forever as Layer 3.
    """
    if retention_days <= 0:
        return 0  # 0 means keep everything

    from .summarizer import summarize_session

    cutoff = f"datetime('now', '-{retention_days} days')"

    with db.connection() as conn:
        # Find sessions with entries older than cutoff
        old_sessions = conn.execute(f"""
            SELECT DISTINCT session_id FROM transcript
            WHERE timestamp < {cutoff}
        """).fetchall()

    if not old_sessions:
        return 0

    # Ensure summaries exist for these sessions
    for row in old_sessions:
        sid = row["session_id"]
        with db.connection() as conn:
            session = conn.execute(
                "SELECT disposition FROM sessions WHERE session_id = ?", (sid,)
            ).fetchone()

        if not session or not session["disposition"]:
            summary = summarize_session(db, sid)
            with db.connection() as conn:
                conn.execute(
                    """UPDATE sessions SET
                        disposition = ?,
                        exchange_count = ?
                    WHERE session_id = ?""",
                    (summary["disposition"], summary["exchange_count"], sid),
                )
                conn.commit()

    # Prune old raw entries (summaries in sessions table persist)
    with db.connection() as conn:
        result = conn.execute(f"""
            DELETE FROM transcript
            WHERE timestamp < {cutoff}
        """)
        pruned = result.rowcount
        conn.commit()

        if pruned:
            # Rebuild FTS after bulk delete
            conn.execute("INSERT INTO transcript_fts(transcript_fts) VALUES('rebuild')")
            conn.commit()

    if pruned:
        log.info("Consolidated %d old entries from %d sessions", pruned, len(old_sessions))
    return pruned


def hybrid_search(
    db: TranscriptDB,
    query: str,
    limit: int = 20,
    channel: str | None = None,
    after: str | None = None,
    before: str | None = None,
    entry_types: list[str] | None = None,
) -> list[dict]:
    """Hybrid search: merge FTS5 keyword results + vector similarity via RRF.

    Catches both exact keyword matches AND semantic matches
    (e.g. "car" finds "vehicle").

    Falls back to FTS-only if embeddings aren't available.
    """
    if not query or not query.strip():
        return []

    if not entry_types:
        entry_types = list(CONVERSATION_TYPES)

    # Over-fetch from both channels (3x limit for RRF merge)
    pool_size = limit * 3

    # Channel 1: FTS5 keyword search
    fts_results = _fts_search(
        db, query, channel, pool_size, after, before, None, None, False,
    )
    fts_ranking = {r["id"]: rank for rank, r in enumerate(fts_results)}

    # Channel 2: Vector similarity search
    vec_ranking = {}
    try:
        from .embeddings import embed_text, vector_search, init_embedding_table

        query_emb = embed_text(query)
        with db.connection() as conn:
            init_embedding_table(conn)
            vec_results = vector_search(conn, query_emb, limit=pool_size, entry_types=entry_types)
        vec_ranking = {tid: rank for rank, (tid, _score) in enumerate(vec_results)}
    except Exception as e:
        log.debug("Vector search unavailable, using FTS only: %s", e)

    # RRF merge
    all_ids = set(fts_ranking.keys()) | set(vec_ranking.keys())
    if not all_ids:
        return []

    rrf_scores = {}
    for doc_id in all_ids:
        score = 0.0
        if doc_id in fts_ranking:
            score += 1.0 / (RRF_K + fts_ranking[doc_id] + 1)
        if doc_id in vec_ranking:
            score += 1.0 / (RRF_K + vec_ranking[doc_id] + 1)
        rrf_scores[doc_id] = score

    # Sort by RRF score descending
    sorted_ids = sorted(rrf_scores.keys(), key=lambda x: -rrf_scores[x])[:limit]

    # Fetch full entries
    if not sorted_ids:
        return []

    placeholders = ",".join("?" * len(sorted_ids))
    sql = f"""
        SELECT id, session_id, entry_type, timestamp, content, source,
               channel, chat_id, message_id, author, author_id, tool_name, metadata
        FROM transcript
        WHERE id IN ({placeholders})
    """

    with db.connection() as conn:
        rows = conn.execute(sql, sorted_ids).fetchall()

    # Re-sort by RRF score (DB returns in arbitrary order)
    results = [_row_to_dict(row) for row in rows]
    results.sort(key=lambda r: -rrf_scores.get(r.get("id", 0), 0))

    # Apply re-ranking
    results = _rerank(results)

    return results


def _rerank(results: list[dict]) -> list[dict]:
    """Post-RRF re-ranking with contextual signals.

    Multiplicative scoring:
    - Recency: 2^(-days/14), floor 0.1 (14-day half-life)
    - Session affinity: boost results from the most recent session
    - Observation richness: 1 + log1p(content_length) / 10
    """
    if not results:
        return results

    now = datetime.now(timezone.utc)
    most_recent_session = results[0].get("session_id") if results else None

    scored = []
    for r in results:
        score = 1.0

        # Recency signal
        ts_str = r.get("timestamp", "")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                days_ago = (now - ts).total_seconds() / 86400
                recency = max(0.1, 2 ** (-days_ago / 14))
                score *= recency
            except (ValueError, TypeError):
                pass

        # Session affinity (boost same-session results)
        if r.get("session_id") == most_recent_session:
            score *= 1.3

        # Observation richness (longer content = richer)
        content_len = len(r.get("content", "") or "")
        if content_len > 0:
            richness = 1 + math.log1p(content_len) / 10
            score *= richness

        scored.append((score, r))

    # Re-sort by adjusted score
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored]


def _row_to_dict(row) -> dict:
    """Convert a sqlite3.Row to a clean dict."""
    d = dict(row)
    # Parse metadata JSON
    if d.get("metadata"):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except (json.JSONDecodeError, TypeError):
            pass
    # Remove None values for cleaner output
    return {k: v for k, v in d.items() if v is not None}
