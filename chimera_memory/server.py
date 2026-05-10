"""MCP server for chimera-memory. Exposes discord_recall and transcript_stats tools."""

import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def get_default_jsonl_dir() -> Path:
    """Auto-detect the JSONL directory from CWD-based project path."""
    home = Path.home()
    cwd = Path.cwd().resolve()

    # Claude Code project dir naming: non-alnum chars become hyphens
    import re
    project_key = re.sub(r'[^a-zA-Z0-9]', '-', str(cwd))
    project_dir = home / ".claude" / "projects" / project_key

    if project_dir.exists():
        return project_dir

    # Try case-insensitive match on Windows
    projects_dir = home / ".claude" / "projects"
    if projects_dir.exists():
        for d in projects_dir.iterdir():
            if d.is_dir() and d.name.lower() == project_key.lower():
                return d

    # Fallback: return the expected path even if it doesn't exist yet
    return project_dir


def get_default_db_path() -> Path:
    """Default database path. Centralized in user home directory."""
    db_dir = Path.home() / ".chimera-memory"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "transcript.db"


def _resolved_path(value: str | Path | None) -> str | None:
    if value is None:
        return None
    return str(Path(value).expanduser())


def _env_provenance(key: str) -> dict[str, str]:
    return {"source": "env", "key": key}


def _missing_provenance(key: str) -> dict[str, str]:
    return {"source": "missing", "key": key}


def _identity_field(value: object, key: str) -> tuple[object, dict[str, str]]:
    if value is None:
        return None, _missing_provenance(key)
    return value, _env_provenance(key)


def resolve_memory_whereami() -> dict:
    """Resolve Chimera Memory runtime paths and identity with provenance."""
    from .config import load_config_with_provenance
    from .identity import load_identity_from_env

    config, config_provenance = load_config_with_provenance()
    identity = load_identity_from_env()

    resolved: dict[str, object] = {}
    provenance: dict[str, dict[str, str]] = {}

    db_env = os.environ.get("TRANSCRIPT_DB_PATH", "").strip()
    if db_env:
        resolved["db_path"] = _resolved_path(db_env)
        provenance["db_path"] = _env_provenance("TRANSCRIPT_DB_PATH")
    else:
        resolved["db_path"] = str(get_default_db_path())
        provenance["db_path"] = {
            "source": "default",
            "function": "get_default_db_path",
        }

    jsonl_dir = config.get("jsonl_dir")
    if jsonl_dir:
        resolved["jsonl_dir"] = _resolved_path(str(jsonl_dir))
        provenance["jsonl_dir"] = config_provenance.get("jsonl_dir", {"source": "unknown"})
    else:
        resolved["jsonl_dir"] = str(get_default_jsonl_dir())
        provenance["jsonl_dir"] = {
            "source": "default",
            "function": "get_default_jsonl_dir",
        }

    resolved["transcript_persona"] = config.get("persona")
    provenance["transcript_persona"] = config_provenance.get("persona", {"source": "unknown"})

    resolved["client"] = config.get("client")
    provenance["client"] = config_provenance.get("client", {"source": "unknown"})

    field_specs = {
        "persona_id": (identity.persona_id, "CHIMERA_PERSONA_ID"),
        "persona_name": (identity.persona_name, "CHIMERA_PERSONA_NAME"),
        "persona_root": (_resolved_path(identity.persona_root), "CHIMERA_PERSONA_ROOT"),
        "personas_dir": (_resolved_path(identity.personas_dir), "CHIMERA_PERSONAS_DIR"),
        "shared_root": (_resolved_path(identity.shared_root), "CHIMERA_SHARED_ROOT"),
    }
    for field, (value, env_key) in field_specs.items():
        resolved[field], provenance[field] = _identity_field(value, env_key)

    persona_db_root = os.environ.get("CHIMERA_MEMORY_PERSONA_DB_ROOT", "").strip()
    if persona_db_root:
        resolved["persona_db_root"] = _resolved_path(persona_db_root)
        provenance["persona_db_root"] = _env_provenance("CHIMERA_MEMORY_PERSONA_DB_ROOT")
    else:
        resolved["persona_db_root"] = None
        provenance["persona_db_root"] = {
            "source": "default",
            "function": "chimera_memory.paths.persona_db_root",
        }

    warnings = identity.warnings()
    if persona_db_root and db_env:
        warnings.append("CHIMERA_MEMORY_PERSONA_DB_ROOT is set but TRANSCRIPT_DB_PATH overrides db_path")

    return {
        "resolved": resolved,
        "provenance": provenance,
        "warnings": warnings,
    }


def create_server():
    """Create and configure the MCP server with tools."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        log.error("mcp package not installed. Install with: pip install chimera-memory[mcp]")
        sys.exit(1)

    server = FastMCP("chimera-memory")

    # Load config (env vars > config file > defaults)
    from .config import load_config, ensure_config_exists
    ensure_config_exists()
    _config = load_config()
    from .identity import load_identity_from_env
    _identity = load_identity_from_env()
    if _identity.persona_id or _identity.persona_name or _identity.client:
        log.info(
            "persona identity: id=%s name=%s client=%s",
            _identity.persona_id or "-",
            _identity.display_name,
            _identity.client or "-",
        )
    for warning in _identity.warnings():
        log.warning("persona identity warning: %s", warning)

    # Lazy-init DB and indexer
    _state = {}

    def _get_db():
        if "db" not in _state:
            from .db import TranscriptDB
            db_path = os.environ.get("TRANSCRIPT_DB_PATH", str(get_default_db_path()))
            _state["db"] = TranscriptDB(db_path)
        return _state["db"]

    def _get_indexer():
        if "indexer" not in _state:
            from .indexer import Indexer
            jsonl_dir = _config.get("jsonl_dir") or os.environ.get("TRANSCRIPT_JSONL_DIR") or str(get_default_jsonl_dir())
            persona = _config.get("persona")
            client = _config.get("client") or os.environ.get("CHIMERA_CLIENT")
            _state["indexer"] = Indexer(_get_db(), jsonl_dir, persona=persona, parser_format=client)
        return _state["indexer"]

    @server.tool()
    def memory_whereami() -> str:
        """Show resolved Chimera Memory runtime paths, identity, and provenance."""
        return json.dumps(resolve_memory_whereami(), indent=2)

    @server.tool()
    def discord_recall(
        channel: str | None = None,
        limit: int = 50,
        search: str | None = None,
        after: str | None = None,
        before: str | None = None,
        direction: str | None = None,
        author: str | None = None,
    ) -> str:
        """Recall Discord conversation history from indexed session transcripts.

        This replaces fetch_messages with zero API calls and zero rate limits.
        Queries a local SQLite database built from Claude Code JSONL session files.

        Args:
            channel: Filter by Discord chat_id
            limit: Maximum messages to return (default 50)
            search: Full-text search query (e.g. "umbrella research")
            after: Only messages after this ISO timestamp
            before: Only messages before this ISO timestamp
            direction: Filter by 'inbound' or 'outbound'
            author: Filter by author username

        Returns:
            Formatted conversation history with timestamps, authors, and content.
        """
        from .search import discord_recall as _recall

        results = _recall(
            _get_db(),
            channel=channel,
            limit=limit,
            search=search,
            after=after,
            before=before,
            direction=direction,
            author=author,
        )

        if not results:
            return "No messages found matching your query."

        # Format as readable conversation
        lines = []
        for msg in results:
            ts = msg.get("timestamp", "?")[:19]
            author_name = msg.get("author", "unknown")
            entry_type = msg.get("entry_type", "")
            content = msg.get("content", "")
            msg_id = msg.get("message_id", "")
            chat_id = msg.get("chat_id", "")

            # Direction indicator
            if entry_type == "discord_inbound":
                prefix = f"[{ts}] {author_name}"
            elif entry_type == "discord_outbound":
                prefix = f"[{ts}] → (sent)"
            elif entry_type == "user_message":
                prefix = f"[{ts}] USER"
            elif entry_type == "assistant_message":
                prefix = f"[{ts}] ASSISTANT"
            else:
                prefix = f"[{ts}] {entry_type}"

            # Include IDs for react/reply/edit operations
            id_suffix = ""
            if msg_id:
                id_suffix = f" [msg:{msg_id}]"
            if chat_id:
                id_suffix += f" [ch:{chat_id}]"

            lines.append(f"{prefix}{id_suffix}")
            if content:
                lines.append(content)
            lines.append("")

        return "\n".join(lines)

    @server.tool()
    def transcript_stats() -> str:
        """Get statistics about the transcript database.

        Shows entry counts, session counts, DB size, last import time,
        and breakdowns by entry type and source.
        """
        from .search import transcript_stats as _stats

        stats = _stats(_get_db())

        lines = [
            "## Transcript Database Stats",
            f"**Entries:** {stats['entry_count']:,}",
            f"**Sessions:** {stats['session_count']}",
            f"**DB Size:** {stats['db_size_mb']:.1f} MB",
            f"**Last Entry:** {stats.get('last_entry', 'none')}",
            f"**Files Indexed:** {stats.get('files_indexed', 0)}",
            f"**Last Import:** {stats.get('last_import', 'never')}",
            "",
            "**Entry Types:**",
        ]
        for etype, count in stats.get("entry_types", {}).items():
            lines.append(f"  {etype}: {count:,}")

        lines.append("")
        lines.append("**Sources:**")
        for source, count in stats.get("sources", {}).items():
            lines.append(f"  {source}: {count:,}")

        if stats.get("session_dispositions"):
            lines.append("")
            lines.append("**Session Dispositions:**")
            for disp, count in stats["session_dispositions"].items():
                lines.append(f"  {disp}: {count}")

        return "\n".join(lines)

    @server.tool()
    def transcript_backfill() -> str:
        """Index all historical JSONL session files into the transcript database.

        Run this once on first setup, or after clearing the database.
        Skips files that haven't changed since last import.
        """
        indexer = _get_indexer()
        progress = {"current": 0, "total": 0}

        def _progress(current, total):
            progress["current"] = current
            progress["total"] = total

        indexer.backfill(progress_callback=_progress)
        stats = _get_db().stats()

        return (
            f"Backfill complete.\n"
            f"Files processed: {progress['total']}\n"
            f"Total entries: {stats['entry_count']:,}\n"
            f"Total sessions: {stats['session_count']}\n"
            f"DB size: {stats['db_size_mb']:.1f} MB"
        )

    @server.tool()
    def semantic_search(
        query: str,
        limit: int = 20,
        channel: str | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> str:
        """Hybrid semantic + keyword search across all transcripts.

        Combines FTS5 keyword matching with vector similarity (cosine) via
        Reciprocal Rank Fusion. Finds both exact matches AND semantically
        similar content (e.g. "car" finds "vehicle").

        Results are re-ranked by recency, session affinity, and content richness.

        Requires embeddings to be built (run transcript_backfill first).
        Falls back to keyword-only search if embeddings aren't available.
        """
        from .search import hybrid_search

        results = hybrid_search(
            _get_db(), query, limit=limit, channel=channel,
            after=after, before=before,
        )

        if not results:
            return "No results found."

        lines = []
        for msg in results:
            ts = msg.get("timestamp", "?")[:19]
            author_name = msg.get("author", "unknown")
            entry_type = msg.get("entry_type", "")
            content = msg.get("content", "")
            msg_id = msg.get("message_id", "")

            if entry_type == "discord_inbound":
                prefix = f"[{ts}] {author_name}"
            elif entry_type == "discord_outbound":
                prefix = f"[{ts}] -> (sent)"
            else:
                prefix = f"[{ts}] {entry_type}"

            id_suffix = f" [msg:{msg_id}]" if msg_id else ""
            lines.append(f"{prefix}{id_suffix}")
            if content:
                lines.append(content[:300] + ("..." if len(content) > 300 else ""))
            lines.append("")

        return "\n".join(lines)

    @server.tool()
    def embed_transcripts() -> str:
        """Generate embeddings for all transcript entries that don't have them yet.

        Run this after backfill to enable semantic search. Only embeds
        conversation content (user messages, assistant messages, Discord messages).
        Tool results and system entries are skipped.

        Uses bge-small-en-v1.5 (23MB ONNX model, runs locally, no API calls).
        CPU usage is capped to 75% of available cores.

        This may take several minutes on first run (e.g. 5,000 entries ~ 4 minutes).
        """
        from .embeddings import embed_transcript_entries, init_embedding_table
        import os

        db = _get_db()
        cores_used = max(1, int((os.cpu_count() or 4) * 0.75))

        with db.connection() as conn:
            init_embedding_table(conn)
            # Check how many need embedding
            pending = conn.execute("""
                SELECT COUNT(*) FROM transcript t
                LEFT JOIN transcript_embeddings e ON e.transcript_id = t.id
                WHERE e.transcript_id IS NULL
                  AND t.content IS NOT NULL AND t.content != ''
                  AND t.entry_type IN ('user_message', 'assistant_message', 'discord_inbound', 'discord_outbound')
            """).fetchone()[0]

        if pending == 0:
            return "All entries already have embeddings. Semantic search is ready."

        with db.connection() as conn:
            init_embedding_table(conn)
            count = embed_transcript_entries(db, conn)

        return (
            f"Embedded {count} entries using {cores_used}/{os.cpu_count()} threads.\n"
            f"Semantic search is now available.\n"
            f"Use semantic_search(query) to find content by meaning, not just keywords."
        )

    @server.tool()
    def discord_recall_index(
        channel: str | None = None,
        limit: int = 50,
        search: str | None = None,
        after: str | None = None,
        before: str | None = None,
        direction: str | None = None,
        author: str | None = None,
    ) -> str:
        """Search conversation history and return a compact index (~100 tokens/result).

        USE THIS FIRST instead of discord_recall to save tokens.
        Returns: ID, timestamp, author, and 80-char preview for each result.
        Then call discord_detail with specific IDs to get full content.

        3-step workflow:
        1. discord_recall_index(search="topic") -> scan the index
        2. Pick the IDs that look relevant
        3. discord_detail(ids=[...]) -> get full content

        This saves 5-10x tokens compared to fetching everything at once.
        """
        from .search import discord_recall_index as _index

        results = _index(
            _get_db(), channel=channel, limit=limit, search=search,
            after=after, before=before, direction=direction, author=author,
        )

        if not results:
            return "No messages found matching your query."

        lines = ["ID | Timestamp | Author | Preview"]
        lines.append("---|-----------|--------|--------")
        for r in results:
            eid = r.get("id", "?")
            ts = r.get("timestamp", "?")
            auth = r.get("author", "?")
            preview = r.get("preview", "")
            mid = r.get("message_id", "")
            mid_str = f" [msg:{mid}]" if mid else ""
            lines.append(f"{eid} | {ts} | {auth} | {preview}{mid_str}")

        return "\n".join(lines)

    @server.tool()
    def discord_detail(ids: list[int]) -> str:
        """Fetch full content for specific transcript entries by ID.

        Use after discord_recall_index to get full content for the entries you care about.
        Pass the IDs from the index results.
        """
        from .search import discord_detail as _detail

        results = _detail(_get_db(), ids)

        if not results:
            return "No entries found for the given IDs."

        lines = []
        for msg in results:
            ts = msg.get("timestamp", "?")[:19]
            author_name = msg.get("author", "unknown")
            entry_type = msg.get("entry_type", "")
            content = msg.get("content", "")
            msg_id = msg.get("message_id", "")
            chat_id = msg.get("chat_id", "")

            if entry_type == "discord_inbound":
                prefix = f"[{ts}] {author_name}"
            elif entry_type == "discord_outbound":
                prefix = f"[{ts}] → (sent)"
            elif entry_type == "user_message":
                prefix = f"[{ts}] USER"
            elif entry_type == "assistant_message":
                prefix = f"[{ts}] ASSISTANT"
            else:
                prefix = f"[{ts}] {entry_type}"

            id_suffix = ""
            if msg_id:
                id_suffix = f" [msg:{msg_id}]"
            if chat_id:
                id_suffix += f" [ch:{chat_id}]"

            lines.append(f"{prefix}{id_suffix}")
            if content:
                lines.append(content)
            lines.append("")

        return "\n".join(lines)

    @server.tool()
    def session_list(
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        persona: str | None = None,
        disposition: str | None = None,
    ) -> str:
        """Browse sessions with summaries, dispositions, and date ranges.

        Shows what sessions happened, when, how long, and how they ended.
        Filter by date range, persona, or disposition (COMPLETED/IN_PROGRESS/INTERRUPTED).
        """
        from .search import session_list as _list

        results = _list(
            _get_db(), limit=limit, after=after, before=before,
            persona=persona, disposition=disposition,
        )

        if not results:
            return "No sessions found."

        lines = []
        for s in results:
            title = s.get("title") or "Untitled"
            sid = s.get("session_id", "?")[:8]
            started = (s.get("started_at") or "?")[:16]
            ended = (s.get("ended_at") or "?")[:16]
            disp = s.get("disposition") or "unknown"
            exchanges = s.get("exchange_count", 0)
            persona_name = s.get("persona") or ""
            branch = s.get("git_branch") or ""

            lines.append(f"**{title}** ({sid}...)")
            lines.append(f"  {started} → {ended} | {disp} | {exchanges} exchanges")
            if persona_name or branch:
                extra = []
                if persona_name:
                    extra.append(f"persona: {persona_name}")
                if branch:
                    extra.append(f"branch: {branch}")
                lines.append(f"  {' | '.join(extra)}")
            lines.append("")

        return "\n".join(lines)

    # ─── Curated Memory Tools ────────────────────────────────────────

    def _get_memory_conn():
        """Get a connection with memory tables initialized."""
        if "memory_conn" not in _state:
            from .memory import init_memory_tables
            db = _get_db()
            conn = db._connect()
            init_memory_tables(conn)
            _state["memory_conn"] = conn
        return _state["memory_conn"]

    def _ensure_memory_indexed():
        """Ensure memory files are indexed on first use, and start the live watcher.

        Graceful degradation (Day 25 fix): if embeddings fail (e.g. ONNX cache
        missing/broken), fall back to FTS-only reindex rather than crashing the
        server. The whole chimera-memory session dying because the embedding
        model can't load is what caused every previous MCP disconnect.

        Step-granular logging (Day 25 fix): each phase logs entry + duration so
        the NEXT slow-path disconnect shows which step hung.
        """
        if "memory_indexed" not in _state:
            import logging as _logging, time as _time
            _log = _logging.getLogger("chimera_memory.indexing")
            _log.info("_ensure_memory_indexed: starting")
            t_total = _time.time()

            _log.info("  [1/4] importing memory module")
            from .memory import full_reindex, start_memory_watcher

            _log.info("  [2/4] resolving personas_dir")
            personas_dir = Path(os.environ.get("CHIMERA_PERSONAS_DIR", "C:/Github/ChimeraPersonas/personas"))
            _log.info("     personas_dir=%s", personas_dir)

            _log.info("  [3/4] getting memory conn")
            conn = _get_memory_conn()

            _log.info("  [4/4] full_reindex starting (embed=True)")
            t0 = _time.time()
            try:
                full_reindex(conn, personas_dir, embed=True)
                _state["memory_indexed"] = "full"
                _log.info("  [4/4] full_reindex COMPLETED in %.2fs (mode=full)", _time.time() - t0)
            except Exception as exc:
                _log.warning(
                    "  [4/4] full_reindex with embeddings FAILED in %.2fs: %s",
                    _time.time() - t0, exc
                )
                t1 = _time.time()
                try:
                    full_reindex(conn, personas_dir, embed=False)
                    _state["memory_indexed"] = "fts-only"
                    _log.info(
                        "  [4/4] FTS-only fallback succeeded in %.2fs. "
                        "Run `chimera-memory reindex` to rebuild embeddings later.",
                        _time.time() - t1
                    )
                except Exception:
                    _log.exception(
                        "  [4/4] FTS-only fallback ALSO FAILED in %.2fs — memory_search unavailable",
                        _time.time() - t1
                    )
                    _state["memory_indexed"] = "failed"
                    # Don't re-raise: server stays alive, specific tools may error out.

            _log.info("_ensure_memory_indexed: done in %.2fs total (mode=%s)",
                      _time.time() - t_total, _state.get("memory_indexed"))

            # Live file watcher: incremental upsert/delete on .md changes.
            # Opens its own connections per event, so it's safe alongside the cached memory_conn.
            try:
                observer = start_memory_watcher(_get_db(), personas_dir)
                if observer is not None:
                    _state["memory_watcher"] = observer
            except Exception:
                _logging.getLogger(__name__).exception("Failed to start memory file watcher")

    @server.tool()
    def memory_search(query: str, persona: str | None = None, limit: int = 20) -> str:
        """Full-text search across all persona memory files. Returns paths, snippets, and metadata."""
        _ensure_memory_indexed()
        from .memory import memory_search as _search
        results = _search(_get_memory_conn(), query, persona, limit)
        if not results:
            return "No memories found matching your query."
        lines = []
        for r in results:
            imp = f" [importance:{r['importance']}]" if r.get("importance") else ""
            lines.append(f"**{r['relative_path']}** ({r['persona']}){imp}")
            lines.append(f"  {r.get('snippet', '')}")
            lines.append("")
        return "\n".join(lines)

    @server.tool()
    def memory_query(
        persona: str | None = None, type: str | None = None,
        min_importance: int | None = None, max_importance: int | None = None,
        status: str | None = None, tag: str | None = None,
        about: str | None = None, sort_by: str = "importance",
        sort_order: str = "DESC", limit: int = 50,
    ) -> str:
        """Query memories by frontmatter fields (type, importance, status, tags, etc)."""
        _ensure_memory_indexed()
        from .memory import memory_query as _query
        results = _query(_get_memory_conn(), persona=persona, fm_type=type,
                         min_importance=min_importance, max_importance=max_importance,
                         status=status, tag=tag, about=about, sort_by=sort_by,
                         sort_order=sort_order, limit=limit)
        if not results:
            return "No memories match your criteria."
        lines = []
        for r in results:
            imp = r.get("importance", "?")
            lines.append(f"[{imp}] {r['relative_path']} ({r['persona']}) — {r.get('type', '?')} — {r.get('about', '')}")
        return "\n".join(lines)

    @server.tool()
    def memory_recall(concept: str, persona: str | None = None, limit: int = 10) -> str:
        """Semantic recall: find memories most similar to a concept or question. Uses embeddings."""
        _ensure_memory_indexed()
        from .memory import memory_recall as _recall
        results = _recall(_get_memory_conn(), concept, persona, limit)
        if not results:
            return "No similar memories found."
        lines = []
        for r in results:
            lines.append(f"[{r.get('similarity', 0):.3f}] {r['relative_path']} ({r['persona']}) — {r.get('about', '')}")
        return "\n".join(lines)

    @server.tool()
    def memory_stats(persona: str | None = None) -> str:
        """Get memory corpus statistics: file counts by type, status, persona."""
        _ensure_memory_indexed()
        from .memory import memory_stats as _stats
        stats = _stats(_get_memory_conn(), persona)
        lines = [f"**Total files:** {stats['total_files']}"]
        if stats.get("by_type"):
            lines.append("**By type:**")
            for t, c in stats["by_type"].items():
                lines.append(f"  {t}: {c}")
        if stats.get("by_status"):
            lines.append("**By status:**")
            for s, c in stats["by_status"].items():
                lines.append(f"  {s}: {c}")
        if stats.get("by_persona"):
            lines.append("**By persona:**")
            for p, c in stats["by_persona"].items():
                lines.append(f"  {p}: {c}")
        return "\n".join(lines)

    @server.tool()
    def memory_gaps(persona: str | None = None) -> str:
        """Detect knowledge gaps using graph analysis. Finds disconnected clusters and isolated files."""
        _ensure_memory_indexed()
        from .memory import memory_gaps as _gaps
        result = _gaps(_get_memory_conn(), persona)
        if "error" in result:
            return result["error"]
        lines = [
            f"**Nodes:** {result['total_nodes']} | **Edges:** {result['total_edges']} | **Components:** {result['connected_components']}",
        ]
        if result.get("clusters"):
            lines.append("\n**Clusters:**")
            for c in result["clusters"]:
                lines.append(f"  Size {c['size']}: {', '.join(c['top_concepts'][:5])}")
        if result.get("isolated_files"):
            lines.append(f"\n**Isolated files:** {len(result['isolated_files'])}")
            for f in result["isolated_files"][:5]:
                lines.append(f"  {f['path']}")
        return "\n".join(lines)

    @server.tool()
    def memory_guard(content: str) -> str:
        """Scan text for prompt injection, exfiltration, invisible unicode, and credential leaks."""
        from .sanitizer import scan_for_injection
        findings = scan_for_injection(content)
        if not findings:
            return "Clean. No issues detected."
        lines = [f"**{len(findings)} issue(s) found:**"]
        for f in findings:
            lines.append(f"  [{f['type']}] {f.get('sample', f.get('pattern', ''))}")
        return "\n".join(lines)

    @server.tool()
    def memory_consolidation_report(persona: str | None = None) -> str:
        """Dry-run analysis of memory consolidation. Shows what would be decayed, marked stale, or archived."""
        _ensure_memory_indexed()
        from .memory import consolidation_report
        result = consolidation_report(_get_memory_conn(), persona)
        s = result["summary"]
        lines = [
            f"**Analyzed:** {result['total_analyzed']} files",
            f"**Would mark stale:** {s['would_mark_stale']}",
            f"**Would archive:** {s['would_archive']}",
        ]
        if result.get("stale_candidates"):
            lines.append("\n**Stale candidates:**")
            for c in result["stale_candidates"][:5]:
                lines.append(f"  {c['path']} (importance: {c['importance']} -> {c['decayed']})")
        return "\n".join(lines)

    @server.tool()
    def memory_reindex() -> str:
        """Force a full reindex of all persona memory files."""
        from .memory import full_reindex
        personas_dir = Path(os.environ.get("CHIMERA_PERSONAS_DIR", "C:/Github/ChimeraPersonas/personas"))
        conn = _get_memory_conn()
        updated = full_reindex(conn, personas_dir, embed=True)
        return f"Reindexed. {updated} files new or updated."

    @server.tool()
    def memory_mark_failure(file_path: str) -> str:
        """Increment failure_count for a memory that led to wrong advice or a bad decision."""
        from .memory import mark_failure
        if mark_failure(_get_memory_conn(), file_path):
            return f"Marked failure on {file_path}. It will rank lower in future searches."
        return f"File not found: {file_path}"

    # ─── Cognitive Layer Tools ───────────────────────────────────────

    @server.tool()
    def memory_decay_report(persona: str | None = None) -> str:
        """Show how memory importance has decayed based on access patterns.

        Uses per-type exponential decay rates:
        - Facts/entities: very slow (0.005/day)
        - Procedural: slowest (0.003/day, load-bearing knowledge)
        - Episodes: moderate (0.010/day)
        - Opinions: fastest (0.020/day)
        """
        _ensure_memory_indexed()
        from .cognitive import apply_salience_decay
        result = apply_salience_decay(_get_memory_conn(), persona)
        return (
            f"Analyzed {result['total_analyzed']} memories.\n"
            f"Decayed from original importance: {result['decayed_count']}\n\n"
            f"Decay rates by type:\n" +
            "\n".join(f"  {t}: {r}/day" for t, r in result["decay_rates"].items())
        )

    @server.tool()
    def memory_surprise(persona: str | None = None, limit: int = 20) -> str:
        """Show novelty scores for memories. High surprise = unique knowledge. Low = redundant.

        Computed via nearest-neighbor similarity in embedding space. Zero LLM calls.
        """
        _ensure_memory_indexed()
        from .cognitive import score_all_surprise
        results = score_all_surprise(_get_memory_conn(), persona)
        if not results:
            return "No embedded memories found. Run memory_reindex first."
        lines = ["Surprise | Importance | Type | Path"]
        lines.append("---------|-----------|------|-----")
        for r in results[:limit]:
            imp = r.get('importance') or '?'
            typ = r.get('type') or '?'
            lines.append(f"{r['surprise']:.3f}    | {str(imp):>9} | {str(typ):<4} | {r['path']}")
        return "\n".join(lines)

    @server.tool()
    def memory_zones(persona: str | None = None) -> str:
        """Show zone assignments for all memories.

        Zones determine loading behavior:
        - CORE (>=0.80): always loaded every session
        - ACTIVE (>=0.60): loaded when tags match current task
        - PASSIVE (>=0.30): loaded only on direct query
        - ARCHIVE (<0.30): never auto-loaded

        Score = confidence + frequency + recency - failure_penalty
        """
        _ensure_memory_indexed()
        from .cognitive import compute_all_zones
        results, counts = compute_all_zones(_get_memory_conn(), persona)
        if not results:
            return "No memories with importance scores found."
        lines = [
            f"**Zone distribution:** core={counts['core']}, active={counts['active']}, passive={counts['passive']}, archive={counts['archive']}",
            "",
            "Score | Zone    | Importance | Access | Days | Failures | Path",
            "------|---------|-----------|--------|------|----------|-----",
        ]
        for r in results[:30]:
            lines.append(
                f"{r['score']:.3f} | {r['zone']:<7} | {r.get('importance', '?'):>9} | "
                f"{r.get('access_count', 0):>6} | {r.get('days_since_access', 0):>4.0f} | "
                f"{r.get('failure_count', 0):>8} | {r['path']}"
            )
        return "\n".join(lines)

    return server


def _configure_diagnostic_logging() -> Path:
    """Add a RotatingFileHandler so we have server-side logs across MCP disconnects.

    Claude Code does not persist MCP server stderr, so previously every crash
    was a black box. This writes to `~/.chimera-memory/server.log` with 5MB
    rotation, 3 backups. Stays alongside stderr — doesn't replace it.
    """
    import sys as _sys
    import traceback as _traceback
    from logging.handlers import RotatingFileHandler

    log_dir = Path.home() / ".chimera-memory"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "server.log"

    file_handler = RotatingFileHandler(
        str(log_path), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(process)d | %(name)s | %(levelname)s | %(message)s"
    ))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().setLevel(logging.DEBUG)

    # Unhandled-exception hook — captures any crash before the process dies.
    def _excepthook(exc_type, exc_value, exc_tb):
        logging.getLogger("chimera_memory").critical(
            "UNHANDLED EXCEPTION — server is about to die\n%s",
            "".join(_traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        # Chain to default so CC still sees it on stderr.
        _sys.__excepthook__(exc_type, exc_value, exc_tb)

    _sys.excepthook = _excepthook
    return log_path


def _prewarm_embeddings() -> None:
    """Eager-load the embedding model at server startup.

    Day 25 fix: previously, fastembed's cache-validation-against-HuggingFace
    happened on the first tool call that needed embeddings. That validation
    blocked 10+ minutes on slow networks, outrunning Claude Code's tool-call
    timeout and causing `[Tool result missing due to internal error]`.

    Pre-warming at startup moves the slow path to server boot (where CC doesn't
    time out) so every subsequent tool call is fast. Also sets HF_HUB_OFFLINE
    if the cache looks intact, to skip the HF validation round-trip entirely.
    """
    log = logging.getLogger("chimera_memory.prewarm")
    try:
        # If local cache looks intact, skip HF validation on subsequent imports.
        from pathlib import Path
        cache_root = Path.home() / ".chimera-memory" / "cache"
        onnx_files = list(cache_root.rglob("model_optimized.onnx")) if cache_root.exists() else []
        if onnx_files:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            log.info("Cache looks intact (%d ONNX file(s)). Set HF_HUB_OFFLINE=1 to skip HF roundtrip.", len(onnx_files))
        else:
            log.info("Cache empty — fastembed will download the model. This is a one-time cost.")

        log.info("Pre-warming embedding model (this blocks startup but prevents tool-call timeouts later)...")
        import time
        t0 = time.time()
        from .embeddings import _get_model
        _get_model()
        log.info("Embedding model pre-warmed in %.1fs", time.time() - t0)
    except Exception:
        log.exception(
            "Pre-warm FAILED. Server will start anyway; memory_search tools will degrade "
            "to FTS-only per the _ensure_memory_indexed fallback."
        )


def _start_transcript_indexer() -> object | None:
    """Backfill any JSONL files written while the server was down, then start a
    live watcher that ingests new entries incrementally.

    Day 25 fix: previously the Indexer was lazy-instantiated only when the
    `transcript_backfill` MCP tool was invoked, and even then it did a one-shot
    backfill and stopped. The `start_watching()` code existed but was never
    called. Result: every session between `transcript_backfill` invocations
    accumulated JSONL entries that never made it into the DB. CEO's Day 22-24
    transcripts were invisible to memory_search and semantic_search for 3 days.

    This fix: backfill on startup (catches up missed JSONL) + start_watching
    (stay live — watchdog on_modified fires within ~100ms, 30s poll safety net).
    """
    log = logging.getLogger("chimera_memory.indexer-bootstrap")
    try:
        from .db import TranscriptDB
        from .indexer import Indexer
        from .config import load_config

        db_path = os.environ.get("TRANSCRIPT_DB_PATH", str(get_default_db_path()))
        db = TranscriptDB(db_path)
        cfg = load_config()
        jsonl_dir = cfg.get("jsonl_dir") or os.environ.get("TRANSCRIPT_JSONL_DIR") or str(get_default_jsonl_dir())
        persona = cfg.get("persona")
        client = cfg.get("client") or os.environ.get("CHIMERA_CLIENT")
        indexer = Indexer(db, jsonl_dir, persona=persona, parser_format=client)

        log.info("Backfilling transcripts from %s ...", jsonl_dir)
        stats = indexer.backfill()
        log.info("Backfill complete: %s", stats)

        log.info("Starting live file watcher ...")
        observer = indexer.start_watching()
        log.info("Transcript watcher active (watchdog + 30s poll safety net)")
        return indexer
    except Exception:
        log.exception(
            "Transcript indexer bootstrap FAILED. Server will start anyway; "
            "discord_recall / semantic_search will return stale data until manual `transcript_backfill`."
        )
        return None


def main():
    """Entry point for running the MCP server."""
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(levelname)s | %(message)s")
    log_path = _configure_diagnostic_logging()
    logging.getLogger("chimera_memory").info(
        "chimera-memory server starting (pid=%s, log=%s)", os.getpid(), log_path
    )
    _prewarm_embeddings()
    _indexer = _start_transcript_indexer()  # keep reference so watcher threads don't get GC'd
    try:
        server = create_server()
        server.run(transport="stdio")
    except KeyboardInterrupt:
        logging.getLogger("chimera_memory").info("shutdown via KeyboardInterrupt")
    except Exception:
        logging.getLogger("chimera_memory").exception("server.run() crashed")
        raise
    finally:
        if _indexer is not None:
            try:
                _indexer.stop_watching()
            except Exception:
                pass
        logging.getLogger("chimera_memory").info("server exiting (pid=%s)", os.getpid())


if __name__ == "__main__":
    main()
