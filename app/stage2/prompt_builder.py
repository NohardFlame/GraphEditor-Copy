"""Stage 2 prompt builder: load prompt files, compose system/user with context pack."""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Prompts live in app/stage2/prompts/
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

PLACEHOLDER = "<CONTEXT PACK>"

# Value fields to show per claim type (unpacked, no claim_id / dedupe_key / value_json)
_VALUE_FIELDS: dict[str, list[str]] = {
    "ACTOR": ["name"],
    "OBJECT": ["name"],
    "STATE": ["object_name", "state"],
    "ACTION": ["actor", "verb", "object", "qualifiers"],
    "DENY": ["actor", "verb", "object"],
}

PASS_PROMPT_FILES = {
    "ACTOR": "actor_resolution.md",
    "OBJECT": "object_resolution.md",
    "STATE": "state_resolution.md",
    "ACTION": "action_resolution.md",
}


def _read_file(name: str) -> str:
    p = _PROMPTS_DIR / name
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8").strip()


def _extract_section(content: str, header: str) -> str:
    """Extract content after '## HEADER' until next ## or end."""
    marker = f"## {header}"
    idx = content.find(marker)
    if idx == -1:
        return ""
    start = idx + len(marker)
    rest = content[start:].lstrip()
    # Stop at next ##
    next_h2 = rest.find("\n## ")
    if next_h2 != -1:
        rest = rest[:next_h2]
    return rest.strip()


def _format_value_fields(claim_type: str, value: dict[str, Any]) -> list[str]:
    """Return lines 'key: value' for this claim type's value fields only."""
    lines = []
    fields = _VALUE_FIELDS.get(claim_type, [])
    for key in fields:
        v = value.get(key)
        if v is None:
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        else:
            v = str(v).strip()
        if v:
            lines.append(f"{key}: {v}")
    return lines


def _format_evidence_with_excerpts(evidence_list: list[dict[str, Any]]) -> list[str]:
    """Format evidence list: each snippet, and if present its chunk_excerpt."""
    lines = []
    for e in evidence_list:
        snippet = (e.get("snippet") or "").strip()
        chunk_excerpt = (e.get("chunk_excerpt") or "").strip()
        if snippet:
            lines.append(f"evidence: {snippet}")
        if chunk_excerpt:
            lines.append(chunk_excerpt)
    return lines


def _format_block(block: dict[str, Any]) -> str:
    """Format one claim block: type, value fields, evidence (with per-evidence excerpts if present), or legacy chunk_excerpt."""
    claim_type = block.get("claim_type", "")
    value = block.get("value") or {}
    evidence_list = block.get("evidence") or []
    chunk_excerpt_block = (block.get("chunk_excerpt") or "").strip()

    lines = [f"type: {claim_type}"]
    lines.extend(_format_value_fields(claim_type, value))

    # Per-evidence excerpts: if any evidence item has chunk_excerpt, show each snippet + excerpt
    has_per_evidence_excerpts = any((e.get("chunk_excerpt") or "").strip() for e in evidence_list)
    if has_per_evidence_excerpts:
        lines.extend(_format_evidence_with_excerpts(evidence_list))
    else:
        # Legacy: snippets only, then single block-level chunk_excerpt (e.g. seed)
        snippets = [e.get("snippet", "").strip() for e in evidence_list if (e.get("snippet") or "").strip()]
        if snippets:
            lines.append("evidence: " + " | ".join(snippets))
        elif "evidence:" not in "\n".join(lines):
            lines.append("evidence:")
        if chunk_excerpt_block:
            lines.append(chunk_excerpt_block)
    return "\n".join(lines)


def _format_canonical_block(block: dict[str, Any]) -> str:
    """Format closest-canonical block with canonical_claim_id on first line for MERGE_INTO."""
    claim_id = block.get("claim_id", "")
    rest = _format_block(block)
    return f"canonical_claim_id: {claim_id}\n{rest}"


def format_context_pack_to_text(pack: dict[str, Any]) -> str:
    """Render context pack as unpacked text blocks (no JSON, no claim_id / dedupe_key)."""
    parts = []
    # Seed
    seed = pack.get("seed")
    if seed:
        parts.append("--- Seed ---")
        parts.append(_format_block(seed))
    # Closest canonical (same type, already accepted) — only block that exposes claim_id for MERGE_INTO
    closest = pack.get("closest_canonical")
    if closest:
        parts.append("\n--- Closest canonical (same type, already accepted) ---")
        parts.append(_format_canonical_block(closest))
    # Same-type neighbors
    neighbors = pack.get("same_type_neighbors") or []
    if neighbors:
        parts.append("\n--- Same-type neighbors ---")
        for i, blk in enumerate(neighbors, 1):
            parts.append(f"\n--- Neighbor {i} ---")
            parts.append(_format_block(blk))
    # Cross-type by type
    cross = pack.get("cross_type") or {}
    for ct, blocks in sorted(cross.items()):
        if not blocks:
            continue
        parts.append(f"\n--- Cross-type {ct} ---")
        for i, blk in enumerate(blocks, 1):
            parts.append(f"\n--- {ct} {i} ---")
            parts.append(_format_block(blk))
    return "\n".join(parts).strip()


def get_system_message(pass_kind: str) -> str:
    """Compose system message: common_instructions + pass-specific SYSTEM + output schema."""
    common = _read_file("common_instructions.md")
    filename = PASS_PROMPT_FILES.get(pass_kind)
    if not filename:
        return common
    pass_content = _read_file(filename)
    system_part = _extract_section(pass_content, "SYSTEM")
    schema_content = _read_file("output_schema.md")
    parts = [common]
    if system_part:
        parts.append(system_part)
    if schema_content:
        parts.append(f"## Output schema\n\n{schema_content}")
    return "\n\n".join(parts)


def get_user_message(pass_kind: str, context_pack: dict) -> str:
    """Compose user message: pass-specific USER section with context pack as unpacked text."""
    filename = PASS_PROMPT_FILES.get(pass_kind)
    if not filename:
        return format_context_pack_to_text(context_pack)
    pass_content = _read_file(filename)
    user_part = _extract_section(pass_content, "USER")
    if not user_part:
        return format_context_pack_to_text(context_pack)
    pack_text = format_context_pack_to_text(context_pack)
    return user_part.replace(PLACEHOLDER, pack_text)


def build_messages(pass_kind: str, context_pack: dict) -> list[dict[str, str]]:
    """Build [system, user] messages for LLM. Same shape as Stage-1 extract (role + content)."""
    return [
        {"role": "system", "content": get_system_message(pass_kind)},
        {"role": "user", "content": get_user_message(pass_kind, context_pack)},
    ]
