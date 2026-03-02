"""
LiteLLM client wrapper: normalize request/response, timeouts, semaphore, retries.
Exception mapping (LiteLLM → LLMError):
  - litellm.exceptions.APITimeoutError / Timeout → LLMTimeout
  - litellm.exceptions.RateLimitError → LLMRateLimited
  - litellm.exceptions.AuthenticationError / PermissionDeniedError → LLMAuthError
  - litellm.exceptions.BadRequestError / InvalidRequestError → LLMBadRequest
  - litellm.exceptions.APIError / ServiceUnavailableError / APIConnectionError → LLMUnavailable
  - unknown → LLMUnavailable(retryable=True) or LLMError(UNKNOWN)
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import litellm
from litellm import acompletion

from app.llm.errors import (
    LLMAuthError,
    LLMBadRequest,
    LLMError,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)
from app.llm.telemetry import emit_error_metric, emit_latency_metric, emit_tokens_metric
from app.llm.types import LLMProvider, LLMRequest, LLMResponse, LLMUsage

# Exception mapping uses type(e).__name__ (see _map_exception) so LiteLLM layout changes are safe.


def _map_exception(e: Exception, provider: LLMProvider) -> LLMError:
    """Map LiteLLM/provider exceptions to LLMError. Uses class name so it works across import paths."""
    exc_name = type(e).__name__
    if isinstance(e, LLMError):
        return e
    # Map by exception class name (robust to different litellm.exceptions layouts)
    if exc_name in ("APITimeoutError", "Timeout"):
        return LLMTimeout(details=exc_name, provider=provider)
    if exc_name == "RateLimitError":
        return LLMRateLimited(details=exc_name, provider=provider)
    if exc_name in ("AuthenticationError", "PermissionDeniedError"):
        return LLMAuthError(details=exc_name, provider=provider)
    if exc_name in ("BadRequestError", "InvalidRequestError"):
        return LLMBadRequest(details=exc_name, provider=provider)
    if exc_name in ("ServiceUnavailableError", "APIConnectionError", "APIError"):
        return LLMUnavailable(str(e), details=exc_name, provider=provider)
    if getattr(e, "status_code", None) in (500, 502, 503, 504) or "timeout" in str(e).lower():
        return LLMUnavailable(str(e), details=exc_name, provider=provider, retryable=True)
    return LLMError(
        str(e),
        code="UNKNOWN",
        retryable=False,
        provider=provider,
        details=exc_name,
    )


def _request_to_kwargs(req: LLMRequest, model: str, timeout_s: float) -> dict[str, Any]:
    """Build LiteLLM completion kwargs from LLMRequest."""
    messages = [m.model_dump() for m in req.messages]
    if req.cache_system_prompt and messages and messages[0].get("role") == "system":
        content = messages[0].get("content")
        if isinstance(content, str):
            messages = [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
                *messages[1:],
            ]
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "timeout": timeout_s,
    }
    if req.temperature is not None:
        kwargs["temperature"] = req.temperature
    if req.max_output_tokens is not None:
        kwargs["max_tokens"] = req.max_output_tokens
    if req.tools is not None:
        kwargs["tools"] = req.tools
    if req.response_format is not None:
        kwargs["response_format"] = req.response_format
    return kwargs


def _response_from_completion(
    raw: Any,
    provider: LLMProvider,
    model: str,
    latency_ms: int,
) -> LLMResponse:
    """Build LLMResponse from LiteLLM response object."""
    text = ""
    usage = None
    finish_reason = None
    if hasattr(raw, "choices") and raw.choices:
        c0 = raw.choices[0]
        msg = getattr(c0, "message", None) or getattr(c0, "message", None)
        if msg:
            text = getattr(msg, "content", None) or getattr(msg, "content", "") or ""
            finish_reason = getattr(msg, "finish_reason", None) or getattr(c0, "finish_reason", None)
        else:
            text = getattr(c0, "text", None) or ""
    if hasattr(raw, "usage") and raw.usage:
        u = raw.usage
        usage = LLMUsage(
            input_tokens=getattr(u, "input_tokens", None) or getattr(u, "prompt_tokens", 0),
            output_tokens=getattr(u, "output_tokens", None) or getattr(u, "completion_tokens", 0),
            total_tokens=getattr(u, "total_tokens", 0),
            cost_usd=getattr(u, "cost_usd", None),
        )
    raw_dict: dict[str, Any] = {}
    if hasattr(raw, "model_dump"):
        raw_dict = raw.model_dump()
    elif hasattr(raw, "__dict__"):
        raw_dict = {k: v for k, v in raw.__dict__.items() if not k.startswith("_")}
    return LLMResponse(
        text=text or "",
        raw=raw_dict,
        usage=usage,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        finish_reason=finish_reason,
    )


class LiteLLMClient:
    """Async LiteLLM wrapper: semaphore, timeout, retries, request/response normalization."""

    def __init__(self, *, concurrency_limit: int = 8, max_retries: int = 2, drop_params: bool = True) -> None:
        self._sem = asyncio.Semaphore(concurrency_limit)
        self._max_retries = max_retries
        self._drop_params = drop_params

    async def acompletion(
        self,
        provider: LLMProvider,
        model: str,
        req: LLMRequest,
        *,
        timeout_s: float | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        extra_completion_kwargs: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Execute one completion. Applies semaphore, timeout, retries. Raises LLMError on failure."""
        timeout = timeout_s if timeout_s is not None else req.timeout_s or 60.0
        kwargs = _request_to_kwargs(req, model, timeout)
        if self._drop_params:
            kwargs["drop_params"] = True
        if api_base is not None:
            kwargs["api_base"] = api_base
        if api_key is not None:
            kwargs["api_key"] = api_key
        if extra_completion_kwargs:
            kwargs.update(extra_completion_kwargs)

        last_error: LLMError | None = None
        for attempt in range(self._max_retries + 1):
            async with self._sem:
                t0 = time.perf_counter()
                try:
                    raw = await acompletion(**kwargs)
                    latency_ms = int((time.perf_counter() - t0) * 1000)
                    resp = _response_from_completion(raw, provider, model, latency_ms)
                    emit_latency_metric(provider.value, model, float(latency_ms))
                    if resp.usage:
                        emit_tokens_metric(provider.value, model, "in", resp.usage.input_tokens)
                        emit_tokens_metric(provider.value, model, "out", resp.usage.output_tokens)
                    return resp
                except Exception as e:  # noqa: BLE001
                    err = _map_exception(e, provider)
                    last_error = err
                    emit_error_metric(provider.value, err.code)
                    if not err.retryable or attempt == self._max_retries:
                        raise err
                    delay = min(0.5 * (2**attempt), 8.0)
                    await asyncio.sleep(delay)
        if last_error is not None:
            raise last_error
        raise LLMUnavailable("Max retries exceeded", provider=provider)
