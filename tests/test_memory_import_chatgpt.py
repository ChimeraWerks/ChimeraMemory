import json
import sqlite3
import zipfile
from pathlib import Path

from chimera_memory.memory import (
    init_memory_tables,
    memory_audit_query,
    memory_import_chatgpt_export,
    memory_pyramid_summary_query,
)
from chimera_memory.memory_import_chatgpt import build_chatgpt_import_plans


def _conversation_payload() -> list[dict]:
    return [
        {
            "id": "conv-1",
            "title": "OB to CM import planning",
            "create_time": 1_715_000_000,
            "mapping": {
                "root": {"message": None},
                "user": {
                    "message": {
                        "author": {"role": "user"},
                        "create_time": 1_715_000_001,
                        "content": {"content_type": "text", "parts": ["Should CM import ChatGPT exports?"]},
                    }
                },
                "assistant": {
                    "message": {
                        "author": {"role": "assistant"},
                        "create_time": 1_715_000_002,
                        "content": {
                            "content_type": "text",
                            "parts": ["Yes. Import into local memory, then build pyramid summaries."],
                        },
                    }
                },
            },
        }
    ]


def _write_export(path: Path) -> Path:
    export_file = path / "conversations.json"
    export_file.write_text(json.dumps(_conversation_payload()), encoding="utf-8")
    return export_file


def test_chatgpt_import_plans_from_json_file(tmp_path: Path) -> None:
    export_file = _write_export(tmp_path)

    result = build_chatgpt_import_plans(export_file, persona="asa")

    assert result["ok"] is True
    assert result["plan_count"] == 1
    plan = result["plans"][0]
    assert plan["relative_path"].startswith("memory/imports/chatgpt/")
    assert plan["message_count"] == 2
    assert "Should CM import ChatGPT exports?" in plan["body"]
    assert "review_status: \"pending\"" in plan["body"]
    assert "can_use_as_instruction: false" in plan["body"]


def test_chatgpt_import_writes_memory_and_pyramid(tmp_path: Path) -> None:
    personas_dir = tmp_path / "personas"
    persona_root = personas_dir / "developer" / "asa"
    persona_root.mkdir(parents=True)
    export_file = _write_export(tmp_path)
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    result = memory_import_chatgpt_export(
        conn,
        personas_dir,
        export_path=str(export_file),
        persona="asa",
        write=True,
        build_pyramid=True,
    )

    assert result["ok"] is True
    assert result["summary"]["written_count"] == 1
    assert result["summary"]["pyramid_built_count"] == 1
    relative_path = result["written_items"][0]["relative_path"]
    assert (persona_root / relative_path).exists()
    summaries = memory_pyramid_summary_query(conn, file_path=relative_path, level_name="document")
    assert len(summaries) == 1
    assert "ChatGPT" in summaries[0]["summary_text"] or "Import" in summaries[0]["summary_text"]
    events = memory_audit_query(conn, event_type="memory_import_chatgpt_completed", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["written_count"] == 1


def test_chatgpt_import_reads_zip_export(tmp_path: Path) -> None:
    zip_path = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("export/conversations.json", json.dumps(_conversation_payload()))

    result = build_chatgpt_import_plans(zip_path, persona="asa")

    assert result["ok"] is True
    assert result["plan_count"] == 1
