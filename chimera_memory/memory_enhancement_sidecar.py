"""Dry-run HTTP sidecar for memory-enhancement contract testing."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .enhancement_worker import derive_dry_run_metadata
from .memory_enhancement import ENHANCEMENT_SCHEMA_VERSION, normalize_memory_enhancement_response

MAX_REQUEST_BYTES = 2_000_000


def build_dry_run_sidecar_response(invocation: Mapping[str, Any]) -> dict[str, Any]:
    """Build a successful sidecar response using deterministic local metadata."""
    started = time.perf_counter()
    request_payload = invocation.get("request") if isinstance(invocation.get("request"), Mapping) else {}
    metadata = normalize_memory_enhancement_response(
        derive_dry_run_metadata({"request_payload": dict(request_payload)})
    )
    encoded_metadata = json.dumps(metadata, separators=(",", ":"), sort_keys=True)
    wrapped = str(request_payload.get("wrapped_content") or "")
    return {
        "schema_version": ENHANCEMENT_SCHEMA_VERSION,
        "request_id": str(invocation.get("request_id") or ""),
        "status": "ok",
        "metadata": metadata,
        "diagnostics": {
            "model": "dry-run/deterministic-local",
            "input_chars": len(wrapped),
            "output_chars": len(encoded_metadata),
            "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
            "token_estimate": max(1, len(wrapped) // 4) if wrapped else 0,
            "rate_limit_bucket": "memory_enhancement",
        },
        "error": {
            "code": "",
            "message": "",
        },
    }


def build_dry_run_sidecar_error(code: str, *, status: str = "error") -> dict[str, Any]:
    """Build an error envelope without provider details or raw request content."""
    return {
        "schema_version": ENHANCEMENT_SCHEMA_VERSION,
        "request_id": "",
        "status": status,
        "metadata": {},
        "diagnostics": {
            "model": "dry-run/deterministic-local",
            "input_chars": 0,
            "output_chars": 0,
            "duration_ms": 0,
            "token_estimate": 0,
            "rate_limit_bucket": "memory_enhancement",
        },
        "error": {
            "code": code,
            "message": "",
        },
    }


def create_dry_run_sidecar_server(
    host: str = "127.0.0.1",
    port: int = 8944,
    *,
    bearer_token: str = "",
) -> ThreadingHTTPServer:
    """Create a dry-run HTTP server for the sidecar contract."""

    class Handler(DryRunMemoryEnhancementSidecarHandler):
        expected_bearer_token = bearer_token

    return ThreadingHTTPServer((host, port), Handler)


def run_dry_run_sidecar(
    host: str = "127.0.0.1",
    port: int = 8944,
    *,
    bearer_token: str = "",
) -> None:
    """Run the dry-run sidecar forever."""
    server = create_dry_run_sidecar_server(host, port, bearer_token=bearer_token)
    try:
        server.serve_forever()
    finally:
        server.server_close()


class DryRunMemoryEnhancementSidecarHandler(BaseHTTPRequestHandler):
    """HTTP handler for the deterministic memory-enhancement sidecar."""

    expected_bearer_token = ""

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler method
        if self.path != "/enhance":
            self._write_json(404, build_dry_run_sidecar_error("not_found"))
            return
        if self.expected_bearer_token:
            expected = f"Bearer {self.expected_bearer_token}"
            if self.headers.get("Authorization", "") != expected:
                self._write_json(401, build_dry_run_sidecar_error("auth_error"))
                return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write_json(400, build_dry_run_sidecar_error("parse_error"))
            return
        if content_length < 1 or content_length > MAX_REQUEST_BYTES:
            self._write_json(413, build_dry_run_sidecar_error("quota_exceeded"))
            return

        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_json(400, build_dry_run_sidecar_error("parse_error"))
            return
        if not isinstance(payload, dict):
            self._write_json(400, build_dry_run_sidecar_error("parse_error"))
            return

        self._write_json(200, build_dry_run_sidecar_response(payload))

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _write_json(self, status_code: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
