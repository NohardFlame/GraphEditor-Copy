"""Unit tests for comparator v2: guardrails, short-circuit, input validation (03_comparator_integration_notes_v2)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.canonical.comparator import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    DECISION_DIFFERENT,
    DECISION_SAME,
    DECISION_UNSURE,
    DecisionResult,
    _apply_guardrails,
    _parse_response,
    compare_mention_with_candidate,
)
from app.canonical.models import Candidate


def _fake_mention(mention_type: str, text_or_fields: str | dict) -> MagicMock:
    """Minimal mention-like object for comparator (type + fields_json -> text)."""
    if isinstance(text_or_fields, dict):
        fields = text_or_fields
        text = (
            fields.get("name")
            or fields.get("actor_name")
            or fields.get("object_name")
            or fields.get("state_name")
            or fields.get("state")
            or ""
        )
    else:
        text = text_or_fields
        fields = {"name": text} if mention_type == "ACTOR" else {"name": text} if mention_type == "OBJECT" else {"state_name": text}
    m = MagicMock()
    m.type = mention_type
    m.fields_json = json.dumps(fields)
    m.evidence = None
    return m


# --- _parse_response ---


def test_parse_response_valid_json_returns_decision_result():
    data = {"decision": "SAME", "confidence": "HIGH", "reason": "Exact match."}
    result = _parse_response(json.dumps(data))
    assert result is not None
    assert result.decision == DECISION_SAME
    assert result.confidence == CONFIDENCE_HIGH
    assert "Exact" in result.reason


def test_parse_response_invalid_json_returns_none():
    assert _parse_response("not json") is None
    assert _parse_response("") is None
    assert _parse_response("[]") is None  # not a dict


def test_parse_response_missing_decision_returns_none():
    assert _parse_response(json.dumps({"confidence": "HIGH", "reason": "x"})) is None


def test_parse_response_strips_markdown_fence():
    payload = {"decision": "DIFFERENT", "confidence": "LOW", "reason": "x"}
    result = _parse_response("```json\n" + json.dumps(payload) + "\n```")
    assert result is not None
    assert result.decision == DECISION_DIFFERENT


# --- _apply_guardrails ---


def test_apply_guardrails_same_with_exact_claim_but_strings_differ_downgrades_to_unsure():
    """Model claims exact match when norm(mention) != norm(name) -> UNSURE LOW (plan test 6)."""
    result = DecisionResult(decision=DECISION_SAME, confidence=CONFIDENCE_HIGH, reason="Exact match.")
    candidate = Candidate(
        canonical_id="c1",
        name="Система",
        norm_name="система",
        aliases=[],
        score=0.9,
        payload={},
    )
    out = _apply_guardrails(result, "Пользователь", candidate)
    assert out.decision == DECISION_UNSURE
    assert out.confidence == CONFIDENCE_LOW
    assert "Guardrail" in out.reason or "exact" in out.reason.lower()


def test_apply_guardrails_same_with_alias_claim_but_no_aliases_downgrades_to_unsure():
    result = DecisionResult(decision=DECISION_SAME, confidence=CONFIDENCE_HIGH, reason="Alias match.")
    candidate = Candidate(
        canonical_id="c1",
        name="Document",
        norm_name="document",
        aliases=[],
        score=0.9,
        payload={},
    )
    out = _apply_guardrails(result, "док", candidate)
    assert out.decision == DECISION_UNSURE
    assert out.confidence == CONFIDENCE_LOW


def test_apply_guardrails_same_with_valid_exact_match_passes():
    result = DecisionResult(decision=DECISION_SAME, confidence=CONFIDENCE_HIGH, reason="Exact normalized match.")
    candidate = Candidate(
        canonical_id="c1",
        name="Документ",
        norm_name="документ",
        aliases=[],
        score=0.9,
        payload={},
    )
    out = _apply_guardrails(result, "Документ", candidate)
    assert out.decision == DECISION_SAME
    assert out.confidence == CONFIDENCE_HIGH


def test_apply_guardrails_different_unchanged():
    result = DecisionResult(decision=DECISION_DIFFERENT, confidence=CONFIDENCE_HIGH, reason="Different classes.")
    candidate = Candidate(canonical_id="c1", name="X", norm_name="x", aliases=[], score=0.9, payload={})
    out = _apply_guardrails(result, "Y", candidate)
    assert out.decision == DECISION_DIFFERENT
    assert out.confidence == CONFIDENCE_HIGH


# --- compare_mention_with_candidate: input validation & short-circuit ---


@pytest.mark.asyncio
async def test_compare_invalid_task_kind_returns_unsure_low():
    """Invalid task_kind -> do not call LLM; return UNSURE LOW (guardrails §4.1)."""
    m = _fake_mention("ACTOR", "User")
    c = Candidate(canonical_id="c1", name="User", norm_name="user", aliases=[], score=0.9, payload={})
    llm = AsyncMock()
    result = await compare_mention_with_candidate(m, c, "INVALID", llm_client=llm)
    assert result.decision == DECISION_UNSURE
    assert result.confidence == CONFIDENCE_LOW
    llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_compare_empty_mention_text_returns_unsure_low():
    """Empty mention.text or candidate.name -> UNSURE LOW."""
    m = _fake_mention("ACTOR", "")
    c = Candidate(canonical_id="c1", name="User", norm_name="user", aliases=[], score=0.9, payload={})
    llm = AsyncMock()
    result = await compare_mention_with_candidate(m, c, "ACTOR", llm_client=llm)
    assert result.decision == DECISION_UNSURE
    assert result.confidence == CONFIDENCE_LOW
    llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_compare_actor_user_vs_system_returns_different_high():
    """ACTOR: Пользователь vs Система -> DIFFERENT HIGH (short-circuit, no LLM) (plan test 1)."""
    m = _fake_mention("ACTOR", "Пользователь")
    c = Candidate(
        canonical_id="c1",
        name="Система",
        norm_name="система",
        aliases=[],
        score=0.9,
        payload={},
    )
    llm = AsyncMock()
    result = await compare_mention_with_candidate(m, c, "ACTOR", llm_client=llm)
    assert result.decision == DECISION_DIFFERENT
    assert result.confidence == CONFIDENCE_HIGH
    llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_compare_object_same_name_returns_same_high():
    """OBJECT: Документ vs Документ -> SAME HIGH (short-circuit) (plan test 3)."""
    m = _fake_mention("OBJECT", "Документ")
    c = Candidate(
        canonical_id="c1",
        name="Документ",
        norm_name="документ",
        aliases=[],
        score=0.9,
        payload={},
    )
    llm = AsyncMock()
    result = await compare_mention_with_candidate(m, c, "OBJECT", llm_client=llm)
    assert result.decision == DECISION_SAME
    assert result.confidence == CONFIDENCE_HIGH
    llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_compare_alias_match_returns_same_high_without_llm():
    """Система vs Платформа with alias Платформа -> SAME HIGH (short-circuit) (plan test 2)."""
    m = _fake_mention("ACTOR", "Платформа")
    c = Candidate(
        canonical_id="c1",
        name="Система",
        norm_name="система",
        aliases=["Платформа", "платформа"],
        score=0.9,
        payload={},
    )
    llm = AsyncMock()
    result = await compare_mention_with_candidate(m, c, "ACTOR", llm_client=llm)
    assert result.decision == DECISION_SAME
    assert result.confidence == CONFIDENCE_HIGH
    llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_compare_invalid_json_from_llm_returns_unsure_low():
    """Invalid JSON output -> UNSURE LOW (plan test 5)."""
    m = _fake_mention("ACTOR", "UnknownRole")
    c = Candidate(canonical_id="c1", name="Other", norm_name="other", aliases=[], score=0.9, payload={})
    llm = AsyncMock()
    resp = MagicMock()
    resp.text = "not valid json at all"
    llm.chat.return_value = resp
    result = await compare_mention_with_candidate(m, c, "ACTOR", llm_client=llm)
    assert result.decision == DECISION_UNSURE
    assert result.confidence == CONFIDENCE_LOW
    llm.chat.assert_called_once()


@pytest.mark.asyncio
async def test_compare_llm_same_but_guardrail_downgrades_to_unsure():
    """LLM returns SAME with 'exact match' but strings differ -> guardrail downgrades to UNSURE LOW."""
    m = _fake_mention("ACTOR", "User")
    c = Candidate(canonical_id="c1", name="Admin", norm_name="admin", aliases=[], score=0.9, payload={})
    llm = AsyncMock()
    resp = MagicMock()
    resp.text = json.dumps({"decision": "SAME", "confidence": "HIGH", "reason": "Exact match."})
    llm.chat.return_value = resp
    result = await compare_mention_with_candidate(m, c, "ACTOR", llm_client=llm)
    assert result.decision == DECISION_UNSURE
    assert result.confidence == CONFIDENCE_LOW
