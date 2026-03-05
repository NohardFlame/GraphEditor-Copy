"""Adapter so LLMService can be used as ComparatorLLMClient (Phase 5)."""
from __future__ import annotations

from typing import Any

from app.llm.service import LLMService
from app.llm.types import LLMRequest


class LLMServiceComparatorAdapter:
    """Wraps LLMService to satisfy ComparatorLLMClient (async chat returning response with .text)."""

    def __init__(self, service: LLMService, *, run_repo: Any = None) -> None:
        self._service = service
        self._run_repo = run_repo

    async def chat(
        self,
        req: LLMRequest,
        *,
        run_repo: Any = None,
        workspace_id: str | None = None,
    ) -> Any:
        """Execute chat; returns response with .text (LLMResponse)."""
        return await self._service.chat(
            req,
            run_repo=run_repo or self._run_repo,
            workspace_id=workspace_id,
        )
