import sqlite3
from pathlib import Path

from chimera_memory.enhancement_worker import (
    derive_dry_run_metadata,
    run_memory_enhancement_dry_run,
)
from chimera_memory.memory import (
    index_file,
    init_memory_tables,
    memory_audit_query,
    memory_enhancement_enqueue,
)


def _index_worker_memory(conn: sqlite3.Connection, tmp_path: Path, name: str = "worker.md") -> None:
    memory_file = tmp_path / name
    memory_file.write_text(
        "\n".join(
            [
                "---",
                "type: procedural",
                "importance: 7",
                "tags: [sidecar, queue]",
                "---",
                "Review queued metadata on 2026-05-14.",
                "TODO: wire the cheap model after the dry-run worker.",
            ]
        ),
        encoding="utf-8",
    )
    assert index_file(conn, "asa", name, memory_file)


def test_derive_dry_run_metadata_uses_existing_type_tags_and_body() -> None:
    job = {
        "request_payload": {
            "existing_frontmatter": {"type": "procedural", "tags": ["sidecar"]},
            "wrapped_content": "\n".join(
                [
                    "----- BEGIN UNTRUSTED MEMORY CONTENT -----",
                    "Review queued metadata on 2026-05-14.",
                    "TODO: wire the cheap model after the dry-run worker.",
                    "----- END UNTRUSTED MEMORY CONTENT -----",
                ]
            ),
        }
    }

    metadata = derive_dry_run_metadata(job)

    assert metadata["memory_type"] == "procedural"
    assert metadata["summary"] == "Review queued metadata on 2026-05-14."
    assert "sidecar" in metadata["topics"]
    assert "2026-05-14" in metadata["dates"]
    assert metadata["action_items"] == ["wire the cheap model after the dry-run worker."]
    assert metadata["confidence"] == 0.35


def test_run_memory_enhancement_dry_run_consumes_queue_without_mutating_memory(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_worker_memory(conn, tmp_path)
    enqueued = memory_enhancement_enqueue(conn, file_path="worker.md")

    processed = run_memory_enhancement_dry_run(conn, persona="asa")

    assert len(processed) == 1
    job = processed[0]
    assert job["job_id"] == enqueued["job"]["job_id"]
    assert job["status"] == "succeeded"
    assert job["result_payload"]["memory_type"] == "procedural"
    assert job["result_payload"]["review_status"] == "pending"
    assert job["result_payload"]["can_use_as_instruction"] is False

    memory_row = conn.execute(
        """
        SELECT fm_review_status, fm_can_use_as_instruction
        FROM memory_files
        WHERE relative_path = 'worker.md'
        """
    ).fetchone()
    assert memory_row == ("confirmed", 1)

    events = memory_audit_query(conn, persona="asa")
    event_types = {event["event_type"] for event in events}
    assert {
        "memory_enhancement_enqueued",
        "memory_enhancement_started",
        "memory_enhancement_completed",
    }.issubset(event_types)


def test_run_memory_enhancement_dry_run_respects_persona_filter(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_worker_memory(conn, tmp_path, "asa.md")

    sarah_file = tmp_path / "sarah.md"
    sarah_file.write_text("Sarah queue body", encoding="utf-8")
    assert index_file(conn, "sarah", "sarah.md", sarah_file)

    memory_enhancement_enqueue(conn, file_path="asa.md")
    memory_enhancement_enqueue(conn, file_path="sarah.md")

    processed = run_memory_enhancement_dry_run(conn, persona="sarah")

    assert len(processed) == 1
    assert processed[0]["persona"] == "sarah"
    statuses = dict(
        conn.execute(
            "SELECT persona, status FROM memory_enhancement_jobs ORDER BY persona"
        ).fetchall()
    )
    assert statuses == {"asa": "pending", "sarah": "succeeded"}
