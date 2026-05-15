"""Curated memory system: index, search, and manage persona memory files.

Ported from the original chimera-memory MCP server. Indexes markdown files
with YAML frontmatter, provides FTS5 + semantic search, gap detection,
and consolidation analysis.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .memory_enhancement_queue import (
    ENHANCEMENT_JOB_STATUSES,
    memory_enhancement_claim_next,
    memory_enhancement_complete,
    memory_enhancement_enqueue,
)
from .memory_entities import (
    ENTITY_TYPES,
    MENTION_ROLES,
    apply_enhancement_entities,
    memory_entity_connections,
    memory_entity_edge_query,
    memory_entity_index,
    memory_entity_query,
    memory_file_entity_links,
    normalize_entity_name,
    upsert_memory_entity,
    upsert_memory_entity_edge,
)
from .memory_frontmatter import parse_frontmatter
from .memory_file_edges import (
    MEMORY_FILE_EDGE_RELATION_TYPES,
    memory_file_edge_query,
    memory_file_edge_temporal_sweep,
    memory_file_edge_upsert,
)
from .memory_import_chatgpt import memory_import_chatgpt_export as _memory_import_chatgpt_export
from .memory_import_gmail import memory_import_gmail_mbox as _memory_import_gmail_mbox
from .memory_import_grok import memory_import_grok_export as _memory_import_grok_export
from .memory_import_instagram import memory_import_instagram_export as _memory_import_instagram_export
from .memory_import_obsidian import memory_import_obsidian_vault as _memory_import_obsidian_vault
from .memory_import_perplexity import memory_import_perplexity_export as _memory_import_perplexity_export
from .memory_import_twitter import memory_import_twitter_archive as _memory_import_twitter_archive
from .memory_governance import (
    INSTRUCTION_GRADE_PROVENANCE,
    LIFECYCLE_STATUSES,
    PROVENANCE_STATUSES,
    REVIEW_STATUSES,
    SENSITIVITY_TIERS,
    governance_from_frontmatter,
)
from .memory_live_retrieval import memory_live_retrieval_check
from .memory_profile_export import memory_profile_export
from .memory_pyramid import memory_pyramid_summary_build, memory_pyramid_summary_query
from .memory_observability import (
    _json_object,
    _json_text,
    memory_audit_query,
    memory_recall_trace_query,
    record_memory_audit_event,
    record_memory_recall_trace,
)
from .memory_auto_capture import build_auto_capture_plan, write_auto_capture_file
from .memory_review import REVIEW_ACTIONS, memory_review_action, memory_review_pending
from .memory_schema import init_memory_tables

log = logging.getLogger(__name__)

# Config
MEMORY_DIRS = {"memory", "reading", "shared"}
INDEX_EXTENSIONS = {".md"}
SKIP_DIRS = {".git", ".obsidian", ".claude", "__pycache__", "node_modules", ".chimera"}

# Consolidation thresholds
IMPORTANCE_DECAY_RATE = 0.05
MIN_IMPORTANCE_ACTIVE = 3
MIN_IMPORTANCE_STALE = 1
CONSOLIDATION_AGE_DAYS = 7

# Helpers
_FINGERPRINT_WHITESPACE_RE = re.compile(r"\s+")


def normalized_content_fingerprint(text: str) -> str:
    """Return OB1-style normalized SHA256 for duplicate-content detection."""
    normalized = _FINGERPRINT_WHITESPACE_RE.sub(" ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_for_fts(text: str) -> str:
    """Expand text for better FTS5 matching.

    Splits CamelCase and file paths into separate tokens.
    """

    def expand_camel(match):
        word = match.group(0)
        parts = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", word)
        return f"{word} {parts}" if parts != word else word

    def expand_path(match):
        path = match.group(0)
        segments = re.split(r"[/\\]", path)
        segments = [s for s in segments if s and s not in ("", "C:")]
        return f"{path} {' '.join(segments)}"

    result = re.sub(r"[A-Za-z]:[/\\][^\s,;)}\]]+", expand_path, text)
    result = re.sub(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b", expand_camel, result)
    return result

# â”€â”€â”€ File Discovery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def discover_files(personas_dir: Path) -> list[tuple[str, str, Path]]:
    """Discover indexable markdown files for the current persona only.

    When TRANSCRIPT_PERSONA env var is set, only files belonging to that persona
    (plus shared/) are indexed. This enforces per-persona privacy: each persona
    sees its own memory + shared content, never another persona's files.

    When TRANSCRIPT_PERSONA is unset, walks all personas (legacy / multi-persona
    aggregation use case). The MCP-server-per-persona deployment should always
    set the env var.

    Returns [(persona, relative_path, full_path)].
    """
    import os
    results = []
    if not personas_dir.exists():
        return results

    scope_persona = os.environ.get("TRANSCRIPT_PERSONA", "").strip()

    for persona_dir in personas_dir.iterdir():
        if not persona_dir.is_dir() or persona_dir.name.startswith("."):
            continue
        for sub in persona_dir.iterdir():
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            if scope_persona and sub.name != scope_persona:
                continue
            _walk_for_files(sub, sub.name, sub, results)

    shared_dir = personas_dir.parent / "shared"
    if shared_dir.exists():
        _walk_for_files(shared_dir, "shared", shared_dir, results)

    return results


def cleanup_other_personas(conn, scope_persona: str) -> dict:
    """Delete memory rows belonging to other personas.

    Used to enforce the privacy boundary on existing data when TRANSCRIPT_PERSONA
    scope changes. Removes from memory_files, memory_embeddings, memory_fts.
    The 'shared' persona is preserved.

    Returns {'memory_files': N, 'memory_embeddings': N, 'memory_fts': N} counts.
    """
    if not scope_persona:
        return {"error": "scope_persona required"}

    cur = conn.cursor()
    counts = {}

    # Find file IDs to delete (everything except scope_persona and shared)
    cur.execute(
        "SELECT id FROM memory_files WHERE persona NOT IN (?, 'shared')",
        (scope_persona,),
    )
    ids_to_delete = [row[0] for row in cur.fetchall()]

    if not ids_to_delete:
        return {"memory_files": 0, "memory_embeddings": 0, "memory_fts": 0}

    placeholders = ",".join("?" * len(ids_to_delete))

    cur.execute(
        f"DELETE FROM memory_embeddings WHERE file_id IN ({placeholders})",
        ids_to_delete,
    )
    counts["memory_embeddings"] = cur.rowcount

    cur.execute(
        f"DELETE FROM memory_fts WHERE rowid IN ({placeholders})",
        ids_to_delete,
    )
    counts["memory_fts"] = cur.rowcount

    cur.execute(
        f"DELETE FROM memory_files WHERE id IN ({placeholders})",
        ids_to_delete,
    )
    counts["memory_files"] = cur.rowcount

    conn.commit()
    return counts


def _walk_for_files(directory: Path, persona: str, base: Path, results: list):
    for item in directory.iterdir():
        if item.name in SKIP_DIRS:
            continue
        if item.is_dir():
            _walk_for_files(item, persona, base, results)
        elif item.is_file() and item.suffix in INDEX_EXTENSIONS:
            rel = str(item.relative_to(base)).replace("\\", "/")
            results.append((persona, rel, item))


# â”€â”€â”€ Indexing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def index_file(conn: sqlite3.Connection, persona: str, relative_path: str,
               full_path: Path, maintenance: bool = False) -> bool:
    """Index a single memory file. Returns True if new or updated.

    Args:
        maintenance: If True, don't bump access counters (anti-inflation).
    """
    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    content_fingerprint = normalized_content_fingerprint(content)
    path_str = str(full_path).replace("\\", "/")

    row = conn.execute(
        "SELECT id, content_hash FROM memory_files WHERE path = ?", (path_str,)
    ).fetchone()

    if row and row[1] == content_hash:
        return False

    fm, body = parse_frontmatter(content)
    tags_json = json.dumps(fm.get("tags", []))
    governance = governance_from_frontmatter(fm)
    now = time.time()

    if row:
        file_id = row[0]
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (file_id,))
        conn.execute("""
            UPDATE memory_files SET
                content_hash=?, indexed_at=?,
                fm_type=?, fm_importance=?, fm_created=?, fm_last_accessed=?,
                fm_access_count=?, fm_status=?, fm_about=?, fm_tags=?,
                fm_entity=?, fm_relationship_temperature=?, fm_trust_level=?,
                fm_trend=?, fm_failure_count=?, content_fingerprint=?,
                fm_provenance_status=?, fm_confidence=?, fm_lifecycle_status=?,
                fm_review_status=?, fm_sensitivity_tier=?,
                fm_can_use_as_instruction=?, fm_can_use_as_evidence=?,
                fm_requires_user_confirmation=?
            WHERE id=?
        """, (
            content_hash, now,
            fm.get("type"), fm.get("importance"), fm.get("created"),
            fm.get("last_accessed"), fm.get("access_count", 0),
            fm.get("status", "active"), fm.get("about"), tags_json,
            fm.get("entity"), fm.get("relationship_temperature"),
            fm.get("trust_level"), fm.get("trend"),
            fm.get("failure_count", 0), content_fingerprint,
            governance["provenance_status"], governance["confidence"],
            governance["lifecycle_status"], governance["review_status"],
            governance["sensitivity_tier"], governance["can_use_as_instruction"],
            governance["can_use_as_evidence"], governance["requires_user_confirmation"],
            file_id
        ))
    else:
        cursor = conn.execute("""
            INSERT INTO memory_files (
                path, persona, relative_path, content_hash, indexed_at,
                fm_type, fm_importance, fm_created, fm_last_accessed,
                fm_access_count, fm_status, fm_about, fm_tags,
                fm_entity, fm_relationship_temperature, fm_trust_level,
                fm_trend, fm_failure_count, content_fingerprint,
                fm_provenance_status, fm_confidence, fm_lifecycle_status,
                fm_review_status, fm_sensitivity_tier,
                fm_can_use_as_instruction, fm_can_use_as_evidence,
                fm_requires_user_confirmation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            path_str, persona, relative_path, content_hash, now,
            fm.get("type"), fm.get("importance"), fm.get("created"),
            fm.get("last_accessed"), fm.get("access_count", 0),
            fm.get("status", "active"), fm.get("about"), tags_json,
            fm.get("entity"), fm.get("relationship_temperature"),
            fm.get("trust_level"), fm.get("trend"),
            fm.get("failure_count", 0), content_fingerprint,
            governance["provenance_status"], governance["confidence"],
            governance["lifecycle_status"], governance["review_status"],
            governance["sensitivity_tier"], governance["can_use_as_instruction"],
            governance["can_use_as_evidence"], governance["requires_user_confirmation"],
        ))
        file_id = cursor.lastrowid

    fts_body = normalize_for_fts(body)
    conn.execute("""
        INSERT INTO memory_fts (rowid, path, persona, relative_path, content, fm_type, fm_tags, fm_about)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (file_id, path_str, persona, relative_path, fts_body, fm.get("type", ""), tags_json, fm.get("about", "")))

    return True


def full_reindex(conn: sqlite3.Connection, personas_dir: Path, embed: bool = True) -> int:
    """Full reindex of all persona memory files."""
    files = discover_files(personas_dir)
    updated = 0
    updated_ids = []

    for persona, rel, full_path in files:
        if index_file(conn, persona, rel, full_path, maintenance=True):
            updated += 1
            row = conn.execute("SELECT id FROM memory_files WHERE path = ?",
                               (str(full_path).replace("\\", "/"),)).fetchone()
            if row:
                updated_ids.append(row[0])
    conn.commit()

    # Clean up deleted files
    indexed_paths = {str(fp).replace("\\", "/") for _, _, fp in files}
    rows = conn.execute("SELECT id, path FROM memory_files").fetchall()
    for file_id, path in rows:
        if path not in indexed_paths:
            conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (file_id,))
            conn.execute("DELETE FROM memory_embeddings WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM memory_files WHERE id = ?", (file_id,))
    conn.commit()

    if embed and updated_ids:
        embed_memory_files(conn, updated_ids)
    if embed:
        missing = conn.execute("""
            SELECT f.id FROM memory_files f
            LEFT JOIN memory_embeddings e ON e.file_id = f.id
            WHERE e.file_id IS NULL
        """).fetchall()
        missing_ids = [r[0] for r in missing if r[0] not in updated_ids]
        if missing_ids:
            embed_memory_files(conn, missing_ids)

    return updated


def embed_memory_files(conn: sqlite3.Connection, file_ids: list[int]):
    """Generate and store embeddings for memory files using fastembed."""
    if not file_ids:
        return

    from .embeddings import embed_batch, pack_embedding

    placeholders = ",".join("?" * len(file_ids))
    rows = conn.execute(f"""
        SELECT id, path, persona, relative_path, fm_type, fm_about, fm_tags
        FROM memory_files WHERE id IN ({placeholders})
    """, file_ids).fetchall()

    texts = []
    ids = []
    for r in rows:
        path = Path(r[1])
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            _, body = parse_frontmatter(content)
        except OSError:
            body = ""

        text_parts = [f"persona:{r[2]}", f"file:{r[3]}"]
        if r[4]:
            text_parts.append(f"type:{r[4]}")
        if r[5]:
            text_parts.append(f"about:{r[5]}")
        if r[6]:
            tags = json.loads(r[6]) if r[6] else []
            if tags:
                text_parts.append(f"tags:{','.join(str(t) for t in tags)}")
        text_parts.append(body[:2000])
        texts.append(" ".join(text_parts))
        ids.append(r[0])

    if not texts:
        return

    log.info("Embedding %d memory files...", len(texts))
    now = time.time()

    for file_id, emb in zip(ids, embed_batch(texts)):
        conn.execute("""
            INSERT OR REPLACE INTO memory_embeddings (file_id, embedding, embedded_at)
            VALUES (?, ?, ?)
        """, (file_id, pack_embedding(emb), now))
    conn.commit()


# â”€â”€â”€ Search Tools â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def memory_search(conn: sqlite3.Connection, query: str, persona: Optional[str] = None, limit: int = 20) -> list[dict]:
    """Full-text search across memory files."""
    from .cognitive import reinforce_on_access

    if persona:
        rows = conn.execute("""
            SELECT f.id, f.path, f.persona, f.relative_path, f.fm_type, f.fm_importance,
                   f.fm_status, snippet(memory_fts, 3, '>>>', '<<<', '...', 40) as snippet
            FROM memory_fts
            JOIN memory_files f ON f.id = memory_fts.rowid
            WHERE memory_fts MATCH ? AND f.persona = ?
            ORDER BY rank LIMIT ?
        """, (query, persona, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT f.id, f.path, f.persona, f.relative_path, f.fm_type, f.fm_importance,
                   f.fm_status, snippet(memory_fts, 3, '>>>', '<<<', '...', 40) as snippet
            FROM memory_fts
            JOIN memory_files f ON f.id = memory_fts.rowid
            WHERE memory_fts MATCH ?
            ORDER BY rank LIMIT ?
        """, (query, limit)).fetchall()

    for r in rows:
        reinforce_on_access(conn, r[0])

    results = [
        {"id": r[0], "path": r[1], "persona": r[2], "relative_path": r[3], "type": r[4],
         "importance": r[5], "status": r[6], "snippet": r[7]}
        for r in rows
    ]
    record_memory_recall_trace(
        conn,
        tool_name="memory_search",
        query_text=query,
        persona=persona,
        requested_limit=limit,
        results=results,
        request_payload={"query": query, "persona": persona, "limit": limit},
        response_policy={"ranking": "fts5_rank", "returned": "all_results"},
    )
    return results


def memory_query(
    conn: sqlite3.Connection, persona: Optional[str] = None,
    fm_type: Optional[str] = None, min_importance: Optional[int] = None,
    max_importance: Optional[int] = None, status: Optional[str] = None,
    tag: Optional[str] = None, about: Optional[str] = None,
    sort_by: str = "importance", sort_order: str = "DESC", limit: int = 50,
) -> list[dict]:
    """Structured query against frontmatter fields."""
    conditions, params = [], []

    if persona:
        conditions.append("persona = ?"); params.append(persona)
    if fm_type:
        conditions.append("fm_type = ?"); params.append(fm_type)
    if min_importance is not None:
        conditions.append("fm_importance >= ?"); params.append(min_importance)
    if max_importance is not None:
        conditions.append("fm_importance <= ?"); params.append(max_importance)
    if status:
        conditions.append("fm_status = ?"); params.append(status)
    if tag:
        conditions.append("fm_tags LIKE ?"); params.append(f"%{tag}%")
    if about:
        conditions.append("fm_about LIKE ?"); params.append(f"%{about}%")

    where = " AND ".join(conditions) if conditions else "1=1"
    valid_sorts = {
        "importance": "fm_importance", "created": "fm_created",
        "last_accessed": "fm_last_accessed", "access_count": "fm_access_count",
        "trust_level": "fm_trust_level", "relationship_temperature": "fm_relationship_temperature",
    }
    sort_col = valid_sorts.get(sort_by, "fm_importance")
    order = "ASC" if sort_order.upper() == "ASC" else "DESC"

    rows = conn.execute(f"""
        SELECT path, persona, relative_path, fm_type, fm_importance,
               fm_created, fm_last_accessed, fm_access_count, fm_status,
               fm_about, fm_tags, fm_entity, fm_relationship_temperature,
               fm_trust_level, fm_trend, fm_failure_count,
               fm_provenance_status, fm_confidence, fm_lifecycle_status,
               fm_review_status, fm_sensitivity_tier,
               fm_can_use_as_instruction, fm_can_use_as_evidence,
               fm_requires_user_confirmation
        FROM memory_files WHERE {where}
        ORDER BY {sort_col} {order} NULLS LAST LIMIT ?
    """, params + [limit]).fetchall()

    return [
        {"path": r[0], "persona": r[1], "relative_path": r[2], "type": r[3],
         "importance": r[4], "created": r[5], "last_accessed": r[6],
         "access_count": r[7], "status": r[8], "about": r[9],
         "tags": json.loads(r[10]) if r[10] else [], "entity": r[11],
         "relationship_temperature": r[12], "trust_level": r[13],
         "trend": r[14], "failure_count": r[15],
         "provenance_status": r[16], "confidence": r[17],
         "lifecycle_status": r[18], "review_status": r[19],
         "sensitivity_tier": r[20], "can_use_as_instruction": bool(r[21]),
         "can_use_as_evidence": bool(r[22]),
         "requires_user_confirmation": bool(r[23])}
        for r in rows
    ]


def memory_recall(conn: sqlite3.Connection, concept: str, persona: Optional[str] = None, limit: int = 10) -> list[dict]:
    """Semantic recall: find memories most similar to a concept."""
    from .embeddings import embed_text, unpack_embedding, cosine_similarity

    query_emb = embed_text(concept)

    if persona:
        rows = conn.execute("""
            SELECT f.id, f.path, f.persona, f.relative_path, f.fm_type,
                   f.fm_importance, f.fm_status, f.fm_about, e.embedding
            FROM memory_files f
            JOIN memory_embeddings e ON e.file_id = f.id
            WHERE f.persona = ?
        """, (persona,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT f.id, f.path, f.persona, f.relative_path, f.fm_type,
                   f.fm_importance, f.fm_status, f.fm_about, e.embedding
            FROM memory_files f
            JOIN memory_embeddings e ON e.file_id = f.id
        """).fetchall()

    scored = []
    for r in rows:
        emb = unpack_embedding(r[8])
        sim = cosine_similarity(query_emb, emb)
        scored.append((sim, r))

    scored.sort(key=lambda x: -x[0])
    top = scored[:limit]

    from .cognitive import reinforce_on_access
    for _, r in top:
        reinforce_on_access(conn, r[0])

    results = [
        {"id": r[0], "path": r[1], "persona": r[2], "relative_path": r[3], "type": r[4],
         "importance": r[5], "status": r[6], "about": r[7], "similarity": round(sim, 4)}
        for sim, r in top
    ]
    record_memory_recall_trace(
        conn,
        tool_name="memory_recall",
        query_text=concept,
        persona=persona,
        requested_limit=limit,
        results=results,
        request_payload={"concept": concept, "persona": persona, "limit": limit},
        response_policy={"ranking": "embedding_cosine", "returned": "top_limit"},
    )
    return results


def memory_stats(conn: sqlite3.Connection, persona: Optional[str] = None) -> dict:
    """Get memory corpus statistics."""
    where = "WHERE persona = ?" if persona else ""
    params = [persona] if persona else []

    total = conn.execute(f"SELECT COUNT(*) FROM memory_files {where}", params).fetchone()[0]
    by_type = conn.execute(f"SELECT fm_type, COUNT(*) FROM memory_files {where} GROUP BY fm_type ORDER BY COUNT(*) DESC", params).fetchall()
    by_status = conn.execute(f"SELECT fm_status, COUNT(*) FROM memory_files {where} GROUP BY fm_status ORDER BY COUNT(*) DESC", params).fetchall()
    by_persona = conn.execute("SELECT persona, COUNT(*) FROM memory_files GROUP BY persona ORDER BY COUNT(*) DESC").fetchall()

    return {
        "total_files": total,
        "by_type": {r[0] or "unknown": r[1] for r in by_type},
        "by_status": {r[0] or "unknown": r[1] for r in by_status},
        "by_persona": {r[0]: r[1] for r in by_persona},
    }


def memory_gaps(conn: sqlite3.Connection, persona: Optional[str] = None) -> dict:
    """Detect knowledge gaps using graph analysis."""
    try:
        import networkx as nx
    except ImportError:
        return {"error": "networkx not installed. pip install networkx"}

    where = "WHERE persona = ?" if persona else ""
    params = [persona] if persona else []

    rows = conn.execute(f"""
        SELECT id, path, persona, relative_path, fm_type, fm_importance, fm_tags, fm_about
        FROM memory_files {where}
    """, params).fetchall()

    if not rows:
        return {"error": "No files found", "gaps": [], "clusters": [], "bridges": []}

    G = nx.Graph()
    file_concepts = {}

    for r in rows:
        file_id, rel_path = r[0], r[3]
        fm_type = r[4] or "unknown"
        tags = json.loads(r[6]) if r[6] else []
        about = str(r[7]) if r[7] else ""

        concepts = set()
        for tag in tags:
            concepts.add(str(tag).lower())
        if about:
            concepts.add(about.lower())
        concepts.add(fm_type.lower())
        stem = Path(rel_path).stem.replace("-", " ").replace("_", " ").lower()
        for word in stem.split():
            if len(word) > 3:
                concepts.add(word)

        file_concepts[file_id] = concepts
        G.add_node(file_id, path=rel_path, persona=r[2], type=fm_type,
                    importance=r[5], concepts=list(concepts))

    file_ids = list(file_concepts.keys())
    for i in range(len(file_ids)):
        for j in range(i + 1, len(file_ids)):
            shared = file_concepts[file_ids[i]] & file_concepts[file_ids[j]]
            if shared:
                G.add_edge(file_ids[i], file_ids[j], weight=len(shared))

    components = list(nx.connected_components(G))
    clusters = []
    for comp in sorted(components, key=len, reverse=True)[:5]:
        files_in = [{"path": G.nodes[n]["path"], "type": G.nodes[n]["type"]} for n in comp]
        all_concepts = set()
        for n in comp:
            all_concepts.update(G.nodes[n].get("concepts", []))
        clusters.append({"size": len(comp), "files": files_in[:10], "top_concepts": sorted(all_concepts)[:15]})

    isolated = [{"path": G.nodes[n]["path"], "type": G.nodes[n]["type"]} for n in nx.isolates(G)]

    return {
        "total_nodes": len(G.nodes), "total_edges": len(G.edges),
        "connected_components": len(components),
        "clusters": clusters, "isolated_files": isolated[:20],
    }


def consolidation_report(conn: sqlite3.Connection, persona: Optional[str] = None) -> dict:
    """Dry-run analysis of what consolidation would do. Does NOT modify anything."""
    where = "WHERE persona = ?" if persona else ""
    params = [persona] if persona else []
    now = datetime.now()

    rows = conn.execute(f"""
        SELECT id, path, persona, relative_path, fm_type, fm_importance,
               fm_created, fm_last_accessed, fm_access_count, fm_status
        FROM memory_files {where}
    """, params).fetchall()

    stale_candidates = []
    archive_candidates = []

    for r in rows:
        importance = r[5]
        if importance is None:
            continue

        last_accessed = r[7]
        days_since = 30  # default
        if last_accessed:
            try:
                days_since = (now - datetime.fromisoformat(str(last_accessed))).days
            except (ValueError, TypeError):
                pass
        elif r[6]:
            try:
                days_since = (now - datetime.fromisoformat(str(r[6]))).days
            except (ValueError, TypeError):
                pass

        decayed = max(0, importance - IMPORTANCE_DECAY_RATE * days_since)
        status = r[9] or "active"

        if status == "active" and decayed < MIN_IMPORTANCE_ACTIVE:
            stale_candidates.append({"path": r[3], "persona": r[2],
                                     "importance": importance, "decayed": round(decayed, 2), "type": r[4]})

        if status in ("active", "stale") and decayed < MIN_IMPORTANCE_STALE:
            archive_candidates.append({"path": r[3], "persona": r[2],
                                       "importance": importance, "decayed": round(decayed, 2), "type": r[4]})

    return {
        "total_analyzed": len(rows),
        "stale_candidates": stale_candidates,
        "archive_candidates": archive_candidates,
        "summary": {
            "would_mark_stale": len(stale_candidates),
            "would_archive": len(archive_candidates),
        }
    }


def mark_failure(conn: sqlite3.Connection, file_path: str) -> bool:
    """Increment failure_count for a memory file. Returns True if found."""
    path_str = file_path.replace("\\", "/")
    row = conn.execute("SELECT id, fm_failure_count FROM memory_files WHERE path LIKE ?",
                        (f"%{path_str}%",)).fetchone()
    if not row:
        return False
    new_count = (row[1] or 0) + 1
    conn.execute("UPDATE memory_files SET fm_failure_count = ? WHERE id = ?", (new_count, row[0]))
    conn.commit()
    return True


def memory_auto_capture_session_close(
    conn: sqlite3.Connection,
    personas_dir: Path,
    *,
    persona: str,
    title: str = "",
    summary: str = "",
    session_text: str = "",
    act_now_text: str = "",
    source_session_id: str = "",
    write: bool = False,
    actor: str = "agent",
) -> dict:
    """Plan or write a governed session-close capture memory."""
    plan = build_auto_capture_plan(
        persona=persona,
        title=title,
        summary=summary,
        session_text=session_text,
        act_now_text=act_now_text,
        source_session_id=source_session_id,
    )
    if not plan.get("ok"):
        return plan

    audit_payload = {
        "schema_version": plan["schema_version"],
        "capture_id": plan["capture_id"],
        "relative_path": plan["relative_path"],
        "action_item_count": len(plan.get("action_items", [])),
        "guard_findings": plan.get("guard_findings", []),
        "write": bool(write),
    }

    if not write:
        record_memory_audit_event(
            conn,
            "memory_auto_capture_planned",
            persona=persona,
            target_kind="auto_capture",
            target_id=plan["capture_id"],
            payload=audit_payload,
            actor=actor,
        )
        preview = {key: value for key, value in plan.items() if key != "body"}
        preview["body_preview"] = plan["body"][:1200]
        return {"ok": True, "written": False, "plan": preview}

    write_result = write_auto_capture_file(personas_dir, plan)
    if not write_result.get("ok"):
        return write_result

    full_path = Path(write_result["path"])
    relative_path = write_result["relative_path"]
    indexed = index_file(conn, persona, relative_path, full_path)
    row = conn.execute(
        "SELECT id FROM memory_files WHERE path = ?",
        (str(full_path).replace("\\", "/"),),
    ).fetchone()
    file_id = row[0] if row else None
    audit_payload.update(
        {
            "relative_path": relative_path,
            "path": str(full_path).replace("\\", "/"),
            "indexed": indexed,
            "file_id": file_id,
        }
    )
    record_memory_audit_event(
        conn,
        "memory_auto_capture_written",
        persona=persona,
        target_kind="memory_file",
        target_id=str(file_id or relative_path),
        payload=audit_payload,
        actor=actor,
        commit=False,
    )
    conn.commit()
    return {
        "ok": True,
        "written": True,
        "path": str(full_path),
        "relative_path": relative_path,
        "file_id": file_id,
        "indexed": indexed,
        "capture_id": plan["capture_id"],
        "action_items": plan.get("action_items", []),
        "guard_findings": plan.get("guard_findings", []),
    }


def memory_import_chatgpt_export(
    conn: sqlite3.Connection,
    personas_dir: Path,
    *,
    export_path: str,
    persona: str,
    limit: int = 50,
    write: bool = False,
    force: bool = False,
    build_pyramid: bool = True,
    actor: str = "agent",
) -> dict:
    """Plan or write governed memories from a ChatGPT conversations export."""
    return _memory_import_chatgpt_export(
        conn,
        personas_dir,
        export_path=export_path,
        persona=persona,
        index_file_func=index_file,
        pyramid_summary_builder=memory_pyramid_summary_build,
        limit=limit,
        write=write,
        force=force,
        build_pyramid=build_pyramid,
        actor=actor,
    )


def memory_import_obsidian_vault(
    conn: sqlite3.Connection,
    personas_dir: Path,
    *,
    vault_path: str,
    persona: str,
    limit: int = 200,
    write: bool = False,
    force: bool = False,
    build_pyramid: bool = True,
    actor: str = "agent",
) -> dict:
    """Plan or write governed memories from an Obsidian vault."""
    return _memory_import_obsidian_vault(
        conn,
        personas_dir,
        vault_path=vault_path,
        persona=persona,
        index_file_func=index_file,
        pyramid_summary_builder=memory_pyramid_summary_build,
        limit=limit,
        write=write,
        force=force,
        build_pyramid=build_pyramid,
        actor=actor,
    )


def memory_import_gmail_mbox(
    conn: sqlite3.Connection,
    personas_dir: Path,
    *,
    import_path: str,
    persona: str,
    limit: int = 200,
    write: bool = False,
    force: bool = False,
    build_pyramid: bool = True,
    actor: str = "agent",
) -> dict:
    """Plan or write governed memories from Gmail mbox exports."""
    return _memory_import_gmail_mbox(
        conn,
        personas_dir,
        import_path=import_path,
        persona=persona,
        index_file_func=index_file,
        pyramid_summary_builder=memory_pyramid_summary_build,
        limit=limit,
        write=write,
        force=force,
        build_pyramid=build_pyramid,
        actor=actor,
    )


def memory_import_perplexity_export(
    conn: sqlite3.Connection,
    personas_dir: Path,
    *,
    import_path: str,
    persona: str,
    limit: int = 200,
    write: bool = False,
    force: bool = False,
    build_pyramid: bool = True,
    actor: str = "agent",
) -> dict:
    """Plan or write governed memories from Perplexity exports."""
    return _memory_import_perplexity_export(
        conn,
        personas_dir,
        import_path=import_path,
        persona=persona,
        index_file_func=index_file,
        pyramid_summary_builder=memory_pyramid_summary_build,
        limit=limit,
        write=write,
        force=force,
        build_pyramid=build_pyramid,
        actor=actor,
    )


def memory_import_grok_export(
    conn: sqlite3.Connection,
    personas_dir: Path,
    *,
    import_path: str,
    persona: str,
    limit: int = 200,
    write: bool = False,
    force: bool = False,
    build_pyramid: bool = True,
    actor: str = "agent",
) -> dict:
    """Plan or write governed memories from Grok exports."""
    return _memory_import_grok_export(
        conn,
        personas_dir,
        import_path=import_path,
        persona=persona,
        index_file_func=index_file,
        pyramid_summary_builder=memory_pyramid_summary_build,
        limit=limit,
        write=write,
        force=force,
        build_pyramid=build_pyramid,
        actor=actor,
    )


def memory_import_twitter_archive(
    conn: sqlite3.Connection,
    personas_dir: Path,
    *,
    import_path: str,
    persona: str,
    limit: int = 200,
    write: bool = False,
    force: bool = False,
    build_pyramid: bool = True,
    actor: str = "agent",
) -> dict:
    """Plan or write governed memories from X/Twitter tweet archives."""
    return _memory_import_twitter_archive(
        conn,
        personas_dir,
        import_path=import_path,
        persona=persona,
        index_file_func=index_file,
        pyramid_summary_builder=memory_pyramid_summary_build,
        limit=limit,
        write=write,
        force=force,
        build_pyramid=build_pyramid,
        actor=actor,
    )


def memory_import_instagram_export(
    conn: sqlite3.Connection,
    personas_dir: Path,
    *,
    import_path: str,
    persona: str,
    limit: int = 200,
    write: bool = False,
    force: bool = False,
    build_pyramid: bool = True,
    actor: str = "agent",
) -> dict:
    """Plan or write governed memories from Instagram exports."""
    return _memory_import_instagram_export(
        conn,
        personas_dir,
        import_path=import_path,
        persona=persona,
        index_file_func=index_file,
        pyramid_summary_builder=memory_pyramid_summary_build,
        limit=limit,
        write=write,
        force=force,
        build_pyramid=build_pyramid,
        actor=actor,
    )


# â”€â”€â”€ Live File Watcher â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def start_memory_watcher(db, personas_dir: Path):
    """Watch persona memory dirs for .md changes and incrementally reindex.

    Returns the watchdog Observer (caller can stop it) or None if watchdog
    is unavailable. The watcher opens its own SQLite connections per event,
    so it is safe to run alongside the cached memory_conn in the main thread.
    """
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        log.warning("watchdog not installed, memory file watcher disabled")
        return None

    personas_dir = Path(personas_dir)
    shared_dir = personas_dir.parent / "shared"

    try:
        personas_root = personas_dir.resolve()
    except OSError:
        personas_root = personas_dir
    try:
        shared_root = shared_dir.resolve()
    except OSError:
        shared_root = shared_dir

    import os as _os
    _scope_persona = _os.environ.get("TRANSCRIPT_PERSONA", "").strip()

    def _resolve(path: Path) -> tuple[str, str] | None:
        """Map an absolute path to (persona, relative_path) or None.

        Respects TRANSCRIPT_PERSONA env var: returns None for files belonging
        to other personas. Shared content is always allowed through.
        """
        if path.suffix not in INDEX_EXTENSIONS:
            return None
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if any(part in SKIP_DIRS for part in resolved.parts):
            return None

        # shared/** â†’ persona="shared", rel relative to shared_root
        try:
            rel = resolved.relative_to(shared_root)
            return ("shared", str(rel).replace("\\", "/"))
        except ValueError:
            pass

        # personas/<persona>/<sub>/** â†’ persona=<sub>, rel relative to <sub>
        try:
            rel_full = resolved.relative_to(personas_root)
        except ValueError:
            return None
        parts = rel_full.parts
        if len(parts) < 3:
            return None
        # Privacy boundary: skip files belonging to other personas
        if _scope_persona and parts[1] != _scope_persona:
            return None
        sub_root = personas_root / parts[0] / parts[1]
        try:
            rel = resolved.relative_to(sub_root)
        except ValueError:
            return None
        return (parts[1], str(rel).replace("\\", "/"))

    def _upsert(path: Path):
        resolved = _resolve(path)
        if not resolved:
            return
        persona, rel = resolved
        try:
            with db.connection() as conn:
                init_memory_tables(conn)
                changed = index_file(conn, persona, rel, path, maintenance=True)
                if changed:
                    row = conn.execute(
                        "SELECT id FROM memory_files WHERE path = ?",
                        (str(path).replace("\\", "/"),),
                    ).fetchone()
                    if row:
                        try:
                            embed_memory_files(conn, [row[0]])
                        except Exception:
                            log.exception("Embedding failed for %s", path)
                conn.commit()
        except Exception:
            log.exception("Error reindexing memory file %s", path)

    def _delete(path: Path):
        if path.suffix not in INDEX_EXTENSIONS:
            return
        path_str = str(path).replace("\\", "/")
        try:
            with db.connection() as conn:
                init_memory_tables(conn)
                row = conn.execute(
                    "SELECT id FROM memory_files WHERE path = ?", (path_str,)
                ).fetchone()
                if not row:
                    return
                file_id = row[0]
                conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (file_id,))
                conn.execute("DELETE FROM memory_embeddings WHERE file_id = ?", (file_id,))
                conn.execute("DELETE FROM memory_files WHERE id = ?", (file_id,))
                conn.commit()
        except Exception:
            log.exception("Error removing memory file from index %s", path)

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event):
            if not event.is_directory:
                _upsert(Path(event.src_path))

        def on_created(self, event):
            if not event.is_directory:
                _upsert(Path(event.src_path))

        def on_deleted(self, event):
            if not event.is_directory:
                _delete(Path(event.src_path))

        def on_moved(self, event):
            if event.is_directory:
                return
            _delete(Path(event.src_path))
            _upsert(Path(event.dest_path))

    observer = Observer()
    handler = _Handler()
    scheduled = []
    if personas_dir.exists():
        observer.schedule(handler, str(personas_dir), recursive=True)
        scheduled.append(str(personas_dir))
    if shared_dir.exists():
        observer.schedule(handler, str(shared_dir), recursive=True)
        scheduled.append(str(shared_dir))

    if not scheduled:
        log.warning("start_memory_watcher: no directories to watch")
        return None

    observer.daemon = True
    observer.start()
    log.info("Memory file watcher started on %s", ", ".join(scheduled))
    return observer

