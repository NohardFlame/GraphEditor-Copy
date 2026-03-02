"""Stage 3 prompt builder: load prompts, inject evidence windows and candidate cards."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

CARD_PROMPT_FILES = {
    "ACTOR": "actor_card.md",
    "OBJECT": "object_card.md",
    "STATE": "state_card.md",
    "ACTION": "action_card.md",
}


def _read_prompt(name: str) -> str:
    p = _PROMPTS_DIR / name
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8").strip()


def _format_evidence_windows(windows: list[dict[str, Any]]) -> str:
    """Format evidence windows for prompt: each snippet and its window_text."""
    lines = []
    for i, w in enumerate(windows, 1):
        snippet = (w.get("snippet") or "").strip()
        window_text = (w.get("window_text") or "").strip()
        lines.append(f"--- Window {i} ---")
        if snippet:
            lines.append(f"snippet: {snippet}")
        if window_text:
            lines.append(f"context: {window_text}")
    return "\n\n".join(lines) if lines else "(no evidence)"


def _format_canonical_block(claim_id: str, claim_type: str, value: dict[str, Any]) -> str:
    """Format canonical claim for actor/object: id, type, and value name."""
    lines = [f"canonical_claim_id: {claim_id}", f"type: {claim_type}"]
    name = (value.get("name") or "").strip() if claim_type in ("ACTOR", "OBJECT") else ""
    if name:
        lines.append(f"name: {name}")
    return "\n".join(lines)


def _format_candidate_objects(candidates: list[dict[str, Any]]) -> str:
    """Format for state card: object_id and summary per candidate."""
    lines = []
    for c in candidates:
        oid = c.get("object_id") or c.get("canonical_claim_id") or ""
        summary = (c.get("summary") or "").strip()
        lines.append(f"object_id: {oid}")
        if summary:
            lines.append(f"summary: {summary}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_candidate_objects_with_states(candidates: list[dict[str, Any]]) -> str:
    """Format for action card: object_id, summary, and baked states list."""
    lines = []
    for c in candidates:
        oid = c.get("object_id") or c.get("canonical_claim_id") or ""
        summary = (c.get("summary") or "").strip()
        states = c.get("states") or []
        lines.append(f"object_id: {oid}")
        if summary:
            lines.append(f"summary: {summary}")
        if states:
            lines.append("states: " + "; ".join(str(s) for s in states))
        lines.append("")
    return "\n".join(lines).strip()


def _format_candidate_actors(candidates: list[dict[str, Any]]) -> str:
    """Format for action card: actor_id and summary per candidate."""
    lines = []
    for c in candidates:
        aid = c.get("actor_id") or c.get("canonical_claim_id") or ""
        summary = (c.get("summary") or "").strip()
        lines.append(f"actor_id: {aid}")
        if summary:
            lines.append(f"summary: {summary}")
        lines.append("")
    return "\n".join(lines).strip()


def build_actor_prompt(
    evidence_windows: list[dict[str, Any]],
    canonical_claim_id: str,
    claim_type: str,
    value: dict[str, Any],
) -> str:
    content = _read_prompt(CARD_PROMPT_FILES["ACTOR"])
    if not content:
        return ""
    windows_text = _format_evidence_windows(evidence_windows)
    canonical_text = _format_canonical_block(canonical_claim_id, claim_type, value)
    content = content.replace("<EVIDENCE_WINDOWS>", windows_text)
    content = content.replace("<CANONICAL_BLOCK>", canonical_text)
    return content


def build_object_prompt(
    evidence_windows: list[dict[str, Any]],
    canonical_claim_id: str,
    claim_type: str,
    value: dict[str, Any],
) -> str:
    content = _read_prompt(CARD_PROMPT_FILES["OBJECT"])
    if not content:
        return ""
    windows_text = _format_evidence_windows(evidence_windows)
    canonical_text = _format_canonical_block(canonical_claim_id, claim_type, value)
    content = content.replace("<EVIDENCE_WINDOWS>", windows_text)
    content = content.replace("<CANONICAL_BLOCK>", canonical_text)
    return content


def build_state_prompt(
    evidence_windows: list[dict[str, Any]],
    candidate_objects: list[dict[str, Any]],
) -> str:
    content = _read_prompt(CARD_PROMPT_FILES["STATE"])
    if not content:
        return ""
    windows_text = _format_evidence_windows(evidence_windows)
    candidates_text = _format_candidate_objects(candidate_objects) if candidate_objects else "(no candidates)"
    content = content.replace("<EVIDENCE_WINDOWS>", windows_text)
    content = content.replace("<CANDIDATE_OBJECTS>", candidates_text)
    return content


def build_action_prompt(
    evidence_windows: list[dict[str, Any]],
    candidate_actors: list[dict[str, Any]],
    candidate_objects: list[dict[str, Any]],
) -> str:
    content = _read_prompt(CARD_PROMPT_FILES["ACTION"])
    if not content:
        return ""
    windows_text = _format_evidence_windows(evidence_windows)
    actors_text = _format_candidate_actors(candidate_actors) if candidate_actors else "(no candidates)"
    objects_text = _format_candidate_objects_with_states(candidate_objects) if candidate_objects else "(no candidates)"
    content = content.replace("<EVIDENCE_WINDOWS>", windows_text)
    content = content.replace("<CANDIDATE_ACTORS>", actors_text)
    content = content.replace("<CANDIDATE_OBJECTS>", objects_text)
    return content


def build_messages(kind: str, **kwargs: Any) -> list[dict[str, str]]:
    """
    Build [system, user] messages for the LLM.
    kwargs: evidence_windows (required), plus canonical_claim_id/claim_type/value for actor/object,
    candidate_objects for state, candidate_actors + candidate_objects for action.
    """
    system = "You must respond with a single JSON object only. No markdown, no explanation outside JSON."
    user_content: str
    if kind == "ACTOR":
        user_content = build_actor_prompt(
            kwargs["evidence_windows"],
            kwargs["canonical_claim_id"],
            kwargs.get("claim_type", "ACTOR"),
            kwargs.get("value") or {},
        )
    elif kind == "OBJECT":
        user_content = build_object_prompt(
            kwargs["evidence_windows"],
            kwargs["canonical_claim_id"],
            kwargs.get("claim_type", "OBJECT"),
            kwargs.get("value") or {},
        )
    elif kind == "STATE":
        user_content = build_state_prompt(
            kwargs["evidence_windows"],
            kwargs.get("candidate_objects") or [],
        )
    elif kind == "ACTION":
        user_content = build_action_prompt(
            kwargs["evidence_windows"],
            kwargs.get("candidate_actors") or [],
            kwargs.get("candidate_objects") or [],
        )
    else:
        user_content = ""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
