"""
LLMService: single public entrypoint. Orchestrates router, client, persistence, policy.
Other modules import only LLMService (and types/profiles). Never create sessions or commit.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.llm.client_litellm import LiteLLMClient
from app.llm.errors import (
    LLMError,
    LLMInProgressError,
    LLMWorkspaceRequiredError,
)
from app.llm.ports import LLMClientPort, LLMRunRepoPort
from app.llm.providers import completion_kwargs_for_provider, gemini_kwargs, ollama_kwargs
from app.llm.router import LLMRouter
from app.llm.settings import LLMSettings
from app.llm.telemetry import log_llm_call, redact_preview, stable_hash
from app.llm.types import (
    LLMConstraints,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)


def _canonical_request_hash(req: LLMRequest, model: str, profile: str) -> str:
    """Stable hash for cache key: messages + model + profile + params that affect output."""
    parts = [
        json.dumps([m.model_dump() for m in req.messages], sort_keys=True),
        model,
        profile,
        str(req.temperature),
        str(req.max_output_tokens),
        json.dumps(req.tools or [], sort_keys=True),
        json.dumps(req.response_format or {}, sort_keys=True),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class LLMService:
    """Orchestrates routing, persistence, and guardrails. Caller provides session-bound repo."""

    def __init__(
        self,
        settings: LLMSettings,
        *,
        client: LLMClientPort | None = None,
        router: LLMRouter | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or LiteLLMClient(
            concurrency_limit=settings.concurrency_limit,
            max_retries=settings.max_retries,
            drop_params=settings.drop_unsupported_params,
        )
        self._router = router or LLMRouter(settings)

    async def chat(
        self,
        req: LLMRequest,
        *,
        run_repo: LLMRunRepoPort | None = None,
        workspace_id: str | None = None,
    ) -> LLMResponse:
        """
        Execute one chat completion. Uses router for provider selection and fallback.
        When persist_runs=True, workspace_id must be set (via arg or req.metadata["workspace_id"]).
        When run_repo is provided, it must be bound to the caller's session; service does not commit.
        """
        persist = self._settings.persist_runs
        ws_id = workspace_id or (req.metadata.get("workspace_id") if req.metadata else None)
        if persist and not ws_id:
            raise LLMWorkspaceRequiredError()
        pipeline_run_id = req.metadata.get("pipeline_run_id") if req.metadata else None
        stage = req.metadata.get("stage") if req.metadata else None

        constraints = LLMConstraints(
            requires_tools=bool(req.tools),
            requires_json=req.response_format is not None,
            allow_fallbacks=True,
        )
        timeout_s = req.timeout_s or self._settings.default_timeout_s

        # 1) Idempotency
        if req.idempotency_key and run_repo is not None:
            status = await run_repo.get_by_status_and_idempotency_key(req.idempotency_key)
            if status == "STARTED":
                raise LLMInProgressError(
                    f"Run with idempotency_key={req.idempotency_key!r} already in progress"
                )
            if status == "SUCCEEDED":
                cached = await run_repo.get_by_idempotency_key(req.idempotency_key)
                if cached:
                    return LLMResponse(
                        text=cached["text"],
                        provider=LLMProvider(cached["provider"]),
                        model=cached["model"],
                        latency_ms=cached["latency_ms"],
                        usage=LLMUsage(
                            input_tokens=cached.get("input_tokens", 0),
                            output_tokens=cached.get("output_tokens", 0),
                            total_tokens=cached.get("total_tokens", 0),
                        ),
                    )

        # 2) Cache (content-addressable)
        if self._settings.cache_enabled and run_repo is not None:
            provider = self._router.select_provider(req, constraints)
            model = self._router.get_model_for_provider(provider)
            prompt_sha256 = _canonical_request_hash(req, model, req.profile.value)
            cached = await run_repo.get_cached(
                prompt_sha256, model, req.profile.value, self._settings.cache_ttl_s
            )
            if cached:
                return LLMResponse(
                    text=cached["text"],
                    provider=LLMProvider(cached["provider"]),
                    model=cached["model"],
                    latency_ms=cached["latency_ms"],
                    usage=LLMUsage(
                        input_tokens=cached.get("input_tokens", 0),
                        output_tokens=cached.get("output_tokens", 0),
                        total_tokens=cached.get("total_tokens", 0),
                    ),
                )

        # 3) Select provider and run
        provider = self._router.select_provider(req, constraints)
        model = self._router.get_model_for_provider(provider)
        run_id: str | None = None
        if persist and run_repo is not None:
            prompt_sha256 = _canonical_request_hash(req, model, req.profile.value)
            run_id = await run_repo.create_started(
                workspace_id=ws_id,
                provider=provider.value,
                model=model,
                profile=req.profile.value,
                prompt_sha256=prompt_sha256,
                idempotency_key=req.idempotency_key,
                pipeline_run_id=pipeline_run_id,
                stage=stage,
            )

        # Try selected provider first, then rest of fallback list on retryable errors
        order = [provider] + [p for p in self._router.get_fallback_order() if p != provider]
        last_error: LLMError | None = None
        for current in order:
            if last_error is not None and not last_error.retryable:
                break
            model_current = self._router.get_model_for_provider(current)
            if current == LLMProvider.OLLAMA:
                k = ollama_kwargs(self._settings)
            else:
                k = gemini_kwargs(self._settings)
            try:
                completion_extra = completion_kwargs_for_provider(current, self._settings).get(
                    "extra_completion_kwargs"
                )
                resp = await self._client.acompletion(
                    current,
                    model_current,
                    req,
                    timeout_s=timeout_s,
                    api_base=k.get("api_base"),
                    api_key=k.get("api_key"),
                    extra_completion_kwargs=completion_extra,
                )
                if persist and run_repo is not None and run_id is not None:
                    await run_repo.mark_succeeded(
                        run_id,
                        response_text=resp.text,
                        response_sha256=stable_hash(resp.text),
                        latency_ms=resp.latency_ms,
                        input_tokens=resp.usage.input_tokens if resp.usage else 0,
                        output_tokens=resp.usage.output_tokens if resp.usage else 0,
                        total_tokens=resp.usage.total_tokens if resp.usage else 0,
                        cost_usd=resp.usage.cost_usd if resp.usage else None,
                        response_preview=redact_preview(resp.text) if self._settings.persist_prompt_text else None,
                        prompt_preview=None,
                    )
                    if req.idempotency_key:
                        await run_repo.save_result_for_idempotency_key(
                            req.idempotency_key,
                            run_id,
                            resp.text,
                            stable_hash(resp.text),
                            resp.latency_ms,
                            resp.usage.input_tokens if resp.usage else 0,
                            resp.usage.output_tokens if resp.usage else 0,
                            resp.usage.total_tokens if resp.usage else 0,
                            resp.provider.value,
                            resp.model,
                        )
                log_llm_call(
                    llm_run_id=run_id or "",
                    workspace_id=ws_id,
                    provider=resp.provider.value,
                    model=resp.model,
                    profile=req.profile.value,
                    stage=stage,
                    latency_ms=resp.latency_ms,
                    status="SUCCEEDED",
                )
                return resp
            except LLMError as e:
                last_error = e
                if persist and run_repo is not None and run_id is not None:
                    await run_repo.mark_failed(
                        run_id,
                        error_code=e.code,
                        error_message=str(e),
                        error_details_json=e.details,
                    )
                log_llm_call(
                    llm_run_id=run_id or "",
                    workspace_id=ws_id,
                    provider=current.value,
                    model=model_current,
                    profile=req.profile.value,
                    stage=stage,
                    latency_ms=0,
                    status="FAILED",
                    error_code=e.code,
                )
                if not e.retryable or not constraints.allow_fallbacks:
                    raise
                # try next provider
                continue
        if last_error is not None:
            raise last_error
        raise LLMError("No provider succeeded", code="UNKNOWN", retryable=False)
