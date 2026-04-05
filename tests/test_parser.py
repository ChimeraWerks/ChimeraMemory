"""Test parser edge cases."""

import json
import tempfile
import shutil
from pathlib import Path
from chimera_memory.parser import parse_jsonl_file, extract_session_metadata

tmpdir = None
passed = 0
failed = 0


def setup():
    global tmpdir
    tmpdir = tempfile.mkdtemp()


def teardown():
    if tmpdir:
        shutil.rmtree(tmpdir)


def write_jsonl(name, entries):
    path = Path(tmpdir) / name
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def parse(path):
    return [r for r in parse_jsonl_file(path) if not isinstance(r, int)]


def test(name, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name}")
        failed += 1


def run():
    setup()

    # 1. User message
    r = parse(write_jsonl("t1.jsonl", [
        {"type": "user", "message": {"content": "Hello world"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u1"}
    ]))
    test("User message string", len(r) == 1 and r[0]["entry_type"] == "user_message")

    # 2. Discord inbound via queue-operation
    r = parse(write_jsonl("t2.jsonl", [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1",
         "content": '<channel source="plugin:discord:discord" chat_id="123" message_id="456" user="testuser" user_id="789" ts="2026-04-05T10:00:00Z">\nHello from Discord\n</channel>'}
    ]))
    test("Discord inbound (queue-op)", len(r) == 1 and r[0]["entry_type"] == "discord_inbound" and r[0]["author"] == "testuser")

    # 3. Discord inbound via user string
    r = parse(write_jsonl("t3.jsonl", [
        {"type": "user", "message": {"content": '<channel source="plugin:discord:discord" chat_id="123" message_id="789" user="ceo" user_id="111" ts="2026-04-05T10:01:00Z">\nDirect entry\n</channel>'}, "timestamp": "2026-04-05T10:01:00Z", "sessionId": "test-1", "uuid": "u2"}
    ]))
    test("Discord inbound (user string)", len(r) == 1 and r[0]["entry_type"] == "discord_inbound" and r[0]["author"] == "ceo")

    # 4. Discord reply tool call
    r = parse(write_jsonl("t4.jsonl", [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me reply"},
            {"type": "tool_use", "id": "tool1", "name": "mcp__plugin_discord_discord__reply", "input": {"chat_id": "123", "text": "My reply"}}
        ]}, "timestamp": "2026-04-05T10:02:00Z", "sessionId": "test-1", "uuid": "a1"}
    ]))
    types = [x["entry_type"] for x in r]
    test("Discord outbound (reply tool)", "discord_outbound" in types and "assistant_message" in types)
    discord_out = [x for x in r if x["entry_type"] == "discord_outbound"][0]
    test("Discord outbound content", discord_out["content"] == "My reply" and discord_out["chat_id"] == "123")

    # 5. Tool result (metadata only)
    r = parse(write_jsonl("t5.jsonl", [
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool1", "content": "Huge output NOT indexed"}
        ]}, "timestamp": "2026-04-05T10:03:00Z", "sessionId": "test-1", "uuid": "u3"}
    ]))
    test("Tool result (no content)", len(r) == 1 and r[0]["content"] is None)

    # 6. isMeta (skip)
    r = parse(write_jsonl("t6.jsonl", [
        {"type": "user", "isMeta": True, "message": {"content": "meta"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u4"}
    ]))
    test("isMeta skipped", len(r) == 0)

    # 7. Skip types
    r = parse(write_jsonl("t7.jsonl", [
        {"type": "file-history-snapshot", "messageId": "1", "snapshot": {}},
        {"type": "custom-title", "customTitle": "Test", "sessionId": "test-1"},
        {"type": "agent-name", "agentName": "test", "sessionId": "test-1"},
    ]))
    test("Skip types", len(r) == 0)

    # 8. System-reminder (skip)
    r = parse(write_jsonl("t8.jsonl", [
        {"type": "user", "message": {"content": "<system-reminder>\nstuff\n</system-reminder>"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u5"}
    ]))
    test("System-reminder skipped", len(r) == 0)

    # 9. Task notification
    r = parse(write_jsonl("t9.jsonl", [
        {"type": "user", "message": {"content": "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u6"}
    ]))
    test("Task notification -> system", len(r) == 1 and r[0]["entry_type"] == "system" and r[0]["content"] is None)

    # 10. Thinking blocks not indexed
    r = parse(write_jsonl("t10.jsonl", [
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "secret reasoning"},
            {"type": "text", "text": "visible output"}
        ]}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "a2"}
    ]))
    texts = [x for x in r if x["entry_type"] == "assistant_message"]
    test("Thinking not indexed", len(texts) == 1 and "secret" not in texts[0]["content"])

    # 11. Empty file
    r = parse(write_jsonl("t11.jsonl", []))
    test("Empty file", len(r) == 0)

    # 12. Malformed JSON recovery
    path = Path(tmpdir) / "t12.jsonl"
    with open(path, "w") as f:
        f.write('{"type": "user", "message": {"content": "good"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u7"}\n')
        f.write("this is not json\n")
        f.write('{"type": "user", "message": {"content": "also good"}, "timestamp": "2026-04-05T10:00:01Z", "sessionId": "test-1", "uuid": "u8"}\n')
    r = parse(path)
    test("Malformed JSON recovery", len(r) == 2)

    # 13. Unicode content
    r = parse(write_jsonl("t13.jsonl", [
        {"type": "user", "message": {"content": "Hello 🎉 émojis"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u9"}
    ]))
    test("Unicode content", len(r) == 1 and "🎉" in r[0]["content"])

    # 14. Very long content (100k chars)
    r = parse(write_jsonl("t14.jsonl", [
        {"type": "user", "message": {"content": "x" * 100000}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u10"}
    ]))
    test("Very long content", len(r) == 1 and len(r[0]["content"]) == 100000)

    # 15. Discord with attachment attributes
    r = parse(write_jsonl("t15.jsonl", [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1",
         "content": '<channel source="plugin:discord:discord" chat_id="123" message_id="456" user="testuser" user_id="789" ts="2026-04-05T10:00:00Z" attachment_count="1" attachments="image.png (image/webp, 39KB)">\nCheck this image\n</channel>'}
    ]))
    test("Discord with attachments", len(r) == 1 and r[0]["content"] == "Check this image")

    # 16. Session metadata extraction
    path = write_jsonl("t16.jsonl", [
        {"type": "custom-title", "customTitle": "My Session", "sessionId": "sess-1"},
        {"type": "user", "message": {"content": "first"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "sess-1", "gitBranch": "main", "cwd": "/home/user", "uuid": "u1"},
        {"type": "assistant", "message": {"content": "reply"}, "timestamp": "2026-04-05T10:05:00Z", "sessionId": "sess-1", "uuid": "a1"},
        {"type": "user", "message": {"content": "end"}, "timestamp": "2026-04-05T11:00:00Z", "sessionId": "sess-1", "uuid": "u2"},
    ])
    meta = extract_session_metadata(path)
    test("Session metadata", meta["session_id"] == "sess-1" and meta["title"] == "My Session" and meta["git_branch"] == "main" and meta["exchange_count"] == 3)

    # 17. Regular tool call
    r = parse(write_jsonl("t17.jsonl", [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tool2", "name": "Read", "input": {"file_path": "/tmp/test.py"}}
        ]}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "a3"}
    ]))
    test("Regular tool call", len(r) == 1 and r[0]["entry_type"] == "tool_call" and r[0]["tool_name"] == "Read")

    # 18. Discord react tool
    r = parse(write_jsonl("t18.jsonl", [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tool3", "name": "mcp__plugin_discord_discord__react", "input": {"chat_id": "123", "message_id": "456", "emoji": "👍"}}
        ]}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "a4"}
    ]))
    test("Discord react tool", len(r) == 1 and r[0]["entry_type"] == "discord_outbound" and r[0]["tool_name"] == "mcp__plugin_discord_discord__react")

    # 19. Multiple Discord messages in one queue-operation (shouldn't happen but test)
    r = parse(write_jsonl("t19.jsonl", [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1",
         "content": "no discord tag here, just plain text"}
    ]))
    test("Queue-op without discord tag", len(r) == 0)

    # 20. Offset/tail-read test
    path = write_jsonl("t20.jsonl", [
        {"type": "user", "message": {"content": "first line"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u20"},
        {"type": "user", "message": {"content": "second line"}, "timestamp": "2026-04-05T10:00:01Z", "sessionId": "test-1", "uuid": "u21"},
    ])
    # Read from offset past the first line
    with open(path, "r") as f:
        first_line = f.readline()
        offset = f.tell()
    r = parse(Path(path))
    test("Full parse gets both", len(r) == 2)
    r2 = [x for x in parse_jsonl_file(path, start_offset=offset) if not isinstance(x, int)]
    test("Offset parse gets second only", len(r2) == 1 and r2[0]["content"] == "second line")

    teardown()
    print(f"\nParser tests: {passed}/{passed + failed}")


if __name__ == "__main__":
    run()
