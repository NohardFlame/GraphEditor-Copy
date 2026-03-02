# Prompt — Stage‑2 STATE Resolution

Use together with:
- `common_instructions.md`
- `output_schema.md`



## SYSTEM
You are a careful analyst performing deduplication of STATE claim cards. STATEs are entity in buiseness models, that represent some stae of an object, they can be changed by an ACTIONS. 
Examples are:
user submits document -> STATE submited for document OBJECT
admin validates payload -> STATE is validated for payload OBJECT
system sends request -> STATE is sent for request OBJECT
there are a lot of users here -> no STATE here, reject

## USER
You will receive a Context Pack as text blocks. Each block has:
- type (e.g. ACTOR, STATE)
- value fields for that type name for STATE (name and the object this state is refered to)
- chunk excerpt (text by wich it wa extracted)

The pack contains:
- Seed STATE claim (one block - the claim, you would evaluate)
- Optionally: **Closest canonical** (one block, same type, already accepted) — if present, it is the one most similar canonical you can decide to merge seed claim to.
- Same-type neighbors: similar STATE candidates
- Cross-type: a few ACTION/ACTOR/OBJECT claims, that could be related to a seed claim and provide you with context

When **Closest canonical** is present: decide either **MERGE_INTO** (seed is a duplicate of that canonical) or **ACCEPT_AS_CANONICAL** (only if DISTINCT from canonical, complitly new entity) . 
When **Closest canonical** is absent: do not use MERGE_INTO; choose among ACCEPT_AS_CANONICAL, REJECT.

**How to deside, that STATE is new (in addition to other instructions)**:
- States can have similar name, but can be related to a different objects (judged by object field of given state)

also try to tie the seed STATE to one of OBJECT this state is related to, presented to you (by filling attachments.state_endpoints.object_claim_id with chosen object id)


Task: decide whether the seed STATE is canonical, a duplicate or should be rejected (does not fit into canonical or no cneighbour claims are largely unrelated, judging by context)


Set `pass_kind="STATE"`.

### Context Pack
<CONTEXT PACK>
