"""Content sanitization: filter secrets and sensitive data before indexing."""

import re
import logging

log = logging.getLogger(__name__)

# Patterns that indicate secrets/credentials
SECRET_PATTERNS = [
    # API keys and tokens
    (re.compile(r'sk-ant-[a-zA-Z0-9_-]{20,}'), '<REDACTED:anthropic-key>'),
    (re.compile(r'sk-[a-zA-Z0-9]{20,}'), '<REDACTED:api-key>'),
    (re.compile(r'MTQ[a-zA-Z0-9+/=.]{20,}'), '<REDACTED:discord-token>'),
    (re.compile(r'xoxb-[a-zA-Z0-9-]+'), '<REDACTED:slack-token>'),
    (re.compile(r'ghp_[a-zA-Z0-9]{36,}'), '<REDACTED:github-pat>'),
    (re.compile(r'gho_[a-zA-Z0-9]{36,}'), '<REDACTED:github-oauth>'),

    # Webhook URLs
    (re.compile(r'https://discord\.com/api/webhooks/\d+/[a-zA-Z0-9_-]+'), '<REDACTED:discord-webhook>'),
    (re.compile(r'https://hooks\.slack\.com/[a-zA-Z0-9/]+'), '<REDACTED:slack-webhook>'),

    # Generic patterns
    (re.compile(r'Bearer\s+[a-zA-Z0-9._-]{20,}'), 'Bearer <REDACTED>'),
    (re.compile(r'(?:password|passwd|pwd)\s*[=:]\s*["\']?[^\s"\']{8,}', re.IGNORECASE), '<REDACTED:password>'),
    (re.compile(r'(?:secret|token|key|apikey|api_key)\s*[=:]\s*["\']?[a-zA-Z0-9_-]{16,}', re.IGNORECASE), '<REDACTED:secret>'),

    # AWS
    (re.compile(r'AKIA[0-9A-Z]{16}'), '<REDACTED:aws-key>'),
    (re.compile(r'(?:aws_secret_access_key|aws_session_token)\s*[=:]\s*[^\s]{20,}', re.IGNORECASE), '<REDACTED:aws-secret>'),

    # Private keys
    (re.compile(r'-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----'), '<REDACTED:private-key>'),
]

# FTS5 operator sanitization (from Claudest)
FTS_OPERATORS = re.compile(r'["\(\)*\-^]')
FTS_KEYWORDS = re.compile(r'\b(NEAR|AND|OR|NOT)\b', re.IGNORECASE)


def sanitize_content(text: str | None) -> str | None:
    """Remove secrets and sensitive patterns from content before indexing."""
    if not text:
        return text

    redacted = False
    for pattern, replacement in SECRET_PATTERNS:
        new_text = pattern.sub(replacement, text)
        if new_text != text:
            redacted = True
            text = new_text

    if redacted:
        log.info("Redacted sensitive content from transcript entry")

    return text


def sanitize_fts_term(term: str) -> str:
    """Sanitize a search term for safe FTS5 querying.

    Strips FTS operators and keywords to prevent injection.
    """
    sanitized = FTS_OPERATORS.sub("", term)
    sanitized = FTS_KEYWORDS.sub("", sanitized)
    return sanitized.strip()


def build_fts_query(terms: list[str]) -> str:
    """Build a safe FTS5 query from a list of search terms.

    Each term is sanitized, quoted, and joined with OR.
    """
    clean_terms = []
    for term in terms:
        cleaned = sanitize_fts_term(term)
        if cleaned:
            clean_terms.append(f'"{cleaned}"')
    return " OR ".join(clean_terms) if clean_terms else ""
