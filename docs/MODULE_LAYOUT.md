# ChimeraMemory Module Layout

Status: current after the Day 58 memory-module split.

This document explains where future work belongs. The goal is to keep `memory.py` small enough to reason about while preserving its public facade for existing callers.

## Public Facade

`chimera_memory/memory.py` remains the compatibility surface. Existing imports such as `from chimera_memory.memory import memory_review_action` should keep working.

Keep `memory.py` focused on:

- file discovery and persona scoping
- indexing curated markdown memories
- search and recall orchestration
- stats, gaps, consolidation, and watcher integration
- re-exporting focused helpers where older callers already import from `memory.py`

Do not add new sidecar, review, audit, or schema logic directly to `memory.py`.

## Focused Modules

### `memory_schema.py`

Owns SQLite DDL, additive migrations, prerequisite checks, and `init_memory_tables`.

Rules:

- Keep migrations additive and idempotent.
- Do not import focused memory behavior modules from schema code.
- Schema changes need focused tests plus full pytest.

### `memory_governance.py`

Owns memory trust metadata:

- provenance statuses
- lifecycle statuses
- review statuses
- sensitivity tiers
- instruction-grade provenance rules
- frontmatter-to-governance parsing

Rules:

- Generated/agent-written metadata starts as evidence, not instruction.
- Instruction-grade use requires user-confirmed or imported provenance.
- Keep these helpers pure where possible.

### `memory_observability.py`

Owns recall and audit visibility:

- `memory_recall_traces`
- `memory_recall_items`
- `memory_audit_events`
- trace query helpers
- audit query helpers
- JSON payload serialization helpers

Rules:

- This module must not import review or enhancement queue modules.
- Audit payloads should be structured and safe. Do not write raw secrets.

### `memory_review.py`

Owns human review workflows:

- pending review query
- review action application
- before/after metadata snapshots
- review audit events

Rules:

- Review actions may call observability audit helpers.
- Review actions should not mutate raw markdown files.
- Keep review state transitions explicit.

### `memory_enhancement.py`

Owns the model-free sidecar contract:

- request shape
- response normalization
- untrusted content wrapping
- schema version constants

Rules:

- No OAuth or model calls here.
- Treat captured content as untrusted input.
- Validate sidecar output before queue completion or writeback.

### `memory_enhancement_queue.py`

Owns enhancement job persistence:

- enqueue
- claim next
- complete/fail/skip
- job serialization

Rules:

- Queue helpers may depend on `memory_frontmatter.py`, `memory_observability.py`, and `memory_enhancement.py`.
- Queue helpers must not import `memory.py`.
- Completing a job does not directly promote generated metadata to instruction-grade.

### `memory_frontmatter.py`

Owns markdown frontmatter parsing shared by indexing and enhancement enqueue.

This tiny module exists to avoid circular imports between `memory.py` and `memory_enhancement_queue.py`.

### `enhancement_worker.py`

Owns the deterministic dry-run worker for Phase 5c.

Rules:

- This is not the real OAuth/model adapter.
- Keep deterministic behavior for tests.
- Real sidecar/model work should land behind explicit provider boundaries.

## Import Direction

Allowed direction:

```text
memory.py facade
  imports focused modules

memory_enhancement_queue.py
  imports memory_frontmatter.py
  imports memory_observability.py
  imports memory_enhancement.py

memory_review.py
  imports memory_observability.py

memory_observability.py
  imports only stdlib

memory_governance.py
  imports only stdlib

memory_schema.py
  imports only stdlib
```

Avoid:

- focused modules importing `memory.py`
- schema importing queue/review/observability behavior
- review and queue importing each other
- model/OAuth code inside the queue module

## Test Map

- Schema: `tests/test_memory_schema_hygiene.py`
- Governance: `tests/test_memory_governance.py`
- Observability: `tests/test_memory_observability.py`
- Review: `tests/test_memory_review.py`
- Sidecar contract: `tests/test_memory_enhancement.py`
- Enhancement queue: `tests/test_memory_enhancement_queue.py`
- Dry-run worker: `tests/test_memory_enhancement_worker.py`

When touching `memory.py`, also run the legacy standalone scripts:

```powershell
python tests/test_persona_scope.py
python tests/test_memory_watcher.py
python tests/test_indexer.py
python tests/test_search.py
python tests/test_parser.py
```
