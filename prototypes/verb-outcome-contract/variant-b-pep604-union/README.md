# Variant B — PEP 604 sealed dataclass union + `match`

Type-first. Frozen dataclasses, a `TypeAlias` union, and a `match` statement
in the engine. Exhaustiveness is enforced by `assert_never` in the `case _:`
fall-through — mypy --strict refuses to compile a match that misses an arm.

## What it shows
- Five `@dataclass(frozen=True, slots=True)` records (no metaclass overhead)
- `Outcome: TypeAlias = Success | RetryableFailure | ...`
- Engine + UI both dispatch via `match`; both end in `assert_never(outcome)`
- JSON is hand-rolled: `kind` tag is synthesized at the wire boundary
- Unknown wire `kind` raises (INV-NO-CORRUPT-FORWARD — no silent coercion)

## Run
```powershell
python demo.py
python -m mypy --strict demo.py
```

## Verdict in one paragraph
Cleanest read at the dispatch site of the three. `match` with class patterns
is the most natural way to write the engine; mypy's exhaustiveness check is
strict and fires at exactly the right place. Cost: JSON is hand-rolled (one
registry dict, plus the read-side ValueError for unknown kinds). Compared to
A: trades free pydantic JSON for the simplest possible in-memory shape.
