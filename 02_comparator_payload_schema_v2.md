# Comparator Payload Schema v2 (coding instructions)

This document defines the **input payload schema** that the comparator LLM must receive, and the **validation/guardrails** the orchestrator must apply.

The main bug observed previously was:
`task` was sent as a sentence (“Are these the same…”) instead of an enum, causing the model to ignore task rules and hallucinate matches.

---

## 1) Required input JSON schema (v2)

### Top-level fields (required)
- `task_kind` (string): `"ACTOR"` | `"OBJECT"` | `"ACTION"` | `"STATE"`
- `mention` (object)
- `candidate` (object)

### mention object
- `text` (string, required)
- `evidence` (string, optional)
- `extra` (object, optional) — structured fields (IDs, verb, place, epistemic, etc.)

### candidate object
- `canonical_id` (string/uuid, optional but recommended)
- `name` (string, required)
- `aliases` (array of strings, optional; default empty)
- `extra` (object, optional)

### Allowed extra top-level fields
You may include fields like `question`, `notes`, `trace_id`, but they must not replace `task_kind`.

---

## 2) Example payloads

### 2.1 ACTOR example
```json
{
  "task_kind": "ACTOR",
  "mention": {
    "text": "Пользователь",
    "evidence": "Пользователь создаёт документ",
    "extra": {"name": "Пользователь"}
  },
  "candidate": {
    "canonical_id": "a4daf542-993b-4b1c-bb4c-d0a5e4b25b93",
    "name": "Система",
    "aliases": []
  }
}
```
Expected: `DIFFERENT` (human role/group vs software system).

### 2.2 OBJECT example
```json
{
  "task_kind": "OBJECT",
  "mention": {"text": "Документ", "evidence": "создаёт документ"},
  "candidate": {"canonical_id": "…", "name": "Документ", "aliases": ["док", "doc"]}
}
```

### 2.3 ACTION example (post actor/object canonicalization)
```json
{
  "task_kind": "ACTION",
  "mention": {
    "text": "создать документ",
    "evidence": "Пользователь создаёт документ",
    "extra": {
      "verb": "создать",
      "actor_canonical_id": "actor-uuid-1",
      "object_canonical_id": "object-uuid-9"
    }
  },
  "candidate": {
    "canonical_id": "action-uuid-7",
    "name": "создать документ",
    "aliases": ["создание документа"],
    "extra": {
      "verb_norm": "создать",
      "actor_canonical_id": "actor-uuid-1",
      "object_canonical_id": "object-uuid-9"
    }
  }
}
```

### 2.4 STATE example (object-scoped)
```json
{
  "task_kind": "STATE",
  "mention": {"text": "создан", "evidence": "документ создан", "extra": {"canonical_object_id": "obj-1"}},
  "candidate": {"canonical_id": "state-1", "name": "CREATED", "aliases": ["создан"], "extra": {"canonical_object_id": "obj-1"}}
}
```

---

## 3) Required output schema
Comparator output must be valid JSON with exact keys:
```json
{"decision":"SAME"|"DIFFERENT"|"UNSURE","confidence":"LOW"|"MEDIUM"|"HIGH","reason":"..."}
```

---

## 4) Orchestrator guardrails (required)

### 4.1 Hard input validation
- If `task_kind` not in allowed set → do not call LLM; return `UNSURE` (LOW) and log.
- If `mention.text` or `candidate.name` empty → return `UNSURE` (LOW).

### 4.2 Strict output parsing
- If output is not valid JSON → treat as `UNSURE` (LOW), log raw output.
- If required keys missing → treat as `UNSURE` (LOW).

### 4.3 Post-checks against hallucinated reasons (critical)
Implement `norm(s)` exactly as in prompt.

If decision is `SAME`:
1) If reason contains "exact" but norm(text) != norm(name) → downgrade to `UNSURE` (LOW).
2) If reason contains "alias" but aliases empty OR none matches norm(text) → downgrade to `UNSURE` (LOW).

### 4.4 Class-contradiction heuristic (recommended)
Keyword heuristics for RU:
- system keywords: ["система","сервис","платформа","приложение","бот","api","backend","сервер"]
- human group keywords: ["пользовател","менеджер","оператор","администратор","сотрудник","команда","группа"]
- org keywords: ["компания","отдел","департамент","организац","комитет"]

If mention and candidate are in a HARD contradiction (system vs human group):
- optionally skip LLM and return `DIFFERENT` (HIGH), OR
- if LLM says `SAME`, override to `DIFFERENT` and log.

### 4.5 Tournament policy
- Retrieve top-K from Qdrant (e.g., 10)
- Compare sequentially top 1..3
- Stop on `SAME`
- If all DIFFERENT/UNSURE → create new canonical

Treat `UNSURE` as “no merge” (continue to next candidate).
