# Stage‑3 State Card

You are given evidence excerpts about a **STATE** (a condition or status that an object can be in). You are also given **candidate objects** — objects that might have this state. Use only the evidence and the candidate object summaries to decide.

**Task:** Write a short card that summarizes this state and, if clear from evidence, which object(s) it applies to. You may list zero or more **candidate object IDs** from the list below (use the exact IDs provided).

**Output a single JSON object:**
- **summary** (required): 1–5 sentences describing the state and, if relevant, which object(s) it applies to.
- **candidate_objects** (optional): list of object IDs from the candidate objects block (use the exact `object_id` values). Omit or empty if unclear.
- **notes** (optional): any caveats.

**Example:**
```json
{
  "summary": "Validated means the document has been checked and approved by an admin.",
  "candidate_objects": ["claim-uuid-1", "claim-uuid-2"],
  "notes": null
}
```

---

## Evidence (snippet windows from the document)

<EVIDENCE_WINDOWS>

---

## Candidate objects (object_id and summary; you may reference object_id in candidate_objects)

<CANDIDATE_OBJECTS>
