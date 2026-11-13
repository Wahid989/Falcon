# Scope Boundaries (FALCON-082)

What FALCON deliberately does **not** do this term. Documenting the boundary
protects against scope creep and tells the sponsor and any future team exactly
where v1 stops.

## Explicitly out of scope

FALCON v1 does not implement any of the following. These are not tickets and no
implementation work is planned for them this term:

- Physical drone / robot integration, sensors, real navigation
- Real-world terrain sensing and autonomous route generation
- LLM integration and open-ended natural-language negotiation
- **Dynamic group splitting** (see below)
- Production deployment
- ZeroMQ and MQTT transports

ZeroMQ and MQTT remain **documented future scaling paths** behind the transport
interface, not development work, unless the sponsor later changes scope.

## Group splitting — not implemented; `chain_id` reserved

FALCON does **not** implement dynamic group splitting (a chain dividing into
independent sub-chains at runtime). v1 operates as a **single root chain**.

The protocol reserves `chain_id` as the future, backward-compatible way to
distinguish independent chains. Per the versioning strategy
([versioning.md](versioning.md)), adding `chain_id` would be a **MINOR**,
backward-compatible change: v1 agents default to one root chain and neither emit
nor require the field. This keeps the door open for a later team to add group
splitting without breaking v1 clients.

## Where this is reflected

- The hierarchy is a single chain of arbitrary depth, one commander per level
  ([hierarchy-model.md](hierarchy-model.md)).
- The message envelope carries no `chain_id` in v1 ([protocol.md](protocol.md)).
- The versioning rules record `chain_id` as a reserved future addition
  ([versioning.md](versioning.md)).
