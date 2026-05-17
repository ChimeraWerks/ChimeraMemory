import sqlite3
from pathlib import Path

from chimera_memory.memory import index_file, init_memory_tables, memory_file_edge_query
from chimera_memory.memory_entities import apply_enhancement_entities
from chimera_memory.memory_file_edge_classifier import (
    CLASSIFIER_VERSION,
    StaticMemoryFileEdgeClassifierClient,
    run_memory_file_edge_classifier_batch,
    sample_memory_file_edge_candidates,
)
from chimera_memory.memory_file_edges import memory_file_edge_upsert


def _write_memory(path: Path, about: str, body: str) -> None:
    path.write_text(
        "\n".join(["---", "type: procedural", f"about: {about}", "---", body]),
        encoding="utf-8",
    )


def _index_classifier_pair(conn: sqlite3.Connection, tmp_path: Path) -> tuple[int, int]:
    first = tmp_path / "spark-grade.md"
    second = tmp_path / "spark-default.md"
    _write_memory(
        first,
        "Spark passes enrichment gate",
        "Spark passed the corrected enrichment-only gate with the fastest passing latency.",
    )
    _write_memory(
        second,
        "Spark default decision",
        "Spark should become the default enrichment provider after slice 6 validation.",
    )
    assert index_file(conn, "asa", "memory/spark-grade.md", first)
    assert index_file(conn, "asa", "memory/spark-default.md", second)
    first_id = conn.execute(
        "SELECT id FROM memory_files WHERE relative_path = ?",
        ("memory/spark-grade.md",),
    ).fetchone()[0]
    second_id = conn.execute(
        "SELECT id FROM memory_files WHERE relative_path = ?",
        ("memory/spark-default.md",),
    ).fetchone()[0]
    for file_id in (first_id, second_id):
        apply_enhancement_entities(
            conn,
            file_id=file_id,
            metadata={
                "entities": [
                    {"name": "Spark", "type": "tool", "confidence": 1.0},
                    {"name": "memory-enhancement", "type": "topic", "confidence": 1.0},
                ]
            },
            source="test",
        )
    return first_id, second_id


def test_edge_classifier_samples_and_skips_non_related_edges(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    first_id, second_id = _index_classifier_pair(conn, tmp_path)

    candidates = sample_memory_file_edge_candidates(conn, persona="asa", min_support=2, limit=10)

    assert len(candidates) == 1
    assert candidates[0]["source_file_id"] == first_id
    assert candidates[0]["target_file_id"] == second_id
    assert candidates[0]["support"] == 2

    memory_file_edge_upsert(
        conn,
        source_file_path=str(first_id),
        target_file_path=str(second_id),
        relation_type="supports",
    )
    assert sample_memory_file_edge_candidates(conn, persona="asa", min_support=2, limit=10) == []


def test_edge_classifier_dry_run_and_insert(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)
    _index_classifier_pair(conn, tmp_path)

    client = StaticMemoryFileEdgeClassifierClient(
        [
            {"worth_classifying": True, "hunch": "supports"},
            {
                "relation": "supports",
                "direction": "A_to_B",
                "confidence": 0.91,
                "rationale": "The grade result supports the default-provider decision.",
                "valid_from": None,
                "valid_until": None,
            },
        ]
    )
    dry = run_memory_file_edge_classifier_batch(
        conn,
        client=client,
        env={"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"},
        persona="asa",
        dry_run=True,
    )

    assert dry["status_counts"] == {"would_insert": 1}
    assert dry["llm_call_count"] == 2
    assert client.invocations[0]["raw_json"] is True
    assert "worth_classifying" in client.invocations[0]["system_prompt"]
    assert memory_file_edge_query(conn) == []

    insert_client = StaticMemoryFileEdgeClassifierClient(
        [
            {"worth_classifying": True, "hunch": "supports"},
            {
                "relation": "supports",
                "direction": "A_to_B",
                "confidence": 0.91,
                "rationale": "The grade result supports the default-provider decision.",
            },
        ]
    )
    inserted = run_memory_file_edge_classifier_batch(
        conn,
        client=insert_client,
        env={"CHIMERA_MEMORY_ENHANCEMENT_PROVIDER_ORDER": "dry_run"},
        persona="asa",
        dry_run=False,
    )

    assert inserted["status_counts"] == {"inserted": 1}
    edges = memory_file_edge_query(conn, relation_type="supports", current_only=False)
    assert len(edges) == 1
    assert edges[0]["classifier_version"] == CLASSIFIER_VERSION
    assert edges[0]["confidence"] == 0.91
