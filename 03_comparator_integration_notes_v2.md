# Comparator Integration Notes v2

Checklist for wiring the comparator into the canonicalization runner.

---

## 1) Comparator is ONLY for canonicalization
Extraction is done by the big LLM. Comparator is used only to decide:
- merge mention into canonical (SAME), or
- keep separate / create new canonical (DIFFERENT/UNSURE in tournament context).

---

## 2) Runtime settings
- temperature: 0.0–0.2
- max tokens: 128–256
- enforce JSON output mode if available

---

## 3) Candidate retrieval
- Query the appropriate canonical Qdrant collection (actors/objects/actions/object_states).
- top-K=10, then unique by canonical_id or norm_name.
- tournament on top 1..3.

---

## 4) Deterministic short-circuit before LLM
Before calling the comparator:
- compute norm(mention.text)
- if it matches canonical norm_name or a known alias in SQL → merge without LLM

---

## 5) Apply guardrails after LLM
- strict JSON parse
- forbidden-claim checks (exact/alias)
- optional class-contradiction override

Store:
- raw model output (audit)
- final guarded decision (used by system)
- override reason if applied

---

## 6) Unit tests (minimum)
1) ACTOR: "Пользователь" vs "Система" -> DIFFERENT HIGH
2) ACTOR: "Система" vs "Платформа" with alias -> SAME HIGH if alias exists
3) OBJECT: "Документ" vs "Документ" -> SAME HIGH
4) STATE: canonical_object_id differs -> DIFFERENT HIGH
5) invalid JSON output -> UNSURE LOW
6) model claims exact match when strings differ -> downgraded to UNSURE LOW
