import json
import sqlite3
import zipfile
from pathlib import Path

from chimera_memory.memory import (
    init_memory_tables,
    memory_audit_query,
    memory_import_grok_export,
    memory_pyramid_summary_query,
)
from chimera_memory.memory_import_grok import build_grok_import_plans


def _personas_dir(tmp_path: Path) -> Path:
    personas = tmp_path / "personas"
    (personas / "developer" / "asa").mkdir(parents=True)
    return personas


def _write_export_dir(tmp_path: Path) -> Path:
    root = tmp_path / "grok"
    root.mkdir()
    (root / "conversations.json").write_text(
        json.dumps(
            {
                "conversations": [
                    {
                        "id": "grok-1",
                        "title": "Memory import planning",
                        "created_at": "2026-05-15T01:02:03Z",
                        "messages": [
                            {"role": "user", "content": "How should Grok imports enter CM?"},
                            {"role": "assistant", "content": "As governed evidence-only memories."},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (root / "thread.jsonl").write_text(
        json.dumps(
            {
                "id": "grok-2",
                "conversation_title": "Portable context",
                "messages": [
                    {"sender": "user", "text": "What should export include?"},
                    {"sender": "grok", "text": "Reviewed memory and concise profile artifacts."},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def test_grok_import_plans_from_directory(tmp_path: Path) -> None:
    export_dir = _write_export_dir(tmp_path)

    result = build_grok_import_plans(export_dir, persona="asa")

    assert result["ok"] is True
    assert result["document_count"] == 2
    assert result["plan_count"] == 2
    paths = {plan["source_path"] for plan in result["plans"]}
    assert paths == {"conversations.json", "thread.jsonl#1"}
    assert all(plan["relative_path"].startswith("memory/imports/grok/") for plan in result["plans"])
    assert all('review_status: "pending"' in plan["body"] for plan in result["plans"])
    assert all("can_use_as_instruction: false" in plan["body"] for plan in result["plans"])


def test_grok_import_writes_memory_and_pyramid(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    personas = _personas_dir(tmp_path)
    export_dir = _write_export_dir(tmp_path)

    result = memory_import_grok_export(
        conn,
        personas,
        import_path=str(export_dir),
        persona="asa",
        limit=1,
        write=True,
        build_pyramid=True,
    )

    assert result["ok"] is True
    assert result["summary"]["written_count"] == 1
    assert result["summary"]["pyramid_built_count"] == 1
    relative_path = result["written_items"][0]["relative_path"]
    memory_file = personas / "developer" / "asa" / relative_path
    assert memory_file.exists()
    content = memory_file.read_text(encoding="utf-8")
    assert 'provenance_status: "imported"' in content
    assert 'review_status: "pending"' in content
    row = conn.execute(
        """
        SELECT fm_review_status, fm_can_use_as_instruction, fm_can_use_as_evidence
        FROM memory_files
        WHERE relative_path = ?
        """,
        (relative_path,),
    ).fetchone()
    assert row == ("pending", 0, 1)
    summaries = memory_pyramid_summary_query(conn, file_path=relative_path, level_name="document")
    assert len(summaries) == 1
    events = memory_audit_query(conn, event_type="memory_import_grok_completed", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["written_count"] == 1


def test_grok_import_reads_zip_export(tmp_path: Path) -> None:
    zip_path = tmp_path / "grok-export.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("exports/thread.txt", "Grok notes about CM import pipelines.")

    result = build_grok_import_plans(zip_path, persona="asa")

    assert result["ok"] is True
    assert result["plan_count"] == 1
    assert result["plans"][0]["source_path"] == "exports/thread.txt"
