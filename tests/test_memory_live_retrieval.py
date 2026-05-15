import sqlite3
from pathlib import Path

from chimera_memory.memory import (
    index_file,
    init_memory_tables,
    memory_audit_query,
    memory_live_retrieval_check,
    memory_recall_trace_query,
)
from chimera_memory.memory_live_retrieval import build_live_retrieval_plan


def _write_memory(path: Path, frontmatter: list[str], body: str) -> None:
    path.write_text(
        "\n".join(["---", *frontmatter, "---", body]),
        encoding="utf-8",
    )


def test_live_retrieval_plan_detects_topic_shift() -> None:
    plan = build_live_retrieval_plan(
        previous_context="We are closing workboard triage and Discord gateway cleanup.",
        current_context="Now we are debugging memory provider smoke and sidecar credentials.",
    )

    assert plan["should_retrieve"] is True
    assert plan["shift_score"] >= plan["shift_threshold"]
    assert "memory" in plan["query_terms"]
    assert "sidecar" in plan["query_terms"]


def test_live_retrieval_skips_without_shift_and_audits() -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    result = memory_live_retrieval_check(
        conn,
        previous_context="memory provider smoke sidecar credentials",
        current_context="memory provider smoke sidecar credentials",
        persona="asa",
    )

    assert result["ok"] is True
    assert result["retrieved"] is False
    assert result["reason"] == "no_topic_shift"
    assert memory_recall_trace_query(conn, tool_name="memory_live_retrieval") == []
    events = memory_audit_query(conn, event_type="memory_live_retrieval_skipped", persona="asa")
    assert len(events) == 1


def test_live_retrieval_returns_suggestions_and_records_trace(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    target = tmp_path / "provider-smoke.md"
    _write_memory(
        target,
        ["type: procedural", "importance: 8", "about: provider smoke harness"],
        "Provider smoke harness verifies sidecar credentials and memory enhancement rails.",
    )
    restricted = tmp_path / "restricted.md"
    _write_memory(
        restricted,
        [
            "type: semantic",
            "importance: 9",
            "sensitivity_tier: restricted",
            "about: restricted provider smoke",
        ],
        "Restricted provider smoke note should not appear in default live retrieval.",
    )
    assert index_file(conn, "asa", "memory/provider-smoke.md", target)
    assert index_file(conn, "asa", "memory/restricted.md", restricted)

    result = memory_live_retrieval_check(
        conn,
        current_context="Need provider smoke sidecar credentials verification now.",
        previous_context="Earlier topic was workboard cleanup.",
        persona="asa",
        force=True,
        limit=5,
    )

    assert result["ok"] is True
    assert result["retrieved"] is True
    assert len(result["results"]) == 1
    assert result["results"][0]["relative_path"] == "memory/provider-smoke.md"
    assert result["trace_id"]

    traces = memory_recall_trace_query(conn, tool_name="memory_live_retrieval", include_items=True)
    assert len(traces) == 1
    assert traces[0]["trace_id"] == result["trace_id"]
    assert traces[0]["items"][0]["relative_path"] == "memory/provider-smoke.md"

    events = memory_audit_query(conn, event_type="memory_live_retrieval_suggested", persona="asa")
    assert len(events) == 1
    assert events[0]["trace_id"] == result["trace_id"]


def test_live_retrieval_miss_is_traced_and_silent() -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    result = memory_live_retrieval_check(
        conn,
        current_context="quantum zebra umbrella impossible context",
        persona="asa",
        force=True,
    )

    assert result["ok"] is True
    assert result["retrieved"] is True
    assert result["results"] == []
    assert result["trace_id"]
    events = memory_audit_query(conn, event_type="memory_live_retrieval_miss", persona="asa")
    assert len(events) == 1
