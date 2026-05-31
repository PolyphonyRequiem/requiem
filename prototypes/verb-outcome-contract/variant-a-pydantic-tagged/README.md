# Variant A — Pydantic v2 discriminated union

JSON-first. `kind: Literal[...]` is the tag, Pydantic does discriminated-union
validation, `TypeAdapter` is the gateway for the event log.

## What it shows
- Five `BaseModel` subclasses, one per outcome kind, each with a `Literal` tag
- `Outcome = Annotated[Union[...], Field(discriminator="kind")]`
- `TypeAdapter(Outcome)` gives free JSON round-trip with type narrowing
- Engine dispatches via `if outcome.kind == "..."` chain, closed by `assert_never`
- `render_for_ui` is the UI surface — `PermanentFailure.message` is human-readable
- Comment block at the bottom shows the failure shape when a 6th kind is added

## Run
```powershell
pip install -r ..\requirements.txt
python demo.py
python -m mypy --strict demo.py
```

## Verdict in one paragraph
Strongest JSON story of the three. Pydantic's discriminated-union machinery
gives you write/read symmetry for free; the engine never inspects payloads to
decide success/failure. Cost: BaseModel ergonomics are heavier than dataclasses
for what is fundamentally a data record. mypy `assert_never` enforces
exhaustiveness, but only inside the dispatch function — Pydantic itself does
not block adding a kind without updating callers.
