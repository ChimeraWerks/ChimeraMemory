from __future__ import annotations

GOOGLE_GEMINI_CLI_OAUTH_MODELS = (
    "gemini-3.1-pro-preview",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
)

GOOGLE_CLOUDCODE_MEMORY_DEFAULT_MODEL = "gemini-3-flash-preview"
GOOGLE_CLOUDCODE_MEMORY_MODEL_CANDIDATES = (
    GOOGLE_CLOUDCODE_MEMORY_DEFAULT_MODEL,
    "gemini-3.1-pro-preview",
    "gemini-3-pro-preview",
)


def google_cloudcode_model_candidates(requested: str) -> tuple[str, ...]:
    """Return the requested model plus Hermes-current Google OAuth fallbacks."""
    ordered: list[str] = []
    for model in (requested.strip(), *GOOGLE_CLOUDCODE_MEMORY_MODEL_CANDIDATES):
        if model and model not in ordered:
            ordered.append(model)
    return tuple(ordered)
