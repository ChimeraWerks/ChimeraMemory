import json
import sqlite3
import sys
from pathlib import Path

import pytest

from chimera_memory.cli import main
from chimera_memory.memory import index_file, init_memory_tables


def _index_cli_memory(db_path: Path, memory_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        init_memory_tables(conn)
        memory_path.write_text(
            "\n".join(
                [
                    "---",
                    "type: procedural",
                    "importance: 7",
                    "tags: [cli, sidecar]",
                    "---",
                    "CLI dry-run should process queued metadata on 2026-05-14.",
                    "TODO: keep the real model adapter behind a separate seam.",
                ]
            ),
            encoding="utf-8",
        )
        assert index_file(conn, "asa", memory_path.name, memory_path)
        conn.commit()
    finally:
        conn.close()


def test_cli_enhance_provider_plan_json_excludes_credential_refs(monkeypatch, capsys) -> None:
    monkeypatch.setenv("CHIMERA_MEMORY_ENHANCEMENT_OPENAI_CREDENTIAL_REF", "oauth:openai-memory")
    monkeypatch.setattr(sys, "argv", ["chimera-memory", "enhance", "provider-plan", "--json"])

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_provider"] == "openai"
    assert payload["candidates"][0]["credential_ref_present"] is True
    assert "oauth:openai-memory" not in json.dumps(payload)


def test_cli_enhance_enqueue_and_dry_run_json(tmp_path: Path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "transcript.db"
    memory_path = tmp_path / "cli-memory.md"
    _index_cli_memory(db_path, memory_path)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "chimera-memory",
            "enhance",
            "enqueue",
            "--db",
            str(db_path),
            "--file",
            memory_path.name,
            "--json",
        ],
    )
    main()
    enqueued = json.loads(capsys.readouterr().out)
    assert enqueued["ok"] is True
    assert enqueued["job"]["status"] == "pending"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "chimera-memory",
            "enhance",
            "dry-run",
            "--db",
            str(db_path),
            "--persona",
            "asa",
            "--json",
        ],
    )
    main()
    processed = json.loads(capsys.readouterr().out)
    assert processed["processed_count"] == 1
    assert processed["processed"][0]["status"] == "succeeded"
    assert processed["processed"][0]["result_payload"]["review_status"] == "pending"
    assert processed["processed"][0]["result_payload"]["can_use_as_instruction"] is False


def test_cli_enhance_enqueue_missing_file_exits_cleanly(tmp_path: Path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "transcript.db"
    conn = sqlite3.connect(db_path)
    try:
        init_memory_tables(conn)
    finally:
        conn.close()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "chimera-memory",
            "enhance",
            "enqueue",
            "--db",
            str(db_path),
            "--file",
            "missing.md",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    assert "Enhancement enqueue failed" in capsys.readouterr().out
