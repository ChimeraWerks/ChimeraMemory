"""Test indexer: import log, backfill, tail-read, file watching."""

import json
import tempfile
import shutil
import time
from pathlib import Path
from chimera_memory.db import TranscriptDB
from chimera_memory.indexer import Indexer, get_file_hash
from chimera_memory.search import discord_recall

tmpdir = None
passed = 0
failed = 0


def test(name, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name}")
        failed += 1


def write_jsonl(dirpath, name, entries):
    path = Path(dirpath) / name
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def run():
    global tmpdir
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test.db"
    jsonl_dir = Path(tmpdir) / "sessions"
    jsonl_dir.mkdir()

    # Create test JSONL files
    write_jsonl(jsonl_dir, "session-1.jsonl", [
        {"type": "user", "message": {"content": "Hello from session 1"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "session-1", "uuid": "u1", "gitBranch": "main", "cwd": "/test"},
        {"type": "assistant", "message": {"content": "Hi there"}, "timestamp": "2026-04-05T10:00:01Z", "sessionId": "session-1", "uuid": "a1"},
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-04-05T10:01:00Z", "sessionId": "session-1",
         "content": '<channel source="plugin:discord:discord" chat_id="123" message_id="m1" user="ceo" user_id="111" ts="2026-04-05T10:01:00Z">\nHey Sarah\n</channel>'},
    ])

    write_jsonl(jsonl_dir, "session-2.jsonl", [
        {"type": "user", "message": {"content": "Session 2 start"}, "timestamp": "2026-04-06T10:00:00Z", "sessionId": "session-2", "uuid": "u2", "gitBranch": "dev", "cwd": "/test"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Replying in session 2"},
            {"type": "tool_use", "id": "t1", "name": "mcp__plugin_discord_discord__reply", "input": {"chat_id": "123", "text": "Discord reply from session 2"}}
        ]}, "timestamp": "2026-04-06T10:00:01Z", "sessionId": "session-2", "uuid": "a2"},
    ])

    # === BACKFILL ===

    print("=== BACKFILL TESTS ===")

    db = TranscriptDB(db_path)
    indexer = Indexer(db, jsonl_dir, persona="sarah")

    progress_log = []
    def progress(current, total):
        progress_log.append((current, total))

    indexer.backfill(progress_callback=progress)

    test("Backfill progress callback", len(progress_log) > 0 and progress_log[-1][0] == progress_log[-1][1])

    stats = db.stats()
    test("Backfill entry count", stats["entry_count"] > 0)
    test("Backfill session count", stats["session_count"] == 2)

    # Check sessions table
    with db.connection() as conn:
        s1 = conn.execute("SELECT * FROM sessions WHERE session_id = ?", ("session-1",)).fetchone()
        s2 = conn.execute("SELECT * FROM sessions WHERE session_id = ?", ("session-2",)).fetchone()
    test("Session 1 metadata", s1 and s1["git_branch"] == "main" and s1["persona"] == "sarah")
    test("Session 2 metadata", s2 and s2["git_branch"] == "dev")

    # Check discord messages indexed
    r = discord_recall(db, channel="123")
    discord_msgs = [x for x in r if x["entry_type"] in ("discord_inbound", "discord_outbound")]
    test("Discord messages indexed", len(discord_msgs) >= 2)

    # === IMPORT LOG ===

    print("\n=== IMPORT LOG TESTS ===")

    with db.connection() as conn:
        log_entries = conn.execute("SELECT * FROM import_log").fetchall()
    test("Import log has entries", len(log_entries) == 2)

    # Re-run backfill — should skip unchanged files
    initial_count = stats["entry_count"]
    indexer.backfill()
    stats2 = db.stats()
    test("Re-backfill skips unchanged (same count)", stats2["entry_count"] == initial_count)

    # === FILE HASH ===

    print("\n=== FILE HASH TESTS ===")

    path1 = jsonl_dir / "session-1.jsonl"
    hash1 = get_file_hash(path1)
    hash2 = get_file_hash(path1)
    test("Same file same hash", hash1 == hash2)

    hash3 = get_file_hash(jsonl_dir / "session-2.jsonl")
    test("Different files different hash", hash1 != hash3)

    # Modify file and check hash changes
    with open(path1, "a") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "appended"}, "timestamp": "2026-04-05T12:00:00Z", "sessionId": "session-1", "uuid": "u99"}) + "\n")
    hash4 = get_file_hash(path1)
    test("Modified file different hash", hash1 != hash4)

    # Re-backfill should pick up the changed file
    indexer.backfill()
    stats3 = db.stats()
    test("Modified file re-indexed", stats3["entry_count"] > initial_count)

    # === TAIL-READ ===

    print("\n=== TAIL-READ TESTS ===")

    # Create a new file to tail
    new_file = write_jsonl(jsonl_dir, "session-3.jsonl", [
        {"type": "user", "message": {"content": "Tail test line 1"}, "timestamp": "2026-04-07T10:00:00Z", "sessionId": "session-3", "uuid": "u30"},
    ])

    # Index it first
    indexer.index_file(new_file)

    # Append new content
    with open(new_file, "a") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "Tail test line 2 (appended)"}, "timestamp": "2026-04-07T10:01:00Z", "sessionId": "session-3", "uuid": "u31"}) + "\n")

    # Tail-read should pick up only the new content
    indexer.tail_file(new_file)
    r = discord_recall(db, search="Tail test", limit=10)
    test("Tail-read picks up appended content", len(r) >= 2)

    # Tail-read with no new content should be a no-op
    before_count = db.stats()["entry_count"]
    indexer.tail_file(new_file)
    after_count = db.stats()["entry_count"]
    test("Tail-read no-op when no new content", before_count == after_count)

    # === CONCURRENT / EDGE CASES ===

    print("\n=== EDGE CASES ===")

    # Empty directory
    empty_dir = Path(tmpdir) / "empty_sessions"
    empty_dir.mkdir()
    empty_db = TranscriptDB(Path(tmpdir) / "empty.db")
    empty_indexer = Indexer(empty_db, empty_dir)
    empty_indexer.backfill()  # Should not crash
    test("Empty directory backfill", empty_db.stats()["entry_count"] == 0)

    # File with only skip-type entries
    write_jsonl(jsonl_dir, "session-skiponly.jsonl", [
        {"type": "file-history-snapshot", "messageId": "1", "snapshot": {}},
        {"type": "custom-title", "customTitle": "Test", "sessionId": "skip-1"},
    ])
    indexer.index_file(jsonl_dir / "session-skiponly.jsonl")
    test("Skip-only file (no crash)", True)

    # File with single partial line (simulating active write)
    partial_path = jsonl_dir / "session-partial.jsonl"
    with open(partial_path, "w") as f:
        f.write('{"type": "user", "message": {"content": "complete"}, "timestamp": "2026-04-08T10:00:00Z", "sessionId": "partial-1", "uuid": "up1"}\n')
        f.write('{"type": "user", "message": {"cont')  # Partial line
    indexer.index_file(partial_path)
    # Should index the complete line, ignore the partial
    r = discord_recall(db, search="complete", limit=5)
    test("Partial line handling", len(r) >= 1)

    # Non-JSONL files in directory (should be ignored)
    (jsonl_dir / "readme.txt").write_text("not a jsonl file")
    (jsonl_dir / "data.json").write_text('{"not": "jsonl"}')
    indexer.backfill()  # Should not crash or try to parse non-.jsonl files
    test("Non-JSONL files ignored", True)

    # === PERSONA TAGGING ===

    print("\n=== PERSONA TAGGING ===")

    with db.connection() as conn:
        sarah_entries = conn.execute("SELECT COUNT(*) FROM transcript WHERE persona = 'sarah'").fetchone()[0]
    test("Persona tagged on entries", sarah_entries > 0)

    # Different persona
    db2 = TranscriptDB(Path(tmpdir) / "asa.db")
    idx2 = Indexer(db2, jsonl_dir, persona="asa")
    idx2.backfill()
    with db2.connection() as conn:
        asa_entries = conn.execute("SELECT COUNT(*) FROM transcript WHERE persona = 'asa'").fetchone()[0]
    test("Different persona tagged", asa_entries > 0)

    shutil.rmtree(tmpdir)
    print(f"\nIndexer tests: {passed}/{passed + failed}")


if __name__ == "__main__":
    run()
