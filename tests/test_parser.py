"""Test parser edge cases."""

import json
import tempfile
import shutil
from pathlib import Path
from chimera_memory.parser import parse_jsonl_file, extract_session_metadata, get_parser

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


def _check(name, condition):
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
    _check("User message string", len(r) == 1 and r[0]["entry_type"] == "user_message")

    # 2. Discord inbound via queue-operation
    r = parse(write_jsonl("t2.jsonl", [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1",
         "content": '<channel source="plugin:discord:discord" chat_id="123" message_id="456" user="testuser" user_id="789" ts="2026-04-05T10:00:00Z">\nHello from Discord\n</channel>'}
    ]))
    _check("Discord inbound (queue-op)", len(r) == 1 and r[0]["entry_type"] == "discord_inbound" and r[0]["author"] == "testuser")

    # 3. Discord inbound via user string
    r = parse(write_jsonl("t3.jsonl", [
        {"type": "user", "message": {"content": '<channel source="plugin:discord:discord" chat_id="123" message_id="789" user="ceo" user_id="111" ts="2026-04-05T10:01:00Z">\nDirect entry\n</channel>'}, "timestamp": "2026-04-05T10:01:00Z", "sessionId": "test-1", "uuid": "u2"}
    ]))
    _check("Discord inbound (user string)", len(r) == 1 and r[0]["entry_type"] == "discord_inbound" and r[0]["author"] == "ceo")

    # 4. Discord reply tool call
    r = parse(write_jsonl("t4.jsonl", [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me reply"},
            {"type": "tool_use", "id": "tool1", "name": "mcp__plugin_discord_discord__reply", "input": {"chat_id": "123", "text": "My reply"}}
        ]}, "timestamp": "2026-04-05T10:02:00Z", "sessionId": "test-1", "uuid": "a1"}
    ]))
    types = [x["entry_type"] for x in r]
    _check("Discord outbound (reply tool)", "discord_outbound" in types and "assistant_message" in types)
    discord_out = [x for x in r if x["entry_type"] == "discord_outbound"][0]
    _check("Discord outbound content", discord_out["content"] == "My reply" and discord_out["chat_id"] == "123")

    # 5. Tool result (metadata only)
    r = parse(write_jsonl("t5.jsonl", [
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool1", "content": "Huge output NOT indexed"}
        ]}, "timestamp": "2026-04-05T10:03:00Z", "sessionId": "test-1", "uuid": "u3"}
    ]))
    _check("Tool result (no content)", len(r) == 1 and r[0]["content"] is None)

    # 6. isMeta (skip)
    r = parse(write_jsonl("t6.jsonl", [
        {"type": "user", "isMeta": True, "message": {"content": "meta"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u4"}
    ]))
    _check("isMeta skipped", len(r) == 0)

    # 7. Skip types
    r = parse(write_jsonl("t7.jsonl", [
        {"type": "file-history-snapshot", "messageId": "1", "snapshot": {}},
        {"type": "custom-title", "customTitle": "Test", "sessionId": "test-1"},
        {"type": "agent-name", "agentName": "test", "sessionId": "test-1"},
    ]))
    _check("Skip types", len(r) == 0)

    # 8. System-reminder (skip)
    r = parse(write_jsonl("t8.jsonl", [
        {"type": "user", "message": {"content": "<system-reminder>\nstuff\n</system-reminder>"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u5"}
    ]))
    _check("System-reminder skipped", len(r) == 0)

    # 9. Task notification
    r = parse(write_jsonl("t9.jsonl", [
        {"type": "user", "message": {"content": "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u6"}
    ]))
    _check("Task notification -> system", len(r) == 1 and r[0]["entry_type"] == "system" and r[0]["content"] is None)

    # 10. Thinking blocks not indexed
    r = parse(write_jsonl("t10.jsonl", [
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "secret reasoning"},
            {"type": "text", "text": "visible output"}
        ]}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "a2"}
    ]))
    texts = [x for x in r if x["entry_type"] == "assistant_message"]
    _check("Thinking not indexed", len(texts) == 1 and "secret" not in texts[0]["content"])

    # 11. Empty file
    r = parse(write_jsonl("t11.jsonl", []))
    _check("Empty file", len(r) == 0)

    # 12. Malformed JSON recovery
    path = Path(tmpdir) / "t12.jsonl"
    with open(path, "w") as f:
        f.write('{"type": "user", "message": {"content": "good"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u7"}\n')
        f.write("this is not json\n")
        f.write('{"type": "user", "message": {"content": "also good"}, "timestamp": "2026-04-05T10:00:01Z", "sessionId": "test-1", "uuid": "u8"}\n')
    r = parse(path)
    _check("Malformed JSON recovery", len(r) == 2)

    # 13. Unicode content
    r = parse(write_jsonl("t13.jsonl", [
        {"type": "user", "message": {"content": "Hello 🎉 émojis"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u9"}
    ]))
    _check("Unicode content", len(r) == 1 and "🎉" in r[0]["content"])

    # 14. Very long content (100k chars)
    r = parse(write_jsonl("t14.jsonl", [
        {"type": "user", "message": {"content": "x" * 100000}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "u10"}
    ]))
    _check("Very long content", len(r) == 1 and len(r[0]["content"]) == 100000)

    # 15. Discord with attachment attributes
    r = parse(write_jsonl("t15.jsonl", [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1",
         "content": '<channel source="plugin:discord:discord" chat_id="123" message_id="456" user="testuser" user_id="789" ts="2026-04-05T10:00:00Z" attachment_count="1" attachments="image.png (image/webp, 39KB)">\nCheck this image\n</channel>'}
    ]))
    _check("Discord with attachments", len(r) == 1 and r[0]["content"] == "Check this image")

    # 16. Session metadata extraction
    path = write_jsonl("t16.jsonl", [
        {"type": "custom-title", "customTitle": "My Session", "sessionId": "sess-1"},
        {"type": "user", "message": {"content": "first"}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "sess-1", "gitBranch": "main", "cwd": "/home/user", "uuid": "u1"},
        {"type": "assistant", "message": {"content": "reply"}, "timestamp": "2026-04-05T10:05:00Z", "sessionId": "sess-1", "uuid": "a1"},
        {"type": "user", "message": {"content": "end"}, "timestamp": "2026-04-05T11:00:00Z", "sessionId": "sess-1", "uuid": "u2"},
    ])
    meta = extract_session_metadata(path)
    _check("Session metadata", meta["session_id"] == "sess-1" and meta["title"] == "My Session" and meta["git_branch"] == "main" and meta["exchange_count"] == 3)

    # 17. Regular tool call
    r = parse(write_jsonl("t17.jsonl", [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tool2", "name": "Read", "input": {"file_path": "/tmp/test.py"}}
        ]}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "a3"}
    ]))
    _check("Regular tool call", len(r) == 1 and r[0]["entry_type"] == "tool_call" and r[0]["tool_name"] == "Read")

    # 18. Discord react tool
    r = parse(write_jsonl("t18.jsonl", [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tool3", "name": "mcp__plugin_discord_discord__react", "input": {"chat_id": "123", "message_id": "456", "emoji": "👍"}}
        ]}, "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1", "uuid": "a4"}
    ]))
    _check("Discord react tool", len(r) == 1 and r[0]["entry_type"] == "discord_outbound" and r[0]["tool_name"] == "mcp__plugin_discord_discord__react")

    # 19. Multiple Discord messages in one queue-operation (shouldn't happen but test)
    r = parse(write_jsonl("t19.jsonl", [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-04-05T10:00:00Z", "sessionId": "test-1",
         "content": "no discord tag here, just plain text"}
    ]))
    _check("Queue-op without discord tag", len(r) == 0)

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
    _check("Full parse gets both", len(r) == 2)
    r2 = [x for x in parse_jsonl_file(path, start_offset=offset) if not isinstance(x, int)]
    _check("Offset parse gets second only", len(r2) == 1 and r2[0]["content"] == "second line")

    # 21. Codex parser: Discord inbound, assistant message, tool metadata
    codex_parser = get_parser("codex")
    codex_path = write_jsonl("codex.jsonl", [
        {"timestamp": "2026-05-02T10:00:00Z", "type": "session_meta", "payload": {
            "id": "codex-session-1",
            "timestamp": "2026-05-02T10:00:00Z",
            "cwd": "C:/repo/personas/developer/asa",
            "git": {"branch": "main"},
        }},
        {"timestamp": "2026-05-02T10:00:01Z", "type": "event_msg", "payload": {
            "type": "thread_name_updated",
            "thread_name": "Asa - Day 1",
        }},
        {"timestamp": "2026-05-02T10:00:02Z", "type": "event_msg", "payload": {
            "type": "user_message",
            "message": "[Discord context]\nchat_id=123\nroute_chat_id=123\nchannel_name=asa-developer\nmessage_id=456\nauthor_id=111\nauthor_name=Charles - CEO\ntimestamp=2026-05-02T10:00:02+00:00\n\nuser_id=111: Check Codex parser",
            "images": [],
            "local_images": [],
            "text_elements": [],
        }},
        {"timestamp": "2026-05-02T10:00:03Z", "type": "event_msg", "payload": {
            "type": "agent_message",
            "phase": "final",
            "message": "Parser wired.",
        }},
        {"timestamp": "2026-05-02T10:00:04Z", "type": "response_item", "payload": {
            "type": "function_call",
            "call_id": "call-1",
            "name": "shell_command",
            "arguments": "{\"command\":\"echo ok\"}",
        }},
        {"timestamp": "2026-05-02T10:00:05Z", "type": "response_item", "payload": {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "ok",
        }},
    ])
    r = [x for x in codex_parser.parse_file(codex_path) if not isinstance(x, int)]
    types = [x["entry_type"] for x in r]
    _check("Codex parser selects recursively", codex_parser.recursive is True)
    _check("Codex Discord inbound", "discord_inbound" in types and [x for x in r if x["entry_type"] == "discord_inbound"][0]["chat_id"] == "123")
    _check("Codex assistant message", "assistant_message" in types and [x for x in r if x["entry_type"] == "assistant_message"][0]["content"] == "Parser wired.")
    _check("Codex tool metadata", "tool_call" in types and "tool_result" in types)
    meta = codex_parser.extract_session_metadata(codex_path)
    _check("Codex session metadata", meta["session_id"] == "codex-session-1" and meta["title"] == "Asa - Day 1" and meta["git_branch"] == "main")

    teardown()
    print(f"\nParser tests: {passed}/{passed + failed}")


if __name__ == "__main__":
    run()
