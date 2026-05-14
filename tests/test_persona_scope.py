"""Test TRANSCRIPT_PERSONA scoping for curated memory indexing."""

import os
import tempfile
import time
from pathlib import Path

from chimera_memory.db import TranscriptDB
from chimera_memory.memory import (
    cleanup_other_personas,
    full_reindex,
    init_memory_tables,
    memory_search,
    start_memory_watcher,
)


passed = 0
failed = 0


def _check(name, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name}")
        failed += 1


def _write_memory(path: Path, marker: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: procedural\nimportance: 5\n---\n" + marker + "\n",
        encoding="utf-8",
    )


def _search(conn, query):
    return [(r["persona"], r["relative_path"]) for r in memory_search(conn, query)]


def _with_persona(persona: str | None):
    old = os.environ.get("TRANSCRIPT_PERSONA")
    if persona is None:
        os.environ.pop("TRANSCRIPT_PERSONA", None)
    else:
        os.environ["TRANSCRIPT_PERSONA"] = persona
    return old


def _restore_persona(old: str | None):
    if old is None:
        os.environ.pop("TRANSCRIPT_PERSONA", None)
    else:
        os.environ["TRANSCRIPT_PERSONA"] = old


def run():
    root = Path(tempfile.mkdtemp(prefix="chimera_scope_"))
    personas = root / "personas"

    asa_file = personas / "developer" / "asa" / "memory" / "procedural" / "asa.md"
    sarah_file = personas / "researcher" / "sarah" / "memory" / "procedural" / "sarah.md"
    shared_file = root / "shared" / "team.md"

    _write_memory(asa_file, "asa private scope marker")
    _write_memory(sarah_file, "sarah private scope marker")
    _write_memory(shared_file, "shared team scope marker")

    print("=== SCOPED FULL REINDEX ===")
    old = _with_persona("asa")
    try:
        db = TranscriptDB(root / "scoped.db")
        with db.connection() as conn:
            init_memory_tables(conn)
            full_reindex(conn, personas, embed=False)

            _check("Asa memory indexed", _search(conn, "asa private scope marker") == [("asa", "memory/procedural/asa.md")])
            _check("Shared memory indexed", _search(conn, "shared team scope marker") == [("shared", "team.md")])
            _check("Sarah memory excluded", _search(conn, "sarah private scope marker") == [])

        print("\n=== CLEANUP OTHER PERSONAS ===")
        mixed_db = TranscriptDB(root / "mixed.db")
        with mixed_db.connection() as conn:
            init_memory_tables(conn)
            _restore_persona(None)
            full_reindex(conn, personas, embed=False)
            old = _with_persona("asa")

            before = _search(conn, "sarah private scope marker")
            counts = cleanup_other_personas(conn, "asa")
            after = _search(conn, "sarah private scope marker")

            _check("Unscoped DB initially contains Sarah", before == [("sarah", "memory/procedural/sarah.md")])
            _check("Cleanup removes Sarah rows", after == [] and counts.get("memory_files", 0) >= 1)

        print("\n=== SCOPED WATCHER ===")
        watcher_db = TranscriptDB(root / "watcher.db")
        with watcher_db.connection() as conn:
            init_memory_tables(conn)
            full_reindex(conn, personas, embed=False)

        observer = start_memory_watcher(watcher_db, personas)
        _check("Watcher started", observer is not None)
        if observer is not None:
            try:
                _write_memory(
                    personas / "researcher" / "sarah" / "memory" / "procedural" / "sarah-new.md",
                    "sarah live watcher marker",
                )
                _write_memory(
                    personas / "developer" / "asa" / "memory" / "procedural" / "asa-new.md",
                    "asa live watcher marker",
                )
                time.sleep(2.0)
                with watcher_db.connection() as conn:
                    init_memory_tables(conn)
                    _check("Watcher indexes scoped persona", _search(conn, "asa live watcher marker") == [("asa", "memory/procedural/asa-new.md")])
                    _check("Watcher ignores other persona", _search(conn, "sarah live watcher marker") == [])
            finally:
                observer.stop()
                observer.join(timeout=2)
    finally:
        _restore_persona(old)

    print(f"\nPersona scope tests: {passed}/{passed + failed}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
