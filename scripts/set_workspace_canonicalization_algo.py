"""One-off: set canonicalization to ALGO_THRESHOLDS for a workspace.
Usage: python -m scripts.set_workspace_canonicalization_algo <workspace_id>
"""
from __future__ import annotations

import sys
from uuid import uuid4

from sqlalchemy import select

from app.db.session import init_db, session_scope
from app.db.models.extraction import CanonicalizationSettings
from app.db.repositories.canonical_settings_repo import DEDUP_STRATEGY_ALGO_THRESHOLDS


def main(workspace_id: str) -> int:
    init_db()
    with session_scope() as session:
        row = session.execute(
            select(CanonicalizationSettings).where(
                CanonicalizationSettings.workspace_id == workspace_id
            )
        ).scalar_one_or_none()
        if row is not None:
            row.dedup_strategy = DEDUP_STRATEGY_ALGO_THRESHOLDS
            row.min_mentions_for_canonical = 5
            row.cosine_high_merge_threshold = 0.9
            row.cosine_low_new_threshold = 0.2
            session.flush()
            print(f"Updated canonicalization_settings for workspace {workspace_id} -> ALGO_THRESHOLDS")
        else:
            settings = CanonicalizationSettings(
                id=str(uuid4()),
                workspace_id=workspace_id,
                dedup_strategy=DEDUP_STRATEGY_ALGO_THRESHOLDS,
                min_mentions_for_canonical=5,
                cosine_high_merge_threshold=0.9,
                cosine_low_new_threshold=0.2,
            )
            session.add(settings)
            session.flush()
            print(f"Inserted canonicalization_settings for workspace {workspace_id} -> ALGO_THRESHOLDS")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.set_workspace_canonicalization_algo <workspace_id>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
