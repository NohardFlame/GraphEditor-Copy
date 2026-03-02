"""Stage 3 evidence packaging: snippet windows from claim evidence + ChunkRepo."""
from __future__ import annotations

import unicodedata
from typing import Any

from sqlalchemy.orm import Session

from app.db.models.claim import Claim
from app.db.repositories.chunk_repo import ChunkRepo

# Plan §5.1: ±200 chars around snippet
WINDOW_BEFORE_CHARS = 200
WINDOW_AFTER_CHARS = 200
# Plan §5.2: up to K snippet windows per claim
DEFAULT_MAX_WINDOWS = 10

_REPLACEMENT_CHAR = "\uFFFD"
_SANITIZE_CATEGORIES = frozenset(("Cc", "Cf", "Co", "Cn", "Cs"))


def _sanitize_string(s: str) -> str:
    """Replace invalid/problematic Unicode so the prompt does not trigger empty LLM responses."""
    if _REPLACEMENT_CHAR in s:
        s = s.replace(_REPLACEMENT_CHAR, "?")
    result = []
    for c in s:
        cat = unicodedata.category(c)
        if cat in _SANITIZE_CATEGORIES:
            result.append("?")
        else:
            result.append(c)
    return "".join(result)


def _sanitize_windows(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize string fields in each window dict."""
    out = []
    for w in windows:
        out.append({
            "chunk_id": w.get("chunk_id"),
            "snippet": _sanitize_string((w.get("snippet") or "").strip()),
            "window_text": _sanitize_string((w.get("window_text") or "").strip()),
            **{k: v for k, v in w.items() if k not in ("chunk_id", "snippet", "window_text")},
        })
    return out


def build_evidence_windows(
    session: Session,
    claim: Claim,
    chunk_repo: ChunkRepo | None = None,
    *,
    max_windows: int = DEFAULT_MAX_WINDOWS,
    before_chars: int = WINDOW_BEFORE_CHARS,
    after_chars: int = WINDOW_AFTER_CHARS,
) -> list[dict[str, Any]]:
    """
    Build up to max_windows snippet windows for the claim.
    Prefer chunks that contribute more snippets; then earlier chunk order.
    If the claim has no evidence, build one fallback window from the claim's chunk (first N chars).
    Returns list of dicts with chunk_id, snippet, window_text (and optional metadata).
    """
    chunk_repo = chunk_repo or ChunkRepo()
    evidence_list = list(claim.evidence or [])
    if not evidence_list:
        # Fallback 1: use claim's chunk_id to get chunk text (first 400 chars)
        chunk_id = getattr(claim, "chunk_id", None)
        if chunk_id:
            mapping = chunk_repo.get_by_ids(session, [chunk_id], max_text_chars=400)
            text = (mapping.get(chunk_id) or "").strip()
            if text:
                windows = [{"chunk_id": chunk_id, "snippet": "", "window_text": text}]
                return _sanitize_windows(windows)
        # Fallback 2: no chunk text; use placeholder so the card still runs (LLM can use value_json)
        chunk_id = chunk_id or ""
        windows = [{
            "chunk_id": chunk_id,
            "snippet": "",
            "window_text": "(No excerpt available for this claim.)",
        }]
        return _sanitize_windows(windows)

    # Group by chunk_id and preserve order of first occurrence for tie-break
    by_chunk: dict[str, list[Any]] = {}
    chunk_order: list[str] = []
    for ev in evidence_list:
        cid = ev.chunk_id
        if cid not in by_chunk:
            by_chunk[cid] = []
            chunk_order.append(cid)
        by_chunk[cid].append(ev)

    # Sort chunk_ids: more snippets first, then earlier in chunk_order
    def key(cid: str) -> tuple[int, int]:
        return (-len(by_chunk[cid]), chunk_order.index(cid) if cid in chunk_order else 999)

    ordered_chunk_ids = sorted(by_chunk.keys(), key=key)

    windows: list[dict[str, Any]] = []
    for cid in ordered_chunk_ids:
        if len(windows) >= max_windows:
            break
        for ev in by_chunk[cid]:
            if len(windows) >= max_windows:
                break
            snippet = (ev.snippet_text or "").strip()
            window_text = chunk_repo.get_excerpt_around_snippet(
                session,
                cid,
                snippet,
                before_chars=before_chars,
                after_chars=after_chars,
                fallback_chars=200,
            )
            windows.append({
                "chunk_id": cid,
                "snippet": snippet,
                "window_text": window_text,
            })

    return _sanitize_windows(windows)
