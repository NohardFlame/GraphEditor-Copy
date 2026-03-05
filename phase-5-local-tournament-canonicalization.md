# Phase 5 — Local tournament canonicalization

### Purpose

Implement canonicalization runners for actors, objects, object states, and actions using deterministic keys, Qdrant candidate retrieval, and a small local LLM comparator tournament to decide SAME / DIFFERENT / UNSURE.

---

### Assumptions

- Phase 1 schema is in place (`canonicals`, `canonical_aliases`, `object_states`, `mention_to_canonical`, `canonical_merges`).
- Phase 3 big‑LLM extraction is populating `mentions` with `dedupe_key`, `fields_json`, and evidence.
- Phase 4 canonical registries and Qdrant collections are operational.
- A local LLM client exists (or will be wired) that can answer SAME / DIFFERENT / UNSURE questions with strict JSON output.

### Alternative inference path (big LLM)

Mentions may be produced by either the **API path** (Phase 3 `run_big_llm_extract` with an LLM client) or the **manual path** (prepare → user runs LLM → ingest). Canonicalization runners (`canonicalize_actors`, `canonicalize_objects`, etc.) consume mentions from `mentions` and `mention_to_canonical`; they do not depend on how the big-LLM step was executed. For audit and debugging (Phase 6): when the big-LLM step was run manually, `llm_calls` rows linked to that `extraction_run_id` will have `request_json` indicating `"source": "manual_run"` and `response_text` populated from the ingested result file(s). Tournament logic and deterministic matching are unchanged.

---

### 1. Normalization and dedupe keys implementation

Goal: implement consistent normalization and dedupe key functions that are reused across the pipeline.

1. Implement `norm(s: str) -> str` (shared utility):
   - Lowercase the string.
   - Strip leading/trailing whitespace.
   - Replace any internal runs of whitespace with a single space.
   - Optionally remove or normalize punctuation (e.g., remove trailing periods, commas, parentheses) except when semantically important.
   - Keep this implementation in a central module used in:
     - Phase 3 for `dedupe_key` computation.
     - Phase 4 for `norm_name` and alias normalization.
     - Phase 5 for any additional normalization needs.

2. Define dedupe key formats (per `plan.md` section 8.1):
   - Actor:
     - `dedupe_key = "actor::" + norm(actor_name)`
   - Object:
     - `dedupe_key = "object::" + norm(object_name)`
   - Pre‑canonical Action (before we know canonical actor/object IDs):
     - `dedupe_key = "action::" + norm(actor_name) + "::" + norm(verb) + "::" + norm(object_name)`
   - Object‑state (after canonical object is known; see below for action/state handling):
     - `dedupe_key = "state::" + canonical_object_id + "::" + norm(state_name)`

3. Ensure `dedupe_key` is set for all mentions:
   - During Phase 3 (persistence), compute and store `dedupe_key` for each mention.
   - If migrating existing data, run a one‑time backfill job to compute `dedupe_key` in SQL or via ORM.

---

### 2. Local LLM comparator contract

Goal: define a small, robust JSON contract for comparing a single mention with a single canonical candidate.

1. Define request schema:
   - Example JSON:
     ```json
     {
       "mention": {
         "type": "OBJECT",
         "text": "Invoice",
         "evidence": "The user uploads an invoice document.",
         "extra": {
           "actor_name": "User",
           "verb": "upload",
           "object_name": "invoice document",
           "state": "UPLOADED"
         }
       },
       "candidate": {
         "canonical_id": "uuid-123",
         "canonical_type": "OBJECT",
         "name": "Invoice",
         "norm_name": "invoice",
         "aliases": ["billing invoice", "bill"]
       },
       "task": "Are these the same real-world business entity?"
     }
     ```

2. Define response schema:
   - Example JSON:
     ```json
     {
       "decision": "SAME",
       "confidence": "HIGH",
       "reason": "Invoice and billing invoice refer to the same business document."
     }
     ```
   - Allowed `decision` values:
     - `SAME`
     - `DIFFERENT`
     - `UNSURE`
   - `confidence` can be a small enum (`LOW | MEDIUM | HIGH`) or numeric; align with implementation.

3. Implement wrapper:
   - `compare_mention_with_candidate(mention, canonical, type) -> DecisionResult`:
     - Builds the JSON request.
     - Calls the local LLM.
     - Parses the JSON response.
     - Validates `decision` and `confidence`.
     - Handles:
       - Timeouts.
       - Invalid JSON.
       - Missing fields.
     - On error:
       - Optionally default to `UNSURE` or treat as a failure, but log clearly.

---

### 3. Canonicalization runners per type

Goal: provide dedicated runners to canonicalize all mentions of each type for a given document (or across documents).

Define four runners:

1. `canonicalize_actors(doc_id: UUID) -> run_id`
2. `canonicalize_objects(doc_id: UUID) -> run_id`
3. `canonicalize_object_states(doc_id: UUID) -> run_id`
4. `canonicalize_actions(doc_id: UUID) -> run_id`

Common runner structure:

1. Create an `extraction_runs` row:
   - `run_kind` set to the appropriate enum (`CANONICALIZE_ACTORS`, etc.).
   - `doc_id` set to the target document (or `NULL` if cross‑document run; document this behavior).
   - `status = RUNNING`.

2. Fetch mentions to canonicalize:
   - Query `mentions` for:
     - `doc_id` (if document‑scoped).
     - Specific `type` (`ACTOR`, `OBJECT`, `STATE`, `ACTION`).
   - Exclude mentions that already have a final mapping in `mention_to_canonical` unless you are re‑running / remediating:
     - Example filter: left join `mention_to_canonical` and only pick rows with no mapping or `decision = PENDING`.

3. Group by `dedupe_key`:
   - For the relevant type, group mentions by `dedupe_key` to avoid repeating work:
     - All mentions in the same group are strong candidates to map to the same canonical.
   - Process one representative mention (or aggregated evidence) for each group, then apply the result to all mentions in that group.

4. For each group:
   - Step 1: Attempt deterministic match (SQL lookup; see section 4).
   - Step 2: If deterministic match fails:
     - Use Qdrant candidate retrieval (Phase 4 API).
   - Step 3: Run local LLM tournament (section 5) among top candidates.
   - Step 4: Based on outcome, either:
     - Link group’s mentions to an existing canonical, or
     - Create a new canonical and link.

5. Finalize run:
   - Compute statistics:
     - Number of mentions processed.
     - Number resolved by deterministic rule.
     - Number resolved by LLM.
     - Number that created new canonicals.
   - Update `extraction_runs.stats_json` and set `status = SUCCEEDED` (or `FAILED` on unrecoverable error).

---

### 4. Deterministic match implementation

Goal: resolve as many mentions as possible using cheap SQL lookups before invoking any LLM.

1. Implement helper:
   - `find_canonical_by_dedupe_key(mention_type: str, dedupe_key: str) -> Optional[Canonical]`.

2. Strategy per type:
   - **Actor / Object**:
     - Extract normalized name from `dedupe_key` (strip the `actor::` or `object::` prefix).
     - Query `canonicals`:
       - Where `canonical_type` matches and `norm_name` equals the normalized name.
     - If not found:
       - Query `canonical_aliases` via `alias_norm` to find a canonical by alias.
   - **State (within an object)**:
     - This is handled after canonical objects are known:
       - For each state mention, we know `canonical_object_id`.
       - Use `object_states` (or encoded state canonicals) with:
         - `canonical_object_id` and `state_norm = norm(state_name)` to look up an existing state.
   - **Action**:
     - Once actor and object mentions are canonicalized:
       - Compute `action_sig`:
         - `action_sig = "action::" + canonical_actor_id + "::" + verb_norm + "::" + canonical_object_id`.
       - Use this as a deterministic lookup key in `canonicals` if you choose to encode it there, or in a dedicated `canonical_actions` signature column.

3. When deterministic match succeeds:
   - For the mention group:
     - Insert `mention_to_canonical` rows for each mention with:
       - `canonical_id` of the matched canonical.
       - `decision = ACCEPT`.
       - `decided_by = RULE`.
       - `run_id` set to the canonicalization run.
   - Skip Qdrant and LLM for these mentions.

---

### 5. Tournament logic

Goal: given a mention (or mention group) and a list of candidate canonicals from Qdrant, decide whether any candidate is the SAME entity, otherwise create a new canonical.

1. Define a generic function:
   - `run_tournament(mention_group, candidates: List[Canonical]) -> TournamentOutcome`.
   - `TournamentOutcome`:
     - Either:
       - `LINK_TO_EXISTING(canonical_id)` or
       - `CREATE_NEW_CANONICAL`.

2. Steps:
   - Step 1: Limit candidate list:
     - Take top `N` candidates (e.g., `N = 3`).
   - Step 2: Iterate candidates in order (best first):
     - For each candidate:
       - Call `compare_mention_with_candidate`.
       - If `decision == SAME` and `confidence` is at or above a chosen threshold:
         - Return `LINK_TO_EXISTING(canonical_id)`.
       - If `decision == DIFFERENT`, continue.
       - If `decision == UNSURE`:
         - Optionally continue or treat as soft negative depending on policy.
   - Step 3: If no candidate yields `SAME`:
     - Return `CREATE_NEW_CANONICAL`.

3. Applying outcome:
   - If `LINK_TO_EXISTING`:
     - For each mention in the group:
       - Insert or update `mention_to_canonical`:
         - `decision = ACCEPT`.
         - `decided_by = LLM`.
         - `canonical_id` = chosen canonical.
   - If `CREATE_NEW_CANONICAL`:
     - Use information from the mention group to propose:
       - `name`, `norm_name`, and aliases.
     - Call `upsert_canonical_and_qdrant` (Phase 4).
     - Map all mentions in the group to the new canonical with `decision = ACCEPT`, `decided_by = LLM`.

---

### 6. Ordering and dependencies between types

Goal: enforce canonicalization order to reduce ambiguity and ensure action and state resolution have required context.

1. Canonicalization order (per `plan.md`):
   - Step 1: Actors.
   - Step 2: Objects.
   - Step 3: States within each canonical object.
   - Step 4: Actions (after actor/object canonical IDs are known).

2. Enforcement:
   - When orchestrating runs:
     - Run `canonicalize_actors(doc_id)` first.
     - Then `canonicalize_objects(doc_id)`.
     - Then `canonicalize_object_states(doc_id)` (requires object mappings).
     - Finally `canonicalize_actions(doc_id)` (requires actor and object mappings).

3. Action canonicalization details:
   - For each ACTION mention:
     - Retrieve its associated ACTOR and OBJECT mentions (via relations).
     - Ensure these mentions already have `mention_to_canonical` mappings.
     - Use canonical IDs and normalized verb:
       - `action_sig = "action::" + canonical_actor_id + "::" + verb_norm + "::" + canonical_object_id`.
     - First try deterministic lookup using `action_sig`.
     - If not found:
       - Use Qdrant `canonical_actions` collection and local LLM comparison focused primarily on verb synonymy (since actor/object identity is now fixed).

4. State canonicalization details:
   - For each STATE mention:
     - Identify its object mention (via `OBJECT_HAS_STATE` relation).
     - Resolve that object’s canonical ID.
     - Within the context of that canonical object:
       - Use `object_states` with (`canonical_object_id`, `state_norm`) for deterministic match.
       - Only if necessary, consider Qdrant‑based retrieval within that object’s state space.
   - Never compare states across unrelated objects in MVP.

---

### 7. Audit and replayability in `mention_to_canonical` and `canonical_merges`

Goal: keep a full audit trail of how mappings were decided and allow safe merges and re‑runs.

1. Audit fields:
   - Ensure `mention_to_canonical` records:
     - `mention_id`.
     - `canonical_id`.
     - `decision` (`ACCEPT | REJECT | PENDING`).
     - `decided_by` (`RULE | LLM | HUMAN`).
     - `run_id` (canonicalization run).
   - For LLM‑based decisions:
     - Consider logging a short explanation or pointer to the LLM call in `llm_calls`.

2. Merges:
   - When merging two canonicals:
     - Record in `canonical_merges` with:
       - `from_canonical_id`, `to_canonical_id`, `run_id`, `reason`.
     - Update `canonicals.superseded_by` for `from_canonical_id`.
     - In Qdrant, either:
       - Remove the `from` point, or
       - Keep and mark as superseded.
   - Query‑time handling:
     - When resolving canonical IDs for display or join:
       - Optionally follow `superseded_by` chains to an active canonical.

3. Replayability:
   - Allow re‑running canonicalization for a document:
     - Option A: treat each run as independent and:
       - Insert new `mention_to_canonical` decisions only where none exists or where `decision = PENDING`.
     - Option B: allow overrides:
       - New run can update existing mappings with a new `run_id`, but keep previous decisions in logs/`llm_calls` for audit.

---

### 8. Testing

Goal: verify that deterministic paths, tournament logic, and ordering work as intended without relying on real external services.

1. **Deterministic path tests**
   - Create a small in‑memory or test DB dataset:
     - Canonical actors: “User”, “Admin”.
     - Canonical objects: “Document”.
   - Insert mentions with:
     - `type = ACTOR`, `fields["name"] = "User"`.
     - `type = OBJECT`, `fields["name"] = "Document"`.
   - Assign appropriate `dedupe_key`s.
   - Run `canonicalize_actors` and `canonicalize_objects` with:
     - Local LLM comparator mocked but not used.
   - Assert:
     - All mentions are resolved via deterministic matches.
     - No local LLM calls occur.

2. **Qdrant + LLM path tests**
   - Stub / mock Qdrant candidate retrieval to return:
     - One or more plausible candidates with distances.
   - Stub local LLM comparator to:
     - Return `SAME` for a known good pair.
     - Return `DIFFERENT` for known negatives.
   - Run canonicalization and assert:
     - Mappings are created with `decided_by = LLM`.
     - New canonicals are created when all candidates are DIFFERENT/UNSURE.

3. **Action ordering tests**
   - Create synthetic mentions:
     - ACTOR “User”.
     - OBJECT “Document”.
     - ACTION “create” linking them via relations.
   - Canonicalize actors and objects first.
   - Then canonicalize actions:
     - Assert that action canonicalization fails gracefully or is skipped if actor/object mappings are missing.
     - Once mappings exist, assert that action canonicalization succeeds and stores a stable `action_sig`.

4. **State scoping tests**
   - Create two object canonicals: “Document” and “Order”.
   - Create state mentions “ARCHIVED” for both.
   - After object canonicalization:
     - Canonicalize states such that:
       - “ARCHIVED” under `Document` and `Order` do **not** get merged cross‑object.
       - Each object gets its own `object_states` entry with unique constraint enforced.

Once this phase is complete, the system will be able to turn raw mentions into stable canonical entities using a combination of deterministic rules, Qdrant retrieval, and local LLM decisions, with full auditability and a clear ordering across entity types.

