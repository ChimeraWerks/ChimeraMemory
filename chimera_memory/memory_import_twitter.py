"""X / Twitter archive import planning and file writing."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .memory_auto_capture import resolve_persona_root
from .memory_observability import record_memory_audit_event
from .sanitizer import sanitize_content, scan_for_injection

TWITTER_IMPORT_SCHEMA_VERSION = "chimera-memory.twitter-import.v1"
TWITTER_IMPORT_TAGS = ["import", "twitter", "x", "social"]
TWITTER_TEXT_CHAR_LIMIT = 12000

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_BLOCKING_FINDING_TYPES = {"credential"}
_SUPPORTED_SUFFIXES = {".js", ".json", ".jsonl", ".txt"}
_SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(value: str | None) -> str:
    sanitized = sanitize_content(value or "") or ""
    return sanitized.replace("\r\n", "\n").replace("\r", "\n").strip()


def _yaml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=True)


def _slugify(value: str, fallback: str = "tweet") -> str:
    text = _SLUG_RE.sub("-", value.lower()).strip("-")
    return (text or fallback)[:96].strip("-") or fallback


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_findings(content: str) -> tuple[list[dict], list[dict]]:
    findings = []
    blocking = []
    for finding in scan_for_injection(content):
        safe = {
            "type": finding.get("type", "unknown"),
            "match_count": finding.get("match_count", 1),
        }
        findings.append(safe)
        if safe["type"] in _BLOCKING_FINDING_TYPES:
            blocking.append(safe)
    return findings, blocking


def _created_from_mtime(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return _utc_now()
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _created_from_zip(info: zipfile.ZipInfo) -> str:
    try:
        return datetime(*info.date_time, tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError):
        return _utc_now()


def _twitter_date(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback or _utc_now()
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _looks_like_tweet_export(source_rel: str) -> bool:
    normalized = source_rel.replace("\\", "/").lower()
    name = Path(normalized).name
    return (
        "tweet" in name
        or "/tweets/" in normalized
        or "/tweet/" in normalized
        or normalized.endswith("data/tweets.js")
    )


def _strip_js_assignment(raw: str) -> str:
    text = raw.strip()
    start_positions = [position for position in (text.find("["), text.find("{")) if position >= 0]
    if not start_positions:
        return text
    text = text[min(start_positions):].strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    return text


def _load_jsonish(raw: str, suffix: str) -> object | None:
    text = _strip_js_assignment(raw) if suffix == ".js" else raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _iter_jsonl(raw: str) -> list[object]:
    values = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return values


def _extract_tweet_candidates(value: object) -> list[dict]:
    if isinstance(value, list):
        candidates = []
        for item in value:
            candidates.extend(_extract_tweet_candidates(item))
        return candidates
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("tweet"), dict):
        return [value["tweet"]]
    for key in ("tweets", "tweet", "data", "items"):
        nested = value.get(key)
        if isinstance(nested, list):
            return _extract_tweet_candidates(nested)
    if value.get("full_text") or value.get("text"):
        return [value]
    return []


def _tweet_entities(tweet: dict) -> dict:
    entities = tweet.get("entities") if isinstance(tweet.get("entities"), dict) else {}
    hashtags = []
    mentions = []
    urls = []
    for item in entities.get("hashtags") or []:
        if isinstance(item, dict) and item.get("text"):
            hashtags.append(_clean_text(str(item["text"])))
    for item in entities.get("user_mentions") or []:
        if isinstance(item, dict):
            screen_name = item.get("screen_name") or item.get("name")
            if screen_name:
                mentions.append(_clean_text(str(screen_name)))
    for item in entities.get("urls") or []:
        if isinstance(item, dict):
            url = item.get("expanded_url") or item.get("url")
            if url:
                urls.append(_clean_text(str(url)))
    return {"hashtags": hashtags, "mentions": mentions, "urls": urls}


def _tweet_to_document(source_rel: str, tweet: dict, ordinal: int, created: str) -> dict | None:
    text = _clean_text(str(tweet.get("full_text") or tweet.get("text") or ""))
    if not text:
        return None
    if len(text) > TWITTER_TEXT_CHAR_LIMIT:
        text = text[:TWITTER_TEXT_CHAR_LIMIT].rstrip() + "\n\n[Truncated by ChimeraMemory X/Twitter import.]"
    tweet_id = str(tweet.get("id_str") or tweet.get("id") or _hash_text(f"{source_rel}\n{ordinal}\n{text}"))
    tweet_created = _twitter_date(tweet.get("created_at"), created)
    title = text.splitlines()[0][:120] or f"Tweet {tweet_id}"
    return {
        "source_path": source_rel,
        "source_id": tweet_id,
        "title": _clean_text(title),
        "created": tweet_created,
        "body": text,
        "entities": _tweet_entities(tweet),
        "favorite_count": str(tweet.get("favorite_count") or ""),
        "retweet_count": str(tweet.get("retweet_count") or ""),
        "source_app": _clean_text(str(tweet.get("source") or "")),
    }


def _documents_from_raw(source_rel: str, raw: str, created: str) -> list[dict]:
    suffix = Path(source_rel).suffix.lower()
    values: list[object]
    if suffix == ".jsonl":
        values = _iter_jsonl(raw)
    elif suffix in {".js", ".json"}:
        parsed = _load_jsonish(raw, suffix)
        values = [parsed] if parsed is not None else []
    else:
        text = _clean_text(raw)
        if not text:
            return []
        return [
            {
                "source_path": source_rel,
                "source_id": _hash_text(f"{source_rel}\n{text}"),
                "title": text.splitlines()[0][:120] or Path(source_rel).stem,
                "created": created or _utc_now(),
                "body": text[:TWITTER_TEXT_CHAR_LIMIT],
                "entities": {"hashtags": [], "mentions": [], "urls": []},
                "favorite_count": "",
                "retweet_count": "",
                "source_app": "",
            }
        ]

    documents = []
    ordinal = 0
    for value in values:
        for tweet in _extract_tweet_candidates(value):
            document = _tweet_to_document(source_rel, tweet, ordinal, created)
            ordinal += 1
            if document:
                documents.append(document)
    return documents


def _iter_twitter_documents(import_path: Path) -> list[dict]:
    path = Path(import_path)
    documents: list[dict] = []
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                suffix = Path(name).suffix.lower()
                if info.is_dir() or suffix not in _SUPPORTED_SUFFIXES or not _looks_like_tweet_export(name):
                    continue
                parts = [part for part in name.split("/") if part]
                if any(part in _SKIP_DIRS or part.startswith(".") for part in parts):
                    continue
                raw = archive.read(info).decode("utf-8", errors="replace")
                documents.extend(_documents_from_raw(name, raw, _created_from_zip(info)))
    elif path.is_dir():
        for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
            rel = file_path.relative_to(path).as_posix()
            if Path(rel).suffix.lower() not in _SUPPORTED_SUFFIXES or not _looks_like_tweet_export(rel):
                continue
            if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.split("/")):
                continue
            documents.extend(_documents_from_raw(rel, file_path.read_text(encoding="utf-8", errors="replace"), _created_from_mtime(file_path)))
    elif path.is_file() and path.suffix.lower() in _SUPPORTED_SUFFIXES:
        documents.extend(_documents_from_raw(path.name, path.read_text(encoding="utf-8", errors="replace"), _created_from_mtime(path)))
    else:
        raise ValueError("X/Twitter import path must be a supported file, directory, or zip export")
    return documents


def render_twitter_import_markdown(document: dict) -> str:
    """Render one governed X/Twitter import memory."""
    title = _clean_text(str(document.get("title") or "Tweet"))
    frontmatter = {
        "type": "episodic",
        "importance": 4,
        "created": document.get("created") or _utc_now(),
        "status": "active",
        "about": title,
        "tags": TWITTER_IMPORT_TAGS,
        "provenance_status": "imported",
        "confidence": 0.75,
        "lifecycle_status": "active",
        "review_status": "pending",
        "sensitivity_tier": "standard",
        "can_use_as_instruction": False,
        "can_use_as_evidence": True,
        "requires_user_confirmation": True,
    }
    entities = document.get("entities") or {}
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {_yaml_value(value)}")
    lines.extend(
        [
            "---",
            "",
            f"# {title}",
            "",
            "## Import Metadata",
            "- source: x-twitter",
            f"- source_path: {document.get('source_path') or ''}",
            f"- source_id: {document.get('source_id') or ''}",
            f"- schema: {TWITTER_IMPORT_SCHEMA_VERSION}",
            f"- favorite_count: {document.get('favorite_count') or ''}",
            f"- retweet_count: {document.get('retweet_count') or ''}",
            f"- source_app: {document.get('source_app') or ''}",
            f"- hashtags: {', '.join(entities.get('hashtags') or [])}",
            f"- mentions: {', '.join(entities.get('mentions') or [])}",
            f"- urls: {', '.join(entities.get('urls') or [])}",
            "",
            "## Tweet Text",
            "",
            _clean_text(str(document.get("body") or "")),
            "",
        ]
    )
    return "\n".join(lines)


def build_twitter_import_plans(
    import_path: Path,
    *,
    persona: str,
    limit: int = 200,
) -> dict:
    """Build governed markdown import plans from X/Twitter tweet exports."""
    persona = persona.strip()
    if not persona:
        return {"ok": False, "error": "persona required"}
    try:
        documents = _iter_twitter_documents(Path(import_path))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"ok": False, "error": f"failed to load X/Twitter export: {exc}"}

    plans = []
    for document in documents[: max(0, min(int(limit), 5000))]:
        rendered = render_twitter_import_markdown(document)
        findings, blocking_findings = _safe_findings(rendered)
        source_hash = _hash_text(f"{document.get('source_path')}\n{document.get('source_id')}\n{document.get('body')}")
        date_prefix = str(document.get("created") or _utc_now())[:10].replace("-", "")
        slug = _slugify(str(document.get("title") or document.get("source_id") or "tweet"))
        relative_path = f"memory/imports/twitter/{date_prefix}-{slug}-{source_hash[:10]}.md"
        plans.append(
            {
                "ok": True,
                "schema_version": TWITTER_IMPORT_SCHEMA_VERSION,
                "source": "x-twitter",
                "source_path": document.get("source_path"),
                "source_id": document.get("source_id"),
                "title": document.get("title"),
                "created": document.get("created"),
                "relative_path": relative_path,
                "guard_findings": findings,
                "blocking_findings": blocking_findings,
                "body": rendered,
            }
        )
    return {
        "ok": True,
        "schema_version": TWITTER_IMPORT_SCHEMA_VERSION,
        "source": "x-twitter",
        "persona": persona,
        "import_path": str(import_path),
        "document_count": len(documents),
        "plan_count": len(plans),
        "plans": plans,
    }


def write_twitter_import_file(personas_dir: Path, persona: str, plan: dict, *, force: bool = False) -> dict:
    """Write one planned X/Twitter import memory under the persona folder."""
    if not plan.get("ok"):
        return plan
    if plan.get("blocking_findings"):
        return {
            "ok": False,
            "error": "X/Twitter import content failed safety scan",
            "blocking_findings": plan["blocking_findings"],
            "source_id": plan.get("source_id"),
        }
    persona_root = resolve_persona_root(personas_dir, persona)
    if persona_root is None:
        return {"ok": False, "error": "persona root not found", "persona": persona}
    relative_path = Path(str(plan["relative_path"]))
    target = persona_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        return {
            "ok": True,
            "written": False,
            "skipped": True,
            "reason": "target exists",
            "path": str(target),
            "relative_path": str(relative_path).replace("\\", "/"),
            "source_id": plan.get("source_id"),
        }
    target.write_text(str(plan["body"]), encoding="utf-8", newline="\n")
    return {
        "ok": True,
        "written": True,
        "skipped": False,
        "path": str(target),
        "relative_path": str(relative_path).replace("\\", "/"),
        "source_id": plan.get("source_id"),
    }


def memory_import_twitter_archive(
    conn: sqlite3.Connection,
    personas_dir: Path,
    *,
    import_path: str,
    persona: str,
    index_file_func,
    pyramid_summary_builder,
    limit: int = 200,
    write: bool = False,
    force: bool = False,
    build_pyramid: bool = True,
    actor: str = "agent",
) -> dict:
    """Plan or write governed memories from X/Twitter tweet archives."""
    plans = build_twitter_import_plans(Path(import_path), persona=persona, limit=limit)
    if not plans.get("ok"):
        return plans

    preview = [
        {
            "source_path": plan.get("source_path"),
            "source_id": plan.get("source_id"),
            "title": plan.get("title"),
            "relative_path": plan.get("relative_path"),
            "guard_findings": plan.get("guard_findings", []),
            "blocking_findings": plan.get("blocking_findings", []),
        }
        for plan in plans.get("plans", [])
    ]
    audit_payload = {
        "schema_version": plans["schema_version"],
        "source": "x-twitter",
        "import_path": str(import_path),
        "document_count": plans.get("document_count", 0),
        "plan_count": len(preview),
        "write": bool(write),
        "build_pyramid": bool(build_pyramid),
    }
    if not write:
        record_memory_audit_event(
            conn,
            "memory_import_twitter_planned",
            persona=persona,
            target_kind="twitter_import",
            target_id=str(import_path),
            payload=audit_payload,
            actor=actor,
        )
        return {"ok": True, "written": False, "plans": preview, "summary": audit_payload}

    written = []
    skipped = []
    failed = []
    pyramid_built = 0
    for plan in plans.get("plans", []):
        result = write_twitter_import_file(personas_dir, persona, plan, force=force)
        if not result.get("ok"):
            failed.append(result)
            continue
        if result.get("skipped"):
            skipped.append(result)
            continue
        full_path = Path(result["path"])
        relative_path = result["relative_path"]
        indexed = index_file_func(conn, persona, relative_path, full_path)
        row = conn.execute(
            "SELECT id FROM memory_files WHERE path = ?",
            (str(full_path).replace("\\", "/"),),
        ).fetchone()
        file_id = row[0] if row else None
        pyramid = None
        if build_pyramid and file_id is not None:
            pyramid = pyramid_summary_builder(
                conn,
                file_path=str(file_id),
                persona=persona,
                force=force,
                actor=actor,
            )
            if pyramid.get("ok") and pyramid.get("built"):
                pyramid_built += 1
        written.append(
            {
                **result,
                "file_id": file_id,
                "indexed": indexed,
                "pyramid_built": bool(pyramid and pyramid.get("built")),
            }
        )
        record_memory_audit_event(
            conn,
            "memory_import_twitter_tweet",
            persona=persona,
            target_kind="memory_file",
            target_id=str(file_id or relative_path),
            payload={
                "source": "x-twitter",
                "source_path": plan.get("source_path"),
                "source_id": plan.get("source_id"),
                "relative_path": relative_path,
                "file_id": file_id,
                "indexed": indexed,
                "pyramid_built": bool(pyramid and pyramid.get("built")),
            },
            actor=actor,
            commit=False,
        )
    record_memory_audit_event(
        conn,
        "memory_import_twitter_completed",
        persona=persona,
        target_kind="twitter_import",
        target_id=str(import_path),
        payload={
            **audit_payload,
            "written_count": len(written),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "pyramid_built_count": pyramid_built,
        },
        actor=actor,
        commit=False,
    )
    conn.commit()
    return {
        "ok": not failed,
        "written": True,
        "summary": {
            "written_count": len(written),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "pyramid_built_count": pyramid_built,
            "plan_count": len(preview),
        },
        "written_items": written,
        "skipped_items": skipped,
        "failed_items": failed,
    }
