from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .memory_enhancement_credentials import (
    EnvMemoryEnhancementCredentialResolver,
    MemoryEnhancementCredentialRef,
    MemoryEnhancementCredentialResolver,
    ProtocolValidationError,
)
from .memory_enhancement_oauth import (
    MemoryEnhancementOAuthCredential,
    OAuthMemoryEnhancementCredentialResolver,
)


OPENAI_CODEX_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
ANTHROPIC_OAUTH_ENDPOINT = "https://api.anthropic.com/v1/messages"
GOOGLE_CLOUDCODE_ENDPOINT = "https://cloudcode-pa.googleapis.com/v1internal:generateContent"

OPENAI_CODEX_HEADERS = {
    "User-Agent": "codex_cli_rs/0.0.0 (ChimeraMemory)",
    "originator": "codex_cli_rs",
}
ANTHROPIC_OAUTH_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
    "x-app": "cli",
    "user-agent": "claude-cli/1.0.0 (external, cli)",
}
GOOGLE_CLOUDCODE_HEADERS = {
    "User-Agent": "chimera-memory (gemini-cli-compat)",
    "X-Goog-Api-Client": "gl-python/chimera-memory",
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
            return self._invoke_oauth(invocation, provider, credential)
        resolved = self._credential_resolver.resolve(ref)
        return self._api_key_client_factory(resolved.value).invoke(invocation)

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
            return _invoke_anthropic_oauth(invocation, provider, credential, opener=self._opener)
        if provider_id == "google" and credential.transport == "google_cloudcode":
            return _invoke_google_cloudcode(invocation, provider, credential, opener=self._opener)
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
        "prompt_cache_key": cache_key,
    }
    response = model_client._post_json(
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
    )
    return model_client._metadata_from_model_text(_openai_codex_response_text(response))


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
    opener: Callable[..., Any] | None,
) -> Mapping[str, Any]:
    if not credential.project_id:
        raise RuntimeError("memory enhancement google project unavailable")
    model_client = _memory_model_client_module()
    budget = model_client._budget(invocation)
    inner_request = {
        "systemInstruction": {"parts": [{"text": model_client._system_prompt()}]},
        "contents": [{"role": "user", "parts": [{"text": model_client._user_prompt(invocation)}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": budget.max_output_tokens,
            "responseMimeType": "application/json",
        },
    }
    payload = {
        "project": credential.project_id,
        "model": provider["model"],
        "user_prompt_id": str(uuid.uuid4()),
        "request": inner_request,
    }
    response = model_client._post_json(
        provider.get("endpoint") or GOOGLE_CLOUDCODE_ENDPOINT,
        payload,
        {
            "Authorization": f"Bearer {credential.access_token}",
            "x-activity-request-id": str(uuid.uuid4()),
            **GOOGLE_CLOUDCODE_HEADERS,
        },
        opener=opener or model_client.urllib.request.urlopen,
        timeout_seconds=budget.timeout_seconds,
    )
    return _metadata_from_google_cloudcode_response(response, model_client)


def _metadata_from_google_cloudcode_response(response: Mapping[str, Any], model_client: Any) -> Mapping[str, Any]:
    text = _google_response_text(response)
    return model_client._metadata_from_model_text(text)


def _google_response_text(response: Mapping[str, Any]) -> str:
    candidates = response.get("candidates") if isinstance(response.get("candidates"), list) else []
    first = candidates[0] if candidates and isinstance(candidates[0], Mapping) else {}
    content = first.get("content") if isinstance(first.get("content"), Mapping) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    return "".join(str(part.get("text") or "") for part in parts if isinstance(part, Mapping))


def _memory_model_client_module() -> Any:
    from chimera_memory import memory_enhancement_model_client as model_client

    return model_client


if __name__ == "__main__":
    raise SystemExit(main())
