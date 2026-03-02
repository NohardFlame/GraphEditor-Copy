# Stage‑3 Action Card

You are given evidence excerpts about an **ACTION** (who does what to what). You are also given **candidate actors** and **candidate objects** (with their baked state pools) so you can ground the action. Use only the evidence and the candidates to decide.

**Task:** Write a short card that summarizes what happens in this action and, if clear, which actors/objects are involved. You may list **candidate_actors** and **candidate_objects** by their IDs. Optionally describe preconditions or effects (prefer referencing state names from the object state pools when possible).

**Output a single JSON object:**
- **summary** (required): 3–10 sentences describing the action in plain language.
- **candidate_actors** (optional): list of actor IDs from the candidate block.
- **candidate_objects** (optional): list of object IDs from the candidate block.
- **preconditions** (optional): string or list of conditions that must hold before the action (e.g. "document is in draft").
- **effects** (optional): string or list of outcomes (e.g. "document state becomes validated").
- **notes** (optional): any caveats.

**Example:**
```json
{
  "summary": "The user submits a document for validation. The document moves from draft to submitted.",
  "candidate_actors": ["actor-claim-id"],
  "candidate_objects": ["object-claim-id"],
  "preconditions": "Document is in draft state",
  "effects": "Document state becomes submitted",
  "notes": null
}
```

---

## Evidence (snippet windows from the document)

<EVIDENCE_WINDOWS>

---

## Candidate actors (actor_id and summary)

<CANDIDATE_ACTORS>

---

## Candidate objects (object_id, summary, and baked states)

<CANDIDATE_OBJECTS>
