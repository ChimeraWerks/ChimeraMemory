# Chimera Memory

**Perfect recall and cognitive memory for any agent harness.** A standalone MCP server that indexes session transcripts into queryable SQLite, adds a curated memory layer with semantic search and zone-based loading, and gives you tools for everything from "what did we talk about yesterday" to "which memories are stale and should decay."

Works with Claude Code, Codex CLI, and Hermes Agent. No required dependency on any other repo.

## What It Does

Modern coding agents write detailed session logs (JSONL files) every time you use them. Chimera Memory indexes those files into a local SQLite database, embeds them for semantic search, and layers a curated memory system on top so you can write memories as markdown + YAML frontmatter and query them through the same MCP interface.

```
Agent harness writes  →  JSONL files  →  ChimeraMemory indexes  →  SQLite + embeddings  →  You query via MCP
Your memory files     →  markdown+YAML →  ChimeraMemory indexes  →  FTS5 + zones          →  You query via MCP
```

**Two layers, one interface:**

- **Transcript layer** — everything the harness has ever said, heard, or tooled. Auto-indexed. Zero effort.
- **Curated memory layer** — markdown files you deliberately write (facts, episodes, procedural lessons). Opinionated structure. Importance scoring. Zones. Decay. Graph analysis. Optional.

Use whichever layer you want. Both are exposed through the same MCP server.

## Problems It Solves

- **No native query for transcripts.** Claude Code / Codex / Hermes write JSONL session logs but offer no recall API. Without indexing, "what did we discuss last Tuesday" requires opening files manually.
- **Context loss between sessions.** Agents forget across `/clear` and across days. A queryable transcript DB plus a curated memory layer gives an agent persistent recall.
- **Curated knowledge degrades silently.** Without decay, importance scoring, or graph analysis, written memories pile up; bad knowledge doesn't get penalized; outdated facts mix with current ones.
- **No principled "what loads on session start."** Without zones and importance scoring, you load too much (token waste) or too little (forgotten context).
- **Hidden secrets in transcripts.** Raw JSONL contains tokens, API keys, webhook URLs. A naive grep can leak. Sanitization at index time keeps the DB clean.

Chimera Memory addresses each.

## Quick Start

```bash
# Clone and install (editable mode = live source updates)
git clone https://github.com/ChimeraWerks/ChimeraMemory.git
cd ChimeraMemory
pip install -e .

# Index your existing sessions
chimera-memory backfill

# Run as MCP server
chimera-memory serve
```

`pip install -e .` creates the `chimera-memory` CLI on PATH and adds the Python package via `.pth` so any edit to source flows through immediately on next process spawn. No re-install needed for code changes; only dependency changes (new `pyproject.toml` requires) need a re-run.

## Integration Patterns (Cross-Runtime)

Chimera Memory works with three agent harnesses today, each with a slightly different "front door":

### Claude Code

Spawned as an MCP server. Wire it into your `.mcp.json`:

```json
{
  "mcpServers": {
    "chimera-memory": {
      "command": "chimera-memory",
      "args": ["serve"],
      "env": {
        "TRANSCRIPT_JSONL_DIR": "~/.claude/projects/YOUR-PROJECT/",
        "OMP_NUM_THREADS": "12"
      }
    }
  }
}
```

`OMP_NUM_THREADS` caps the embedding model's CPU usage. Set it to roughly 75% of your cores BEFORE the server starts — ONNX won't respect the setting if applied later.

Restart Claude Code and the tools appear as `mcp__chimera-memory__*`.

### Codex CLI

Same shape, different file. Codex reads `~/.codex/mcp_servers.json`:

```json
{
  "mcpServers": {
    "chimera-memory": {
      "command": "chimera-memory",
      "args": ["serve"],
      "env": {
        "TRANSCRIPT_JSONL_DIR": "~/.codex/sessions/",
        "TRANSCRIPT_PERSONA": "your-persona",
        "CHIMERA_CLIENT": "codex"
      }
    }
  }
}
```

Check the wiring without exposing raw environment values:

```bash
chimera-memory codex doctor
```

The doctor verifies that the Codex MCP config exists, the `chimera-memory`
server entry is present, the command resolves, `serve` is passed, and the Codex
parser is selected with `CHIMERA_CLIENT=codex`.

Generate a safe config template without reading or modifying your live Codex
config:

```bash
chimera-memory codex template --persona your-persona
```

Add identity fields when you want persona-scoped indexing:

```bash
chimera-memory codex template \
  --persona asa \
  --persona-id developer/asa \
  --persona-name asa \
  --persona-root C:/Github/ChimeraAgency/personas/developer/asa \
  --personas-dir C:/Github/ChimeraAgency/personas \
  --shared-root C:/Github/ChimeraAgency/shared
```

The template command prints JSON only. It does not write `mcp_servers.json` and
does not include secrets or OAuth tokens.

### Hermes Agent

Hermes supports two integration modes simultaneously:

1. **As an MCP server** (same as Claude Code / Codex). Lives in `<HERMES_HOME>/config.yaml` under `mcp_servers.chimera-memory`.
2. **As a native memory provider** via plugin filesystem symlink at `<HERMES_HOME>/plugins/chimera_memory` plus `memory.provider: chimera_memory` in `config.yaml`. This is Hermes's first-class memory backend — used during agent turns for live recall, replacing or supplementing Honcho.

The plugin path uses a filesystem **symlink** to the source repo, so source edits flow through. Different mechanism from Claude Code's pip-install pattern (Hermes scans a directory, Claude Code spawns a CLI), same outcome.

### Via PersonifyAgents Installer (Automated)

PA bundles three handlers that wire Chimera Memory into any of the three runtimes deterministically:

```bash
# Wire CM as a Hermes memory provider (plugin symlink + config.yaml mutation)
personifyagents install apply \
  --runtime hermes \
  --feature chimera_memory.hermes_provider \
  --hermes-home /path/to/hermes-home \
  --chimera-memory-repo /path/to/ChimeraMemory \
  --mode symlink \
  --yes

# Wire CM as an MCP server in any runtime's config
personifyagents install apply \
  --runtime claude_code \
  --feature chimera_memory.mcp_server \
  --runtime-home /path/to/claude-project \
  --yes

# Install a transcript backfill helper script (calls chimera-memory backfill on schedule)
personifyagents install apply \
  --runtime hermes \
  --feature chimera_memory.transcript_backfill_helper \
  --persona <persona-name> \
  --yes
```

Each PA apply writes a backup, a receipt, and updates the install-state ledger ... fully audited and reversible. PA assumes `chimera-memory` is already on PATH (it doesn't pip-install the binary itself; that's a separate concern).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Your Machine                            │
│                                                              │
│  Agent harness  ──writes──►  JSONL Session Files             │
│                                 │                            │
│  Your Memory   ──writes──►  Markdown + YAML files            │
│                                 │                            │
│                         ┌───────▼────────┐                   │
│                         │   Indexer      │  watchdog +       │
│                         │                │  poll safety      │
│                         └───────┬────────┘                   │
│                                 │                            │
│                         ┌───────▼────────┐                   │
│                         │   Sanitizer    │  secret +         │
│                         │                │  injection scan   │
│                         └───────┬────────┘                   │
│                                 │                            │
│                         ┌───────▼────────┐                   │
│                         │  SQLite + FTS5 │  WAL mode         │
│                         │  + embeddings  │  bge-small-en     │
│                         └───────┬────────┘                   │
│                                 │                            │
│                         ┌───────▼────────┐                   │
│                         │ Cognitive Layer│  decay, surprise, │
│                         │                │  zones, gaps      │
│                         └───────┬────────┘                   │
│                                 │                            │
│                  ┌──────────────┴──────────────┐             │
│                  │                             │             │
│             MCP Server                         CLI           │
│        (local memory tools)                (setup + query)   │
└─────────────────────────────────────────────────────────────┘
```

## MCP Tools

### Transcript Layer (everything the harness wrote)

| Tool | What it does |
|------|-------------|
| `discord_recall_index` | Compact search index (~100 tokens/result). **Use this first.** Returns ID, timestamp, author, 80-char preview. |
| `discord_detail` | Fetch full content for specific entry IDs from the index. Used after `discord_recall_index`. |
| `discord_recall` | Direct full-content search. Heavier than the index flow. Use when you need everything at once. |
| `semantic_search` | Hybrid FTS5 + vector search via Reciprocal Rank Fusion. Finds "car" when you search "vehicle." |
| `session_list` | Browse sessions with dates, durations, dispositions, persona filters. |
| `transcript_stats` | Entry count, session count, DB size, last entry timestamp, breakdowns by type and source. |
| `transcript_backfill` | Index all historical JSONL files. Safe to re-run (skips unchanged via MD5). |
| `embed_transcripts` | Generate embeddings for entries that don't have them. Required for semantic search. |

**Recommended recall workflow** (3-10x token savings vs direct recall):
1. `discord_recall_index(search="topic")` — scan previews
2. Pick relevant IDs
3. `discord_detail(ids=[...])` — get full content only for those

### Curated Memory Layer (markdown files you write)

| Tool | What it does |
|------|-------------|
| `memory_stats` | Corpus overview. File counts by type, status, persona. Zero-token session start check. |
| `memory_search` | FTS5 full-text search across your memory files. |
| `memory_recall` | Semantic similarity search via embeddings. Use for fuzzy/conceptual queries. |
| `memory_query` | Structured filter by type, importance, status, tags, about field. |
| `memory_guard` | Scan text for credentials, injection patterns, invisible unicode before persisting. |
| `memory_gaps` | Graph analysis. Finds disconnected memory clusters and isolated files. |
| `memory_entity_index` | Build the local entity graph from indexed memory frontmatter and tags. Enhancement results can add links too. |
| `memory_entity_query` | Query entities, shared-file connections, or explicit typed entity edges. |
| `memory_edge_upsert` | Create or reinforce a typed reasoning edge between two memory files. |
| `memory_edge_query` | Query memory-to-memory reasoning edges such as supports or supersedes. |
| `memory_edge_temporal_sweep` | Expire current memory edges whose validity inputs are stale. |
| `memory_pyramid_summary_build` | Build deterministic chunk, section, and document summaries for an indexed memory file. |
| `memory_pyramid_summary_query` | Query multi-resolution summaries for long imported memories. |
| `memory_import_chatgpt_export` | Plan or write governed memories from a ChatGPT `conversations.json` export, with optional pyramid summaries. |
| `memory_import_obsidian_vault` | Plan or write governed memories from an Obsidian markdown vault directory or zip export. |
| `memory_import_gmail_mbox` | Plan or write restricted, evidence-only memories from Gmail / Google Takeout mbox exports. |
| `memory_import_perplexity_export` | Plan or write governed memories from Perplexity markdown, text, or JSON exports. |
| `memory_import_grok_export` | Plan or write governed memories from Grok markdown, text, JSON, or JSONL exports. |
| `memory_import_twitter_archive` | Plan or write governed tweet/status memories from X/Twitter archive exports. |
| `memory_profile_export` | Plan or write portable USER.md / SOUL.md / HEARTBEAT.md / JSON context artifacts from reviewed memory. |
| `memory_reindex` | Force re-scan after bulk file changes. |
| `memory_mark_failure` | Flag a memory that led to wrong advice. Penalizes its zone score. |
| `memory_consolidation_report` | Dry-run analysis: what would be decayed, staled, or archived. |

### Governance and Enhancement

| Tool | What it does |
|------|-------------|
| `memory_recall_trace_query` | Inspect recent recall traces and optional returned items. Useful for tuning retrieval quality. |
| `memory_audit_query` | Inspect memory audit events such as recall, review, and enhancement operations. |
| `memory_live_retrieval_check` | Dry-run proactive recall on topic shifts, silent on miss and logged for tuning. |
| `memory_review_pending` | List generated or restricted memories that need review before instructional use. |
| `memory_review_action` | Confirm, restrict, reject, stale, merge, dispute, or supersede a memory review item. |
| `memory_auto_capture_session_close` | Plan or write an evidence-only session-close memory with ACT NOW items. |
| `memory_enhancement_provider_plan` | Show the selected enhancement provider and budget caps without exposing credential refs. |
| `memory_enhancement_enqueue` | Queue an indexed memory file for metadata enrichment. |
| `memory_enhancement_dry_run` | Process queued enhancement jobs with deterministic local metadata. No model call required. |

### Cognitive Analytics

| Tool | What it does |
|------|-------------|
| `memory_zones` | Assigns every memory to CORE/ACTIVE/PASSIVE/ARCHIVE tier based on importance, frequency, recency, and failures. Drives "what loads automatically." |
| `memory_decay_report` | Per-type exponential decay rates. Procedural decays slowest (load-bearing), opinions fastest. |
| `memory_surprise` | Novelty scoring via nearest-neighbor embedding distance. High surprise = unique. Low = redundant. |

## Memory File Format

Markdown + YAML frontmatter:

```markdown
---
type: procedural        # episodic | semantic | procedural | entity | reflection | social
importance: 8           # 1-10
created: 2026-04-06
last_accessed: 2026-04-06
access_count: 0
tags: [topic, topic]
status: active
---

Natural language content. How you'd actually think about it.
```

The frontmatter drives everything — importance feeds zone scoring, access_count tracks reinforcement, tags enable graph analysis, failure_count penalizes bad knowledge.

## Zone-Based Loading

```
CORE     (≥0.70)   load automatically on session start
ACTIVE   (≥0.55)   load when tags match current task
PASSIVE  (≥0.30)   loaded only on direct query
ARCHIVE  (<0.30)   never auto-loaded, still queryable
```

**Scoring formula:**
```
score = confidence·0.25 + frequency·0.20 + recency·0.15
      + context_match·0.20 + spec_alignment·0.15
      - failure_penalty·0.25
```

Access reinforcement happens automatically on every `memory_search` or `memory_recall` hit. Frequency grows naturally through use. Failure marks (`memory_mark_failure`) penalize bad memories so they fall down the zones over time.

## How It Works

### JSONL Parsing

Each agent harness stores sessions as JSONL — one JSON object per line. Each object is a user message, assistant response, tool call, system event, attachment, or platform-specific event (e.g. Discord). The parser classifies each entry:

| Entry Type | What It Captures | Indexed Content |
|-----------|-----------------|-----------------|
| `discord_inbound` | Messages received from Discord | Full message text |
| `discord_outbound` | Messages sent to Discord | Full message text |
| `user_message` | CLI user input | Full text |
| `assistant_message` | Agent responses | Full text (no thinking blocks) |
| `tool_call` | Tool invocations (Read, Bash, etc.) | Metadata only |
| `tool_result` | Tool output | Metadata only |
| `system` | System events, notifications | Metadata only |
| `attachment` | File attachments | Path and metadata |

**Design choice:** Conversation content gets full-text indexed and embedded. Tool I/O gets metadata only. This keeps the DB lean and search results relevant — you won't get 50 `tool_result` hits when you search for "umbrella."

### Content Sanitization

Every entry passes through a sanitizer that detects and redacts:

- API keys (`sk-ant-*`, `sk-*`, `ghp_*`, AWS keys)
- Bot tokens (Discord, Slack)
- Webhook URLs
- Bearer tokens
- Passwords and secrets in env-var format
- Private keys
- Invisible unicode (injection vector)

Redacted content is replaced with `<REDACTED:type>` markers. The original never touches disk.

### Embeddings

Semantic search uses `bge-small-en-v1.5` via [fastembed](https://github.com/qdrant/fastembed) — a 23MB ONNX model that runs locally, no API calls. First run downloads the model (~80MB including runtime). Subsequent runs are offline.

Embeddings are only generated for conversation content (user messages, assistant messages, Discord messages). Tool results and system entries are skipped — they'd just add noise.

### Hybrid Search (semantic_search)

`semantic_search` combines FTS5 keyword matching with vector similarity via Reciprocal Rank Fusion. Results are re-ranked by recency, session affinity, and content richness. Finds both exact matches and semantically similar content.

If embeddings aren't built yet, it falls back to keyword-only search automatically.

### Import Log (Incremental Indexing)

Every JSONL file is tracked with an MD5 hash. On restart or re-run:
- **Unchanged files** are skipped instantly
- **Modified files** (grew since last read) are re-indexed from the last position
- **New files** are indexed from scratch

First backfill of 31 sessions (55MB JSONL): **~2 seconds.** Re-run: **~0.3 seconds.**

### Concurrency

- **WAL mode** — readers never block writers, writers never block readers
- **Retry with backoff** — automatic retry on `SQLITE_BUSY`
- **Tail-read pattern** — reads JSONL files the harness is actively writing to, without locking

## Performance

Tested on a real 31-session corpus:

| Metric | Result |
|--------|--------|
| Backfill (31 files, 55MB) | ~2s |
| Re-backfill (skip unchanged) | ~0.3s |
| Entries indexed | 19,500+ |
| Embeddings (5,600 entries) | ~4 min first time, then incremental |
| DB size | ~32 MB (with embeddings) |
| Chronological query | <10ms |
| FTS5 search | <15ms |
| Semantic search (hybrid) | ~50ms |
| DB integrity | ✓ |

SQLite handles databases up to 281 TB. At projected 12-month scale (~700K entries, ~3GB raw), indexed queries remain under 1ms.

## CLI Reference

```bash
chimera-memory serve              # Run MCP server (stdio)
chimera-memory backfill           # Index all historical sessions
chimera-memory backfill --jsonl-dir <DIR> --persona <NAME> --client claude|codex
chimera-memory stats              # Show database statistics
chimera-memory split-db           # Split a shared transcript DB into per-persona DBs
chimera-memory codex doctor       # Diagnose Codex MCP setup without printing env values
chimera-memory enhance provider-plan --json
chimera-memory enhance enqueue --file <MEMORY_PATH>
chimera-memory enhance dry-run --persona <NAME>
chimera-memory enhance sidecar-run --endpoint http://127.0.0.1:8944/enhance
chimera-memory enhance serve-dry-run --port 8944
```

`backfill` accepts `--client claude|codex` to use the right parser for the JSONL flavor (Claude Code and Codex CLI write structurally different JSONL).

`split-db` is for splitting a multi-persona DB after the fact, useful if you started with one shared DB and want per-persona isolation.

`enhance` commands exercise the memory-enhancement sidecar pipeline without
requiring a model call. `provider-plan` shows the selected provider and budget
caps with credential refs hidden. `enqueue` queues an indexed memory file for
metadata enrichment. `dry-run` consumes queued jobs with deterministic local
metadata and keeps generated output review-gated: evidence-only, pending review,
not instruction-grade. `serve-dry-run` exposes the same deterministic behavior
over CM's HTTP sidecar contract for local integration tests. `sidecar-run`
processes queued jobs through a sidecar endpoint.

## Configuration

A config file is auto-generated on first run at `~/.chimera-memory/config.yaml`. Every option is commented with plain-English explanations.

Priority: **environment variables > config file > defaults**.

| Setting | Env Variable | Default |
|---------|--------------|---------|
| Database path | `TRANSCRIPT_DB_PATH` | `~/.chimera-memory/transcript.db` |
| JSONL directory | `TRANSCRIPT_JSONL_DIR` | Auto-detected from CWD |
| Memory root | `MEMORY_ROOT` | Auto-detected |
| Persona name | `TRANSCRIPT_PERSONA` | — |
| Client/parser | `CHIMERA_CLIENT` | Auto-detected / parser default |
| Retention (days) | `TRANSCRIPT_RETENTION_DAYS` | 90 |
| Max DB size (MB) | `TRANSCRIPT_MAX_DB_SIZE_MB` | 1024 |
| OMP thread cap | `OMP_NUM_THREADS` | System default |

## Database Schema

```sql
-- Session metadata
sessions (session_id, persona, title, git_branch, cwd,
          started_at, ended_at, exchange_count, disposition)

-- Transcript entries (full-text indexed)
transcript (session_id, entry_type, timestamp, content, persona,
            source, channel, chat_id, message_id, author, ...)

-- Transcript embeddings (separate table to keep base schema lean)
transcript_embeddings (transcript_id, embedding_blob, model)

-- Curated memory files
memory_files (id, path, relative_path, persona, fm_type, fm_importance,
              fm_status, fm_tags, fm_last_accessed, fm_access_count,
              fm_failure_count, ...)

-- Memory embeddings
memory_embeddings (file_id, embedding_blob, model)

-- Incremental indexing
import_log (file_path, file_hash, file_size, last_position, entries_imported)

-- Full-text search
transcript_fts (content)
memory_fts (content)
```

## Roadmap

### Phase 1 ✅ — Foundation
- [x] JSONL parser with content extraction and entry classification
- [x] SQLite schema (sessions, transcript, import_log)
- [x] FTS5 full-text search with Porter stemming
- [x] Content sanitization (secret/token redaction, injection detection)
- [x] Incremental indexing with MD5 hashes
- [x] WAL mode + retry with backoff
- [x] File watcher (watchdog + poll safety net)
- [x] MCP tools: recall, stats, backfill
- [x] CLI: serve, backfill, stats

### Phase 2 ✅ — Search & Session Intelligence
- [x] Progressive disclosure (`discord_recall_index` + `discord_detail`)
- [x] Session browser (`session_list`)
- [x] Retention consolidation
- [x] Auto-generated config file
- [x] Precomputed session summaries (zero LLM, deterministic)

### Phase 3 ✅ — Semantic Layer
- [x] Local embeddings (bge-small-en-v1.5 via fastembed, ~80MB, offline)
- [x] Hybrid search (FTS5 + vector via Reciprocal Rank Fusion)
- [x] Multi-signal re-ranking (recency, session affinity, content richness)
- [x] Pluggable parser interface (BaseParser ABC)

### Phase 4 ✅ — Cognitive Layer
- [x] Curated memory layer (markdown + YAML frontmatter, separate from transcripts)
- [x] Algorithmic memory decay (per-type exponential salience)
- [x] Surprise scoring (novelty via nearest-neighbor embeddings, no LLM)
- [x] Zone-based loading (CORE / ACTIVE / PASSIVE / ARCHIVE)
- [x] Access reinforcement (auto-boost on search/recall hits)
- [x] Failure marking (penalize memories that led to wrong advice)
- [x] Graph analysis (disconnected clusters, isolated files)
- [x] Memory guard (pre-write credential + injection scan)

### Phase 5 ✅ — Cross-Runtime
- [x] Codex CLI parser (separate from Claude Code parser)
- [x] Hermes Agent integration (memory provider plugin + MCP server)
- [x] PersonifyAgents installer handlers (deterministic per-runtime wiring)
- [x] `split-db` CLI for per-persona DB isolation

### Phase 6 — Future
- [ ] Claim extraction + contradiction detection
- [ ] Runtime context-aware zone scoring (currently uses neutral baseline)
- [ ] Encryption at rest
- [ ] Export (markdown / JSON / CSV)
- [ ] Conversation branch detection (harness rewinds)

## Compatibility

- **Python:** 3.10+
- **SQLite:** 3.35+ (ships with Python)
- **OS:** Windows, macOS, Linux, WSL
- **Harnesses:** Claude Code, Codex CLI, Hermes Agent (any version writing JSONL session files)
- **Dependencies:** `fastembed`, `watchdog`, `mcp`, `networkx` (for graph analysis), `pyyaml`

## Using Without the Curated Memory Layer

If you only want transcript search (no curated memory files), just skip the memory tools. The transcript layer works independently — no setup required beyond `backfill`. The curated memory layer is opt-in.

Set `MEMORY_ROOT=/dev/null` (or simply leave it unset) to tell the indexer there's no memory directory to watch.

## Related

- [PersonifyAgents](https://github.com/nexu-io/personifyagents) — Person-shaped agent platform built on top of ChimeraMemory. Uses the curated memory layer heavily. Provides the deterministic installer handlers that wire CM into any runtime.
- [ChimeraPersonas](https://github.com/ChimeraWerks/ChimeraPersonas) — Earlier opinionated persona system using ChimeraMemory's curated layer. PA is the successor.

## License

MIT
