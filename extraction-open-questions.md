# Extraction pipeline — open questions and clarifications

This file collects questions and clarifications that will help fine‑tune the implementation of the extraction and canonicalization pipeline. None of these block implementation, but answers will allow us to lock in better defaults and reduce future refactors.

---

### 1. Infrastructure and tech stack

- **Database engine**  
  - Which SQL engine is authoritative in production (PostgreSQL, MySQL, SQLite, other)?  
  - Are there any restrictions on using enums, JSON/JSONB, or advanced indexing features?

- **ORM and migrations tooling**  
  - Which ORM / data access layer is used (e.g., SQLAlchemy, Prisma, TypeORM, custom)?  
  - What is the standard migrations tool in this repo (Alembic, Prisma Migrate, Flyway, custom)?  
  - Are there established conventions for naming tables/columns and enums that we should follow?

---

### 2. LLM and embedding details

- **Big LLM provider**  
  - Which provider/model family is preferred for the big extraction run (OpenAI, Anthropic, Gemini, etc.)?  
  - Any hard token/size limits we should target beyond the 2 MB text guideline?

- **Local LLM for tournament**  
  - Which local model (or API) will be used for SAME/DIFFERENT/UNSURE?  
  - What are the latency and context constraints we should design for (e.g., max prompt size, throughput expectations)?

- **Embedding model(s)**  
  - Will the same embedding model be used for chunks and canonical entities, or do you prefer a dedicated identity‑focused model for canonicals?  
  - What is the expected embedding dimension so we can lock in Qdrant collection configuration?

---

### 3. Deployment and performance expectations

- **Throughput and scale**  
  - Rough target volume: how many documents per day and typical document size?  
  - Expected latency budget for a full pipeline run on a single document (ingestion → extraction → canonicalization)?

- **Retry and failure behavior**  
  - For partially failed runs (e.g., one LLM call fails or one part’s JSON is invalid), should we:  
    - Fail the entire run and require a manual rerun, or  
    - Retry only failed parts automatically up to N times?

---

### 4. Canonicalization policy

- **Global vs per‑document canonicals**  
  - Should canonical registries be global across *all* documents, or is there any need for per‑tenant / per‑domain scoping?  
  - If multi‑tenant: how should tenant context be encoded in SQL and Qdrant payloads?

- **Manual overrides**  
  - Is there a requirement for a UI or admin API to manually override canonicalization (e.g., force‑merge two canonicals, or pin a mention to a specific canonical)?  
  - If yes, what is the desired audit trail format for human interventions?

---

### 5. Observability and APIs

- **Preferred interface for debugging**  
  - Should debug surfaces (like `doc_debug`, `chunk_view`, etc.) be implemented as:  
    - CLI commands only,  
    - HTTP/JSON endpoints, or  
    - Both?  
  - Are there existing admin/debug endpoints we should conform to (URL structure, auth, etc.)?

- **Metrics and monitoring stack**  
  - Which metrics/monitoring system is in use (Prometheus, OpenTelemetry, custom)?  
  - Do you want key counters/gauges for LLM usage and canonicalization decisions exported there in addition to `stats_json` in SQL?

---

### 6. Idempotency and data retention

- **Rerun semantics**  
  - When re‑running big extraction on the same document and prompt version, should previous frames/mentions be:  
    - Preserved alongside new ones under different runs, or  
    - Replaced/overwritten to keep only the latest state?  
  - Similarly, for canonicalization reruns: should new runs be able to overwrite previous `mention_to_canonical` rows, or only fill gaps?

- **Retention of raw LLM outputs**  
  - How long should raw `llm_calls.request` / `llm_calls.response` data be kept (forever, N days, until compaction)?  
  - Are there storage or privacy constraints that require redaction or summarization of older LLM outputs?

---

You can answer these questions in any format (inline comments, a separate design note, or edits to this file). The implementation can proceed with the current defaults, but your answers will help finalize configuration and reduce future migration work.

