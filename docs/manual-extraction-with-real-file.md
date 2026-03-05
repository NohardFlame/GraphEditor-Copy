# Manual extraction with a real file and your own LLM

Use the **manual path**: ingest your file into the DB, export prompts and doc parts, run the big LLM yourself (by hand), then ingest the results back.

## 1. Set up the database

From the project root:

```bash
# Create DB directory if using default SQLite (optional)
mkdir -p data

# Run migrations (creates workspaces, sources, source_versions, documents, chunks, extraction_runs, etc.)
alembic upgrade head
```

Database URL defaults to `sqlite:///./data/app.db`. Override with `DB_DB_URL` in env or `.env` (e.g. `DB_DB_URL=sqlite:///C:/path/to/app.db`).

## 2. Ingest your file

This creates a **workspace** (default name `manual`), a **source**, a **source version**, a **document**, and **chunks** (via Docling):

```bash
python -m app.observability.cli ingest-file "C:\path\to\your\file.pdf"
# Or with a custom workspace:
python -m app.observability.cli ingest-file "C:\path\to\your\file.pdf" --workspace my-workspace
```

Output is JSON with `doc_id`. **Save this `doc_id`** for the next step.

Example output:
```json
{"doc_id": "a1b2c3d4-...", "path": "C:\\path\\to\\your\\file.pdf"}
```

## 3. Export for manual LLM run

This creates an **extraction run** and writes one file per “part” (each file has the SYSTEM and USER prompt text), plus a manifest:

```bash
python -m app.observability.cli export-for-llm <doc_id> ./my_export_dir --model manual
```

Example output:
```json
{
  "run_id": "e5f6a7b8-...",
  "doc_id": "a1b2c3d4-...",
  "model": "manual",
  "manifest": "C:\\...\\my_export_dir\\manifest.json",
  "parts": ["C:\\...\\my_export_dir\\part_0.txt", "C:\\...\\my_export_dir\\part_1.txt"]
}
```

**Save the `run_id`** for step 5.

In `my_export_dir` you get:

- `part_0.txt`, `part_1.txt`, … — each file contains:
  - `=== SYSTEM ===` … system prompt …
  - `=== USER ===` … user prompt (chunk-marked document) …
- `manifest.json` — run_id, doc_id, model, list of part files.

## 4. Run the LLM yourself

For each `part_N.txt`:

1. Open the file and copy the **SYSTEM** block (from `=== SYSTEM ===` to just before `=== USER ===`).
2. Copy the **USER** block (from `=== USER ===` to the end).
3. Call your LLM (e.g. OpenAI, Claude, Ollama UI, API script) with that system + user content.
4. The model must return **only valid JSON** in the schema expected by the pipeline (see below).
5. Save the **raw JSON response** to `part_N.json` in the **same directory** as the export (or in a new directory you’ll pass to ingest-results).

Schema for each part response (one JSON object per part):

- Top-level: `doc_id`, `prompt_version` (e.g. `"big_extract_v1"`), `frames`, `references`, `errors`.
- Each frame: `frame_id`, `chunk_id`, `frame_index`, `frame_text`, `mentions`, `relations`.
- Each mention: `mention_id`, `type` (e.g. ACTOR, OBJECT, ACTION, STATE), `fields`, `epistemic`, `evidence` (must contain `snippet` that is a **verbatim substring** of the chunk text), `confidence`.
- Each relation: `src_mention_id`, `rel_type`, `dst_mention_id`.

If you have one part, you get one JSON file: `part_0.json`. If you have three parts, you need `part_0.json`, `part_1.json`, `part_2.json`.

## 5. Ingest the results

Point the CLI at the **directory** that contains `part_0.json`, `part_1.json`, … (or at a single file that contains a JSON array of part responses in order):

```bash
python -m app.observability.cli ingest-results <run_id> ./my_export_dir
# Or a directory with only the JSON files:
python -m app.observability.cli ingest-results <run_id> ./results
```

The pipeline will:

- Read the part JSONs.
- Parse and validate (including evidence substring checks).
- Persist frames, mentions, relations, and run stats.
- Set the extraction run status to `SUCCEEDED` or `FAILED`.

You can then use **doc-debug** and **chunk-view** to inspect:

```bash
python -m app.observability.cli doc-debug <doc_id>
python -m app.observability.cli chunk-view <doc_id> 0
```

## Reusing saved JSON (same file)

To reuse a **saved** big-extract JSON for a file without re-running the LLM (e.g. costly run stored in `my_extractions`):

```bash
python -m app.observability.cli ingest-file-and-json "d:\path\to\large_tz.md" "d:\path\to\my_extractions\part_0.json"
```

This ingests the file (Docling chunks), creates a run, **remaps** `doc_id` and `chunk_id` in the JSON to the new document by position (Docling is deterministic), and runs the pipeline. **json_path** can be a single file (one part or JSON array of parts) or a directory with `part_0.json`, `part_1.json`, ...

## Summary

| Step | Command | What you do |
|------|---------|-------------|
| 1 | `alembic upgrade head` | Create DB and tables |
| 2 | `ingest-file <path>` | Get `doc_id` |
| 3 | `export-for-llm <doc_id> <output_dir>` | Get `run_id`, part files |
| 4 | (by hand) | Run LLM per part, save `part_N.json` |
| 5 | `ingest-results <run_id> <path>` | Load results into DB |

All CLI commands use the app’s DB (via `DB_DB_URL` / default SQLite path). The same DB is used for ingest, export, and ingest-results.
