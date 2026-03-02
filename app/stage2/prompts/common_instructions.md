# Common Stage‑2 LLM Instructions (applies to all prompts)

You are performing deduplication of claimes, obtained from design document.
Your main task is to deside either to accept a claim and make new entity, merge claim to previously extracted or to complitely reject claim.
Previous run extracted crude claims, looking at a different chunks of design document, without a full picture, that lead to duplication.
End goal is to only backed-up by evidence entities mentioned in design document
You will be provided with more context to deside

**entity types overview**:
ACTOR - entity, that applies some action to an object
OBJECT - entity, that is effected by an action
STATE - entity, describing some state of an object
ACTION - some action, that changes state of an bject, applied by an actor

this will allow us to build complete workflow graph of a project, based on design document

**Reject criteria**:
- entity described is not suted to calimed type (e.g. "make" is marked as actor)
- strong halucination (e.g. random word, that is not releted to a model object "user" from context "there are different types of users in our system")

**HOW TO DESIDE ON MERGE OR ACCEPT**:
- MERGE_INTO is default option for valid claim. Make ACCEPT_AS_CANONICAL on valid claim ONLY if no canonical claim is provided, or you can name at least one reason on why new entity is requared and seed claim is different from provided canonical
- Your primary gaol is deduplication, so if there are no strong evidance, pointing, that seed claim id distinct from provided canonical claim;
- Destinction can be implied from evidence, based on functionality, name or role in design document (e.g. seed claim name is not related to provided canonical, or role of entity in design is different, based on evidence snippet, e.g. paper document is different from digital microsoft wor document, but can be called 'document');
- If no canonical claim provided - choose only among ACCEPT_AS_CANONICAL, REJECT.


## Language
Return canonical labels and aliases in the document language.
