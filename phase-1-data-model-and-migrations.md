# Phase 1 — Data model and migrations

### Purpose

Define and implement the authoritative SQL data model for frames, mentions, relations, and canonical registries, including migrations, ORM models, and repository methods.

---

### Assumptions

- Existing tables like `documents`, `chunks`, `runs` / `extraction_runs` (or equivalent), and `llm_calls` already exist or have clear patterns to follow.
- There is an ORM layer (for example, SQLAlchemy, Prisma, TypeORM, etc.) and a migrations system already in place.
- The database is PostgreSQL or another SQL engine that supports enums, indexes, and constraints similar to those outlined in `plan.md`.

If any of these assumptions are wrong, adapt the steps but preserve the **roles, constraints, and invariants** described here.

---

### 1. Schema design recap

Goal: restate the required tables and core relationships before touching migrations.

1. List or sketch all required tables and their roles:
   - `extraction_runs` (or an extension of your existing `runs` table).
   - `frames`, `mentions`, `mention_evidence`, `relations`.
   - `canonicals`, `canonical_aliases`, `mention_to_canonical`, `canonical_merges`.
   - Optional `object_states` if you decide not to encode object states directly into `canonicals`.
2. For each table, identify:
   - Primary key shape (UUID vs string ID).
   - Foreign keys to existing tables (`documents`, `chunks`, etc.).
   - Unique constraints required for idempotency (for example, uniqueness of `frame_index` within a chunk).
3. Capture these relationships in a short diagram or comment‑only design note to reference while writing migrations.

---

### 2. Concrete table specifications

Goal: fully specify columns, types, and constraints for each new (or extended) table so migrations can be written mechanically.

For each table below, adapt types and naming to your ORM / migrations tool, but preserve semantics and constraints.

#### 2.1 `extraction_runs`

- `run_id` — UUID primary key.
- `run_kind` — enum with values:
  - `DOCLING_CHUNK`
  - `BIG_LLM_EXTRACT`
  - `CANONICALIZE_ACTORS`
  - `CANONICALIZE_OBJECTS`
  - `CANONICALIZE_OBJECT_STATES`
  - `CANONICALIZE_ACTIONS`
- `doc_id` — FK → `documents.doc_id`.
- `prompt_version` — string.
- `model_id` — string.
- `status` — enum `RUNNING | SUCCEEDED | FAILED`.
- `input_hash` — string, indexed; used for idempotent caching of runs.
- `stats_json` — JSON field for run‑level metrics.
- `created_at`, `updated_at` — timestamps.
- Recommended index / constraint:
  - Unique or partial index on (`run_kind`, `doc_id`, `input_hash`) if you want strict idempotency per input.

#### 2.2 `frames`

- `frame_id` — string or UUID primary key (stable, can embed `chunk_id` + local index).
- `doc_id` — FK → `documents.doc_id`.
- `chunk_id` — FK → `chunks.chunk_id`.
- `frame_index` — integer; index of the frame within the chunk.
- `frame_text` — TEXT, nullable.
- `section_path` — TEXT, nullable (optional copy of chunk section path).
- Unique constraint:
  - (`chunk_id`, `frame_index`) to prevent duplicates inside a chunk.

#### 2.3 `mentions`

- `mention_id` — string or UUID primary key.
- `frame_id` — FK → `frames.frame_id`.
- `doc_id` — FK → `documents.doc_id`.
- `chunk_id` — FK → `chunks.chunk_id`.
- `type` — enum `ACTOR | OBJECT | ACTION | STATE`.
- `fields_json` — JSONB (or equivalent) for type‑specific fields (`name`, `verb`, `state`, etc.).
- `epistemic` — enum `EXPLICIT | INFERRED`.
- `confidence` — numeric or enum for confidence level.
- `dedupe_key` — string, indexed (used for deterministic canonicalization).
- `created_run_id` — FK → `extraction_runs.run_id`.
- Indexes:
  - `dedupe_key`.
  - `type` (optional but recommended).

#### 2.4 `mention_evidence`

- `mention_id` — FK → `mentions.mention_id` (use as PK or unique).
- `snippet` — TEXT (verbatim substring from the originating chunk).
- `char_start` — integer, nullable.
- `char_end` — integer, nullable.
- `validation_status` — enum `PENDING | VALID | INVALID`.
- Unique constraint:
  - `mention_id` as primary key or a unique index to ensure single evidence record per mention.

#### 2.5 `relations`

- `relation_id` — UUID primary key.
- `frame_id` — FK → `frames.frame_id`.
- `src_mention_id` — FK → `mentions.mention_id`.
- `rel_type` — enum limited to allowed MVP values:
  - `ACTION_HAS_ACTOR`
  - `ACTION_HAS_OBJECT`
  - `OBJECT_HAS_STATE`
  - (optional later) `ACTION_HAS_PLACE`, `MENTION_REFERENCES_REF`
- `dst_mention_id` — FK → `mentions.mention_id`.
- Unique constraint:
  - (`src_mention_id`, `rel_type`, `dst_mention_id`) to avoid duplicate relations.

#### 2.6 `canonicals`

- `canonical_id` — UUID primary key.
- `canonical_type` — enum `ACTOR | OBJECT | ACTION | STATE`.
- `name` — TEXT (display name).
- `norm_name` — TEXT (normalized name, indexed).
- `superseded_by` — nullable FK → `canonicals.canonical_id` (for merges).
- `created_run_id` — FK → `extraction_runs.run_id`.
- Indexes:
  - (`canonical_type`, `norm_name`) for deterministic lookup.

#### 2.7 `canonical_aliases`

- `alias_id` — UUID primary key.
- `canonical_id` — FK → `canonicals.canonical_id`.
- `alias_text` — TEXT.
- `alias_norm` — TEXT.
- Constraints:
  - Unique (`canonical_id`, `alias_norm`) to avoid duplicate aliases per canonical.
  - Optionally, a global unique (`canonical_type`, `alias_norm`) if you want cross‑canonical uniqueness.

#### 2.8 `mention_to_canonical`

- `mention_id` — PK, FK → `mentions.mention_id`.
- `canonical_id` — FK → `canonicals.canonical_id`.
- `decision` — enum `ACCEPT | REJECT | PENDING`.
- `decided_by` — enum `RULE | LLM | HUMAN`.
- `run_id` — FK → `extraction_runs.run_id` (canonicalization run that made the decision).
- Constraint:
  - Primary key on `mention_id` to enforce a single final mapping per mention.

#### 2.9 `canonical_merges`

- `from_canonical_id` — FK → `canonicals.canonical_id`.
- `to_canonical_id` — FK → `canonicals.canonical_id`.
- `run_id` — FK → `extraction_runs.run_id`.
- `reason` — TEXT.
- `created_at`, `updated_at` — timestamps.
- Optional unique constraint:
  - (`from_canonical_id`, `to_canonical_id`) to avoid recording the same merge multiple times.

#### 2.10 `object_states` (optional but recommended)

If you do not encode object‑scoped states directly into `canonicals`, introduce an `object_states` table:

- `state_id` — UUID primary key.
- `canonical_object_id` — FK → `canonicals.canonical_id` where `canonical_type = OBJECT`.
- `state_name` — TEXT.
- `state_norm` — TEXT (normalized).
- Unique constraint:
  - (`canonical_object_id`, `state_norm`) so each object has a finite set of unique states.

---

### 3. Migrations plan

Goal: create the schema safely, respecting foreign keys and allowing rollback.

1. **Enum creation / extension**
   - Add or extend database enums:
     - `run_kind`.
     - `mention_type` (if separate from `mentions.type` enum).
     - `rel_type`.
     - `epistemic`.
     - `canonical_type`.
     - `decision`, `decided_by`.
     - Any `status` enums (`RUNNING`, `SUCCEEDED`, `FAILED`, etc.).
   - For PostgreSQL:
     - Use `CREATE TYPE ... AS ENUM` for new enums.
     - Use `ALTER TYPE ... ADD VALUE` for extending existing enums.

2. **Create tables in FK‑safe order**
   - Step 1: `extraction_runs` (depends only on `documents`).
   - Step 2: `frames`, `mentions`, `mention_evidence`, `relations`:
     - These reference `documents`, `chunks`, and `extraction_runs`.
   - Step 3: `canonicals` and `object_states`:
     - `canonicals` may reference `extraction_runs`.
     - `object_states` references `canonicals`.
   - Step 4: `canonical_aliases`, `mention_to_canonical`, `canonical_merges`:
     - These reference `canonicals`, `mentions`, and `extraction_runs`.

3. **Add indexes and unique constraints**
   - `frames`:
     - Unique (`chunk_id`, `frame_index`).
   - `mentions`:
     - Indexes on `dedupe_key` and `type`.
   - `canonicals`:
     - Index on (`canonical_type`, `norm_name`).
   - `canonical_aliases`:
     - Unique (`canonical_id`, `alias_norm`).
     - Optional global unique on (`alias_norm`, `canonical_id` or `canonical_type`).
   - `object_states`:
     - Unique (`canonical_object_id`, `state_norm`).
   - `mention_to_canonical`:
     - Primary key on `mention_id` (and implied unique).
   - Any additional performance indexes discovered later can be added in follow‑up migrations.

4. **Down / rollback migrations**
   - For each migration:
     - Provide a corresponding `down` step that:
       - Drops constraints that depend on a table before dropping the table.
       - Drops tables in reverse dependency order:
         - `canonical_merges`, `mention_to_canonical`, `canonical_aliases`, `object_states`, `canonicals`, `relations`, `mention_evidence`, `mentions`, `frames`, `extraction_runs`.
       - Only drops enums if safe (or leaves them in place for future reuse).

---

### 4. ORM models and relationships

Goal: mirror the SQL schema faithfully in ORM models and wire up relationships.

1. **Create ORM entities**
   - For each new table, create a model class following existing conventions (base classes, timestamp mixins, naming).
   - Ensure column types match migrations (UUID / string / JSON / enum).
2. **Define relationships**
   - `Frame`:
     - Has many `mentions`.
     - Has many `relations`.
   - `Mention`:
     - Belongs to `Frame`, `Document`, `Chunk`.
     - Has one `MentionEvidence`.
     - Has optional `MentionToCanonical`.
   - `Canonical`:
     - Has many `CanonicalAliases`.
     - Has many `ObjectStates` (if using a separate table).
     - Can be superseded by another `Canonical`.
   - `MentionToCanonical`:
     - Belongs to `Mention` and `Canonical`.
   - `CanonicalMerge`:
     - Belongs to `Canonical` (`from` and `to`) and to `ExtractionRun`.
3. **Add helper methods (optional but useful)**
   - `Canonical.add_alias(text)`:
     - Normalizes `text` to `alias_norm` and inserts a `CanonicalAlias`.
   - `Mention.get_evidence()`:
     - Returns the associated `MentionEvidence` if it exists.
   - Factory methods for creating `Frame` + nested `Mention` + `MentionEvidence` from a parsed JSON frame.

---

### 5. Repository layer methods

Goal: provide a clean, testable API for runs, frames, mentions, relations, and canonicals.

Implement repository or service methods similar to the following (adapt naming to your codebase):

1. **Runs**
   - `create_extraction_run(doc_id, run_kind, prompt_version, model_id, input_hash) -> run`:
     - Inserts a new `extraction_runs` row with `status = RUNNING`.
   - `complete_run_success(run_id, stats_json)`:
     - Sets `status = SUCCEEDED`, updates `stats_json` and timestamps.
   - `complete_run_failure(run_id, error_summary)`:
     - Sets `status = FAILED`, updates `stats_json` with error info.

2. **Frames / mentions / relations**
   - `bulk_insert_frames(run_id, frames: List[FrameCreate])`:
     - Inserts frames in bulk; returns created IDs.
   - `bulk_insert_mentions(run_id, mentions: List[MentionCreate])`.
   - `bulk_insert_mention_evidence(evidence_list: List[MentionEvidenceCreate])`.
   - `bulk_insert_relations(relations: List[RelationCreate])`.
   - Query helpers:
     - `get_mentions_by_doc_and_type(doc_id, type)`:
       - Returns mentions filtered by document and mention type.
     - `get_mentions_by_run(run_id)`:
       - Returns mentions produced by a specific extraction run.

3. **Canonicals and aliases**
   - `get_canonical_by_norm_name(canonical_type, norm_name) -> Optional[Canonical]`.
   - `get_canonical_by_alias(canonical_type, alias_norm) -> Optional[Canonical]`.
   - `create_canonical(canonical_type, name, norm_name, created_run_id) -> Canonical`.
   - `add_canonical_alias(canonical_id, alias_text)`:
     - Normalizes `alias_text` and inserts a `CanonicalAlias` if not present.

4. **Mention → canonical mapping and merges**
   - `link_mention_to_canonical(mention_id, canonical_id, decision, decided_by, run_id)`:
     - Inserts or updates `mention_to_canonical`.
   - `record_canonical_merge(from_id, to_id, run_id, reason)`:
     - Inserts into `canonical_merges` and updates `canonicals.superseded_by` where appropriate.

---

### 6. Testing strategy

Goal: ensure migrations are structurally correct and ORM layer behaves as expected.

1. **Migration tests / integration checks**
   - Apply all migrations on a fresh test database:
     - Verify all tables, columns, and constraints exist as expected.
   - Run a full `migrate up` and `migrate down` cycle (where supported):
     - Confirm the schema can be rolled back cleanly without orphaned FKs.
   - Attempt to insert:
     - Duplicate (`chunk_id`, `frame_index`) in `frames` and confirm the unique constraint rejects it.
     - Duplicate (`canonical_object_id`, `state_norm`) in `object_states` and confirm rejection.

2. **ORM tests**
   - Create a fake `Document` and `Chunk` row in the test database.
   - Create an `ExtractionRun` row.
   - Insert:
     - A couple of `Frame` rows for the chunk.
     - Several `Mention` rows linked to frames.
     - Corresponding `MentionEvidence` and `Relation` rows.
     - A few `Canonical` rows and `CanonicalAlias` entries.
     - `MentionToCanonical` and `CanonicalMerge` records.
   - Fetch back these records via repository methods and assert:
     - Relationships (`frame.mentions`, `mention.evidence`, `canonical.aliases`, etc.) work as expected.
     - Cascade behavior on deletes is correct according to your policy (soft delete vs hard delete, etc.).

3. **Idempotency checks**
   - Simulate re‑running the same extraction:
     - Use the same `input_hash` and ensure uniqueness/indexing either:
       - Prevents creating a conflicting second run, **or**
       - Allows a second run but does not break uniqueness constraints on frames/mentions if keys are reused.
   - Decide and document which behavior you want; adjust constraints accordingly.

Once this phase is complete, the database and ORM layer should be ready to support the big LLM extraction (Phase 3) and canonicalization pipeline (Phases 4–5) with strong idempotency and traceability guarantees.

