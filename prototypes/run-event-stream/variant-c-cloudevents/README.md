# Variant C — CloudEvents 1.0 envelope

On-disk record is a CloudEvents 1.0 *Structured Mode JSON* envelope
(spec: https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md).

```jsonc
{
  "specversion": "1.0",
  "type":        "io.requiem.verb.completed",
  "source":      "/requiem/engine/run/<run_id>",
  "id":          "<run_id>:<event_id>",   // unique within source
  "time":        "2026-05-31T22:00:00Z",
  "datacontenttype": "application/json",
  "dataschema":  "https://requiem.dev/schemas/io/requiem/verb/completed/v1.json",
  "data":        { ...typed payload... }
}
```

## Files

| File | Purpose |
|------|---------|
| `events.py`   | CE envelope (`CloudEvent`), per-type body models, `TYPE_REGISTRY_V1` / `_V2`, `make_parser`. |
| `writer.py` / `reader.py` / `state.py` | Same shape as A/B. |
| `demo.py`     | End-to-end exercise of all 6 required behaviours. |

## What we get

- **Interop signal:** any tool that already speaks CloudEvents (otel
  bridges, Knative sinks, the official Python SDK) can consume the file
  without per-tool adapters.
- **Envelope/body separation by construction.** `dataschema` carries the
  per-type version pointer, so a body can evolve to v2 without touching
  the envelope.
- **Identity is spec'd.** `(source, id)` is globally unique; the file is
  one of N delivery channels — re-broadcast over SSE without rewriting
  fields.

## What we pay

- **Verbosity:** ~2× line size vs variant B. For a 10k-event run that is
  ~3 MB instead of ~1.5 MB — not catastrophic, but real.
- **Awkward placement of Requiem-specific fields.** CE *extension
  attributes* must be flat top-level primitives (string/int/bool); they
  cannot carry the structured `outcome` Requiem needs. So `run_id`,
  `node_path`, and the discriminated outcome all live under `data`,
  which slightly defeats the "envelope-only" lens that jq users expect.
- **Type-string discipline:** `io.requiem.verb.completed` must be a stable
  enum across the codebase. Cheap to manage, but a coordination point.
