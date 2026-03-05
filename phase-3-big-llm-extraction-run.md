# Phase 3 — Big LLM extraction run

### Purpose

Implement the big LLM extraction run: prompting, calling the cloud model, parsing and validating JSON output, performing evidence substring checks, and persisting frames, mentions, relations, and run metadata into SQL.

---

### Assumptions

- There is an `llm_client` abstraction capable of calling a cloud LLM and returning a text response.
- There is a way to store LLM requests/responses in `llm_calls` linked to `extraction_runs`.
- `build_marked_doc` and `size_guard` from Phase 2 are implemented and tested.
- Phase 1 schema (tables like `extraction_runs`, `frames`, `mentions`, `mention_evidence`, `relations`) is in place and accessible via the ORM.

---

### 1. Prompt design (`big_extract_v1`)

Goal: define a robust prompt that yields strict JSON with the exact schema we need, and that enforces the evidence‑substring rule.

1. Define constant identifiers:
   - `PROMPT_VERSION_BIG_EXTRACT = "big_extract_v1"`.
   - `BIG_EXTRACT_ALLOWED_MENTION_TYPES = ["ACTOR", "OBJECT", "ACTION", "STATE"]`.
   - `BIG_EXTRACT_ALLOWED_REL_TYPES = ["ACTION_HAS_ACTOR", "ACTION_HAS_OBJECT", "OBJECT_HAS_STATE"]` (plus optional future ones).

2. Design the **system prompt**:
   - Explain high‑level task:
     - The model receives a chunk‑marked document describing a business domain.
     - It must extract **frames**, **mentions**, and **relations** according to the business model (Actor, Object, State, Action).
   - Emphasize core rules:
     - Output MUST be **valid JSON only** (no comments, no trailing commas, no extra text).
     - Every mention must have `evidence.snippet` that is a verbatim substring of its source chunk.
     - Relations must be explicit objects; do not rely on co‑location of mentions.
   - Describe business model briefly:
     - Actor: participant (User/Admin/System/ExternalService).
     - Object: business entity with states.
     - State: label for object lifecycle state.
     - Action: business action linking actor and object, possibly causing state transition.

3. Design the **user prompt** template:
   - Parameters: `doc_part_text`, `doc_id`, `chunk_id_list`.
   - Content:
     - Brief reminder of JSON schema:
       - Top‑level keys: `doc_id`, `prompt_version`, `frames`, `references`, `errors`.
       - Frame structure: `frame_id`, `chunk_id`, `frame_index`, `frame_text`, `mentions`, `relations`.
       - Mention structure: `mention_id`, `type`, `fields`, `epistemic`, `evidence`, `confidence`.
       - Relation structure: `src_mention_id`, `rel_type`, `dst_mention_id`.
     - Explicitly list allowed mention types and relation types.
     - Instruction to:
       - Reuse `chunk_id` from the markers.
       - Construct `frame_id` as `frame::<chunk_id>::<n>`.
       - Construct `mention_id` as `m::<frame_id>::<n>`.
       - If references are extracted, include them in `references` with the specified schema.
     - Include a literal block with the chunk‑marked document:
       - For example: `DOCUMENT_START` / `DOCUMENT_END` markers surrounding the `doc_part_text`.

4. Decide how to handle multi‑part documents:
   - Option A (simpler): each part produces its own top‑level object with `doc_id` and `frames`; you later merge frames across parts.
   - Ensure that:
     - `frame_id` remains unique across parts (embedded `chunk_id` + `frame_index` handles this).

5. Store prompt templates:
   - Place system and user prompt templates in a dedicated module, with `PROMPT_VERSION_BIG_EXTRACT` referenced in code and stored in `extraction_runs.prompt_version`.

---

### 2. Runner entrypoint design

Goal: define a clean entrypoint that orchestrates the full big LLM extraction for a given document.

1. Define a top‑level function:
   - `run_big_llm_extract(doc_id: UUID, model_id: str) -> UUID` (returns `run_id`).

2. Steps inside `run_big_llm_extract`:
   - Step 1: Create an `extraction_runs` record:
     - `run_kind = BIG_LLM_EXTRACT`.
     - `doc_id = doc_id`.
     - `prompt_version = PROMPT_VERSION_BIG_EXTRACT`.
     - `model_id = model_id`.
     - `status = RUNNING`.
     - `input_hash` = hash of:
       - Document content fingerprint (e.g., doc file hash).
       - Prompt version.
       - Model id.
   - Step 2: Build chunk‑marked document and parts:
     - Call `build_marked_doc(doc_id)`.
     - Call `size_guard(marked_doc)` to get `DocPart[]`.
   - Step 3: For each `DocPart`:
     - Construct prompts (system + user) with:
       - The part text (`DocPart.text`).
       - The doc id and chunk index range for context.
     - Call the LLM via `llm_client`:
       - Capture request payload (full prompts and parameters) and raw response text.
       - Measure latency and token usage if available.
     - Insert a row into `llm_calls` linking to `run_id` with:
       - `model_id`, `prompt_version`, request, response, token counts, latency.
     - Store raw responses for later parsing (e.g., in memory list or by querying `llm_calls` again).
   - Step 4: After all parts are processed:
     - Pass all raw responses into a parsing and validation pipeline (described below).
   - Step 5: Depending on validation outcome:
     - On success:
       - Persist frames, mentions, evidence, relations in SQL.
       - Compute run statistics.
       - Mark run `status = SUCCEEDED`.
     - On failure:
       - Mark run `status = FAILED`.
       - Populate `stats_json` / error fields with validation errors.

---

### 3. Parsing and schema validation

Goal: convert raw LLM responses (strings) into typed objects, validate them against the schema, and collect any errors.

1. Define internal data models (DTOs) mirroring the JSON schema:
   - `BigExtractResult`:
     - `doc_id: str`
     - `prompt_version: str`
     - `frames: List[FrameResult]`
     - `references: List[ReferenceResult]`
     - `errors: List[str]`
   - `FrameResult`:
     - `frame_id: str`
     - `chunk_id: str`
     - `frame_index: int`
     - `frame_text: str | None`
     - `mentions: List[MentionResult]`
     - `relations: List[RelationResult]`
   - `MentionResult`:
     - `mention_id: str`
     - `type: str`
     - `fields: Dict[str, Any]`
     - `epistemic: str`
     - `evidence: EvidenceResult`
     - `confidence: str | float`
   - `EvidenceResult`:
     - `snippet: str`
     - `char_start: int | None`
     - `char_end: int | None`
   - `RelationResult`:
     - `src_mention_id: str`
     - `rel_type: str`
     - `dst_mention_id: str`
   - `ReferenceResult` (optional for MVP):
     - `ref_id: str`
     - `chunk_id: str`
     - `ref_text_raw: str`
     - `ref_norm: str`
     - `evidence: EvidenceResult`

2. Implement `parse_big_extract_response(raw_text: str) -> BigExtractResult | ParseError`:
   - Attempt to parse `raw_text` as JSON.
   - If JSON parsing fails:
     - Return a structured `ParseError` including original text snippet and error message.
   - If parsing succeeds:
     - Validate presence and type of top‑level fields:
       - `doc_id` (string), `prompt_version` (string), `frames` (list), `references` (list or absent), `errors` (list or absent).

3. Implement `validate_big_extract_result(result: BigExtractResult, doc_id: UUID, known_chunk_ids: Set[str]) -> List[ValidationError]`:
   - Top‑level checks:
     - `result.doc_id` must equal the target `doc_id`.
     - `result.prompt_version` must equal `PROMPT_VERSION_BIG_EXTRACT`.
   - For each frame:
     - `frame_id` non‑empty string; matches pattern `frame::<chunk_id>::<n>` (log warning if not).
     - `chunk_id` is present and in `known_chunk_ids`.
     - `frame_index` is an integer ≥ 0.
     - Maintain a map of (`chunk_id`, `frame_index`) to ensure uniqueness.
   - For each mention:
     - `mention_id` non‑empty; ideally matches pattern `m::<frame_id>::<n>`.
     - `type` is in `BIG_EXTRACT_ALLOWED_MENTION_TYPES`.
     - `fields` is an object/dict.
     - `epistemic` is `EXPLICIT` or `INFERRED`.
     - `evidence` has non‑empty `snippet`.
   - For each relation:
     - `rel_type` is in `BIG_EXTRACT_ALLOWED_REL_TYPES`.
     - `src_mention_id` and `dst_mention_id` reference existing mention IDs within the same `doc_id`.

4. Combine parsing and validation:
   - For each part’s raw response:
     - Parse, then validate against the global context (`doc_id`, known chunk IDs).
   - Aggregate all `ValidationError`s across parts.
   - Decide error policy:
     - MVP rule (per `plan.md`): if any validation error is **fatal**, mark the run as `FAILED` and **do not** persist frames/mentions/relations, but still keep `llm_calls` data and possibly store validation errors in `stats_json`.

---

### 4. Evidence substring validation

Goal: enforce the evidence‑substring discipline by checking that every `evidence.snippet` is a verbatim substring of the originating chunk.

1. Prepare chunk text cache:
   - For all `chunk_id`s referenced in `FrameResult`s:
     - Load `chunks.text` from SQL into a dictionary: `chunk_text_by_id: Dict[str, str]`.

2. Implement `validate_evidence_snippet(mention: MentionResult, frame: FrameResult, chunk_text_by_id) -> Optional[ValidationError]`:
   - Get `chunk_text = chunk_text_by_id[frame.chunk_id]`.
   - Check that `mention.evidence.snippet` is a substring of `chunk_text`:
     - If not, record a validation error.
   - If `char_start` / `char_end` are present:
     - Verify `0 <= char_start < char_end <= len(chunk_text)`.
     - Verify `chunk_text[char_start:char_end] == snippet`.

3. Apply to all mentions:
   - Iterate over all frames and their mentions.
   - Collect all evidence validation errors.

4. Decide behavior on failure:
   - MVP: treat any failed evidence validation as a fatal error:
     - Mark the `extraction_run` as `FAILED`.
     - Do **not** persist frames/mentions/relations.
   - Still store:
     - Raw `llm_calls` responses.
     - Summary of validation issues in `stats_json` / logs.

---

### 5. Persistence to SQL

Goal: transform validated DTOs into ORM entities and insert them in a safe, transactional way.

1. Define mapping functions:
   - `to_frame_entity(frame_result: FrameResult, doc_id: UUID, run_id: UUID) -> Frame`.
   - `to_mention_entity(mention_result: MentionResult, frame: Frame, doc_id: UUID, run_id: UUID) -> Mention`.
   - `to_mention_evidence_entity(mention_result: MentionResult) -> MentionEvidence`.
   - `to_relation_entity(relation_result: RelationResult, frame: Frame, run_id: UUID) -> Relation`.

2. Compute `dedupe_key` for mentions (for later canonicalization):
   - Implement a shared normalization function `norm(s: str) -> str` (Phase 5 will reuse this).
   - For each mention, compute `dedupe_key` based on:
     - Actor: `actor::<norm(name)>`.
     - Object: `object::<norm(name)>`.
     - Action: `action::<norm(actor_name)>::<norm(verb)>::<norm(object_name)>`.
     - State (pre‑canonical): e.g., `state::<norm(object_name_or_hint)>::<norm(state_name)>` (exact strategy can be refined later).

3. Transactional insertion:
   - Start a database transaction spanning the entire persistence step (per document or per run).
   - Insert entities in dependency order:
     - Frames.
     - Mentions (with `dedupe_key` and `created_run_id`).
     - MentionEvidence.
     - Relations.
   - On any DB exception:
     - Roll back the transaction.
     - Mark run `status = FAILED`.

4. Run status and stats:
   - If all inserts succeed:
     - Compute basic stats:
       - Number of frames.
       - Number of mentions per type.
       - Number of relations.
     - Store in `extraction_runs.stats_json`.
     - Mark run `status = SUCCEEDED`.

---

### 6. Integration test

Goal: verify that the end‑to‑end big extraction runner works without calling a real external LLM.

1. Prepare test fixtures:
   - Create a small test document in the system:
     - Insert a `documents` row.
     - Insert a few `chunks` rows with simple text like:
       - “The User creates a new Document.”
       - “The Admin archives the Document.”
   - Provide a canned JSON response that matches the schema and references those `chunk_id`s.

2. Mock `llm_client`:
   - Configure `llm_client` such that:
     - When called with the test prompts, it returns the canned JSON string (or equivalent for each part).

3. Execute:
   - Call `run_big_llm_extract(doc_id, model_id="test-model")`.

4. Assertions:
   - There is an `extraction_runs` row with:
     - `doc_id` equal to the test document.
     - `run_kind = BIG_LLM_EXTRACT`.
     - `status = SUCCEEDED`.
   - `llm_calls` has entries linked to `run_id` with stored request/response.
   - `frames`, `mentions`, `mention_evidence`, `relations` contain the expected rows:
     - Mentions have correct `type`, `fields_json`, `epistemic`, and `confidence`.
     - Evidence snippets are valid substrings of the appropriate chunk text.
   - `stats_json` includes correct counts.

5. Negative test:
   - Provide a canned JSON response with an invalid `evidence.snippet` or schema violation.
   - Ensure:
     - Run `status = FAILED`.
     - No frames/mentions/relations are persisted.
     - Raw `llm_calls` entry is still present for debugging.

Once this phase is complete, the system will be able to run a full big‑LLM extraction over any ingested document, producing structured frames, mentions, and relations that are ready for canonicalization in Phases 4 and 5.

