# ChimeraMemory Repository Contract

ChimeraMemory indexes Claude Code / Codex / Hermes session transcripts into queryable SQLite. Lightweight standalone library plus an MCP server for agent integration.

This file is repo-level guidance for coding agents working in this repository.

## What This Repo Is

The canonical source of the `chimera_memory` Python package and its CLI / MCP server. Persona-scoped storage, embeddings + RRF semantic search, salience decay, surprise scoring, zone partitioning.

Two consumers:

- **Standalone**: any project that pip-installs `chimera-memory` directly.
- **Bundled inside PersonifyAgents**: PA vendors a copy of this repo under `PA/vendor/chimera-memory/` so the install pipeline can pip-install it into target Python environments.

## Dual-Source Rule

**This repo is the source of truth.** PersonifyAgents at `../PersonifyAgents` (when present) bundles a mirror copy under `vendor/chimera-memory/`. Edits land here first, then mirror into PA via PA's sync script.

**Workflow when changing CM:**

1. Edit + commit here as normal.
2. If `../PersonifyAgents` exists, the post-commit hook will print a reminder banner with the sync command.
3. From PA root: `python scripts/sync-chimera-memory.py`.
4. Stage `vendor/chimera-memory/` and commit in PA: `vendor: sync CM <new-sha>`.

PA's CI verifies its vendor copy matches the recorded sync hash. If you forget to update PA, PA's CI catches it on the next PA build.

## Hooks Setup

This repo ships hooks under `.githooks/`. To enable them once per clone:

```bash
git config core.hooksPath .githooks
```

The post-commit hook prints the PA-sync reminder when `../PersonifyAgents` is detected. It is non-blocking; commits always succeed.

## Editing Rules

- The package layout is stable: `chimera_memory/{cli,memory,parser,server,indexer,embeddings,db,db_split,identity,paths,cognitive,sanitizer,summarizer,search,config}.py`.
- Tests in `tests/` cover identity, persona-scope, parser, indexer, search, db_split, memory_watcher.
- Persona scoping is the default (see `chimera_memory/identity.py` + `paths.py`). Don't add code paths that ignore scope.
- Don't commit `.venv/`, `dist/`, `build/`, or runtime DBs. The `.gitignore` should already cover these.
- Never commit secrets, tokens, or session data. The MCP server reads transcripts; transcripts may contain sensitive content — they belong on disk under `~/.chimera-memory/`, not in this repo.

## Validation Checklist

Before calling work complete:

- `pytest` passes locally.
- New env vars or config keys are documented in README.md.
- If you added a new public function in a module that has a `__init__.py` re-export, update the re-export.
- `git diff` shows only intended changes.
- If `../PersonifyAgents` exists, run PA's sync script and commit there too.

## Useful References

- `README.md` — full tool reference, config docs, and architecture overview.
- `pyproject.toml` — dependencies (watchdog, fastembed, pyyaml; optional mcp).
- `chimera_memory/identity.py` — PersonaIdentity dataclass + env-driven scoping.
- `chimera_memory/paths.py` — per-persona DB path helpers.
- `chimera_memory/db_split.py` — migration tool for splitting shared DBs into per-persona DBs.
