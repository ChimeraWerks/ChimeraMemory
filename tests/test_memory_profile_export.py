import json
import sqlite3
from pathlib import Path

from chimera_memory.memory import (
    index_file,
    init_memory_tables,
    memory_audit_query,
    memory_profile_export,
)


def _write_memory(path: Path, frontmatter: list[str], body: str) -> None:
    path.write_text("\n".join(["---", *frontmatter, "---", body]), encoding="utf-8")


def test_profile_export_preview_uses_reviewed_memory_only(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    confirmed = tmp_path / "confirmed.md"
    evidence = tmp_path / "evidence.md"
    pending = tmp_path / "pending.md"
    restricted = tmp_path / "restricted.md"
    _write_memory(
        confirmed,
        [
            "type: procedural",
            "importance: 9",
            "about: explicit sync procedure",
            "provenance_status: user_confirmed",
        ],
        "Use explicit sync when shipping faster than the scheduled cadence.",
    )
    _write_memory(
        evidence,
        [
            "type: reflection",
            "importance: 7",
            "about: OB lift posture",
            "provenance_status: generated",
            "review_status: evidence_only",
            "can_use_as_instruction: false",
        ],
        "Lift OB patterns additively and keep CM local-first.",
    )
    _write_memory(
        pending,
        [
            "type: semantic",
            "importance: 8",
            "about: pending generated claim",
            "provenance_status: generated",
        ],
        "This pending generated claim must not leave the review queue.",
    )
    _write_memory(
        restricted,
        [
            "type: social",
            "importance: 8",
            "about: restricted social context",
            "provenance_status: user_confirmed",
            "sensitivity_tier: restricted",
            "review_status: restricted",
        ],
        "Restricted context stays out by default.",
    )
    assert index_file(conn, "asa", "memory/procedural/sync.md", confirmed)
    assert index_file(conn, "asa", "memory/reflections/ob-lift.md", evidence)
    assert index_file(conn, "asa", "memory/semantic/pending.md", pending)
    assert index_file(conn, "asa", "memory/social/restricted.md", restricted)

    result = memory_profile_export(conn, persona="asa", write=False)

    assert result["ok"] is True
    selected = {row["relative_path"] for row in result["records"]}
    assert selected == {"memory/procedural/sync.md", "memory/reflections/ob-lift.md"}
    assert "explicit sync procedure" in result["artifacts"]["USER.md"]
    assert "OB lift posture" in result["artifacts"]["SOUL.md"]
    assert "pending generated claim" not in result["artifacts"]["memory-profile.json"]
    assert "Restricted context" not in result["artifacts"]["memory-profile.json"]
    events = memory_audit_query(conn, event_type="memory_profile_export_planned", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["selected_count"] == 2


def test_profile_export_write_creates_portable_artifacts(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    memory_file = tmp_path / "persona.md"
    _write_memory(
        memory_file,
        [
            "type: semantic",
            "importance: 8",
            "about: Charles wants local-first memory",
            "provenance_status: user_confirmed",
        ],
        "Charles wants CM to stay local-first and use OB patterns as additive lifts.",
    )
    assert index_file(conn, "asa", "memory/semantic/local-first.md", memory_file)

    output_dir = tmp_path / "exports" / "context-profile"
    result = memory_profile_export(conn, output_dir=output_dir, persona="asa", write=True)

    assert result["ok"] is True
    assert result["written"] is True
    assert (output_dir / "USER.md").exists()
    assert (output_dir / "SOUL.md").exists()
    assert (output_dir / "HEARTBEAT.md").exists()
    profile = json.loads((output_dir / "memory-profile.json").read_text(encoding="utf-8"))
    assert profile["schema_version"] == "chimera-memory.profile-export.v1"
    assert profile["counts"]["selected"] == 1
    assert profile["records"][0]["relative_path"] == "memory/semantic/local-first.md"
    assert "path" not in profile["records"][0]
    events = memory_audit_query(conn, event_type="memory_profile_export_written", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["written_files"]


def test_profile_export_can_include_restricted_when_explicit(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    restricted = tmp_path / "restricted.md"
    _write_memory(
        restricted,
        [
            "type: social",
            "importance: 8",
            "about: restricted social context",
            "provenance_status: user_confirmed",
            "sensitivity_tier: restricted",
            "review_status: restricted",
        ],
        "Restricted context is included only by explicit request.",
    )
    assert index_file(conn, "asa", "memory/social/restricted.md", restricted)

    default = memory_profile_export(conn, persona="asa", write=False)
    included = memory_profile_export(conn, persona="asa", include_restricted=True, write=False)

    assert default["summary"]["selected_count"] == 0
    assert included["summary"]["selected_count"] == 1
    assert "restricted social context" in included["artifacts"]["SOUL.md"]
