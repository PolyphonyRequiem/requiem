# Handoff receipt — the requiem delivery wire contract

You are a **requiem delivery worker**. The requiem orchestrator dispatched this
kanban task; it decomposed an Azure DevOps work-item tree, picked exactly one
*implementable leaf*, and handed it to you. When you finish, requiem does **not
trust** that the task is `done` — it **verifies** what you claim. This skill is
how you make a verifiable claim.

## The rule

When (and only when) you have genuinely delivered the leaf — branch pushed, PR
opened, tests run — you MUST complete the task with a structured receipt:

```
kanban_complete(metadata={ ...the receipt below... })
```

A `done` task with **no** receipt, or a receipt that does not match the task you
were given, is **not** accepted as delivered. requiem will route it to a human
instead of merging it. An honest *failure* (you could not deliver) should NOT
emit a receipt — leave the task blocked/failed and say why in the summary.

## Where the identity fields come from

Your task's **idempotency key** is the source of truth for who you are working
for. It has the exact shape:

```
requiem:{root_item}:{plan_hash}:{leaf_id}
```

Split it on `:` and copy the three trailing segments verbatim into the receipt.
Never invent, reformat, or "correct" them — requiem matches them byte-for-byte
to attribute your evidence to the right leaf. A mismatch is treated as
**misattributed evidence** and rejected.

- `worker_profile` is **your own profile name** (the `name` from this profile's
  `distribution.yaml` — e.g. `requiem-implementer`, `requiem-reviewer`, or
  `requiem-closer`). Copy it exactly; it is how requiem records which fleet
  member did the work.
- `worker_profile_version` is this profile's `version` from `distribution.yaml`.

## The receipt (schema_version 1)

Required (strict, non-empty strings — a missing one fails the contract):
`schema_version`, `leaf_id`, `root_item`, `plan_hash`, `worker_profile`.

Evidence (optional — include every field you can actually substantiate; their
*absence* is honest under-claiming, never silently treated as success):
`branch`, `commit_sha`, `pr_url`, `changed_files`, `tests_run`,
`worker_profile_version`.

A complete, contract-valid receipt looks exactly like this:

```json
{
  "schema_version": 1,
  "leaf_id": "22002",
  "root_item": "880",
  "plan_hash": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2",
  "worker_profile": "requiem-implementer",
  "branch": "impl/880-22002",
  "commit_sha": "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c",
  "pr_url": "https://github.com/acme/widgets/pull/417",
  "changed_files": [
    "src/widgets/auth/reset.py",
    "src/widgets/auth/tests/test_reset.py",
    "migrations/003_single_use_reset_tokens.sql"
  ],
  "tests_run": [
    "tests/test_reset.py::test_happy_path",
    "tests/test_reset.py::test_expired_token_410"
  ],
  "worker_profile_version": "1.4.2"
}
```

## Hard don'ts

- Do **not** emit `schema_version` other than `1`. If a future requiem speaks a
  newer schema it will tell you; until then `1` is the only accepted version.
- Do **not** put the identity fields (`leaf_id`/`root_item`/`plan_hash`) under a
  nested object. They are top-level keys of the `metadata` blob.
- Do **not** claim a `pr_url` / `commit_sha` you did not actually create. requiem
  verifies the claims it can; a fabricated claim is worse than an absent one.
- Do **not** emit a receipt for a task you did not finish.
