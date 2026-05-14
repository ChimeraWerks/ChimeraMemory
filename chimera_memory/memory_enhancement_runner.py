"""Provider-aware runner boundary for memory-enhancement jobs.

The runner accepts an injected client. CM does not resolve raw OAuth tokens or
perform provider-specific network calls here.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .memory_enhancement_provider import (
    EnhancementProviderPlan,
    build_enhancement_invocation,
    classify_enhancement_failure,
    resolve_enhancement_provider_plan,
    safe_provider_receipt,
)
from .memory_enhancement_queue import (
    memory_enhancement_claim_next,
    memory_enhancement_complete,
)


class MemoryEnhancementClient(Protocol):
    """Client interface supplied by a host application or sidecar adapter."""

    def invoke(self, invocation: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return provider metadata for one invocation."""


def _safe_failure_payload(category: str, plan: EnhancementProviderPlan, job: Mapping[str, Any]) -> dict:
    return {
        "failure_category": category,
        "provider_id": plan.selected.provider_id,
        "model": plan.selected.model,
        "job_id": job.get("job_id"),
    }


def run_memory_enhancement_provider_batch(
    conn: sqlite3.Connection,
    *,
    client: MemoryEnhancementClient,
    env: Mapping[str, str] | None = None,
    persona: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run pending enhancement jobs through an injected provider client.

    The returned receipt is safe to log: it contains provider names, models,
    budget caps, and job ids, but no raw content and no credential values.
    """
    plan = resolve_enhancement_provider_plan(env or os.environ)
    max_jobs = plan.budget.max_jobs_per_run if limit is None else min(limit, plan.budget.max_jobs_per_run)
    max_jobs = max(0, max_jobs)
    processed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for _ in range(max_jobs):
        job = memory_enhancement_claim_next(conn, persona=persona)
        if job is None:
            break

        invocation = build_enhancement_invocation(job.get("request_payload") or {}, plan)
        try:
            response_payload = dict(client.invoke(invocation))
            result = memory_enhancement_complete(
                conn,
                job_id=str(job["job_id"]),
                status="succeeded",
                response_payload=response_payload,
            )
            if result.get("ok"):
                processed.append(
                    {
                        "job_id": job["job_id"],
                        "status": "succeeded",
                        "provider_id": plan.selected.provider_id,
                        "model": plan.selected.model,
                    }
                )
                continue
            category = "unknown_error"
        except Exception as exc:
            category = classify_enhancement_failure(str(exc))

        failure_payload = _safe_failure_payload(category, plan, job)
        memory_enhancement_complete(
            conn,
            job_id=str(job["job_id"]),
            status="failed",
            response_payload=failure_payload,
            error=category,
        )
        failures.append(
            {
                "job_id": job["job_id"],
                "status": "failed",
                "failure_category": category,
                "provider_id": plan.selected.provider_id,
                "model": plan.selected.model,
            }
        )

    return {
        "provider": safe_provider_receipt(plan),
        "processed": processed,
        "failures": failures,
        "processed_count": len(processed),
        "failure_count": len(failures),
    }


class StaticMemoryEnhancementClient:
    """Deterministic test client for host-side runner wiring."""

    def __init__(self, responses: Sequence[Mapping[str, Any]]):
        self._responses = list(responses)
        self.invocations: list[Mapping[str, Any]] = []

    def invoke(self, invocation: Mapping[str, Any]) -> Mapping[str, Any]:
        self.invocations.append(invocation)
        if not self._responses:
            return {}
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
