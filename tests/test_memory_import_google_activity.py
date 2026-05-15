import json
import sqlite3
import zipfile
from pathlib import Path

from chimera_memory.memory import (
    init_memory_tables,
    memory_audit_query,
    memory_import_google_activity_export,
    memory_pyramid_summary_query,
)
from chimera_memory.memory_import_google_activity import build_google_activity_import_plans


def _personas_dir(tmp_path: Path) -> Path:
    personas = tmp_path / "personas"
    (personas / "developer" / "asa").mkdir(parents=True)
    return personas


def _write_export_dir(tmp_path: Path) -> Path:
    root = tmp_path / "Takeout" / "My Activity" / "Search"
    root.mkdir(parents=True)
    (root / "MyActivity.json").write_text(
        json.dumps(
            [
                {
                    "header": "Search",
                    "title": "Searched for ChimeraMemory import pipeline",
                    "titleUrl": "https://www.google.com/search?q=ChimeraMemory+import+pipeline",
                    "time": "2026-05-15T05:30:00.000Z",
                    "products": ["Search"],
                    "details": [{"name": "From your Google Account"}],
                }
            ]
        ),
        encoding="utf-8",
    )
    return root.parent.parent


def test_google_activity_import_plans_from_directory(tmp_path: Path) -> None:
    export_dir = _write_export_dir(tmp_path)

    result = build_google_activity_import_plans(export_dir, persona="asa")

    assert result["ok"] is True
    assert result["document_count"] == 1
    assert result["plan_count"] == 1
    plan = result["plans"][0]
    assert plan["relative_path"].startswith("memory/imports/google-activity/")
    assert "Searched for ChimeraMemory import pipeline" in plan["body"]
    assert 'review_status: "pending"' in plan["body"]
    assert 'sensitivity_tier: "restricted"' in plan["body"]
    assert "can_use_as_instruction: false" in plan["body"]


def test_google_activity_import_writes_memory_and_pyramid(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    personas = _personas_dir(tmp_path)
    export_dir = _write_export_dir(tmp_path)

    result = memory_import_google_activity_export(
        conn,
        personas,
        import_path=str(export_dir),
        persona="asa",
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
    assert 'sensitivity_tier: "restricted"' in content
    row = conn.execute(
        """
        SELECT fm_review_status, fm_sensitivity_tier, fm_can_use_as_instruction, fm_can_use_as_evidence
        FROM memory_files
        WHERE relative_path = ?
        """,
        (relative_path,),
    ).fetchone()
    assert row == ("pending", "restricted", 0, 1)
    summaries = memory_pyramid_summary_query(conn, file_path=relative_path, level_name="document")
    assert len(summaries) == 1
    events = memory_audit_query(conn, event_type="memory_import_google_activity_completed", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["written_count"] == 1


def test_google_activity_import_reads_zip_export(tmp_path: Path) -> None:
    zip_path = tmp_path / "takeout.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(
            "Takeout/My Activity/Search/MyActivity.json",
            json.dumps(
                [
                    {
                        "header": "Search",
                        "title": "Visited Chimera docs",
                        "time": "2026-05-15T05:31:00.000Z",
                        "products": ["Search"],
                    }
                ]
            ),
        )

    result = build_google_activity_import_plans(zip_path, persona="asa")

    assert result["ok"] is True
    assert result["plan_count"] == 1
    assert result["plans"][0]["source_path"].endswith("MyActivity.json")
