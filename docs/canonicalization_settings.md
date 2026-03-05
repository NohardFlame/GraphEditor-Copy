# Canonicalization settings

Canonicalization can use either an **LLM-based tournament** (default) or an **algorithmic thresholds** strategy for resolving ACTOR and OBJECT mentions. Settings are stored in the `canonicalization_settings` table and can be changed at runtime per workspace (or use a global default).

## Strategy: `dedup_strategy`

- **`LLM_TOURNAMENT`** (default)  
  Resolve mentions by deterministic `dedupe_key` first; for the rest, use Qdrant candidates and an LLM comparator to decide SAME/DIFFERENT. Best when you have a capable LLM.

- **`ALGO_THRESHOLDS`**  
  No LLM. Resolve by:
  1. Existing canonical for the same `dedupe_key`.
  2. If no canonical and mention count for that key ≥ `min_mentions_for_canonical` → create a new canonical (frequency-based).
  3. Otherwise use Qdrant cosine score vs nearest canonical:
     - score ≥ `cosine_high_merge_threshold` → link to that canonical;
     - score ≤ `cosine_low_new_threshold` → create new canonical;
     - in-between → create a “debated” canonical (new canonical merged into nearest, so it is suppressed in future search).

## Enabling algorithmic mode

Set `dedup_strategy` to `ALGO_THRESHOLDS` for the workspace (or for the global default row):

```sql
-- Per workspace (example workspace_id = 'your-workspace-id')
INSERT INTO canonicalization_settings (id, workspace_id, dedup_strategy, min_mentions_for_canonical, cosine_high_merge_threshold, cosine_low_new_threshold, created_at, updated_at)
VALUES (lower(hex(randomblob(16))), 'your-workspace-id', 'ALGO_THRESHOLDS', 5, 0.9, 0.2, datetime('now'), datetime('now'))
ON CONFLICT(workspace_id) DO UPDATE SET dedup_strategy = 'ALGO_THRESHOLDS';

-- Or create/update a global default (workspace_id = NULL) so all workspaces without their own row use it
INSERT INTO canonicalization_settings (id, workspace_id, dedup_strategy, min_mentions_for_canonical, cosine_high_merge_threshold, cosine_low_new_threshold, created_at, updated_at)
VALUES (lower(hex(randomblob(16))), NULL, 'ALGO_THRESHOLDS', 5, 0.9, 0.2, datetime('now'), datetime('now'));
```

If no row exists for a workspace, the code falls back to a global row (`workspace_id IS NULL`); if none exists, it creates one with default values (including `LLM_TOURNAMENT`).

## Tunable thresholds (all strategies)

| Setting | Default | Meaning |
|--------|---------|--------|
| `min_mentions_for_canonical` | 5 | In ALGO_THRESHOLDS: minimum number of mentions with the same `dedupe_key` to create a canonical from that key. |
| `cosine_high_merge_threshold` | 0.9 | In ALGO_THRESHOLDS: Qdrant cosine similarity above this → link mention group to the nearest canonical. |
| `cosine_low_new_threshold` | 0.2 | In ALGO_THRESHOLDS: Qdrant cosine below this → create a new canonical. Between low and high → create debated canonical. |

Tune these for your corpus and embedding model (e.g. stricter 0.95/0.15 or looser 0.85/0.25). Changes take effect on the next canonicalization run; no restart needed.

## Where settings are read

- **`canonicalize_actors`** and **`canonicalize_objects`** in `app/canonical/runners.py` resolve `workspace_id` from the document, then call `get_or_create_for_workspace(session, workspace_id)` and branch on `settings.dedup_strategy`.
- Object states and actions are resolved after actors/objects using the same `mention_to_canonical` links; they are not affected by this strategy choice except indirectly (better/worse actor/object resolution).

## Switching back to LLM

Set `dedup_strategy` back to `LLM_TOURNAMENT` for that workspace (or the global row). Next run will use the LLM comparator again for unresolved ACTOR/OBJECT groups.
