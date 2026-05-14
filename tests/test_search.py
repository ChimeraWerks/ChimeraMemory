"""Test search and recall functions."""

import tempfile
import shutil
from pathlib import Path
from chimera_memory.db import TranscriptDB
from chimera_memory.search import discord_recall, transcript_stats

tmpdir = None
db = None
passed = 0
failed = 0


def _check(name, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name}")
        failed += 1


def setup():
    global tmpdir, db
    tmpdir = tempfile.mkdtemp()
    db = TranscriptDB(Path(tmpdir) / "test.db")

    # Populate with test data
    entries = [
        # Discord inbound messages
        {"session_id": "s1", "entry_type": "discord_inbound", "timestamp": "2026-04-05T10:00:00Z",
         "content": "Hey Sarah, can you research umbrellas?", "source": "discord",
         "chat_id": "office-123", "message_id": "m1", "author": "chimerawerks", "author_id": "111"},
        {"session_id": "s1", "entry_type": "discord_inbound", "timestamp": "2026-04-05T10:05:00Z",
         "content": "Also check the weather forecast for Tokyo", "source": "discord",
         "chat_id": "office-123", "message_id": "m2", "author": "chimerawerks", "author_id": "111"},
        {"session_id": "s1", "entry_type": "discord_inbound", "timestamp": "2026-04-05T10:10:00Z",
         "content": "Random message in break room", "source": "discord",
         "chat_id": "breakroom-456", "message_id": "m3", "author": "asa", "author_id": "222"},

        # Discord outbound
        {"session_id": "s1", "entry_type": "discord_outbound", "timestamp": "2026-04-05T10:01:00Z",
         "content": "On it. Give me a sec to check umbrella options.", "source": "discord",
         "chat_id": "office-123", "message_id": "m4", "author": "assistant"},
        {"session_id": "s1", "entry_type": "discord_outbound", "timestamp": "2026-04-05T10:06:00Z",
         "content": "Tokyo forecast looks rainy for late April.", "source": "discord",
         "chat_id": "office-123", "message_id": "m5", "author": "assistant"},

        # User/assistant CLI messages
        {"session_id": "s1", "entry_type": "user_message", "timestamp": "2026-04-05T09:50:00Z",
         "content": "System startup message", "source": "cli"},
        {"session_id": "s1", "entry_type": "assistant_message", "timestamp": "2026-04-05T09:51:00Z",
         "content": "Session initialized successfully", "source": "cli"},

        # Tool calls
        {"session_id": "s1", "entry_type": "tool_call", "timestamp": "2026-04-05T10:02:00Z",
         "content": None, "source": "tool", "tool_name": "WebSearch"},
        {"session_id": "s1", "entry_type": "tool_result", "timestamp": "2026-04-05T10:02:01Z",
         "content": None, "source": "tool", "tool_name": "WebSearch"},

        # Second session
        {"session_id": "s2", "entry_type": "discord_inbound", "timestamp": "2026-04-06T10:00:00Z",
         "content": "Good morning, what happened yesterday?", "source": "discord",
         "chat_id": "office-123", "message_id": "m6", "author": "chimerawerks", "author_id": "111"},

        # Message with special characters for FTS testing
        {"session_id": "s1", "entry_type": "discord_inbound", "timestamp": "2026-04-05T11:00:00Z",
         "content": "What about the Knirps T.200 umbrella model?", "source": "discord",
         "chat_id": "office-123", "message_id": "m7", "author": "chimerawerks", "author_id": "111"},
    ]
    db.insert_entries(entries)


def run():
    setup()

    # === CHRONOLOGICAL QUERIES ===

    # 1. Basic recall (last N)
    r = discord_recall(db, limit=5)
    _check("Basic recall (last 5)", len(r) == 5)

    # 2. All messages
    r = discord_recall(db, limit=100)
    _check("All conversation messages", len(r) > 0 and all(
        x["entry_type"] in ("discord_inbound", "discord_outbound", "user_message", "assistant_message")
        for x in r
    ))
    _check("Tool calls excluded by default", not any(x["entry_type"] in ("tool_call", "tool_result") for x in r))

    # 3. Channel filter
    r = discord_recall(db, channel="office-123")
    office_count = len(r)
    _check("Channel filter (office)", office_count > 0 and all(
        x.get("chat_id") == "office-123" or x.get("chat_id") is None for x in r
    ))

    r = discord_recall(db, channel="breakroom-456")
    _check("Channel filter (breakroom)", len(r) == 1 and r[0]["author"] == "asa")

    r = discord_recall(db, channel="nonexistent-999")
    _check("Channel filter (nonexistent)", len(r) == 0)

    # 4. Direction filter
    r = discord_recall(db, direction="inbound")
    _check("Direction inbound", len(r) > 0 and all(
        x["entry_type"] in ("discord_inbound", "user_message") for x in r
    ))

    r = discord_recall(db, direction="outbound")
    _check("Direction outbound", len(r) > 0 and all(
        x["entry_type"] in ("discord_outbound", "assistant_message") for x in r
    ))

    # 5. Author filter
    r = discord_recall(db, author="chimerawerks")
    _check("Author filter", len(r) > 0 and all(x["author"] == "chimerawerks" for x in r))

    r = discord_recall(db, author="nobody")
    _check("Author filter (no match)", len(r) == 0)

    # 6. Time range filters
    r = discord_recall(db, after="2026-04-05T10:04:00Z")
    _check("After filter", len(r) > 0 and all(x["timestamp"] > "2026-04-05T10:04:00Z" for x in r))

    r = discord_recall(db, before="2026-04-05T10:02:00Z")
    _check("Before filter", len(r) > 0 and all(x["timestamp"] < "2026-04-05T10:02:00Z" for x in r))

    r = discord_recall(db, after="2026-04-05T10:00:00Z", before="2026-04-05T10:06:00Z")
    _check("After+Before range", len(r) > 0 and all(
        "2026-04-05T10:00:00Z" < x["timestamp"] < "2026-04-05T10:06:00Z" for x in r
    ))

    # 7. Combined filters
    r = discord_recall(db, channel="office-123", direction="inbound", author="chimerawerks", limit=3)
    _check("Combined filters", len(r) > 0 and len(r) <= 3 and all(
        x["author"] == "chimerawerks" for x in r
    ))

    # 8. Include tool calls
    r = discord_recall(db, include_tool_calls=True, limit=100)
    has_tool = any(x["entry_type"] == "tool_call" for x in r)
    _check("Include tool calls flag", has_tool)

    # 9. Chronological order
    r = discord_recall(db, limit=100)
    timestamps = [x["timestamp"] for x in r]
    _check("Chronological order", timestamps == sorted(timestamps))

    # 10. Message IDs preserved
    r = discord_recall(db, channel="office-123", limit=1)
    _check("Message IDs preserved", r[0].get("message_id") is not None)

    # === FTS SEARCH ===

    # 11. Basic search
    r = discord_recall(db, search="umbrella")
    _check("FTS search 'umbrella'", len(r) > 0 and any("umbrella" in x.get("content", "").lower() for x in r))

    # 12. Multi-word search
    r = discord_recall(db, search="weather Tokyo")
    _check("FTS search multi-word", len(r) > 0)

    # 13. Search + channel filter
    r = discord_recall(db, search="umbrella", channel="office-123")
    _check("FTS + channel filter", len(r) > 0)

    # 14. Search with no results
    r = discord_recall(db, search="xyznonexistent")
    _check("FTS no results", len(r) == 0)

    # 15. Search with FTS-unsafe characters
    r = discord_recall(db, search='test"OR"hack')
    _check("FTS injection safe (quotes)", True)  # Shouldn't crash

    r = discord_recall(db, search="test*")
    _check("FTS injection safe (wildcard)", True)

    r = discord_recall(db, search="NOT everything")
    _check("FTS injection safe (NOT keyword)", True)

    # 16. Search with special model name
    r = discord_recall(db, search="Knirps T.200")
    _check("FTS with dots/numbers", len(r) > 0)

    # 17. Empty search
    r = discord_recall(db, search="")
    _check("Empty search string", len(r) == 0)

    # === STATS ===

    # 18. Stats
    stats = transcript_stats(db)
    _check("Stats entry_count", stats["entry_count"] == 11)
    _check("Stats entry_types", "discord_inbound" in stats.get("entry_types", {}))
    _check("Stats sources", "discord" in stats.get("sources", {}))

    # === EDGE CASES ===

    # 19. Limit 0
    r = discord_recall(db, limit=0)
    _check("Limit 0", len(r) == 0)

    # 20. Limit 1
    r = discord_recall(db, limit=1)
    _check("Limit 1", len(r) == 1)

    # 21. Very large limit
    r = discord_recall(db, limit=999999)
    _check("Very large limit", len(r) <= 11)  # Can't return more than what exists

    # === EMPTY DB ===

    # 22. Empty database
    empty_db = TranscriptDB(Path(tmpdir) / "empty.db")
    r = discord_recall(empty_db, limit=10)
    _check("Empty DB recall", len(r) == 0)

    stats = transcript_stats(empty_db)
    _check("Empty DB stats", stats["entry_count"] == 0 and stats["session_count"] == 0)

    teardown()
    print(f"\nSearch tests: {passed}/{passed + failed}")


def teardown():
    if tmpdir:
        shutil.rmtree(tmpdir)


if __name__ == "__main__":
    run()
