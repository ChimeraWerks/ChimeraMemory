import sqlite3
from pathlib import Path

from chimera_memory.memory import (
    apply_enhancement_entities,
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
    assert {link["canonical_name"] for link in links} == {"Charles", "memory", "PersonifyAgents", "Codex"}

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

    assert {row["canonical_name"] for row in connections} == {"PersonifyAgents", "Codex"}
    for row in connections:
        assert row["overlap_count"] == 1
        assert row["evidence_paths"] == ["pa.md"]


def test_entity_index_links_authored_memory_payload_entities(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    memory_file = tmp_path / "writer-is-persona.md"
    _write_memory(
        memory_file,
        [
            "type: procedural",
            "importance: 9",
            "tags:",
            "  - memory-architecture",
            "memory_payload:",
            "  entities:",
            "    people:",
            "      - Charles",
            "      - Sarah",
            "    projects:",
            "      - ChimeraMemory",
            "    topics:",
            "      - structured-writeback",
            "    tools:",
            "      - Codex",
        ],
        "Writer means persona-authored payload, not model output.",
    )
    assert index_file(conn, "sarah", "procedural/writer-is-persona.md", memory_file)

    result = memory_entity_index(conn, persona="sarah")

    assert result["file_count"] == 1
    assert result["link_count"] == 6
    links = memory_file_entity_links(conn, file_path="writer-is-persona.md")
    by_name = {link["canonical_name"]: link for link in links}

    assert by_name["Charles"]["entity_type"] == "person"
    assert by_name["Sarah"]["entity_type"] == "person"
    assert by_name["ChimeraMemory"]["entity_type"] == "project"
    assert by_name["structured-writeback"]["entity_type"] == "topic"
    assert by_name["Codex"]["entity_type"] == "tool"
    assert by_name["Charles"]["source"] == "memory_payload"
    assert by_name["ChimeraMemory"]["mention_role"] == "mentioned"
    assert by_name["ChimeraMemory"]["evidence"] == "memory_payload.entities.projects"


def test_entity_index_canonicalizes_common_legacy_tag_aliases(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    memory_file = tmp_path / "aliases.md"
    _write_memory(
        memory_file,
        [
            "type: procedural",
            "importance: 9",
            "tags:",
            "  - asa",
            "  - ceo",
            "  - chimera-memory",
            "  - pa",
            "  - hermes",
            "  - ceo-feedback",
        ],
        "Legacy tags should not fragment the entity graph.",
    )
    assert index_file(conn, "sarah", "procedural/aliases.md", memory_file)
    memory_entity_index(conn, persona="sarah")

    links = memory_file_entity_links(conn, file_path="aliases.md")
    by_name = {link["canonical_name"]: link for link in links}

    assert by_name["Asa"]["entity_type"] == "person"
    assert by_name["Charles"]["entity_type"] == "person"
    assert by_name["ChimeraMemory"]["entity_type"] == "project"
    assert by_name["Hermes"]["entity_type"] == "project"
    assert by_name["ceo-feedback"]["entity_type"] == "topic"

    assert memory_entity_query(conn, query="ceo", entity_type="person", persona="sarah")[0]["canonical_name"] == "Charles"
    assert memory_entity_query(conn, query="pa", entity_type="project", persona="sarah")[0]["canonical_name"] == "PersonifyAgents"


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


def test_apply_enhancement_entities_links_typed_contract_entities(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    memory_file = tmp_path / "adapter.md"
    _write_memory(
        memory_file,
        ["type: procedural", "importance: 7", "tags: []"],
        "Sarah compared the Anthropic adapter against live-call behavior on May 17.",
    )
    assert index_file(conn, "asa", "adapter.md", memory_file)
    file_id = conn.execute("SELECT id FROM memory_files WHERE relative_path = ?", ("adapter.md",)).fetchone()[0]

    result = apply_enhancement_entities(
        conn,
        file_id=file_id,
        metadata={
            "entities": [
                {"name": "Anthropic adapter", "type": "tool", "confidence": 0.9},
                {"name": "Sarah", "type": "person", "confidence": 0.8},
                {"name": "May 17 2026", "type": "date", "confidence": 0.7},
                {"name": "discard me", "type": "project", "confidence": 0.2},
            ],
            "relationships": [
                {"from": "Sarah", "to": "Anthropic adapter", "relation": "works_on", "confidence": 0.85},
                {"from": "Anthropic adapter", "to": "May 17 2026", "relation": "made_up", "confidence": 0.95},
            ],
            "topics": ["wire-level"],
            "confidence": 0.6,
        },
    )

    assert result == {"link_count": 4, "edge_count": 7}
    links = memory_file_entity_links(conn, file_path="adapter.md")
    by_name = {link["canonical_name"]: link for link in links}

    assert set(by_name) == {"Anthropic adapter", "Sarah", "May 17 2026", "wire-level"}
    assert by_name["Anthropic adapter"]["entity_type"] == "tool"
    assert by_name["Sarah"]["entity_type"] == "person"
    assert by_name["May 17 2026"]["entity_type"] == "date"
    assert by_name["wire-level"]["entity_type"] == "topic"
    assert by_name["wire-level"]["mention_role"] == "tag"
    assert by_name["Anthropic adapter"]["confidence"] == 0.9
    typed_edges = memory_entity_edge_query(conn, entity_name="Sarah", relation_type="works_on")
    assert len(typed_edges) == 1
    assert typed_edges[0]["target"]["canonical_name"] == "Anthropic adapter"
    assert typed_edges[0]["confidence"] == 0.85
