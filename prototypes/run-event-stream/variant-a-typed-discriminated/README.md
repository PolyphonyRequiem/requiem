# Variant A — typed discriminated union

One pydantic model per event kind. A `TypeAdapter` over the
`Annotated[Union[...], Field(discriminator="event_type")]` is the
single entry point. Strong typing all the way from producer to projection.

## Files

| File | Purpose |
|------|---------|
| `outcomes.py` | Verb-outcome discriminated union (Stravinsky's shape). Carried inline as `VerbCompleted.outcome`. |
| `events.py`   | Per-kind models, the v1 and v2 unions, `parse_v1` / `parse_v2`. |
| `writer.py`   | Append-only line-buffered writer with optional fsync. |
| `reader.py`   | `read_all` + `tail` generators. Surfaces `CorruptLine` rather than skipping. |
| `state.py`    | `derive(events) -> RunState`; halts on `CorruptLine`. |
| `demo.py`     | End-to-end exercise of all 6 required behaviours. |

## Run

```
pip install -r ../requirements.txt
python demo.py
```

## Schema-evolution model

- **Adding a kind** = add a model + extend the union + bump `SCHEMA_VERSION`.
  An old reader sees an unknown discriminator and returns an `UnknownEvent`
  sentinel; it is never silently lost.
- **Adding a field to an existing kind** = `extra="forbid"` makes additive
  fields a breaking change for the *writer*, not the reader. Two options:
  (a) bump the kind to `node_entered_v2` (most conservative); (b) relax to
  `extra="ignore"` per-kind once the shape stabilises. Phase-A default is
  `forbid` so drift surfaces immediately.

## Corruption handling

`reader._parse_line` distinguishes:

- **JSONDecodeError** → `CorruptLine(error="json_decode: …")`
- **Unknown discriminator** → `UnknownEvent` (forward-compat, not corruption)
- **Schema violation inside a known kind** → `CorruptLine(error="schema: …")`

`derive` raises `CorruptionDetected` on any `CorruptLine`; per
INV-NO-CORRUPT-FORWARD the projection refuses to advance.
