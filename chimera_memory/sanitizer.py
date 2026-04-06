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

# ─── Prompt Injection & Exfiltration Detection ──────────────────────

INJECTION_PATTERNS = [
    re.compile(r'(?i)ignore\s+(all\s+)?(previous|above|prior)\s+(instructions|prompts)'),
    re.compile(r'(?i)disregard\s+(your|all|previous)'),
    re.compile(r'(?i)you\s+are\s+now\s+(a|an|the)\s+'),
    re.compile(r'(?i)new\s+instructions?\s*:'),
    re.compile(r'(?i)system\s+prompt\s*:'),
    re.compile(r'(?i)forget\s+(everything|your\s+instructions)'),
    re.compile(r'(?i)send\s+(this|the|all|my)\s+.{0,30}(to|via)\s+(http|email|webhook|slack|telegram)'),
    re.compile(r'(?i)curl\s+.*\s+http'),
    re.compile(r'(?i)fetch\s*\(\s*["\']https?://'),
    re.compile(r'(?i)base64\.(encode|decode)'),
]

# Invisible unicode characters (zero-width spaces, joiners, etc.)
INVISIBLE_CHARS = {0x200b, 0x200c, 0x200d, 0x2060, 0xfeff}

# FTS5 operator sanitization
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


def scan_for_injection(content: str) -> list[dict]:
    """Scan content for prompt injection, exfiltration, and hidden content.

    Returns list of findings. Empty list = clean.
    Use this before writing to memory files (memory_guard).
    """
    if not content:
        return []

    findings = []

    for pattern in INJECTION_PATTERNS:
        matches = pattern.findall(content)
        if matches:
            findings.append({
                "type": "injection",
                "pattern": pattern.pattern[:60],
                "match_count": len(matches),
                "sample": str(matches[0])[:100],
            })

    # Check for invisible unicode
    invisible_count = sum(1 for c in content if ord(c) in INVISIBLE_CHARS)
    if invisible_count > 5:
        findings.append({
            "type": "invisible_unicode",
            "match_count": invisible_count,
            "sample": f"{invisible_count} invisible unicode characters detected",
        })

    # Check for HTML comments (could hide instructions)
    html_comments = re.findall(r'<!--.*?-->', content, re.DOTALL)
    if html_comments:
        findings.append({
            "type": "html_comment",
            "match_count": len(html_comments),
            "sample": html_comments[0][:100],
        })

    # Check for credential patterns
    for pattern, replacement in SECRET_PATTERNS:
        if pattern.search(content):
            findings.append({
                "type": "credential",
                "pattern": replacement,
                "match_count": 1,
                "sample": replacement,
            })

    return findings
