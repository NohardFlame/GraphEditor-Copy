"""Repository for canonicalization settings (workspace-scoped or global default)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.extraction import CanonicalizationSettings


DEDUP_STRATEGY_LLM_TOURNAMENT = "LLM_TOURNAMENT"
DEDUP_STRATEGY_ALGO_THRESHOLDS = "ALGO_THRESHOLDS"


def get_or_create_for_workspace(
    session: Session,
    workspace_id: str,
) -> CanonicalizationSettings:
    """Return settings for the workspace, or create with defaults. Falls back to global (workspace_id=None) if no workspace row exists."""
    row = session.execute(
        select(CanonicalizationSettings).where(
            CanonicalizationSettings.workspace_id == workspace_id
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    global_row = session.execute(
        select(CanonicalizationSettings).where(
            CanonicalizationSettings.workspace_id.is_(None)
        )
    ).scalar_one_or_none()
    if global_row is not None:
        return global_row
    settings = CanonicalizationSettings(
        workspace_id=None,
        dedup_strategy=DEDUP_STRATEGY_LLM_TOURNAMENT,
        min_mentions_for_canonical=5,
        cosine_high_merge_threshold=0.9,
        cosine_low_new_threshold=0.2,
    )
    session.add(settings)
    session.flush()
    return settings
