# Seam: Verb outcome contract

> Phase A, seam #1. Three runnable prototype variants for the
> single most consequential type in Requiem.

## What this seam is

Every verb in Requiem returns a **discriminated outcome**:

- `Success` — verb did its thing; receipts attached
- `RetryableFailure` — transient; engine may retry under a bounded policy
- `PermanentFailure` — non-retryable; human-readable message; routes to surrender
- `NeedsHuman` — verb deliberately routed to a gate (e.g. receipts violation)
- `Cancelled` — operator / deadline / supersession / parent-cancel; short-circuits retry

The engine reads this shape; the state machine routes on the variant tag;
retry policies key off the tag; persistence records the full envelope; the
UI eventually renders it.

## Why it's load-bearing

It is the *type* that crosses every layer of Requiem. If it has the wrong
shape we pay for it in every consumer: engine dispatch, retry decisions,
event-log schema, UI rendering, harness assertions, reconcile-verb
diagnostics. Getting this right early is the highest-leverage move on the
seam list — it is the type whose change blast-radius is everything.

## Invariants this seam directly serves

| Invariant                            | How this seam serves it                                                                                                                              |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| **INV-DISCRIMINATED-OUTCOMES**       | This *is* the invariant. The variant tag is the contract; nothing inspects the inner payload to determine success/failure.                           |
| **INV-CANCEL-SHORT-CIRCUITS-RETRY**  | `Cancelled` is its own variant — the engine dispatch handler can refuse to retry without consulting `retry.max`. Asserted in every demo.             |
| **INV-NO-ENGINE-ABANDONMENT**        | `RetryableFailure` with exhausted budget routes to `surrender`, not to a terminal `abandoned` state. Modelled in `engine_decide` in all three demos. |
| **INV-NO-CORRUPT-FORWARD**           | `Success.inspected_artifacts` (receipts) is part of the contract, not an afterthought. Variant B/C `read_event` refuses unknown wire `kind` values.  |
| **INV-EVENT-LOG-AUTHORITATIVE**      | The outcome envelope is what gets written to `run.events.jsonl`. JSON round-trip must narrow back to the original variant; tested in every demo.     |
| **INV-RESTART**                      | `RetryableFailure.retry_key` is part of the contract, so the idempotency story doesn't need a separate envelope to ride alongside the outcome.       |
| **INV-SINGLE-PROCESS**               | These shapes are *Python types*, not exit codes plus JSON envelopes. They cross no process boundary. This is the single biggest simplification.      |

## Variant comparison

| Axis                              | A: Pydantic tagged union              | B: PEP 604 + match                          | C: ABC + visitor dispatch                                  |
| --------------------------------- | -------------------------------------- | ------------------------------------------- | ---------------------------------------------------------- |
| **Type safety at dispatch**       | ★★☆ — needs `isinstance`, *not* tag   | ★★★ — `match` + `assert_never` is idiomatic | ★★★ — Protocol-enforced; covers *every* consumer class      |
| **Verb-author ergonomics**        | ★★☆ — `BaseModel` boilerplate         | ★★★ — `@dataclass(frozen, slots)` minimal   | ★★☆ — `dispatch` boilerplate on every leaf                  |
| **JSON contract clarity**         | ★★★ — `discriminator="kind"` is self-documenting | ★★☆ — `kind` synthesized at the seam        | ★★☆ — `to_dict` per class; registry on read                 |
| **Engine dispatch ergonomics**    | ★★☆ — isinstance chain reads OK       | ★★★ — `match` is the cleanest of the three  | ★★☆ — visitor adds a layer; reads as method dispatch        |
| **Evolvability (add a 6th kind)** | ★★☆ — must update every `isinstance` chain | ★★★ — `match` + `assert_never` flags every dispatch *function* | ★★★ — flags every *consumer class* (strongest blast radius) |
| **Error-message UX**              | ★★★ — `model_dump_json` for free      | ★★☆ — manual fields                         | ★★☆ — manual fields                                         |
| **mypy --strict support**         | ★★☆ — `Literal` tag does not narrow; `isinstance` does | ★★★ — `match` narrows perfectly             | ★★★ — Protocol mismatch is a compile-time error              |
| **Lines of code per leaf**        | ~7                                     | ~6                                          | ~10 (extra `dispatch` + `to_dict`)                          |
| **Net seam complexity**           | medium                                 | low                                         | medium-high                                                  |

Stars are calibrated against this seam specifically, not against an
abstract "good Python." All three variants pass mypy --strict; all three
have a runnable `demo_sixth.py` that demonstrates the failure mode of
adding a sixth kind.

## Recommendation

**Advance Variant B (PEP 604 + `match`).** It is the smallest workable
shape with the cleanest engine dispatch site, and mypy --strict's
exhaustiveness check fires at exactly the right place (every function that
matches on the union). The variant whose engine reads the simplest is the
one whose engine will keep reading the simplest as Requiem grows.

The one thing B gives up vs A is free JSON discrimination — but the
hand-rolled `write_event` / `read_event` is ten lines, and it gives us
explicit control of the wire shape (e.g. ordering `kind` first, rejecting
unknown kinds rather than coercing — both directly serve INV-NO-CORRUPT-FORWARD).

The one thing B gives up vs C is consumer-class exhaustiveness — if Daniel
later wants the engine to *force* every downstream consumer (engine, UI,
persistence, harness, reconcile, diagnose) to handle every outcome kind at
compile time, the visitor pattern is the only one that guarantees it. My
read is that B + a single `assert_never`-bearing dispatch function per
consumer module is *enough* discipline and the visitor adds boilerplate
without proportional value. If we are wrong about that, the migration from
B → C is mechanical (add `dispatch` methods, add Protocol). The other
direction is harder.

A **hybrid is also viable**: ship B's in-memory shape (dataclasses + match)
but use pydantic *only* at the event-log seam (one `TypeAdapter` validates
the read; serialization is `asdict` + `kind` tag). This gives us B's
ergonomics + A's JSON guarantees with one TypeAdapter at the boundary.

## Constraints on adjacent seams

These are notes for the other Phase A authors in flight; not requests,
just signals.

- **Bach (persistence / event log):** the wire shape this seam produces is
  `{"kind": "<one of 5>", ...payload}`. If the event-log envelope needs
  additional outer fields (`event_id`, `ts`, `run_id`, `node_id`), the
  outcome dict goes under a `payload` key — it should not pollute the
  outcome's own namespace. Important: `kind` belongs to the outcome, not
  to the event envelope, so the engine can `outcome_from_dict(event["payload"])`
  without contortion.

- **Beethoven (state machine kernel):** the state machine routes on
  `type(outcome)`, not on `outcome.kind`. The `kind` field is for the wire;
  the type system is for the engine. The state machine should accept a
  handler-shaped callback (or a `match` block) per node; both A and B make
  this trivial.

- **Mahler (retry policy / on_error):** `RetryableFailure.retry_key` is the
  idempotency key; the state machine should pass it back into the verb on
  the next attempt. The retry budget lives in *the state machine's
  per-node config*, not on the outcome — the outcome reports "this is
  transient" and `attempt`; the budget decision is the engine's.

- **Wagner (DSL / workflow definition):** the workflow router consumes the
  outcome's *type tag*, not its payload. `on_outcome:` should branch on
  `success | retryable_failure | permanent_failure | needs_human | cancelled`,
  not on `error_kind` strings.

- **All persistence/harness/UI consumers:** if you read an outcome and your
  code has a fallthrough `else` arm, you are violating the seam — add an
  `assert_never` or use the visitor. The whole point is that adding a
  sixth kind has a single, well-defined surface where every consumer
  surfaces.

## Open questions for Daniel

1. **In-memory shape vs wire shape — same or different?** B's recommendation
   above hand-rolls JSON. A gives you both for free at the cost of
   `BaseModel` ergonomics. Is the trade *worth* hand-rolling, or should we
   ship the hybrid (B in memory + pydantic TypeAdapter at the event-log
   boundary)? Default: hybrid.

2. **Consumer-side exhaustiveness — function-local (B) or class-wide (C)?**
   If the engine, UI, persistence, harness, reconcile, and diagnose each
   have their own `match` block with `assert_never`, function-local is
   enough — adding a kind fails six places in CI. If we want a *single*
   place where forgetting any consumer is a compile-time error, only the
   visitor pattern delivers it. Default: function-local — the cost of
   `assert_never` discipline is low and the boilerplate of visitor is
   high. Worth a check.

3. **`PermanentFailure.error_kind` — open string vs closed enum?** Right now
   the prototypes treat `error_kind` as an open string (`"auth.invalid_token"`,
   `"config.missing_field"`). If it should be a closed enum keyed off the
   21-signal seed catalogue (Beethoven §3) so the UI can render category
   icons and the reconcile verb can mechanical-classify, that closes the
   set — but adding new permanent-failure categories then requires an ADR.
   Default: open string with a *recommended* prefix taxonomy
   (`auth.*`, `config.*`, `git.*`, `external.*`, `internal.*`) and a lint.

4. **`RetryableFailure.retry_key` — required or optional?** Boulez F-CONSOL-3 +
   Ravel M-3 said "ship `retry:` together with the `retry_key:` validator,
   else it's an attractive nuisance." In this seam, that translates to
   `retry_key: str` (required, not `str | None`). Default: required.
   Question: is there a class of retryable failure where no idempotency
   key is meaningful (e.g. pure-read operations like `gh pr view`)? If so,
   we need a sentinel — `retry_key: str | Literal["readonly"]` — or a
   `ReadOnlyRetryable` sub-variant.

5. **`Cancelled.cause` — closed Literal or open string?** Current shape is
   `Literal["operator", "deadline", "superseded", "parent_cancelled"]`.
   Adding a fifth cause is an ADR. Is this the right closure boundary or
   should it be open?

6. **What about `Skipped`?** The current 5-kind set is what the deep-dive
   asserts. Some workflow nodes (`predicate: false`, `condition: skipped`)
   may want a sixth `Skipped` outcome that is *not* a failure — it's
   "verb chose not to act, but state is fine." Default: model `Skipped` as
   a `Success` with `result={"skipped": true, "reason": ...}`. But if
   Wagner's workflow DSL wants to route on `skipped` specifically, it
   becomes the sixth variant. Worth deciding now while the contract is
   small.

## Layout

```
prototypes/verb-outcome-contract/
├── README.md                                # this file
├── requirements.txt                         # pydantic v2, mypy
├── variant-a-pydantic-tagged/
│   ├── README.md
│   ├── demo.py                              # runnable; mypy --strict clean
│   └── demo_sixth.py                        # mypy --strict FAILS (proof)
├── variant-b-pep604-union/
│   ├── README.md
│   ├── demo.py
│   └── demo_sixth.py
└── variant-c-abc-dispatch/
    ├── README.md
    ├── demo.py
    └── demo_sixth.py
```

## How to run

```powershell
# from repo root
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r prototypes\verb-outcome-contract\requirements.txt

# run all three
foreach ($v in 'variant-a-pydantic-tagged','variant-b-pep604-union','variant-c-abc-dispatch') {
  .\.venv\Scripts\python.exe "prototypes\verb-outcome-contract\$v\demo.py"
}

# mypy --strict (all should PASS)
foreach ($v in 'variant-a-pydantic-tagged','variant-b-pep604-union','variant-c-abc-dispatch') {
  .\.venv\Scripts\python.exe -m mypy --strict "prototypes\verb-outcome-contract\$v\demo.py"
}

# exhaustiveness check (all should FAIL with helpful errors)
foreach ($v in 'variant-a-pydantic-tagged','variant-b-pep604-union','variant-c-abc-dispatch') {
  .\.venv\Scripts\python.exe -m mypy --strict "prototypes\verb-outcome-contract\$v\demo_sixth.py"
}
```
