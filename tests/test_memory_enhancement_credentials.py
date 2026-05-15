from __future__ import annotations

import json

import pytest

from chimera_memory.memory_enhancement_credentials import (
    EnvMemoryEnhancementCredentialResolver,
    MappingMemoryEnhancementCredentialResolver,
    MemoryEnhancementCredentialResolutionError,
    MemoryEnhancementCredentialRef,
    ProtocolValidationError,
    ResolvedMemoryEnhancementCredential,
    parse_memory_enhancement_credential_ref,
    resolve_memory_enhancement_credential,
)


def test_memory_enhancement_credential_ref_parses_safe_refs():
    ref = parse_memory_enhancement_credential_ref("oauth:openai_memory_sidecar")

    assert ref == MemoryEnhancementCredentialRef(scheme="oauth", name="openai_memory_sidecar")
    assert ref.raw_ref == "oauth:openai_memory_sidecar"
    assert ref.to_safe_dict() == {
        "scheme": "oauth",
        "ref_hash_prefix": ref.safe_hash_prefix,
    }


def test_memory_enhancement_credential_ref_rejects_raw_or_unsafe_values():
    for value in (
        "raw-token-material",
        "env:BAD NAME",
        "../secret",
        "https://example.invalid/token",
        "",
        None,
    ):
        with pytest.raises(ProtocolValidationError):
            parse_memory_enhancement_credential_ref(value)


def test_mapping_resolver_returns_value_but_safe_receipt_excludes_it():
    fake_secret = "TEST_ONLY_FAKE_TOKEN_123"
    resolved = resolve_memory_enhancement_credential(
        "secret:memory_sidecar",
        MappingMemoryEnhancementCredentialResolver({"secret:memory_sidecar": fake_secret}),
    )

    assert resolved.value == fake_secret
    safe_json = json.dumps(resolved.to_safe_dict(), sort_keys=True)
    assert fake_secret not in safe_json
    assert "memory_sidecar" not in safe_json
    assert '"resolved": true' in safe_json


def test_env_resolver_reads_only_env_refs_without_echoing_missing_name():
    fake_secret = "TEST_ONLY_ENV_TOKEN_456"
    resolved = resolve_memory_enhancement_credential(
        "env:PA_MEMORY_ENHANCEMENT_TOKEN",
        EnvMemoryEnhancementCredentialResolver({"PA_MEMORY_ENHANCEMENT_TOKEN": fake_secret}),
    )

    assert resolved.value == fake_secret
    assert fake_secret not in json.dumps(resolved.to_safe_dict(), sort_keys=True)

    with pytest.raises(MemoryEnhancementCredentialResolutionError) as exc_info:
        resolve_memory_enhancement_credential(
            "env:PA_MEMORY_ENHANCEMENT_TOKEN",
            EnvMemoryEnhancementCredentialResolver({}),
        )
    assert "PA_MEMORY_ENHANCEMENT_TOKEN" not in str(exc_info.value)


def test_env_resolver_rejects_non_env_refs():
    with pytest.raises(MemoryEnhancementCredentialResolutionError, match="unsupported"):
        resolve_memory_enhancement_credential(
            "oauth:openai_memory_sidecar",
            EnvMemoryEnhancementCredentialResolver({"openai_memory_sidecar": "TEST_ONLY_FAKE_TOKEN"}),
        )


def test_resolved_credential_validates_before_safe_serialization():
    ref = parse_memory_enhancement_credential_ref("secret:memory_sidecar")
    resolved = ResolvedMemoryEnhancementCredential(ref=ref, value="bad\x00value", source="test")

    with pytest.raises(ProtocolValidationError, match="control"):
        resolved.to_safe_dict()
