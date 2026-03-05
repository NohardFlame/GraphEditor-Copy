# plan.md — Big‑LLM Extraction + Local Tournament Canonicalization (SQL authoritative, Qdrant retrieval-only)

This plan is a handoff document for a coding agent to implement the revised strategy:

- **Keep**: business model (Actor/Object/State/AtomicAction/BusinessOperation), existing infrastructure modules (SQL, Qdrant, Docling, LLM client).
- **Change**: the pipeline. A **powerful cloud LLM** extracts *mentions + relations* from the whole document (prepared with Docling chunk markers). A **small local LLM** only performs cheap pairwise decisions: *SAME / DIFFERENT / UNSURE* during canonicalization.

The design preserves the core principle from the existing spec: **SQL is authoritative (ledger + provenance + audit), Qdrant is retrieval-only (embeddings + compact payload pointing to SQL)**.

---

## 0) Goals and non-goals

### Goals
1) **Stable extraction**: avoid chunk-local context loss by giving the big LLM the entire document (with chunk boundaries).
2) **Traceability**: every extracted item must link back to **verbatim evidence** in a specific chunk (and ideally offsets).
3) **Cheap canonicalization**: use Qdrant to propose top candidates; use small LLM to answer a narrow question.
4) **Idempotency**: reruns with same inputs/prompt versions do not create duplicates.

### Non-goals (for MVP)
- Full Stage‑3 “domain snapshot” (AtomicActions + BusinessOperations) generation. We keep the *business model* constraints, but we only build canonical registries + mention mappings.
- Full cross-document reference resolution (e.g., “see §3.2”). We only **extract** reference mentions; resolution is a later step.

---

## 1) Core principles (must not be violated)

1) **SQL is source of truth.** Qdrant never stores the only copy of important data.
2) **Evidence-substring discipline.** Any extracted mention must include a snippet that is a verbatim substring of the originating chunk text.
3) **Deterministic normalization first; semantics second.** Always try deterministic keys before LLM comparisons.
4) **Explicit vs inferred separated.** The extractor may infer states, but must tag them; inferred states never automatically become new enum states.
5) **Single-target action invariant (business model).** AtomicAction targets exactly one Object (used later; keep the structure compatible).

---

## 2) Business model (conceptual)

### 2.1 Actor
Business participant capable of performing actions (User/Admin/System/ExternalService).

### 2.2 Object
Business entity that has a finite set of states (Document/Project/Order/Session/etc.).

### 2.3 State
A label representing an Object’s lifecycle position. Ultimately a **finite enum per Object** (not global free-text).

### 2.4 AtomicAction (single-target)
Strict business action with:
- exactly one `target_object`
- optional state gating: `allowed_states`
- effect: `transition_to` OR `no_change`

### 2.5 BusinessOperation (macro)
A higher-level workflow composed of AtomicActions (future work).

---

## 3) End-to-end pipeline overview

### Step A — Ingest and chunk
1) Ingest external file → create `documents` row.
2) Docling parse → produce ordered `chunks` (with section path/heading info).
3) Store each chunk in SQL with `chunk_index` (neighbor lookup by index).
4) (Optional) Upsert each chunk embedding into Qdrant `raw_chunks` for fast context retrieval.

### Step B — Prepare “chunk-marked” document for big LLM
1) Build a **single text artifact** (<=2MB) containing:
   - document header metadata
   - all chunks in order
   - explicit chunk boundary markers (stable `chunk_id`)
2) If the assembled artifact exceeds size limit:
   - split into **parts** by chunk ranges (e.g., 1–30, 31–60) with a small overlap (1–2 chunks)
   - keep consistent `chunk_id`s across parts (no re-numbering)

### Step C — Big LLM preprocessing run (whole document)
1) Send the chunk-marked document (or parts) to a powerful model.
2) Ask it to output **strict JSON**: frames → mentions → relations (+ optional refs).
3) Validate output; store raw response for audit.

### Step D — Store extracted frames/mentions/relations in SQL
1) Create an `extraction_run` record with `run_kind = BIG_LLM_EXTRACT`.
2) Insert `frames`, `mentions`, `relations`, and `evidence` rows.
3) Mark extraction run `SUCCEEDED` only after evidence-substring validation passes.

### Step E — Canonicalization runs (local tournament)
Canonicalize in this order (important for stability):
1) Actors
2) Objects
3) States **within each canonical object**
4) Actions (after actor/object canonical IDs are known)

Each canonicalization step:
- Deterministic match (dedupe_key) → immediate link
- Else: Qdrant top‑K candidates (unique by key) → local LLM tournament on top 1..3
- If all DIFFERENT/UNSURE → create new canonical entity

---

## 4) Big LLM preprocessing input format

### 4.1 Chunk-marked document template
Use a format that is:
- easy for LLM to parse
- stable for downstream parsing

Recommended:

```
# DOC_META
doc_id: <UUID>
title: <optional>
source: <path/url optional>

# CHUNKS (ordered)
<<<CHUNK id="<chunk_id>" index=<chunk_index> section_path="<a/b/c>">>>
<chunk text verbatim>
<<<END_CHUNK>>>

<<<CHUNK id="..." index=... section_path="...">>>
...
<<<END_CHUNK>>>
```

Notes:
- `chunk_id` must match SQL chunk primary key (or a stable surrogate).
- `section_path` helps the model interpret bullets/prose scopes.

### 4.2 Future-proof reference markup (optional now)
In the prompt, tell the LLM to detect references like “see §3.2” and output them as `references[]` objects with:
- `ref_text_raw` (verbatim)
- `ref_norm` (e.g., `"3.2"`, `"Appendix A"`)
- `chunk_id` and evidence snippet

No resolution required in MVP.

---

## 5) Big LLM output schema (strict JSON)

### 5.1 Top-level
```json
{
  "doc_id": "UUID",
  "prompt_version": "big_extract_v1",
  "frames": [ ... ],
  "references": [ ... ],
  "errors": []
}
```

### 5.2 Frame object
A **frame** is the smallest extraction unit (sentence/bullet). Frames exist mainly to keep “things that belong together”.

```json
{
  "frame_id": "frame::<chunk_id>::<n>",
  "chunk_id": "UUID",
  "frame_index": 12,
  "frame_text": "verbatim excerpt (optional, may equal sentence/bullet)",
  "mentions": [ ... ],
  "relations": [ ... ]
}
```

### 5.3 Mention object (typed)
Mention types: `ACTOR | OBJECT | ACTION | STATE`.

```json
{
  "mention_id": "m::<frame_id>::<n>",
  "type": "OBJECT",
  "fields": {
    "name": "Document",
    "actor_name": "User",
    "verb": "create",
    "object_name": "Document",
    "state": "CREATED",
    "place": "personal console"
  },
  "epistemic": "EXPLICIT",
  "evidence": {
    "snippet": "verbatim substring from the chunk",
    "char_start": 120,
    "char_end": 168
  },
  "confidence": "HIGH"
}
```

Rules:
- `evidence.snippet` MUST be a verbatim substring of the chunk text.
- `epistemic` for states/actions can be `EXPLICIT | INFERRED`. Prefer EXPLICIT whenever possible.

### 5.4 Relations (explicit links inside a frame)
We avoid “implicit linking by co-location”. Store explicit relations:

```json
{
  "src_mention_id": "m::...",
  "rel_type": "ACTION_HAS_OBJECT",
  "dst_mention_id": "m::..."
}
```

Allowed relation types (MVP):
- `ACTION_HAS_ACTOR`
- `ACTION_HAS_OBJECT`
- `OBJECT_HAS_STATE`
- (optional later) `ACTION_HAS_PLACE`, `MENTION_REFERENCES_REF`

### 5.5 References (optional now)
```json
{
  "ref_id": "r::<chunk_id>::<n>",
  "chunk_id": "UUID",
  "ref_text_raw": "пункт 3.2",
  "ref_norm": "3.2",
  "evidence": {"snippet": "....", "char_start": 10, "char_end": 20}
}
```

---

## 6) SQL storage model (authoritative ledger)

Use your existing `documents`, `chunks`, `llm_calls`, and `runs` patterns. Add/extend the following tables (names may differ in code; preserve roles and constraints).

### 6.1 Runs
`extraction_runs`
- `run_id` (UUID)
- `run_kind` enum:
  - `DOCLING_CHUNK`
  - `BIG_LLM_EXTRACT`
  - `CANONICALIZE_ACTORS`
  - `CANONICALIZE_OBJECTS`
  - `CANONICALIZE_OBJECT_STATES`
  - `CANONICALIZE_ACTIONS`
- `doc_id`
- `prompt_version`
- `model_id`
- `status` (`RUNNING|SUCCEEDED|FAILED`)
- `input_hash` (for idempotent caching)
- `stats_json`, timestamps

`llm_calls`
- store request/response, token usage, model, latency; link to `run_id`

### 6.2 Frames / mentions / relations
`frames`
- `frame_id` (PK; stable string or UUID)
- `doc_id`, `chunk_id`, `frame_index`
- `frame_text` (optional), `section_path` (optional)
- unique constraint `(chunk_id, frame_index)` or `(frame_id)`

`mentions`
- `mention_id` (PK)
- `frame_id` (FK), `doc_id`, `chunk_id`
- `type` enum
- `fields_json`
- `epistemic` enum
- `confidence`
- `dedupe_key` (computed; indexed)
- `created_run_id`

`mention_evidence`
- `mention_id` (FK)
- `snippet` (TEXT)
- `char_start`, `char_end` (nullable)
- evidence validation status

`relations`
- `relation_id` (PK)
- `frame_id` (FK)
- `src_mention_id` (FK)
- `rel_type`
- `dst_mention_id` (FK)
- unique constraint `(src_mention_id, rel_type, dst_mention_id)`

### 6.3 Canonical registry (entity resolution)
`canonicals`
- `canonical_id` (UUID PK)
- `canonical_type` enum: `ACTOR|OBJECT|ACTION|STATE`
- `name` (display)
- `norm_name` (normalized; indexed)
- `superseded_by` (nullable FK to canonicals)
- `created_run_id`

`canonical_aliases`
- `alias_id`
- `canonical_id`
- `alias_text`
- `alias_norm`
- unique constraint `(canonical_id, alias_norm)` and/or global `(canonical_type, alias_norm)` depending on policy

`mention_to_canonical`
- `mention_id`
- `canonical_id`
- `decision` (`ACCEPT|REJECT|PENDING`)
- `decided_by` (`RULE|LLM|HUMAN`)
- `run_id`
- unique `(mention_id)` (one final mapping)

`canonical_merges`
- `from_canonical_id`
- `to_canonical_id`
- `run_id`
- `reason`
- timestamps

### 6.4 Object-scoped states (recommended)
Instead of global `STATE` canonicals, prefer states *scoped to object*:
- Either encode canonical_state as `(canonical_object_id, state_norm)` in `canonicals`
- Or store `object_states`:
  - `canonical_object_id`
  - `state_id` (UUID)
  - `state_name`
  - `state_norm`
  - unique `(canonical_object_id, state_norm)`

This prevents accidental “ARCHIVED means the same everywhere” merges.

---

## 7) Qdrant storage schema

### 7.1 Collection: `raw_chunks` (optional)
Purpose: quick semantic retrieval of context chunks.

- **Point**: 1 point per chunk
- `point_id = chunk_id`
- vector: embedding(chunk_text + section_path)
- payload:
  - `doc_id`, `chunk_id`, `chunk_index`, `section_path`
  - optional: `text_preview` (first N chars)

Indexes:
- `doc_id`, `chunk_index`

### 7.2 Collections: canonical registries (required)
Purpose: candidate retrieval for tournament dedupe.

Create 3–4 collections (recommended):
- `canonical_actors`
- `canonical_objects`
- `canonical_actions`
- `canonical_object_states` (or `canonical_states` if you go global)

Common settings:
- distance: Cosine
- vector size validated at startup (matches embedding model)
- `point_id = canonical_id` (stable idempotent upsert)

Common payload fields:
- `canonical_id`
- `canonical_type`
- `name`
- `norm_name`
- `aliases` (optional short list)
- `created_at`
- `superseded_by` (optional)
- (for actions) `actor_id`, `object_id`, `verb_norm`

Payload indexes:
- `canonical_type` (if single collection)
- `norm_name`
- (actions) `actor_id`, `object_id`, `verb_norm`
- (object_states) `canonical_object_id`, `state_norm`

### 7.3 Embedding text templates (canonical points)
Keep templates stable and short:

- Actor:
  - `embedding_text = "ACTOR: <name>. Aliases: <a1,a2>. Context: <optional representative snippet>"`
- Object:
  - `embedding_text = "OBJECT: <name>. Aliases: <...>. Context: <...>"`
- Action:
  - `embedding_text = "ACTION: <actor_name> <verb_norm> <object_name>. Synonyms: <...>"`
- Object-State:
  - `embedding_text = "STATE of <object_name>: <state_name>."`

> Important: canonical points should embed **identity**, not entire documents.

### 7.4 Do we store mention-cards in Qdrant?
MVP: **No.** You can canonicalize from SQL mentions and only use Qdrant for canonical candidate retrieval.
Add mention-cards later only if you need “find similar mentions” analytics/debug.

---

## 8) Canonicalization algorithm (local tournament)

### 8.1 Normalization and dedupe keys
Define `norm(s)`:
- lowercase
- strip
- collapse whitespace
- optional: remove punctuation except meaningful characters

Define `dedupe_key`:
- Actor: `actor::<norm(name)>`
- Object: `object::<norm(name)>`
- Action (pre-canonical): `action::<norm(actor_name)>::<norm(verb)>::<norm(object_name)>`
- Object-state: `state::<canonical_object_id>::<norm(state_name)>`

### 8.2 Candidate retrieval
For each mention:
1) Deterministic lookup:
   - check aliases / norm_name match in SQL (fast index)
2) Else Qdrant search:
   - embed mention “card text”
   - search corresponding canonical collection top‑K (K=10)
   - collapse to unique candidates (by canonical_id and/or norm_name)
   - take best 1..3 for tournament

### 8.3 Local LLM comparator contract
The local LLM receives *only* the new mention and one candidate at a time.

**Input JSON**
```json
{
  "mention": {"type":"OBJECT","text":"Invoice","evidence":"..."},
  "candidate": {"canonical_id":"...","name":"Billing invoice","aliases":["invoice","bill"]},
  "task":"Are these the same real-world business entity?"
}
```

**Output JSON**
```json
{"decision":"SAME","confidence":"HIGH","reason":"Synonyms; same concept."}
```

Allowed decisions: `SAME | DIFFERENT | UNSURE`.

Tournament rule:
- If SAME → link mention→candidate canonical
- Else try next candidate (up to 3)
- If all DIFFERENT/UNSURE → create new canonical, link mention

### 8.4 Canonicalizing actions (after actor/object)
For each ACTION mention:
1) resolve its actor/object mention → canonical IDs
2) compute stable action signature:
   - `action_sig = action::<canonical_actor_id>::<verb_norm>::<canonical_object_id>`
3) deterministic match on `action_sig` → link
4) else Qdrant candidate retrieval filtered by `(actor_id, object_id)` if indexed
5) local LLM decides verb equivalence/synonymy only

### 8.5 States within objects
After canonical object is known:
- canonicalize state within that object’s state registry only
- never compare states across unrelated objects in MVP

---

## 9) Implementation plan (tasks for coding agent)

### Phase 1 — Data model + migrations
1) Add SQL tables: `frames`, `mentions`, `mention_evidence`, `relations`,
   `canonicals`, `canonical_aliases`, `mention_to_canonical`, `canonical_merges`,
   and object-scoped state storage.
2) Add indexes + unique constraints for idempotency.

Deliverable: migrations + ORM models + repository layer methods.

### Phase 2 — Docling chunking + chunk-marked file builder
1) Implement `docling_chunk(document) -> [Chunk]` (likely exists).
2) Implement `build_marked_doc(doc_id, chunks) -> str` with the marker format.
3) Implement `size_guard(marked_doc)`: if >2MB, split into parts with overlap.

Deliverable: unit tests verifying:
- markers parse back to chunk_id mapping
- size splitting is stable and does not re-number chunk IDs

### Phase 3 — Big LLM extraction run
1) Define prompt `big_extract_v1`:
   - strict JSON output requirement
   - evidence-substring rule
   - explicit relations requirement
2) Implement call via `llm_client` (cloud).
3) Implement parser + validator:
   - JSON parse
   - schema validation
   - evidence snippet validation (substring of SQL chunk text)
4) Persist to SQL (run + llm_calls + frames/mentions/relations).

Deliverable: integration test on one sample doc.

### Phase 4 — Canonical registries + Qdrant canonical collections
1) Create/ensure Qdrant collections for canonicals (distance/cosine, vector size).
2) Implement canonical upsert:
   - SQL create canonical entity
   - Qdrant upsert point with embedding_text + payload
3) Implement retrieval:
   - embed mention card text
   - query Qdrant top-K
   - return best 3 unique candidates

Deliverable: smoke tests for insert + search.

### Phase 5 — Local tournament canonicalization
1) Implement canonicalization runners per type:
   - `canonicalize_actors(doc_id, run_id)`
   - `canonicalize_objects(...)`
   - `canonicalize_object_states(...)`
   - `canonicalize_actions(...)`
2) Implement deterministic match first (SQL alias lookup).
3) Implement local LLM comparator contract and tournament loop.
4) Persist mapping decisions in `mention_to_canonical` with audit fields.
5) Support canonical merges (manual/admin later; data model now).

Deliverable: end-to-end pipeline run producing:
- extracted mentions
- canonical entities in SQL + Qdrant
- mention→canonical links

### Phase 6 — Observability and replayability
1) Store all prompts/versions (`prompt_version` fields).
2) Store full LLM request/response in `llm_calls`.
3) Add run stats: counts per mention type, % linked by deterministic key, % by LLM.

Deliverable: CLI/debug endpoints to inspect:
- a chunk → frames → mentions → canonical mapping
- canonical entity → all mentions that mapped to it

---

## 10) Success criteria (MVP)

1) For a test document, pipeline produces:
   - chunks in SQL
   - one BIG_LLM_EXTRACT run with frames/mentions/relations
   - canonical registries with non-trivial dedupe (aliases merged)
2) Re-running with same inputs:
   - does not create duplicates (idempotent keys + unique constraints)
3) Debuggability:
   - any canonical entity traces back to mention evidence and chunk text.

---

## 11) Practical defaults

- Chunk size: keep Docling defaults; prefer “semantic chunks” over fixed tokens.
- Qdrant K: 10 for retrieval, then tournament on top 3.
- Local LLM temperature: 0–0.2, strict JSON.
- Evidence snippet length: <=300 chars.
- If big extraction output is partially invalid: store run as FAILED but persist raw LLM output for debugging.

---

**End of plan.**
