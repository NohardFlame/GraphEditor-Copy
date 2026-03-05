"""Embedding text templates for canonical entities (Phase 4)."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.models.extraction import Canonical, CanonicalAlias, ObjectState

_MAX_ALIASES = 5


def _alias_list(aliases: list[CanonicalAlias] | None) -> str:
    if not aliases:
        return ""
    texts = [a.alias_text for a in aliases[:_MAX_ALIASES]]
    return ", ".join(texts)


def build_actor_embedding_text(
    canonical: Canonical,
    aliases: list[CanonicalAlias] | None = None,
    context_snippet: str | None = None,
) -> str:
    """Build identity-focused text for embedding an actor canonical."""
    parts = [f"ACTOR: {canonical.name}."]
    if aliases:
        parts.append(f" Aliases: {_alias_list(aliases)}.")
    if context_snippet:
        parts.append(f" Context: {context_snippet[:200]}.")
    return "".join(parts)


def build_object_embedding_text(
    canonical: Canonical,
    aliases: list[CanonicalAlias] | None = None,
    context_snippet: str | None = None,
) -> str:
    """Build identity-focused text for embedding an object canonical."""
    parts = [f"OBJECT: {canonical.name}."]
    if aliases:
        parts.append(f" Aliases: {_alias_list(aliases)}.")
    if context_snippet:
        parts.append(f" Context: {context_snippet[:200]}.")
    return "".join(parts)


def build_action_embedding_text(
    canonical: Canonical,
    actor_canonical: Canonical | None,
    object_canonical: Canonical | None,
    aliases: list[CanonicalAlias] | None = None,
    verb_norm: str | None = None,
) -> str:
    """Build identity-focused text for embedding an action canonical."""
    actor_name = actor_canonical.name if actor_canonical else canonical.name
    object_name = object_canonical.name if object_canonical else ""
    verb = verb_norm or canonical.norm_name
    parts = [f"ACTION: {actor_name} {verb} {object_name}.".strip()]
    if aliases:
        parts.append(f" Synonyms: {_alias_list(aliases)}.")
    return "".join(parts)


def build_state_embedding_text(
    object_canonical: Canonical,
    state: ObjectState,
) -> str:
    """Build identity-focused text for embedding an object state."""
    return f"STATE of {object_canonical.name}: {state.state_name}."
