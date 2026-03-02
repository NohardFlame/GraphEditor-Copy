# Stage 3 (Run‑3) — Canonical-to-Single-Model Assembly Plan (Middle JSON Approach)

> Purpose of this document: **formalize the ideas and motivations** behind the Stage‑3 pipeline so a coding agent can implement it by **reusing the existing infrastructure** (SQL ledger + Qdrant + LLM client + run tracking), without being constrained by overly rigid implementation details.

---

## 1) Stage‑3 goal (what we are building)

Stage 3 takes the **canonical, de‑duplicated claims** produced by Stage 2 and turns them into a **single, usable “document model”** that is:

- **Readable**: human-friendly summaries of actors/objects/states/actions.
- **Queryable**: structured enough to support retrieval, linking, and later graph/operation building.
- **Auditable**: every summary is grounded in evidence from the document (via the Stage‑2 evidence pools).
- **Robust to imperfect LLM output**: the pipeline must keep moving even if the model returns partial/messy JSON.

In practical terms, Stage‑3’s deliverables are:
- **Actor cards** (summary + possible actions)
- **Object cards** (summary + possible states)
- **State cards** (summary + candidate object links)
- **Action cards** (summary + candidate actors/objects + state effects/preconditions)

And importantly:
- **Objects “bake in” resolved states over time** as we process states (so action resolution can rely on the object’s state pool).

---

## 2) Design principles (why we chose this approach)

### 2.1 Middle approach: JSON output, but minimal and forgiving
We ask the LLM to output JSON because it is convenient for downstream processing, **but** we intentionally avoid “heavy schema rules” that often produce empty outputs or brittle failures.

**Core idea:**
- **The JSON is small.**
- Only a few fields are asked.
- **The code normalizes** identity fields (IDs, names) using Stage‑2 canonicals.
- If JSON parsing fails, we still store the raw response and produce a safe fallback.

### 2.2 Evidence is the foundation (Stage‑2 contract)
Stage 2 already created canonicals with evidence grouped by chunk IDs (`{chunk_id → [snippets...]}`). Stage 3 does not invent new evidence; it **reuses** that evidence to generate “cards”.

### 2.3 Predictable context size via snippet windows
Chunks vary in size; pushing entire chunks into prompts is unstable.
So Stage 3 uses a deterministic “context window” approach:
- take each Stage‑2 evidence snippet,
- re-open the chunk text,
- extract **~200 characters before and after the snippet** (clamped at boundaries),
- use those windows in prompts.

This gives:
- stable token budgets,
- consistent prompts,
- less risk of “lost in long context”.

### 2.4 Dependency-respecting pass order
We process claim types in this order:
1) ACTOR
2) OBJECT
3) STATE (attached to objects)
4) ACTION (grounded on actors/objects/states)

Motivation:
- actions depend on stable actor/object anchors,
- states become meaningful when attached to objects,
- later workflow/operations need stable endpoints.

### 2.5 Separation of “canonical memory” vs “assembly surface”
- Stage‑2 Qdrant: `stage2/canonical` is the stable canonical memory.
- Stage‑3 Qdrant: a separate “cards surface” used for retrieval during assembly (summaries, state pools, etc.).

Motivation: keep Stage‑2 dedupe semantics clean while Stage‑3 evolves richer representations.

---

## 3) Inputs and outputs

### 3.1 Inputs (what Stage‑3 consumes)
- **Stage‑2 canonical claims** (ACTOR/OBJECT/STATE/ACTION)
- For each canonical claim: **evidence pool** `{chunk_id → [snippets...]}`
- Access to **chunk text** (Docling output or stored chunk content) so we can extract snippet windows.
- Stage‑2 canonical retrieval surface in Qdrant (`stage2/canonical`) for “candidate discovery” where needed.

### 3.2 Outputs (what Stage‑3 produces)
- A Stage‑3 “resolved cards” dataset in SQL (one per canonical claim, per kind).
- A Stage‑3 embedding surface in Qdrant for:
  - retrieving candidate objects/actors when resolving states/actions,
  - semantic navigation of the final model.

---

## 4) Stage‑3 artifacts (“cards”) and minimal JSON contracts

### 4.1 Actor card (minimal)
We do **not** ask the LLM to decide the actor name/ID (Stage‑2 already did canonicalization).
We only want a stable, multi-sentence description.

**LLM JSON shape (minimal):**
- `summary` (3–10 sentences, required)
- `possible_actions` (optional list)
- `notes` (optional)

### 4.2 Object card (minimal + possible states)
**LLM JSON shape (minimal):**
- `summary` (3–10 sentences, required)
- `possible_states` (optional list of human-readable states)
- `notes` (optional)

### 4.3 State card (minimal + candidate objects)
**LLM JSON shape (minimal):**
- `summary` (required)
- `candidate_objects` (optional list of object IDs from the provided candidates)
- `notes` (optional)

### 4.4 Action card (minimal + grounding candidates)
**LLM JSON shape (minimal):**
- `summary` (required)
- `candidate_actors` (optional IDs from provided candidates)
- `candidate_objects` (optional IDs from provided candidates)
- `preconditions` / `effects` (optional, human-readable or state references if the prompt provides them)
- `notes` (optional)

**Motivation for minimalism:** the card’s *summary* is the core value; everything else is helpful but not required.

---

## 5) Evidence packaging: “snippet windows”

### 5.1 Window extraction rule
For each evidence snippet `(chunk_id, snippet_text)`:
- open the full `chunk_text`,
- locate `snippet_text` in it (exact match preferred; a small normalization fallback is acceptable),
- extract `window_text = chunk_text[pos-200 : pos+len(snippet)+200]` with boundary clamping.

Store enough metadata to audit:
- chunk_id
- snippet text
- start/end offsets (optional)
- window hash (optional)

### 5.2 Selecting what to include (to avoid overwhelming context)
Per claim, include up to **K=10** snippet windows (configurable).
Selection is deterministic:
- prefer chunks that contribute more snippets,
- then earlier chunk order,
- then diversify if needed.

---

## 6) Pipeline flow (Run‑3 execution)

Stage‑3 is executed as a **single run for a single document** (consistent with earlier run semantics), producing a “resolved model surface”.

### Pass A — Resolve ACTOR cards
For each canonical ACTOR:
1) gather top evidence windows,
2) ask LLM for minimal JSON actor card,
3) store:
   - raw response
   - parsed JSON (if parseable)
   - normalized “resolved actor card JSON” (with canonical id/name filled by code),
4) embed the card text into Stage‑3 Qdrant surface.

### Pass B — Resolve OBJECT cards
For each canonical OBJECT:
1) gather top evidence windows,
2) ask LLM for minimal JSON object card,
3) store resolved object JSON,
4) embed into Stage‑3 Qdrant.

**Additional Stage‑3 behavior:** initialize `object.states=[]` (baked state pool) to be filled in Pass C.

### Pass C — Resolve STATE cards and attach to objects
For each canonical STATE:
1) gather top evidence windows,
2) retrieve candidate objects from Stage‑3 Qdrant (objects only),
3) provide the candidate object cards (summaries) to the LLM,
4) ask LLM for:
   - a state summary,
   - optional candidate object IDs (ranked or listed),
5) store the state card,
6) **append the state (short summary) into the baked state pool** of the selected object(s).

Motivation: actions later can be resolved with object summaries + their baked state pools, without re-reading the whole document.

### Pass D — Resolve ACTION cards (grounded using Stage‑3 cards)
For each canonical ACTION:
1) gather action evidence windows,
2) retrieve candidate actors + objects from Stage‑3 Qdrant,
3) for each candidate object, include:
   - object summary,
   - baked state pool (short state summaries),
4) include candidate actor summaries,
5) ask LLM for action minimal JSON:
   - a plain-language summary of what happens,
   - which actors/objects are likely involved (from candidates),
   - optional preconditions/effects (prefer referencing the state pool when possible),
6) store action card,
7) optionally record “candidate links” in SQL for later graph-building (soft links, not hard constraints).

---

## 7) Robustness strategy (avoid “empty JSON” failure modes)

### 7.1 Always store raw outputs
No matter what happens, persist:
- prompt context metadata,
- raw LLM response text,
- parse status.

Motivation: makes debugging and iterative prompt improvement practical.

### 7.2 Best-effort JSON parsing with repair
Parsing strategy:
1) attempt direct JSON parse,
2) if it fails, run a single “repair” attempt with a truncated head+tail of the raw output,
3) if still fails, create a fallback resolved JSON such as:
   - `{"summary": "<cleaned first N chars>"}`

### 7.3 Minimal validation
We validate only what we truly need:
- `summary` exists and is “long enough” (soft threshold),
- everything else is optional.

If summary is too short:
- mark status as “SUCCESS_WITH_WARNINGS”,
- keep the card (do not fail the run).

Motivation: keep the pipeline moving; correctness is improved iteratively.

---

## 8) Storage and retrieval surfaces (high level)

### 8.1 SQL: resolved cards + baked object model
Stage‑3 should add a small “resolved cards” ledger that is separate from Stage‑2 canonicals.

Suggested conceptual storage (not prescriptive):
- `stage3_resolved_cards`: one row per (doc_id, canonical_claim_id, kind)
  - resolved_json
  - raw_response
  - parse_status
  - versions (prompt_version/model_id/extractor_version)
  - evidence refs (chunk_id + snippet + window metadata)
- `stage3_objects_baked`: object JSON with embedded `states[]` updated during Pass C.

### 8.2 Qdrant: Stage‑3 cards surface
Maintain a Stage‑3 collection for card embeddings.
Card embeddings support:
- finding candidate objects for a state,
- finding candidate actors/objects for an action,
- browsing the assembled model.

Payload should include:
- doc_id
- canonical_claim_id
- kind (ACTOR/OBJECT/STATE/ACTION)
- versions (prompt_version, model_id, embedding_model_id)

Motivation: retrieval stays reliable across runs while allowing version filtering.

---

## 9) Reuse of existing infrastructure (guidance for coding agents)

### 9.1 Reuse run tracking + observability patterns
Stage‑3 should follow the established run discipline:
- one run per document,
- clear run status transitions,
- per-run stats and error summaries,
- linking outputs to run_id for audit.

### 9.2 Reuse LLM call logging
Use the existing LLM client and audit storage (or extend it) so Stage‑3 calls are traceable:
- request context metadata
- response text
- tokens/latency where available
- error fields

### 9.3 Reuse the “stable collection name + versioned payload” approach for Qdrant
Keep collection names stable; put version identifiers into payload to support filtering and future migrations.

### 9.4 Reuse Stage‑2 evidence pools and chunk store
Stage‑3 should not invent new evidence.
Its evidence packaging is a deterministic transformation of:
- Stage‑2 snippet references
- chunk text already stored by your pipeline

---

## 10) Incremental rollout plan (practical order)

1) **Implement Pass A+B (Actor/Object cards)**
   - easiest prompts, no cross-linking required,
   - immediate value: a stable Stage‑3 retrieval surface.

2) **Add Pass C (State cards + baked object states)**
   - introduces first cross-type retrieval,
   - creates the state pool needed for action grounding.

3) **Add Pass D (Action cards grounded on Stage‑3 cards)**
   - biggest value for “data flow reconstruction”.

4) (Later) Extend into “atomic actions / business operations / workflow graph”
   - Stage‑2 explicitly leaves this to Stage‑3,
   - but it is a separate layer once cards + grounding exist.

---

## 11) Success criteria (how we know Stage‑3 MVP works)

- For a document, Stage‑3 produces:
  - non-empty actor/object/state/action cards for most canonicals,
  - stored in SQL and embedded in Qdrant.
- Action prompts can be answered using:
  - action evidence windows,
  - actor/object summaries,
  - baked state pools,
  without requiring full-document context.
- Failures are **auditable**:
  - raw LLM outputs are stored,
  - parse failures do not crash the pipeline,
  - reruns can reuse caches and compare outcomes by version.

---

## 12) Notes on future evolution (without locking implementation now)

This plan intentionally stays high-level. The implementation agent can adapt details (tables, modules, naming), but should preserve the core decisions:

- minimal JSON, summary-first,
- deterministic snippet windows,
- pass order (actor → object → state → action),
- baked object state pool,
- separate Stage‑3 retrieval surface,
- robust “store raw + best-effort parse” discipline.

