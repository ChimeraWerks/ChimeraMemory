# OB1 to ChimeraMemory Feature Comparison and Lift Plan

**Status:** Research deliverable with implementation shipped through Phase 5e dashboard plus the first Phase 6 entity-graph slice.

**Last updated:** 2026-05-14 (Day 58)

## Purpose

This document captures the comparative analysis between [OpenBrain (OB1)](https://github.com/NateBJones-Projects/OB1) and ChimeraMemory (CM), and the agreed upgrade path for CM that lifts OB1's best patterns while preserving CM's local-first architecture.

The lift path was developed jointly across two days of pair-research between PersonifyAgents personas Sarah (researcher) and Asa (developer), with comparative AR cycles converging on a 12-item ordered roadmap. The full research transcript lives in the PersonifyAgents Discord workspace under `#active-development`; this doc is the durable distillation.

## Core Decisions

1. **CM remains the core memory engine.** We are NOT adopting OB1 as core. We are NOT making CM a plugin on top of OB1. CM's local-first architecture (SQLite + markdown+YAML files + fastembed BGE-small + persona folder scoping + MCP stdio) stays the default. This preserves architectural sovereignty over the memory layer, keeps CM at $0/mo at any scale, and protects PA's identity layer from upstream-team direction shifts.

2. **No Supabase, no Postgres dependency.** Every OB1 pattern worth lifting is implementable as SQLite tables in CM. The lift list does not require Postgres infrastructure.

3. **Default-additive, replace-only-with-receipts.** Governing rule for the upgrade path:
   - **Add:** trace tables, audit events, review queue, provenance fields, sensitivity labels, content fingerprints, entity/edge sidecars, optional dashboard/API wrappers.
   - **Do not replace without empirical proof:** CM's hybrid FTS5+vector retrieval with Reciprocal Rank Fusion + multi-signal rerank, markdown source-of-truth, SQLite local store, BGE local embeddings, persona folder scoping, MCP stdio default.

4. **Memory-enhancement sidecar architecture is separate from role-play persona sidecar.** Shared infrastructure (OAuth token management, subprocess spawn discipline, prompt-injection-safe content wrapper, rate-limit observability) but distinct workers. Different threat models, different model sizing, different cadence, different failure domains.

5. **Adoption-trigger.** If PA's product shape ever shifts toward SaaS / multi-user / subscription-monetized cloud-memory layer, re-evaluate whether OB1 should become core. For the current personal-use-local-first shape, the answer is "lift patterns, not adopt platform."

## Baseline: What ChimeraMemory Already Has

- **Storage:** SQLite + FTS5 + ONNX embeddings (bge-small-en-v1.5 via [fastembed](https://github.com/qdrant/fastembed), 23MB local model)
- **Two layers:** transcript layer (auto-indexed JSONL session logs) + curated memory layer (markdown + YAML frontmatter)
- **Concurrency:** WAL mode + retry-with-backoff + tail-read pattern for active JSONL files
- **Watcher:** watchdog filesystem watcher + poll safety net
- **Incremental indexing:** MD5 file hashes for skip-unchanged
- **MCP surface:** tools across transcript, curated memory, governance, enhancement, entity graph, and cognitive analytics layers (stdio MCP)
- **Cognitive features:**
  - Zone-based loading (CORE/ACTIVE/PASSIVE/ARCHIVE) with weighted scoring
  - Algorithmic decay per memory type
  - Surprise scoring via nearest-neighbor embedding distance
  - Failure marking with zone penalty
  - Memory guard pre-write credential/injection scan
  - Sanitizer at index time (API keys, bot tokens, webhook URLs, bearer tokens, secrets, invisible unicode) redacted as `<REDACTED:type>`
- **Hybrid search via Reciprocal Rank Fusion** (FTS5 + vector) with multi-signal re-ranking (recency, session affinity, content richness)
- **Memory file format:** YAML frontmatter (`type` / `importance` 1-10 / `created` / `last_accessed` / `access_count` / `tags` / `status` / optional `about` / `entity` / `relationship_temperature` / `trust_level` / `trend` / `failure_count`) + markdown body
- **Cross-runtime:** Claude Code / Codex CLI / Hermes Agent integrations
- **CLI:** `chimera-memory serve|backfill|stats|split-db|codex doctor|codex template`

## What OB1 Has That CM Doesn't

### High-value lifts (top tier)

1. **Recall observability:** OB1's `agent_memory_recall_traces` + `agent_memory_recall_items` + `agent_memory_audit_events` tables capture every recall request, every returned item with rank/similarity/used/ignored, and every memory operation. CM has zero retrieval-quality measurement today. (OB1 ref: `schemas/agent-memory/schema.sql:181-247`)

2. **Memory governance schema:** OB1's `agent_memories` sidecar carries `provenance_status` (observed/inferred/user_confirmed/imported/generated/superseded/disputed), `confidence` 0-1, `lifecycle_status`, `review_status`, use-policy fields (`can_use_as_instruction` default false, `can_use_as_evidence` default true, `requires_user_confirmation` default true). CHECK constraint enforces instruction-grade only from user_confirmed or imported provenance. CM treats all written memories as equally authoritative. (OB1 ref: `schemas/agent-memory/schema.sql:22-94`)

3. **Human review workflow:** OB1's `agent_memory_review_actions` table captures every review action (confirm/edit/evidence_only/restrict_scope/mark_stale/merge/reject/dispute/supersede) with before/after JSONB snapshots. CM's `memory_mark_failure` is the closest analog (single binary action). (OB1 ref: `schemas/agent-memory/schema.sql:154-179`)

4. **Sensitivity tier with restrict-by-default filtering:** OB1's `sensitivity_tier` field on content rows + RPC defaults to `p_exclude_restricted=true`. CM excludes gossip/social paths via code policy but has no per-memory sensitivity label. Once recall traces exist, sensitivity is policy not decoration. (OB1 ref: `schemas/enhanced-thoughts/schema.sql:10, 143-189`)

5. **Content fingerprinting + idempotency:** OB1's normalized SHA256 (`lower + trim + collapse_whitespace`) lets `upsert_thought` dedupe on functional content. UNIQUE constraint on `idempotency_key` for retry-safe writes. CM dedupes file-level via MD5 in import_log; OB1's content-level dedup is a different shape. (OB1 ref: `schemas/agent-memory/schema.sql:265-273` + `schemas/enhanced-thoughts/schema.sql:342-345`)

### Medium-value lifts (second tier)

6. **MCP tool-surface discipline:** OB1's `docs/05-tool-audit.md` has explicit recommendations on MCP tool count + capture/query/admin splits. CM's tool surface is already broad; audit before adding more. (OB1 ref: `docs/05-tool-audit.md:15-27, 183-238`)

7. **Typed memory relations:** OB1's `thought_edges` table with relations `supports / contradicts / evolved_into / supersedes / depends_on / related_to`. Each edge has `confidence`, `support_count` (bumped on re-classification), `valid_from`, `valid_until`, `decay_weight`, `classifier_version`. CM has graph analysis for disconnected clusters but no explicit relations. (OB1 ref: `schemas/typed-reasoning-edges/schema.sql:60-101`)

8. **Temporal validity on relations:** `valid_from / valid_until / decay_weight` columns + partial index for current-only queries. Lets you say "this was true between dates X-Y" and decay relations over time. CM's decay is per-file importance, not per-relation. (OB1 ref: `schemas/typed-reasoning-edges/schema.sql:91-95, 122-130, 276-301`)

9. **Entity extraction system:** OB1's `entities` + `edges` + `thought_entities` + `entity_extraction_queue` tables. Auto-extracted entities (people/projects/topics/tools/organizations/places) with canonical/normalized/aliases, evidence-bearing links to thoughts, async-processing queue with auto-queue trigger on `INSERT OR UPDATE`. CM now has the local SQLite graph tables, frontmatter-derived indexing, enhancement-result entity linking, shared-file connection queries, and typed entity-edge query/upsert helpers; live-tokened LLM extraction into those tables remains future work. (OB1 ref: `schemas/entity-extraction/schema.sql:32-178`)

10. **Prompt-injection-safe enrichment wrapper:** OB1's `wrapThoughtContent` wraps captured content as untrusted data before LLM extraction. Critical precondition for any LLM-driven enrichment in CM. (OB1 ref: `integrations/entity-extraction-worker/index.ts:166, 202`)

### Third tier (after governance + sidecar exist)

11. **HTTP/SSE MCP server with CORS + access-key auth:** OB1's Deno + Hono + StreamableHTTPTransport with `MCP_ACCESS_KEY` via header OR URL `?key=`, CORS, Claude Desktop Accept-header patch. CM is stdio-only today. (OB1 ref: `server/index.ts:507-548`)

12. **REST API surface alongside MCP:** `POST /recall`, `POST /writeback`, `PATCH /memories/:id/review`, health endpoint. (OB1 ref: `integrations/agent-memory-api/README.md:11-19`)

13. **Review/trace dashboard:** Next.js UI for memory review + recall traces. CM is MCP/CLI only. For PA, this would be folded into PA's PWA rather than a separate Next.js. (OB1 ref: `dashboards/open-brain-dashboard-next/app/agent-memory/page.tsx`, `traces/page.tsx`)

14. **Adaptive capture classification:** Learning loop with per-type confidence thresholds + user correction feedback + A/B model comparison. CM's frontmatter type is static/manual. (OB1 ref: `recipes/adaptive-capture-classification/schema.sql:37`, `capture-with-gating.ts:236-247`)

15. **Live retrieval loop:** Proactive recall on topic/person/project shifts, silent on miss, logged for tuning. CM has recall tools but no proactive retrieval behavior. (OB1 ref: `recipes/live-retrieval/README.md:5-61`)

16. **Auto-capture session-close protocol:** Captures ACT NOW items + session summary at wrap-up. (OB1 ref: `skills/auto-capture/SKILL.md:22-57`)

17. **Heavy-file ingestion before memory:** PDF/docs/sheets converted into indexed artifacts before reasoning. (OB1 ref: `skills/heavy-file-ingestion/SKILL.md:23-66`)

18. **Pyramid summaries:** Multi-resolution summaries for long-horizon recall. (OB1 ref: `recipes/chatgpt-conversation-import/README.md:194-221`)

19. **Import pipelines:** ChatGPT export, Perplexity, Obsidian vault, X/Twitter, Instagram, Google Activity, Grok, Gmail, Atom/Blogger. ChatGPT, Obsidian, Gmail, Perplexity, Grok, X/Twitter, Instagram, Google Activity, and Atom/Blogger scaffolding shipped. (OB1 ref: `recipes/*-import/`)

20. **Portable context profile export:** Reviewed memory to USER.md / SOUL.md / HEARTBEAT.md / structured JSON. Shipped as deterministic reviewed-memory export. (OB1 ref: `recipes/bring-your-own-context/README.md:206-231`)

## What We Are NOT Lifting (Intentional Exclusions)

- **Row-Level Security:** CM is single-user (or per-persona). RLS doesn't transfer without committing to multi-tenant.
- **Multi-workspace/org isolation:** Same reason. Relevant only if CM goes multi-tenant.
- **Supabase / pgvector / PostgreSQL-specific functions:** CM's SQLite + fastembed stack is a deliberate local-first choice.
- **Heavy cloud-LLM dependencies for the base path:** CM is explicitly local-offline-first. Cloud-LLM enrichment lands as opt-in sidecar, not as a baseline requirement.

## Where CM is Better Than OB1 (Don't Flatten These)

- **Retrieval intelligence:** CM's hybrid FTS5+vector via Reciprocal Rank Fusion + multi-signal rerank (recency, session affinity, content richness) is more sophisticated than OB1's pure pgvector path. OB1 is better at scale; CM is better at personal-memory relevance.
- **Markdown source-of-truth:** Human-readable raw storage. Editable with Obsidian / VS Code / vim. Git-versionable. Backup via file copy. Editor-as-UI for free. Survives DB corruption.
- **Cognitive features:** Zone-based loading, algorithmic decay per memory type, surprise scoring, failure marking with zone penalty, memory_gaps graph analysis. CM-original features OB1 doesn't have.
- **Persona-folder scoping:** ChimeraAgency's per-persona memory folders are CM-shaped: markdown files in trees. Native to how personas actually think.

## Six-Phase Upgrade Path

The lift items above are sequenced into six phases. Each phase ships independently, AR'd between Asa and Sarah, with backups at phase boundaries.

- **Phase 0:** Small impact, easy changes. Codex MCP commands, SQLite hygiene, comparison docs, baseline backup. Shipped.
- **Phase 1:** Sidecar architecture spec. Shipped.
- **Phase 2:** Observability spine. `recall_traces` + `recall_items` + `audit_events` SQLite tables, MCP tools to query. Shipped.
- **Phase 3:** Safety spine. Governance fields (provenance, confidence, lifecycle_status, review_status, sensitivity_tier, use-policy) on `memory_files` + YAML frontmatter extensions. Shipped.
- **Phase 4:** Writeback hygiene. content_fingerprint UNIQUE index, idempotency_key UNIQUE index, review-queue MCP tools. Shipped.
- **Phase 5:** Sidecar implementation + usability layer. Contract, queue, deterministic dry-run worker, provider plumbing, optional models.dev-backed OpenAI/Anthropic/Gemini/OpenRouter model defaults, PA supervisor rails, smoke harness, PA dashboard, auto-capture session-close protocol, and live-retrieval dry-run checks shipped. Live tokened provider verification remains.
- **Phase 6:** Expansion. Local entity graph tables, frontmatter-derived indexing, enhancement-result entity linking, shared-file connection query, typed entity-edge query/upsert helpers, typed memory-file reasoning edges, temporal validity sweep helpers, deterministic pyramid summaries, ChatGPT, Obsidian, Gmail, Perplexity, Grok, X/Twitter, Instagram, Google Activity, and Atom/Blogger import scaffolding, and portable profile export shipped.

## Memory-Enhancement Sidecar Design

Separate worker from role-play persona sidecar. Shared infrastructure (OAuth token management, subprocess spawn discipline, prompt-injection-safe content wrapping, rate-limit observability) but distinct processes.

- **Input:** raw memory content + persona context.
- **Output:** typed metadata JSON centered on `entities[]` objects (`name`, closed-set `type`, `confidence`), with compatibility projections for topics, people, projects, tools, organizations, places, dates, action items, confidence, and sensitivity hints.
- **Model choice (user-toggleable, priority order):**
  1. OpenAI, Anthropic, Gemini, or OpenRouter via configured credential refs
  2. Local OpenAI-compatible, Ollama, or LM Studio as fallback candidates
  3. Local fastembed retains as default for embeddings
- **Extraction architecture:** OB1-style closed-set entities, confidence filtering, name sanitization, deterministic canonicalization, and prompt-injection-safe content wrapping. CM adapts the pattern to Markdown/YAML memory files plus the local SQLite entity graph instead of copying OB1's storage model.
- **Subprocess discipline:** port-close-wait pattern, supervisor cadence hook (lifted from PA Day 56-57 work).
- **Content wrapper:** OB1's `wrapThoughtContent` equivalent. Captured content always wrapped as untrusted before LLM call.
- **OAuth plumbing:** scoped tokens passed to sidecar, not raw refresh-tokens. Same shape as PA's runtime-profile credential handling.

## Provenance

Research compiled jointly by Sarah (researcher) and Asa (developer) in the PersonifyAgents workspace, Day 57 (2026-05-13) into Day 58 (2026-05-14):

- Independent feature lists posted to PA's `#planning` channel: Sarah 37 items across 11 sections, Asa 24 items
- Comparative AR with 11 items Asa caught that Sarah missed (mostly recipes/skills/dashboards/behavior-layer) and 8 items Sarah caught that Asa missed (mostly schema/SQL/migration-discipline)
- Asa's 5-spine reframe (observability/safety/writeback hygiene/usability/expansion) as cleaner mental model than tiered ordering
- Sensitivity-tier misclassification correction (Sarah had it in "polish" but it's the column governance filters on, belongs top-tier)
- Day 58 morning honest-analysis arc on whether to adopt OB1 as core (converged: no, lift patterns, revisit if PA shifts to SaaS)
- Day 58 plan synthesis by Sarah with Asa's corrections, posted in PA Discord thread `CM Dev - OB1 Lift Progress` (id `1504468117999456377`)

Full research artifact lives in Sarah's persona memory at `personas/researcher/sarah/research/proposals/ob1-cm-feature-comparison-day57.md` in the ChimeraAgency repo.

## License

OB1 is MIT licensed by Nate B. Jones. Lift work preserves attribution where OB1 patterns are adapted. CM stays MIT.
