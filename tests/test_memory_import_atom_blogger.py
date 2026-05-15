import sqlite3
import zipfile
from pathlib import Path

from chimera_memory.memory import (
    init_memory_tables,
    memory_audit_query,
    memory_import_atom_blogger_export,
    memory_pyramid_summary_query,
)
from chimera_memory.memory_import_atom_blogger import build_atom_blogger_import_plans


ATOM_EXPORT = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:blogger.com,1999:blog-1.post-1</id>
    <published>2026-05-15T05:45:00Z</published>
    <updated>2026-05-15T05:46:00Z</updated>
    <title>CM import plan</title>
    <author><name>Asa</name></author>
    <category term="memory" />
    <link rel="alternate" href="https://example.test/cm-import" />
    <content type="html">&lt;p&gt;Atom/Blogger exports should become governed memories.&lt;/p&gt;</content>
  </entry>
</feed>
"""


def _personas_dir(tmp_path: Path) -> Path:
    personas = tmp_path / "personas"
    (personas / "developer" / "asa").mkdir(parents=True)
    return personas


def test_atom_blogger_import_plans_from_xml_file(tmp_path: Path) -> None:
    export_file = tmp_path / "blog.xml"
    export_file.write_text(ATOM_EXPORT, encoding="utf-8")

    result = build_atom_blogger_import_plans(export_file, persona="asa")

    assert result["ok"] is True
    assert result["document_count"] == 1
    assert result["plan_count"] == 1
    plan = result["plans"][0]
    assert plan["relative_path"].startswith("memory/imports/atom-blogger/")
    assert "Atom/Blogger exports should become governed memories." in plan["body"]
    assert 'review_status: "pending"' in plan["body"]
    assert "can_use_as_instruction: false" in plan["body"]


def test_atom_blogger_import_writes_memory_and_pyramid(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    personas = _personas_dir(tmp_path)
    export_file = tmp_path / "blog.xml"
    export_file.write_text(ATOM_EXPORT, encoding="utf-8")

    result = memory_import_atom_blogger_export(
        conn,
        personas,
        import_path=str(export_file),
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
    events = memory_audit_query(conn, event_type="memory_import_atom_blogger_completed", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["written_count"] == 1


def test_atom_blogger_import_reads_zip_export(tmp_path: Path) -> None:
    zip_path = tmp_path / "blogger-export.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("blog.xml", ATOM_EXPORT)

    result = build_atom_blogger_import_plans(zip_path, persona="asa")

    assert result["ok"] is True
    assert result["plan_count"] == 1
    assert result["plans"][0]["source_path"] == "blog.xml"
