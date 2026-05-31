# Variant B — Log + Periodic Snapshots

JSONL is still truth. A snapshot is a derived dump of the in-memory
`Projection`, written every N events (default: 5; in production: ~500).
Restart loads the latest snapshot + replays only the events after it.

- **Truth substrate:** `<run>.events.jsonl`
- **Restart accelerator:** `<run>.snapshots/<at_event_id>.snapshot.json`
- **Snapshot validation:** every restart re-derives the projection purely
  from the log and compares fingerprints. Divergence → `SnapshotDivergenceError`.
  This is the only design choice in B that earns its keep — it defends
  the snapshot from being silently weaponised against the invariant.

## Run

```
pip install -r ../requirements.txt
python demo.py
```

## Strengths

- Restart latency bounded by snapshot interval, not log length.
- Snapshot fingerprint mismatch catches a whole class of "snapshot is
  lying" failures that pure-log restart never has to worry about.

## Weaknesses

- The verifier in this prototype re-folds the whole log on restart to
  prove the snapshot. That dissolves the headline benefit. In production
  you'd verify lazily (e.g., once per N restarts) or only when an event
  with `event_id` ≤ snapshot is observed (which would itself be a defect).
  Either choice is a *real* design decision Daniel has to make.
- Two file types means two corruption modes (truncated log line; tampered
  snapshot). Both are handled, but the test surface doubles.
- Snapshot dirs accumulate; needs a pruning policy (out of prototype scope).
