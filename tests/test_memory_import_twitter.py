import json
import sqlite3
import zipfile
from pathlib import Path

from chimera_memory.memory import (
    init_memory_tables,
    memory_audit_query,
    memory_import_twitter_archive,
    memory_pyramid_summary_query,
)
from chimera_memory.memory_import_twitter import build_twitter_import_plans


def _personas_dir(tmp_path: Path) -> Path:
    personas = tmp_path / "personas"
    (personas / "developer" / "asa").mkdir(parents=True)
    return personas


def _tweet_payload() -> list[dict]:
    return [
        {
            "tweet": {
                "id_str": "111",
                "created_at": "Fri May 15 05:00:00 +0000 2026",
                "full_text": "CM should import X archives as governed evidence-only memory. #memory",
                "favorite_count": "2",
                "retweet_count": "1",
                "source": "Twitter Web App",
                "entities": {
                    "hashtags": [{"text": "memory"}],
                    "user_mentions": [{"screen_name": "ChimeraWerks"}],
                    "urls": [{"expanded_url": "https://example.test/context"}],
                },
            }
        }
    ]


def _write_export_dir(tmp_path: Path) -> Path:
    root = tmp_path / "twitter"
    data = root / "data"
    data.mkdir(parents=True)
    (data / "tweets.js").write_text(
        "window.YTD.tweets.part0 = " + json.dumps(_tweet_payload()) + ";",
        encoding="utf-8",
    )
    return root


def test_twitter_import_plans_from_archive_directory(tmp_path: Path) -> None:
    export_dir = _write_export_dir(tmp_path)

    result = build_twitter_import_plans(export_dir, persona="asa")

    assert result["ok"] is True
    assert result["document_count"] == 1
    assert result["plan_count"] == 1
    plan = result["plans"][0]
    assert plan["source_path"] == "data/tweets.js"
    assert plan["relative_path"].startswith("memory/imports/twitter/")
    assert "CM should import X archives" in plan["body"]
    assert 'review_status: "pending"' in plan["body"]
    assert "can_use_as_instruction: false" in plan["body"]
    assert "- hashtags: memory" in plan["body"]


def test_twitter_import_writes_memory_and_pyramid(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    personas = _personas_dir(tmp_path)
    export_dir = _write_export_dir(tmp_path)

    result = memory_import_twitter_archive(
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
    events = memory_audit_query(conn, event_type="memory_import_twitter_completed", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["written_count"] == 1


def test_twitter_import_reads_zip_export(tmp_path: Path) -> None:
    zip_path = tmp_path / "twitter-archive.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("data/tweets.js", "window.YTD.tweets.part0 = " + json.dumps(_tweet_payload()) + ";")

    result = build_twitter_import_plans(zip_path, persona="asa")

    assert result["ok"] is True
    assert result["plan_count"] == 1
    assert result["plans"][0]["source_path"] == "data/tweets.js"
