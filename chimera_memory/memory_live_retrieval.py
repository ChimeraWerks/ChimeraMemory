"""Local live-retrieval planning helpers."""

from __future__ import annotations

import re
import sqlite3

from .memory_observability import record_memory_audit_event, record_memory_recall_trace
from .sanitizer import build_fts_query

LIVE_RETRIEVAL_SCHEMA_VERSION = "chimera-memory.live-retrieval.v1"

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_STOPWORDS = {
    "about", "after", "again", "also", "and", "are", "because", "been",
    "but", "can", "could", "did", "does", "done", "for", "from", "had",
    "has", "have", "how", "into", "just", "like", "more", "need", "not",
    "now", "our", "out", "over", "should", "that", "the", "then", "this",
    "through", "use", "was", "what", "when", "where", "which", "with",
    "would", "you", "your",
}
_BLOCKED_LIFECYCLE = {"disputed", "rejected"}


def extract_live_retrieval_terms(text: str, *, limit: int = 10) -> list[str]:
    """Extract stable keyword cues from live context."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for match in _WORD_RE.finditer(text or ""):
        term = match.group(0).strip("-_").lower()
        if len(term) < 3 or term in _STOPWORDS:
            continue
        if term not in counts:
            order.append(term)
            counts[term] = 0
        counts[term] += 1
    first_pos = {term: index for index, term in enumerate(order)}
    order.sort(key=lambda item: (-counts[item], first_pos[item]))
    return order[: max(0, limit)]


def build_live_retrieval_plan(
    *,
    current_context: str,
    previous_context: str = "",
    shift_threshold: float = 0.55,
    min_terms: int = 2,
    force: bool = False,
) -> dict:
    """Decide whether a context shift should trigger recall."""
    current_terms = extract_live_retrieval_terms(current_context)
    previous_terms = extract_live_retrieval_terms(previous_context)
    current_set = set(current_terms)
    previous_set = set(previous_terms)
    if not current_set:
        shift_score = 0.0
    elif not previous_set:
        shift_score = 1.0
    else:
        shift_score = 1.0 - (len(current_set & previous_set) / len(current_set | previous_set))
    should_retrieve = force or (len(current_terms) >= min_terms and shift_score >= shift_threshold)
    return {
        "schema_version": LIVE_RETRIEVAL_SCHEMA_VERSION,
        "current_terms": current_terms,
        "previous_terms": previous_terms,
        "query_terms": current_terms[:8],
        "query_text": " ".join(current_terms[:8]),
        "shift_score": round(shift_score, 4),
        "shift_threshold": shift_threshold,
        "should_retrieve": should_retrieve,
        "force": force,
    }


def memory_live_retrieval_check(
    conn: sqlite3.Connection,
    *,
    current_context: str,
    previous_context: str = "",
    persona: str | None = None,
    limit: int = 5,
    shift_threshold: float = 0.55,
    force: bool = False,
    include_restricted: bool = False,
    actor: str = "system",
) -> dict:
    """Run a local proactive recall check and return suggestions without injecting them."""
    plan = build_live_retrieval_plan(
        current_context=current_context,
        previous_context=previous_context,
        shift_threshold=shift_threshold,
        force=force,
    )
    if not plan["should_retrieve"]:
        record_memory_audit_event(
            conn,
            "memory_live_retrieval_skipped",
            persona=persona,
            target_kind="memory_live_retrieval",
            target_id="skipped",
            payload=plan,
            actor=actor,
        )
        return {"ok": True, "retrieved": False, "reason": "no_topic_shift", "plan": plan, "results": []}

    fts_query = build_fts_query(plan["query_terms"])
    if not fts_query:
        record_memory_audit_event(
            conn,
            "memory_live_retrieval_skipped",
            persona=persona,
            target_kind="memory_live_retrieval",
            target_id="empty_query",
            payload=plan,
            actor=actor,
        )
        return {"ok": True, "retrieved": False, "reason": "empty_query", "plan": plan, "results": []}

    conditions = ["memory_fts MATCH ?", "COALESCE(f.fm_can_use_as_evidence, 1) = 1"]
    params: list[object] = [fts_query]
    if persona:
        conditions.append("f.persona = ?")
        params.append(persona)
    if not include_restricted:
        conditions.append("COALESCE(f.fm_sensitivity_tier, 'standard') <> 'restricted'")
    placeholders = ",".join("?" * len(_BLOCKED_LIFECYCLE))
    conditions.append(f"COALESCE(f.fm_lifecycle_status, 'active') NOT IN ({placeholders})")
    params.extend(sorted(_BLOCKED_LIFECYCLE))
    rows = conn.execute(
        f"""
        SELECT f.id, f.path, f.persona, f.relative_path, f.fm_type,
               f.fm_importance, f.fm_status, f.fm_about,
               snippet(memory_fts, 3, '>>>', '<<<', '...', 32) AS snippet,
               rank
        FROM memory_fts
        JOIN memory_files f ON f.id = memory_fts.rowid
        WHERE {' AND '.join(conditions)}
        ORDER BY rank
        LIMIT ?
        """,
        params + [max(0, min(int(limit), 50))],
    ).fetchall()
    results = [
        {
            "id": row[0],
            "path": row[1],
            "persona": row[2],
            "relative_path": row[3],
            "type": row[4],
            "importance": row[5],
            "status": row[6],
            "about": row[7],
            "snippet": row[8],
            "ranking_score": row[9],
        }
        for row in rows
    ]
    trace_id = record_memory_recall_trace(
        conn,
        tool_name="memory_live_retrieval",
        query_text=plan["query_text"],
        persona=persona,
        requested_limit=limit,
        results=results,
        request_payload={
            "current_context_chars": len(current_context or ""),
            "previous_context_chars": len(previous_context or ""),
            "plan": plan,
            "include_restricted": include_restricted,
        },
        response_policy={
            "mode": "proactive_dry_run",
            "ranking": "fts5_rank",
            "silent_on_miss": True,
            "injects_into_prompt": False,
        },
    )
    event_type = "memory_live_retrieval_suggested" if results else "memory_live_retrieval_miss"
    record_memory_audit_event(
        conn,
        event_type,
        persona=persona,
        target_kind="memory_live_retrieval",
        target_id=trace_id,
        trace_id=trace_id,
        payload={"result_count": len(results), "plan": plan},
        actor=actor,
    )
    return {"ok": True, "retrieved": True, "trace_id": trace_id, "plan": plan, "results": results}
