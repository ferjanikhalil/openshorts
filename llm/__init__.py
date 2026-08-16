"""LLM provider factory — resolves the active provider from environment variables.

Backward compatible: if LLM_PROVIDER is unset and GEMINI_API_KEY exists, Gemini is used.
"""
import os
from typing import Optional

from .base import LLMConfig, LLMProvider, LLMResponse


def create_provider(
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> LLMProvider:
    provider = provider or os.environ.get("LLM_PROVIDER", "")

    if not provider:
        if api_key or os.environ.get("GEMINI_API_KEY"):
            provider = "gemini"
        elif base_url or os.environ.get("OPENAI_BASE_URL"):
            provider = "openai_compat"
        else:
            provider = "gemini"

    if provider == "gemini":
        from .gemini import GeminiProvider
        key = api_key or os.environ.get("GEMINI_API_KEY", "")
        mdl = model or os.environ.get("GEMINI_MODEL") or "gemini-3.1-flash-lite"
        return GeminiProvider(api_key=key, model=mdl)

    if provider == "openai_compat":
        from .openai_compat import OpenAICompatProvider
        url = base_url or os.environ.get("OPENAI_BASE_URL", "")
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        mdl = model or os.environ.get("OPENAI_MODEL", "")
        if not url:
            raise ValueError("OPENAI_BASE_URL is required for openai_compat provider")
        if not mdl:
            raise ValueError("OPENAI_MODEL is required for openai_compat provider")
        return OpenAICompatProvider(base_url=url, api_key=key or "no-key", model=mdl)

    raise ValueError(f"Unknown LLM_PROVIDER: {provider!r}. Use 'gemini' or 'openai_compat'.")
