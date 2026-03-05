# Phase 6 — Observability and replayability

### Purpose

Add observability, metrics, and replay/debug capabilities to inspect extraction runs, canonicalization decisions, and LLM usage across the pipeline.

---

### Assumptions

- Phases 1–5 are implemented and functional.
- `extraction_runs` and `llm_calls` tables are in place.
- There is a CLI or HTTP API surface where debug commands/endpoints can be added.

### Alternative inference path (manual big LLM)

Phase 3 supports two ways to run the big LLM step:

- **API path**: `run_big_llm_extract(doc_id, model_id)` calls an injectable LLM client and writes request/response into `llm_calls` as each part is completed.
- **Manual path**: `prepare_big_llm_export(doc_id, model_id, output_dir)` creates an `extraction_run` and writes prompt+document files (and a manifest); the user runs the LLM externally and saves result file(s); `ingest_big_llm_results(run_id, result_paths)` reads those files, writes one `llm_calls` row per part (with `request_json` e.g. `{"source": "manual_run", "part_index": i}` and `response_text` from the file), then runs the same parse → validate → evidence → persist pipeline.

Observability and replay below apply to **both** paths unless stated otherwise. For the manual path, `llm_calls` still provide an audit trail; the only semantic difference is that `request_json` does not contain the full prompt (it may reference the exported file or `"manual_run"`), while `response_text` is the ingested LLM output.

---

### 1. Prompt and version tracking

Goal: make it easy to know exactly which prompt and model produced any given run.

1. Ensure every LLM‑related run stores:
   - `prompt_version` in `extraction_runs`:
     - For example:
       - Big extract: `big_extract_v1`.
       - Local comparator: `local_compare_v1`.
   - `model_id` in `extraction_runs` and in `llm_calls`.

2. Optional: introduce a prompt template registry:
   - Either a table (`prompt_templates`) or a configuration file/module mapping:
     - `prompt_version` → full system/user prompt text.
   - Provide helper:
     - `get_prompt_template(prompt_version)` used by all LLM callers.
   - This allows:
     - Quick inspection of prompts for any past run.
     - Safer evolution of prompts (new version string for each material change).

---

### 2. LLM calls logging

Goal: capture sufficient detail about each LLM request/response for debugging and analysis.

1. Confirm `llm_calls` schema includes:
   - `call_id` (PK).
   - `run_id` (FK → `pipeline_runs`; nullable when used for extraction runs) and/or `extraction_run_id` (FK → `extraction_runs`).
   - `model_id`.
   - `prompt_version` (or equivalent).
   - `request_json` or `request_text` (full serialized prompt and parameters, or a placeholder for manual path).
   - `response_text`.
   - `token_in`, `token_out` (if available).
   - `latency_ms`.
   - `created_at`.

2. Update all LLM call sites to:
   - Always write a `llm_calls` row:
     - Big extract (Phase 3): API path stores full request + response; **manual path** stores a row per part with `request_json` e.g. `{"source": "manual_run", "part_index": i}` and `response_text` from the ingested file (no token/latency from external LLM).
     - Local comparator (Phase 5).
   - Capture:
     - Structured request (e.g., system + user messages), or for manual big extract, a minimal stub for audit.
     - Raw response.

3. Indexes:
   - Add indexes as needed for query performance:
     - Index on `run_id`.
     - Optional: composite index on (`model_id`, `prompt_version`).

---

### 3. Run statistics and summary

Goal: record high‑level metrics for each run so you can quickly assess pipeline behavior.

1. Extend `extraction_runs.stats_json` to include:
   - For a **BIG_LLM_EXTRACT** run:
     - `frames_count`.
     - `mentions_count_total`.
     - `mentions_count_by_type` (per `ACTOR|OBJECT|ACTION|STATE`).
     - `relations_count`.
     - Evidence validation summary:
       - `evidence_valid_count`.
       - `evidence_invalid_count`.
   - For canonicalization runs (`CANONICALIZE_ACTORS`, etc.):
     - `mentions_processed`.
     - `resolved_by_rule` (deterministic).
     - `resolved_by_llm`.
     - `new_canonicals_created`.
     - `llm_calls_count`.

2. Implement helpers:
   - `compute_big_extract_stats(frames, mentions, relations, evidence_errors) -> dict`.
   - `compute_canonicalization_stats(mentions, mappings, new_canonicals, llm_call_count) -> dict`.

3. Store stats:
   - At the end of each run’s main function:
     - Compute the appropriate stats dictionary.
     - Serialize to JSON and store in `extraction_runs.stats_json`.

---

### 4. Debug surfaces (CLI / HTTP)

Goal: provide quick ways for developers/analysts to inspect how the pipeline interpreted and canonicalized a document.

Define a small set of high‑value debug commands or endpoints. Names below are suggestive; adapt to your infrastructure.

1. `doc_debug(doc_id)`:
   - Shows pipeline status for a document:
     - Document metadata (title, source).
     - All related `extraction_runs`:
       - `run_id`, `run_kind`, `status`, `created_at`, `prompt_version`, `model_id`.
     - For each run:
       - High‑level stats from `stats_json`.
   - Implementation:
     - Single query to fetch runs by `doc_id`.
     - Map to a human‑readable summary.

2. `chunk_view(doc_id, chunk_index)`:
   - Shows:
     - Chunk text and metadata (`chunk_id`, `section_path`).
     - Frames associated with the chunk (from `frames` table).
     - Mentions within those frames:
       - Type, fields, evidence, and any `mention_to_canonical` mapping.
   - Implementation:
     - Lookup chunk by (`doc_id`, `chunk_index`).
     - Join to `frames` via `chunk_id`.
     - Join to `mentions` and `mention_evidence`.
     - Left‑join `mention_to_canonical` to show mapping decisions.

3. `mention_view(mention_id)`:
   - Shows:
     - Mention details (type, fields, evidence).
     - Frame and chunk context (frame text, chunk text snippet).
     - Canonical mapping (if any):
       - `canonical_id`, canonical `name`, `norm_name`, aliases.
       - `decision`, `decided_by`, `run_id`.
     - If decided by LLM:
       - Optionally, a link to the relevant `llm_calls` record.

4. `canonical_view(canonical_id)`:
   - Shows:
     - Canonical details: `name`, `norm_name`, `canonical_type`, `superseded_by`, etc.
     - Aliases from `canonical_aliases`.
     - If `canonical_type = OBJECT` and using `object_states`:
       - All states for that object.
     - All mentions mapped to this canonical:
       - List of `mention_id`s, mention type, doc/chunk/frame context.
   - Implementation:
     - Query `canonicals` and its relationships (`aliases`, `object_states`).
     - Query `mention_to_canonical` by `canonical_id` and join to `mentions`.

5. Implementation detail:
   - Decide whether these are:
     - CLI commands that print to stdout, or
     - HTTP endpoints returning JSON.
   - Favor JSON responses if you expect tools or UIs to consume them.

---

### 5. Replayability hooks

Goal: allow safe re‑execution of big extraction and canonicalization steps for experimentation and remediation, without breaking invariants.

1. Re‑running big extraction:
   - Provide a CLI or function:
     - `rerun_big_extract(doc_id, prompt_version=None, model_id=None, force=False)`.
   - Behavior:
     - If `force=False`:
       - Check for an existing `BIG_LLM_EXTRACT` run with the same `input_hash`.
       - If found and `status = SUCCEEDED`, return that run (do not re‑run).
     - If `force=True` or prompt/model differ:
       - Create a new `extraction_runs` row with:
         - New `run_id`.
         - New `prompt_version`/`model_id`.
       - Run the extraction pipeline as in Phase 3.
   - Idempotency:
     - Ensure that unique constraints on frames/mentions use IDs that prevent conflicting duplicates:
       - Option A: run extractions into a new “version” of frames/mentions, keyed by `run_id`.
       - Option B: overwrite/replace previous data for that `doc_id` and run type (more complex; only if required).

2. Re‑running canonicalization:
   - Provide functions:
     - `rerun_canonicalization(doc_id, run_kind, force=False)`.
   - Behavior:
     - Default: Canonicalization operates only on mentions **without** existing mappings or with `decision = PENDING`.
     - If `force=True`:
       - Option A: allow updating existing mappings with a new run ID (preserving history in `llm_calls`).
       - Option B: mark previous mappings as superseded by the new run (requires extra fields).

3. Re‑running big extraction when using the **manual path**:
   - “Rerun” can mean two things:
     - **Re-export**: Call `prepare_big_llm_export` again to get a new `run_id` and fresh prompt/document files; the user then runs the LLM and calls `ingest_big_llm_results` with the new result files. No change to `input_hash` semantics unless document or prompt/model changed.
     - **Re-ingest only**: If the user already has result files from a previous manual run and wants to re-apply the pipeline (e.g. after a bug fix in parsing or persistence), they can call `ingest_big_llm_results(run_id, result_paths)` for an existing run that is still in `RUNNING` (or a dedicated status like `AWAITING_RESULTS`). If the run was already completed, document whether re-ingest is allowed (e.g. block, or support a “replace” mode that clears frames/mentions for that run and re-persists).
   - For idempotency when re-exporting: the same `doc_id`, `prompt_version`, `model_id`, and document content produce the same `input_hash`; if a run with that `input_hash` already exists and succeeded, callers may optionally return that run instead of creating a new one (consistent with `rerun_big_extract(doc_id, force=False)` on the API path).

4. CLI flags:
   - For big extraction and canonicalization commands:
     - `--force` to ignore or bypass caching based on `input_hash`.
     - `--prompt-version` and `--model-id` to try new prompt/model configurations.
     - Optional: `--dry-run` to parse and validate without writing to SQL.
   - For the manual path specifically:
     - A “prepare” command that calls `prepare_big_llm_export` and prints `run_id` and output paths.
     - An “ingest” command that takes `run_id` and path(s) to result file(s) and calls `ingest_big_llm_results`.

---

### 6. Testing

Goal: ensure observability and replay features behave correctly and do not violate data invariants.

1. **Stats population tests**
   - Use existing Phase 3 and Phase 5 integration tests.
   - After runs complete:
     - Fetch `extraction_runs.stats_json`.
     - Assert:
       - Expected keys exist.
       - Values match known numbers of frames, mentions, relations, etc.

2. **Debug endpoints/commands tests**
   - For `doc_debug`:
     - Insert a document with:
       - One big extract run.
       - One or more canonicalization runs.
     - Call `doc_debug` and assert:
       - All runs are listed with correct metadata and summaries.
   - For `chunk_view`:
     - Confirm that:
       - Chunk text and section path are shown.
       - Frames and mentions are listed.
       - Canonical mapping information is displayed where it exists.
   - For `mention_view` and `canonical_view`:
     - Assert correct joining and formatting of data.

3. **Replay tests**
   - Big extraction:
     - Run big extract for a doc (with mocked LLM).
     - Call `rerun_big_extract(doc_id, force=False)` and ensure:
       - It returns the existing run (no new `extraction_runs` row).
     - Call `rerun_big_extract(doc_id, force=True)` and ensure:
       - A new run is created.
       - Behavior aligns with chosen strategy for managing previous frames/mentions.
   - Canonicalization:
     - After one canonicalization run:
       - Call `rerun_canonicalization(doc_id, run_kind, force=False)` and verify:
         - Only unmapped mentions (if any) are processed.
       - Call with `force=True` and verify:
         - Mappings can be updated or re‑computed without violating uniqueness constraints on `mention_to_canonical`.

Once this phase is complete, you will have robust observability and replay controls: every extraction/canonicalization run will be traceable back to its prompts and models, and you will be able to inspect, debug, and safely re‑execute any part of the pipeline.

