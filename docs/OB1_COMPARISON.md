# OB1 ↔ ChimeraMemory: Deep-Dive Comparison & Adoption Candidates

> Source: https://github.com/NateBJones-Projects/OB1 (Open Brain) reviewed against the
> current `main` of this repo. Written for ChimeraMemory engineers and AI agents
> deciding which OB1 ideas (if any) to port. Every recommendation includes
> concrete file:line references so a future agent can jump straight to the code
> without re-doing the discovery work.
>
> All file paths under `chimera_memory/` and `tests/` are inside *this* repo.
> All file paths beginning with `OB1:` refer to the OB1 source tree cloned at
> `https://github.com/NateBJones-Projects/OB1`.

---

## TL;DR

OB1 is the **opposite shape** of ChimeraMemory: a Postgres/pgvector/Supabase
cloud-first knowledge graph for a single human's "second brain", with heavy
community contributions (recipes, skills, dashboards). ChimeraMemory is a
**local-first, single-binary, persona-scoped** SQLite/FTS5/fastembed MCP
server purpose-built for indexing agent transcripts.

**They don't compete — they barely overlap.** The interesting question isn't
"should we be more like OB1" but "which OB1 design patterns are worth porting
without giving up our local-first, zero-cloud DNA?"

The seven adoption candidates worth serious consideration:

1. **Content fingerprint + upsert (CM-1)** — small change, huge win for any
   future "write memory" or "write thought" flow.
2. **Agent-memory provenance schema (CM-2)** — codify trust into the schema
   instead of inferring it from frontmatter.
3. **Recall trace + audit log (CM-3)** — observability layer over every
   search/recall so we can debug "why did the agent pick that memory?"
4. **Typed reasoning edges (CM-4)** — `supports`/`contradicts`/`supersedes`
   between memories upgrades our graph from "shared concepts" to "what
   actually disagrees with what."
5. **Schema-versioned MCP contracts (CM-5)** — `chimera_memory.recall.v1` style
   so we can evolve tool shapes without breaking installed agents.
6. **ChatGPT-compatible `search`/`fetch` tool pair (CM-6)** — five-line MCP win
   that lets ChatGPT Custom GPTs and connectors consume ChimeraMemory.
7. **Metadata-driven contribution structure + CI gate (CM-7)** — only relevant
   if we ever open up community recipes.

Everything else (Supabase Edge Functions, pgvector, Slack/Discord capture
bots, web dashboard, multi-table household-tracker extensions, LLM
metadata extraction via OpenRouter) is **explicitly off-strategy** for
ChimeraMemory. Don't adopt those.

---

## 1. The Two Repos Side-by-Side

| Dimension | ChimeraMemory | OB1 (Open Brain) |
|---|---|---|
| Primary data source | Agent transcripts (JSONL from Claude Code / Codex / Hermes) + opt-in curated `.md` files | A single human's typed/captured "thoughts" |
| Storage | Local SQLite + FTS5 + WAL | Supabase Postgres + pgvector (cloud) or self-hosted K8s |
| Embeddings | `bge-small-en-v1.5` via `fastembed` (local, ~80MB, offline) | OpenRouter `text-embedding-3-small` (paid API); Ollama is an opt-in community recipe |
| Search | FTS5 + cosine via RRF + re-rank (`chimera_memory/search.py:1`) | pgvector `<=>` + `to_tsvector`/`websearch_to_tsquery` (`OB1:schemas/enhanced-thoughts/schema.sql:32-133`) |
| Metadata enrichment | None — author writes frontmatter manually | LLM-based via GPT-4o-mini (`OB1:server/index.ts:66-97`) |
| Provenance / governance | Status field, frontmatter `failure_count` only (`chimera_memory/memory.py:36-56`) | First-class: `agent_memories` table with `provenance_status`, `review_status`, `can_use_as_instruction` constraints (`OB1:schemas/agent-memory/schema.sql:22-94`) |
| Cognitive layer | Per-type decay, surprise scoring, CORE/ACTIVE/PASSIVE/ARCHIVE zones (`chimera_memory/cognitive.py:19-260`) | Importance + quality_score columns only; no decay/zones |
| Persona scoping | Mandatory; per-persona DBs and discovery boundary (`chimera_memory/identity.py`, `chimera_memory/paths.py`, `chimera_memory/memory.py:129-212`) | Workspace/project/channel `visibility` scope on `agent_memories` only |
| MCP tools | 21 tools, stdio (`chimera_memory/server.py:179-822`) | 5 base tools (`search`, `fetch`, `search_thoughts`, `list_thoughts`, `thought_stats`, `capture_thought`) + Agent-Memory REST API tools (`OB1:server/index.ts:101-505`) |
| Distribution | `pip install chimera-memory` → single CLI on PATH | Supabase project + edge functions + frontends; AI-assisted setup ~45 min |
| Secret sanitization | Compiled-regex pass at indexing time (`chimera_memory/sanitizer.py:9-148`) | Unsafe-content regex inside agent-memory API only (`OB1:integrations/agent-memory-api/index.ts` ~lines 204-211 per investigation) |
| Graph analysis | `networkx` over shared tags/concepts; finds isolated files (`chimera_memory/memory.py:543-607`) | Two graphs: entity↔entity (`entities`+`edges`) and thought↔thought (`thought_edges`) populated by separate workers |
| Watch / incremental | `watchdog` observer with poll fallback; MD5 hash + last-position tail-read | Postgres triggers + queue table for entity extraction |
| Contribution model | Internal; PA mirrors via `vendor/chimera-memory/` | Open community: extensions/primitives/recipes/skills/schemas/dashboards/integrations + CI gate (`OB1:.github/workflows/ob1-gate.yml`) |
| LLM dependency | **Zero.** All cognitive work is algorithmic. | Multiple LLM-dependent paths (extract metadata, classify edges, extract entities) |

The single most important entry in this table is the last one. **Almost every
"impressive" feature in OB1 is an LLM call wrapped in a Postgres function.**
ChimeraMemory's design rule — "no LLM calls in the memory backend" — would
have to be relaxed to adopt them as-is. The candidates below were chosen
because they're useful *without* breaking that rule.

---

## 2. What Each Repo Does Well (Real Pros)

### ChimeraMemory's strengths (don't lose these when porting)

- **Zero-config, offline-first install.** `pip install -e .` → `chimera-memory serve` and you have an MCP server reading your existing JSONL. No accounts, no API keys, no cloud. (`README.md:35-46`)
- **Transcript indexing is a category of one.** OB1 has nothing like it. Every transcript entry the harness writes gets parsed, classified, sanitized, and indexed without the user lifting a finger. (`chimera_memory/parser.py:1-827`, `chimera_memory/indexer.py:1-313`)
- **The progressive-disclosure recall pattern (`discord_recall_index` → `discord_detail`)** is genuinely better than OB1's "return the whole content" pattern for token economy. 5–10× token savings. (`chimera_memory/server.py:421-512`)
- **The cognitive layer is the differentiator.** Zone-based loading + surprise + decay encode opinions about what a memory *is for*, not just where it lives. OB1 has `importance` and `quality_score` integers and stops there. (`chimera_memory/cognitive.py:185-303`)
- **Persona scoping at the filesystem + DB level.** OB1's `visibility` column is enforced inside the API; CM's persona boundary is enforced by *which DB file is opened* and *which directories are walked*. That's a stronger guarantee. (`chimera_memory/memory.py:129-212`, `tests/test_persona_scope.py`)
- **Bulletproof secret redaction.** The OB1 unsafe-content check rejects writes; CM redacts at index time on every entry, including transcript content the user never typed. (`chimera_memory/sanitizer.py:9-73`)
- **WAL + retry + tail-read + MD5 import log.** All four production-grade SQLite patterns in one place. Re-backfill of an unchanged 55MB corpus completes in ~0.3s. (`chimera_memory/db.py:140-198`, `chimera_memory/indexer.py`)

### OB1's strengths (worth studying even if we don't adopt)

- **Provenance is in the schema, not in a comment.** Every `agent_memories` row has `provenance_status`, `review_status`, `can_use_as_instruction`, `can_use_as_evidence`, `requires_user_confirmation`, `lifecycle_status`. The CHECK constraint at `OB1:schemas/agent-memory/schema.sql:90-94` literally prevents an agent from upgrading its own memory to "instruction" without user confirmation.
- **Idempotency is a first-class concern.** `idempotency_key` and `content_hash` columns with partial unique indexes mean every writer is safe to retry. (`OB1:schemas/agent-memory/schema.sql:85-87, 96-98`)
- **Recall is auditable.** `agent_memory_recall_traces` + `agent_memory_recall_items` record *every* recall request, what was returned, in what rank, and whether the agent ultimately used or ignored each result. That's an evaluation harness baked into the data model. (`OB1:schemas/agent-memory/schema.sql:181-219`)
- **Typed thought-to-thought edges with temporal validity.** `supports / contradicts / evolved_into / supersedes / depends_on / related_to` between thoughts, with `valid_from`, `valid_until`, `decay_weight`. (`OB1:schemas/typed-reasoning-edges/schema.sql:83-101`)
- **Schema-versioned contracts.** OB1's recall/writeback APIs include `schema_version: "openbrain.agent_memory.recall.v1"` so the client and the server can evolve independently. (per investigation of `OB1:integrations/agent-memory-api/index.ts`)
- **ChatGPT compatibility.** OB1 ships `search` and `fetch` tools whose shapes ChatGPT's Custom GPT connectors look for verbatim. Five-line wrapper, huge surface gain. (`OB1:server/index.ts:108-207`)
- **Content fingerprint dedup.** SHA-256 of normalized text + UNIQUE INDEX + `ON CONFLICT DO UPDATE` upsert. Trivial, bulletproof, applicable any time we write something we might re-write. (`OB1:recipes/content-fingerprint-dedup/README.md:30-79`, `OB1:schemas/enhanced-thoughts/schema.sql:309-389`)
- **Contribution metadata + CI gate.** `metadata.json` per contribution + an automated review workflow that checks structure, secrets, SQL safety, dep choices, README quality. (`OB1:.github/metadata.schema.json`, `OB1:.github/workflows/ob1-gate.yml`)

---

## 3. The Real Cons of Each (Honest)

### ChimeraMemory's cons

- **No provenance ladder.** A memory file is "active" or "archived". There's no "I observed this" vs "the user confirmed this" vs "an agent generated this" distinction. Failure marking is one bit. Once we let agents *write* memories (not just read them), this becomes the #1 missing primitive.
- **No idempotency on writes.** `memory_guard` validates content but the actual file write is the agent's responsibility. If the agent retries, we get a duplicate file in `personas/<persona>/memory/`. No content fingerprint anywhere in the stack.
- **No recall observability.** We don't log what `memory_search` / `memory_recall` returned. If a user complains "the agent picked the wrong memory," we have no trace.
- **No typed inter-memory edges.** The graph in `memory_gaps` finds connected components by shared tags. It can't say "this memory contradicts that one" or "this one supersedes that one."
- **No MCP contract version.** Tool shapes can drift silently between CM versions. There's no `schema_version` agents can negotiate.
- **No ChatGPT/Custom-GPT entry point.** Our MCP tools are named for humans (`discord_recall_index`); ChatGPT's connector framework expects literal tools named `search` and `fetch`.
- **`memory_recall` is O(N) in memory.** `chimera_memory/memory.py:483-522` fetches every embedding row and computes cosine in Python. Fine at 10K memories, breaks somewhere around 100K. Same shape in `chimera_memory/cognitive.py:110-154` for surprise. No ANN index, no sqlite-vec.
- **`compute_surprise` is O(N) *per memory*.** `score_all_surprise` is therefore O(N²). At a few thousand memories this is fine; beyond that it's a problem.
- **Hard-coded Windows path in `_ensure_memory_indexed`.** `chimera_memory/server.py:594` ships `C:/Github/ChimeraPersonas/personas` as the default. Functionally fine because it's only the fallback, but it leaks the author's machine layout into the binary.
- **The 21-tool surface is itself a UX cost.** Agents wading through 21 tool descriptions consume tokens even when they pick the right one. OB1's 5-tool surface is leaner; ours could be too with grouping.

### OB1's cons

- **No local-only path.** Even with K8s self-host, you're running Postgres + pgvector + edge-function-style workers. There is no "just install this binary."
- **Mandatory cloud LLM dependency.** Capture without metadata extraction is *possible* via the fallback regex (`OB1:server/index.ts:79-97`) but the "good" path requires OpenRouter, and the entity worker requires it absolutely.
- **No transcript indexing at all.** OB1 has no concept of "the agent already wrote 50,000 messages, let me query them." Every thought is captured manually by the human.
- **The 717-file repo is mostly content, not code.** 38 recipes + 16 skills + 6 extensions + 5 schemas + 8 integrations + 7 primitives. Hard to know what's load-bearing vs. demo-grade.
- **`thoughts` is a single flat table.** Adding `type` / `importance` / `sensitivity_tier` / `quality_score` to one table mixes concerns; the agent-memory schema acknowledges this by sidecarring everything (`OB1:schemas/agent-memory/schema.sql:6-9`).
- **No file-watcher / incremental indexing.** OB1 expects you to capture into the DB. CM watches your filesystem.
- **Some recipe quality is variable.** Community contribs are gated by an LLM reviewer + admin, but inevitably some are demo-grade. Treat anything outside `schemas/`, `server/`, and `integrations/` as worked example, not API surface.
- **CORS-bending shim in `server/index.ts:530-543`** — Claude Desktop doesn't send the right `Accept` header so OB1 rewrites the request. We'd have to do similar for any HTTP transport we add.

---

## 4. Overlap (Where the Repos Agree)

A surprisingly short list:

| Concern | CM mechanism | OB1 mechanism | Comment |
|---|---|---|---|
| Local semantic search | `bge-small-en-v1.5` via fastembed | Ollama via `recipes/local-ollama-embeddings/` | Both work offline; both lossy. We're already there. |
| FTS + vector hybrid | `chimera_memory/search.py` RRF | `OB1:schemas/enhanced-thoughts/schema.sql:32-133` `search_thoughts_text` + `match_thoughts` | OB1 does its merging at the API layer, we do ours via SQL+Python. Equivalent. |
| Persona / workspace scoping | Per-persona SQLite file | `workspace_id` / `project_id` / `visibility` column | CM's mechanism is stronger (filesystem boundary). |
| Importance scoring | `fm_importance` int + decay + zones | `importance` SMALLINT + `quality_score` NUMERIC | CM has decay/zones; OB1 has separate quality vs importance dimensions. Useful distinction. |
| Capture from Discord | `discord_inbound` / `discord_outbound` entry types | `integrations/discord-capture/` bot | Totally different mechanism: CM picks Discord up *because the agent harness logged the JSONL*; OB1 runs its own bot. |
| Failure tracking | `fm_failure_count` per memory | `agent_memory_review_actions` with `reject`/`dispute` | OB1's is auditable; CM's is a counter. |

That's it. The other ~80% of each repo doesn't have a counterpart in the
other.

---

## 5. Adoption Candidates — In Priority Order

Each candidate has: **what**, **why for CM**, **risks**, **scope estimate**,
**where to look in OB1** and **where to land in CM**. Future agents working
from this doc should be able to start each implementation without re-doing
the research above.

---

### CM-1 (HIGH) — Content fingerprint + upsert for memory writes

**What:** Add a `content_fingerprint TEXT` column to `memory_files` (or a
new "agent-written memory" table — see CM-2), backed by a partial UNIQUE
index. On any "write memory" path, compute SHA-256 of `lower(trim(collapse_ws(content)))`
and `INSERT ... ON CONFLICT DO UPDATE` — merging metadata instead of
duplicating.

**Why for CM:**
- We don't have an agent-write path yet, but `memory_guard` (`chimera_memory/server.py:735-745`) is clearly the stub for one. The moment an agent writes back through CM, we will hit duplicate-row problems unless we have this.
- This is the *cheapest* improvement in this whole document. ~30 LOC in `chimera_memory/memory.py` and a one-line schema migration in the `MEMORY_SCHEMA` constant.
- The fingerprint also makes our import_log + tail-read story stronger: we can dedup at the *content* layer, not just the file-offset layer.

**Risks:**
- None real. The OB1 evidence (`OB1:recipes/content-fingerprint-dedup/README.md:158-160`: "tested against 75,000+ thoughts across 9 sources with zero duplicates") is reassuring.
- Watch: SHA-256 of normalized text means whitespace-only edits produce the same fingerprint. For markdown files where whitespace matters, scope the fingerprint to "the body after frontmatter" and decide what merging frontmatter means (we already do this elsewhere via `parse_frontmatter` — `chimera_memory/memory.py:112-124`).

**Scope:** 1–2 hours including tests.

**Where in OB1:**
- Recipe & rationale: `OB1:recipes/content-fingerprint-dedup/README.md:30-115`
- Reference impl as Postgres function: `OB1:schemas/enhanced-thoughts/schema.sql:309-389` (`upsert_thought`)
- Idempotency-key variant for agent memory: `OB1:schemas/agent-memory/schema.sql:85-98`

**Where to land in CM:**
- Add column + index in `MEMORY_SCHEMA` (`chimera_memory/memory.py:35-79`).
- Add `compute_fingerprint(content)` helper next to `parse_frontmatter`.
- New `memory_upsert(conn, persona, relative_path, full_path)` that wraps `index_file` and merges by fingerprint when path is new but content matches.
- Test in `tests/` mirroring the pattern of `tests/test_indexer.py`.

---

### CM-2 (HIGH) — Provenance-and-review schema for agent-written memory

**What:** A new sidecar table — say `agent_memory` — keyed by
`(persona, fingerprint)` with columns roughly modeled on
`OB1:schemas/agent-memory/schema.sql:22-94`:

```
provenance_status     enum: observed | inferred | user_confirmed | imported | generated | superseded | disputed
review_status         enum: pending | confirmed | evidence_only | restricted | rejected | stale | merged
lifecycle_status      enum: active | stale | superseded | disputed | rejected
can_use_as_instruction  bool   CHECK (NOT true OR provenance IN (user_confirmed, imported))
can_use_as_evidence     bool
requires_user_confirmation bool
confidence            real 0..1
runtime_name / runtime_version / model / task_id   text (provenance tail)
```

Plus a small **`agent_memory_review_actions`** table: `action`, `actor_id`,
`actor_label`, `notes`, `before/after JSON`, `created_at`.

**Why for CM:**
- We're a memory system that is going to be written to by agents. Today we
  encode trust in two places: a frontmatter `failure_count` integer and an
  ad-hoc `status: active|archived`. That's enough for human-written notes;
  it isn't close to enough for agent-written ones.
- The CHECK constraint
  `can_use_as_instruction = false OR provenance_status IN ('user_confirmed','imported')`
  (`OB1:schemas/agent-memory/schema.sql:90-93`) is the single most valuable
  line of SQL in OB1: it makes "the agent decided to upgrade its own memory
  from suggestion to instruction" *physically impossible at the DB layer*.
  That's the right place to enforce it.
- Pairs naturally with our `memory_mark_failure` and would replace it with
  a logged review action.

**Risks:**
- Two-table data model (memory_files for "human-authored markdown", agent_memory for "agent-written records") is the right shape but requires the search/recall surface to either UNION them or expose two parallel tool families. Don't conflate.
- Vocabulary drift: if we adopt OB1's `memory_type` enum (`decision`/`output`/`lesson`/`constraint`/`open_question`/`failure`/`artifact_reference`/`work_log`), we are committing to that taxonomy. It's a reasonable one. Document the choice.

**Scope:** ~1 day of schema + migration + minimal tooling around it. Don't
boil the ocean on a full review UI in v1 — just give us the table and the
CHECK constraint, plus an `agent_memory_write()` helper, then iterate.

**Where in OB1:**
- Core schema: `OB1:schemas/agent-memory/schema.sql:22-323` (the full thing)
- Provenance doctrine: `OB1:docs/safe-agent-memory-provenance.md` (read this *before* implementing)
- API consumer pattern: `OB1:integrations/agent-memory-api/index.ts` (recall/writeback/review flows)

**Where to land in CM:**
- New module `chimera_memory/agent_memory.py` with the SQL schema constant and the helpers (`write`, `confirm`, `reject`, `mark_stale`, `supersede`, `merge`).
- A small wrapper tool `agent_memory_write(...)` joining the `memory_guard` sanitizer (`chimera_memory/sanitizer.py:99-148`) and the new write helper.
- New MCP tool `agent_memory_review(id, action, notes)` callable by the human.
- The CHECK constraint translates 1:1 to SQLite — both engines accept the same syntax.

---

### CM-3 (HIGH) — Recall trace + audit log

**What:** Two tables — `recall_traces` (query + request scope + response policy + schema_version) and `recall_items` (which row was returned at which rank with which similarity + whether it was later marked used / ignored / wrong) — modeled on `OB1:schemas/agent-memory/schema.sql:181-219`. Every `memory_search`, `memory_recall`, `semantic_search`, and `discord_recall_index` writes one trace row and N item rows.

**Why for CM:**
- The single biggest debugging gap today: a user says "the agent retrieved the wrong memory" and we have *nothing* to look at. No trace, no recall_id to refer to.
- Trace IDs make `memory_mark_failure` an order of magnitude more useful: it can now point at "this trace + this item" rather than a free-text file path.
- Trace data is a free evaluation harness. Once we have weeks of traces, we can compute "memories that were returned in top-3 but ignored" — that's a stronger weak signal than `fm_access_count` for surfacing stale knowledge.
- Cost: small. SQLite handles millions of rows of this shape with no thought.

**Risks:**
- Trace tables grow forever. Add a `retention_days` setting (we already have one for transcripts — `chimera_memory/db.py:106-112`) and a CLI/CRON for `chimera-memory traces prune`.
- Privacy: traces contain queries. They go in the same per-persona DB; no new cross-persona exposure surface.
- Slight write amplification on every recall. Negligible.

**Scope:** ~1 day. Schema + a single decorator/middleware around the existing recall paths.

**Where in OB1:**
- Trace schema: `OB1:schemas/agent-memory/schema.sql:181-219`
- Audit event schema: `OB1:schemas/agent-memory/schema.sql:221-250`
- Write path inside the recall API: see the agent-memory-api summary in this document's research — every recall logs the trace, every returned item gets a row, returned/used/ignored is updated post-hoc.

**Where to land in CM:**
- New module `chimera_memory/observability.py` housing the two tables and a `record_recall(query, items, scope)` helper.
- Wrap the recall paths in `chimera_memory/search.py` and the memory recall functions in `chimera_memory/memory.py:396-522`.
- New tool `recall_explain(trace_id)` to retrieve a past trace.

---

### CM-4 (MEDIUM) — Typed thought-to-thought edges with temporal validity

**What:** A `memory_edges` table — basically `OB1:schemas/typed-reasoning-edges/schema.sql:83-101` mapped onto SQLite, with the same `supports / contradicts / evolved_into / supersedes / depends_on / related_to` vocabulary and `valid_from`/`valid_until`/`decay_weight`/`confidence`/`classifier_version`/`support_count` columns. Optionally a small classifier (CM-4a, optional) that uses our existing embeddings to seed `supports` and `related_to`, and leaves the LLM-only relations (`contradicts`, `evolved_into`, `supersedes`) for an *optional* opt-in classifier.

**Why for CM:**
- Today, `memory_gaps` returns "disconnected files" but it can't say "this memory and that one agree" or "this newer reflection supersedes that older procedural."
- Once provenance (CM-2) is in place, the natural follow-up question is "which memory replaces which?" — that requires `supersedes` edges.
- `decay_weight` on edges pairs naturally with our existing per-type salience decay (`chimera_memory/cognitive.py:19-92`). Same idea, applied to relations instead of nodes.

**Risks:**
- The full OB1 implementation uses LLM classification (`OB1:recipes/typed-edge-classifier/`). For CM, ship the **table and the upsert** in v1 and gate the classifier behind an *opt-in* config flag. We don't want to introduce a default-on LLM dependency.
- Embedding similarity gives you `related_to` and `supports` cheaply; the others genuinely need an LLM call to be reliable. Don't pretend otherwise.

**Scope:** Table + upsert + read tools: ~half a day. Optional classifier (gated): ~2 days.

**Where in OB1:**
- Schema (table + indexes + RLS + `thought_edges_upsert` RPC): `OB1:schemas/typed-reasoning-edges/schema.sql:83-267`
- Classifier strategy + cost model: see the agent's research notes on `OB1:recipes/typed-edge-classifier/classify-edges.mjs` (Haiku filter → Opus classifier hybrid). Useful as a template if we ever build an opt-in version.

**Where to land in CM:**
- Add to `chimera_memory/memory.py` near the existing `memory_gaps` function or, cleaner, a new `chimera_memory/edges.py`.
- Surface as MCP tools: `memory_relate(from, to, relation, confidence)`, `memory_relations(file_path)`, `memory_relations_graph(persona)`.

---

### CM-5 (MEDIUM) — Schema-versioned MCP tool contracts

**What:** Pick a contract version label — e.g. `chimera_memory.tools.v1` —
and stamp every tool's response with it. When we change the JSON shape of
a tool's response in a non-additive way, bump to `v2` and (optionally)
keep a v1 alias for a release.

**Why for CM:**
- We're already shipping a stable surface to PA's vendor copy. Drift between PA's bundled CM and a freshly-installed one is going to bite us. A version label gives agents and PA an explicit handshake.
- OB1 does this on its recall/writeback APIs (`schema_version: "openbrain.agent_memory.recall.v1"` per investigation). It's tiny, it's free, it pays off the first time you change a shape.

**Risks:**
- Cosmetic until we actually have a v2. The point is to set the precedent now.

**Scope:** 1–2 hours.

**Where in OB1:** Inside `OB1:integrations/agent-memory-api/index.ts` — every recall/writeback request and response carries a `schema_version` field; the trace schema (`OB1:schemas/agent-memory/schema.sql:193`) stores it.

**Where to land in CM:**
- Add a `CONTRACT_VERSION = "chimera_memory.tools.v1"` constant in `chimera_memory/server.py`.
- A tiny helper `_tool_response(data: dict | str) -> str` that emits `{ "schema_version": CONTRACT_VERSION, "data": ... }` for JSON-shaped tools.
- Add the same field to the existing `memory_whereami` response (`chimera_memory/server.py:63-131`).

---

### CM-6 (MEDIUM) — ChatGPT-compatible `search` + `fetch` tools

**What:** Two thin MCP tools named literally `search` and `fetch` that
wrap our existing `memory_recall` and `memory_query`/transcript fetch
flows. Shape borrowed verbatim from `OB1:server/index.ts:108-207`.

```ts
search(query: string)                  -> { results: [{ id, title, url }] }
fetch(id: string)                      -> { id, title, text, url, metadata }
```

**Why for CM:**
- ChatGPT's "Custom GPT actions" and Claude Desktop's connector schemas look for these exact tool names. Without them, ChimeraMemory is a Claude Code / Codex / Hermes citizen only — not a ChatGPT citizen.
- This costs ~30 LOC. The dual surface OB1 ships (custom-named *and* compatibility-named) is the right idea: keep our richer tools and add the spec-compatible aliases.
- Citation URLs (`chimera_memory://memory/<id>` or similar) are valuable for "show your work" UIs even if no browser ever resolves them.

**Risks:**
- We don't have a stable id-and-URL convention for memory files yet. Decide that *first*, then ship the tool.
- The `search` name collides with the temptation to call other tools `search`. Reserve it.

**Scope:** Half a day including spec for the URL convention.

**Where in OB1:**
- Tool definitions: `OB1:server/index.ts:108-207`
- Title generator: `OB1:server/index.ts:36-44`
- URL convention: `OB1:server/index.ts:33-44` (env-configurable base + `/<id>`)

**Where to land in CM:**
- New section in `chimera_memory/server.py` next to the existing `memory_*` tools. Reuse `memory_recall` / `memory_search` for the search side; reuse the `memory_files` row → markdown body path (already present in `chimera_memory/memory.py:344-391`) for fetch.

---

### CM-7 (LOW unless we open a community model) — `metadata.json` + CI gate for contributions

**What:** If we ever invite community recipes/integrations into this repo,
adopt OB1's pattern: every contribution folder has a `metadata.json` with
structured fields (`OB1:.github/metadata.schema.json`), and a GitHub
Actions workflow gates the PR on structure, secrets, SQL safety, deps,
and README quality (`OB1:.github/workflows/ob1-gate.yml`,
`OB1:.github/workflows/claude-review.yml`).

**Why for CM:**
- Only relevant if we open up. Today CM is a tightly-scoped library with one consumer (PA). If/when we expand to community parsers (Codex variants, Hermes variants, Discord-mode plug-ins, etc.), the structure pays off.
- The OB1 PR-review workflow is interesting in its own right and worth reading regardless.

**Risks:**
- Premature. Don't adopt until we have ≥3 community-contributed parsers waiting at the door.

**Scope:** ~1 day if we adopt; 0 if we don't.

**Where in OB1:**
- `OB1:CONTRIBUTING.md` (full doctrine)
- `OB1:.github/metadata.schema.json`
- `OB1:.github/workflows/ob1-gate.yml`
- `OB1:.github/workflows/claude-review.yml`

---

## 6. Explicit "Do Not Adopt" List

For completeness, the OB1 features ChimeraMemory should **not** copy and
why. A future agent contemplating these should read this section before
re-litigating:

1. **pgvector / Postgres / Supabase Edge Functions.** Our local-first DNA is the product, not an accident. SQLite + `bge-small` covers the same surface for our scale. If we ever need ANN, use `sqlite-vec` (pure SQLite extension), not a database migration.
2. **OpenRouter / GPT-4o-mini metadata extraction.** Forces a paid API + sends content to a third party. Our cognitive layer is algorithmic on purpose; that's a feature.
3. **Slack/Discord capture bots.** We already capture Discord via the agent harness's JSONL. Running our own bot creates a second source of truth and a credentials surface we don't want.
4. **The web dashboard.** Out of scope. If we ever need one, it can be a separate repo speaking MCP to us; don't bundle it.
5. **Household / family-calendar / job-hunt / meal-planning extensions.** OB1's "learning path" is *content for a tutorial repo*. Not memory infrastructure.
6. **Multi-table per-extension schemas (e.g. `family_members`, `recipes_table`, `applications`).** Wrong shape for us. Our `memory_files` is filesystem-backed; that's the point.
7. **LLM-driven entity extraction worker.** The full pattern (`OB1:integrations/entity-extraction-worker/index.ts` + `OB1:schemas/entity-extraction/schema.sql`) is impressive but couples to a cloud LLM. Our `memory_gaps` (`chimera_memory/memory.py:543-607`) already does the cheap version. Don't upgrade to the expensive one.
8. **The `enhanced-thoughts` denormalization** (`OB1:schemas/enhanced-thoughts/schema.sql`). Moving `type` / `importance` / `quality_score` from JSON-in-metadata into typed columns is OB1's *recovery* from having started with a flat `thoughts` JSON-only model. We started with typed frontmatter (`fm_type`, `fm_importance`, `fm_status` in `chimera_memory/memory.py:36-56`) and don't need this fix.
9. **CORS / browser-MCP shims** (`OB1:server/index.ts:530-543`). Only relevant if we add an HTTP transport. We're stdio.
10. **`FSL-1.1-MIT` license.** We're MIT (`pyproject.toml:11`). Don't change.

---

## 7. Quick Refactor / Hygiene Items (Side Quests)

These are not adoption items — they're things this comparison surfaced
about ChimeraMemory itself that are worth fixing regardless:

- **`chimera_memory/server.py:594`** hardcodes `C:/Github/ChimeraPersonas/personas` as the `CHIMERA_PERSONAS_DIR` fallback. Replace with a portable default (e.g. `~/.chimera-memory/personas`) and document the env var.
- **`chimera_memory/memory.py:483-522`** (`memory_recall`) loads every embedding into Python and ranks in a loop. Acceptable today; will start to bite around 100K memories. When we do CM-1, also evaluate `sqlite-vec` (the SQLite extension for ANN). It's MIT, single shared lib, ~200KB, and it preserves our local-first story. The migration is a single SQL `CREATE VIRTUAL TABLE` plus a swap in the recall function.
- **`chimera_memory/cognitive.py:157-182`** (`score_all_surprise`) is O(N²). Once CM has more than a few thousand memories, it should sample neighbors via the same ANN index rather than full-pair-scan.
- **`chimera_memory/server.py:179-822`** is 21 `@server.tool()` decorators in a single function. Worth splitting into `transcript_tools.py` / `memory_tools.py` / `cognitive_tools.py` modules now, before CM-2 and CM-3 add more.
- **Cognitive layer + curated memory layer are tightly coupled to the `memory_files` table.** When we add `agent_memory` (CM-2), be deliberate about which cognitive functions apply to which table; don't accidentally decay agent-written records by the same per-type rates as human-curated ones.
- **Tests are good but only cover the algorithmic side.** Once CM-2 / CM-3 land, add tests for "agent retries a write" (CM-1), "agent can't upgrade its own memory to instruction" (CM-2 CHECK constraint), and "recall trace + item rows exist after a search" (CM-3).

---

## 8. Suggested Implementation Order

If we end up doing this:

1. **CM-1 (fingerprint + upsert)** — foundation; needed by both CM-2 and CM-3.
2. **CM-5 (contract version)** — trivial; do it as a piggyback commit on CM-1.
3. **CM-2 (provenance schema)** — biggest unlock; only do this once we actually have an agent that wants to write back.
4. **CM-3 (recall trace + audit)** — best done concurrently with CM-2 so the table comes online with traffic.
5. **CM-6 (`search`/`fetch`)** — independent; can ship any time. Good for cross-runtime story.
6. **CM-4 (typed edges)** — only after CM-2 lands; the relations want to point at provenance-tagged rows.
7. **CM-7 (community gate)** — defer until needed.

Side quests can interleave with any of the above.

---

## 9. References for Future Agents

When picking up any item above, **start here, not on Google**:

- **This file.** `docs/OB1_COMPARISON.md`.
- **CM canonical anchors:**
  - Schema & DB primitives: `chimera_memory/db.py:1-338`
  - Curated memory layer: `chimera_memory/memory.py:1-829`
  - Cognitive layer: `chimera_memory/cognitive.py:1-318`
  - MCP server surface (21 tools): `chimera_memory/server.py:1-1002`
  - Sanitizer / injection scan: `chimera_memory/sanitizer.py:1-148`
  - Persona scoping: `chimera_memory/identity.py`, `chimera_memory/paths.py`, `chimera_memory/memory.py:129-212`
  - Tests as living docs: `tests/test_persona_scope.py`, `tests/test_indexer.py`, `tests/test_search.py`, `tests/test_whereami.py`
- **OB1 canonical anchors** (clone fresh from https://github.com/NateBJones-Projects/OB1):
  - Base capture/search MCP server: `OB1:server/index.ts:1-551`
  - Agent-memory governance schema: `OB1:schemas/agent-memory/schema.sql:1-328`
  - Enhanced thoughts (typed columns + FTS RPCs + upsert): `OB1:schemas/enhanced-thoughts/schema.sql:1-395`
  - Entity extraction schema: `OB1:schemas/entity-extraction/schema.sql:1-276`
  - Typed reasoning edges: `OB1:schemas/typed-reasoning-edges/schema.sql:1-316`
  - Agent memory API (recall/writeback/review/trace): `OB1:integrations/agent-memory-api/index.ts`
  - Content fingerprint doctrine: `OB1:recipes/content-fingerprint-dedup/README.md`
  - Local Ollama embeddings (reference for local-only embedding alternatives): `OB1:recipes/local-ollama-embeddings/embed-local.py`
  - Provenance philosophy: `OB1:docs/safe-agent-memory-provenance.md`
  - Contribution gate: `OB1:.github/workflows/ob1-gate.yml`, `OB1:.github/metadata.schema.json`, `OB1:CONTRIBUTING.md`

---

## 10. Closing Note

OB1 and ChimeraMemory have almost no code in common and almost no users in
common, but they're both wrestling with the same underlying problem:
*memory you can actually trust an agent to use.* OB1's answer is heavy
schema + heavy governance + cloud convenience. CM's answer is local,
algorithmic, persona-bounded, and zero-LLM in the hot path.

The seven candidates above (especially CM-1, CM-2, CM-3) are where OB1's
schema-side maturity outruns ours and we'd be silly not to learn from it.
The "do not adopt" list is where their cloud-first design choices would
*delete* the thing that makes us valuable. Keep both lists honest.
