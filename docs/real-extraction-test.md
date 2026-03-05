# Running a real extraction pipeline test

To run the extraction pipeline against a **real LLM** (Ollama or Gemini) instead of mocks:

## Prerequisites

1. **Database** with the extraction schema (Phase 1 migrations applied) and at least one **document that has chunks**.
2. **LLM**:
   - **Ollama**: run `ollama serve` and pull a model, e.g. `ollama pull llama3.2`. Set `LLM_OLLAMA_MODEL` if you use a different model (default `ollama/llama3.2`).
   - **Gemini**: set `LLM_GEMINI_API_KEY` and use model `gemini/gemini-2.0-flash` (or another `gemini/...` model).

## Option A: CLI (real DB + real document)

1. Ensure your app DB is configured (e.g. via `DATABASE_URL` or `DB_URL`).
2. Ensure the document exists and has chunks (e.g. from docling chunking or from your own seeding).
3. Run:

```bash
# Use default model (Ollama from env or ollama/llama3.2)
python -m app.observability.cli run-extract <doc_id>

# Explicit model and timeout
python -m app.observability.cli run-extract <doc_id> --model ollama/llama3.2 --timeout 120
```

Output is JSON with `run_id`, `doc_id`, and `model`. Check the DB for `extraction_runs`, `frames`, `mentions`, and `llm_calls`.

## Option B: Pytest with in-memory DB (real LLM only)

Uses the test fixture document `doc-big` (two chunks) and calls the real LLM. No real DB needed.

```bash
# Ollama (default model)
set E2E_LLM=1
pytest tests/test_big_extract_pipeline.py::test_run_big_llm_extract_real_llm -v

# Or with Gemini (set LLM_GEMINI_API_KEY)
set E2E_LLM=1
set LLM_OLLAMA_MODEL=gemini/gemini-2.0-flash
pytest tests/test_big_extract_pipeline.py::test_run_big_llm_extract_real_llm -v
```

The test asserts that the run completes with status `SUCCEEDED` or `FAILED` and that stats contain `frames_count` and `mentions_count_total`.

## Option C: Manual path (export → run LLM yourself → ingest)

If you prefer to run the LLM outside the app (e.g. in another env or UI):

1. **Prepare export** (creates `extraction_run`, writes prompt + doc parts and a manifest):

   Use the Python API: `prepare_big_llm_export(doc_id, model_id, output_dir=path, session=session)`. It returns `(run_id, list_of_part_files, manifest_path)`.

2. **Run the LLM** on each part file using the prompts from the export (or use `observability prompt-template big_extract_v1` for the template).

3. **Ingest results**: `ingest_big_llm_results(run_id, list_of_result_json_paths, session=session)`.

Existing tests cover this flow with canned JSON (see `test_ingest_big_llm_results_success`).

## Environment (for real LLM)

| Variable | Purpose |
|----------|--------|
| `LLM_OLLAMA_MODEL` | Model for CLI/pytest default (e.g. `ollama/llama3.2`) |
| `LLM_OLLAMA_API_BASE` | Ollama server URL (default `http://localhost:11434`) |
| `LLM_GEMINI_API_KEY` | Required when using a `gemini/...` model |

## Troubleshooting

- **"no chunks for document"**: The document has no rows in the chunks table. Chunk it first (e.g. via docling chunking) or use the pytest option (Option B) which uses fixture data.
- **LLM timeout**: Increase `--timeout` or set a larger value in the client (e.g. 600s for long documents).
- **Parse/validation errors**: Check `extraction_runs.stats_json` and `llm_calls.response_text` for the raw LLM output and validation messages.
