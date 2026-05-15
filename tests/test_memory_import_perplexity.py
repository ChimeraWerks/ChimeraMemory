import json
import sqlite3
import zipfile
from pathlib import Path

from chimera_memory.memory import (
    init_memory_tables,
    memory_audit_query,
    memory_import_perplexity_export,
    memory_pyramid_summary_query,
)
from chimera_memory.memory_import_perplexity import build_perplexity_import_plans


def _personas_dir(tmp_path: Path) -> Path:
    personas = tmp_path / "personas"
    (personas / "developer" / "asa").mkdir(parents=True)
    return personas


def _write_export_dir(tmp_path: Path) -> Path:
    root = tmp_path / "perplexity"
    root.mkdir()
    (root / "CM Retrieval.md").write_text(
        "\n".join(
            [
                "---",
                "title: CM Retrieval",
                "---",
                "# CM Retrieval",
                "",
                "Perplexity research says hybrid retrieval should keep lexical and semantic signals.",
            ]
        ),
        encoding="utf-8",
    )
    (root / "thread.json").write_text(
        json.dumps(
            {
                "title": "Portable context research",
                "messages": [
                    {"role": "user", "content": "How should portable context export work?"},
                    {"role": "assistant", "content": "Use reviewed memory and structured markdown artifacts."},
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


def test_perplexity_import_plans_from_directory(tmp_path: Path) -> None:
    export_dir = _write_export_dir(tmp_path)

    result = build_perplexity_import_plans(export_dir, persona="asa")

    assert result["ok"] is True
    assert result["document_count"] == 2
    assert result["plan_count"] == 2
    paths = {plan["source_path"] for plan in result["plans"]}
    assert paths == {"CM Retrieval.md", "thread.json"}
    assert all(plan["relative_path"].startswith("memory/imports/perplexity/") for plan in result["plans"])
    assert all('review_status: "pending"' in plan["body"] for plan in result["plans"])
    assert all("can_use_as_instruction: false" in plan["body"] for plan in result["plans"])


def test_perplexity_import_writes_memory_and_pyramid(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    personas = _personas_dir(tmp_path)
    export_dir = _write_export_dir(tmp_path)

    result = memory_import_perplexity_export(
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
    events = memory_audit_query(conn, event_type="memory_import_perplexity_completed", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["written_count"] == 1


def test_perplexity_import_reads_zip_export(tmp_path: Path) -> None:
    zip_path = tmp_path / "perplexity-export.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("exports/answer.txt", "A Perplexity answer about CM import pipelines.")

    result = build_perplexity_import_plans(zip_path, persona="asa")

    assert result["ok"] is True
    assert result["plan_count"] == 1
    assert result["plans"][0]["source_path"] == "exports/answer.txt"
