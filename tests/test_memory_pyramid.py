import sqlite3
from pathlib import Path

from chimera_memory.memory import (
    index_file,
    init_memory_tables,
    memory_audit_query,
    memory_pyramid_summary_build,
    memory_pyramid_summary_query,
)


def _write_memory(path: Path, frontmatter: list[str], body: str) -> None:
    path.write_text(
        "\n".join(["---", *frontmatter, "---", body]),
        encoding="utf-8",
    )


def test_pyramid_summary_builds_chunk_section_document_levels(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    memory_file = tmp_path / "imported-chat.md"
    body = "\n\n".join(
        [
            "Charles asked whether OB should become the core memory engine. Asa argued CM should stay local-first.",
            "Sarah compared OB1 governance with CM retrieval and agreed the lift should stay additive.",
            "The team selected recall traces, governance fields, review queues, and entity graph work first.",
            "Later work needs pyramid summaries so imported conversations can be recalled at multiple resolutions.",
        ]
    )
    _write_memory(
        memory_file,
        ["type: episodic", "importance: 7", "about: OB1 CM planning import"],
        body,
    )
    assert index_file(conn, "asa", "memory/episodes/imported-chat.md", memory_file)

    result = memory_pyramid_summary_build(
        conn,
        file_path="memory/episodes/imported-chat.md",
        chunk_chars=220,
        section_size=2,
        max_summary_chars=180,
        actor="test",
    )

    assert result["ok"] is True
    assert result["built"] is True
    assert result["counts"]["chunk"] >= 2
    assert result["counts"]["section"] >= 1
    assert result["counts"]["document"] == 1
    document = memory_pyramid_summary_query(
        conn,
        file_path="memory/episodes/imported-chat.md",
        level_name="document",
    )
    assert len(document) == 1
    assert "OB" in document[0]["summary_text"]
    assert document[0]["file"]["relative_path"] == "memory/episodes/imported-chat.md"
    events = memory_audit_query(conn, event_type="memory_pyramid_summary_built", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["counts"]["document"] == 1


def test_pyramid_summary_build_is_idempotent_for_current_hash(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    memory_file = tmp_path / "long-note.md"
    _write_memory(
        memory_file,
        ["type: semantic", "importance: 5", "about: import note"],
        "First chunk explains the import plan.\n\nSecond chunk explains the review plan.",
    )
    assert index_file(conn, "asa", "memory/long-note.md", memory_file)

    first = memory_pyramid_summary_build(conn, file_path="memory/long-note.md", chunk_chars=200)
    second = memory_pyramid_summary_build(conn, file_path="memory/long-note.md", chunk_chars=200)

    assert first["ok"] is True
    assert first["built"] is True
    assert second["ok"] is True
    assert second["built"] is False
    assert second["counts"] == first["counts"]
    assert len(memory_pyramid_summary_query(conn, file_path="memory/long-note.md")) == first["counts"]["total"]


def test_pyramid_summary_query_filters_by_persona_level_and_search(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    asa_file = tmp_path / "asa.md"
    sarah_file = tmp_path / "sarah.md"
    _write_memory(
        asa_file,
        ["type: episodic", "importance: 6", "about: pyramid import"],
        "Pyramid summaries help imported conversations keep document-level recall.",
    )
    _write_memory(
        sarah_file,
        ["type: episodic", "importance: 6", "about: dashboard import"],
        "Dashboard work made recall traces visible.",
    )
    assert index_file(conn, "asa", "memory/asa.md", asa_file)
    assert index_file(conn, "sarah", "memory/sarah.md", sarah_file)
    assert memory_pyramid_summary_build(conn, file_path="memory/asa.md")["ok"] is True
    assert memory_pyramid_summary_build(conn, file_path="memory/sarah.md")["ok"] is True

    results = memory_pyramid_summary_query(
        conn,
        persona="asa",
        level_name="document",
        search="pyramid",
    )

    assert len(results) == 1
    assert results[0]["file"]["persona"] == "asa"
    assert results[0]["level_name"] == "document"
