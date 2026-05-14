import sqlite3
from pathlib import Path

from chimera_memory.memory import (
    index_file,
    init_memory_tables,
    memory_audit_query,
    memory_recall_trace_query,
    memory_search,
    record_memory_audit_event,
    record_memory_recall_trace,
)


def test_memory_search_records_recall_trace_and_audit_items(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    memory_file = tmp_path / "trace.md"
    memory_file.write_text(
        "---\ntype: procedural\nimportance: 8\nabout: trace testing\n---\nalpha trace marker\n",
        encoding="utf-8",
    )
    assert index_file(conn, "asa", "trace.md", memory_file)

    results = memory_search(conn, "alpha trace", persona="asa", limit=5)
    assert len(results) == 1
    assert results[0]["id"]

    traces = memory_recall_trace_query(conn, persona="asa", tool_name="memory_search", include_items=True)
    assert len(traces) == 1
    assert traces[0]["query_text"] == "alpha trace"
    assert traces[0]["requested_limit"] == 5
    assert traces[0]["returned_count"] == 1
    assert traces[0]["items"][0]["relative_path"] == "trace.md"

    events = memory_audit_query(conn, persona="asa", limit=10)
    event_types = {event["event_type"] for event in events}
    assert "recall_requested" in event_types
    assert "memory_returned" in event_types


def test_record_memory_recall_trace_handles_empty_results() -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    trace_id = record_memory_recall_trace(
        conn,
        tool_name="memory_recall",
        query_text="nothing here",
        persona="asa",
        requested_limit=3,
        results=[],
        request_payload={"concept": "nothing here"},
        response_policy={"ranking": "embedding_cosine"},
    )

    traces = memory_recall_trace_query(conn, include_items=True)
    assert traces[0]["trace_id"] == trace_id
    assert traces[0]["returned_count"] == 0
    assert traces[0]["items"] == []

    events = memory_audit_query(conn, event_type="recall_requested")
    assert len(events) == 1
    assert events[0]["trace_id"] == trace_id


def test_memory_audit_query_filters_event_type_and_persona() -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    first = record_memory_audit_event(
        conn,
        "memory_written",
        persona="asa",
        target_kind="memory_file",
        target_id="a.md",
        payload={"status": "pending"},
    )
    record_memory_audit_event(
        conn,
        "memory_rejected",
        persona="sarah",
        target_kind="memory_file",
        target_id="b.md",
    )

    events = memory_audit_query(conn, event_type="memory_written", persona="asa")
    assert len(events) == 1
    assert events[0]["event_id"] == first
    assert events[0]["payload"] == {"status": "pending"}
