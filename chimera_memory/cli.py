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

    # enhance: memory-enhancement queue and dry-run helpers
    sub_enhance = subparsers.add_parser("enhance", help="Memory enhancement sidecar helpers")
    enhance_subparsers = sub_enhance.add_subparsers(dest="enhance_command")
    sub_enhance_plan = enhance_subparsers.add_parser("provider-plan", help="Show safe provider-resolution plan")
    sub_enhance_plan.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    sub_enhance_enqueue = enhance_subparsers.add_parser("enqueue", help="Queue an indexed memory file for enhancement")
    sub_enhance_enqueue.add_argument("--db", help="Path to transcript.db")
    sub_enhance_enqueue.add_argument("--file", required=True, help="Indexed memory file path or relative path")
    sub_enhance_enqueue.add_argument("--provider", default="", help="Requested provider hint")
    sub_enhance_enqueue.add_argument("--model", default="", help="Requested model hint")
    sub_enhance_enqueue.add_argument("--force", action="store_true", help="Supersede an existing pending/running job")
    sub_enhance_enqueue.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    sub_enhance_dry_run = enhance_subparsers.add_parser("dry-run", help="Process queued jobs with deterministic local metadata")
    sub_enhance_dry_run.add_argument("--db", help="Path to transcript.db")
    sub_enhance_dry_run.add_argument("--persona", help="Only process jobs for this persona")
    sub_enhance_dry_run.add_argument("--limit", type=int, default=10, help="Maximum jobs to process")
    sub_enhance_dry_run.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    sub_enhance_sidecar_run = enhance_subparsers.add_parser("sidecar-run", help="Process queued jobs through an HTTP sidecar")
    sub_enhance_sidecar_run.add_argument("--db", help="Path to transcript.db")
    sub_enhance_sidecar_run.add_argument("--endpoint", required=True, help="Sidecar endpoint URL")
    sub_enhance_sidecar_run.add_argument("--persona", help="Only process jobs for this persona")
    sub_enhance_sidecar_run.add_argument("--limit", type=int, default=10, help="Maximum jobs to process")
    sub_enhance_sidecar_run.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    sub_enhance_sidecar_run.add_argument("--token-env", default="", help="Optional env var containing bearer token")
    sub_enhance_sidecar_run.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    sub_enhance_sidecar = enhance_subparsers.add_parser("serve-dry-run", help="Run a deterministic local enhancement sidecar")
    sub_enhance_sidecar.add_argument("--host", default="127.0.0.1", help="Bind host")
    sub_enhance_sidecar.add_argument("--port", type=int, default=8944, help="Bind port")
    sub_enhance_sidecar.add_argument("--token-env", default="", help="Optional env var containing bearer token")
    sub_enhance_provider_sidecar = enhance_subparsers.add_parser("serve-provider", help="Run a provider-backed enhancement sidecar")
    sub_enhance_provider_sidecar.add_argument("--host", default="127.0.0.1", help="Bind host")
    sub_enhance_provider_sidecar.add_argument("--port", type=int, default=8944, help="Bind port")
    sub_enhance_provider_sidecar.add_argument("--token-env", default="", help="Optional env var containing sidecar HTTP bearer token")
    sub_enhance_provider_sidecar.add_argument("--provider-token-env", default="", help="Optional env var containing the selected model provider token")

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
    elif args.command == "enhance":
        _run_enhance(args)
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


def _open_memory_db(db_path: str | None):
    import sqlite3

    from .memory import init_memory_tables
    from .server import get_default_db_path

    path = db_path or str(get_default_db_path())
    conn = sqlite3.connect(path)
    init_memory_tables(conn)
    return conn


def _emit_json_or_lines(payload: object, *, json_output: bool, lines: list[str]) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for line in lines:
        print(line)


def _run_enhance(args):
    if args.enhance_command == "provider-plan":
        import os

        from .memory_enhancement_provider import resolve_enhancement_provider_plan, safe_provider_receipt

        receipt = safe_provider_receipt(resolve_enhancement_provider_plan(os.environ))
        selected = receipt["selected_provider"]
        model = receipt["selected_model"]
        _emit_json_or_lines(
            receipt,
            json_output=args.json,
            lines=[
                f"Selected provider: {selected}",
                f"Selected model:    {model}",
                "Credential refs:   hidden; only presence is reported in JSON mode",
            ],
        )
        return

    if args.enhance_command == "enqueue":
        from .memory import memory_enhancement_enqueue

        conn = _open_memory_db(args.db)
        try:
            result = memory_enhancement_enqueue(
                conn,
                file_path=args.file,
                requested_provider=args.provider,
                requested_model=args.model,
                force=args.force,
            )
        finally:
            conn.close()

        if not result.get("ok"):
            _emit_json_or_lines(
                result,
                json_output=args.json,
                lines=[f"Enhancement enqueue failed: {result.get('error', 'unknown error')}"],
            )
            sys.exit(2)

        job = result.get("job") or {}
        action = "Enqueued" if result.get("enqueued") else "Already queued"
        _emit_json_or_lines(
            result,
            json_output=args.json,
            lines=[
                f"{action} enhancement job: {job.get('job_id', '')}",
                f"Status: {job.get('status', '')}",
                f"Persona: {job.get('persona', '')}",
            ],
        )
        return

    if args.enhance_command == "dry-run":
        from .enhancement_worker import run_memory_enhancement_dry_run

        conn = _open_memory_db(args.db)
        try:
            processed = run_memory_enhancement_dry_run(conn, persona=args.persona, limit=args.limit)
        finally:
            conn.close()

        payload = {
            "processed_count": len(processed),
            "processed": processed,
        }
        _emit_json_or_lines(
            payload,
            json_output=args.json,
            lines=[f"Processed enhancement jobs: {len(processed)}"],
        )
        return

    if args.enhance_command == "sidecar-run":
        import os

        from .memory_enhancement_http_client import MemoryEnhancementHttpClient
        from .memory_enhancement_runner import run_memory_enhancement_provider_batch

        bearer_token = ""
        if args.token_env:
            bearer_token = os.environ.get(args.token_env, "")
            if not bearer_token:
                print("Bearer token env var is not set", file=sys.stderr)
                sys.exit(2)
        client = MemoryEnhancementHttpClient(
            args.endpoint,
            bearer_token=bearer_token,
            timeout_seconds=args.timeout,
        )
        conn = _open_memory_db(args.db)
        try:
            receipt = run_memory_enhancement_provider_batch(
                conn,
                client=client,
                persona=args.persona,
                limit=args.limit,
            )
        finally:
            conn.close()

        _emit_json_or_lines(
            receipt,
            json_output=args.json,
            lines=[
                f"Processed enhancement jobs: {receipt['processed_count']}",
                f"Failed enhancement jobs: {receipt['failure_count']}",
            ],
        )
        return

    if args.enhance_command == "serve-dry-run":
        import os

        from .memory_enhancement_sidecar import run_dry_run_sidecar

        bearer_token = ""
        if args.token_env:
            bearer_token = os.environ.get(args.token_env, "")
            if not bearer_token:
                print("Bearer token env var is not set", file=sys.stderr)
                sys.exit(2)
        print(f"Dry-run memory enhancement sidecar listening on http://{args.host}:{args.port}/enhance")
        run_dry_run_sidecar(args.host, args.port, bearer_token=bearer_token)
        return

    if args.enhance_command == "serve-provider":
        import os

        from .memory_enhancement_model_client import ProviderModelMemoryEnhancementClient
        from .memory_enhancement_sidecar import run_provider_sidecar

        bearer_token = ""
        if args.token_env:
            bearer_token = os.environ.get(args.token_env, "")
            if not bearer_token:
                print("Sidecar bearer token env var is not set", file=sys.stderr)
                sys.exit(2)
        provider_token = ""
        if args.provider_token_env:
            provider_token = os.environ.get(args.provider_token_env, "")
            if not provider_token:
                print("Provider token env var is not set", file=sys.stderr)
                sys.exit(2)
        print(f"Provider memory enhancement sidecar listening on http://{args.host}:{args.port}/enhance")
        run_provider_sidecar(
            args.host,
            args.port,
            client=ProviderModelMemoryEnhancementClient(bearer_token=provider_token),
            bearer_token=bearer_token,
        )
        return

    print("Missing enhance command. Try: chimera-memory enhance provider-plan", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
