from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

try:  # Compatibility for PA wrappers while ChimeraMemory remains standalone.
    from personifyagents.gateway_protocol import ProtocolValidationError as _ProtocolValidationBase
except Exception:  # noqa: BLE001 - optional downstream package may not exist
    _ProtocolValidationBase = ValueError


CREDENTIAL_REF_SCHEMES = frozenset(("oauth", "secret", "env"))
MAX_CREDENTIAL_VALUE_CHARS = 16_384

_CREDENTIAL_REF_RE = re.compile(r"^(?P<scheme>oauth|secret|env):(?P<name>[A-Za-z_][A-Za-z0-9_.:\-]{0,119})$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,119}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ProtocolValidationError(_ProtocolValidationBase):
    """Raised when memory-enhancement protocol input is invalid."""


class MemoryEnhancementCredentialResolutionError(ProtocolValidationError):
    """Raised when a memory-enhancement credential ref cannot be resolved."""


@dataclass(frozen=True)
class MemoryEnhancementCredentialRef:
    """A reference to credential material, never the credential material itself."""

    scheme: str
    name: str

    @classmethod
    def parse(cls, value: object) -> "MemoryEnhancementCredentialRef":
        if not isinstance(value, str):
            raise ProtocolValidationError("memory enhancement credential ref must be a string")
        text = value.strip()
        match = _CREDENTIAL_REF_RE.fullmatch(text)
        if match is None:
            raise ProtocolValidationError("memory enhancement credential ref is invalid")
        return cls(scheme=match.group("scheme"), name=match.group("name"))

    @property
    def raw_ref(self) -> str:
        return f"{self.scheme}:{self.name}"

    @property
    def safe_hash_prefix(self) -> str:
        return hashlib.sha256(self.raw_ref.encode("utf-8")).hexdigest()[:12]

    def to_safe_dict(self) -> dict[str, str]:
        return {
            "scheme": self.scheme,
            "ref_hash_prefix": self.safe_hash_prefix,
        }


@dataclass(frozen=True)
class ResolvedMemoryEnhancementCredential:
    """Credential material plus safe diagnostics for logging.

    The raw value is intentionally omitted from `to_safe_dict`.
    """

    ref: MemoryEnhancementCredentialRef
    value: str
    source: str

    def to_safe_dict(self) -> dict[str, object]:
        require_valid_memory_enhancement_credential_value(self.value)
        return {
            "scheme": self.ref.scheme,
            "ref_hash_prefix": self.ref.safe_hash_prefix,
            "source": self.source,
            "resolved": True,
            "value_present": bool(self.value),
        }


class MemoryEnhancementCredentialResolver(Protocol):
    def resolve(self, ref: MemoryEnhancementCredentialRef) -> ResolvedMemoryEnhancementCredential:
        """Resolve a credential ref to raw credential material."""


@dataclass(frozen=True)
class EnvMemoryEnhancementCredentialResolver:
    """Resolve `env:NAME` refs from an environment mapping.

    The resolver rejects non-env refs. OAuth and secret refs must be resolved by
    PA's future secret-store adapter, not by this environment fallback.
    """

    environ: Mapping[str, str] | None = None

    def resolve(self, ref: MemoryEnhancementCredentialRef) -> ResolvedMemoryEnhancementCredential:
        require_valid_memory_enhancement_credential_ref(ref)
        if ref.scheme != "env":
            raise MemoryEnhancementCredentialResolutionError("memory enhancement credential resolver unsupported")
        if _ENV_NAME_RE.fullmatch(ref.name) is None:
            raise ProtocolValidationError("memory enhancement env credential ref is invalid")
        environ = os.environ if self.environ is None else self.environ
        value = str(environ.get(ref.name, ""))
        if not value:
            raise MemoryEnhancementCredentialResolutionError("memory enhancement credential unavailable")
        require_valid_memory_enhancement_credential_value(value)
        return ResolvedMemoryEnhancementCredential(ref=ref, value=value, source="env")


@dataclass(frozen=True)
class MappingMemoryEnhancementCredentialResolver:
    """Test and adapter helper that resolves refs from an injected mapping."""

    values: Mapping[str, str]

    def resolve(self, ref: MemoryEnhancementCredentialRef) -> ResolvedMemoryEnhancementCredential:
        require_valid_memory_enhancement_credential_ref(ref)
        value = str(self.values.get(ref.raw_ref, ""))
        if not value:
            raise MemoryEnhancementCredentialResolutionError("memory enhancement credential unavailable")
        require_valid_memory_enhancement_credential_value(value)
        return ResolvedMemoryEnhancementCredential(ref=ref, value=value, source="mapping")


def parse_memory_enhancement_credential_ref(value: object) -> MemoryEnhancementCredentialRef:
    return MemoryEnhancementCredentialRef.parse(value)


def resolve_memory_enhancement_credential(
    value: object,
    resolver: MemoryEnhancementCredentialResolver,
) -> ResolvedMemoryEnhancementCredential:
    ref = parse_memory_enhancement_credential_ref(value)
    resolved = resolver.resolve(ref)
    if resolved.ref != ref:
        raise ProtocolValidationError("memory enhancement credential resolver returned mismatched ref")
    require_valid_memory_enhancement_credential_value(resolved.value)
    return resolved


def require_valid_memory_enhancement_credential_ref(ref: MemoryEnhancementCredentialRef) -> None:
    if ref.scheme not in CREDENTIAL_REF_SCHEMES:
        raise ProtocolValidationError("memory enhancement credential ref scheme unsupported")
    if _CREDENTIAL_REF_RE.fullmatch(ref.raw_ref) is None:
        raise ProtocolValidationError("memory enhancement credential ref is invalid")


def require_valid_memory_enhancement_credential_value(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise MemoryEnhancementCredentialResolutionError("memory enhancement credential value unavailable")
    if len(value) > MAX_CREDENTIAL_VALUE_CHARS:
        raise ProtocolValidationError("memory enhancement credential value is too large")
    if _CONTROL_RE.search(value):
        raise ProtocolValidationError("memory enhancement credential value contains control characters")
