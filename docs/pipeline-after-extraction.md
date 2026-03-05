# Continuing the pipeline after extraction

After **big-LLM extraction** you have **frames**, **mentions**, and **relations** in the DB. The next step is **canonicalization**: deduplication, merging, and mapping mentions to canonical entities (actors, objects, actions, object states) using **Qdrant** (vector search) and a **local LLM** (SAME / DIFFERENT / UNSURE comparator).

---

## What canonicalization does

1. **Deterministic matching** – Mentions with the same `dedupe_key` are grouped; exact match on canonical `norm_name` can link without LLM.
2. **Qdrant retrieval** – For each mention (or group), candidate canonicals are fetched by embedding similarity from the right collection (`canonical_actors`, `canonical_objects`, `canonical_actions`, `canonical_object_states`).
3. **Tournament (local LLM)** – When there are multiple candidates or no deterministic match, the comparator LLM is called to decide SAME / DIFFERENT / UNSURE between mention and candidate(s). The tournament either links to an existing canonical or creates a new one.
4. **Order** – Actors → Objects → Object states → Actions (actions depend on actor/object canonicals).

Result: **canonicals** and **canonical_aliases** in SQL, **mention_to_canonical** links, and Qdrant collections populated for future retrieval.

---

## Prerequisites

- **DB** – Extraction already run for the document (frames, mentions, relations present).
- **Qdrant** – Running and reachable (default `http://localhost:6333`). Set `QDRANT_URL` if different.
- **Ollama** (recommended for local):
  - **Embedding model** – e.g. `nomic-embed-text` (768 dims). Set `EMBED_OLLAMA_MODEL` and ensure `EMBED_DIMS=768` (default).
  - **Chat model** – For the comparator (SAME/DIFFERENT/UNSURE). Set `LLM_OLLAMA_MODEL` (e.g. `ollama/llama3.2`).
- **LLM settings** – If you use **only Ollama** (no Gemini), set in `.env`:
  - `LLM_GEMINI_ENABLED=false`
  Otherwise the app may require `LLM_GEMINI_API_KEY`.

---

## One-shot: run full canonicalization for a document

```bash
python -m app.observability.cli run-canonicalization <doc_id>
```

This will:

1. **Bootstrap** – Ensure the four canonical Qdrant collections exist (vector size from `EMBED_DIMS`).
2. **Run in order** – `canonicalize_actors` → `canonicalize_objects` → `canonicalize_object_states` → `canonicalize_actions`.

Output is JSON with `doc_id` and `run_ids` (one per step):

```json
{
  "doc_id": "b013a960-d76a-42ce-8f26-9c7c04b690d0",
  "run_ids": {
    "CANONICALIZE_ACTORS": "uuid-1",
    "CANONICALIZE_OBJECTS": "uuid-2",
    "CANONICALIZE_OBJECT_STATES": "uuid-3",
    "CANONICALIZE_ACTIONS": "uuid-4"
  }
}
```

---

## Environment summary

| Variable | Purpose |
|----------|--------|
| `QDRANT_URL` | Qdrant server (default `http://localhost:6333`) |
| `EMBED_OLLAMA_MODEL` | Embedding model (default `nomic-embed-text`) |
| `EMBED_DIMS` | Vector size (default `768` for nomic-embed-text) |
| `EMBED_OLLAMA_API_BASE` | Ollama URL for embed (default `http://localhost:11434`) |
| `LLM_OLLAMA_MODEL` | Chat model for comparator (e.g. `ollama/llama3.2`) |
| `LLM_GEMINI_ENABLED` | Set `false` for local-only (Ollama) |

---

## Inspecting results

- **Document runs and stats:**  
  `python -m app.observability.cli doc-debug <doc_id>`
- **Chunk with frames/mentions and canonical mappings:**  
  `python -m app.observability.cli chunk-view <doc_id> <chunk_index>`
- **Mention and its canonical:**  
  `python -m app.observability.cli mention-view <mention_id>`
- **Canonical and linked mentions:**  
  `python -m app.observability.cli canonical-view <canonical_id>`

---

## Pipeline order (full flow)

1. **DB** – `alembic upgrade head`
2. **Ingest file** – `ingest-file <path>` → get `doc_id`
3. **Extraction** – Either:
   - **Manual:** `export-for-llm` → run LLM yourself → `ingest-results`
   - **Automated:** `run-extract <doc_id>`
4. **Canonicalization** – `run-canonicalization <doc_id>` (Qdrant + local LLM)

After that you can query canonicals, mention-to-canonical links, and use Qdrant for search or downstream features.
