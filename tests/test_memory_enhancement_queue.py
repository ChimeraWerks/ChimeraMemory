import sqlite3
from pathlib import Path

from chimera_memory.memory import (
    index_file,
    init_memory_tables,
    memory_audit_query,
    memory_enhancement_claim_next,
    memory_enhancement_complete,
    memory_enhancement_enqueue,
    memory_enhancement_enqueue_authored,
    memory_entity_connections,
    memory_entity_query,
)


def _index_memory(conn: sqlite3.Connection, tmp_path: Path, name: str = "target.md") -> None:
    memory_file = tmp_path / name
    memory_file.write_text(
        "\n".join(
            [
                "---",
                "type: procedural",
                "importance: 6",
                "tags: [sidecar]",
                "---",
                "Sidecar queue target body.",
            ]
        ),
        encoding="utf-8",
    )
    assert index_file(conn, "asa", name, memory_file)


def test_memory_enhancement_enqueue_builds_pending_job(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_memory(conn, tmp_path)

    result = memory_enhancement_enqueue(
        conn,
        file_path="target.md",
        requested_provider="local",
        requested_model="dry-run",
    )

    assert result["ok"] is True
    assert result["enqueued"] is True
    job = result["job"]
    assert job["status"] == "pending"
    assert job["persona"] == "asa"
    assert job["requested_provider"] == "local"
    assert job["requested_model"] == "dry-run"
    assert job["request_payload"]["task"] == "extract_memory_metadata"
    assert job["request_payload"]["policy"]["content_is_untrusted"] is True
    assert "Sidecar queue target body." in job["request_payload"]["wrapped_content"]

    events = memory_audit_query(conn, event_type="memory_enhancement_enqueued", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["job_id"] == job["job_id"]


def test_memory_enhancement_enqueue_dedupes_active_job(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_memory(conn, tmp_path)

    first = memory_enhancement_enqueue(conn, file_path="target.md")
    second = memory_enhancement_enqueue(conn, file_path="target.md")

    assert first["enqueued"] is True
    assert second["enqueued"] is False
    assert second["job"]["job_id"] == first["job"]["job_id"]


def test_memory_enhancement_claim_and_complete_success(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_memory(conn, tmp_path)
    enqueued = memory_enhancement_enqueue(conn, file_path="target.md")

    claimed = memory_enhancement_claim_next(conn, persona="asa")

    assert claimed is not None
    assert claimed["job_id"] == enqueued["job"]["job_id"]
    assert claimed["status"] == "running"
    assert claimed["attempt_count"] == 1
    assert claimed["locked_at"]

    completed = memory_enhancement_complete(
        conn,
        job_id=claimed["job_id"],
        status="succeeded",
        response_payload={
            "memory_type": "lesson",
            "summary": "Queue outputs stay review gated.",
            "topics": ["queue", "sidecar"],
            "people": ["Charles"],
            "projects": ["PA"],
            "tools": ["Codex"],
            "confidence": 0.88,
        },
    )

    assert completed["ok"] is True
    job = completed["job"]
    assert job["status"] == "succeeded"
    assert job["locked_at"] is None
    assert job["result_payload"]["memory_type"] == "lesson"
    assert job["result_payload"]["review_status"] == "pending"
    assert job["result_payload"]["can_use_as_instruction"] is False
    assert memory_entity_query(conn, query="Charles", entity_type="person")[0]["file_count"] == 1
    assert memory_entity_query(conn, query="PA", entity_type="project")[0]["file_count"] == 1
    connections = memory_entity_connections(conn, entity_name="Charles", entity_type="person")
    assert {row["canonical_name"] for row in connections} == {"PA", "Codex", "queue", "sidecar"}

    events = memory_audit_query(conn, persona="asa")
    event_types = {event["event_type"] for event in events}
    assert "memory_enhancement_started" in event_types
    assert "memory_enhancement_completed" in event_types
    completed_events = [event for event in events if event["event_type"] == "memory_enhancement_completed"]
    assert completed_events[0]["payload"]["entities"] == {"link_count": 5, "edge_count": 10}


def test_memory_enhancement_complete_failure_records_error(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_memory(conn, tmp_path)
    enqueued = memory_enhancement_enqueue(conn, file_path="target.md")
    claimed = memory_enhancement_claim_next(conn)

    result = memory_enhancement_complete(
        conn,
        job_id=claimed["job_id"],
        status="failed",
        error="model unavailable",
    )

    assert result["ok"] is True
    assert result["job"]["status"] == "failed"
    assert result["job"]["error"] == "model unavailable"
    assert result["job"]["job_id"] == enqueued["job"]["job_id"]

    events = memory_audit_query(conn, event_type="memory_enhancement_failed", persona="asa")
    assert len(events) == 1


def test_memory_enhancement_enqueue_authored_builds_pending_job() -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    result = memory_enhancement_enqueue_authored(
        conn,
        persona="asa",
        memory_payload={
            "memory_type": "procedural",
            "lessons": [{"teaching": "Structured payloads own the memory."}],
            "next_steps": [{"action": "Keep LLM enrichment narrow"}],
        },
        provenance={"status": "generated"},
        source_ref="day61/structured-writeback",
        requested_provider="local",
        requested_model="dry-run",
    )

    assert result["ok"] is True
    job = result["job"]
    assert job["file_id"] is None
    assert job["path"] == "day61/structured-writeback"
    assert job["requested_provider"] == "local"
    assert job["requested_model"] == "dry-run"
    assert job["request_payload"]["task"] == "enrich_authored_memory_payload"
    assert job["request_payload"]["contract"]["action_items"] == ["Keep LLM enrichment narrow"]

    events = memory_audit_query(conn, event_type="memory_enhancement_authored_enqueued", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["job_id"] == job["job_id"]


def test_memory_enhancement_complete_authored_uses_agent_fields() -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    enqueued = memory_enhancement_enqueue_authored(
        conn,
        persona="asa",
        memory_payload={
            "memory_type": "episode",
            "summary": "Structured writeback was added beside legacy enhancement.",
            "lessons": [{"teaching": "Agent-authored payloads are authoritative."}],
            "next_steps": [{"action": "Preserve structured writeback discipline"}],
            "entities": {"topics": ["writeback discipline"], "projects": ["ChimeraMemory"]},
        },
        provenance={"status": "generated"},
        source_ref="day61/structured-writeback",
    )
    claimed = memory_enhancement_claim_next(conn, persona="asa")

    completed = memory_enhancement_complete(
        conn,
        job_id=claimed["job_id"],
        status="succeeded",
        response_payload={
            "memory_type": "semantic",
            "summary": "Model summary must not win.",
            "action_items": ["Model action must not win."],
            "topics": ["memory enhancement", "not-in-enum"],
            "people": ["Charles"],
            "confidence": 0.8,
        },
    )

    assert completed["ok"] is True
    assert completed["job"]["job_id"] == enqueued["job"]["job_id"]
    result_payload = completed["job"]["result_payload"]
    assert result_payload["schema_version"] == "chimera-memory.authored-writeback.v1"
    assert result_payload["memory_type"] == "episodic"
    assert result_payload["summary"] == "Structured writeback was added beside legacy enhancement."
    assert result_payload["action_items"] == ["Preserve structured writeback discipline"]
    assert result_payload["topics"] == ["writeback-discipline", "memory-enhancement"]
    assert result_payload["people"] == ["Charles"]
    assert result_payload["can_use_as_instruction"] is False
    assert result_payload["review_status"] == "pending"
    assert result_payload["enrichment_status"] == "complete"
    assert memory_entity_query(conn, query="Charles", entity_type="person") == []


def test_memory_enhancement_enqueue_reports_missing_file() -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    result = memory_enhancement_enqueue(conn, file_path="missing.md")

    assert result == {
        "ok": False,
        "error": "memory file not found",
        "file_path": "missing.md",
    }
