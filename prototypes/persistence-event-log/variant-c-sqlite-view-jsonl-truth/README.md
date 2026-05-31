# Variant C — SQLite View + JSONL Truth

JSONL is the durable, append-only truth. SQLite is a hot, indexed,
queryable *view* into it — rebuildable from JSONL at any time.

Write path:
1. Append to `<run>.events.jsonl` and `fsync`.
2. Apply the projection delta to `<run>.view.sqlite`.

If we crash between 1 and 2, `_reconcile_on_open` replays the tail into
SQLite on next start. If SQLite is ever *ahead* of the log (e.g.
someone restored an old log without rebuilding the view), startup raises
`ViewAheadOfLogError` and refuses to advance — INV-NO-CORRUPT-FORWARD.

- **Truth substrate:** `<run>.events.jsonl`
- **Hot store:** `<run>.view.sqlite` (WAL mode, indexed PKs, no ORM)
- **Restart:** "view caught up to log" check — O(1) if synced, O(tail)
  if log advanced past the view

## Run

```
pip install -r ../requirements.txt
python demo.py
```

## Strengths

- Fast targeted queries: `is_mg_retired(mg_id)` is one indexed lookup.
  This matters as soon as the "current" projection has hundreds of merge
  groups and thousands of approval rows.
- SQL ergonomics for the UI backend: `SELECT * FROM merge_group WHERE
  retired=0 ORDER BY mg_path` is cheap to write and cheap to run.
- Restart-near-instant when the view is in sync, even for very long logs.

## Weaknesses

- Two stores ⇒ two ways to be wrong. We handle the canonical wrong cases
  (view-behind, view-ahead, schema unknown), but each new event kind
  needs a *projector* (SQL `INSERT/UPDATE`) **and** a *reader*. Variant A
  needs only the projector. That tax is real and recurring.
- SQLite holds a file lock on Windows; cross-process inspection requires
  a separate connection (fine; we use WAL).
- `unknown_kind` events are counted in a side-table but won't influence
  any indexed query — by design (we don't know what they mean) but worth
  surfacing.
