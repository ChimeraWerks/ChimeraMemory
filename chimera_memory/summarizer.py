"""Deterministic session summarization. Zero LLM calls."""

import re
import json
import logging
from .db import TranscriptDB

log = logging.getLogger(__name__)

# Greetings to skip when finding topic
GREETING_RE = re.compile(
    r"^(hi|hello|hey|sup|yo|good\s*morning|gm|good\s*evening|"
    r"what'?s\s*up|howdy|greetings|morning|evening|afternoon)[\s!.,?]*$",
    re.IGNORECASE,
)

# Slash commands to skip
SLASH_CMD_RE = re.compile(r"^/\w+")

# Completion language in assistant's last message
COMPLETION_RE = re.compile(
    r"done|pushed|merged|all tests pass|completed|finished|shipped|deployed|"
    r"PR\s*#?\d+|committed|changes?\s*live|ready\s*for\s*review|implemented|"
    r"fixed|resolved|created|built|set\s*up",
    re.IGNORECASE,
)

# User confirmation patterns (short messages)
CONFIRM_RE = re.compile(
    r"^(y(a|ep|es)?|thanks?|thank\s*you|(looks?\s*)?good|nice|perfect|"
    r"great|ok\.?|lgtm|k|cool|awesome|sweet|dope|bet|word|tight)[\s!.]*$",
    re.IGNORECASE,
)

# User action patterns (indicates more work coming)
ACTION_RE = re.compile(
    r"^(now|next|also|can\s*you|let'?s|please|I\s*want|I\s*need|"
    r"what\s*about|how\s*about|do|make|add|fix|change|update|show|check)",
    re.IGNORECASE,
)


def summarize_session(db: TranscriptDB, session_id: str) -> dict:
    """Generate a deterministic summary for a session.

    Returns a dict with: topic, disposition, exchange_count,
    tool_counts, first_message_at, last_message_at, summary_text.
    """
    with db.connection() as conn:
        # Get all conversation entries for this session
        entries = conn.execute(
            """SELECT entry_type, timestamp, content, author, tool_name
               FROM transcript
               WHERE session_id = ?
               ORDER BY timestamp ASC""",
            (session_id,),
        ).fetchall()

        # Get session metadata
        session = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    if not entries:
        return {"topic": "Empty session", "disposition": "INTERRUPTED", "exchange_count": 0}

    # Separate by type
    user_msgs = [e for e in entries if e["entry_type"] in ("user_message", "discord_inbound")]
    assistant_msgs = [e for e in entries if e["entry_type"] in ("assistant_message", "discord_outbound")]
    tool_calls = [e for e in entries if e["entry_type"] == "tool_call"]

    # Topic extraction
    topic = _extract_topic(session, user_msgs)

    # Disposition detection
    disposition = _detect_disposition(user_msgs, assistant_msgs)

    # Tool usage breakdown
    tool_counts = {}
    for tc in tool_calls:
        name = tc["tool_name"] or "unknown"
        tool_counts[name] = tool_counts.get(name, 0) + 1

    # Exchange count (user + assistant turns)
    exchange_count = len(user_msgs) + len(assistant_msgs)

    # Time range
    first_ts = entries[0]["timestamp"] if entries else None
    last_ts = entries[-1]["timestamp"] if entries else None

    # Build compact summary text
    summary_text = _build_summary_text(
        topic, disposition, exchange_count, tool_counts,
        first_ts, last_ts, user_msgs, assistant_msgs,
    )

    return {
        "topic": topic,
        "disposition": disposition,
        "exchange_count": exchange_count,
        "tool_counts": tool_counts,
        "first_message_at": first_ts,
        "last_message_at": last_ts,
        "summary_text": summary_text,
    }


def _extract_topic(session, user_msgs: list) -> str:
    """Extract session topic. Priority: custom title > first substantive message."""
    # Check custom title first
    if session and session["title"]:
        return session["title"]

    # Scan first 5 user messages for something substantive
    for msg in user_msgs[:5]:
        content = (msg["content"] or "").strip()
        if not content:
            continue
        if len(content) < 20 and GREETING_RE.match(content):
            continue
        if SLASH_CMD_RE.match(content):
            continue
        if content.startswith("<system-reminder>"):
            continue
        # Found a substantive message
        return content[:120].strip()

    return "Untitled session"


def _detect_disposition(user_msgs: list, assistant_msgs: list) -> str:
    """Detect session disposition from final exchange patterns."""
    if not user_msgs and not assistant_msgs:
        return "INTERRUPTED"

    last_assistant = None
    last_user = None

    if assistant_msgs:
        last_assistant = (assistant_msgs[-1]["content"] or "").strip()
    if user_msgs:
        last_user = (user_msgs[-1]["content"] or "").strip()

    # Check for completion: assistant used completion language
    if last_assistant and COMPLETION_RE.search(last_assistant):
        # AND user confirmed (or user's last msg is short/absent)
        if not last_user or len(last_user) < 30 or CONFIRM_RE.match(last_user):
            return "COMPLETED"

    # Check for user confirmation without assistant completion language
    if last_user and CONFIRM_RE.match(last_user):
        return "COMPLETED"

    # Check for in-progress: user's last message indicates more work
    if last_user and ACTION_RE.match(last_user):
        return "IN_PROGRESS"

    # Default
    return "INTERRUPTED"


def _build_summary_text(
    topic, disposition, exchange_count, tool_counts,
    first_ts, last_ts, user_msgs, assistant_msgs,
) -> str:
    """Build a compact markdown summary of the session."""
    lines = []
    lines.append(f"**Topic:** {topic}")
    lines.append(f"**Status:** {disposition}")
    lines.append(f"**Exchanges:** {exchange_count}")

    if first_ts and last_ts:
        lines.append(f"**Time:** {first_ts[:16]} to {last_ts[:16]}")

    if tool_counts:
        top_tools = sorted(tool_counts.items(), key=lambda x: -x[1])[:5]
        tool_str = ", ".join(f"{name}: {count}" for name, count in top_tools)
        lines.append(f"**Tools:** {tool_str}")

    return "\n".join(lines)


def summarize_all_sessions(db: TranscriptDB):
    """Generate summaries for all sessions that don't have one yet."""
    with db.connection() as conn:
        sessions = conn.execute(
            "SELECT session_id FROM sessions WHERE disposition IS NULL"
        ).fetchall()

    count = 0
    for row in sessions:
        sid = row["session_id"]
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
        count += 1

    if count:
        log.info("Generated summaries for %d sessions", count)
    return count


def truncate_mid(text: str, front: int = 300, back: int = 600) -> str:
    """Truncate text keeping front and back (back is larger because
    conclusions and next steps are at the end)."""
    if len(text) <= front + back + 20:
        return text
    return text[:front] + "\n[... truncated ...]\n" + text[-back:]
