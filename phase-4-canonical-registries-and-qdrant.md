# Phase 4 — Canonical registries and Qdrant collections

### Purpose

Create and manage canonical registries in SQL and corresponding Qdrant collections for Actors, Objects, Actions, and Object States, and implement embedding and retrieval logic for candidate canonicalization.

---

### Assumptions

- Phase 1 schema is implemented (`canonicals`, `canonical_aliases`, `object_states`, `mention_to_canonical`, `canonical_merges`).
- Phase 3 extraction is implemented, and mentions are being persisted with `dedupe_key` and `fields_json`.
- A Qdrant client is available in the codebase, or can be added.
- An embedding model is available for canonical entities (this can be the same model used for chunks or a different one; choose and document it).

### Alternative inference path (big LLM)

Phase 3 supports two ways to obtain big-LLM extraction results: **API path** (`run_big_llm_extract` with an LLM client) and **manual path** (`prepare_big_llm_export` → user runs the LLM → `ingest_big_llm_results`). In both cases, frames, mentions, and relations are persisted with the same schema; mentions have `dedupe_key`, `fields_json`, and `created_run_id` pointing to the same `extraction_runs` row. Phase 4 does not need to distinguish how the mentions were produced: canonical registries, Qdrant collections, and candidate retrieval operate identically on mentions regardless of whether they came from the API or manual path.

---

### 1. Qdrant collections design

Goal: define the Qdrant collections used for canonical entities and make their structure explicit.

1. Decide collection layout:
   - Recommended: one collection per canonical type:
     - `canonical_actors`
     - `canonical_objects`
     - `canonical_actions`
     - `canonical_object_states` (or `canonical_states` if you opt for global states—MVP recommends object‑scoped states).
   - Alternatively: a single `canonicals` collection with a `canonical_type` payload field:
     - Only do this if you have a strong reason; per‑type collections usually keep things simpler.

2. Common collection settings (per collection):
   - Distance metric: `Cosine`.
   - Vector size: must match the embedding model’s output dimension; validate at startup.
   - `point_id`: equal to `canonical_id` (UUID string).
   - Payload fields:
     - `canonical_id`: UUID.
     - `canonical_type`: `ACTOR | OBJECT | ACTION | STATE` (if using shared collection).
     - `name`: display name.
     - `norm_name`: normalized name.
     - `aliases`: optional list of short alias strings.
     - `created_at`: timestamp.
     - `superseded_by`: optional canonical ID.
     - Type‑specific fields:
       - For actions: `actor_id`, `object_id`, `verb_norm`.
       - For object states: `canonical_object_id`, `state_name`, `state_norm`.

3. Indexing:
   - Define payload indexes where Qdrant supports them:
     - `norm_name`.
     - `canonical_type` (if single collection).
     - For actions:
       - `actor_id`, `object_id`, `verb_norm`.
     - For object states:
       - `canonical_object_id`, `state_norm`.

---

### 2. Embedding text templates

Goal: build concise, identity‑focused text representations for canonical entities to be embedded and stored in Qdrant.

1. Implement a shared normalization helper (if not already present):
   - `norm(s: str) -> str`:
     - Lowercase.
     - Trim whitespace.
     - Collapse internal whitespace.
     - Optionally remove punctuation except for characters important to meaning.

2. For each canonical type, define an embedding template:
   - **Actor**:
     - Template: `ACTOR: <name>. Aliases: <a1, a2>. Context: <optional representative snippet>`.
     - Use canonical’s `name`, plus a small set of key aliases and possibly one short context snippet.
   - **Object**:
     - Template: `OBJECT: <name>. Aliases: <a1, a2>. Context: <optional>.`.
   - **Action**:
     - Template: `ACTION: <actor_name> <verb_norm> <object_name>. Synonyms: <optional verbs>.`.
     - `actor_name` and `object_name` should be canonical display names (when available).
   - **Object‑state**:
     - Template: `STATE of <object_name>: <state_name>.`.

3. Implement helper functions:
   - `build_actor_embedding_text(canonical: Canonical, aliases: List[CanonicalAlias]) -> str`.
   - `build_object_embedding_text(canonical: Canonical, aliases: List[CanonicalAlias]) -> str`.
   - `build_action_embedding_text(canonical: Canonical, actor: Canonical, obj: Canonical, aliases: List[CanonicalAlias]) -> str`.
   - `build_state_embedding_text(object_canonical: Canonical, state: ObjectState) -> str`.

4. Constraints:
   - Keep embedding text short and focused on identity; do not embed entire documents.
   - If alias list is long, truncate to a reasonable number (e.g., 3–5 aliases).

---

### 3. Canonical upsert flow (SQL + Qdrant)

Goal: define a single high‑level operation that creates/updates a canonical entity in SQL and syncs it with Qdrant.

1. Define API:
   - `upsert_canonical_and_qdrant(canonical_data) -> Canonical`:
     - `canonical_data` includes:
       - `canonical_type`, `name`, optional `aliases`, optional type‑specific info (`actor_id`, `object_id`, etc.).

2. SQL part (transactional):
   - Step 1: Normalize `name` to `norm_name` via `norm()`.
   - Step 2: Lookup existing canonical:
     - By `canonical_id` (if provided), or
     - By (`canonical_type`, `norm_name`) for idempotent creation.
   - Step 3: Insert or update:
     - If none found:
       - Insert new `canonicals` row with `name`, `norm_name`, `canonical_type`, `created_run_id`.
     - If found:
       - Update `name` or metadata as necessary (avoiding destructive changes).
   - Step 4: Upsert aliases:
     - For each alias text in `canonical_data.aliases`:
       - Normalize to `alias_norm = norm(alias_text)`.
       - Insert into `canonical_aliases` if not already present (unique constraint ensures idempotency).

3. Qdrant part:
   - Step 1: Build embedding text using the appropriate helper, based on canonical type.
   - Step 2: Call embedding model to get vector.
   - Step 3: Upsert point into Qdrant collection:
     - Choose collection by canonical type.
     - `point_id = canonical_id`.
     - `vector = embedding`.
     - `payload` includes canonical metadata (as described above).

4. Idempotency:
   - Calling `upsert_canonical_and_qdrant` multiple times with the same canonical should:
     - Keep exactly one SQL canonical row and one Qdrant point.
     - Update metadata and payload but not create duplicates.

---

### 4. Candidate retrieval API

Goal: given a mention and a target canonical type, retrieve the top‑K candidate canonicals from Qdrant.

1. Define a mention “card” text builder:
   - For an **actor mention**:
     - `OBJECT MENTION` → `ACTOR MENTION: <fields["name"]>. Context: <evidence.snippet>`.
   - For an **object mention**:
     - `OBJECT MENTION: <fields["name"]>. Context: <evidence.snippet>`.
   - For an **action mention**:
     - `ACTION MENTION: <actor_name> <verb> <object_name>. Context: <evidence.snippet>`.
   - For a **state mention**:
     - `STATE MENTION: <state_name>. Object hint: <object_name or fields>. Context: <evidence.snippet>`.

2. Implement `embed_mention_card(mention: Mention, frame: Frame) -> Vector`:
   - Build the card text.
   - Call embedding model to get vector.

3. Implement the main retrieval function:
   - `get_canonical_candidates_for_mention(mention: Mention, type: str, k: int = 10) -> List[Candidate]`.
   - Steps:
     - Step 1: Determine target collection based on `type`:
       - `ACTOR` → `canonical_actors`.
       - `OBJECT` → `canonical_objects`.
       - `ACTION` → `canonical_actions`.
       - `STATE` → `canonical_object_states` (or similar).
     - Step 2: Build mention card text and embed.
     - Step 3: Query Qdrant `search`:
       - `top_k = k`.
       - Apply any collection‑specific filters:
         - For actions, optionally filter by `actor_id` / `object_id` if already known.
     - Step 4: Deduplicate by canonical identity:
       - Collapse results by `canonical_id` or `norm_name`.
     - Step 5: Return top 1–3 distinct candidates as `Candidate` objects:
       - Each contains `canonical_id`, `name`, `norm_name`, aliases, distance/score, and type‑specific payload.

4. Distance interpretation:
   - Decide how to surface similarity:
     - Provide raw Qdrant distance.
     - Optionally convert to a confidence heuristic (for logging/analysis; actual decision is left to the local LLM in Phase 5).

---

### 5. Initialization and maintenance

Goal: ensure Qdrant is correctly configured at service startup and remains consistent as canonicals are merged.

1. Startup initialization:
   - On service start, run a bootstrap routine:
     - For each canonical collection:
       - If the collection does not exist, create it with:
         - Correct vector size and distance metric.
         - Recommended payload index configuration.
       - If it exists, validate:
         - Vector size matches expected.
         - Distance metric is Cosine.

2. Backfill / resync tasks (if upgrading existing data):
   - If canonicals already exist in SQL but Qdrant is empty or outdated:
     - Implement a one‑time job:
       - Iterate through all `canonicals` and `object_states`.
       - For each, call `upsert_canonical_and_qdrant` to create Qdrant points.

3. Handling canonical merges:
   - When a merge is requested (via admin or automatic process):
     - Insert into `canonical_merges` with `from_canonical_id`, `to_canonical_id`, `run_id`, and `reason`.
     - Update `canonicals.superseded_by` for `from_canonical_id`.
     - In Qdrant:
       - Option 1: delete the `from_canonical_id` point.
       - Option 2: keep it but set `superseded_by` in payload; retrieval code can penalize or filter superseded entities.
     - Optionally:
       - Run a background job that rewrites `mention_to_canonical` rows from `from` to `to`.

4. Consistency checks:
   - Periodically run a health check:
     - Verify each SQL `canonical_id` has at most one point in Qdrant.
     - Verify each Qdrant point’s `canonical_id` exists in SQL.
     - Log or repair discrepancies.

---

### 6. Testing

Goal: validate that canonical registries and Qdrant integration work as expected and are idempotent.

1. **Smoke test: insert + search**
   - Create a small set of canonical actors and objects in SQL:
     - Example:
       - Actor: “User” with aliases `["End user", "Customer"]`.
       - Actor: “Admin”.
       - Object: “Document” with aliases `["File", "Record"]`.
   - Upsert them using `upsert_canonical_and_qdrant`.
   - Build a mention card for “User creates Document” and query:
     - `get_canonical_candidates_for_mention` for an actor mention “User”.
     - Assert that the top candidate is the “User” canonical.
   - Run an object mention “File” and verify “Document” is surfaced appropriately.

2. **Idempotency test**
   - Call `upsert_canonical_and_qdrant` twice for the same canonical data.
   - Verify:
     - There is still only one row in `canonicals`.
     - There is a single Qdrant point for that `canonical_id`.
     - No duplicate aliases were created.

3. **Merge test**
   - Create two similar canonical objects (e.g., “Invoice” and “Billing invoice”).
   - Manually record a merge (`from` = “Billing invoice”, `to` = “Invoice”).
   - Verify:
     - `canonical_merges` contains a row.
     - `canonicals.superseded_by` is set for the `from` entity.
     - Qdrant either:
       - Has removed the `from` point, or
       - Marked it as superseded in payload.

4. **Startup validation**
   - With collections already present:
     - Restart the service and verify:
       - No errors are thrown during collection validation.
       - Mismatched vector sizes or distance metrics are detected and surfaced clearly in logs.

Once this phase is complete, canonical entities will have robust SQL ledger storage and Qdrant‑backed semantic retrieval, enabling effective candidate selection for the local canonicalization tournament in Phase 5.

