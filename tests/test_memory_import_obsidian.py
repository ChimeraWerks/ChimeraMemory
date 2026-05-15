import json
import sqlite3
import zipfile
from pathlib import Path

from chimera_memory.memory import (
    init_memory_tables,
    memory_audit_query,
    memory_import_obsidian_vault,
    memory_pyramid_summary_query,
)
from chimera_memory.memory_import_obsidian import build_obsidian_import_plans


def _personas_dir(tmp_path: Path) -> Path:
    personas = tmp_path / "personas"
    (personas / "developer" / "asa").mkdir(parents=True)
    return personas


def _write_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "Projects").mkdir(parents=True)
    (vault / ".obsidian").mkdir()
    (vault / "Projects" / "CM OB Lift.md").write_text(
        "\n".join(
            [
                "---",
                "tags: [memory, obsidian]",
                "title: CM OB Lift",
                "---",
                "# CM OB Lift",
                "",
                "CM should lift OB features additively while staying local-first.",
            ]
        ),
        encoding="utf-8",
    )
    (vault / ".obsidian" / "workspace.md").write_text("Ignore this internal file.", encoding="utf-8")
    return vault


def test_obsidian_import_plans_from_vault_directory(tmp_path: Path) -> None:
    vault = _write_vault(tmp_path)

    result = build_obsidian_import_plans(vault, persona="asa")

    assert result["ok"] is True
    assert result["note_count"] == 1
    assert result["plan_count"] == 1
    plan = result["plans"][0]
    assert plan["relative_path"].startswith("memory/imports/obsidian/")
    assert plan["source_path"] == "Projects/CM OB Lift.md"
    assert 'review_status: "pending"' in plan["body"]
    assert "can_use_as_instruction: false" in plan["body"]
    assert "CM should lift OB features additively" in plan["body"]


def test_obsidian_import_writes_memory_and_pyramid(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    personas = _personas_dir(tmp_path)
    vault = _write_vault(tmp_path)

    result = memory_import_obsidian_vault(
        conn,
        personas,
        vault_path=str(vault),
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
    events = memory_audit_query(conn, event_type="memory_import_obsidian_completed", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["written_count"] == 1


def test_obsidian_import_reads_zip_vault(tmp_path: Path) -> None:
    zip_path = tmp_path / "obsidian-vault.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(
            "Vault/Notes/Portable Context.md",
            "\n".join(
                [
                    "---",
                    json.dumps({"tags": ["profile", "export"]})[1:-1],
                    "---",
                    "# Portable Context",
                    "",
                    "Portable context export should use reviewed memory.",
                ]
            ),
        )
        archive.writestr("Vault/.obsidian/workspace.json", "{}")

    result = build_obsidian_import_plans(zip_path, persona="asa")

    assert result["ok"] is True
    assert result["plan_count"] == 1
    assert result["plans"][0]["source_path"] == "Vault/Notes/Portable Context.md"
