"""Stage 3 minimal card JSON schemas (summary required, rest optional)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ActorCardLLM(BaseModel):
    """LLM output for actor card. Code fills canonical id/name from claim."""
    model_config = ConfigDict(extra="ignore")
    summary: str = Field(..., min_length=1, description="3–10 sentences")
    possible_actions: list[str] = Field(default_factory=list)
    notes: str | None = None


class ObjectCardLLM(BaseModel):
    """LLM output for object card. Code fills canonical id/name from claim."""
    model_config = ConfigDict(extra="ignore")
    summary: str = Field(..., min_length=1, description="3–10 sentences")
    possible_states: list[str] = Field(default_factory=list)
    notes: str | None = None


class StateCardLLM(BaseModel):
    """LLM output for state card. candidate_objects are IDs from provided candidates."""
    model_config = ConfigDict(extra="ignore")
    summary: str = Field(..., min_length=1)
    candidate_objects: list[str] = Field(default_factory=list)
    notes: str | None = None


class ActionCardLLM(BaseModel):
    """LLM output for action card. candidate_* are IDs from provided candidates."""
    model_config = ConfigDict(extra="ignore")
    summary: str = Field(..., min_length=1)
    candidate_actors: list[str] = Field(default_factory=list)
    candidate_objects: list[str] = Field(default_factory=list)
    preconditions: str | list[str] | None = None
    effects: str | list[str] | None = None
    notes: str | None = None


def parse_actor_card(data: dict[str, Any]) -> dict[str, Any]:
    """Parse and return resolved actor card (with summary guaranteed)."""
    try:
        m = ActorCardLLM.model_validate(data)
        return m.model_dump(mode="json")
    except Exception:
        return _fallback_resolved(data, "summary")


def parse_object_card(data: dict[str, Any]) -> dict[str, Any]:
    """Parse and return resolved object card (with summary guaranteed)."""
    try:
        m = ObjectCardLLM.model_validate(data)
        return m.model_dump(mode="json")
    except Exception:
        return _fallback_resolved(data, "summary")


def parse_state_card(data: dict[str, Any]) -> dict[str, Any]:
    """Parse and return resolved state card (with summary guaranteed)."""
    try:
        m = StateCardLLM.model_validate(data)
        return m.model_dump(mode="json")
    except Exception:
        return _fallback_resolved(data, "summary")


def parse_action_card(data: dict[str, Any]) -> dict[str, Any]:
    """Parse and return resolved action card (with summary guaranteed)."""
    try:
        m = ActionCardLLM.model_validate(data)
        return m.model_dump(mode="json")
    except Exception:
        return _fallback_resolved(data, "summary")


def _fallback_resolved(data: dict[str, Any], required_key: str) -> dict[str, Any]:
    """Ensure at least required_key is present; use first N chars of raw if needed."""
    out: dict[str, Any] = {}
    if isinstance(data.get(required_key), str) and data[required_key].strip():
        out[required_key] = (data[required_key] or "").strip()
    else:
        raw = str(data)[:500]
        out[required_key] = raw if raw else "(no summary)"
    return out
