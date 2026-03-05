# Phase 2 — Docling chunking and chunk‑marked document builder

### Purpose

Implement the Docling‑based chunking pipeline and the builder that assembles a single chunk‑marked document (or multiple parts) in the prescribed format, with size guarding and stable IDs.

---

### Assumptions

- There is already a Docling integration producing some form of structured output, or it can be added.
- `documents` and `chunks` tables and an ingestion pipeline either exist or will be extended to store chunks in SQL.
- There is an agreed size limit for the big LLM input (for example, 2 MB of UTF‑8 text, as described in `plan.md`).

---

### 1. Define the chunk model and interface

Goal: standardize the in‑memory representation of a chunk used across ingestion, storage, and marked‑doc building.

1. Define a `Chunk` data structure (class, dataclass, or interface) with at least:
   - `chunk_id` — UUID or stable string matching the SQL primary key in `chunks`.
   - `doc_id` — ID of the parent document.
   - `chunk_index` — integer, ordered per document (0‑based or 1‑based; match SQL).
   - `section_path` — string like `"1/1.2/3"` or similar hierarchy path from Docling.
   - `text` — verbatim chunk text from Docling.
2. Document invariants:
   - For any given `doc_id`, chunks are in strictly increasing `chunk_index` order.
   - `text` is exactly the same as stored in SQL for the corresponding chunk row (no post‑processing that would break substring evidence checks).
3. Add small helper/constructor:
   - `Chunk.from_docling(node, doc_id, index)` to encapsulate mapping from Docling nodes to internal `Chunk`.

---

### 2. Implement `docling_chunk(document) -> List[Chunk]`

Goal: take a raw document (file/path/blob) and produce ordered semantic chunks.

1. Define function signature:
   - Input: `doc_id` and either:
     - The raw file bytes or path, **or**
     - A pre‑parsed Docling representation if parsing is done earlier.
   - Output: ordered `List[Chunk]`.
2. Implementation steps:
   - Step 1: Run Docling parsing:
     - Call Docling with the document input to obtain its structured representation (sections, headings, paragraphs, tables, etc.).
   - Step 2: Map Docling output to chunks:
     - Decide a consistent chunking strategy (e.g., semantic paragraphs/sections, keeping Docling defaults).
     - For each segment, build a `Chunk` instance:
       - Assign `chunk_index` sequentially (starting from 0 or 1, consistently).
       - Derive `section_path` from Docling’s section hierarchy.
       - Use verbatim text for `text`.
   - Step 3: Generate or reuse `chunk_id`:
     - If chunks are inserted into SQL in the same function:
       - Insert them first and use returned primary keys as `chunk_id`.
     - Otherwise, use a deterministic ID scheme (for example, `uuid5(doc_id, f"chunk-{index}")`) and re‑use it consistently.
3. Persist chunks:
   - Insert each chunk into the `chunks` table with:
     - `doc_id`, `chunk_index`, `section_path`, `text` (and `chunk_id` if not auto‑generated).
   - Decide whether this happens in the same transaction as document ingestion or as a separate step.
4. Optional: Qdrant `raw_chunks` points (if you want them in this phase):
   - For each chunk, compute an embedding of `text` (plus `section_path`).
   - Upsert a point into `raw_chunks` Qdrant collection with:
     - `point_id = chunk_id`.
     - Payload including `doc_id`, `chunk_index`, `section_path`, `text_preview`, etc.
   - This can be stubbed out now and fully implemented in Phase 4 if preferred.

---

### 3. Design the chunk‑marked document format

Goal: define a text format that is stable, easy for the LLM to parse, and easy to reverse‑map back to chunks.

The format should follow section 4.1 of `plan.md`:

1. **Header section**
   - Literal header line: `# DOC_META`.
   - Metadata lines:
     - `doc_id: <UUID>`
     - `title: <optional>` (if available from `documents` table).
     - `source: <path/url optional>` (if available).

2. **Chunks section**
   - Literal header line: `# CHUNKS (ordered)`.
   - For each chunk in order of `chunk_index`:
     - Opening marker on its own line:
       - `<<<CHUNK id="<chunk_id>" index=<chunk_index> section_path="<a/b/c>">>>`
     - The chunk’s verbatim text `text` on subsequent lines (may span multiple lines).
     - Closing marker on its own line:
       - `<<<END_CHUNK>>>`

3. **Section path encoding**
   - Define a stable encoding for `section_path`:
     - Simple slash‑separated string is acceptable, e.g., `"1/1.2/3"`.
   - Ensure character set is safe for inclusion in quotes:
     - If necessary, escape `"` characters or restrict to a safe subset.

4. **No internal transformation of text**
   - Do not trim or normalize whitespace inside chunk texts; evidence substring checks must work on the raw stored `text`.

---

### 4. Implement `build_marked_doc(doc_id) -> str`

Goal: build a single marked document string for a given `doc_id` using chunk metadata and text from SQL.

1. Define function signature:
   - `build_marked_doc(doc_id: UUID) -> str`.
2. Implementation steps:
   - Step 1: Load document metadata:
     - Query `documents` for title and source (if available).
   - Step 2: Load all chunks:
     - Query `chunks` for the given `doc_id`, ordered by `chunk_index`.
     - Map to the `Chunk` structure if needed.
   - Step 3: Stream‑build the marked document:
     - Start with header section lines (`# DOC_META`, metadata).
     - Add `# CHUNKS (ordered)`.
     - For each chunk:
       - Append opening `CHUNK` marker line.
       - Append its text exactly as stored (no modification).
       - Append `<<<END_CHUNK>>>`.
   - Step 4: Return the concatenated string.
     - Use a streaming or incremental approach if memory is a concern, but final output to caller is a single `str`.

3. Invariants to enforce:
   - `chunk_id` and `chunk_index` in markers exactly match SQL values.
   - Chunks appear strictly in `chunk_index` order.
   - There is exactly one `CHUNK` / `END_CHUNK` pair per stored chunk.

---

### 5. Implement `size_guard(marked_doc) -> List[DocPart]`

Goal: enforce an upper bound on the size of the string sent to the big LLM, while preserving stable chunk IDs and indices.

1. Define a `DocPart` structure:
   - `part_id` — string like `"doc::<doc_id>::part::<n>"`.
   - `doc_id` — the parent document ID.
   - `part_index` — 0‑based or 1‑based index of the part.
   - `text` — the marked document fragment text for this part.
   - `first_chunk_index` — index of the first chunk in this part.
   - `last_chunk_index` — index of the last chunk in this part.

2. Decide size limit and splitting strategy:
   - Size limit: e.g., 2 MB of UTF‑8 bytes per part (configurable constant).
   - Splitting strategy:
     - Prefer splitting on **chunk boundaries**, not raw characters.
     - Use windows of `N` chunks per part (for example, 30 chunks) with overlap of 1–2 chunks between adjacent parts.

3. Implementation approach A (simple, chunk‑driven):
   - Instead of splitting an already built `marked_doc` string:
     - Work from the list of chunks.
   - Steps:
     - Group chunks into ranges (`[0..29]`, `[28..57]`, etc.) such that:
       - Each range’s rendered marked‑doc text size ≤ limit.
       - Adjacent ranges share 1–2 overlapping chunk indices.
     - For each range, call a helper (like `build_marked_doc_for_range(doc_id, chunks[range])`) to build `DocPart.text`.
     - Create `DocPart` objects with chunk index ranges and part indices.

4. Implementation approach B (string‑driven, if needed):
   - Build a single `marked_doc` string and:
     - Track offsets of chunk markers while building.
     - Use those offsets to split the string into segments ≤ limit, at marker boundaries.
   - This is more complex and usually unnecessary if you can work from `Chunk` lists.

5. Determinism requirements:
   - Given the same `doc_id`, chunk list, and size limit:
     - The resulting list of `DocPart` objects must be identical across runs (same `part_index`, same chunk ranges, same `text` bytes).

6. Return value:
   - If `marked_doc` size ≤ limit:
     - Return a single `DocPart` with the entire document.
   - Otherwise:
     - Return a list of `DocPart`s covering all chunks with configured overlaps.

---

### 6. Unit tests

Goal: verify that chunk markers are correct, size splitting is stable, and splitting does not alter chunk IDs or indices.

1. **Round‑trip markers → chunks mapping**
   - Build a synthetic document with 3–5 chunks and known IDs and indices.
   - Call `build_marked_doc(doc_id)`:
     - Parse the resulting string by finding `<<<CHUNK ...>>>` / `<<<END_CHUNK>>>` markers.
     - Verify:
       - Extracted `chunk_id` and `chunk_index` for each marker match the original chunk list.
       - Number of chunks matches.

2. **Size splitting behavior**
   - Configure a small size limit (for example, 1 KB) to force multiple parts.
   - Run the splitting logic to get `DocPart[]`.
   - Verify:
     - All chunks are covered by at least one part.
     - Overlaps exist as configured (e.g., 1–2 chunk indices overlap between adjacent parts).
     - There is no re‑numbering or loss of `chunk_id` or `chunk_index`.

3. **Stability / determinism**
   - Run `build_marked_doc` and `size_guard` twice with identical inputs.
   - Verify:
     - The produced marked document strings are byte‑for‑byte identical.
     - The sequence of `DocPart` objects (indices, ranges, and texts) is identical.

4. **Integration sanity check**
   - For a small realistic document:
     - Ingest it via Docling → `docling_chunk`.
     - Persist chunks to SQL.
     - Run `build_marked_doc` and `size_guard`.
   - Confirm:
     - The LLM input artifact(s) look correct and human‑readable.
     - Chunk markers reference real `chunk_id`s stored in the database.

Once this phase is complete, you will have a reliable way to transform any ingested document into one or more chunk‑marked text artifacts suitable for the big LLM extraction step in Phase 3.

