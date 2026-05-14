"""CLI entry point for chimera-memory."""

import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="chimera-memory",
        description="Index local agent session transcripts into queryable SQLite.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # serve: run MCP server
    subparsers.add_parser("serve", help="Run the MCP server (stdio transport)")

    # backfill: index all historical JSONL files
    sub_bf = subparsers.add_parser("backfill", help="Index all historical JSONL session files")
    sub_bf.add_argument("--jsonl-dir", help="Directory containing JSONL files")
    sub_bf.add_argument("--db", help="Path to transcript.db")
    sub_bf.add_argument("--persona", help="Persona name to tag entries with")
    sub_bf.add_argument("--client", help="Transcript client/parser to use, e.g. claude or codex")

    # stats: show database statistics
    sub_stats = subparsers.add_parser("stats", help="Show transcript database statistics")
    sub_stats.add_argument("--db", help="Path to transcript.db")

    # split-db: stage shared transcript DB into per-persona DBs
    sub_split = subparsers.add_parser("split-db", help="Split a shared transcript DB into per-persona DBs")
    sub_split.add_argument("--source", help="Source transcript.db path")
    sub_split.add_argument("--output-root", help="Root for per-persona DBs")
    sub_split.add_argument("--persona", action="append", help="Persona name to split; repeatable. Defaults to all discovered personas")
    sub_split.add_argument("--persona-id", action="append", help="Map persona to role/name id, e.g. sarah=researcher/sarah")
    sub_split.add_argument("--jsonl-dir", action="append", help="Map persona to JSONL dir for import_log filtering, e.g. sarah=~/.claude/projects/...")
    sub_split.add_argument("--apply", action="store_true", help="Write target DBs. Default is dry-run")
    sub_split.add_argument("--replace", action="store_true", help="Replace existing target DBs. Requires --apply")

    # codex: inspect Codex MCP wiring without exposing raw env values
    sub_codex = subparsers.add_parser("codex", help="Codex integration helpers")
    codex_subparsers = sub_codex.add_subparsers(dest="codex_command")
    sub_codex_doctor = codex_subparsers.add_parser("doctor", help="Check Codex MCP ChimeraMemory setup")
    sub_codex_doctor.add_argument("--config", help="Path to Codex mcp_servers.json")
    sub_codex_doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    sub_codex_template = codex_subparsers.add_parser("template", help="Print a safe Codex MCP config template")
    sub_codex_template.add_argument("--persona", required=True, help="Persona tag for indexed Codex transcripts")
    sub_codex_template.add_argument("--jsonl-dir", default="~/.codex/sessions/", help="Codex JSONL sessions directory")
    sub_codex_template.add_argument(
        "--command",
        dest="server_command",
        default="chimera-memory",
        help="Command Codex should spawn",
    )
    sub_codex_template.add_argument("--server-name", default="chimera-memory", help="MCP server name")
    sub_codex_template.add_argument("--persona-id", default="", help="Optional stable persona id, e.g. developer/asa")
    sub_codex_template.add_argument("--persona-name", default="", help="Optional display persona name")
    sub_codex_template.add_argument("--persona-root", default="", help="Optional persona root directory")
    sub_codex_template.add_argument("--personas-dir", default="", help="Optional personas directory")
    sub_codex_template.add_argument("--shared-root", default="", help="Optional shared memory/root directory")

    args = parser.parse_args()

    if args.command == "serve":
        from .server import main as serve_main
        serve_main()
    elif args.command == "backfill":
        _run_backfill(args)
    elif args.command == "stats":
        _run_stats(args)
    elif args.command == "split-db":
        _run_split_db(args)
    elif args.command == "codex":
        _run_codex(args)
    else:
        parser.print_help()
        sys.exit(1)


def _run_backfill(args):
    import logging
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(levelname)s | %(message)s")

    from .db import TranscriptDB
    from .indexer import Indexer
    from .server import get_default_db_path, get_default_jsonl_dir

    db_path = args.db or str(get_default_db_path())
    jsonl_dir = args.jsonl_dir or str(get_default_jsonl_dir())

    print(f"DB: {db_path}")
    print(f"JSONL dir: {jsonl_dir}")
    print()

    db = TranscriptDB(db_path)
    indexer = Indexer(db, jsonl_dir, persona=args.persona, parser_format=args.client)

    def progress(current, total):
        pct = (current / total * 100) if total else 0
        print(f"\r  [{current}/{total}] {pct:.0f}%", end="", flush=True)

    indexer.backfill(progress_callback=progress)
    print()

    stats = db.stats()
    print(f"Done. {stats['entry_count']:,} entries, {stats['session_count']} sessions, {stats['db_size_mb']:.1f} MB")


def _run_stats(args):
    from .db import TranscriptDB
    from .search import transcript_stats
    from .server import get_default_db_path

    db_path = args.db or str(get_default_db_path())
    db = TranscriptDB(db_path)
    stats = transcript_stats(db)

    print(f"Entries:    {stats['entry_count']:,}")
    print(f"Sessions:   {stats['session_count']}")
    print(f"DB Size:    {stats['db_size_mb']:.1f} MB")
    print(f"Last Entry: {stats.get('last_entry', 'none')}")
    print(f"Indexed:    {stats.get('files_indexed', 0)} files")
    print()
    if stats.get("entry_types"):
        print("Entry Types:")
        for etype, count in stats["entry_types"].items():
            print(f"  {etype}: {count:,}")
    if stats.get("sources"):
        print("Sources:")
        for source, count in stats["sources"].items():
            print(f"  {source}: {count:,}")


def _run_split_db(args):
    from .db_split import parse_mapping, results_to_json, split_db
    from .server import get_default_db_path

    if args.replace and not args.apply:
        print("--replace requires --apply", file=sys.stderr)
        sys.exit(2)

    try:
        persona_ids = parse_mapping(args.persona_id)
        jsonl_dirs = parse_mapping(args.jsonl_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    source = args.source or str(get_default_db_path())
    results = split_db(
        source,
        output_root=args.output_root,
        personas=args.persona,
        persona_ids=persona_ids,
        jsonl_dirs=jsonl_dirs,
        dry_run=not args.apply,
        replace=args.replace,
    )
    print(results_to_json(results))


def _run_codex(args):
    if args.codex_command == "doctor":
        from .codex_setup import format_codex_doctor_report, inspect_codex_mcp_config

        report = inspect_codex_mcp_config(args.config)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(format_codex_doctor_report(report))

        status = report.get("status")
        if status == "ok":
            sys.exit(0)
        if status == "warning":
            sys.exit(1)
        sys.exit(2)
    if args.codex_command == "template":
        from .codex_setup import build_codex_mcp_config

        config = build_codex_mcp_config(
            persona=args.persona,
            jsonl_dir=args.jsonl_dir,
            command=args.server_command,
            server_name=args.server_name,
            persona_id=args.persona_id,
            persona_name=args.persona_name,
            persona_root=args.persona_root,
            personas_dir=args.personas_dir,
            shared_root=args.shared_root,
        )
        print(json.dumps(config, indent=2))
        return

    print("Missing Codex command. Try: chimera-memory codex doctor", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
