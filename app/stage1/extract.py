"""Stage 1 LLM extraction: call LLM, parse JSON, validate (no repair)."""
from __future__ import annotations

import logging
import re
from typing import Any

from app.llm.client_litellm import LiteLLMClient
from app.llm.providers import completion_kwargs_for_provider
from app.llm.settings import LLMSettings
from app.llm.types import LLMMessage, LLMRequest, LLMProvider
from app.stage1.parser_registry import parse_with_registry
from app.stage1.schema import Stage1ExtractionResult
from app.stage1.schema_v4 import Stage1ResultV4

PROMPT_VERSION_V4 = "chunk_claims_extract_v4_minimal_explicit_v2"
logger = logging.getLogger(__name__)


def _get_extraction_builder(prompt_version: str):
    """Return build_extraction_messages for the given prompt version."""
    if prompt_version == PROMPT_VERSION_V4:
        from app.stage1 import prompt_v4
        return prompt_v4.build_extraction_messages
    from app.stage1 import prompt
    return prompt.build_extraction_messages

# Default model_id format: "provider/modelname" e.g. ollama/llama3.2
def _provider_from_model_id(model_id: str) -> LLMProvider:
    prefix = (model_id or "").split("/")[0].lower()
    if prefix == "gemini" or prefix.startswith("gemini/"):
        return LLMProvider.GEMINI
    return LLMProvider.OLLAMA


def _strip_json_block(raw: str) -> str:
    """Remove markdown code fence if present."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()


async def extract_one_chunk(
    chunk_id: str,
    chunk_text: str,
    model_id: str,
    temperature: float,
    max_tokens: int,
    client: LiteLLMClient | None = None,
    timeout_s: float = 210.0,
    prompt_version: str = PROMPT_VERSION_V4,
) -> tuple[Stage1ExtractionResult | Stage1ResultV4 | None, str | None, str | None, dict[str, Any] | None]:
    """
    Call LLM to extract claims from one chunk. Returns (result, raw_text, error_message, usage_dict).
    On parse/validation failure, returns None with error message (no repair).
    """
    client = client or LiteLLMClient(concurrency_limit=8, max_retries=2)
    provider = _provider_from_model_id(model_id)
    build_extraction_messages = _get_extraction_builder(prompt_version)
    messages = build_extraction_messages(chunk_id, chunk_text or "")
    req = LLMRequest(
        messages=[LLMMessage(role=m["role"], content=m["content"]) for m in messages],
        temperature=temperature,
        max_output_tokens=max_tokens,
        response_format={"type": "json_object"},
        cache_system_prompt=True,
    )
    completion_kwargs = completion_kwargs_for_provider(provider, LLMSettings())
    raw_text: str | None = None
    usage_dict: dict[str, Any] | None = None
    try:
        resp = await client.acompletion(
            provider, model_id, req, timeout_s=timeout_s, **completion_kwargs
        )
        raw_text = resp.text
        usage_dict = {
            "prompt_tokens": resp.usage.input_tokens if resp.usage else 0,
            "completion_tokens": resp.usage.output_tokens if resp.usage else 0,
            "total_tokens": resp.usage.total_tokens if resp.usage else 0,
            "latency_ms": resp.latency_ms,
        }
    except Exception as e:
        logger.warning("LLM call failed for chunk %s: %s", chunk_id, e)
        return None, None, str(e), None

    # Parse and validate (registry: v3/v4 schema + v4 post-validators)
    chunk_text_norm = chunk_text or ""
    try:
        cleaned = _strip_json_block(raw_text)
        result, extra_warnings = parse_with_registry(
            cleaned, chunk_id, chunk_text_norm, prompt_version=prompt_version
        )
        result.warnings = list(getattr(result, "warnings", [])) + extra_warnings
        return result, raw_text, None, usage_dict
    except Exception as e:
        logger.warning("Parse/validation failed for chunk %s: %s", chunk_id, e)
        return None, raw_text, str(e), usage_dict
