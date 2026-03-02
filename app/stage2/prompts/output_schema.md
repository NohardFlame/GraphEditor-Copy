# Stage‑2 Decision Output (JSON)

Output a **single JSON object**. The system sets `seed_claim_id`, `pass_kind`, and (when you choose MERGE_INTO) `canonical_claim_id` from the context; it also resolves evidence IDs from your snippets. You never output those.

**Required from you, MUST PROVIDE:**
- **decision.kind** — `"ACCEPT_AS_CANONICAL"` | `"MERGE_INTO"` | `"REJECT"`
- **decision.new_entity_reason** — reason on why new claim has been accepted (null if MERGE_INTO is chosen)

**decision.kind explanation:**
- ACCEPT_AS_CANONICAL: seed claim seems valid based on provided evidence.
- MERGE_INTO: only when a **Closest canonical** block is present in the context. Seed claim seems valid but is the same entity as that canonical; the system will set `canonical_claim_id` for you. If no Closest canonical block is in the context, do not use MERGE_INTO.
- REJECT: seed statement seems to be a hallucination; based on the provided chunk/neighbors, the seed does not fit as a separate entity.

**Optional:**
- **attachments** — for **ACTION** pass only: optionally `attachments.action_endpoints.actor_claim_id`, `object_claim_id` when you can link to canonical ACTOR/OBJECT claims from context.
- **attachments** — for **ACTION** pass only: optionally `attachments.state_endpoints.object_claim_id`, when you can link to canonical OBJECT claims from context.

**Minimal JSON example:**
```json
{
  "decision": { "kind": <desision here>,
    "new_entity_reason": <reason here>
  }
}
```

With optional attachments (for action and state only):
```json
{
  "decision": {
    "kind": "MERGE_INTO",
  },
  "attachments": {
    "action_endpoints": { "actor_claim_id": null, "object_claim_id": null },
    "state_endpoints": { "object_claim_id": null }
  }
}
```

Omit or null optional fields when not needed.
