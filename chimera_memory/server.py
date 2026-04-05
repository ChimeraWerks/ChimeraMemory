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
            _state["indexer"] = Indexer(_get_db(), jsonl_dir, persona=persona)
        return _state["indexer"]

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

    return server


def main():
    """Entry point for running the MCP server."""
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(levelname)s | %(message)s")
    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
