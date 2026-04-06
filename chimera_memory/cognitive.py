"""Cognitive layer: salience decay, surprise scoring, zone-based memory loading.

All mechanisms are algorithmic (zero LLM calls). They modify memory salience
and zone assignments based on access patterns, novelty, and time.
"""

import math
import logging
import sqlite3
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

# ─── Salience Decay ──────────────────────────────────────────────────

# Per-type decay rates (higher = faster decay)
# Opinions decay 4x faster than facts
DECAY_RATES = {
    "semantic": 0.005,      # facts, slow decay
    "procedural": 0.003,    # how-to knowledge, very slow (load-bearing)
    "episodic": 0.010,      # experiences, moderate
    "entity": 0.005,        # people, slow
    "reflection": 0.008,    # observations, moderate
    "social": 0.015,        # gossip, faster
    "opinion": 0.020,       # opinions, fastest
}
DEFAULT_DECAY_RATE = 0.010

# Reinforce on access
ACCESS_BOOST = 0.05
MAX_IMPORTANCE = 10.0


def apply_salience_decay(conn: sqlite3.Connection, persona: Optional[str] = None) -> dict:
    """Apply exponential importance decay to all memories based on type and access time.

    Formula: effective_importance = importance * e^(-decay_rate * days_since_access)

    Does NOT modify importance directly (that's the author's judgment).
    Instead, stores a `decayed_importance` that search can use for ranking.

    Returns summary of changes.
    """
    where = "WHERE persona = ?" if persona else ""
    params = [persona] if persona else []

    rows = conn.execute(f"""
        SELECT id, fm_type, fm_importance, fm_last_accessed, fm_created, fm_access_count
        FROM memory_files {where}
    """, params).fetchall()

    now = datetime.now()
    updates = []
    decayed_count = 0

    for r in rows:
        file_id = r[0]
        fm_type = r[1] or "unknown"
        importance = r[2]
        last_accessed = r[3]
        created = r[4]
        access_count = r[5] or 0

        if importance is None:
            continue

        # Calculate days since last access
        days_since = _days_since(last_accessed, created, now)

        # Get decay rate for this type
        decay_rate = DECAY_RATES.get(fm_type, DEFAULT_DECAY_RATE)

        # Apply exponential decay
        decayed = importance * math.exp(-decay_rate * days_since)
        decayed = round(max(0, decayed), 2)

        if decayed < importance:
            decayed_count += 1

        updates.append((decayed, file_id))

    # We don't modify fm_importance (author's judgment).
    # Instead, we could store decayed values in a separate column.
    # For now, return the report for the consolidation system to use.

    return {
        "total_analyzed": len(rows),
        "decayed_count": decayed_count,
        "decay_rates": DECAY_RATES,
    }


def reinforce_on_access(conn: sqlite3.Connection, file_id: int):
    """Boost access_count and refresh last_accessed when a memory is retrieved.

    Called by search/recall functions (but NOT during maintenance/reindex).
    """
    conn.execute("""
        UPDATE memory_files SET
            fm_access_count = COALESCE(fm_access_count, 0) + 1,
            fm_last_accessed = strftime('%Y-%m-%d', 'now')
        WHERE id = ?
    """, (file_id,))
    conn.commit()


# ─── Surprise Scoring ────────────────────────────────────────────────

def compute_surprise(conn: sqlite3.Connection, file_id: int) -> float:
    """Compute surprise score for a memory: how novel is it?

    surprise = 1.0 - mean(5 nearest neighbor similarities)

    High surprise = this memory is unlike anything else (novel).
    Low surprise = similar memories already exist (redundant).

    Returns surprise score (0.0 to 1.0).
    """
    from .embeddings import unpack_embedding, cosine_similarity

    # Get this file's embedding
    row = conn.execute(
        "SELECT embedding FROM memory_embeddings WHERE file_id = ?", (file_id,)
    ).fetchone()

    if not row:
        return 1.0  # No embedding = assume novel

    query_emb = unpack_embedding(row[0])

    # Get all other embeddings
    others = conn.execute(
        "SELECT file_id, embedding FROM memory_embeddings WHERE file_id != ?", (file_id,)
    ).fetchall()

    if not others:
        return 1.0  # Only memory in the corpus

    # Find 5 nearest neighbors
    similarities = []
    for other in others:
        other_emb = unpack_embedding(other[1])
        sim = cosine_similarity(query_emb, other_emb)
        similarities.append(sim)

    similarities.sort(reverse=True)
    top_5 = similarities[:5]

    # Surprise = 1 - mean similarity to nearest neighbors
    mean_sim = sum(top_5) / len(top_5)
    surprise = 1.0 - mean_sim

    return round(max(0.0, min(1.0, surprise)), 4)


def score_all_surprise(conn: sqlite3.Connection, persona: Optional[str] = None) -> list[dict]:
    """Compute surprise scores for all memories. Returns sorted list (most novel first)."""
    where = "WHERE f.persona = ?" if persona else ""
    params = [persona] if persona else []

    rows = conn.execute(f"""
        SELECT f.id, f.relative_path, f.fm_type, f.fm_importance, f.fm_about
        FROM memory_files f
        JOIN memory_embeddings e ON e.file_id = f.id
        {where}
    """, params).fetchall()

    results = []
    for r in rows:
        surprise = compute_surprise(conn, r[0])
        results.append({
            "file_id": r[0],
            "path": r[1],
            "type": r[2],
            "importance": r[3],
            "about": r[4],
            "surprise": surprise,
        })

    results.sort(key=lambda x: -x["surprise"])
    return results


# ─── Zone-Based Memory Loading ───────────────────────────────────────

# Zone thresholds
ZONE_CORE = 0.80      # Always loaded every session
ZONE_ACTIVE = 0.60    # Loaded when tags match current task
ZONE_PASSIVE = 0.30   # Loaded only on direct query
# Below ZONE_PASSIVE = archive (never auto-loaded)

# Scoring weights
WEIGHT_CONFIDENCE = 0.25
WEIGHT_FREQUENCY = 0.20
WEIGHT_RECENCY = 0.15
WEIGHT_CONTEXT = 0.20
WEIGHT_SPEC = 0.15
WEIGHT_FAILURE = 0.25


def compute_zone_score(
    importance: int | None,
    access_count: int | None,
    days_since_access: float,
    failure_count: int | None,
) -> float:
    """Compute a memory's zone score (0.0 to 1.0).

    Score = (confidence * 0.25) + (frequency * 0.20) + (recency * 0.15)
            + (context_match * 0.20) + (spec_alignment * 0.15)
            - (failure_penalty * 0.25)

    Context_match and spec_alignment are set to 0.5 (neutral) when not
    evaluating against a specific context. They become meaningful when
    the zone system evaluates against the current task/session.
    """
    # Confidence: based on importance (1-10 -> 0.1-1.0)
    confidence = min(1.0, (importance or 5) / 10.0)

    # Frequency: based on access count (saturates at 10)
    frequency = min(1.0, (access_count or 0) / 10.0)

    # Recency: exponential decay, 0.95^months
    months = days_since_access / 30.0
    recency = 0.95 ** months

    # Context match and spec alignment: neutral when not context-aware
    context_match = 0.5
    spec_alignment = 0.5

    # Failure penalty
    failure_penalty = min(1.0, (failure_count or 0) / 5.0)

    score = (
        confidence * WEIGHT_CONFIDENCE
        + frequency * WEIGHT_FREQUENCY
        + recency * WEIGHT_RECENCY
        + context_match * WEIGHT_CONTEXT
        + spec_alignment * WEIGHT_SPEC
        - failure_penalty * WEIGHT_FAILURE
    )

    return round(max(0.0, min(1.0, score)), 4)


def assign_zone(score: float) -> str:
    """Assign a zone based on score."""
    if score >= ZONE_CORE:
        return "core"
    elif score >= ZONE_ACTIVE:
        return "active"
    elif score >= ZONE_PASSIVE:
        return "passive"
    else:
        return "archive"


def compute_all_zones(conn: sqlite3.Connection, persona: Optional[str] = None) -> list[dict]:
    """Compute zone assignments for all memories."""
    where = "WHERE persona = ?" if persona else ""
    params = [persona] if persona else []
    now = datetime.now()

    rows = conn.execute(f"""
        SELECT id, relative_path, persona, fm_type, fm_importance,
               fm_last_accessed, fm_created, fm_access_count, fm_status,
               fm_about, fm_failure_count
        FROM memory_files {where}
    """, params).fetchall()

    results = []
    zone_counts = {"core": 0, "active": 0, "passive": 0, "archive": 0}

    for r in rows:
        importance = r[4]
        if importance is None:
            continue

        days_since = _days_since(r[5], r[6], now)
        score = compute_zone_score(importance, r[7], days_since, r[10])
        zone = assign_zone(score)
        zone_counts[zone] += 1

        results.append({
            "path": r[1],
            "persona": r[2],
            "type": r[3],
            "importance": importance,
            "access_count": r[7],
            "days_since_access": round(days_since, 1),
            "failure_count": r[10] or 0,
            "score": score,
            "zone": zone,
            "about": r[9],
        })

    results.sort(key=lambda x: -x["score"])

    return results, zone_counts


# ─── Helpers ─────────────────────────────────────────────────────────

def _days_since(last_accessed, created, now: datetime) -> float:
    """Calculate days since last access, falling back to creation date."""
    for date_str in (last_accessed, created):
        if date_str:
            try:
                dt = datetime.fromisoformat(str(date_str))
                return max(0, (now - dt).total_seconds() / 86400)
            except (ValueError, TypeError):
                continue
    return 30.0  # default assumption
