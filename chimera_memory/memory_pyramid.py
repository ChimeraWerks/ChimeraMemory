"""Deterministic multi-resolution summaries for curated memory files."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path

from .memory_frontmatter import parse_frontmatter
from .memory_observability import record_memory_audit_event
from .sanitizer import sanitize_content

PYRAMID_SUMMARY_SCHEMA_VERSION = "chimera-memory.pyramid-summary.v1"
PYRAMID_LEVELS = {
    "chunk": 0,
    "section": 1,
    "document": 2,
}

_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_BLOCK_RE = re.compile(r"\S(?:.*?\S)?(?=\n\s*\n|$)", re.DOTALL)


def _json_text(value: object) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str)


def _json_object(text: str | None) -> object:
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _clean_text(value: str | None) -> str:
    sanitized = sanitize_content(value or "") or ""
    return sanitized.replace("\r\n", "\n").replace("\r", "\n").strip()


def _collapse(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _find_memory_file(conn: sqlite3.Connection, file_path: str, persona: str | None = None):
    path = str(file_path or "").replace("\\", "/").strip()
    if not path:
        return None
    conditions = []
    params: list[object] = []
    if path.isdigit():
        conditions.append("id = ?")
        params.append(int(path))
    else:
        conditions.append("(path = ? OR relative_path = ? OR path LIKE ?)")
        params.extend([path, path, f"%{path}%"])
    if persona:
        conditions.append("persona = ?")
        params.append(persona)
    return conn.execute(
        f"""
        SELECT id, path, persona, relative_path, content_hash, fm_type, fm_about
        FROM memory_files
        WHERE {' AND '.join(conditions)}
        ORDER BY CASE
            WHEN path = ? THEN 0
            WHEN relative_path = ? THEN 1
            ELSE 2
        END
        LIMIT 1
        """,
        params + [path, path],
    ).fetchone()


def _file_to_dict(row) -> dict | None:
    if row is None:
        return None
    return {
        "id": row[0],
        "path": row[1],
        "persona": row[2],
        "relative_path": row[3],
        "content_hash": row[4],
        "type": row[5],
        "about": row[6],
    }


def _summary_to_dict(row) -> dict:
    return {
        "id": row[0],
        "summary_id": row[1],
        "level": row[2],
        "level_name": row[3],
        "ordinal": row[4],
        "parent_summary_id": row[5],
        "source_content_hash": row[6],
        "source_start": row[7],
        "source_end": row[8],
        "summary_text": row[9],
        "summary_hash": row[10],
        "summarizer_version": row[11],
        "metadata": _json_object(row[12]),
        "created_at": row[13],
        "file": {
            "id": row[14],
            "path": row[15],
            "persona": row[16],
            "relative_path": row[17],
            "type": row[18],
            "about": row[19],
        },
    }


def _iter_blocks(text: str) -> list[tuple[int, int, str]]:
    blocks: list[tuple[int, int, str]] = []
    for match in _BLOCK_RE.finditer(text):
        block = match.group(0).strip()
        if block:
            blocks.append((match.start(), match.end(), block))
    if not blocks and text.strip():
        blocks.append((0, len(text), text.strip()))
    return blocks


def _chunk_text(text: str, chunk_chars: int) -> list[dict]:
    blocks = _iter_blocks(text)
    chunks: list[dict] = []
    current: list[str] = []
    current_start: int | None = None
    current_end = 0
    current_len = 0

    def flush() -> None:
        nonlocal current, current_start, current_end, current_len
        if not current:
            return
        chunks.append(
            {
                "text": "\n\n".join(current).strip(),
                "source_start": int(current_start or 0),
                "source_end": int(current_end),
            }
        )
        current = []
        current_start = None
        current_end = 0
        current_len = 0

    for start, end, block in blocks:
        projected = current_len + len(block) + (2 if current else 0)
        if current and projected > chunk_chars:
            flush()
        if current_start is None:
            current_start = start
        current.append(block)
        current_end = end
        current_len += len(block) + (2 if current_len else 0)
        if len(block) > chunk_chars:
            flush()
    flush()
    return chunks


def _summarize_text(text: str, max_chars: int) -> str:
    collapsed = _collapse(text)
    if len(collapsed) <= max_chars:
        return collapsed
    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(collapsed) if part.strip()]
    selected: list[str] = []
    for sentence in sentences:
        candidate = " ".join([*selected, sentence]).strip()
        if selected and len(candidate) > max_chars:
            break
        selected.append(sentence)
        if len(candidate) >= max_chars:
            break
    summary = " ".join(selected).strip() or collapsed[:max_chars].strip()
    if len(summary) > max_chars:
        summary = summary[: max_chars - 3].rstrip() + "..."
    return summary


def _insert_summary(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    persona: str,
    level_name: str,
    ordinal: int,
    parent_summary_id: str = "",
    source_content_hash: str,
    source_start: int,
    source_end: int,
    summary_text: str,
    metadata: object | None = None,
) -> dict:
    summary_id = str(uuid.uuid4())
    level = PYRAMID_LEVELS[level_name]
    conn.execute(
        """
        INSERT INTO memory_pyramid_summaries (
            summary_id, file_id, persona, level, level_name, ordinal,
            parent_summary_id, source_content_hash, source_start, source_end,
            summary_text, summary_hash, summarizer_version, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            summary_id,
            file_id,
            persona,
            level,
            level_name,
            ordinal,
            parent_summary_id or "",
            source_content_hash,
            int(source_start),
            int(source_end),
            summary_text,
            _hash_text(summary_text),
            PYRAMID_SUMMARY_SCHEMA_VERSION,
            _json_text(metadata),
        ),
    )
    return {
        "summary_id": summary_id,
        "level": level,
        "level_name": level_name,
        "ordinal": ordinal,
        "source_start": int(source_start),
        "source_end": int(source_end),
        "summary_text": summary_text,
    }


def _current_summaries(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    source_content_hash: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.id, s.summary_id, s.level, s.level_name, s.ordinal,
               s.parent_summary_id, s.source_content_hash, s.source_start,
               s.source_end, s.summary_text, s.summary_hash,
               s.summarizer_version, s.metadata, s.created_at,
               f.id, f.path, f.persona, f.relative_path, f.fm_type, f.fm_about
        FROM memory_pyramid_summaries s
        JOIN memory_files f ON f.id = s.file_id
        WHERE s.file_id = ?
          AND s.source_content_hash = ?
          AND s.summarizer_version = ?
        ORDER BY s.level DESC, s.ordinal ASC
        """,
        (file_id, source_content_hash, PYRAMID_SUMMARY_SCHEMA_VERSION),
    ).fetchall()
    return [_summary_to_dict(row) for row in rows]


def memory_pyramid_summary_build(
    conn: sqlite3.Connection,
    *,
    file_path: str,
    persona: str | None = None,
    chunk_chars: int = 1600,
    section_size: int = 4,
    max_summary_chars: int = 500,
    force: bool = False,
    actor: str = "system",
) -> dict:
    """Build deterministic chunk, section, and document summaries for one memory file."""
    memory_file = _find_memory_file(conn, file_path, persona=persona)
    if memory_file is None:
        return {"ok": False, "error": "memory file not found", "file_path": file_path}
    source = _file_to_dict(memory_file)
    assert source is not None
    source_hash = str(source["content_hash"])
    file_id = int(source["id"])
    stale_delete = conn.execute(
        """
        DELETE FROM memory_pyramid_summaries
         WHERE file_id = ?
           AND (source_content_hash <> ? OR summarizer_version <> ?)
        """,
        (file_id, source_hash, PYRAMID_SUMMARY_SCHEMA_VERSION),
    )
    existing = _current_summaries(conn, file_id=file_id, source_content_hash=source_hash)
    if existing and not force:
        if stale_delete.rowcount:
            conn.commit()
        return {
            "ok": True,
            "built": False,
            "reason": "current summaries already exist",
            "file": source,
            "counts": _summary_counts(existing),
            "summaries": existing,
        }
    if force:
        conn.execute(
            """
            DELETE FROM memory_pyramid_summaries
             WHERE file_id = ?
               AND source_content_hash = ?
               AND summarizer_version = ?
            """,
            (file_id, source_hash, PYRAMID_SUMMARY_SCHEMA_VERSION),
        )

    try:
        content = Path(str(source["path"])).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": f"failed to read memory file: {exc}", "file": source}
    _, body = parse_frontmatter(content)
    text = _clean_text(body)
    if not text:
        return {"ok": False, "error": "memory file has no summaryable body", "file": source}

    chunk_chars = max(200, min(int(chunk_chars), 12000))
    section_size = max(1, min(int(section_size), 20))
    max_summary_chars = max(120, min(int(max_summary_chars), 2000))

    chunks = _chunk_text(text, chunk_chars)
    chunk_rows = [
        _insert_summary(
            conn,
            file_id=file_id,
            persona=str(source["persona"]),
            level_name="chunk",
            ordinal=idx,
            source_content_hash=source_hash,
            source_start=chunk["source_start"],
            source_end=chunk["source_end"],
            summary_text=_summarize_text(chunk["text"], max_summary_chars),
            metadata={"chunk_chars": chunk_chars, "source_chars": len(chunk["text"])},
        )
        for idx, chunk in enumerate(chunks)
    ]

    section_rows: list[dict] = []
    for idx in range(0, len(chunk_rows), section_size):
        group = chunk_rows[idx : idx + section_size]
        section_text = "\n".join(row["summary_text"] for row in group)
        section = _insert_summary(
            conn,
            file_id=file_id,
            persona=str(source["persona"]),
            level_name="section",
            ordinal=len(section_rows),
            source_content_hash=source_hash,
            source_start=int(group[0]["source_start"]),
            source_end=int(group[-1]["source_end"]),
            summary_text=_summarize_text(section_text, max_summary_chars),
            metadata={"child_summary_ids": [row["summary_id"] for row in group]},
        )
        section_rows.append(section)
        conn.execute(
            f"""
            UPDATE memory_pyramid_summaries
               SET parent_summary_id = ?
             WHERE summary_id IN ({','.join('?' * len(group))})
            """,
            [section["summary_id"], *[row["summary_id"] for row in group]],
        )

    document_text = "\n".join(row["summary_text"] for row in section_rows or chunk_rows)
    document = _insert_summary(
        conn,
        file_id=file_id,
        persona=str(source["persona"]),
        level_name="document",
        ordinal=0,
        source_content_hash=source_hash,
        source_start=0,
        source_end=len(text),
        summary_text=_summarize_text(document_text, max_summary_chars),
        metadata={"child_summary_ids": [row["summary_id"] for row in section_rows]},
    )
    if section_rows:
        conn.execute(
            f"""
            UPDATE memory_pyramid_summaries
               SET parent_summary_id = ?
             WHERE summary_id IN ({','.join('?' * len(section_rows))})
            """,
            [document["summary_id"], *[row["summary_id"] for row in section_rows]],
        )

    summaries = _current_summaries(conn, file_id=file_id, source_content_hash=source_hash)
    counts = _summary_counts(summaries)
    record_memory_audit_event(
        conn,
        "memory_pyramid_summary_built",
        persona=str(source["persona"]),
        target_kind="memory_file",
        target_id=str(file_id),
        payload={
            "schema_version": PYRAMID_SUMMARY_SCHEMA_VERSION,
            "file_id": file_id,
            "relative_path": source["relative_path"],
            "counts": counts,
            "chunk_chars": chunk_chars,
            "section_size": section_size,
            "max_summary_chars": max_summary_chars,
            "force": bool(force),
        },
        actor=actor,
        commit=False,
    )
    conn.commit()
    return {"ok": True, "built": True, "file": source, "counts": counts, "summaries": summaries}


def _summary_counts(summaries: list[dict]) -> dict:
    counts = {"chunk": 0, "section": 0, "document": 0, "total": len(summaries)}
    for summary in summaries:
        level_name = str(summary.get("level_name") or "")
        if level_name in counts:
            counts[level_name] += 1
    return counts


def memory_pyramid_summary_query(
    conn: sqlite3.Connection,
    *,
    file_path: str | None = None,
    persona: str | None = None,
    level_name: str | None = None,
    search: str | None = None,
    current_only: bool = True,
    limit: int = 50,
) -> list[dict]:
    """Query pyramid summaries across current indexed memory files."""
    conditions: list[str] = []
    params: list[object] = []
    if file_path:
        memory_file = _find_memory_file(conn, file_path, persona=persona)
        if memory_file is None:
            return []
        conditions.append("s.file_id = ?")
        params.append(int(memory_file[0]))
    elif persona:
        conditions.append("s.persona = ?")
        params.append(persona)
    if level_name:
        normalized_level = str(level_name).strip().lower()
        if normalized_level not in PYRAMID_LEVELS:
            return []
        conditions.append("s.level_name = ?")
        params.append(normalized_level)
    if search:
        conditions.append("LOWER(s.summary_text) LIKE ?")
        params.append(f"%{str(search).lower()}%")
    if current_only:
        conditions.append("s.source_content_hash = f.content_hash")
        conditions.append("s.summarizer_version = ?")
        params.append(PYRAMID_SUMMARY_SCHEMA_VERSION)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT s.id, s.summary_id, s.level, s.level_name, s.ordinal,
               s.parent_summary_id, s.source_content_hash, s.source_start,
               s.source_end, s.summary_text, s.summary_hash,
               s.summarizer_version, s.metadata, s.created_at,
               f.id, f.path, f.persona, f.relative_path, f.fm_type, f.fm_about
        FROM memory_pyramid_summaries s
        JOIN memory_files f ON f.id = s.file_id
        {where}
        ORDER BY s.level DESC, f.persona ASC, f.relative_path ASC, s.ordinal ASC
        LIMIT ?
        """,
        params + [max(0, min(int(limit), 500))],
    ).fetchall()
    return [_summary_to_dict(row) for row in rows]
