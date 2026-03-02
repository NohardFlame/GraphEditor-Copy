"""Provider adapters: build kwargs for Ollama and Gemini from settings."""
from __future__ import annotations

from typing import Any

from app.llm.providers.gemini import gemini_kwargs
from app.llm.providers.ollama import ollama_kwargs
from app.llm.settings import LLMSettings
from app.llm.types import LLMProvider


def completion_kwargs_for_provider(provider: LLMProvider, settings: LLMSettings) -> dict[str, Any]:
    """Build api_base, api_key, extra_completion_kwargs for LiteLLMClient.acompletion()."""
    if provider == LLMProvider.OLLAMA:
        k = ollama_kwargs(settings)
    else:
        k = gemini_kwargs(settings)
    out: dict[str, Any] = {}
    if k.get("api_base") is not None:
        out["api_base"] = k["api_base"]
    if k.get("api_key") is not None:
        out["api_key"] = k["api_key"]
    # Ollama: pass num_ctx as top-level kwarg so LiteLLM puts it in options (extra_body is dropped when drop_params=True)
    if provider == LLMProvider.OLLAMA and settings.ollama_num_ctx is not None:
        out["extra_completion_kwargs"] = {
            "num_ctx": settings.ollama_num_ctx,
            "drop_params": False,  # so LiteLLM does not drop num_ctx (not in supported_openai_params)
        }
    elif k.get("extra_body"):
        out["extra_completion_kwargs"] = {"extra_body": k["extra_body"]}
    return out


__all__ = ["completion_kwargs_for_provider", "gemini_kwargs", "ollama_kwargs"]
