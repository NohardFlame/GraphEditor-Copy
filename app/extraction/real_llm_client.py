"""Sync LLM client adapter for run_big_llm_extract (real API calls).

Use this to run the extraction pipeline against Ollama or Gemini instead of a mock.
"""
from __future__ import annotations

import os
from typing import Optional

from app.extraction.big_extract_runner import BigLlmExtractClient


def _get_litellm_completion():
    """Lazy import to avoid litellm at import time."""
    from litellm import completion
    return completion


class RealLLMClient(BigLlmExtractClient):
    """Sync client that calls LiteLLM (Ollama/Gemini/etc.) for run_big_llm_extract."""

    def __init__(
        self,
        model_id: str,
        *,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        timeout_s: float = 300.0,
    ) -> None:
        self.model_id = model_id
        self.api_key = api_key or (model_id.startswith("gemini/") and os.environ.get("LLM_GEMINI_API_KEY"))
        self.api_base = api_base or (model_id.startswith("ollama/") and os.environ.get("LLM_OLLAMA_API_BASE"))
        self.timeout_s = timeout_s

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model_id: str,
    ) -> str:
        completion = _get_litellm_completion()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        kwargs = {
            "model": model_id,
            "messages": messages,
            "timeout": self.timeout_s,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base
        response = completion(**kwargs)
        if not response or not getattr(response, "choices", None) or not response.choices:
            return ""
        msg = response.choices[0].message
        return getattr(msg, "content", None) or ""


def make_real_client(
    model_id: str | None = None,
    *,
    api_key: str | None = None,
    api_base: str | None = None,
    timeout_s: float = 300.0,
) -> RealLLMClient:
    """Build a RealLLMClient from env or args. model_id defaults from LLM_OLLAMA_MODEL or ollama/llama3.2."""
    if model_id is None:
        model_id = os.environ.get("LLM_OLLAMA_MODEL", "ollama/llama3.2")
    return RealLLMClient(
        model_id,
        api_key=api_key,
        api_base=api_base,
        timeout_s=timeout_s,
    )
