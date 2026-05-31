# Phase A — Seam #8: External-Process Abstraction

Three runnable Python prototype variants for the seam where Requiem shells out
to `git`, `gh`, `twig`, `az`, and other tools. Each variant is self-contained,
runs cross-platform on Windows, and demonstrates all seven required behaviours
(success, non-zero exit, missing binary, timeout, test substitution,
exit-code stratification, cross-platform robustness).

## WHAT this seam is

Polyphony shells out to external tools in dozens of places. Every callsite
currently invents its own error handling. This seam defines:

1. How a verb invokes an external tool.
2. How the tool's failure modes become **typed verb outcomes**
   (per `INV-DISCRIMINATED-OUTCOMES`).
3. How tests substitute fakes so the harness can drive the engine in-process
   without ever spawning a real subprocess.
4. The contract for **exit-code stratification** — turning tool-specific exit
   codes into the canonical `Success | RetryableFailure | PermanentFailure |
   NeedsHuman | Cancelled` union.

## WHICH invariants

- **INV-DISCRIMINATED-OUTCOMES** (north-star §2): every verb returns a tagged
  union; the variant tag is the contract. No "exit 0 means success" convention
  at the verb boundary. The runner classifies *what happened to the process*;
  the verb classifies *what it means in domain terms*. Two-layer model.
- **INV-NO-CORRUPT-FORWARD** (north-star §2): an ambiguous failure
  (`gh exit 1` with unrecognized stderr) MUST NOT be silently auto-retried.
  It routes to `NeedsHuman`. This is the **Ravel L-1 caveat** —
  Liszt-2 shipped `exit 1 = transient` in the original deep dive; Ravel
  caught it as a Daniel-invariant violation; all three variants here encode
  the corrected discipline. See `docs/references/error-deep-dive-ravel-review.md`
  §Liszt-2 L-1.
- **INV-SINGLE-PROCESS**: the runner / clients / effects layer is in-process
  Python; the only subprocess boundary is the genuine external tool. Fakes
  replace the implementation, not the seam.

## How to run

```powershell
py -3 -m pip install -r prototypes/external-process/requirements.txt
cd prototypes/external-process/variant-a-process-runner-protocol  # or -b- / -c-
py -3 demo.py
py -3 -m unittest test_verbs.py -v
```

All three variants pass tests on Windows with Python 3.11+ and pydantic v2.

## VARIANT COMPARISON TABLE

| Axis | A — ProcessRunner protocol | B — Per-tool typed clients | C — Effect injection |
|---|---|---|---|
| **Type safety** | Medium. `ProcessOutcome` is typed; verb re-classifies exit codes by hand each call. | High. Each tool method has its own outcome union; verb does pure domain matching. | High. Same outcome unions as B, plus verb signatures declare exactly which effects are touched. |
| **Ergonomics — verb author** | Verb must remember tool-specific exit-code lore (git 128, gh stderr fingerprints). Every verb writes that lore inline. | Verb is short: pattern-match on outcome variants. Tool lore lives once in the client. | Same brevity as B. Verb is a plain function — no client object to wire. |
| **Exit-code stratification ergonomics** | Worst — verb owns it. 12 verbs touching git = 12 places that need to know exit 128. | Best for the lore (one place per tool) but the client surface grows. Verb only sees domain outcomes. | Same as B for stratification location; effect type acts as the contract surface. |
| **Test substitution ergonomics** | Single `FakeProcessRunner` for everything. Scripts by argv prefix. Generic and uniform but argv-coupled (changing a flag breaks the fake). | One fake class per tool. Scripts by method (`script_rev_parse(...)`). Tests are decoupled from argv. | Same per-tool fakes as B. Tests read like `Runtime(git=FakeGit(GitSha(...)), gh=FakeGh(...))`; no patching, no mock library. |
| **Tool-version coupling (e.g., `gh` changes stderr wording)** | Bleeds through to every verb. Migration = grep + patch all callsites. | Contained in one client. Single-point fix. | Same as B. |
| **Cross-platform robustness** | Runner has one place to handle `NotADirectoryError` (Windows) vs `FileNotFoundError` (Unix), missing PATH, encoding. All callers benefit. | Same one-place fix via `_invoke.py` helper. | Same one-place fix via `_invoke()` in `real_effects.py`. |
| **Observability (logging stdout/stderr)** | Single seam to wire structured logging around. Every call goes through `RealProcessRunner.run`. | Each client wraps `_invoke`; we can wire logging at the helper or per-method. Slightly more places, but clean. | Same as B; each `RealGit`/`RealGh` method can wire its own trace. Verb's effect declaration is itself a useful trace anchor ("this verb touched git + gh"). |
| **Per-tool API-surface drift (twig adds a new verb)** | No change to the runner. New verb just invokes new argv — but stratification still belongs to verb code. | **Worst.** Need to extend `TwigClient` with a new method + new outcome union before any verb can use it. | Same as B — extend `Twig` protocol + `RealTwig` implementation. Verb side is symmetrical with B. |
| **Capability discoverability** | None — any verb can shell out anything. Audit = grep for `runner.run`. | Listed in client method surface. | **Best** — every effect a verb uses is in its signature. `inspect.signature(verb)` is an effect catalogue. |
| **Familiarity / friction for contributors** | Highest familiarity (subprocess wrapper). Lowest ceremony for one-off scripts. | Common idiom (clients). Moderate ceremony. | Least familiar in Python. The introspection magic in `Runtime.dispatch` is small (~20 LOC) but novel. |
| **Footprint** | ~150 LOC for runner+outcomes; verbs grow. | ~250 LOC across clients; per-tool client adds ~80 LOC each. | ~300 LOC including `Runtime`; per-tool effect adds ~80 LOC each. |

## RECOMMENDATION

**Adopt Variant B (per-tool typed clients) as the production seam, with one
borrowed move from Variant C: every Real client constructor and every Fake
client constructor enters a single `Toolbelt` value object that the engine
passes to verbs.** I.e., keep the typed-client surface from B; keep "verbs
take a Toolbelt, not five positional clients" from C — without paying for
signature introspection.

Why B over A: the polyphony PR #229 fix was about exit-code classification
*leaking into verb code*. Variant A re-creates exactly that defect — every
verb that touches `gh` would need to know that "exit 1 + 'rate limit' in
stderr = retryable, anything else = NeedsHuman." B confines that knowledge
to `GhClient`, fixed once, asserted once. When `gh` 3.x changes the rate-limit
wording, one client changes, every verb stays correct. This *is* the seam.

Why B over C: C's effect-by-signature is genuinely elegant, but it pays for
the elegance with introspection magic that is non-idiomatic Python. For a
v0 codebase Daniel needs to read every day, the cost is too high relative
to the gain over B. The capability-declaration win of C can be approximated
in B by passing a single `Toolbelt` dataclass to every verb — see the
"shape of the canonical Verb" subsection below.

Why not A: A is the right shape for a generic "I need to run any subprocess"
seam (e.g., the harness's chaos primitives — "spawn this arbitrary binary
with this scripted outcome"). Keep A's `ProcessRunner` protocol *internally*
as the implementation detail of B's clients (it gives us one place to handle
Windows path quirks, encoding, and logging), but do not expose it to verbs.

## OPEN QUESTIONS for Daniel

1. **Toolbelt scope** — should `Toolbelt` carry only tool clients
   (`git`, `gh`, `twig`, `az`), or should it also carry orthogonal effects
   (`Clock`, `Filesystem`, `EventLog`) so verbs have a single injection seam?
   Default: include `Clock` and `EventLog`; defer `Filesystem` until a verb
   needs it.

2. **`twig` and `az` typed clients up front** — V0 needs both. Do we
   pre-design their outcome unions now (so we can size the seam honestly)
   or defer until the first verb needs them?
   Default: design the four outcomes per tool that polyphony's parity
   inventory tells us we need (`twig publish`, `twig sync`, `az pipelines
   run`, `az pipelines show`) and ship those with v0.

3. **Sentinel-cancel on Windows** — Ravel X-2 flagged a race in
   `CONDUCTOR_CANCEL_TOKEN` sentinel-file polling. In Requiem this becomes
   an `asyncio.CancelledError`, but the **external-tool subprocess** still
   needs a cancel story. Should every client method accept an optional
   `cancel: asyncio.Event` that polls every 250 ms and sends `terminate()`?
   Default: yes, but bundle into Phase A seam #5 (agent boundary) since the
   cancellation story is symmetric across all out-of-process work.

4. **Streaming vs buffered stdout** — `capture_output=True` buffers. For
   long-running tools (`gh pr checks --watch`, `az pipelines run --wait`)
   we may want a streaming variant that yields lines as they arrive and
   feeds them into the event log live. Defer or include in v0?
   Default: defer to v0+1; the verbs we need for polyphony parity are all
   short-lived enough that buffering is fine.

5. **`gh` JSON parsing inside the client** — `RealGhClient.pr_view` currently
   returns the raw JSON string. Should the client also parse it into a
   typed model (`GhPr(number=int, state=..., title=...)`)? That's another
   layer of tool-version coupling we'd own — but it's the natural place
   for it.
   Default: parse at the client. Verbs see typed `GhPr`, not raw JSON.

6. **What goes through this seam vs the LLM seam (#5)?** `gh copilot suggest`
   is on the boundary. Default: LLM provider seam owns model calls; this seam
   owns CLI tools. `gh` stays here even if some `gh` subcommands wrap LLMs.

## "Shape of the canonical Verb"

A verb is a Python function. Here is the same verb (`check_pr`) authored
under each variant.

### Variant A — ProcessRunner protocol

```python
def check_pr(runner: ProcessRunner, repo: Path, pr_number: int) -> VerbOutcome:
    out = runner.run(["gh", "pr", "view", str(pr_number), "--json", "..."], cwd=repo, timeout_s=15.0)
    match out:
        case ProcessSuccess(stdout=stdout):
            return VerbSuccess(value={"raw_json": stdout})
        case NonZeroExit(exit_code=ec, stderr=stderr):
            s = stderr.lower()
            if "no pull requests found" in s:
                return PermanentFailure(reason=f"pr {pr_number} not found")
            if "rate limit" in s:
                return RetryableFailure(reason="rate limit", retry_key=f"pr-view-{pr_number}")
            if "401" in s or "403" in s:
                return NeedsHuman(reason="auth lapse", diagnostic={"stderr": stderr})
            return NeedsHuman(reason=f"unknown gh exit {ec}", diagnostic={"stderr": stderr})  # Ravel L-1
        case Timeout():
            return RetryableFailure(reason="timed out", retry_key=f"pr-view-{pr_number}")
        case NotFound():
            return PermanentFailure(reason="gh not on PATH")
```

Verb knows `gh`'s stderr dialect. Verb owns Ravel L-1 discipline at every callsite.

### Variant B — Per-tool typed clients

```python
def check_pr(gh: GhClient, repo: Path, pr_number: int) -> VerbOutcome:
    match gh.pr_view(repo, pr_number):
        case PrViewFound(raw_json=raw):
            return VerbSuccess(value={"raw_json": raw})
        case PrViewNotFound():
            return PermanentFailure(reason=f"pr {pr_number} not found")
        case PrViewRateLimited():
            return RetryableFailure(reason="rate limit", retry_key=f"pr-view-{pr_number}")
        case PrViewAuthLapse(stderr=stderr):
            return NeedsHuman(reason="auth lapse", diagnostic={"stderr": stderr})
        case PrViewUnknown(exit_code=ec, stderr=stderr):
            return NeedsHuman(reason=f"unknown gh exit {ec}", diagnostic={"stderr": stderr})
```

Verb is pure domain logic. The stderr-dialect knowledge is in `GhClient`
once, asserted by `GhClient`'s own tests.

### Variant C — Effect injection

```python
def check_pr(gh: Gh, clock: Clock, repo: Path, pr_number: int) -> VerbOutcome:
    ts = int(clock.now_unix())
    match gh.pr_view(repo, pr_number):
        case GhPrFound(raw_json=raw):
            return VerbSuccess(value={"raw_json": raw, "checked_at": ts})
        case GhPrMissing():
            return PermanentFailure(reason=f"pr {pr_number} not found")
        case GhTransient(reason=r):
            return RetryableFailure(reason=f"gh transient: {r}", retry_key=f"pr-view-{pr_number}")
        case GhAuthLapse(stderr=stderr):
            return NeedsHuman(reason="auth lapse", diagnostic={"stderr": stderr})
        case GhFailure(detail=detail):
            return NeedsHuman(reason="unknown gh failure", diagnostic={"detail": detail})
```

Same as B in body. The difference is at the call site:
`runtime.dispatch(check_pr, repo=repo, pr_number=42)` —
no client passed; signature declares the capabilities; runtime supplies them.

### Recommended (B + Toolbelt borrow from C)

```python
@dataclass(frozen=True)
class Toolbelt:
    git: GitClient
    gh: GhClient
    twig: TwigClient
    az: AzClient
    clock: Clock
    events: EventLog

def check_pr(tools: Toolbelt, repo: Path, pr_number: int) -> VerbOutcome:
    match tools.gh.pr_view(repo, pr_number):
        ...
```

One injection surface, no introspection magic, typed all the way through,
fakes constructed once per test (`Toolbelt(git=FakeGit(...), gh=FakeGh(...), ...)`).
This is the shape I propose Requiem ships at v0.

## File map

```
prototypes/external-process/
├── README.md                                   (this file)
├── requirements.txt                            (pydantic>=2.6,<3)
├── variant-a-process-runner-protocol/
│   ├── outcomes.py                             (ProcessOutcome union)
│   ├── runner.py                               (ProcessRunner protocol + Real + Fake)
│   ├── verbs.py                                (verbs + VerbOutcome union)
│   ├── demo.py                                 (runs all 5 demo blocks)
│   └── test_verbs.py                           (9 unit tests)
├── variant-b-per-tool-clients/
│   ├── outcomes.py                             (per-method outcome unions)
│   ├── _invoke.py                              (shared subprocess helper)
│   ├── git_client.py                           (GitClient + Real + Fake)
│   ├── gh_client.py                            (GhClient + Real + Fake)
│   ├── verbs.py
│   ├── demo.py
│   └── test_verbs.py                           (10 unit tests)
└── variant-c-effect-injection/
    ├── effects.py                              (Git/Gh/Clock protocols + outcomes)
    ├── real_effects.py                         (RealGit/RealGh/RealClock)
    ├── fake_effects.py                         (FakeGit/FakeGh/FrozenClock)
    ├── runtime.py                              (Runtime.dispatch — ~40 LOC of DI)
    ├── verbs.py
    ├── demo.py
    └── test_verbs.py                           (10 unit tests)
```

**Test totals:** 29 tests across 3 variants, all green on Windows
Python 3.11+ with pydantic 2.13.

## Provenance

- `docs/north-star.md` §2 — `INV-DISCRIMINATED-OUTCOMES`, `INV-NO-CORRUPT-FORWARD`,
  `INV-SINGLE-PROCESS`
- `docs/decisions/0001-single-process-architecture.md` — why the verb library
  is Python in-process
- `docs/references/error-handling-deep-dive.md` §1.2 / §2.5 / §5 — Liszt-2's
  original `script:` contract and 5-class exit-code shape
- `docs/references/error-deep-dive-ravel-review.md` §Liszt-2 L-1 — the
  `exit 1 = transient` correction that this seam encodes structurally
- `docs/references/polyphony-parity-inventory.md` §4 — the catalogue of
  external integrations this seam must cover at v0
