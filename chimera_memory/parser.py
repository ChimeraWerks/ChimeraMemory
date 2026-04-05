"""Parse Claude Code JSONL session files into structured transcript entries."""

import json
import re
import logging
from pathlib import Path
from typing import Generator

log = logging.getLogger(__name__)

# Discord inbound message pattern
DISCORD_INBOUND_RE = re.compile(
    r'<channel\s+source="plugin:discord[^"]*"\s+'
    r'chat_id="(?P<chat_id>[^"]+)"\s+'
    r'message_id="(?P<message_id>[^"]+)"\s+'
    r'user="(?P<user>[^"]+)"\s+'
    r'user_id="(?P<user_id>[^"]+)"\s+'
    r'ts="(?P<ts>[^"]+)"[^>]*>\s*'
    r'(?P<content>.*?)\s*</channel>',
    re.DOTALL,
)

# Discord reply tool call
DISCORD_REPLY_TOOL = "mcp__plugin_discord_discord__reply"
DISCORD_REACT_TOOL = "mcp__plugin_discord_discord__react"
DISCORD_EDIT_TOOL = "mcp__plugin_discord_discord__edit_message"

# Claude Code XML artifacts to strip
CC_XML_TAGS = re.compile(
    r'<(?:command-name|command-message|command-args|local-command-stdout)>.*?'
    r'</(?:command-name|command-message|command-args|local-command-stdout)>',
    re.DOTALL,
)

# Entry types we skip entirely
SKIP_TYPES = {"file-history-snapshot", "custom-title", "agent-name", "permission-mode"}


def parse_jsonl_file(path: Path, start_offset: int = 0) -> Generator[dict, None, int]:
    """Parse a JSONL file from a byte offset. Yields structured entries.

    Returns the final byte position read (for tail-read tracking).
    """
    with open(path, "r", encoding="utf-8") as f:
        if start_offset > 0:
            f.seek(start_offset)

        partial = ""
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Partial line (still being written). Save for next read.
                partial = line
                continue

            entry_type = obj.get("type", "")
            if entry_type in SKIP_TYPES:
                continue
            if obj.get("isMeta"):
                continue

            session_id = obj.get("sessionId", "")
            timestamp = obj.get("timestamp", "")
            git_branch = obj.get("gitBranch")
            cwd = obj.get("cwd")

            if entry_type == "user":
                yield from _parse_user_entry(obj, session_id, timestamp)
            elif entry_type == "assistant":
                yield from _parse_assistant_entry(obj, session_id, timestamp)
            elif entry_type == "system":
                yield from _parse_system_entry(obj, session_id, timestamp)
            elif entry_type == "queue-operation":
                yield from _parse_queue_operation(obj, session_id, timestamp)
            elif entry_type == "attachment":
                yield _make_entry(
                    session_id=session_id,
                    entry_type="attachment",
                    timestamp=timestamp,
                    content=json.dumps(obj.get("attachment", {})),
                    source="cli",
                    metadata={"uuid": obj.get("uuid")},
                )

        # Return final position (subtract partial line length if any)
        final_pos = f.tell()
        if partial:
            final_pos -= len(partial.encode("utf-8"))
        return final_pos


def _parse_user_entry(obj: dict, session_id: str, timestamp: str):
    """Parse a user-type JSONL entry."""
    msg = obj.get("message", {})
    if not isinstance(msg, dict):
        return

    content = msg.get("content", "")

    # Check if this is a tool_result (not a real user message)
    if isinstance(content, list):
        if content and isinstance(content[0], dict) and content[0].get("type") == "tool_result":
            # Tool result. Index metadata only, not full content.
            for block in content:
                if block.get("type") == "tool_result":
                    result_content = block.get("content", "")
                    size = len(result_content) if isinstance(result_content, str) else 0
                    yield _make_entry(
                        session_id=session_id,
                        entry_type="tool_result",
                        timestamp=timestamp,
                        content=None,  # Don't index full tool output
                        source="tool",
                        tool_name=block.get("tool_use_id"),
                        metadata={
                            "uuid": obj.get("uuid"),
                            "content_size": size,
                        },
                    )
            return
        # List content that's not tool_result (rare for user)
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        content = "\n".join(text_parts)

    if not isinstance(content, str):
        return

    # Check for Discord inbound message
    discord_match = DISCORD_INBOUND_RE.search(content)
    if discord_match:
        yield _make_entry(
            session_id=session_id,
            entry_type="discord_inbound",
            timestamp=discord_match.group("ts") or timestamp,
            content=discord_match.group("content").strip(),
            source="discord",
            chat_id=discord_match.group("chat_id"),
            message_id=discord_match.group("message_id"),
            author=discord_match.group("user"),
            author_id=discord_match.group("user_id"),
            metadata={"uuid": obj.get("uuid")},
        )
        # Also check for non-discord content around the tag
        remaining = DISCORD_INBOUND_RE.sub("", content).strip()
        remaining = CC_XML_TAGS.sub("", remaining).strip()
        if remaining and len(remaining) > 10:
            yield _make_entry(
                session_id=session_id,
                entry_type="user_message",
                timestamp=timestamp,
                content=remaining,
                source="cli",
                metadata={"uuid": obj.get("uuid")},
            )
        return

    # Check for task-notification (subagent result)
    if content.strip().startswith("<task-notification>"):
        yield _make_entry(
            session_id=session_id,
            entry_type="system",
            timestamp=timestamp,
            content=None,
            source="system",
            metadata={"uuid": obj.get("uuid"), "subtype": "task-notification"},
        )
        return

    # Check for system-reminder
    if content.strip().startswith("<system-reminder>"):
        # Skip these, they're injected context
        return

    # Regular user message
    content = CC_XML_TAGS.sub("", content).strip()
    if content:
        yield _make_entry(
            session_id=session_id,
            entry_type="user_message",
            timestamp=timestamp,
            content=content,
            source="cli",
            author=obj.get("userType", "human"),
            metadata={"uuid": obj.get("uuid")},
        )


def _parse_queue_operation(obj: dict, session_id: str, timestamp: str):
    """Parse a queue-operation entry. Discord plugin delivers messages here."""
    content = obj.get("content", "")
    if not isinstance(content, str):
        return

    # Check for Discord inbound message
    discord_match = DISCORD_INBOUND_RE.search(content)
    if discord_match:
        yield _make_entry(
            session_id=session_id,
            entry_type="discord_inbound",
            timestamp=discord_match.group("ts") or timestamp,
            content=discord_match.group("content").strip(),
            source="discord",
            chat_id=discord_match.group("chat_id"),
            message_id=discord_match.group("message_id"),
            author=discord_match.group("user"),
            author_id=discord_match.group("user_id"),
            metadata={"operation": obj.get("operation")},
        )


def _parse_assistant_entry(obj: dict, session_id: str, timestamp: str):
    """Parse an assistant-type JSONL entry."""
    msg = obj.get("message", {})
    if not isinstance(msg, dict):
        return

    content = msg.get("content", "")
    uuid = obj.get("uuid")

    if isinstance(content, str):
        if content.strip():
            yield _make_entry(
                session_id=session_id,
                entry_type="assistant_message",
                timestamp=timestamp,
                content=content,
                source="cli",
                metadata={"uuid": uuid},
            )
        return

    if not isinstance(content, list):
        return

    # Process content blocks
    text_parts = []
    tool_calls = []
    has_thinking = False

    for block in content:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type")

        if block_type == "text":
            text = block.get("text", "").strip()
            if text:
                text_parts.append(text)

        elif block_type == "thinking":
            has_thinking = True
            # Don't index thinking content (private reasoning)

        elif block_type == "tool_use":
            tool_name = block.get("name", "")
            tool_input = block.get("input", {})
            tool_id = block.get("id", "")

            # Check for Discord outbound
            if tool_name == DISCORD_REPLY_TOOL:
                chat_id = tool_input.get("chat_id", "")
                text = tool_input.get("text", "")
                reply_to = tool_input.get("reply_to")
                yield _make_entry(
                    session_id=session_id,
                    entry_type="discord_outbound",
                    timestamp=timestamp,
                    content=text,
                    source="discord",
                    chat_id=chat_id,
                    author="assistant",
                    tool_name=tool_name,
                    metadata={
                        "uuid": uuid,
                        "tool_use_id": tool_id,
                        "reply_to": reply_to,
                        "files": tool_input.get("files"),
                    },
                )
            elif tool_name in (DISCORD_REACT_TOOL, DISCORD_EDIT_TOOL):
                yield _make_entry(
                    session_id=session_id,
                    entry_type="discord_outbound",
                    timestamp=timestamp,
                    content=json.dumps(tool_input),
                    source="discord",
                    chat_id=tool_input.get("chat_id", ""),
                    message_id=tool_input.get("message_id"),
                    author="assistant",
                    tool_name=tool_name,
                    metadata={"uuid": uuid, "tool_use_id": tool_id},
                )
            else:
                # Regular tool call. Index metadata, not full input.
                tool_calls.append(tool_name)
                yield _make_entry(
                    session_id=session_id,
                    entry_type="tool_call",
                    timestamp=timestamp,
                    content=None,  # Don't index full tool params
                    source="tool",
                    tool_name=tool_name,
                    metadata={
                        "uuid": uuid,
                        "tool_use_id": tool_id,
                        "input_keys": list(tool_input.keys()) if isinstance(tool_input, dict) else [],
                    },
                )

    # Yield the text content as assistant_message
    if text_parts:
        full_text = "\n".join(text_parts)
        yield _make_entry(
            session_id=session_id,
            entry_type="assistant_message",
            timestamp=timestamp,
            content=full_text,
            source="cli",
            metadata={
                "uuid": uuid,
                "has_thinking": has_thinking,
                "tool_calls": tool_calls if tool_calls else None,
            },
        )


def _parse_system_entry(obj: dict, session_id: str, timestamp: str):
    """Parse a system-type JSONL entry. Skip most, keep subtype for metadata."""
    # System entries are low value for search. Index metadata only.
    subtype = obj.get("subtype", "")
    yield _make_entry(
        session_id=session_id,
        entry_type="system",
        timestamp=timestamp,
        content=None,
        source="system",
        metadata={
            "uuid": obj.get("uuid"),
            "subtype": subtype,
            "level": obj.get("level"),
        },
    )


def _make_entry(**kwargs) -> dict:
    """Create a normalized transcript entry dict."""
    metadata = kwargs.get("metadata")
    if metadata:
        # Clean None values from metadata
        metadata = {k: v for k, v in metadata.items() if v is not None}
        kwargs["metadata"] = json.dumps(metadata) if metadata else None
    return kwargs


def extract_session_metadata(path: Path) -> dict:
    """Extract session-level metadata from a JSONL file without full parsing.

    Reads just enough to get session_id, git_branch, cwd, timestamps.
    """
    session_id = None
    git_branch = None
    cwd = None
    first_ts = None
    last_ts = None
    title = None
    exchange_count = 0

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            obj_type = obj.get("type", "")

            if obj_type == "custom-title":
                title = obj.get("customTitle")

            if "sessionId" in obj and not session_id:
                session_id = obj["sessionId"]
            if "gitBranch" in obj and not git_branch:
                git_branch = obj["gitBranch"]
            if "cwd" in obj and not cwd:
                cwd = obj["cwd"]

            ts = obj.get("timestamp")
            if ts:
                if not first_ts:
                    first_ts = ts
                last_ts = ts

            if obj_type in ("user", "assistant"):
                if not obj.get("isMeta"):
                    exchange_count += 1

    # Use filename UUID as fallback session_id
    if not session_id:
        session_id = path.stem

    return {
        "session_id": session_id,
        "title": title,
        "git_branch": git_branch,
        "cwd": cwd,
        "started_at": first_ts,
        "ended_at": last_ts,
        "exchange_count": exchange_count,
    }
