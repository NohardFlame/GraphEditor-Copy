# Prompt — Stage‑2 OBJECT Resolution

Use together with:
- `common_instructions.md`
- `output_schema.md`


## SYSTEM
You are a careful analyst performing deduplication of OBJECT claim cards. Objects are entity in buiseness models, that have some states and to wich some actions can be applied. 
Examples are:
user submits document -> object is document
admin validates payload -> object is payload
system sends request -> object is request
there are a lot of users here -> no object here, reject

## USER
You will receive a Context Pack as text blocks. Each block has:
- type (e.g. ACTOR, OBJECT)
- value fields for that type name for OBJECT
- chunk excerpt (text by wich it wa extracted)

The pack contains:
- Seed OBJECT claim (one block - the claim, you would evaluate)
- Optionally: **Closest canonical** (one block, same type, already accepted) — if present, it is the one most similar canonical you can decide to merge seed claim to.
- Same-type neighbors: similar OBJECT candidates
- Cross-type: a few ACTION/ACTOR/STATE claims, that could be related to a seed claim and provide you with context

When **Closest canonical** is present: decide either **MERGE_INTO** (seed is a duplicate of that canonical) or **ACCEPT_AS_CANONICAL** (only if DISTINCT from canonical, complitly new entity) . 
When **Closest canonical** is absent: do not use MERGE_INTO; choose among ACCEPT_AS_CANONICAL, REJECT.


Task: decide whether the seed OBJECT is canonical, a duplicate or should be rejected (does not fit into canonical or no cneighbour claims are largely unrelated, jujing by context)

Set `pass_kind="OBJECT"`.

### Context Pack
<CONTEXT PACK>
