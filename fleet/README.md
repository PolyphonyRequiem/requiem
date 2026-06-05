# Requiem delivery fleet

The `requiem-*` Hermes profile distributions that turn a Hermes kanban board
into requiem's delivery substrate (ADR-0016, ADR-0017). requiem stays the
decomposition authority and the ADO system of record; these profiles are the
fleet that *delivers* the leaves requiem hands them, and reports back through
the **handoff receipt** wire contract so requiem can **verify** deliveries
rather than trust a raw `done` status.

## The profiles

| Profile               | Role          | Does                                           |
|-----------------------|---------------|------------------------------------------------|
| `requiem-implementer` | `implementer` | Implements one leaf on a worktree, opens a PR. |
| `requiem-reviewer`    | `reviewer`    | Reviews a delivered leaf's PR.                 |
| `requiem-closer`      | `closer`      | Drives a delivered PR through its ADO lifecycle. |

The role → profile mapping is repo policy, set in
`.requiem-config/process.yaml` (`roles:` — see `process-config.example.yaml`).
Renaming a profile here means updating that mapping; the names are not magic.

## The wire contract (single-sourced)

Every profile carries an **identical** `skills/handoff-receipt/SKILL.md`. That
skill IS the emit side of the contract in `src/requiem/handoff.py`; the
consume side is `parse_handoff`. The two are kept in lockstep by
`tests/test_fleet_distribution.py`, which:

- parses each `distribution.yaml`,
- extracts the canonical receipt example from each skill and runs it through
  the real `parse_handoff` (so the documented emission is provably valid), and
- asserts the receipt skill is byte-identical across all three profiles (so the
  contract cannot drift between fleet members).

The golden shape also lives at `tests/fixtures/handoff_v1_golden.json`; neither
the fleet nor the kanban-delivery track changes the emitted/consumed shape
without updating both that fixture and the receipt skill.

## Installing a profile

```bash
hermes profile install ./fleet/requiem-implementer
hermes -p requiem-implementer model            # pin the model (no per-task flag)
```

`config.yaml` ships **Manual** kanban orchestration (`auto_decompose: false`)
on purpose: requiem is the only decomposition authority, so Hermes' own
auto-decomposer must stay off. `config.yaml` is operator-owned and preserved on
`hermes profile update`; `SOUL.md` and the receipt skill are
distribution-owned and replaced verbatim (they are the contract).
