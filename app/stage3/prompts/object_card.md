# Stage‑3 Object Card

You are given evidence excerpts from a document about a single **OBJECT** (an entity that can have states or be acted upon: document, payload, request, etc.). The object's canonical name is provided; do not invent a new name or ID.

**Task:** Write a short card that summarizes what this object is and how it is used, based only on the evidence.

**Output a single JSON object:**
- **summary** (required): 3–10 sentences describing the object and its role. Use the document language.
- **possible_states** (optional): list of human-readable states this object might have (e.g. "draft", "validated", "archived").
- **notes** (optional): any caveats or clarifications.

**Example:**
```json
{
  "summary": "The document is the main artifact that users submit. It can be in various formats and goes through validation.",
  "possible_states": ["draft", "submitted", "validated", "rejected"],
  "notes": null
}
```

---

## Evidence (snippet windows from the document)

<EVIDENCE_WINDOWS>

---

## Canonical object (from system; do not change)

<CANONICAL_BLOCK>
