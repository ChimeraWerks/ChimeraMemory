# Legacy Memory Migration Pipeline

## What This Is

The proven Day 61 Slice 5B pipeline for migrating legacy prose memory files (`.md` with YAML frontmatter) into the OB-pattern structured-writeback architecture (authored `memory_payload` + governance fields + indexable structured content) WITHOUT modifying the prose body.

Validated across 46 files (45 Sarah + 1 Asa pilot). 0 DB/frontmatter mismatches, 0 pending review rows after each batch, 24/24 retrieval regression queries passing, 0 synthesis-pollution sentinels triggered.

This document is the runbook for re-running the pipeline against additional files when curation greenlights more migration. NOT for bulk autopilot conveyor; Day 61 established that bulk migration is unsafe past the high-value-procedural envelope.

## When To Use This Pipeline

**Use when:**
- A specific legacy memory file is identified as load-bearing and would benefit from structured indexing (entities/topics/dates queryable as first-class).
- The file body is prose-shaped (procedural rule, decision record, lesson) and the structured payload is a typed projection of that prose.
- Author (the persona owning the file) is available to AR + greenlight the structured projection.
- Backup of the persona's full memory tree exists OR can be created before write.

**Do NOT use when:**
- The file is security/credential-adjacent (auth, OAuth, secret, password, webhook). Manual review only; never autopilot migration.
- The file is high-risk private (intimate persona content, DM-restricted, sensitive context). Curation decision required.
- The file is an entity file (`memory/entities/`). Entities have a different schema; use the entity-update path instead.
- The file is an episode (`memory/episodes/`). Episodes are narrative-shaped, not procedural; structured projection often loses nuance.
- Body content would lose information when projected to structured fields (poetry, voice docs, emotional context).

## Architecture Components (commit refs from Day 61)

| Component | File | Commit |
| --- | --- | --- |
| Option B retrofit writer | `chimera_memory/memory_authored_writeback.py` + `memory_legacy_migration.py` | CM `26859d1` |
| Durable frontmatter review action | `chimera_memory/memory_review.py` + `memory.py` | CM `06f776c` + `0b67b50` |
| Authored memory_payload indexing (FTS + embeddings) | `chimera_memory/memory.py` + `memory_enhancement.py` | CM `f31d595` |
| Source refs first-class indexing | `chimera_memory/memory.py` + `memory_schema.py` | CM `dc13019` |
| Artifacts first-class indexing | `chimera_memory/memory.py` + `memory_schema.py` | CM `102a03a` |
| Entity-wiki reindex-policy-resync fix | `chimera_memory/memory.py` + `memory_entity_wiki.py` | CM `5009165` |
| Read-only legacy migration planner | `chimera_memory/memory_legacy_migration.py` | CM `e731862` |

## Pipeline Steps

### Pre-flight (one-time per arc)

**1. Create full persona memory backup.**
- Backup script captures `personas/<persona>/memory/**/*.md` to timestamped archive.
- Hash-check the backup: every backed-up file's body sha256 matches the source.
- Result: restore-able rollback target if anything goes wrong.

**2. Run the read-only planner (`memory_legacy_migration_plan`).**
- Inventories all `personas/<persona>/memory/**/*.md`.
- Classifies each by: memory_type (procedural/episode/etc), risk level (low/medium/high), suggested migration mode (`skip` / `manual_frontmatter_retrofit` / `llm_draft_then_review` / `companion_preview`), security flags.
- No writes. Output is YAML/JSON plan only.

**3. Curate the queue.**
- Filter planner output to candidates that are (a) procedural or feedback shape, (b) low-to-medium risk, (c) NOT security/credential-adjacent.
- Order by importance (descending) for high-value-first batches.
- Cap initial pass at 10-20 files per Slice 5C envelope.

### Per-file flow

**4. Author drafts structured payload (preview-only).**
- Author = the persona owning the file (e.g., Sarah for `personas/researcher/sarah/`).
- Read the prose body. Project into `memory_payload` shape: `decisions`, `outputs`, `lessons`, `constraints`, `unresolved_questions`, `next_steps`, `failures`, `artifacts`, `entities` (typed: `person`/`project`/`topic`/`tool`/`organization`/`place`/`date`).
- Tag `memory_type` correctly: `procedural` for operational rules, `feedback` for Charles-directives, `episodic`/`episode` for narrative, `reflection` for higher-order patterns.
- Save preview to YAML alongside body-sha256 hash. NO writes to the source file yet.

**5. AR cycle.**
- Author reviews the preview payload OR a co-AR persona reviews.
- Author-greenlight via Discord message naming the file + payload accuracy + body-preservation check.
- Capture the Discord message ID as the `review_notes` field on the eventual write.

**6. Apply retrofit via `memory_legacy_frontmatter_retrofit`.**
- Writer reads source file, verifies body sha256 matches the previewed hash (refuses to write if mismatch).
- Inserts `memory_payload` block + governance frontmatter (`provenance_status`, `lifecycle_status`, `review_status`, `can_use_as_instruction`, `can_use_as_evidence`, `requires_user_confirmation`, `legacy_migration` block with source+target hashes + persona + path + timestamp + migrator).
- Default `review_status: pending` + `can_use_as_instruction: false` until reviewed.
- Body unchanged. Writer asserts body sha256 after-write matches before-write hash.

**7. Apply durable frontmatter review action via `memory_review_action`.**
- Action: `confirm` (when migration is accurate) or `edit` (with notes) or `reject` (revert).
- Writer updates BOTH the SQLite DB row AND the file frontmatter (`review_status`, `payload_review_status`, `review_action`, `reviewed_at`, `reviewed_by`, `review_notes`).
- DB and frontmatter MUST stay in sync. Old DB-only review path is deprecated (replaced by CM `06f776c`).
- On `confirm`: sets `provenance_status: user_confirmed`, `review_status: confirmed`, `can_use_as_instruction: true`.

**8. Indexing.**
- Indexer detects file change via content_hash, re-indexes structured fields (`memory_payload.lessons`, `constraints`, `decisions`, `next_steps`, `entities.*`) into FTS.
- Embeddings re-encoded on structured payload content.
- Entity index updated (entities from `memory_payload.entities.*` are upserted into `memory_entities` table).
- Edge classifier can later run across migrated files to populate `memory_file_edges`.
- Indexer respects `fm_exclude_from_default_search` flag (set on synthesis rows like wiki/dossier outputs but NOT on procedural retrofits).

### Per-batch validation

**9. Run retrieval regression suite.**
- 24-query suite at `personas/researcher/sarah/research/proposals/retrieval-regression-suite-slice5b-day61.md`.
- Each query has expected top-3 results from migrated files.
- `must_not_outrank` sentinels guard against synthesis-pollution (dossier rows leaking into default recall).
- Pass criterion: ≥22 of 24 queries strict-pass + 0 sentinel triggers.
- Baseline established 2026-05-17 on 45-file corpus: 18/18 sampled strict pass after Group A query-shape fix.
- Re-run after EVERY batch. Failures = stop migration + investigate root cause.

**10. Per-file recall spot check.**
- For each migrated file, run a query that should surface it as rank 1.
- Confirms structured-field indexing is producing retrieval value (not just structural correctness).

## Hard Stops

Halt migration immediately if:
- Body sha256 before/after mismatch on any file. Pipeline integrity broken.
- DB row and file frontmatter diverge (`review_status` DB ≠ frontmatter, etc.). Slice 5C-style indexing or review-path bug.
- Regression suite drops below 22/24 strict pass. Retrieval quality regression.
- Any `must_not_outrank` sentinel triggers. Synthesis-pollution leak.
- Security/credential-language detected in candidate body. Move file to manual-review queue, never autopilot.
- Author cannot AR the projection within reasonable time. Don't write without consent.

## Sibling Procedural Memories

- `writer-is-persona-not-model.md` (the meta-rule on writer vs enrichment-provider terminology)
- `synthesis-back-into-store-pollutes-retrieval.md` (mitigation triplet for any future dossier writeback)
- `confidence-not-evidence.md` (verify-before-stating, governs ALL migration AR claims)
- `recalibrate-after-first-error.md` (cautious-mode if anything breaks mid-batch)

## Future Work / Known Limits

- Asa-side migration: only 1 file migrated (pilot). 42 more in queue, mostly high-risk.
- Sarah-side migration: 45 files migrated. 6 medium-risk preview-safe candidates remain + 182 high-risk/private/sensitive.
- Episode files (`memory/episodes/`) explicitly excluded from this pipeline shape. Episodes need a different projection method (narrative-preserving) that hasn't been built.
- Entity files (`memory/entities/`) excluded; they have their own schema.
- Reading-notes (`memory/reading/`) excluded by default; body IS the value, structured projection loses fidelity.

## When To Run Next

- Charles directs a specific narrower target list.
- A new high-value procedural memory is shipped and ready for structured projection (one-at-a-time, real-time author-write skipping the legacy retrofit step entirely).
- Architectural change requires re-indexing or re-migration (rare).
