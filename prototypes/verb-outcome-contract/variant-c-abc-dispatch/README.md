# Variant C — ABC sealed hierarchy with visitor dispatch

OO-first. `Outcome` is an ABC; each leaf overrides `dispatch(handler)` to
call the right method on a `Protocol`-shaped handler. The engine is one
handler, the UI renderer is another.

## What it shows
- ABC `Outcome` + 5 sealed dataclass leaves
- `OutcomeHandler[T]` Protocol with `on_success`, `on_retryable_failure`, ...
- Engine + UI are two separate handlers — same Protocol, different return types
- `dispatch` is double-dispatch: outcome → handler.on_<kind>(self)

## Run
```powershell
python demo.py
python -m mypy --strict demo.py
```

## Verdict in one paragraph
Strongest exhaustiveness guarantee: every Protocol implementer must cover all
kinds, not just the one function we happen to look at today. Cost: every
outcome class carries a `dispatch` boilerplate method, and the JSON shape
is hand-rolled (same as B). The visitor-pattern overhead is overkill for
Python — `match` (variant B) gets ~90% of the guarantee with a fraction of
the typing. C is only worth it if Daniel wants the engine to enforce that
*all* downstream consumers (engine, UI, harness, persistence, journal) handle
every kind at compile time, not just one of them.
