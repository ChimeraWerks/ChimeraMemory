# Chimera Memory

**Perfect recall for coding agents.** Index Claude Code session transcripts into queryable SQLite — zero API calls, sub-millisecond search, works offline.

## What It Does

Claude Code writes detailed session logs (JSONL files) every time you use it. Chimera Memory watches those files, indexes everything into a local SQLite database, and gives you instant search and recall through MCP tools or the CLI.

```
Claude Code writes → JSONL files → Chimera Memory indexes → SQLite DB → You query
```

**Before:** To recall past conversations, you'd call Discord APIs, parse raw files, or just... not remember.

**After:** `discord_recall(search="umbrella")` — instant results from any session, any day.

## Quick Start

```bash
# Clone and install
git clone https://github.com/YourOrg/ChimeraMemory.git
cd ChimeraMemory
pip install .

# Index your existing sessions
chimera-memory backfill --jsonl-dir ~/.claude/projects/YOUR-PROJECT/

# Run as MCP server (add to your .mcp.json)
chimera-memory serve
```

### Add to Claude Code

Add this to your `.mcp.json`:

```json
{
  "mcpServers": {
    "chimera-memory": {
      "command": "chimera-memory",
      "args": ["serve"],
      "env": {
        "TRANSCRIPT_JSONL_DIR": "~/.claude/projects/YOUR-PROJECT/"
      }
    }
  }
}
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Your Machine                       │
│                                                      │
│  Claude Code ──writes──► JSONL Session Files          │
│                               │                      │
│                          ┌────▼────┐                 │
│                          │  File   │  watchdog +     │
│                          │ Watcher │  poll safety    │
│                          └────┬────┘                 │
│                               │ tail-read            │
│                          ┌────▼────┐                 │
│                          │ Parser  │  content        │
│                          │         │  extraction     │
│                          └────┬────┘                 │
│                               │ sanitize             │
│                          ┌────▼────┐                 │
│                          │ SQLite  │  WAL mode       │
│                          │   DB    │  FTS5 search    │
│                          └────┬────┘                 │
│                               │                      │
│              ┌────────────────┼────────────┐         │
│              │                │            │         │
│         MCP Tools          CLI         (Future)      │
│       discord_recall    search         GUI / API     │
│       transcript_stats  stats                        │
│       backfill          backfill                     │
└─────────────────────────────────────────────────────┘
```

## How It Works

### JSONL Parsing

Claude Code stores every session as a JSONL file — one JSON object per line. Each object contains user messages, assistant responses, tool calls, Discord messages, system events, and more.

The parser extracts and classifies each entry:

| Entry Type | What It Captures | Indexed Content |
|-----------|-----------------|-----------------|
| `discord_inbound` | Messages received from Discord | Full message text |
| `discord_outbound` | Messages sent to Discord | Full message text |
| `user_message` | CLI user input | Full text |
| `assistant_message` | Claude's responses | Full text (no thinking blocks) |
| `tool_call` | Tool invocations (Read, Bash, etc.) | Metadata only (tool name, input keys) |
| `tool_result` | Tool output | Metadata only (tool name, success/fail, size) |
| `system` | System events, notifications | Metadata only |

**Design choice:** Conversation content gets full-text indexed. Tool I/O gets metadata only. This keeps the database lean and search results relevant — you won't get 50 `tool_result` hits when you search for "umbrella."

### Content Sanitization

Before any content hits the database, it passes through a sanitizer that detects and redacts:

- API keys (`sk-ant-*`, `sk-*`, `ghp_*`, AWS keys)
- Bot tokens (Discord, Slack)
- Webhook URLs
- Bearer tokens
- Passwords and secrets in env-var format
- Private keys

Redacted content is replaced with `<REDACTED:type>` markers. The original content never touches disk.

### Search

Two search modes in one tool:

**Chronological** — "Show me the last 50 messages"
```
discord_recall(limit=50)
discord_recall(channel="123456", direction="inbound", limit=20)
discord_recall(after="2026-04-01", before="2026-04-05")
```

**Full-text search** — "Find conversations about X"
```
discord_recall(search="umbrella")
discord_recall(search="Japan hotel", channel="123456")
```

Full-text search uses SQLite FTS5 with Porter stemming — searching "research" also finds "researching," "researched," and "researcher."

### Import Log

Every JSONL file is tracked with an MD5 hash. On restart or re-run:
- **Unchanged files** are skipped instantly
- **Modified files** (grew since last read) are re-indexed
- **New files** are indexed from scratch

First backfill of 31 sessions (55MB of JSONL): **1.6 seconds.** Re-run: **0.2 seconds.**

### Concurrency

- **WAL mode** — readers never block writers, writers never block readers
- **Retry with backoff** — automatic retry on `SQLITE_BUSY` (3 attempts, exponential delay)
- **Tail-read pattern** — reads JSONL files that Claude Code is actively writing to, without locking or conflicts

## Performance

Tested against a real 31-session corpus:

| Metric | Result |
|--------|--------|
| Backfill (31 files, 55MB) | 1.6s |
| Re-backfill (skip unchanged) | 0.2s |
| Entries indexed | 18,900+ |
| DB size | 19 MB |
| Chronological query | 7ms |
| FTS5 search | <10ms |
| Batch insert (5,000 entries) | 0.37s |
| DB integrity | ✓ |

SQLite handles databases up to 281 TB. At projected 12-month scale (~700K entries, ~3GB), indexed queries remain under 1ms.

## MCP Tools

### Recall & Search

#### `discord_recall`
Full conversation recall with filters. Returns complete message content.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `channel` | string | — | Filter by Discord chat_id |
| `limit` | int | 50 | Max messages to return |
| `search` | string | — | Full-text search query |
| `after` | string | — | Messages after this ISO timestamp |
| `before` | string | — | Messages before this ISO timestamp |
| `direction` | string | — | `"inbound"` or `"outbound"` |
| `author` | string | — | Filter by author username |

#### `discord_recall_index`
**Token-efficient search.** Returns compact index (~100 tokens/result) with ID, timestamp, author, and 80-char preview. Use this first, then call `discord_detail` for entries you care about. Same parameters as `discord_recall`.

#### `discord_detail`
Fetch full content for specific entries by ID. Use after `discord_recall_index`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `ids` | list[int] | Entry IDs from the index results |

**Recommended workflow:**
1. `discord_recall_index(search="topic")` — scan previews (~100 tokens each)
2. Pick the IDs that look relevant
3. `discord_detail(ids=[...])` — get full content only for those

This saves 3-10x tokens compared to fetching everything at once.

### Sessions

#### `session_list`
Browse sessions with summaries, dispositions, and date ranges.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 20 | Max sessions to return |
| `after` | string | — | Sessions started after this timestamp |
| `before` | string | — | Sessions started before this timestamp |
| `persona` | string | — | Filter by persona name |
| `disposition` | string | — | `"COMPLETED"`, `"IN_PROGRESS"`, or `"INTERRUPTED"` |

### Database

#### `transcript_stats`
Database health check: entry count, session count, DB size, last entry timestamp, breakdowns by entry type, source, and session disposition.

#### `transcript_backfill`
Index all historical JSONL files. Safe to run multiple times — skips unchanged files automatically.

## CLI

```bash
chimera-memory serve              # Run MCP server (stdio)
chimera-memory backfill           # Index all historical sessions
chimera-memory backfill --persona sarah --jsonl-dir /path/to/sessions/
chimera-memory stats              # Show database statistics
```

## Configuration

A config file is auto-generated on first run at `~/.chimera-memory/config.yaml`. Every option is commented out with plain-English explanations. Uncomment what you want to change.

Priority order: **environment variables > config file > defaults**.

| Setting | Config Key | Env Variable | Default |
|---------|-----------|--------------|---------|
| Database path | — | `TRANSCRIPT_DB_PATH` | `~/.chimera-memory/transcript.db` |
| JSONL directory | `jsonl_dir` | `TRANSCRIPT_JSONL_DIR` | Auto-detected from CWD |
| Persona name | `persona` | `TRANSCRIPT_PERSONA` | — |
| Retention (days) | `retention_days` | `TRANSCRIPT_RETENTION_DAYS` | 90 |
| Max DB size (MB) | `max_db_size_mb` | `TRANSCRIPT_MAX_DB_SIZE_MB` | 1024 |
| Index tool calls | `index_tool_calls` | `TRANSCRIPT_INDEX_TOOL_CALLS` | true |
| Index tool results | `index_tool_results` | `TRANSCRIPT_INDEX_TOOL_RESULTS` | false |
| Progressive disclosure | `progressive_disclosure` | `TRANSCRIPT_PROGRESSIVE_DISCLOSURE` | true |
| Branch detection | `branch_detection` | `TRANSCRIPT_BRANCH_DETECTION` | false |

## Database Schema

```sql
-- One row per session file
sessions (session_id, persona, title, git_branch, cwd,
          started_at, ended_at, exchange_count, disposition)

-- Every indexed entry
transcript (session_id, entry_type, timestamp, content, persona,
            source, channel, chat_id, message_id, author, author_id,
            tool_name, conversation_id, source_refs, metadata)

-- File tracking for incremental indexing
import_log (file_path, file_hash, file_size, last_position,
            entries_imported)

-- Full-text search (FTS5, Porter stemming, external content)
transcript_fts (content)
```

## Roadmap

### Phase 1 ✅ — Foundation
- [x] JSONL parser with content extraction and entry type classification
- [x] SQLite schema with sessions, transcript, import_log tables
- [x] FTS5 full-text search with Porter stemming
- [x] Content sanitization (secret/token redaction)
- [x] Import log with MD5 file hashes (skip unchanged files)
- [x] WAL mode + retry with exponential backoff
- [x] File watcher (watchdog + periodic poll safety net)
- [x] Background backfill with progress reporting
- [x] MCP tools: `discord_recall`, `transcript_stats`, `transcript_backfill`
- [x] CLI: `serve`, `backfill`, `stats`

### Phase 2 ✅ — Search & Session Intelligence
- [x] Progressive disclosure (`discord_recall_index` + `discord_detail`, 3-10x token savings)
- [x] Precomputed session summaries (deterministic, zero LLM, greeting/command filtering)
- [x] Session browser (`session_list` MCP tool with date/persona/disposition filters)
- [x] Retention consolidation (compress old entries to permanent summaries, prune raw transcripts)
- [x] Auto-generated config file (`~/.chimera-memory/config.yaml` with commented defaults)
- [ ] Conversation branch detection (handle Claude Code rewinds)
- [ ] Export (markdown / JSON / CSV with filters)
- [ ] Event hooks (emitter pattern for "new entry indexed" triggers)

### Phase 3 — Semantic Layer
- [ ] Local embeddings (all-MiniLM-L6-v2 ONNX, no API calls)
- [ ] Hybrid search (BM25 + vector via Reciprocal Rank Fusion)
- [ ] Multi-signal re-ranking (recency, project affinity, graph proximity)
- [ ] Per-prompt semantic injection (automatic RAG on every message)
- [ ] Pluggable parsers (support non-Claude-Code session formats)

### Phase 4 — Cognitive Layer
- [ ] Algorithmic memory decay (per-type exponential salience)
- [ ] Surprise scoring (novelty detection without LLM calls)
- [ ] Zone-based memory loading (core / active / passive / archive)
- [ ] Claim extraction + contradiction detection
- [ ] Encryption at rest

## Compatibility

- **Python:** 3.10+
- **SQLite:** 3.35+ (ships with Python)
- **OS:** Windows, macOS, Linux
- **Claude Code:** Any version that writes JSONL session files
- **Dependencies:** `watchdog` (required), `mcp` (optional, for MCP server)

## Viewing in Obsidian

If you use Obsidian, you can browse your persona's memory files by pointing a vault at the memory directory. The markdown + YAML frontmatter format is natively compatible with Obsidian's graph view, backlinks, and Dataview queries. No configuration required.

## License

MIT
