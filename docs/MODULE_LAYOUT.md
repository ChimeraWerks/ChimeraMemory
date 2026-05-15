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

### `memory_live_retrieval.py`

Owns live-retrieval planning lifted from OB1's live retrieval recipe:

- topic-shift cue extraction
- dry-run proactive recall suggestions
- silent miss behavior
- recall trace and audit logging for tuning

Rules:

- Live retrieval must not inject results into prompts by itself.
- Misses should be logged but quiet to the caller unless explicitly queried.
- Exclude restricted memories by default.
- Keep this local and deterministic unless a future classifier adapter is explicitly added.

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

### `memory_auto_capture.py`

Owns the session-close auto-capture protocol lifted from OB1's auto-capture skill:

- deterministic ACT NOW extraction
- governed markdown rendering
- persona-root resolution
- safe file writing under `memory/episodes/`
- safety scan summaries without raw secret payloads

Rules:

- Auto-captured memories are generated, pending review, evidence-only, and require user confirmation.
- Auto-capture helpers may use sanitizer helpers, but must not import `memory.py`.
- The facade is responsible for indexing written files and writing audit events.

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

### `memory_file_edges.py`

Owns typed reasoning relations between memory files lifted from OB1's `thought_edges` pattern:

- `memory_file_edges`
- relation types such as `supports`, `contradicts`, `evolved_into`, `supersedes`, and `depends_on`
- confidence and support-count accumulation
- current-only query filtering via `valid_until`
- temporal sweep helpers for expiring stale current edges
- typed edge upsert and query helpers

Rules:

- Memory-file edges are additive. Do not replace `memory_gaps` or the entity graph.
- Edge helpers may call observability audit helpers.
- Edge helpers must not import `memory.py`.
- Keep relation types explicit. Do not accept arbitrary free-form relation labels through public tools.

### `memory_pyramid.py`

Owns deterministic multi-resolution summaries for long curated or imported memory files:

- `memory_pyramid_summaries`
- chunk, section, and document summary levels
- idempotent rebuilds keyed by memory-file content hash
- query helpers for current summaries
- audit events for summary builds

Rules:

- Pyramid summaries are additive sidecar rows. Do not modify the source markdown file.
- Summary helpers may call observability audit helpers.
- Summary helpers must not import `memory.py`.
- Keep the default path deterministic and local. LLM summaries can be a later provider-backed enhancement, not the baseline.

### `memory_import_chatgpt.py`

Owns ChatGPT export ingestion scaffolding:

- `conversations.json` loading from a file, directory, or zip export
- conversation flattening
- governed markdown planning
- safe file writing under `memory/imports/chatgpt/`

Rules:

- Imported conversations are imported provenance but pending review and evidence-only by default.
- Import helpers must not import `memory.py`.
- The facade owns indexing written files, building pyramid summaries, and audit completion events.
- Keep parser behavior tolerant. ChatGPT export shape changes over time.

### `memory_import_obsidian.py`

Owns Obsidian vault ingestion scaffolding:

- markdown note loading from a vault directory or zip export
- frontmatter/body parsing for source notes
- governed markdown planning
- safe file writing under `memory/imports/obsidian/`

Rules:

- Imported Obsidian notes are imported provenance but pending review and evidence-only by default.
- Import helpers must not import `memory.py`.
- The facade owns indexing written files, building pyramid summaries, and audit completion events.
- Skip Obsidian internals such as `.obsidian/`.

### `memory_import_gmail.py`

Owns Gmail / Google Takeout mbox ingestion scaffolding:

- mbox loading from a file, directory, or zip export
- email header and body extraction
- governed markdown planning
- safe file writing under `memory/imports/gmail/`

Rules:

- Imported Gmail messages are imported provenance, pending review, restricted, and evidence-only by default.
- Import helpers must not import `memory.py`.
- The facade owns indexing written files, building pyramid summaries, and audit completion events.
- Skip attachments. The baseline importer stores text bodies only.

### `memory_import_perplexity.py`

Owns Perplexity export ingestion scaffolding:

- markdown, text, and tolerant JSON loading from a file, directory, or zip export
- conversation/message JSON flattening
- governed markdown planning
- safe file writing under `memory/imports/perplexity/`

Rules:

- Imported Perplexity documents are imported provenance but pending review and evidence-only by default.
- Import helpers must not import `memory.py`.
- The facade owns indexing written files, building pyramid summaries, and audit completion events.
- Keep parser behavior tolerant. Perplexity export shapes are less stable than Gmail mbox or ChatGPT conversations.json.

### `memory_import_grok.py`

Owns Grok export ingestion scaffolding:

- markdown/text/JSON/JSONL parsing
- conversation/message JSON flattening
- governed markdown planning
- safe file writing under `memory/imports/grok/`

Rules:

- Imported Grok documents are imported provenance but pending review and evidence-only by default.
- Import helpers must not import `memory.py`.
- The facade owns indexing written files, building pyramid summaries, and audit completion events.
- Keep parser behavior tolerant. Grok export shapes are less stable than Gmail mbox or ChatGPT conversations.json.

### `memory_import_twitter.py`

Owns X/Twitter tweet archive ingestion scaffolding:

- `data/tweets.js` / tweet JSON / JSONL parsing
- tweet metadata extraction
- governed markdown planning
- safe file writing under `memory/imports/twitter/`

Rules:

- Imported tweet/status documents are imported provenance but pending review and evidence-only by default.
- Import helpers must not import `memory.py`.
- The facade owns indexing written files, building pyramid summaries, and audit completion events.
- Direct messages are intentionally out of scope for this module. DM imports need a separate restricted importer.

### `memory_import_instagram.py`

Owns Instagram export ingestion scaffolding:

- message thread JSON flattening
- content/post JSON flattening
- governed markdown planning
- safe file writing under `memory/imports/instagram/`

Rules:

- Imported Instagram documents are imported provenance, pending review, restricted, and evidence-only by default.
- Import helpers must not import `memory.py`.
- The facade owns indexing written files, building pyramid summaries, and audit completion events.
- Keep the parser tolerant. Instagram Takeout structure changes between export versions.

### `memory_profile_export.py`

Owns deterministic portable context exports from reviewed memory:

- USER.md / SOUL.md / HEARTBEAT.md rendering
- structured `memory-profile.json` output
- review/use-policy filtering
- audit events for preview and write runs

Rules:

- Profile export is generated output. Do not modify source markdown files.
- Export helpers may read source markdown bodies for sanitized excerpts.
- Export helpers may call observability audit helpers.
- Export helpers must not import `memory.py`.
- Pending, rejected, disputed, and restricted memories stay out by default.

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

memory_live_retrieval.py
  imports memory_observability.py
  imports sanitizer.py

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

memory_auto_capture.py
  imports sanitizer.py

memory_entities.py
  imports memory_observability.py

memory_file_edges.py
  imports memory_observability.py

memory_pyramid.py
  imports memory_frontmatter.py
  imports memory_observability.py
  imports sanitizer.py

memory_import_chatgpt.py
  imports memory_auto_capture.py
  imports memory_observability.py
  imports sanitizer.py

memory_import_obsidian.py
  imports memory_auto_capture.py
  imports memory_frontmatter.py
  imports memory_observability.py
  imports sanitizer.py

memory_import_gmail.py
  imports memory_auto_capture.py
  imports memory_observability.py
  imports sanitizer.py

memory_import_perplexity.py
  imports memory_auto_capture.py
  imports memory_frontmatter.py
  imports memory_observability.py
  imports sanitizer.py

memory_import_grok.py
  imports memory_auto_capture.py
  imports memory_frontmatter.py
  imports memory_observability.py
  imports sanitizer.py

memory_import_twitter.py
  imports memory_auto_capture.py
  imports memory_observability.py
  imports sanitizer.py

memory_import_instagram.py
  imports memory_auto_capture.py
  imports memory_observability.py
  imports sanitizer.py

memory_profile_export.py
  imports memory_frontmatter.py
  imports memory_observability.py
  imports sanitizer.py

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
- Live retrieval: `tests/test_memory_live_retrieval.py`
- Review: `tests/test_memory_review.py`
- Auto-capture: `tests/test_memory_auto_capture.py`
- Entities: `tests/test_memory_entities.py`
- Memory-file edges: `tests/test_memory_file_edges.py`
- Pyramid summaries: `tests/test_memory_pyramid.py`
- ChatGPT import: `tests/test_memory_import_chatgpt.py`
- Obsidian import: `tests/test_memory_import_obsidian.py`
- Gmail import: `tests/test_memory_import_gmail.py`
- Perplexity import: `tests/test_memory_import_perplexity.py`
- Grok import: `tests/test_memory_import_grok.py`
- X/Twitter import: `tests/test_memory_import_twitter.py`
- Instagram import: `tests/test_memory_import_instagram.py`
- Portable profile export: `tests/test_memory_profile_export.py`
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
