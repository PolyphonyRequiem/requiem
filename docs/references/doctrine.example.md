# Example Requiem doctrine (house-style)

Copy to `<repo>/.requiem-config/doctrine.md`. Requiem discovers this file by
walking up from the run's repo path; with no file present, the doctrine is
empty and the fleet runs on its baked-in defaults only. See ADR-0016 and
`requiem.doctrine`.

This file is **house-style** — durable conventions true for *every* task in
this repo. It is NOT a place for task/item state (which lives in the event log
and dies with the run). Requiem hashes this file and records the hash in each
run's event log, so the fleet a run was hydrated from is auditable. When a run
learns a durable convention, it proposes an edit here via PR — legible
accumulation, never hidden memory.

---

## Testing

- Run targeted `pytest` suites, never the full suite (it hangs on a heavy module).
- `asyncio_mode=auto`; async test functions run without a decorator.
- New `Fake*` clients must match the real client's async shape (a contract test
  enforces this).

## Branches & commits

- Implementation branches: `feature/<item_id>`.
- Keep commits scoped; do not rewrite published history.

## Conventions

- Type routing is data in `process.yaml`, never hardcoded.
- Fail closed on malformed config or unknown dependencies — never guess.
- Public errors carry a typed `error_kind`.

## Layout

- Source under `src/requiem/`; workflows under `src/requiem/workflows/`.
- ADRs under `docs/decisions/`, numbered and append-only.
