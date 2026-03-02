"""LLM module configuration. Env prefix: LLM_. Gemini key: LLM_GEMINI_API_KEY (Pattern 2)."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.llm.types import LLMProfile, LLMProvider


class LLMSettings(BaseSettings):
    """Settings for LLM router, client, and persistence. All overridable via LLM_* env vars."""

    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    default_profile: LLMProfile = Field(default=LLMProfile.AUTO, description="Default routing profile")
    concurrency_limit: int = Field(default=8, ge=1, description="Max concurrent LLM calls per process")
    default_timeout_s: float = Field(default=600.0, gt=0, description="Default request timeout")
    max_retries: int = Field(default=2, ge=0, description="Max retries for retryable errors")
    retry_backoff_base_s: float = Field(default=0.5, gt=0, description="Base delay for exponential backoff")
    retry_backoff_max_s: float = Field(default=8.0, gt=0, description="Max backoff delay")
    drop_unsupported_params: bool = Field(
        default=True,
        description="Drop OpenAI params not supported by provider (multi-provider safety)",
    )

    ollama_enabled: bool = Field(default=True, description="Enable Ollama provider")
    ollama_api_base: str = Field(default="http://localhost:11434", description="Ollama server URL")
    ollama_model: str = Field(default="ollama/llama3.2", description="Ollama model (ollama/ prefix)")
    ollama_num_ctx: int | None = Field(
        default=None,
        gt=0,
        description="Ollama context length in tokens (num_ctx); unset = server default (env: LLM_OLLAMA_NUM_CTX)",
    )
    ollama_force_chat: bool = Field(default=True, description="Use chat endpoint for Ollama")

    gemini_enabled: bool = Field(default=True, description="Enable Gemini (Google AI Studio)")
    gemini_api_key: str | None = Field(default=None, description="API key (env: LLM_GEMINI_API_KEY)")
    gemini_model: str = Field(default="gemini/gemini-2.0-flash", description="Gemini model (gemini/ prefix)")
    gemini_safety_settings: dict | None = Field(default=None, description="Optional safety settings")

    router_policy: Literal["prefer_local", "prefer_cloud", "auto"] = Field(
        default="auto",
        description="Default routing policy",
    )
    fallback_order: list[LLMProvider] = Field(
        default=[LLMProvider.OLLAMA, LLMProvider.GEMINI],
        description="Provider order for fallback (env: LLM_FALLBACK_ORDER not parsed; set in code if needed)",
    )
    force_cloud_for_tools: bool = Field(default=False, description="Route tool-call requests to cloud")
    force_cloud_for_json: bool = Field(default=False, description="Route JSON-schema requests to cloud")

    persist_runs: bool = Field(default=True, description="Persist every run to llm_runs")
    persist_prompt_text: bool = Field(default=False, description="Store prompt/response previews (redacted)")
    cache_enabled: bool = Field(default=False, description="Content-addressable response cache")
    cache_ttl_s: int = Field(default=86400, gt=0, description="Cache TTL in seconds")

    @model_validator(mode="after")
    def validate_providers(self) -> "LLMSettings":
        if self.gemini_enabled and not self.gemini_api_key:
            raise ValueError("gemini_enabled=True requires gemini_api_key (set LLM_GEMINI_API_KEY)")
        if self.ollama_enabled and not (self.ollama_model or "").strip():
            raise ValueError("ollama_enabled=True requires non-empty ollama_model")
        if self.gemini_enabled and not (self.gemini_model or "").strip():
            raise ValueError("gemini_enabled=True requires non-empty gemini_model")
        return self
