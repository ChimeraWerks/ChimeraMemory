import json
import sys
from pathlib import Path

from chimera_memory.cli import main
from chimera_memory.memory_enhancement_grading import (
    grade_memory_enhancement_records,
    load_action_teachings,
    load_grade_records,
)


def test_typed_grade_passes_stable_canonical_entities_and_core_actions() -> None:
    result = grade_memory_enhancement_records(
        [
            {
                "model_label": "mini",
                "metadata": {
                    "memory_type": "procedural",
                    "sensitivity_tier": "standard",
                    "entities": [
                        {"name": "Charles", "type": "person", "confidence": 0.9},
                        {"name": "Hermes agent", "type": "project", "confidence": 0.9},
                        {"name": "agent/anthropic_adapter.py", "type": "tool", "confidence": 0.9},
                        {"name": "weak", "type": "tool", "confidence": 0.2},
                    ],
                    "topics": ["acceptance testing"],
                    "action_items": [
                        "grep the reference implementation before writing code",
                        "diff live requests/responses against Hermes",
                        "validate wire-level accept/reject behavior",
                    ],
                },
            },
            {
                "model_label": "mini",
                "metadata": {
                    "memory_type": "procedural",
                    "sensitivity_tier": "standard",
                    "people": ["Charles"],
                    "projects": ["Hermes codebase"],
                    "tools": ["anthropic_adapter.py"],
                    "topics": ["acceptance fixture"],
                    "action_items": [
                        "Grep reference install BEFORE writing",
                        "Compare live-call behavior against reference",
                        "Check each wire-level axis independently",
                    ],
                },
            },
        ]
    )

    model = result["models"][0]
    assert result["passing_models"] == ["mini"]
    assert model["gate"]["pass"] is True
    assert model["scores"]["typed_entities"]["pairwise_mean"] == 1.0
    assert model["scores"]["topics"]["pairwise_mean"] == 1.0
    assert model["scores"]["action_items"]["pass"] is True


def test_typed_grade_uses_per_type_entity_score_not_flat_set_inflation() -> None:
    common_people = ["Charles", "Sarah", "Asa", "Brian", "Jon"]
    records = [
        {
            "model_label": "drifty",
            "metadata": {
                "memory_type": "procedural",
                "sensitivity_tier": "standard",
                "people": common_people,
                "projects": ["Hermes"],
                "tools": ["Anthropic adapter"],
                "topics": ["wire level behavior"],
                "action_items": [
                    "Grep reference before writing",
                    "Compare live-call behavior against reference",
                    "Compare individual wire-level axes independently",
                ],
            },
        },
        {
            "model_label": "drifty",
            "metadata": {
                "memory_type": "procedural",
                "sensitivity_tier": "standard",
                "people": common_people,
                "projects": ["OAuth integration"],
                "tools": ["Google adapter"],
                "topics": ["wire level behavior"],
                "action_items": [
                    "Grep reference before writing",
                    "Compare live-call behavior against reference",
                    "Check each wire-level axis independently",
                ],
            },
        },
    ]

    model = grade_memory_enhancement_records(records)["models"][0]

    assert model["scores"]["typed_entities_flat"]["pairwise_mean"] == 0.556
    assert model["scores"]["typed_entities"]["pairwise_mean"] == 0.333
    assert model["gate"]["typed_entity_jaccard_pass"] is False
    assert model["gate"]["pass"] is False


def test_grade_records_loads_jsonl_and_cli_emits_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "runs.jsonl"
    teachings_path = tmp_path / "teachings.yaml"
    teachings_path.write_text(
        "\n".join(
            [
                "teachings:",
                "  - id: grep-before-implementation",
                "    match_patterns:",
                "      - grep",
                "  - id: ar-is-live-call-diff",
                "    match_patterns:",
                "      - live-call",
                "      - live call",
                "  - id: wire-level-axis-independence",
                "    match_patterns:",
                "      - axis",
                "      - wire-level",
            ]
        ),
        encoding="utf-8",
    )
    records = [
        {
            "model_label": "mini",
            "metadata": {
                "memory_type": "procedural",
                "sensitivity_tier": "standard",
                "people": ["Charles"],
                "projects": ["Hermes"],
                "tools": ["grep"],
                "topics": ["acceptance fixture"],
                "action_items": [
                    "Grep reference before writing",
                    "Compare live-call behavior against reference",
                    "Check each wire-level axis independently",
                ],
            },
        },
        {
            "model_label": "mini",
            "metadata": {
                "memory_type": "procedural",
                "sensitivity_tier": "standard",
                "people": ["Charles"],
                "projects": ["Hermes agent"],
                "tools": ["grep"],
                "topics": ["acceptance testing"],
                "action_items": [
                    "Grep reference before writing",
                    "Compare live-call behavior against reference",
                    "Check each wire-level axis independently",
                ],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    assert len(load_grade_records([path])) == 2
    assert [item["id"] for item in load_action_teachings(teachings_path)] == [
        "grep-before-implementation",
        "ar-is-live-call-diff",
        "wire-level-axis-independence",
    ]

    monkeypatch.setattr(
        sys,
        "argv",
        ["chimera-memory", "enhance", "grade-runs", "--input", str(path), "--teachings", str(teachings_path)],
    )
    main()

    output = capsys.readouterr().out
    assert "Models graded: 1" in output
    assert "mini: PASS entity=1.000 topic=1.000 actions=PASS" in output
