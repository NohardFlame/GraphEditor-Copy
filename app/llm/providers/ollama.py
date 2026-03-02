"""Ollama provider: build LiteLLM kwargs from settings. Best-effort local."""
from __future__ import annotations

from typing import Any

from app.llm.settings import LLMSettings


def ollama_kwargs(settings: LLMSettings) -> dict[str, Any]:
    """Build provider kwargs for Ollama. model, api_base, and optional num_ctx from settings."""
    out: dict[str, Any] = {
        "model": settings.ollama_model,
        "api_base": settings.ollama_api_base,
    }
    if settings.ollama_num_ctx is not None:
        out["extra_body"] = {"num_ctx": settings.ollama_num_ctx}
    return out
