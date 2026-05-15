import mailbox
import sqlite3
import zipfile
from email.message import EmailMessage
from pathlib import Path

from chimera_memory.memory import (
    init_memory_tables,
    memory_audit_query,
    memory_import_gmail_mbox,
    memory_pyramid_summary_query,
)
from chimera_memory.memory_import_gmail import build_gmail_import_plans


def _personas_dir(tmp_path: Path) -> Path:
    personas = tmp_path / "personas"
    (personas / "developer" / "asa").mkdir(parents=True)
    return personas


def _message(subject: str = "CM import planning") -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Charles <charles@example.com>"
    message["To"] = "Asa <asa@example.com>"
    message["Subject"] = subject
    message["Date"] = "Fri, 15 May 2026 04:00:00 +0000"
    message["Message-ID"] = "<cm-import-1@example.com>"
    message.set_content("CM should import Gmail mbox exports as restricted evidence-only memory.")
    return message


def _write_mbox(path: Path) -> Path:
    box = mailbox.mbox(str(path))
    try:
        box.lock()
        box.add(_message())
        box.flush()
    finally:
        box.unlock()
        box.close()
    return path


def test_gmail_import_plans_from_mbox_file(tmp_path: Path) -> None:
    mbox_path = _write_mbox(tmp_path / "All Mail.mbox")

    result = build_gmail_import_plans(mbox_path, persona="asa")

    assert result["ok"] is True
    assert result["message_count"] == 1
    assert result["plan_count"] == 1
    plan = result["plans"][0]
    assert plan["relative_path"].startswith("memory/imports/gmail/")
    assert plan["subject"] == "CM import planning"
    assert 'review_status: "pending"' in plan["body"]
    assert 'sensitivity_tier: "restricted"' in plan["body"]
    assert "can_use_as_instruction: false" in plan["body"]


def test_gmail_import_writes_memory_and_pyramid(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    personas = _personas_dir(tmp_path)
    mbox_path = _write_mbox(tmp_path / "All Mail.mbox")

    result = memory_import_gmail_mbox(
        conn,
        personas,
        import_path=str(mbox_path),
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
        SELECT fm_review_status, fm_sensitivity_tier,
               fm_can_use_as_instruction, fm_can_use_as_evidence
        FROM memory_files
        WHERE relative_path = ?
        """,
        (relative_path,),
    ).fetchone()
    assert row == ("pending", "restricted", 0, 1)
    summaries = memory_pyramid_summary_query(conn, file_path=relative_path, level_name="document")
    assert len(summaries) == 1
    events = memory_audit_query(conn, event_type="memory_import_gmail_completed", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["written_count"] == 1


def test_gmail_import_reads_takeout_zip(tmp_path: Path) -> None:
    mbox_path = _write_mbox(tmp_path / "All Mail.mbox")
    zip_path = tmp_path / "takeout.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(mbox_path, "Takeout/Mail/All Mail.mbox")

    result = build_gmail_import_plans(zip_path, persona="asa")

    assert result["ok"] is True
    assert result["plan_count"] == 1
    assert result["plans"][0]["source_path"] == "Takeout/Mail/All Mail.mbox"
