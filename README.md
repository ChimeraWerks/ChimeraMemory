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

### `discord_recall`

Query conversation history from indexed transcripts.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `channel` | string | — | Filter by Discord chat_id |
| `limit` | int | 50 | Max messages to return |
| `search` | string | — | Full-text search query |
| `after` | string | — | Messages after this ISO timestamp |
| `before` | string | — | Messages before this ISO timestamp |
| `direction` | string | — | `"inbound"` or `"outbound"` |
| `author` | string | — | Filter by author username |

### `transcript_stats`

Database health check: entry count, session count, DB size, last entry timestamp, breakdowns by type and source.

### `transcript_backfill`

Index all historical JSONL files. Safe to run multiple times — skips unchanged files automatically.

## CLI

```bash
chimera-memory serve              # Run MCP server (stdio)
chimera-memory backfill           # Index all historical sessions
chimera-memory backfill --persona sarah --jsonl-dir /path/to/sessions/
chimera-memory stats              # Show database statistics
```

## Configuration

| Environment Variable | Description | Default |
|---------------------|-------------|---------|
| `TRANSCRIPT_DB_PATH` | Path to SQLite database | `~/.chimera-memory/transcript.db` |
| `TRANSCRIPT_JSONL_DIR` | Directory containing JSONL session files | Auto-detected from CWD |
| `TRANSCRIPT_PERSONA` | Persona name to tag entries with | — |

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

### Phase 2 — Search & Session Intelligence
- [ ] Progressive disclosure (return summaries first, full content on demand)
- [ ] Precomputed session summaries (deterministic, no LLM)
- [ ] Conversation branch detection (handle Claude Code rewinds)
- [ ] Export (markdown / JSON / CSV with filters)
- [ ] Event hooks (emitter pattern for "new entry indexed" triggers)
- [ ] CLI: `search`, `export`, `recent`

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
