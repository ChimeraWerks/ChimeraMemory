"""Test the live memory file watcher: create, modify, delete propagate to the index."""

import tempfile
import time
from pathlib import Path

from chimera_memory.db import TranscriptDB
from chimera_memory.memory import (
    init_memory_tables,
    full_reindex,
    start_memory_watcher,
    memory_search,
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


def _search_paths(db, query):
    with db.connection() as conn:
        init_memory_tables(conn)
        return [r["relative_path"] for r in memory_search(conn, query)]


def run():
    root = Path(tempfile.mkdtemp(prefix="chimera_watcher_"))
    personas = root / "personas"
    persona_mem = personas / "developer" / "tester" / "memory" / "procedural"
    persona_mem.mkdir(parents=True)
    (root / "shared").mkdir()

    seed = persona_mem / "seed.md"
    seed.write_text(
        "---\ntype: procedural\nimportance: 5\n---\nalphabetical seeding\n",
        encoding="utf-8",
    )

    db = TranscriptDB(root / "test.db")
    with db.connection() as conn:
        init_memory_tables(conn)
        full_reindex(conn, personas, embed=False)

    print("=== INITIAL INDEX ===")
    _check(
        "Seed file indexed by full_reindex",
        any("seed.md" in p for p in _search_paths(db, "alphabetical seeding")),
    )

    observer = start_memory_watcher(db, personas)
    _check("Watcher started", observer is not None)
    if observer is None:
        print(f"\nMemory watcher tests: {passed}/{passed + failed}")
        return

    try:
        print("\n=== FILE CREATION ===")
        new = persona_mem / "new_memory.md"
        new.write_text(
            "---\ntype: procedural\nimportance: 6\n---\nbravo charlie delta marker\n",
            encoding="utf-8",
        )
        time.sleep(2.0)
        _check(
            "New file auto-indexed",
            any("new_memory.md" in p for p in _search_paths(db, "bravo charlie delta")),
        )

        print("\n=== FILE MODIFICATION ===")
        seed.write_text(
            "---\ntype: procedural\nimportance: 7\n---\necho foxtrot golf rewritten\n",
            encoding="utf-8",
        )
        time.sleep(2.0)
        _check(
            "Modification reflected in index",
            any("seed.md" in p for p in _search_paths(db, "echo foxtrot golf")),
        )

        print("\n=== FILE DELETION ===")
        new.unlink()
        time.sleep(2.0)
        _check(
            "Deletion removes file from index",
            not any("new_memory.md" in p for p in _search_paths(db, "bravo charlie delta")),
        )

    finally:
        observer.stop()
        observer.join(timeout=2)

    print(f"\nMemory watcher tests: {passed}/{passed + failed}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
