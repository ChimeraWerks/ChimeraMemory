"""Entity graph helpers for ChimeraMemory curated memories."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from itertools import combinations
from pathlib import Path

from .memory_frontmatter import parse_frontmatter
from .memory_observability import record_memory_audit_event

ENTITY_TYPES = {
    "person",
    "project",
    "topic",
    "tool",
    "organization",
    "place",
    "date",
    "entity",
    "unknown",
}

MENTION_ROLES = {"subject", "tag", "mentioned", "related"}
ENHANCEMENT_RELATION_TYPES = {"works_on", "uses", "related_to", "member_of", "located_in", "co_occurs_with"}

_WHITESPACE_RE = re.compile(r"\s+")
_PREFIX_TO_TYPE = {
    "person": "person",
    "people": "person",
    "project": "project",
    "tool": "tool",
    "org": "organization",
    "orgs": "organization",
    "organization": "organization",
    "organizations": "organization",
    "place": "place",
    "places": "place",
    "topic": "topic",
    "topics": "topic",
    "date": "date",
    "dates": "date",
}


def normalize_entity_name(name: str) -> str:
    """Normalize an entity name for deduplication."""
    clean = str(name or "").replace("_", " ").replace("-", " ").strip().lower()
    return _WHITESPACE_RE.sub(" ", clean)


def _json_text(value: object) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str)


def _json_object(text: str | None) -> object:
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _json_list(text: str | None) -> list:
    parsed = _json_object(text)
    return parsed if isinstance(parsed, list) else []


def _clean_entity_type(entity_type: str | None) -> str:
    clean = normalize_entity_name(entity_type or "unknown")
    return clean if clean in ENTITY_TYPES else "unknown"


def _clean_mention_role(role: str | None) -> str:
    clean = normalize_entity_name(role or "related")
    return clean if clean in MENTION_ROLES else "related"


def _entity_to_dict(row) -> dict | None:
    if row is None:
        return None
    return {
        "id": row[0],
        "entity_id": row[1],
        "entity_type": row[2],
        "canonical_name": row[3],
        "normalized_name": row[4],
        "aliases": _json_list(row[5]),
        "confidence": row[6],
        "source": row[7],
        "metadata": _json_object(row[8]),
        "created_at": row[9],
        "updated_at": row[10],
    }


def upsert_memory_entity(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    canonical_name: str,
    aliases: list[str] | None = None,
    confidence: float | None = None,
    source: str = "frontmatter",
    metadata: object | None = None,
    commit: bool = True,
) -> dict:
    """Create or update an entity and return its row as a dict."""
    canonical = str(canonical_name or "").strip()
    normalized = normalize_entity_name(canonical)
    if not normalized:
        raise ValueError("canonical_name is required")
    clean_type = _clean_entity_type(entity_type)
    clean_aliases = sorted({str(alias).strip() for alias in (aliases or []) if str(alias).strip()})
    clean_confidence = float(confidence if confidence is not None else 1.0)

    existing = conn.execute(
        """
        SELECT id, entity_id, entity_type, canonical_name, normalized_name,
               aliases, confidence, source, metadata, created_at, updated_at
        FROM memory_entities
        WHERE entity_type = ? AND normalized_name = ?
        """,
        (clean_type, normalized),
    ).fetchone()
    if existing:
        existing_aliases = set(_json_list(existing[5]))
        merged_aliases = sorted(existing_aliases | set(clean_aliases))
        merged_metadata = _json_object(existing[8])
        if isinstance(merged_metadata, dict) and isinstance(metadata, dict):
            merged_metadata = {**merged_metadata, **metadata}
        elif metadata is not None:
            merged_metadata = metadata
        conn.execute(
            """
            UPDATE memory_entities
               SET canonical_name = ?,
                   aliases = ?,
                   confidence = MAX(COALESCE(confidence, 0), ?),
                   source = ?,
                   metadata = ?
             WHERE id = ?
            """,
            (
                canonical or existing[3],
                _json_text(merged_aliases),
                clean_confidence,
                source or existing[7] or "frontmatter",
                _json_text(merged_metadata),
                existing[0],
            ),
        )
        entity_id = existing[1]
    else:
        entity_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO memory_entities (
                entity_id, entity_type, canonical_name, normalized_name,
                aliases, confidence, source, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_id,
                clean_type,
                canonical,
                normalized,
                _json_text(clean_aliases),
                clean_confidence,
                source or "frontmatter",
                _json_text(metadata),
            ),
        )

    if commit:
        conn.commit()
    row = conn.execute(
        """
        SELECT id, entity_id, entity_type, canonical_name, normalized_name,
               aliases, confidence, source, metadata, created_at, updated_at
        FROM memory_entities
        WHERE entity_id = ?
        """,
        (entity_id,),
    ).fetchone()
    entity = _entity_to_dict(row)
    if entity is None:
        raise RuntimeError("entity upsert failed")
    return entity


def _find_memory_file(conn: sqlite3.Connection, file_path: str):
    path = file_path.replace("\\", "/").strip()
    return conn.execute(
        """
        SELECT id, path, persona, relative_path
        FROM memory_files
        WHERE path = ? OR relative_path = ? OR path LIKE ?
        ORDER BY CASE
            WHEN path = ? THEN 0
            WHEN relative_path = ? THEN 1
            ELSE 2
        END
        LIMIT 1
        """,
        (path, path, f"%{path}%", path, path),
    ).fetchone()


def link_memory_file_entity(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    entity_row_id: int,
    mention_role: str = "related",
    confidence: float | None = None,
    source: str = "frontmatter",
    evidence: str = "",
    metadata: object | None = None,
    commit: bool = True,
) -> dict:
    """Link an indexed memory file to an entity."""
    clean_role = _clean_mention_role(mention_role)
    clean_confidence = float(confidence if confidence is not None else 1.0)
    conn.execute(
        """
        INSERT INTO memory_file_entities (
            file_id, entity_id, mention_role, confidence, source, evidence, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_id, entity_id, mention_role) DO UPDATE SET
            confidence = MAX(COALESCE(confidence, 0), excluded.confidence),
            source = excluded.source,
            evidence = excluded.evidence,
            metadata = excluded.metadata
        """,
        (
            file_id,
            entity_row_id,
            clean_role,
            clean_confidence,
            source or "frontmatter",
            evidence or "",
            _json_text(metadata),
        ),
    )
    if commit:
        conn.commit()
    row = conn.execute(
        """
        SELECT id, file_id, entity_id, mention_role, confidence,
               source, evidence, metadata, created_at
        FROM memory_file_entities
        WHERE file_id = ? AND entity_id = ? AND mention_role = ?
        """,
        (file_id, entity_row_id, clean_role),
    ).fetchone()
    return {
        "id": row[0],
        "file_id": row[1],
        "entity_id": row[2],
        "mention_role": row[3],
        "confidence": row[4],
        "source": row[5],
        "evidence": row[6],
        "metadata": _json_object(row[7]),
        "created_at": row[8],
    }


def upsert_memory_entity_edge(
    conn: sqlite3.Connection,
    *,
    source_entity_id: int,
    target_entity_id: int,
    relation_type: str = "related_to",
    confidence: float | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    decay_weight: float | None = None,
    classifier_version: str = "",
    metadata: object | None = None,
    commit: bool = True,
) -> dict:
    """Create or reinforce a typed relation between two entities."""
    if source_entity_id == target_entity_id:
        raise ValueError("source and target entities must differ")
    clean_relation = normalize_entity_name(relation_type or "related_to").replace(" ", "_")
    clean_confidence = float(confidence if confidence is not None else 1.0)
    clean_decay = float(decay_weight if decay_weight is not None else 1.0)
    edge_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO memory_entity_edges (
            edge_id, source_entity_id, target_entity_id, relation_type,
            confidence, support_count, valid_from, valid_until,
            decay_weight, classifier_version, metadata
        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
        ON CONFLICT(source_entity_id, target_entity_id, relation_type) DO UPDATE SET
            confidence = MAX(COALESCE(confidence, 0), excluded.confidence),
            support_count = support_count + 1,
            valid_until = CASE
                WHEN excluded.valid_until IS NULL OR excluded.valid_until = '' THEN valid_until
                ELSE excluded.valid_until
            END,
            decay_weight = excluded.decay_weight,
            classifier_version = excluded.classifier_version,
            metadata = excluded.metadata
        """,
        (
            edge_id,
            source_entity_id,
            target_entity_id,
            clean_relation,
            clean_confidence,
            valid_from or "",
            valid_until or "",
            clean_decay,
            classifier_version or "",
            _json_text(metadata),
        ),
    )
    if commit:
        conn.commit()
    row = conn.execute(
        """
        SELECT id, edge_id, source_entity_id, target_entity_id, relation_type,
               confidence, support_count, valid_from, valid_until, decay_weight,
               classifier_version, metadata, created_at, updated_at
        FROM memory_entity_edges
        WHERE source_entity_id = ? AND target_entity_id = ? AND relation_type = ?
        """,
        (source_entity_id, target_entity_id, clean_relation),
    ).fetchone()
    return {
        "id": row[0],
        "edge_id": row[1],
        "source_entity_id": row[2],
        "target_entity_id": row[3],
        "relation_type": row[4],
        "confidence": row[5],
        "support_count": row[6],
        "valid_from": row[7],
        "valid_until": row[8],
        "decay_weight": row[9],
        "classifier_version": row[10],
        "metadata": _json_object(row[11]),
        "created_at": row[12],
        "updated_at": row[13],
    }


def _load_tags(text: str | None) -> list[str]:
    parsed = _json_object(text)
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _tag_to_entity(tag: str) -> tuple[str, str]:
    raw = str(tag or "").strip()
    if ":" not in raw:
        return "topic", raw
    prefix, value = raw.split(":", 1)
    entity_type = _PREFIX_TO_TYPE.get(normalize_entity_name(prefix), "topic")
    return entity_type, value.strip() or raw


def _path_entity_name(relative_path: str) -> str:
    stem = Path(relative_path).stem.replace("_", " ").replace("-", " ").strip()
    return _WHITESPACE_RE.sub(" ", stem)


def _payload_entity_type(raw_key: object) -> str:
    key = normalize_entity_name(str(raw_key or ""))
    if key.endswith("s"):
        key = key[:-1]
    return _PREFIX_TO_TYPE.get(key, key if key in ENTITY_TYPES else "topic")


def _payload_entity_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        name = value.get("name") or value.get("canonical_name") or value.get("value")
        return [str(name).strip()] if str(name or "").strip() else []
    if isinstance(value, (list, tuple)):
        values: list[str] = []
        for item in value:
            values.extend(_payload_entity_values(item))
        return values
    return []


def _memory_payload_entities_for_file(path: str, relative_path: str) -> list[tuple[str, str, str, str]]:
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    frontmatter, _body = parse_frontmatter(content)
    payload = frontmatter.get("memory_payload")
    if not isinstance(payload, dict):
        return []
    entities = payload.get("entities")
    if not isinstance(entities, dict):
        return []

    derived: list[tuple[str, str, str, str]] = []
    for raw_key, raw_values in entities.items():
        entity_type = _payload_entity_type(raw_key)
        evidence = f"memory_payload.entities.{raw_key}"
        for name in _payload_entity_values(raw_values):
            derived.append((entity_type, name, "mentioned", evidence))
    return derived


def index_entities_for_memory_file(conn: sqlite3.Connection, file_id: int) -> list[dict]:
    """Derive entity links for one indexed memory file from local metadata."""
    row = conn.execute(
        """
        SELECT id, path, persona, relative_path, fm_type, fm_tags, fm_entity, fm_about
        FROM memory_files
        WHERE id = ?
        """,
        (file_id,),
    ).fetchone()
    if row is None:
        return []

    derived: list[tuple[str, str, str, str]] = []
    relative_path = str(row[3] or "")
    fm_type = str(row[4] or "")
    fm_entity = str(row[6] or "").strip()
    if fm_entity:
        derived.append(("person", fm_entity, "subject", "fm_entity"))
    elif fm_type == "entity" or relative_path.startswith("entities/"):
        name = _path_entity_name(relative_path)
        if name:
            derived.append(("person", name, "subject", "path"))

    for tag in _load_tags(row[5]):
        entity_type, name = _tag_to_entity(tag)
        if name:
            derived.append((entity_type, name, "tag", f"tag:{tag}"))

    derived.extend(_memory_payload_entities_for_file(str(row[1] or ""), relative_path))

    links = []
    seen_keys = set()
    for entity_type, name, role, evidence in derived:
        key = (_clean_entity_type(entity_type), normalize_entity_name(name), _clean_mention_role(role))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        source = "memory_payload" if str(evidence).startswith("memory_payload.") else "frontmatter"
        entity = upsert_memory_entity(
            conn,
            entity_type=entity_type,
            canonical_name=name,
            source=source,
            metadata={"persona": row[2]},
            commit=False,
        )
        link = link_memory_file_entity(
            conn,
            file_id=row[0],
            entity_row_id=entity["id"],
            mention_role=role,
            confidence=1.0,
            source=source,
            evidence=evidence,
            metadata={"relative_path": relative_path},
            commit=False,
        )
        links.append({"entity": entity, "link": link})
    return links


def memory_entity_index(
    conn: sqlite3.Connection,
    *,
    persona: str | None = None,
    limit: int | None = None,
) -> dict:
    """Rebuild local entity links from indexed memory frontmatter."""
    conditions, params = [], []
    if persona:
        conditions.append("persona = ?")
        params.append(persona)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    limit_sql = "LIMIT ?" if limit is not None else ""
    if limit is not None:
        params.append(max(0, int(limit)))
    rows = conn.execute(
        f"""
        SELECT id, persona
        FROM memory_files
        {where}
        ORDER BY id ASC
        {limit_sql}
        """,
        params,
    ).fetchall()
    file_ids = [int(row[0]) for row in rows]
    if file_ids:
        placeholders = ",".join("?" * len(file_ids))
        conn.execute(f"DELETE FROM memory_file_entities WHERE file_id IN ({placeholders})", file_ids)

    link_count = 0
    for file_id in file_ids:
        link_count += len(index_entities_for_memory_file(conn, file_id))

    conn.execute(
        """
        DELETE FROM memory_entities
        WHERE id NOT IN (SELECT DISTINCT entity_id FROM memory_file_entities)
          AND id NOT IN (SELECT source_entity_id FROM memory_entity_edges)
          AND id NOT IN (SELECT target_entity_id FROM memory_entity_edges)
        """
    )
    record_memory_audit_event(
        conn,
        "memory_entities_indexed",
        persona=persona,
        target_kind="memory_entities",
        target_id=persona or "all",
        payload={"file_count": len(file_ids), "link_count": link_count},
        commit=False,
    )
    conn.commit()
    entity_count = conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0]
    return {"file_count": len(file_ids), "link_count": link_count, "entity_count": entity_count}


def memory_entity_query(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    entity_type: str | None = None,
    persona: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query entities with evidence counts from linked memory files."""
    conditions, params = [], []
    if query:
        normalized_query = f"%{normalize_entity_name(query)}%"
        canonical_query = f"%{query.strip()}%"
        conditions.append("(e.normalized_name LIKE ? OR e.canonical_name LIKE ? OR e.aliases LIKE ?)")
        params.extend([normalized_query, canonical_query, canonical_query])
    if entity_type:
        conditions.append("e.entity_type = ?")
        params.append(_clean_entity_type(entity_type))
    if persona:
        conditions.append("mf.persona = ?")
        params.append(persona)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT e.id, e.entity_id, e.entity_type, e.canonical_name,
               e.normalized_name, e.aliases, e.confidence, e.source,
               e.metadata, e.created_at, e.updated_at,
               COUNT(DISTINCT mfe.file_id) AS file_count,
               GROUP_CONCAT(DISTINCT mf.persona) AS personas
        FROM memory_entities e
        LEFT JOIN memory_file_entities mfe ON mfe.entity_id = e.id
        LEFT JOIN memory_files mf ON mf.id = mfe.file_id
        {where}
        GROUP BY e.id
        ORDER BY file_count DESC, e.canonical_name ASC
        LIMIT ?
        """,
        params + [max(0, min(limit, 500))],
    ).fetchall()
    results = []
    for row in rows:
        entity = _entity_to_dict(row[:11])
        if entity is None:
            continue
        entity["file_count"] = row[11]
        entity["personas"] = sorted({item for item in str(row[12] or "").split(",") if item})
        results.append(entity)
    return results


def memory_entity_connections(
    conn: sqlite3.Connection,
    *,
    entity_name: str,
    entity_type: str | None = None,
    persona: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Find entities connected by shared memory-file evidence."""
    normalized = normalize_entity_name(entity_name)
    if not normalized:
        return []
    params: list[object] = [normalized, normalized]
    conditions = "(source.normalized_name = ? OR source.canonical_name = ?)"
    if entity_type:
        conditions += " AND source.entity_type = ?"
        params.append(_clean_entity_type(entity_type))
    persona_join = ""
    persona_condition = ""
    if persona:
        persona_join = "JOIN memory_files source_mf ON source_mf.id = source_link.file_id"
        persona_condition = "AND source_mf.persona = ?"
        params.append(persona)
    rows = conn.execute(
        f"""
        SELECT target.id, target.entity_id, target.entity_type, target.canonical_name,
               target.normalized_name, target.aliases, target.confidence, target.source,
               target.metadata, target.created_at, target.updated_at,
               COUNT(DISTINCT target_link.file_id) AS overlap_count,
               GROUP_CONCAT(DISTINCT mf.relative_path) AS evidence_paths
        FROM memory_entities source
        JOIN memory_file_entities source_link ON source_link.entity_id = source.id
        {persona_join}
        JOIN memory_file_entities target_link ON target_link.file_id = source_link.file_id
        JOIN memory_entities target ON target.id = target_link.entity_id
        JOIN memory_files mf ON mf.id = target_link.file_id
        WHERE {conditions}
          {persona_condition}
          AND target.id <> source.id
        GROUP BY target.id
        ORDER BY overlap_count DESC, target.canonical_name ASC
        LIMIT ?
        """,
        params + [max(0, min(limit, 200))],
    ).fetchall()
    results = []
    for row in rows:
        entity = _entity_to_dict(row[:11])
        if entity is None:
            continue
        entity["overlap_count"] = row[11]
        entity["evidence_paths"] = sorted({item for item in str(row[12] or "").split(",") if item})[:10]
        results.append(entity)
    return results


def memory_entity_edge_query(
    conn: sqlite3.Connection,
    *,
    entity_name: str | None = None,
    relation_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query explicit typed entity edges."""
    conditions, params = [], []
    if entity_name:
        normalized = normalize_entity_name(entity_name)
        conditions.append(
            "(source.normalized_name = ? OR target.normalized_name = ? "
            "OR source.canonical_name = ? OR target.canonical_name = ?)"
        )
        params.extend([normalized, normalized, entity_name, entity_name])
    if relation_type:
        conditions.append("edge.relation_type = ?")
        params.append(normalize_entity_name(relation_type).replace(" ", "_"))
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT edge.edge_id, edge.relation_type, edge.confidence,
               edge.support_count, edge.valid_from, edge.valid_until,
               edge.decay_weight, edge.classifier_version, edge.metadata,
               edge.created_at, edge.updated_at,
               source.entity_id, source.entity_type, source.canonical_name,
               target.entity_id, target.entity_type, target.canonical_name
        FROM memory_entity_edges edge
        JOIN memory_entities source ON source.id = edge.source_entity_id
        JOIN memory_entities target ON target.id = edge.target_entity_id
        {where}
        ORDER BY edge.support_count DESC, edge.confidence DESC, edge.created_at DESC
        LIMIT ?
        """,
        params + [max(0, min(limit, 500))],
    ).fetchall()
    return [
        {
            "edge_id": row[0],
            "relation_type": row[1],
            "confidence": row[2],
            "support_count": row[3],
            "valid_from": row[4],
            "valid_until": row[5],
            "decay_weight": row[6],
            "classifier_version": row[7],
            "metadata": _json_object(row[8]),
            "created_at": row[9],
            "updated_at": row[10],
            "source": {
                "entity_id": row[11],
                "entity_type": row[12],
                "canonical_name": row[13],
            },
            "target": {
                "entity_id": row[14],
                "entity_type": row[15],
                "canonical_name": row[16],
            },
        }
        for row in rows
    ]


def memory_file_entity_links(conn: sqlite3.Connection, *, file_path: str) -> list[dict]:
    """List entity links for an indexed memory file."""
    row = _find_memory_file(conn, file_path)
    if row is None:
        return []
    link_rows = conn.execute(
        """
        SELECT e.id, e.entity_id, e.entity_type, e.canonical_name,
               e.normalized_name, e.aliases, e.confidence, e.source,
               e.metadata, e.created_at, e.updated_at,
               mfe.mention_role, mfe.evidence
        FROM memory_file_entities mfe
        JOIN memory_entities e ON e.id = mfe.entity_id
        WHERE mfe.file_id = ?
        ORDER BY mfe.mention_role ASC, e.entity_type ASC, e.canonical_name ASC
        """,
        (row[0],),
    ).fetchall()
    links = []
    for link_row in link_rows:
        entity = _entity_to_dict(link_row[:11])
        if entity is None:
            continue
        entity["mention_role"] = link_row[11]
        entity["evidence"] = link_row[12]
        links.append(entity)
    return links


def apply_enhancement_entities(
    conn: sqlite3.Connection,
    *,
    file_id: int | None,
    metadata: dict,
    source: str = "enhancement",
) -> dict:
    """Populate entity links from normalized memory-enhancement metadata."""
    if not file_id:
        return {"link_count": 0, "edge_count": 0}
    memory_row = conn.execute(
        "SELECT id, persona, relative_path FROM memory_files WHERE id = ?",
        (file_id,),
    ).fetchone()
    if memory_row is None:
        return {"link_count": 0, "edge_count": 0}

    field_specs = (
        ("people", "person", "mentioned"),
        ("projects", "project", "mentioned"),
        ("tools", "tool", "mentioned"),
        ("organizations", "organization", "mentioned"),
        ("places", "place", "mentioned"),
        ("dates", "date", "mentioned"),
        ("topics", "topic", "tag"),
    )
    linked_entities: list[dict] = []
    seen_keys = set()
    confidence = metadata.get("confidence")
    confidence_value = float(confidence) if isinstance(confidence, (int, float)) else 1.0
    candidates: list[tuple[str, str, str, float, str]] = []

    raw_entities = metadata.get("entities")
    if isinstance(raw_entities, list):
        for raw in raw_entities:
            if not isinstance(raw, dict):
                continue
            entity_type = _clean_entity_type(raw.get("type") or raw.get("category"))
            name = str(raw.get("name") or raw.get("canonical_name") or raw.get("entity") or "").strip()
            raw_confidence = raw.get("confidence")
            entity_confidence = (
                float(raw_confidence)
                if isinstance(raw_confidence, (int, float))
                else confidence_value
            )
            role = "tag" if entity_type == "topic" else "mentioned"
            candidates.append((name, entity_type, role, entity_confidence, "entities"))

    for field, entity_type, role in field_specs:
        raw_items = metadata.get(field)
        items = raw_items if isinstance(raw_items, list) else []
        for item in items:
            name = str(item or "").strip()
            candidates.append((name, entity_type, role, confidence_value, field))

    for name, entity_type, role, entity_confidence, field in candidates:
        clean_type = _clean_entity_type(entity_type)
        key = (clean_type, normalize_entity_name(name), role)
        if clean_type == "unknown" or not name or key in seen_keys or entity_confidence < 0.5:
            continue
        seen_keys.add(key)
        entity = upsert_memory_entity(
            conn,
            entity_type=clean_type,
            canonical_name=name,
            confidence=entity_confidence,
            source=source,
            metadata={"persona": memory_row[1], "field": field},
            commit=False,
        )
        link_memory_file_entity(
            conn,
            file_id=int(memory_row[0]),
            entity_row_id=int(entity["id"]),
            mention_role=role,
            confidence=entity_confidence,
            source=source,
            evidence=f"enhancement:{field}",
            metadata={"relative_path": memory_row[2], "field": field},
            commit=False,
        )
        linked_entities.append(entity)

    edge_count = 0
    linked_by_name = {
        normalize_entity_name(str(entity.get("canonical_name") or "")): entity
        for entity in linked_entities
        if entity.get("canonical_name")
    }
    for source_entity, target_entity in combinations(linked_entities, 2):
        upsert_memory_entity_edge(
            conn,
            source_entity_id=int(source_entity["id"]),
            target_entity_id=int(target_entity["id"]),
            relation_type="co_occurs_with",
            confidence=confidence_value,
            classifier_version="memory_enhancement.v2",
            metadata={"file_id": file_id, "source": source},
            commit=False,
        )
        edge_count += 1

    raw_relationships = metadata.get("relationships")
    relationships = raw_relationships if isinstance(raw_relationships, list) else []
    for raw in relationships:
        if not isinstance(raw, dict):
            continue
        from_name = str(raw.get("from") or raw.get("source") or "").strip()
        to_name = str(raw.get("to") or raw.get("target") or "").strip()
        relation_type = str(raw.get("relation") or raw.get("relation_type") or "related_to").strip()
        clean_relation = normalize_entity_name(relation_type).replace(" ", "_")
        raw_confidence = raw.get("confidence")
        relation_confidence = (
            float(raw_confidence)
            if isinstance(raw_confidence, (int, float))
            else confidence_value
        )
        source_entity = linked_by_name.get(normalize_entity_name(from_name))
        target_entity = linked_by_name.get(normalize_entity_name(to_name))
        if (
            clean_relation not in ENHANCEMENT_RELATION_TYPES
            or not source_entity
            or not target_entity
            or source_entity["id"] == target_entity["id"]
            or relation_confidence < 0.5
        ):
            continue
        upsert_memory_entity_edge(
            conn,
            source_entity_id=int(source_entity["id"]),
            target_entity_id=int(target_entity["id"]),
            relation_type=clean_relation,
            confidence=relation_confidence,
            classifier_version="memory_enhancement.v2",
            metadata={"file_id": file_id, "source": source, "kind": "typed_relationship"},
            commit=False,
        )
        edge_count += 1

    return {"link_count": len(linked_entities), "edge_count": edge_count}
