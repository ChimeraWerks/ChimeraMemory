# ChimeraMemory Repository Contract

This file is repo-level guidance for coding agents working in ChimeraMemory. Keep `AGENTS.md` and `CLAUDE.md` synchronized when this guidance changes.

## What This Repo Is

ChimeraMemory indexes Claude Code, Codex, and Hermes session transcripts into queryable SQLite. It is a lightweight standalone library plus an MCP server for agent integration.

Two consumers matter:

- **Standalone CM:** any project that installs `chimera-memory` directly.
- **PersonifyAgents vendor copy:** PA mirrors this repo under `../PersonifyAgents/vendor/chimera-memory/` so PA can install CM into target Python environments.

## Core Architecture Rules

- CM stays local-first: SQLite, markdown plus YAML frontmatter, local fastembed/BGE embeddings, MCP stdio by default.
- Do not add Supabase, Postgres, pgvector, or cloud-LLM requirements to the default path.
- Do not replace CM's retrieval core without empirical receipts. The current core is FTS5 plus vector search with Reciprocal Rank Fusion and re-ranking.
- Persona scoping is a privacy boundary. Do not add code paths that ignore `TRANSCRIPT_PERSONA` or cross-persona folder rules.
- Default-additive, replace-only-with-receipts: add sidecars, traces, governance fields, review queues, and optional adapters. Replacements require measured proof.
- Agent-generated memory metadata starts as evidence, not instruction. Generated write paths must use generated/pending/evidence-only defaults until human review confirms them.

## Current OB1 Lift Status

The OB1-inspired lift is implemented through Phase 5e dashboard and auto-capture plus a first Phase 6 entity-graph slice:

- Phase 0: SQLite hygiene, content fingerprinting, idempotency, partial indexes, Codex commands, comparison docs.
- Phase 1: memory-enhancement sidecar spec.
- Phase 2: recall trace, recall item, and audit-event tables/tools.
- Phase 3: provenance, confidence, lifecycle, review, sensitivity, and use-policy fields.
- Phase 4: review queue tools.
- Phase 5a-c: sidecar contract, enhancement job queue, deterministic dry-run worker.
- Phase 5d groundwork: provider priority, credential-reference boundary, budget caps, safe invocation envelope, bounded failure categories, and injected-client runner boundary.
- Phase 5e usability: PWA memory dashboard, session-close auto-capture protocol, and live-retrieval dry-run checks.
- Phase 6 partial: local entity graph schema, frontmatter/enhancement-derived entity indexing, shared-file connection queries, typed entity-edge query/upsert helpers, typed memory-file reasoning edges, temporal sweep helpers, and deterministic pyramid summaries.
- Refactor: `memory.py` split into focused schema, governance, observability, review, enhancement queue, and frontmatter modules.

Pending larger work:

- Phase 5d remaining: real OAuth/model adapter for memory enhancement.
- Phase 6 remaining: classifier integration for edge creation, import pipelines, portable profile export.

See `docs/OB1_COMPARISON.md`, `docs/MEMORY_ENHANCEMENT_SIDECAR.md`, and `docs/MODULE_LAYOUT.md`.

## Module Ownership

Do not use `memory.py` as a dumping ground. It is now the facade/orchestration layer.

- `chimera_memory/memory.py`: public facade, file discovery, indexing, search/recall orchestration, stats, consolidation, watcher integration.
- `chimera_memory/memory_schema.py`: SQLite DDL, additive migrations, prerequisite checks, `init_memory_tables`.
- `chimera_memory/memory_governance.py`: provenance/lifecycle/review/sensitivity constants, frontmatter governance parsing, trust posture helpers.
- `chimera_memory/memory_observability.py`: recall traces, recall items, audit events, query helpers, JSON payload helpers.
- `chimera_memory/memory_live_retrieval.py`: proactive topic-shift recall planning, dry-run suggestion retrieval, and miss/suggestion audit logging.
- `chimera_memory/memory_review.py`: human review queue actions and review audit logging.
- `chimera_memory/memory_auto_capture.py`: session-close capture planning, governed markdown rendering, persona-root resolution, and safe file writing.
- `chimera_memory/memory_entities.py`: local entity graph, entity/file links from frontmatter and enhancement output, shared-file connection queries, typed entity-edge queries/upserts.
- `chimera_memory/memory_file_edges.py`: typed reasoning edges between memory files (`supports`, `contradicts`, `supersedes`, etc.).
- `chimera_memory/memory_pyramid.py`: deterministic multi-resolution summaries for long curated or imported memory files.
- `chimera_memory/memory_enhancement.py`: model-free sidecar request/response contract and untrusted-content wrapper.
- `chimera_memory/memory_enhancement_provider.py`: provider priority, credential references, budget policy, safe invocation envelope, bounded failure categories.
- `chimera_memory/memory_enhancement_runner.py`: provider-aware batch runner using an injected client protocol. No token storage or provider-specific network code.
- `chimera_memory/memory_enhancement_queue.py`: SQLite queue for enhancement jobs, enqueue/claim/complete helpers.
- `chimera_memory/memory_frontmatter.py`: markdown frontmatter parsing shared by indexing and enhancement enqueue.
- `chimera_memory/enhancement_worker.py`: deterministic dry-run worker. No OAuth/model calls here yet.

Dependency direction matters:

- Schema imports nothing from the other memory modules.
- Governance imports no queue/review/observability modules.
- Observability imports no review or queue modules.
- Live retrieval may depend on observability and sanitizer helpers, but must not inject results into prompts by itself.
- Review may depend on governance concepts and observability audit emission.
- Auto-capture may depend on sanitizer helpers, but must not import the `memory.py` facade.
- Entity graph may depend on observability audit emission.
- Memory-file edge helpers may depend on observability audit emission.
- Pyramid summary helpers may depend on frontmatter parsing, sanitizer helpers, and observability audit emission.
- Enhancement provider policy may depend on the sidecar contract only.
- Enhancement runner may depend on provider policy and enhancement queue helpers.
- Enhancement queue may depend on frontmatter, observability, entity graph helpers, and the sidecar contract.
- Avoid imports from `memory.py` inside focused modules. Importing the facade from a focused module risks a circular import.

## Dual-Source Rule

This repo is the source of truth. Edits land here first, then mirror into PersonifyAgents.

Workflow when changing CM:

1. Edit, test, commit, and push in this repo.
2. From `../PersonifyAgents`: run `python scripts/sync-chimera-memory.py`.
3. Stage `vendor/chimera-memory/` and commit in PA as `vendor: sync CM <sha>`.
4. Run PA vendor tests plus PA runtime/PWA tests.
5. Push PA and verify CI. Live PA receipts matter when the vendor change affects runtime behavior.

PA CI checks the recorded vendor hash. If you forget to sync PA, PA will fail later.

## Editing Rules

- Keep new behavior additive unless Charles explicitly greenlights a replacement.
- Prefer focused modules over growing `memory.py`.
- Keep schema migrations additive and idempotent.
- Keep generated memory metadata reviewable. Do not silently promote generated metadata to instruction-grade.
- Never commit runtime DBs, session transcripts, tokens, `.env`, secrets, or local auth files.
- Runtime DBs live under `~/.chimera-memory/`, not this repo.
- If adding an env var or public config key, document it in `README.md` or the relevant docs file.
- If adding a public function that is re-exported through `memory.py` or package `__init__`, update the facade/re-export and tests.

## Validation Checklist

Before calling work complete:

- `python -m py_compile` for touched modules when refactoring imports.
- Focused pytest for the touched area.
- Full `python -m pytest`.
- Legacy standalone scripts when touching indexing/search/parser/memory core:
  - `python tests/test_persona_scope.py`
  - `python tests/test_memory_watcher.py`
  - `python tests/test_indexer.py`
  - `python tests/test_search.py`
  - `python tests/test_parser.py`
- `git diff` shows only intended changes.
- If `../PersonifyAgents` exists, sync PA vendor copy and verify PA tests/CI.

## Useful References

- `README.md`: tool reference, config docs, architecture overview.
- `docs/OB1_COMPARISON.md`: OB1 feature comparison and lift plan.
- `docs/MEMORY_ENHANCEMENT_SIDECAR.md`: sidecar contract and threat model.
- `docs/MODULE_LAYOUT.md`: module ownership and import boundaries.
- `pyproject.toml`: dependencies.
- `chimera_memory/identity.py`: persona identity and env-driven scoping.
- `chimera_memory/paths.py`: per-persona DB path helpers.
- `chimera_memory/db_split.py`: migration tool for splitting shared DBs into per-persona DBs.
