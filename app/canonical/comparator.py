"""Local LLM comparator: SAME / DIFFERENT / UNSURE for mention vs canonical candidate (Phase 5)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from app.canonical.models import Candidate
from app.canonical.norm import norm

if TYPE_CHECKING:
    from app.db.models.extraction import Mention
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

PROMPT_VERSION_LOCAL_COMPARE = "local_compare_v2"

DECISION_SAME = "SAME"
DECISION_DIFFERENT = "DIFFERENT"
DECISION_UNSURE = "UNSURE"
CONFIDENCE_LOW = "LOW"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_HIGH = "HIGH"

# Request JSON object mode for broad provider compatibility; prompt enforces decision/confidence/reason.
COMPARATOR_RESPONSE_FORMAT = {"type": "json_object"}


@dataclass
class DecisionResult:
    """Result of comparing a mention with a canonical candidate."""

    decision: str  # SAME | DIFFERENT | UNSURE
    confidence: str  # LOW | MEDIUM | HIGH
    reason: str = ""


class ComparatorLLMClient(Protocol):
    """Protocol for LLM used by the comparator (e.g. LLMService.chat)."""

    async def chat(
        self,
        req: Any,
        *,
        run_repo: Any = None,
        workspace_id: str | None = None,
    ) -> Any:
        """Execute chat; req has .messages and .response_format; response has .text."""
        ...


def _build_mention_payload(mention: "Mention") -> dict[str, Any]:
    """Build JSON-serializable mention for the comparator request (v2: text, evidence, extra)."""
    try:
        fields = json.loads(mention.fields_json) if mention.fields_json else {}
    except Exception:
        fields = {}
    evidence = ""
    ev = getattr(mention, "evidence", None)
    if ev is not None and getattr(ev, "snippet_text", None):
        evidence = (ev.snippet_text or "")[:500]
    return {
        "text": _mention_display_text(mention, fields),
        "evidence": evidence,
        "extra": fields,
    }


def _mention_display_text(mention: "Mention", fields: dict) -> str:
    """Short display text for the mention."""
    t = (mention.type or "").upper()
    if t == "ACTOR":
        return fields.get("name") or fields.get("actor_name") or ""
    if t == "OBJECT":
        return fields.get("name") or fields.get("object_name") or ""
    if t == "ACTION":
        actor = fields.get("actor_name") or fields.get("actor") or ""
        verb = fields.get("verb") or ""
        obj = fields.get("object_name") or fields.get("object") or ""
        return f"{actor} {verb} {obj}".strip()
    if t == "STATE":
        return fields.get("state_name") or fields.get("state") or ""
    return json.dumps(fields)[:200]


def _build_candidate_payload(candidate: Candidate) -> dict[str, Any]:
    """Build JSON-serializable candidate for the comparator request (v2: canonical_id, name, aliases, extra)."""
    extra: dict[str, Any] = {}
    if candidate.payload:
        extra = dict(candidate.payload)
    return {
        "canonical_id": candidate.canonical_id,
        "name": candidate.name,
        "aliases": list(candidate.aliases or []),
        "extra": extra,
    }


TASK_KINDS = ("ACTOR", "OBJECT", "ACTION", "STATE")

# Class-contradiction keywords (RU) for optional override (02 payload schema §4.4).
_SYSTEM_KEYWORDS = frozenset(
    "система сервис платформа приложение бот api backend сервер по".split()
)
_HUMAN_GROUP_KEYWORDS = frozenset(
    "пользовател менеджер оператор администратор сотрудник команда группа".split()
)


def _class_contradiction_actor(mention_text: str, candidate_name: str) -> bool:
    """True if mention and candidate are in HARD contradiction (system vs human group)."""
    m = norm(mention_text)
    c = norm(candidate_name)
    if not m or not c:
        return False
    mention_system = any(k in m for k in _SYSTEM_KEYWORDS)
    mention_human = any(k in m for k in _HUMAN_GROUP_KEYWORDS)
    cand_system = any(k in c for k in _SYSTEM_KEYWORDS)
    cand_human = any(k in c for k in _HUMAN_GROUP_KEYWORDS)
    return (mention_system and cand_human) or (mention_human and cand_system)


def _apply_guardrails(
    result: DecisionResult,
    mention_text: str,
    candidate: Candidate,
) -> DecisionResult:
    """Downgrade SAME to UNSURE (LOW) if reason claims exact/alias match but claim is false."""
    if result.decision != DECISION_SAME:
        return result
    reason_lower = (result.reason or "").lower()
    n_mention = norm(mention_text)
    n_name = norm(candidate.name or "")
    aliases = list(candidate.aliases or [])

    if "exact" in reason_lower and n_mention != n_name:
        logger.warning(
            "Comparator guardrail: reason claims exact match but norm(mention)=%r != norm(name)=%r -> UNSURE",
            n_mention,
            n_name,
        )
        return DecisionResult(
            decision=DECISION_UNSURE,
            confidence=CONFIDENCE_LOW,
            reason="Guardrail: exact match claimed but strings differ after normalization.",
        )
    if "alias" in reason_lower:
        if not aliases:
            logger.warning(
                "Comparator guardrail: reason claims alias match but candidate has no aliases -> UNSURE"
            )
            return DecisionResult(
                decision=DECISION_UNSURE,
                confidence=CONFIDENCE_LOW,
                reason="Guardrail: alias match claimed but candidate has no aliases.",
            )
        if not any(norm(a) == n_mention for a in aliases):
            logger.warning(
                "Comparator guardrail: reason claims alias match but no alias matches norm(mention)=%r -> UNSURE",
                n_mention,
            )
            return DecisionResult(
                decision=DECISION_UNSURE,
                confidence=CONFIDENCE_LOW,
                reason="Guardrail: alias match claimed but no alias matches after normalization.",
            )
    return result


def _parse_response(text: str) -> DecisionResult | None:
    """Parse LLM response JSON into DecisionResult. Returns None if invalid."""
    text = (text or "").strip()
    if not text:
        return None
    # Strip markdown code block if present
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    decision = data.get("decision")
    if decision not in (DECISION_SAME, DECISION_DIFFERENT, DECISION_UNSURE):
        return None
    confidence = (data.get("confidence") or "").upper()
    if confidence not in (CONFIDENCE_LOW, CONFIDENCE_MEDIUM, CONFIDENCE_HIGH):
        confidence = CONFIDENCE_MEDIUM
    reason = str(data.get("reason") or "").strip()
    return DecisionResult(decision=decision, confidence=confidence, reason=reason)


async def compare_mention_with_candidate(
    mention: "Mention",
    candidate: Candidate,
    canonical_type: str,
    *,
    llm_client: ComparatorLLMClient,
    run_id: str | None = None,
    workspace_id: str | None = None,
    session: "Session | None" = None,
    model_id: str | None = None,
    timeout_s: float = 30.0,
    default_on_error: str = DECISION_UNSURE,
) -> DecisionResult:
    """Call local LLM to compare mention with one canonical candidate.

    Returns DecisionResult with decision (SAME | DIFFERENT | UNSURE) and confidence.
    Applies input validation, deterministic short-circuit (norm match), and post-LLM guardrails.
    On timeout, invalid JSON, or missing fields returns default_on_error (default UNSURE) and logs.
    When session and run_id are provided, writes a row to llm_calls for observability.
    """
    from app.db.models.claim import LlmCall
    from app.llm.types import LLMMessage, LLMRequest

    mention_payload = _build_mention_payload(mention)
    mention_text = (mention_payload.get("text") or "").strip()
    candidate_name = (candidate.name or "").strip()

    # Input validation (guardrails §4.1)
    task_upper = (canonical_type or "").upper()
    if task_upper not in TASK_KINDS:
        logger.warning("Comparator: invalid task_kind=%r -> UNSURE", canonical_type)
        return DecisionResult(
            decision=DECISION_UNSURE,
            confidence=CONFIDENCE_LOW,
            reason="Invalid or missing task_kind.",
        )
    if not mention_text or not candidate_name:
        return DecisionResult(
            decision=DECISION_UNSURE,
            confidence=CONFIDENCE_LOW,
            reason="Empty mention text or candidate name.",
        )

    # Deterministic short-circuit: norm match without LLM (§4 integration notes)
    n_mention = norm(mention_text)
    n_name = norm(candidate_name)
    if n_mention == n_name:
        return DecisionResult(
            decision=DECISION_SAME,
            confidence=CONFIDENCE_HIGH,
            reason="Exact normalized match of names.",
        )
    aliases = list(candidate.aliases or [])
    if any(norm(a) == n_mention for a in aliases):
        return DecisionResult(
            decision=DECISION_SAME,
            confidence=CONFIDENCE_HIGH,
            reason="Normalized alias match.",
        )

    # Class contradiction short-circuit (§4.4): skip LLM for ACTOR system vs human group
    if task_upper == "ACTOR" and _class_contradiction_actor(mention_text, candidate_name):
        logger.info(
            "Comparator short-circuit: ACTOR class contradiction (system vs human) -> DIFFERENT without LLM"
        )
        return DecisionResult(
            decision=DECISION_DIFFERENT,
            confidence=CONFIDENCE_HIGH,
            reason="Different entity classes: system vs human group.",
        )

    payload = {
        "task_kind": task_upper,
        "mention": mention_payload,
        "candidate": _build_candidate_payload(candidate),
    }
    user_content = json.dumps(payload, ensure_ascii=False, indent=2)
    from app.observability.prompt_registry import get_prompt_template
    system_content = get_prompt_template(PROMPT_VERSION_LOCAL_COMPARE)
    if not system_content:
        raise ValueError(
            f"Comparator system prompt not found for version {PROMPT_VERSION_LOCAL_COMPARE}. "
            "Register it in app.observability.prompt_registry."
        )
    messages = [
        LLMMessage(role="system", content=system_content),
        LLMMessage(role="user", content=user_content),
    ]
    metadata: dict[str, str] = {"stage": "comparator"}
    if run_id:
        metadata["extraction_run_id"] = run_id
    if workspace_id:
        metadata["workspace_id"] = workspace_id
    req = LLMRequest(
        messages=messages,
        response_format=COMPARATOR_RESPONSE_FORMAT,
        max_output_tokens=256,
        timeout_s=timeout_s,
        metadata=metadata,
    )
    try:
        resp = await llm_client.chat(
            req,
            workspace_id=workspace_id,
        )
        response_text = resp.text if hasattr(resp, "text") else str(resp)
        if session and run_id:
            request_json = json.dumps({
                "system_prompt_preview": system_content,
                "user_payload": payload,
            })
            llm_call = LlmCall(
                run_id=None,
                extraction_run_id=run_id,
                chunk_id=None,
                model=model_id,
                prompt_version=PROMPT_VERSION_LOCAL_COMPARE,
                request_json=request_json,
                response_text=response_text,
                status="SUCCESS",
            )
            session.add(llm_call)
            session.flush()
        result = _parse_response(response_text)
        if result is not None:
            result = _apply_guardrails(result, mention_text, candidate)
            # Class-contradiction override (§4.4): ACTOR system vs human group
            if (
                result.decision == DECISION_SAME
                and task_upper == "ACTOR"
                and _class_contradiction_actor(mention_text, candidate_name)
            ):
                logger.warning(
                    "Comparator class override: SAME -> DIFFERENT (system vs human group): mention=%r candidate=%r",
                    mention_text[:80],
                    candidate_name[:80],
                )
                result = DecisionResult(
                    decision=DECISION_DIFFERENT,
                    confidence=CONFIDENCE_HIGH,
                    reason="Class contradiction override: system vs human group.",
                )
            return result
        logger.warning("Comparator response invalid or missing fields: %s", (response_text[:200] if response_text else ""))
    except Exception as e:
        logger.warning("Comparator LLM call failed: %s", e, exc_info=True)
    return DecisionResult(
        decision=default_on_error,
        confidence=CONFIDENCE_LOW,
        reason="comparator error or invalid response",
    )
