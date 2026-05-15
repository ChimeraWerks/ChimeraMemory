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

### `memory_entities.py`

Owns the local entity graph lifted from OB1:

- `memory_entities`
- `memory_file_entities`
- `memory_entity_edges`
- entity normalization and deduplication
- frontmatter-derived entity indexing
- enhancement-derived entity linking
- shared-file connection queries
- explicit entity-edge queries
- typed entity-edge upserts

Rules:

- Entity indexing is additive. Do not replace `memory_gaps`.
- Entity helpers may call observability audit helpers.
- Entity helpers must not import `memory.py`.
- LLM extraction can populate these tables later, but this module must keep a local frontmatter-only path.

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

### `memory_enhancement_provider.py`

Owns provider policy for future memory-enhancement sidecar calls:

- provider priority order
- credential-reference validation
- model defaults
- budget caps
- safe invocation envelope
- bounded failure categories
- safe provider receipts

Rules:

- No network calls here.
- No raw OAuth token or bearer token values here.
- Credential references are names such as `oauth:openai-memory`, not credentials.
- This module may import the sidecar contract, but not queue/review/schema/facade modules.

### `memory_enhancement_runner.py`

Owns the provider-aware batch runner boundary:

- claims pending jobs
- builds provider invocation envelopes
- calls an injected `MemoryEnhancementClient`
- completes jobs with normalized metadata
- records failures as bounded categories only

Rules:

- No provider-specific SDK code here.
- No raw OAuth token resolution here.
- Host applications can inject a client that knows how to resolve scoped credentials.
- Failure storage must use categories, not raw provider stderr or exception text.

### `memory_enhancement_queue.py`

Owns enhancement job persistence:

- enqueue
- claim next
- complete/fail/skip
- job serialization

Rules:

- Queue helpers may depend on `memory_frontmatter.py`, `memory_observability.py`, `memory_entities.py`, and `memory_enhancement.py`.
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
  imports memory_entities.py
  imports memory_enhancement.py

memory_enhancement_provider.py
  imports memory_enhancement.py

memory_enhancement_runner.py
  imports memory_enhancement_provider.py
  imports memory_enhancement_queue.py

memory_review.py
  imports memory_observability.py

memory_entities.py
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
- raw credential values in provider policy or safe receipts
- raw provider exception text in queue failure storage

## Test Map

- Schema: `tests/test_memory_schema_hygiene.py`
- Governance: `tests/test_memory_governance.py`
- Observability: `tests/test_memory_observability.py`
- Review: `tests/test_memory_review.py`
- Entities: `tests/test_memory_entities.py`
- Sidecar contract: `tests/test_memory_enhancement.py`
- Provider policy: `tests/test_memory_enhancement_provider.py`
- Provider runner: `tests/test_memory_enhancement_runner.py`
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
