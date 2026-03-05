"""Big-extract prompt templates and builder for Phase 3 (big_extract_v1).

Used by both run_big_llm_extract (API path) and prepare_big_llm_export (manual path).
"""
from __future__ import annotations

from app.extraction.chunking import DocPart

PROMPT_VERSION_BIG_EXTRACT = "big_extract_v1"

BIG_EXTRACT_ALLOWED_MENTION_TYPES = ["ACTOR", "OBJECT", "ACTION", "STATE"]

BIG_EXTRACT_ALLOWED_REL_TYPES = [
    "ACTION_HAS_ACTOR",
    "ACTION_HAS_OBJECT",
    "OBJECT_HAS_STATE",
]

SYSTEM_PROMPT = """You are an extraction assistant for a business-domain document.

You receive a chunk-marked document: each segment is wrapped in markers like <<<CHUNK id="..." index=...>>> ... <<<END_CHUNK>>>.

Your task is to extract:
- **Frames**: smallest extraction units (sentence/bullet) per chunk.
- **Mentions**: typed entities (Actor, Object, Action, State) within each frame.
- **Relations**: explicit links between mentions (e.g. ACTION_HAS_ACTOR, ACTION_HAS_OBJECT, OBJECT_HAS_STATE).

Business model:
- **Actor**: participant (User, Admin, System, ExternalService).
- **Object**: business entity that can have states.
- **State**: label for an object's lifecycle state.
- **Action**: business action linking actor and object, possibly causing a state transition.

Rules:
1. Output MUST be valid JSON only (no comments, no trailing commas, no markdown, no extra text).
2. Every mention MUST have evidence.snippet that is a verbatim substring of its source chunk text.
3. Relations must be explicit objects; do not rely on co-location of mentions.
4. Use the exact schema described in the user message."""

USER_PROMPT_TEMPLATE = """Extract frames, mentions, and relations from the following chunk-marked document.

JSON schema (output this structure only):
- Top-level: "doc_id", "prompt_version", "frames", "references" (optional), "errors" (optional).
- Each frame: "frame_id", "chunk_id", "frame_index", "frame_text", "mentions", "relations".
- Each mention: "mention_id", "type", "fields", "epistemic", "evidence", "confidence".
  - evidence: "snippet" (required), "char_start", "char_end" (optional).
- Each relation: "src_mention_id", "rel_type", "dst_mention_id".

Allowed mention types: {mention_types}.
Allowed relation types: {rel_types}.

Instructions:
- Reuse chunk_id from the markers in the document.
- frame_id format: frame::<chunk_id>::<n> (n = frame index within that chunk).
- mention_id format: m::<frame_id>::<n>.
- epistemic: "EXPLICIT" or "INFERRED".
- If you extract references, put them in "references" with ref_id, chunk_id, ref_text_raw, ref_norm, evidence.

doc_id for this document: {doc_id}
Chunk index range for this part: {first_chunk_index} to {last_chunk_index} (inclusive).

DOCUMENT_START
{doc_part_text}
DOCUMENT_END

Respond with valid JSON only."""


def build_big_extract_prompts(doc_part: DocPart, doc_id: str) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for one DocPart.

    Used by run_big_llm_extract and prepare_big_llm_export.
    """
    user_prompt = USER_PROMPT_TEMPLATE.format(
        mention_types=", ".join(BIG_EXTRACT_ALLOWED_MENTION_TYPES),
        rel_types=", ".join(BIG_EXTRACT_ALLOWED_REL_TYPES),
        doc_id=doc_id,
        first_chunk_index=doc_part.first_chunk_index,
        last_chunk_index=doc_part.last_chunk_index,
        doc_part_text=doc_part.text,
    )
    return (SYSTEM_PROMPT, user_prompt)
