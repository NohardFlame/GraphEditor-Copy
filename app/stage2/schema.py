"""Stage 2 decision output schema (matches output_schema.md)."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvidenceRef(BaseModel):
    """Internal: full reference after resolution (used by applier and audit)."""
    model_config = ConfigDict(extra="forbid")
    claim_id: str
    evidence_id: str | None = None
    chunk_id: str | None = None
    snippet: str | None = None


class EvidenceRefLLM(BaseModel):
    """LLM-facing: only snippet; claim_id, evidence_id, chunk_id are filled server-side."""
    model_config = ConfigDict(extra="forbid")
    snippet: str = ""


class DecisionBlock(BaseModel):
    """Internal decision block; canonical_claim_id set by runner when kind==MERGE_INTO."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "ACCEPT_AS_CANONICAL",
        "MERGE_INTO",
        "REJECT",
        "DEFER",
        "SPLIT_CONFLICT",
    ]
    canonical_claim_id: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class ActionEndpoints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor_claim_id: str | None = None
    object_claim_id: str | None = None


class StateEndpoints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_claim_id: str | None = None


class AttachmentsBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_claim_ids: list[str] = Field(default_factory=list)
    actor_claim_ids: list[str] = Field(default_factory=list)
    action_endpoints: ActionEndpoints = Field(default_factory=ActionEndpoints)
    state_endpoints: StateEndpoints = Field(default_factory=StateEndpoints)

    @field_validator("object_claim_ids", "actor_claim_ids", mode="before")
    @classmethod
    def drop_nulls_from_id_lists(cls, v: object) -> list[str]:
        """LLM sometimes returns [null]; we only allow non-empty strings."""
        if not isinstance(v, list):
            return []
        return [x for x in v if isinstance(x, str) and x.strip()]


DECISION_KINDS = ("ACCEPT_AS_CANONICAL", "MERGE_INTO", "REJECT", "DEFER", "SPLIT_CONFLICT")


class DecisionBlockLLM(BaseModel):
    """LLM-facing: only kind (required) and evidence_refs; IDs filled server-side. We coerce empty/invalid kind to DEFER."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "ACCEPT_AS_CANONICAL",
        "MERGE_INTO",
        "REJECT",
        "DEFER",
        "SPLIT_CONFLICT",
    ]
    evidence_refs: list[EvidenceRefLLM] = Field(default_factory=list)
    # Optional; accepted for consistency but ignored by the system (not used in validation or applier).
    new_entity_reason: str | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def coerce_kind(cls, v: object) -> str:
        """Coerce empty or invalid kind to DEFER so we can parse and leave seed UNREVIEWED."""
        if v in DECISION_KINDS:
            return v
        if isinstance(v, str) and v.strip():
            vn = v.strip().upper().replace(" ", "_")
            if vn in DECISION_KINDS:
                return vn
        return "DEFER"


PASS_KINDS = ("ACTOR", "OBJECT", "STATE", "ACTION")


class Stage2DecisionOutputLLM(BaseModel):
    """LLM output: decision (kind + optional evidence_refs) and optional attachments. pass_kind/canonical_claim_id set by runner."""

    model_config = ConfigDict(extra="ignore")

    decision: DecisionBlockLLM
    attachments: AttachmentsBlock = Field(default_factory=AttachmentsBlock)


class Stage2DecisionOutput(BaseModel):
    """Internal Stage-2 decision (pass_kind, seed_claim_id, canonical_claim_id set by runner); used by applier and audit."""

    model_config = ConfigDict(extra="ignore")

    pass_kind: Annotated[
        Literal["ACTOR", "OBJECT", "STATE", "ACTION"],
        Field(description="Must match the pass that produced this decision"),
    ]
    seed_claim_id: str
    decision: DecisionBlock
    attachments: AttachmentsBlock = Field(default_factory=AttachmentsBlock)
