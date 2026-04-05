"""JSONL indexer: import log, backfill, and file watching."""

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from .db import TranscriptDB
from .parser import parse_jsonl_file, extract_session_metadata
from .sanitizer import sanitize_content

log = logging.getLogger(__name__)

BATCH_SIZE = 500


def get_file_hash(filepath: Path) -> str:
    """Compute MD5 hash of a file (chunked for large files)."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


class Indexer:
    """Indexes JSONL session files into the transcript database."""

    def __init__(self, db: TranscriptDB, jsonl_dir: str | Path, persona: str | None = None):
        self.db = db
        self.jsonl_dir = Path(jsonl_dir)
        self.persona = persona
        self._stop_event = threading.Event()
        self._watcher_thread = None
        self._poll_thread = None

    def backfill(self, progress_callback: Callable[[int, int], None] | None = None):
        """Index all historical JSONL files. Skips unchanged files via import log.

        Args:
            progress_callback: Called with (files_processed, total_files)
        """
        jsonl_files = sorted(self.jsonl_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        total = len(jsonl_files)

        if total == 0:
            log.info("No JSONL files found in %s", self.jsonl_dir)
            return

        log.info("Backfilling %d JSONL files from %s", total, self.jsonl_dir)

        with self.db.bulk_connection() as conn:
            # Disable FTS triggers for bulk performance
            self.db.disable_fts_triggers(conn)

            for i, path in enumerate(jsonl_files):
                self._index_file(path, conn, is_backfill=True)
                if progress_callback:
                    progress_callback(i + 1, total)

            # Rebuild FTS index after all imports
            log.info("Rebuilding FTS index...")
            self.db.rebuild_fts(conn)

        log.info("Backfill complete: %d files processed", total)

    def index_file(self, path: Path):
        """Index a single JSONL file (for real-time use, with FTS triggers active)."""
        with self.db.connection() as conn:
            self._index_file(path, conn, is_backfill=False)
            conn.commit()

    def _index_file(self, path: Path, conn, is_backfill: bool = False):
        """Core file indexing logic with import log check."""
        file_path_str = str(path.resolve())
        file_hash = get_file_hash(path)
        file_size = path.stat().st_size

        # Check import log
        row = conn.execute(
            "SELECT file_hash, last_position FROM import_log WHERE file_path = ?",
            (file_path_str,),
        ).fetchone()

        if row:
            if row["file_hash"] == file_hash:
                # File unchanged, skip
                return
            # File changed (grew). Read from last position for tail-read,
            # or from 0 for backfill (full re-parse).
            start_offset = 0 if is_backfill else (row["last_position"] or 0)
        else:
            start_offset = 0

        # Extract session metadata
        session_meta = extract_session_metadata(path)
        session_meta["persona"] = self.persona
        self.db.upsert_session(session_meta, conn)

        # Parse entries
        entries = []
        final_pos = start_offset
        for entry in parse_jsonl_file(path, start_offset=start_offset):
            if isinstance(entry, int):
                # Generator returned final position
                final_pos = entry
                continue

            # Sanitize content before indexing
            if entry.get("content"):
                entry["content"] = sanitize_content(entry["content"])

            # Add persona
            entry["persona"] = self.persona

            entries.append(entry)

            # Batch insert
            if len(entries) >= BATCH_SIZE:
                self.db.insert_entries(entries, conn)
                conn.commit()
                entries = []

        # Insert remaining
        if entries:
            self.db.insert_entries(entries, conn)
            conn.commit()

        # Update import log
        self.db.execute_with_retry(
            conn,
            """INSERT INTO import_log (file_path, file_hash, file_size, last_position, entries_imported, updated_at)
               VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
               ON CONFLICT(file_path) DO UPDATE SET
                   file_hash = excluded.file_hash,
                   file_size = excluded.file_size,
                   last_position = excluded.last_position,
                   entries_imported = import_log.entries_imported + excluded.entries_imported,
                   updated_at = excluded.updated_at""",
            (file_path_str, file_hash, file_size, file_size, len(entries)),
        )
        conn.commit()

        log.info("Indexed %s: %d entries (offset %d -> %d)", path.name, len(entries), start_offset, file_size)

    def tail_file(self, path: Path):
        """Tail-read new content from an active JSONL file."""
        file_path_str = str(path.resolve())

        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT last_position FROM import_log WHERE file_path = ?",
                (file_path_str,),
            ).fetchone()

        current_size = path.stat().st_size
        last_pos = row["last_position"] if row else 0

        if current_size <= last_pos:
            return  # No new data

        # Parse new content from last position
        entries = []
        with open(path, "r", encoding="utf-8") as f:
            f.seek(last_pos)
            new_data = f.read()
            lines = new_data.split("\n")
            # Last line might be partial (still being written)
            partial = lines.pop()

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Re-use the parse logic by calling the generator
                # This is a simplified inline version for tail reads
                session_id = obj.get("sessionId", path.stem)
                timestamp = obj.get("timestamp", "")
                obj_type = obj.get("type", "")

                if obj_type in ("file-history-snapshot", "custom-title", "agent-name", "permission-mode"):
                    continue
                if obj.get("isMeta"):
                    continue

                for entry in _parse_single_entry(obj, session_id, timestamp):
                    if entry.get("content"):
                        entry["content"] = sanitize_content(entry["content"])
                    entry["persona"] = self.persona
                    entries.append(entry)

            # Calculate new position (exclude partial line)
            new_pos = current_size - len(partial.encode("utf-8"))

        if entries:
            with self.db.connection() as conn:
                self.db.insert_entries(entries, conn)
                conn.commit()

        # Update position in import log
        file_hash = get_file_hash(path)
        with self.db.connection() as conn:
            self.db.execute_with_retry(
                conn,
                """INSERT INTO import_log (file_path, file_hash, file_size, last_position, entries_imported, updated_at)
                   VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                   ON CONFLICT(file_path) DO UPDATE SET
                       file_hash = excluded.file_hash,
                       file_size = excluded.file_size,
                       last_position = excluded.last_position,
                       entries_imported = import_log.entries_imported + excluded.entries_imported,
                       updated_at = excluded.updated_at""",
                (file_path_str, file_hash, current_size, new_pos, len(entries)),
            )
            conn.commit()

        if entries:
            log.debug("Tailed %s: %d new entries", path.name, len(entries))

    def start_watching(self, poll_interval: float = 30.0):
        """Start file watching with watchdog + periodic poll safety net."""
        self._stop_event.clear()

        # Try watchdog first
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class _Handler(FileSystemEventHandler):
                def __init__(self, indexer):
                    self.indexer = indexer

                def on_modified(self, event):
                    if event.is_directory:
                        return
                    path = Path(event.src_path)
                    if path.suffix == ".jsonl":
                        try:
                            self.indexer.tail_file(path)
                        except Exception:
                            log.exception("Error tailing %s", path)

                def on_created(self, event):
                    if event.is_directory:
                        return
                    path = Path(event.src_path)
                    if path.suffix == ".jsonl":
                        try:
                            self.indexer.index_file(path)
                        except Exception:
                            log.exception("Error indexing new file %s", path)

            observer = Observer()
            observer.schedule(_Handler(self), str(self.jsonl_dir), recursive=False)
            observer.start()
            log.info("Watchdog file watcher started on %s", self.jsonl_dir)

        except ImportError:
            log.warning("watchdog not installed, using poll-only mode")
            observer = None

        # Periodic poll safety net (catches anything watchdog missed)
        def _poll_loop():
            while not self._stop_event.is_set():
                self._stop_event.wait(poll_interval)
                if self._stop_event.is_set():
                    break
                try:
                    self._poll_for_changes()
                except Exception:
                    log.exception("Error in poll loop")

        self._poll_thread = threading.Thread(target=_poll_loop, daemon=True, name="transcript-poll")
        self._poll_thread.start()

        return observer

    def stop_watching(self):
        """Stop the file watcher and poll thread."""
        self._stop_event.set()

    def _poll_for_changes(self):
        """Check all JSONL files for changes not caught by watchdog."""
        for path in self.jsonl_dir.glob("*.jsonl"):
            file_path_str = str(path.resolve())
            current_size = path.stat().st_size

            with self.db.connection() as conn:
                row = conn.execute(
                    "SELECT file_size FROM import_log WHERE file_path = ?",
                    (file_path_str,),
                ).fetchone()

            last_size = row["file_size"] if row else 0
            if current_size > last_size:
                self.tail_file(path)


def _parse_single_entry(obj: dict, session_id: str, timestamp: str):
    """Parse a single JSONL object into transcript entries (for tail-read use)."""
    # Import here to avoid circular dependency
    from .parser import _parse_user_entry, _parse_assistant_entry, _parse_system_entry, _parse_queue_operation, _make_entry

    obj_type = obj.get("type", "")

    if obj_type == "user":
        yield from _parse_user_entry(obj, session_id, timestamp)
    elif obj_type == "assistant":
        yield from _parse_assistant_entry(obj, session_id, timestamp)
    elif obj_type == "system":
        yield from _parse_system_entry(obj, session_id, timestamp)
    elif obj_type == "queue-operation":
        yield from _parse_queue_operation(obj, session_id, timestamp)
    elif obj_type == "attachment":
        yield _make_entry(
            session_id=session_id,
            entry_type="attachment",
            timestamp=timestamp,
            content=json.dumps(obj.get("attachment", {})),
            source="cli",
            metadata={"uuid": obj.get("uuid")},
        )
