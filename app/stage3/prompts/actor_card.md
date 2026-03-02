# Stage‑3 Actor Card

You are given evidence excerpts from a document about a single **ACTOR** (an entity that performs actions: user, admin, system, etc.). The actor's canonical name is provided; do not invent a new name or ID.

**Task:** Write a short card that summarizes who this actor is and what role they play, based only on the evidence.

**Output a single JSON object:**
- **summary** (required): 3–10 sentences describing the actor and their role. Use the document language.
- **possible_actions** (optional): list of short action descriptions this actor might perform (e.g. "submit document", "validate payload").
- **notes** (optional): any caveats or clarifications.

**Example:**
```json
{
  "summary": "The user is the person who interacts with the system to submit documents. They may have different roles depending on context.",
  "possible_actions": ["submit document", "upload file", "request validation"],
  "notes": null
}
```

---

## Evidence (snippet windows from the document)

<EVIDENCE_WINDOWS>

---

## Canonical actor (from system; do not change)

<CANONICAL_BLOCK>
