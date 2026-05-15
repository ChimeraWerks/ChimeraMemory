import sqlite3
from pathlib import Path

from chimera_memory.memory import (
    index_file,
    init_memory_tables,
    memory_audit_query,
    memory_entity_connections,
    memory_entity_edge_query,
    memory_entity_index,
    memory_entity_query,
    memory_file_entity_links,
    upsert_memory_entity,
    upsert_memory_entity_edge,
)


def _write_memory(path: Path, frontmatter: list[str], body: str) -> None:
    path.write_text(
        "\n".join(["---", *frontmatter, "---", body]),
        encoding="utf-8",
    )


def test_entity_index_derives_people_topics_projects_and_tools(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    first = tmp_path / "charles-memory.md"
    _write_memory(
        first,
        [
            "type: entity",
            "importance: 8",
            "entity: Charles",
            "tags:",
            "  - memory",
            "  - project:PA",
            "  - tool:Codex",
        ],
        "Charles is shaping PA memory work.",
    )
    second = tmp_path / "sarah-memory.md"
    _write_memory(
        second,
        [
            "type: reflection",
            "importance: 6",
            "entity: Sarah",
            "tags:",
            "  - memory",
            "  - tool:Codex",
        ],
        "Sarah reviewed the memory work.",
    )
    assert index_file(conn, "asa", "entities/charles-memory.md", first)
    assert index_file(conn, "asa", "reflections/sarah-memory.md", second)

    result = memory_entity_index(conn, persona="asa")

    assert result["file_count"] == 2
    assert result["link_count"] == 7
    assert result["entity_count"] == 5

    people = memory_entity_query(conn, entity_type="person", persona="asa")
    assert [row["canonical_name"] for row in people] == ["Charles", "Sarah"]

    memory_topic = memory_entity_query(conn, query="memory", entity_type="topic", persona="asa")
    assert len(memory_topic) == 1
    assert memory_topic[0]["file_count"] == 2

    links = memory_file_entity_links(conn, file_path="charles-memory.md")
    assert {link["canonical_name"] for link in links} == {"Charles", "memory", "PA", "Codex"}

    events = memory_audit_query(conn, event_type="memory_entities_indexed", persona="asa")
    assert len(events) == 1
    assert events[0]["payload"]["file_count"] == 2


def test_entity_connections_use_shared_file_evidence(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    memory_file = tmp_path / "pa.md"
    _write_memory(
        memory_file,
        [
            "type: procedural",
            "importance: 7",
            "entity: Charles",
            "tags:",
            "  - project:PA",
            "  - tool:Codex",
        ],
        "PA work connects Charles and Codex.",
    )
    assert index_file(conn, "asa", "pa.md", memory_file)
    memory_entity_index(conn)

    connections = memory_entity_connections(conn, entity_name="Charles", entity_type="person")

    assert {row["canonical_name"] for row in connections} == {"PA", "Codex"}
    for row in connections:
        assert row["overlap_count"] == 1
        assert row["evidence_paths"] == ["pa.md"]


def test_typed_entity_edge_upsert_accumulates_support() -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    source = upsert_memory_entity(conn, entity_type="project", canonical_name="PA")
    target = upsert_memory_entity(conn, entity_type="tool", canonical_name="Codex")

    first = upsert_memory_entity_edge(
        conn,
        source_entity_id=source["id"],
        target_entity_id=target["id"],
        relation_type="uses",
        confidence=0.4,
    )
    second = upsert_memory_entity_edge(
        conn,
        source_entity_id=source["id"],
        target_entity_id=target["id"],
        relation_type="uses",
        confidence=0.8,
    )

    assert first["edge_id"] == second["edge_id"]
    assert second["support_count"] == 2
    assert second["confidence"] == 0.8

    edges = memory_entity_edge_query(conn, entity_name="PA", relation_type="uses")

    assert len(edges) == 1
    assert edges[0]["edge_id"] == first["edge_id"]
    assert edges[0]["source"]["canonical_name"] == "PA"
    assert edges[0]["target"]["canonical_name"] == "Codex"
    assert edges[0]["support_count"] == 2
