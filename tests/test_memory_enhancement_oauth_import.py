from __future__ import annotations

import json
from pathlib import Path

from chimera_memory.memory_enhancement_oauth import MemoryEnhancementOAuthStore
from chimera_memory.memory_enhancement_oauth_import import import_memory_enhancement_oauth_credential


def test_import_anthropic_claude_code_credentials(tmp_path: Path):
    claude_path = tmp_path / ".claude" / ".credentials.json"
    claude_path.parent.mkdir()
    claude_path.write_text(
        json.dumps(
            {
                "accessToken": "TEST_ONLY_ANTHROPIC_ACCESS",
                "refreshToken": "TEST_ONLY_ANTHROPIC_REFRESH",
                "expiresAt": 1_800_000_000_000,
            }
        ),
        encoding="utf-8",
    )
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")

    credential = import_memory_enhancement_oauth_credential(
        provider_id="anthropic",
        source="claude_code",
        store=store,
        claude_credentials_path=claude_path,
    )

    assert credential.provider_id == "anthropic"
    assert credential.source == "claude_code"
    assert credential.transport == "anthropic_oauth"
    assert store.get("anthropic-memory", provider_id="anthropic").access_token == "TEST_ONLY_ANTHROPIC_ACCESS"


def test_import_google_hermes_credentials_parses_packed_project(tmp_path: Path):
    hermes_home = tmp_path / ".hermes"
    google_path = hermes_home / "auth" / "google_oauth.json"
    google_path.parent.mkdir(parents=True)
    google_path.write_text(
        json.dumps(
            {
                "access": "TEST_ONLY_GOOGLE_ACCESS",
                "refresh": "TEST_ONLY_GOOGLE_REFRESH|project-test|managed-test",
                "expires": 1_800_000_000_000,
                "email": "test@example.invalid",
            }
        ),
        encoding="utf-8",
    )
    store = MemoryEnhancementOAuthStore(tmp_path / "memory-oauth.json")

    credential = import_memory_enhancement_oauth_credential(
        provider_id="google",
        source="hermes_google",
        store=store,
        hermes_home=hermes_home,
    )

    assert credential.provider_id == "google"
    assert credential.source == "hermes_google"
    assert credential.transport == "google_cloudcode"
    assert credential.project_id == "project-test"
    assert credential.account_label == "test@example.invalid"
    assert store.get("google-memory", provider_id="google").refresh_token == "TEST_ONLY_GOOGLE_REFRESH"
