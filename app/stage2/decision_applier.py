"""Stage 2 decision applier: apply parsed decision to claims (review_status, superseded_by, value_json.stage2)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models.claim import Claim
from app.stage1.repo import Stage1ClaimRepo
from app.stage2.schema import Stage2DecisionOutput

# When merging seed into canonical, append seed evidence to canonical only if canonical has at most this many
MAX_EVIDENCE_ON_CANONICAL = 10


def apply_decision(
    session: Session,
    decision: Stage2DecisionOutput,
    *,
    claim_repo: Stage1ClaimRepo | None = None,
) -> None:
    """
    Apply one Stage-2 decision to claims. Call within a transaction.

    - ACCEPT_AS_CANONICAL: seed -> ACCEPTED; ACTION pass may update value_json.stage2.action_endpoints.
    - MERGE_INTO: seed -> SUPERSEDED, superseded_by_id=canonical_claim_id; canonical -> ACCEPTED; seed evidence merged into canonical; ACTION pass may update action_endpoints.
      If canonical_claim_id is missing (no canonical was in context), treat as ACCEPT_AS_CANONICAL.
    - REJECT: seed -> REJECTED.
    - DEFER / SPLIT_CONFLICT: no change (leave UNREVIEWED).
    """
    repo = claim_repo or Stage1ClaimRepo()
    seed_id = decision.seed_claim_id
    kind = decision.decision.kind
    canonical_id = decision.decision.canonical_claim_id

    if kind == "DEFER" or kind == "SPLIT_CONFLICT":
        return

    if kind == "REJECT":
        repo.update_review_status(session, seed_id, "REJECTED", superseded_by_id=None)
        return

    if kind == "ACCEPT_AS_CANONICAL":
        repo.update_review_status(session, seed_id, "ACCEPTED", superseded_by_id=None)
        if decision.pass_kind == "ACTION":
            _merge_action_endpoints(repo, session, seed_id, decision)
        return

    if kind == "MERGE_INTO":
        if not canonical_id:
            # LLM returned MERGE_INTO but no canonical was in context (e.g. first of this type): accept seed as canonical
            repo.update_review_status(session, seed_id, "ACCEPTED", superseded_by_id=None)
            if decision.pass_kind == "ACTION":
                _merge_action_endpoints(repo, session, seed_id, decision)
            return
        canonical = repo.get_claim_with_evidence(session, canonical_id)
        if not canonical:
            return
        repo.update_review_status(session, seed_id, "SUPERSEDED", superseded_by_id=canonical_id)
        repo.update_review_status(session, canonical_id, "ACCEPTED", superseded_by_id=None)
        _merge_seed_evidence_into_canonical(repo, session, seed_id, canonical)
        if decision.pass_kind == "ACTION":
            _merge_action_endpoints(repo, session, canonical_id, decision)
        return


def _merge_seed_evidence_into_canonical(
    repo: Stage1ClaimRepo,
    session: Session,
    seed_id: str,
    canonical: Claim,
) -> None:
    """Append seed claim's evidence to the canonical claim. Do nothing if canonical already has > MAX_EVIDENCE_ON_CANONICAL."""
    if len(canonical.evidence or []) > MAX_EVIDENCE_ON_CANONICAL:
        return
    seed = repo.get_claim_with_evidence(session, seed_id)
    if not seed or not seed.evidence:
        return
    existing = {(ev.chunk_id, (ev.snippet_text or "").strip()) for ev in (canonical.evidence or [])}
    cap = MAX_EVIDENCE_ON_CANONICAL - len(existing)
    if cap <= 0:
        return
    for ev in seed.evidence:
        if cap <= 0:
            break
        key = (ev.chunk_id, (ev.snippet_text or "").strip())
        if key in existing:
            continue
        repo.create_evidence(
            session,
            claim_id=canonical.id,
            chunk_id=ev.chunk_id,
            snippet_text=ev.snippet_text or "",
            char_start=ev.char_start,
            char_end=ev.char_end,
        )
        existing.add(key)
        cap -= 1


def _merge_action_endpoints(
    repo: Stage1ClaimRepo,
    session: Session,
    claim_id: str,
    decision: Stage2DecisionOutput,
) -> None:
    """Resolve actor/object claim IDs to canonical and store in value_json.stage2.action_endpoints."""
    ep = decision.attachments.action_endpoints
    actor_id = ep.actor_claim_id
    object_id = ep.object_claim_id
    if actor_id:
        actor_id = repo.resolve_canonical_claim_id(session, actor_id) or actor_id
    if object_id:
        object_id = repo.resolve_canonical_claim_id(session, object_id) or object_id
    stage2: dict = {"action_endpoints": {"actor_claim_id": actor_id, "object_claim_id": object_id}}
    repo.merge_value_json_stage2(session, claim_id, stage2)
