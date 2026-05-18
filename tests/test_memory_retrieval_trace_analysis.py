import sqlite3
from pathlib import Path

from chimera_memory.memory import (
    index_file,
    init_memory_tables,
    memory_audit_query,
    memory_search,
)
from chimera_memory.memory_retrieval_trace_analysis import (
    StaticMemoryRetrievalTraceAnalysisClient,
    memory_retrieval_trace_analyze,
)


def test_retrieval_trace_analysis_sends_safe_trace_summary(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    target = tmp_path / "charles-deploy.md"
    target.write_text(
        "\n".join(
            [
                "---",
                "type: feedback",
                "importance: 9",
                "about: Charles deployment preferences",
                "---",
                "Always deploy for Charles after building and smoke testing.",
            ]
        ),
        encoding="utf-8",
    )
    assert index_file(conn, "asa", "memory/charles-deploy.md", target)
    assert memory_search(conn, "deploy Charles", persona="asa", limit=3)

    client = StaticMemoryRetrievalTraceAnalysisClient(
        [
            {
                "category": "query_too_vague",
                "secondary_categories": ["wrong_tool_route"],
                "severity": "medium",
                "confidence": 0.88,
                "recommendation": "Try an intent-shaped query before changing ranking.",
                "evidence": ["The trace used a person-shaped query with procedural target intent."],
                "query_expansions": ["Charles deployment preferences", "deploy for Charles rules"],
                "suggested_tool_route": "memory_recall",
            }
        ]
    )

    result = memory_retrieval_trace_analyze(
        conn,
        client=client,
        env={"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"},
        persona="asa",
        limit=1,
    )

    assert result["analysis_count"] == 1
    assert result["category_counts"] == {"query_too_vague": 1}
    assert result["analyses"][0]["suggested_tool_route"] == "memory_recall"
    assert client.invocations[0]["raw_json"] is True
    assert "Raw memory bodies are intentionally absent" in client.invocations[0]["system_prompt"]
    trace = client.invocations[0]["request"]["trace"]
    assert trace["query_text"] == "deploy Charles"
    assert trace["items"][0]["relative_path"] == "memory/charles-deploy.md"
    assert "body" not in trace["items"][0]
    assert "Always deploy" not in str(trace)

    events = memory_audit_query(conn, event_type="memory_retrieval_trace_analysis", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["category_counts"] == {"query_too_vague": 1}


def test_retrieval_trace_analysis_normalizes_untrusted_model_output() -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    conn.execute(
        """
        INSERT INTO memory_recall_traces (
            trace_id, tool_name, persona, query_text, requested_limit,
            result_count, returned_count
        ) VALUES ('trace-1', 'memory_recall', 'asa', 'what should I remember', 5, 0, 0)
        """
    )
    conn.commit()
    client = StaticMemoryRetrievalTraceAnalysisClient(
        [
            {
                "category": "invented_category",
                "secondary_categories": ["diagnostics_noise_pollution", "fake"],
                "severity": "catastrophic",
                "confidence": 9,
                "recommendation": "x" * 1000,
                "evidence": ["e" * 300],
                "query_expansions": ["q1", "q2", "q3", "q4", "q5", "q6"],
                "suggested_tool_route": "magic",
            }
        ]
    )

    result = memory_retrieval_trace_analyze(
        conn,
        client=client,
        env={"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"},
        trace_id="trace-1",
    )

    analysis = result["analyses"][0]
    assert analysis["category"] == "unknown"
    assert analysis["secondary_categories"] == ["diagnostics_noise_pollution"]
    assert analysis["severity"] == "medium"
    assert analysis["confidence"] == 1.0
    assert len(analysis["recommendation"]) == 700
    assert len(analysis["evidence"][0]) == 240
    assert len(analysis["query_expansions"]) == 5
    assert analysis["suggested_tool_route"] == "unknown"
