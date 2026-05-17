import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

from chimera_memory.cli import main
from chimera_memory.memory import index_file, init_memory_tables, memory_enhancement_enqueue
from chimera_memory.memory_enhancement_sidecar import create_dry_run_sidecar_server


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


def test_cli_enhance_authored_enqueue_json(tmp_path: Path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "transcript.db"
    conn = sqlite3.connect(db_path)
    try:
        init_memory_tables(conn)
    finally:
        conn.close()
    payload_path = tmp_path / "authored.json"
    payload_path.write_text(
        json.dumps(
            {
                "memory_payload": {
                    "memory_type": "procedural",
                    "lessons": [{"teaching": "Structured writeback keeps LLM enrichment narrow."}],
                    "next_steps": [{"action": "Keep LLM enrichment narrow"}],
                },
                "provenance": {"status": "generated"},
                "source_ref": "day61/structured-writeback",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "chimera-memory",
            "enhance",
            "authored-enqueue",
            "--db",
            str(db_path),
            "--persona",
            "asa",
            "--payload",
            str(payload_path),
            "--json",
        ],
    )

    main()

    enqueued = json.loads(capsys.readouterr().out)
    assert enqueued["ok"] is True
    assert enqueued["job"]["status"] == "pending"
    assert enqueued["job"]["path"] == "day61/structured-writeback"
    assert enqueued["job"]["request_payload"]["task"] == "enrich_authored_memory_payload"
    assert enqueued["job"]["request_payload"]["contract"]["action_items"] == ["Keep LLM enrichment narrow"]


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


def test_cli_enhance_sidecar_run_processes_queued_job(tmp_path: Path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "transcript.db"
    memory_path = tmp_path / "sidecar-run.md"
    _index_cli_memory(db_path, memory_path)
    conn = sqlite3.connect(db_path)
    try:
        init_memory_tables(conn)
        enqueued = memory_enhancement_enqueue(conn, file_path=memory_path.name)
    finally:
        conn.close()
    assert enqueued["ok"] is True

    fake_token = "TEST_ONLY_SIDE_TOKEN"
    server = create_dry_run_sidecar_server("127.0.0.1", 0, bearer_token=fake_token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("CHIMERA_MEMORY_TEST_SIDECAR_TOKEN", fake_token)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "chimera-memory",
                "enhance",
                "sidecar-run",
                "--db",
                str(db_path),
                "--endpoint",
                f"http://127.0.0.1:{server.server_port}/enhance",
                "--persona",
                "asa",
                "--token-env",
                "CHIMERA_MEMORY_TEST_SIDECAR_TOKEN",
                "--json",
            ],
        )

        main()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    receipt = json.loads(capsys.readouterr().out)
    assert receipt["processed_count"] == 1
    assert receipt["failure_count"] == 0
    assert receipt["processed"][0]["job_id"] == enqueued["job"]["job_id"]
    assert fake_token not in json.dumps(receipt)

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, result_payload FROM memory_enhancement_jobs WHERE job_id = ?",
            (enqueued["job"]["job_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "succeeded"
    assert '"can_use_as_instruction": false' in row[1]


def test_cli_enhance_serve_provider_uses_separate_sidecar_and_provider_tokens(monkeypatch, capsys) -> None:
    sidecar_token = "TEST_ONLY_SIDECAR_TOKEN"
    provider_token = "TEST_ONLY_PROVIDER_TOKEN"
    captured = {}

    def fake_run_provider_sidecar(host, port, *, client, bearer_token):
        captured["host"] = host
        captured["port"] = port
        captured["client"] = client
        captured["bearer_token"] = bearer_token

    monkeypatch.setenv("CHIMERA_MEMORY_TEST_SIDECAR_TOKEN", sidecar_token)
    monkeypatch.setenv("CHIMERA_MEMORY_TEST_PROVIDER_TOKEN", provider_token)
    monkeypatch.setattr(
        "chimera_memory.memory_enhancement_sidecar.run_provider_sidecar",
        fake_run_provider_sidecar,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "chimera-memory",
            "enhance",
            "serve-provider",
            "--host",
            "127.0.0.1",
            "--port",
            "8998",
            "--token-env",
            "CHIMERA_MEMORY_TEST_SIDECAR_TOKEN",
            "--provider-token-env",
            "CHIMERA_MEMORY_TEST_PROVIDER_TOKEN",
        ],
    )

    main()

    output = capsys.readouterr().out
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8998
    assert captured["bearer_token"] == sidecar_token
    assert captured["client"]._api_key_client_factory("").bearer_token == provider_token
    assert sidecar_token not in output
    assert provider_token not in output


def test_cli_enhance_serve_provider_missing_provider_token_exits_without_env_name(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "chimera-memory",
            "enhance",
            "serve-provider",
            "--provider-token-env",
            "CHIMERA_MEMORY_TEST_PROVIDER_TOKEN",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "Provider token env var is not set" in captured.err
    assert "CHIMERA_MEMORY_TEST_PROVIDER_TOKEN" not in captured.err
