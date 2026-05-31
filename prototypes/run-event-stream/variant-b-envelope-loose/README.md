# Variant B — envelope + loose payload

One on-disk model: `Event(event_id, run_id, ts, kind, schema_version,
node_path, payload: dict)`. Per-kind payload validation is **opt-in** on the
consumer side via a registry of pydantic models.

## Files

| File | Purpose |
|------|---------|
| `events.py` | `Event` envelope + per-kind `*P` payload models + `PAYLOAD_REGISTRY_V1` / `_V2` + `make_typed_parser`. |
| `writer.py` / `reader.py` / `state.py` | Same shape as variant A. |
| `demo.py`   | End-to-end exercise of all 6 required behaviours. |

## Schema-evolution model

- **Adding a kind** = register a payload class in the consumer that cares.
  Old consumers see `TypedEvent(known=False)` with the raw payload intact;
  they can still order it by `event_id` and write it through to a downstream
  store. No discriminator gymnastics on the wire.
- **Adding a field to an existing payload** = additive change in the payload
  model. With `extra="ignore"` (Phase B candidate) it is fully backwards-
  compatible. We keep `extra="forbid"` here to surface drift in the demo.

## Corruption vs forward-compat

- *Envelope* corruption → `CorruptLine` → `derive` halts.
- *Payload* corruption inside a registered kind → `CorruptLine` → halt.
- *Unknown kind* → `TypedEvent(known=False)` recorded as
  `unknown_kinds_seen`. Not corruption.

## Why this shape is interesting

- Mixed-version cohorts (engine vN, UI backend vN-1, harness vN+1) work
  without coordinated deploys.
- The 20-signal domain envelope fits as a single kind `domain_signal`
  carried in the *same* file — see seam README §"Domain signals".
- `jq '. | select(.kind == "verb_completed")'` is the natural CLI lens.
