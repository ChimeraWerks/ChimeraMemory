from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .memory_enhancement_credentials import (
    EnvMemoryEnhancementCredentialResolver,
    MemoryEnhancementCredentialRef,
    MemoryEnhancementCredentialResolutionError,
    MemoryEnhancementCredentialResolver,
    ProtocolValidationError,
)
from .memory_enhancement_oauth import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_OAUTH,
    MemoryEnhancementOAuthCredential,
    MemoryEnhancementOAuthStore,
    MemoryEnhancementPooledCredential,
    OAuthMemoryEnhancementCredentialResolver,
)
from .memory_enhancement_google import GOOGLE_CLOUDCODE_MEMORY_DEFAULT_MODEL, google_cloudcode_model_candidates


OPENAI_CODEX_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
ANTHROPIC_OAUTH_ENDPOINT = "https://api.anthropic.com/v1/messages"
GOOGLE_CLOUDCODE_ENDPOINT = "https://cloudcode-pa.googleapis.com/v1internal:generateContent"
GOOGLE_CLOUDCODE_FREE_TIER_ID = "free-tier"

OPENAI_CODEX_HEADERS = {
    "User-Agent": "codex_cli_rs/0.0.0 (ChimeraMemory)",
    "originator": "codex_cli_rs",
}
ANTHROPIC_COMMON_BETAS = (
    "interleaved-thinking-2025-05-14",
    "fine-grained-tool-streaming-2025-05-14",
)
ANTHROPIC_OAUTH_ONLY_BETAS = (
    "claude-code-20250219",
    "oauth-2025-04-20",
)
CLAUDE_CODE_VERSION_FALLBACK = "2.1.74"
ANTHROPIC_OAUTH_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": ",".join((*ANTHROPIC_COMMON_BETAS, *ANTHROPIC_OAUTH_ONLY_BETAS)),
    "x-app": "cli",
    "user-agent": f"claude-cli/{CLAUDE_CODE_VERSION_FALLBACK} (external, cli)",
}
GOOGLE_CLOUDCODE_HEADERS = {
    "User-Agent": "hermes-agent (gemini-cli-compat)",
    "X-Goog-Api-Client": "gl-python/hermes",
}
GOOGLE_CLOUDCODE_DISCOVERY_HEADERS = {
    "User-Agent": "google-api-nodejs-client/9.15.1 (gzip)",
    "X-Goog-Api-Client": "gl-node/24.0.0",
}


class ResolvingMemoryEnhancementProviderClient:
    """Provider client that resolves credential refs for each invocation."""

    def __init__(
        self,
        *,
        credential_resolver: MemoryEnhancementCredentialResolver | None = None,
        oauth_resolver: OAuthMemoryEnhancementCredentialResolver | None = None,
        api_key_client_factory: Callable[[str], Any] | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._credential_resolver = credential_resolver or EnvMemoryEnhancementCredentialResolver(os.environ)
        self._oauth_resolver = oauth_resolver or OAuthMemoryEnhancementCredentialResolver()
        self._api_key_client_factory = api_key_client_factory or _default_api_key_client
        self._opener = opener

    def invoke(self, invocation: Mapping[str, Any]) -> Mapping[str, Any]:
        provider = _provider(invocation)
        provider_id = provider["provider_id"]
        credential_ref = _credential_ref(provider)
        if not credential_ref:
            return self._api_key_client_factory("").invoke(invocation)
        ref = MemoryEnhancementCredentialRef.parse(credential_ref)
        if ref.scheme == "oauth":
            credential = self._oauth_resolver.resolve_oauth(ref, provider_id=_oauth_provider_id(provider_id))
            return self._invoke_with_pooled_failover(invocation, provider, credential)
        if ref.scheme == "secret":
            pooled = self._resolve_pooled_api_key(ref, provider_id=provider_id)
            if pooled:
                return self._invoke_api_key_with_pooled_failover(invocation, provider, pooled)
        resolved = self._credential_resolver.resolve(ref)
        return self._api_key_client_factory(resolved.value).invoke(invocation)

    def _resolve_pooled_api_key(
        self,
        ref: MemoryEnhancementCredentialRef,
        *,
        provider_id: str,
    ):
        store = self._oauth_resolver.store or MemoryEnhancementOAuthStore()
        try:
            credential = store.get_pooled(ref.name, provider_id=provider_id)
        except (MemoryEnhancementCredentialResolutionError, ProtocolValidationError):
            return None
        if credential.auth_type != AUTH_TYPE_API_KEY:
            return None
        return credential

    def _credential_store(self) -> MemoryEnhancementOAuthStore:
        return self._oauth_resolver.store or MemoryEnhancementOAuthStore()

    def _invoke_with_pooled_failover(
        self,
        invocation: Mapping[str, Any],
        provider: Mapping[str, str],
        credential: MemoryEnhancementOAuthCredential,
    ) -> Mapping[str, Any]:
        current = credential
        attempted: set[str] = set()
        while True:
            attempted.add(current.name)
            try:
                return self._invoke_oauth(invocation, provider, current)
            except RuntimeError as exc:
                context = _pool_exhaustion_context(exc)
                if context is None:
                    raise
                next_credential = self._credential_store().mark_pooled_exhausted(
                    current.name,
                    provider_id=current.provider_id,
                    status_code=context["status_code"],
                    reason=context["reason"],
                    message=context["message"],
                    reset_at=context["reset_at"],
                )
                if next_credential is None or next_credential.id in attempted or next_credential.auth_type != AUTH_TYPE_OAUTH:
                    raise
                current = self._oauth_resolver.resolve_oauth(
                    next_credential.ref,
                    provider_id=current.provider_id,
                )

    def _invoke_api_key_with_pooled_failover(
        self,
        invocation: Mapping[str, Any],
        provider: Mapping[str, str],
        credential: MemoryEnhancementPooledCredential,
    ) -> Mapping[str, Any]:
        current = credential
        attempted: set[str] = set()
        while True:
            attempted.add(current.id)
            try:
                return self._api_key_client_factory(current.access_token).invoke(invocation)
            except RuntimeError as exc:
                context = _pool_exhaustion_context(exc)
                if context is None:
                    raise
                next_credential = self._credential_store().mark_pooled_exhausted(
                    current.id,
                    provider_id=current.provider_id,
                    status_code=context["status_code"],
                    reason=context["reason"],
                    message=context["message"],
                    reset_at=context["reset_at"],
                )
                if next_credential is None or next_credential.id in attempted or next_credential.auth_type != AUTH_TYPE_API_KEY:
                    raise
                current = next_credential

    def _invoke_oauth(
        self,
        invocation: Mapping[str, Any],
        provider: Mapping[str, str],
        credential: MemoryEnhancementOAuthCredential,
    ) -> Mapping[str, Any]:
        provider_id = provider["provider_id"]
        if provider_id == "openai" and credential.transport == "openai_codex":
            return _invoke_openai_codex(invocation, provider, credential, opener=self._opener)
        if provider_id == "anthropic" and credential.transport == "anthropic_oauth":
            try:
                return _invoke_anthropic_oauth(invocation, provider, credential, opener=self._opener)
            except RuntimeError as exc:
                if str(exc) != "memory enhancement provider auth failed":
                    raise
                ref = MemoryEnhancementCredentialRef.parse(_credential_ref(provider))
                refreshed = self._oauth_resolver.resolve_oauth(
                    ref,
                    provider_id="anthropic",
                    force_refresh=True,
                )
                return _invoke_anthropic_oauth(invocation, provider, refreshed, opener=self._opener)
        if provider_id == "google" and credential.transport == "google_cloudcode":
            return _invoke_google_cloudcode(
                invocation,
                provider,
                credential,
                store=self._credential_store(),
                opener=self._opener,
            )
        raise RuntimeError("memory enhancement provider oauth transport unavailable")


def run_provider_sidecar(
    *,
    host: str = "127.0.0.1",
    port: int = 8944,
    bearer_token: str = "",
    credential_resolver: MemoryEnhancementCredentialResolver | None = None,
    oauth_resolver: OAuthMemoryEnhancementCredentialResolver | None = None,
) -> None:
    from chimera_memory.memory_enhancement_sidecar import run_provider_sidecar as run_cm_provider_sidecar

    run_cm_provider_sidecar(
        host=host,
        port=port,
        bearer_token=bearer_token,
        client=ResolvingMemoryEnhancementProviderClient(
            credential_resolver=credential_resolver,
            oauth_resolver=oauth_resolver,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the PA memory-enhancement provider sidecar.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8944)
    parser.add_argument("--token-env", default="")
    args = parser.parse_args(argv)

    bearer_token = os.environ.get(args.token_env, "") if args.token_env else ""
    print(f"Provider memory enhancement sidecar listening on http://{args.host}:{args.port}/enhance")
    run_provider_sidecar(host=args.host, port=args.port, bearer_token=bearer_token)
    return 0


def _default_api_key_client(token: str) -> Any:
    from chimera_memory.memory_enhancement_model_client import ProviderModelMemoryEnhancementClient

    return ProviderModelMemoryEnhancementClient(bearer_token=token)


def _provider(invocation: Mapping[str, Any]) -> dict[str, str]:
    raw = invocation.get("provider") if isinstance(invocation.get("provider"), Mapping) else {}
    provider_id = str(raw.get("provider_id") or "").strip()
    model = str(raw.get("model") or "").strip()
    endpoint = str(raw.get("endpoint") or "").strip()
    credential_ref = str(raw.get("credential_ref") or "").strip()
    if provider_id not in {"openai", "anthropic", "google", "openrouter", "ollama", "lmstudio", "openai_compatible", "dry_run"}:
        raise RuntimeError("memory enhancement provider unavailable")
    if not model:
        raise RuntimeError("memory enhancement provider unavailable")
    return {"provider_id": provider_id, "model": model, "endpoint": endpoint, "credential_ref": credential_ref}


def _credential_ref(provider: Mapping[str, str]) -> str:
    return str(provider.get("credential_ref") or "").strip()


def _pool_exhaustion_context(exc: RuntimeError) -> dict[str, Any] | None:
    message = str(exc)
    text = message.lower()
    status_code: int | None = None
    reason = ""
    if "rate limited" in text or "resource_exhausted" in text or "quota" in text or "exhausted" in text:
        status_code = 429
        reason = "rate_limit"
    elif "auth failed" in text or "credential invalid" in text or "unauthorized" in text or "forbidden" in text:
        status_code = 401
        reason = "auth_failed"
    elif "billing" in text or "payment" in text:
        status_code = 402
        reason = "billing"
    if status_code is None:
        return None
    return {
        "status_code": status_code,
        "reason": reason,
        "message": message,
        "reset_at": _retry_reset_at_from_exception(exc),
    }


def _retry_reset_at_from_exception(exc: BaseException) -> object:
    cause = exc.__cause__
    headers = getattr(cause, "headers", None) or getattr(cause, "hdrs", None)
    if headers is None:
        return None
    try:
        return headers.get("Retry-After") or headers.get("X-RateLimit-Reset") or headers.get("X-Rate-Limit-Reset")
    except Exception:
        return None


def _oauth_provider_id(provider_id: str) -> str:
    if provider_id in {"openai", "anthropic", "google"}:
        return provider_id
    raise ProtocolValidationError("memory enhancement oauth provider unsupported")


def _invoke_openai_codex(
    invocation: Mapping[str, Any],
    provider: Mapping[str, str],
    credential: MemoryEnhancementOAuthCredential,
    *,
    opener: Callable[..., Any] | None,
) -> Mapping[str, Any]:
    model_client = _memory_model_client_module()
    budget = model_client._budget(invocation)
    request_id = str(invocation.get("request_id") or uuid.uuid4())
    cache_key = _openai_codex_cache_key(provider)
    user_prompt = model_client._user_prompt(invocation)
    payload = {
        "model": provider["model"],
        "instructions": model_client._system_prompt(),
        "input": [{"role": "user", "content": [{"type": "input_text", "text": user_prompt}]}],
        "reasoning": {"effort": "medium", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "store": False,
        "stream": True,
        "prompt_cache_key": cache_key,
    }
    response_text = _post_openai_codex_stream(
        _openai_codex_endpoint(provider, credential),
        payload,
        {
            "Authorization": f"Bearer {credential.access_token}",
            "session_id": cache_key,
            "x-client-request-id": request_id,
            **_openai_codex_headers(credential.access_token),
        },
        opener=opener or model_client.urllib.request.urlopen,
        timeout_seconds=budget.timeout_seconds,
        model_client=model_client,
    )
    return model_client._metadata_from_model_text(response_text)


def _post_openai_codex_stream(
    endpoint: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    opener: Callable[..., Any],
    timeout_seconds: int,
    model_client: Any,
) -> str:
    safe_endpoint = model_client._validate_endpoint(endpoint)
    request = model_client.urllib.request.Request(
        safe_endpoint,
        data=json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8"),
        headers={
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            **dict(headers),
        },
        method="POST",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            raw_body = response.read()
    except model_client.urllib.error.HTTPError as exc:
        raise RuntimeError(model_client._http_failure_category(exc.code)) from exc
    except model_client.urllib.error.URLError as exc:
        raise RuntimeError("memory enhancement provider unavailable") from exc
    except TimeoutError as exc:
        raise RuntimeError("memory enhancement provider timeout") from exc
    return _openai_codex_stream_text(raw_body)


def _openai_codex_stream_text(raw_body: bytes) -> str:
    try:
        text = raw_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("memory enhancement provider returned invalid JSON") from exc
    deltas: list[str] = []
    completed_text = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError as exc:
            raise RuntimeError("memory enhancement provider returned invalid JSON") from exc
        if not isinstance(event, Mapping):
            continue
        event_type = str(event.get("type") or "")
        if event_type.endswith(".delta"):
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif event_type.endswith(".done"):
            done_text = event.get("text")
            if isinstance(done_text, str) and done_text.strip():
                completed_text = done_text
        elif event_type == "response.completed":
            response = event.get("response") if isinstance(event.get("response"), Mapping) else {}
            response_text = _openai_codex_response_text(response)
            if response_text:
                completed_text = response_text
    output = completed_text or "".join(deltas).strip()
    if not output:
        raise RuntimeError("memory enhancement provider returned invalid JSON")
    return output


def _openai_codex_cache_key(provider: Mapping[str, str]) -> str:
    model = str(provider.get("model") or "model").strip() or "model"
    safe_model = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in model)
    return f"cm-memory-enhancement-openai-{safe_model}"


def _openai_codex_endpoint(provider: Mapping[str, str], credential: MemoryEnhancementOAuthCredential) -> str:
    explicit = str(provider.get("endpoint") or "").strip()
    if explicit:
        return explicit
    base_url = credential.base_url.rstrip("/")
    return f"{base_url}/responses" if base_url else OPENAI_CODEX_ENDPOINT


def _openai_codex_headers(access_token: str) -> dict[str, str]:
    headers = dict(OPENAI_CODEX_HEADERS)
    account_id = _chatgpt_account_id_from_jwt(access_token)
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def _chatgpt_account_id_from_jwt(token: str) -> str:
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    try:
        padded = parts[1] + ("=" * (-len(parts[1]) % 4))
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    auth_claims = claims.get("https://api.openai.com/auth") if isinstance(claims, Mapping) else {}
    account_id = auth_claims.get("chatgpt_account_id") if isinstance(auth_claims, Mapping) else ""
    return account_id.strip() if isinstance(account_id, str) else ""


def _openai_codex_response_text(response: Mapping[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    output = response.get("output") if isinstance(response.get("output"), list) else []
    parts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content") if isinstance(item.get("content"), list) else []
        for part in content:
            if isinstance(part, Mapping) and part.get("type") in {"output_text", "text"}:
                parts.append(str(part.get("text") or ""))
    return "\n".join(part for part in parts if part).strip()


def _invoke_anthropic_oauth(
    invocation: Mapping[str, Any],
    provider: Mapping[str, str],
    credential: MemoryEnhancementOAuthCredential,
    *,
    opener: Callable[..., Any] | None,
) -> Mapping[str, Any]:
    model_client = _memory_model_client_module()
    budget = model_client._budget(invocation)
    payload = {
        "model": provider["model"],
        "system": model_client._system_prompt(),
        "messages": [{"role": "user", "content": model_client._user_prompt(invocation)}],
        "max_tokens": budget.max_output_tokens,
        "temperature": 0,
    }
    response = model_client._post_json(
        provider.get("endpoint") or ANTHROPIC_OAUTH_ENDPOINT,
        payload,
        {
            "Authorization": f"Bearer {credential.access_token}",
            **ANTHROPIC_OAUTH_HEADERS,
        },
        opener=opener or model_client.urllib.request.urlopen,
        timeout_seconds=budget.timeout_seconds,
    )
    content = response.get("content") if isinstance(response.get("content"), list) else []
    first = content[0] if content and isinstance(content[0], Mapping) else {}
    return model_client._metadata_from_model_text(first.get("text"))


def _invoke_google_cloudcode(
    invocation: Mapping[str, Any],
    provider: Mapping[str, str],
    credential: MemoryEnhancementOAuthCredential,
    *,
    store: MemoryEnhancementOAuthStore | None = None,
    opener: Callable[..., Any] | None,
) -> Mapping[str, Any]:
    model_client = _memory_model_client_module()
    budget = model_client._budget(invocation)
    model_candidates = google_cloudcode_model_candidates(provider["model"])
    last_error: RuntimeError | None = None
    from .hermes_google_oauth import bind_credential

    with bind_credential(credential, store=store):
        for model in model_candidates:
            client = _hermes_google_client_class()(
                api_key="google-oauth",
                base_url=credential.base_url or None,
                project_id=credential.project_id,
            )
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": model_client._system_prompt()},
                        {"role": "user", "content": model_client._user_prompt(invocation)},
                    ],
                    stream=True,
                    temperature=0,
                    max_tokens=budget.max_output_tokens,
                    timeout=budget.timeout_seconds,
                )
                text = _hermes_google_response_text(response)
                return model_client._metadata_from_model_text(text)
            except Exception as exc:
                runtime_error = _runtime_error_from_hermes_google_error(exc)
                if not _google_cloudcode_model_retryable(str(runtime_error)):
                    raise runtime_error from exc
                last_error = runtime_error
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
    if last_error is not None:
        raise last_error
    raise RuntimeError("memory enhancement provider unavailable")


def _hermes_google_client_class() -> type:
    from .hermes_gemini_cloudcode_adapter import GeminiCloudCodeClient

    return GeminiCloudCodeClient


def _hermes_google_response_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    first = choices[0] if isinstance(choices, list) and choices else None
    message = getattr(first, "message", None)
    content = getattr(message, "content", None)
    if isinstance(content, str) and content.strip():
        return content
    parts: list[str] = []
    try:
        iterator = iter(response)
    except TypeError:
        iterator = iter(())
    for chunk in iterator:
        chunk_choices = getattr(chunk, "choices", None)
        chunk_first = chunk_choices[0] if isinstance(chunk_choices, list) and chunk_choices else None
        delta = getattr(chunk_first, "delta", None)
        delta_content = getattr(delta, "content", None)
        if isinstance(delta_content, str):
            parts.append(delta_content)
    streamed = "".join(parts).strip()
    if streamed:
        return streamed
    raise RuntimeError("memory enhancement provider returned invalid JSON")


def _runtime_error_from_hermes_google_error(exc: BaseException) -> RuntimeError:
    code = str(getattr(exc, "code", "") or "").strip()
    details = getattr(exc, "details", None)
    pieces = [str(exc)]
    if code:
        pieces.append(f"code={code}")
    if isinstance(details, Mapping):
        status = str(details.get("status") or "").strip()
        reason = str(details.get("reason") or "").strip()
        if status:
            pieces.append(f"status={status}")
        if reason:
            pieces.append(f"reason={reason}")
    return RuntimeError(" ".join(piece for piece in pieces if piece))


def _google_cloudcode_project_id(
    credential: MemoryEnhancementOAuthCredential,
    *,
    provider: Mapping[str, str],
    model_client: Any,
    opener: Callable[..., Any] | None,
    timeout_seconds: int,
) -> str:
    if credential.project_id:
        return credential.project_id
    headers = {
        "Authorization": f"Bearer {credential.access_token}",
        "x-activity-request-id": str(uuid.uuid4()),
        **_google_cloudcode_discovery_headers(provider),
    }
    load_response = _post_google_cloudcode_json(
        _google_cloudcode_endpoint(provider, credential, "loadCodeAssist"),
        _google_cloudcode_load_request(""),
        headers,
        opener=opener or model_client.urllib.request.urlopen,
        timeout_seconds=timeout_seconds,
        model_client=model_client,
    )
    discovered = _google_cloudcode_project_from_response(load_response)
    if discovered:
        return discovered

    tier_id = _google_cloudcode_current_tier(load_response)
    if tier_id:
        raise RuntimeError("memory enhancement google project unavailable")

    onboard_request = {
        "tierId": _google_cloudcode_default_tier(load_response) or GOOGLE_CLOUDCODE_FREE_TIER_ID,
        "metadata": _google_cloudcode_client_metadata(),
    }
    deadline = time.monotonic() + max(1, min(int(timeout_seconds), 30))
    while True:
        onboard_response = _post_google_cloudcode_json(
            _google_cloudcode_endpoint(provider, credential, "onboardUser"),
            onboard_request,
            headers,
            opener=opener or model_client.urllib.request.urlopen,
            timeout_seconds=timeout_seconds,
            model_client=model_client,
        )
        discovered = _google_cloudcode_project_from_response(onboard_response)
        if discovered:
            return discovered
        operation_name = str(onboard_response.get("name") or "").strip()
        if not operation_name or onboard_response.get("done") is True:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
        onboard_response = _post_google_cloudcode_json(
            _google_cloudcode_operation_endpoint(credential, operation_name),
            {},
            headers,
            opener=opener or model_client.urllib.request.urlopen,
            timeout_seconds=timeout_seconds,
            model_client=model_client,
        )
        discovered = _google_cloudcode_project_from_response(onboard_response)
        if discovered:
            return discovered
        if onboard_response.get("done") is True:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
    raise RuntimeError("memory enhancement google project unavailable")


def _post_google_cloudcode_json(
    endpoint: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    opener: Callable[..., Any],
    timeout_seconds: int,
    model_client: Any,
) -> Mapping[str, Any]:
    if not hasattr(model_client, "_validate_endpoint"):
        return model_client._post_json(
            endpoint,
            payload,
            headers,
            opener=opener,
            timeout_seconds=timeout_seconds,
        )
    safe_endpoint = model_client._validate_endpoint(endpoint)
    request = model_client.urllib.request.Request(
        safe_endpoint,
        data=json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            **dict(headers),
        },
        method="POST",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            raw_body = response.read()
    except model_client.urllib.error.HTTPError as exc:
        raise RuntimeError(_google_cloudcode_failure_category(exc, model_client)) from exc
    except model_client.urllib.error.URLError as exc:
        raise RuntimeError("memory enhancement provider unavailable") from exc
    except TimeoutError as exc:
        raise RuntimeError("memory enhancement provider timeout") from exc
    try:
        decoded = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("memory enhancement provider returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("memory enhancement provider returned invalid JSON")
    return decoded


def _google_cloudcode_failure_category(exc: Any, model_client: Any) -> str:
    category = model_client._http_failure_category(exc.code)
    reason = _google_cloudcode_http_error_reason(exc)
    return f"{category} ({reason})" if reason else category


def _google_cloudcode_http_error_reason(exc: Any) -> str:
    try:
        raw_body = exc.read()
    except Exception:
        raw_body = b""
    if not raw_body:
        return ""
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"google_http_{int(exc.code)}"
    error = payload.get("error") if isinstance(payload, Mapping) else {}
    if not isinstance(error, Mapping):
        return f"google_http_{int(exc.code)}"
    status = _safe_google_error_token(error.get("status"))
    reason = ""
    details = error.get("details") if isinstance(error.get("details"), list) else []
    for detail in details:
        if isinstance(detail, Mapping):
            reason = _safe_google_error_token(detail.get("reason"))
            if reason:
                break
    parts = [f"google_http_{int(exc.code)}"]
    if status:
        parts.append(status)
    if reason and reason != status:
        parts.append(reason)
    return "_".join(parts)


def _safe_google_error_token(value: object) -> str:
    text = str(value or "").strip().upper()
    return "".join(ch if ch.isalnum() else "_" for ch in text)[:80].strip("_")


def _google_cloudcode_model_retryable(message: str) -> bool:
    text = message.lower()
    if "auth failed" in text or "rate limited" in text or "timeout" in text:
        return False
    return (
        "provider unavailable" in text
        or "model_unavailable" in text
        or "not available" in text
        or "code_assist_http_404" in text
        or "code_assist_capacity_exhausted" in text
        or "provider rejected content" in text
        or "returned invalid json" in text
    )


def _google_cloudcode_load_request(project_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "metadata": {
            "duetProject": project_id,
            **_google_cloudcode_client_metadata(),
        }
    }
    if project_id:
        payload["cloudaicompanionProject"] = project_id
    return payload


def _google_cloudcode_client_metadata() -> dict[str, str]:
    return {
        "ideType": "IDE_UNSPECIFIED",
        "platform": "PLATFORM_UNSPECIFIED",
        "pluginType": "GEMINI",
    }


def _google_cloudcode_discovery_headers(provider: Mapping[str, str]) -> dict[str, str]:
    headers = dict(GOOGLE_CLOUDCODE_DISCOVERY_HEADERS)
    model = str(provider.get("model") or "").strip()
    if model:
        headers["User-Agent"] = f"{headers['User-Agent']} model/{model}"
    return headers


def _google_cloudcode_endpoint(
    provider: Mapping[str, str],
    credential: MemoryEnhancementOAuthCredential,
    method: str,
) -> str:
    explicit = str(provider.get("endpoint") or "").strip()
    if explicit and method == "generateContent":
        return explicit
    base_url = _google_cloudcode_base_url(credential)
    return f"{base_url}/v1internal:{method}"


def _google_cloudcode_base_url(credential: MemoryEnhancementOAuthCredential) -> str:
    raw = str(credential.base_url or "").strip().rstrip("/")
    default = GOOGLE_CLOUDCODE_ENDPOINT.rsplit("/", 1)[0]
    if not raw or "cloudcode-pa.googleapis.com" not in raw:
        return default
    if raw.endswith("/v1internal"):
        return raw.removesuffix("/v1internal")
    return raw


def _google_cloudcode_operation_endpoint(credential: MemoryEnhancementOAuthCredential, operation_name: str) -> str:
    base_url = _google_cloudcode_base_url(credential)
    return f"{base_url}/v1internal/{operation_name.lstrip('/')}"


def _google_cloudcode_project_from_response(response: Mapping[str, Any]) -> str:
    direct = response.get("cloudaicompanionProject")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    if isinstance(direct, Mapping):
        project_id = str(direct.get("id") or "").strip()
        if project_id:
            return project_id
    nested = response.get("response") if isinstance(response.get("response"), Mapping) else {}
    if isinstance(nested, Mapping):
        nested_project = nested.get("cloudaicompanionProject")
        if isinstance(nested_project, str) and nested_project.strip():
            return nested_project.strip()
        if isinstance(nested_project, Mapping):
            project_id = str(nested_project.get("id") or "").strip()
            if project_id:
                return project_id
    return ""


def _google_cloudcode_default_tier(response: Mapping[str, Any]) -> str:
    tiers = response.get("allowedTiers") if isinstance(response.get("allowedTiers"), list) else []
    for tier in tiers:
        if isinstance(tier, Mapping) and tier.get("isDefault") is True:
            return str(tier.get("id") or "").strip()
    return ""


def _google_cloudcode_current_tier(response: Mapping[str, Any]) -> str:
    current = response.get("currentTier") if isinstance(response.get("currentTier"), Mapping) else {}
    return str(current.get("id") or "").strip() if isinstance(current, Mapping) else ""


def _metadata_from_google_cloudcode_response(response: Mapping[str, Any], model_client: Any) -> Mapping[str, Any]:
    text = _google_response_text(response)
    return model_client._metadata_from_model_text(text)


def _google_response_text(response: Mapping[str, Any]) -> str:
    payload = response.get("response") if isinstance(response.get("response"), Mapping) else response
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    first = candidates[0] if candidates and isinstance(candidates[0], Mapping) else {}
    content = first.get("content") if isinstance(first.get("content"), Mapping) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    return "".join(str(part.get("text") or "") for part in parts if isinstance(part, Mapping))


def _memory_model_client_module() -> Any:
    from chimera_memory import memory_enhancement_model_client as model_client

    return model_client


if __name__ == "__main__":
    raise SystemExit(main())
