import sqlite3
from pathlib import Path

from chimera_memory.memory import (
    governance_from_frontmatter,
    index_file,
    init_memory_tables,
    memory_query,
)


def test_governance_defaults_keep_existing_user_memories_instruction_grade() -> None:
    governance = governance_from_frontmatter({})

    assert governance["provenance_status"] == "imported"
    assert governance["review_status"] == "confirmed"
    assert governance["sensitivity_tier"] == "standard"
    assert governance["can_use_as_instruction"] == 1
    assert governance["can_use_as_evidence"] == 1
    assert governance["requires_user_confirmation"] == 0


def test_generated_memory_cannot_be_instruction_grade_without_review() -> None:
    governance = governance_from_frontmatter(
        {
            "provenance_status": "generated",
            "can_use_as_instruction": True,
            "confidence": 1.5,
            "sensitivity_tier": "restricted",
        }
    )

    assert governance["provenance_status"] == "generated"
    assert governance["review_status"] == "pending"
    assert governance["can_use_as_instruction"] == 0
    assert governance["can_use_as_evidence"] == 1
    assert governance["requires_user_confirmation"] == 1
    assert governance["confidence"] == 1.0
    assert governance["sensitivity_tier"] == "restricted"


def test_index_file_persists_governance_frontmatter(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    memory_file = tmp_path / "governed.md"
    memory_file.write_text(
        "\n".join(
            [
                "---",
                "type: procedural",
                "importance: 9",
                "provenance_status: user_confirmed",
                "confidence: 0.85",
                "lifecycle_status: active",
                "review_status: confirmed",
                "sensitivity_tier: restricted",
                "can_use_as_instruction: true",
                "can_use_as_evidence: true",
                "requires_user_confirmation: false",
                "---",
                "Governed memory marker",
            ]
        ),
        encoding="utf-8",
    )

    assert index_file(conn, "asa", "governed.md", memory_file)

    row = conn.execute(
        """
        SELECT fm_provenance_status, fm_confidence, fm_lifecycle_status,
               fm_review_status, fm_sensitivity_tier,
               fm_can_use_as_instruction, fm_can_use_as_evidence,
               fm_requires_user_confirmation
        FROM memory_files
        WHERE relative_path = ?
        """,
        ("governed.md",),
    ).fetchone()

    assert row == ("user_confirmed", 0.85, "active", "confirmed", "restricted", 1, 1, 0)

    queried = memory_query(conn, persona="asa", limit=1)
    assert queried[0]["provenance_status"] == "user_confirmed"
    assert queried[0]["confidence"] == 0.85
    assert queried[0]["sensitivity_tier"] == "restricted"
    assert queried[0]["can_use_as_instruction"] is True


def test_default_memory_query_excludes_generated_synthesis(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    init_memory_tables(conn)

    atom = tmp_path / "atom.md"
    atom.write_text(
        "---\ntype: procedural\nimportance: 8\n---\nAtomic source marker\n",
        encoding="utf-8",
    )
    wiki = tmp_path / "wiki.md"
    wiki.write_text(
        "\n".join(
            [
                "---",
                "type: generated_entity_wiki",
                "importance: 8",
                "exclude_from_default_search: true",
                "---",
                "Generated wiki marker",
            ]
        ),
        encoding="utf-8",
    )

    assert index_file(conn, "asa", "atom.md", atom)
    assert index_file(conn, "asa", "wiki.md", wiki)

    default_results = memory_query(conn, persona="asa", limit=10)
    assert [row["relative_path"] for row in default_results] == ["atom.md"]

    with_synthesis = memory_query(conn, persona="asa", limit=10, include_synthesis=True)
    assert {row["relative_path"] for row in with_synthesis} == {"atom.md", "wiki.md"}
    wiki_row = next(row for row in with_synthesis if row["relative_path"] == "wiki.md")
    assert wiki_row["exclude_from_default_search"] is True
