import json
import sqlite3
from pathlib import Path

import pytest

from chimera_memory.memory import index_file, init_memory_tables
from chimera_memory.memory_entities import (
    link_memory_file_entity,
    upsert_memory_entity,
    upsert_memory_entity_edge,
)
from chimera_memory.memory_file_edges import memory_file_edge_upsert
from chimera_memory.memory_entity_wiki import (
    memory_entity_wiki_batch,
    memory_entity_wiki_generate,
)


class FakeWikiClient:
    def __init__(self) -> None:
        self.invocations = []

    def invoke(self, invocation):
        self.invocations.append(invocation)
        return {"markdown": "# Hermes\n\n## Summary\nHermes is grounded in [file:1]."}


def _indexed_file(conn: sqlite3.Connection, tmp_path: Path, name: str, body: str) -> int:
    path = tmp_path / name
    path.write_text(
        f"---\ntype: procedural\nimportance: 8\nabout: {name}\n---\n{body}\n",
        encoding="utf-8",
    )
    assert index_file(conn, "asa", name, path)
    return conn.execute("SELECT id FROM memory_files WHERE relative_path = ?", (name,)).fetchone()[0]


def _fixture(conn: sqlite3.Connection, tmp_path: Path) -> dict:
    hermes_file = _indexed_file(conn, tmp_path, "hermes.md", "Hermes OAuth is the acceptance reference.")
    oauth_file = _indexed_file(conn, tmp_path, "oauth.md", "OAuth token storage must match Hermes.")
    unrelated_file = _indexed_file(conn, tmp_path, "unrelated.md", "Nothing useful here.")
    hermes = upsert_memory_entity(conn, entity_type="project", canonical_name="Hermes")
    oauth = upsert_memory_entity(conn, entity_type="topic", canonical_name="OAuth")
    charles = upsert_memory_entity(conn, entity_type="person", canonical_name="Charles")
    link_memory_file_entity(conn, file_id=hermes_file, entity_row_id=hermes["id"], mention_role="subject")
    link_memory_file_entity(conn, file_id=oauth_file, entity_row_id=hermes["id"], mention_role="mentioned")
    link_memory_file_entity(conn, file_id=unrelated_file, entity_row_id=oauth["id"], mention_role="subject")
    upsert_memory_entity_edge(
        conn,
        source_entity_id=hermes["id"],
        target_entity_id=oauth["id"],
        relation_type="uses",
        confidence=0.9,
    )
    upsert_memory_entity_edge(
        conn,
        source_entity_id=hermes["id"],
        target_entity_id=charles["id"],
        relation_type="co_occurs_with",
        confidence=0.9,
    )
    return {"hermes": hermes, "oauth": oauth}


def test_entity_wiki_dry_run_resolves_entity_without_llm_call(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    fixture = _fixture(conn, tmp_path)
    client = FakeWikiClient()

    result = memory_entity_wiki_generate(
        conn,
        entity_id=fixture["hermes"]["id"],
        dry_run=True,
        client=client,
        env={"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"},
    )

    assert result["ok"] is True
    assert result["status"] == "dry_run"
    assert result["linked_file_count"] == 2
    assert result["typed_edge_count"] == 1
    assert client.invocations == []


def test_entity_wiki_file_mode_writes_generated_cached_view(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    fixture = _fixture(conn, tmp_path)
    client = FakeWikiClient()

    result = memory_entity_wiki_generate(
        conn,
        entity_name="Hermes",
        entity_type="project",
        output_dir=str(tmp_path / "wikis"),
        client=client,
        env={"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"},
    )

    assert result["ok"] is True
    assert result["output_mode"] == "file"
    path = Path(result["path"])
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "type: generated_entity_wiki" in content
    assert "exclude_from_default_search: true" in content
    assert "# Hermes" in content
    prompt = client.invocations[0]["user_prompt"]
    assert "uses" in prompt
    assert "co_occurs_with" not in prompt
    assert str(fixture["hermes"]["id"]) in content


def test_entity_wiki_file_mode_repairs_existing_index_exclusion(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    fixture = _fixture(conn, tmp_path)
    client = FakeWikiClient()
    output_dir = tmp_path / "wikis"

    first = memory_entity_wiki_generate(
        conn,
        entity_id=fixture["hermes"]["id"],
        output_dir=str(output_dir),
        client=client,
        env={"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"},
    )
    wiki_path = Path(first["path"])
    assert index_file(conn, "asa", "wikis/hermes.md", wiki_path)
    conn.execute(
        "UPDATE memory_files SET fm_exclude_from_default_search = 0 WHERE path = ?",
        (str(wiki_path.resolve()),),
    )
    conn.commit()

    second = memory_entity_wiki_generate(
        conn,
        entity_id=fixture["hermes"]["id"],
        output_dir=str(output_dir),
        client=client,
        env={"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"},
    )

    assert second["ok"] is True
    assert conn.execute(
        "SELECT fm_exclude_from_default_search FROM memory_files WHERE relative_path = ?",
        ("wikis/hermes.md",),
    ).fetchone()[0] == 1


def test_entity_wiki_prompt_includes_file_edges_between_linked_files(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    fixture = _fixture(conn, tmp_path)
    memory_file_edge_upsert(
        conn,
        source_file_path="hermes.md",
        target_file_path="oauth.md",
        relation_type="supports",
        confidence=0.85,
        classifier_version="test",
        evidence="Hermes OAuth supports the storage rule.",
    )
    client = FakeWikiClient()

    result = memory_entity_wiki_generate(
        conn,
        entity_id=fixture["hermes"]["id"],
        output_dir=str(tmp_path / "wikis"),
        client=client,
        env={"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"},
    )

    assert result["ok"] is True
    assert result["file_edge_count"] == 1
    prompt = client.invocations[0]["user_prompt"]
    assert "file_edges" in prompt
    assert "Hermes OAuth supports the storage rule." in prompt
    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "exclude_from_default_search: true" in content


def test_entity_wiki_entity_metadata_mode_updates_entity_json(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    fixture = _fixture(conn, tmp_path)

    result = memory_entity_wiki_generate(
        conn,
        entity_id=fixture["hermes"]["id"],
        output_mode="entity-metadata",
        client=FakeWikiClient(),
        env={"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"},
    )

    assert result["ok"] is True
    metadata_text = conn.execute(
        "SELECT metadata FROM memory_entities WHERE id = ?",
        (fixture["hermes"]["id"],),
    ).fetchone()[0]
    metadata = json.loads(metadata_text)
    assert metadata["wiki_page"]["schema_version"] == "chimera-memory.entity-wiki.v1"
    assert metadata["wiki_page"]["exclude_from_default_search"] is True
    assert metadata["wiki_page"]["linked_file_count"] == 2


def test_entity_wiki_thought_mode_is_deferred_until_search_filter_exists(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    fixture = _fixture(conn, tmp_path)

    with pytest.raises(ValueError, match="thought output mode is deferred"):
        memory_entity_wiki_generate(
            conn,
            entity_id=fixture["hermes"]["id"],
            output_mode="thought",
            client=FakeWikiClient(),
        )


def test_entity_wiki_batch_uses_min_linked_threshold(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _fixture(conn, tmp_path)
    client = FakeWikiClient()

    result = memory_entity_wiki_batch(
        conn,
        min_linked=2,
        limit=5,
        output_dir=str(tmp_path / "wikis"),
        client=client,
        env={"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"},
    )

    assert result["ok"] is True
    assert result["candidate_count"] == 1
    assert result["status_counts"] == {"generated": 1}
    assert len(client.invocations) == 1
