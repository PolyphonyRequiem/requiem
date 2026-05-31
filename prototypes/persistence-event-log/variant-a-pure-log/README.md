# Variant A — Pure Event Log

The log IS the run. `Projection` (the "manifest") is a pure function over
the JSONL log, rebuilt on startup, cached in memory, mutated by each new
append so reads are O(1) post-warmup.

- **Truth substrate:** `<run>.events.jsonl` (append-only, one file per run)
- **In-memory cache:** `Projection` dataclass — never written to disk
- **Restart:** open file, `fold(events, Projection())`
- **Corruption:** `CorruptLogError` at the first un-decodable line, with
  byte offset for surgical recovery
- **Schema evolution:** unknown `kind` values are *counted* but *not*
  projected (INV-NO-CORRUPT-FORWARD). Operator-visible via
  `projection.unknown_kind_count`

## Run

```
pip install -r ../requirements.txt
python demo.py
```

## Strengths

- Strictest possible adherence to INV-EVENT-LOG-AUTHORITATIVE — there is
  literally no other state to be authoritative about.
- Trivial test ergonomics: assert against `Projection`, no setup/teardown.
- Trivial debuggability: `cat run.events.jsonl | jq` is the whole story.
- Trivial concurrency story per-run: one writer, append-only.

## Weaknesses

- Restart latency is O(log length). For Requiem's expected runs
  (hundreds → low thousands of events) this is a non-issue. At 100k+ it
  starts to bite.
- Every restart re-validates every event through pydantic. Same scaling
  concern, slightly bigger constant.
