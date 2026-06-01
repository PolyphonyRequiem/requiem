"""Implementation workflow — Bizet (Phase C seat).

Takes a leaf plan (an ADO work item with a plan description) and produces
a GitHub pull request the human can review. The second-largest of the
polyphony-parity workflows after planning; the canonical shape for
"agent writes code, we verify, we open the PR, we hand off".

Pipeline
--------

::

    start
      → fetch_plan                  (script · twig.show + read plan)
      → assert_clean_workspace      (script · fs.git_is_clean)
      → create_branch               (script · feature/<item_id>, idempotent)
      → invoke_coder                (agent  · CoderOutput)
          ├─ success           → apply_changes
          ├─ bad_output        → end_handoff (NeedsHuman)
          └─ permanent_failure → end_failed
      → apply_changes               (script · fs.write_text per FileChange)
          ├─ success                       → run_tests
          ├─ permanent_failure:no_changes  → end_failed
          └─ permanent_failure:invalid_path→ end_handoff (NeedsHuman)
      → run_tests                   (script · subprocess: test_command)
          ├─ success                       → commit_changes
          ├─ permanent_failure:tests_failed→ invoke_coder_revision
          └─ permanent_failure:test_error  → end_handoff (NeedsHuman)
      → invoke_coder_revision       (agent  · CoderOutput, fed failure)
          ├─ success           → apply_changes_revision
          ├─ bad_output        → end_handoff
          └─ permanent_failure → end_failed
      → apply_changes_revision      (script)
          → run_tests_final
      → run_tests_final             (script)
          ├─ success                       → commit_changes
          └─ permanent_failure:tests_failed→ end_handoff
      → commit_changes              (script · fs.git_commit)
      → push_branch                 (script · fs.git_push, idempotent)
      → create_pr                   (script · gh.pr_create, idempotent via pr_search)
      → link_pr_to_item             (script · twig.comment, best-effort)
      → end_handoff                 (terminate · NeedsHuman is at the gate)

Closed ``error_kind`` taxonomy (ADR 0004 §4.2) used by this workflow:

``plan.not_found``, ``plan.fetch_failed``, ``workspace.dirty``,
``branch.create_failed``, ``coder.no_changes``, ``coder.invalid_path``,
``coder.apply_failed``, ``tests.failed``, ``tests.error``,
``commit.failed``, ``push.failed``, ``pr.create_failed``,
``pr.link_failed``.

INV-RESTART: every state-mutating step is idempotent against the local
git state. The kernel's resume protocol (events.jsonl → cursor) handles
the "skip already-completed nodes" half; the verbs handle the "re-run
gracefully if we crashed mid-step" half.

INV-NO-CORRUPT-FORWARD: if the coder's revision still leaves tests red,
the workflow surrenders to ``end_handoff`` (NeedsHuman) — we never
push or open a PR with red tests. The branch is left on disk for the
human to inspect; nothing is destroyed.

Out of scope (v0, deferred to ADR if revisited):

* Multi-commit history. One commit per workflow run.
* Squash-vs-merge selection. The repo default applies.
* Linting / formatting outside ``test_command``.
* Concurrent runs against the same item (use plan-file locks later).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from requiem.agent import AgentSpec, FakeProvider
from requiem.clients.fs import FilesystemClient, FsClientError, FsGitError
from requiem.clients.gh import GhClient, GhClientError
from requiem.clients.twig import (
    TwigClient,
    TwigClientError,
    TwigItemNotFoundError,
)
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import (
    Outcome,
    PermanentFailure,
    Success,
)
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt


# ---- public dataclasses (the workflow's return shape) -----------------


@dataclass(frozen=True, slots=True)
class ImplementationResult:
    """What a caller of this workflow can pluck out of the projection.

    Mirrors the brief verbatim. We don't build this dataclass inside the
    engine — the engine returns its own ``Completed`` / ``Failed`` /
    ``Suspended`` types and the CLI's verdict card pulls these fields
    out of ``completed`` for display. The dataclass is a convenience
    shape for programmatic callers; ``from_completed`` is the converter.
    """

    item_id: int
    branch_name: str
    pr_number: int | None
    pr_url: str | None
    files_changed: list[Path]
    tests_passed: bool | None
    test_output_summary: str
    dry_run: bool

    @classmethod
    def from_completed(cls, completed: dict[str, dict[str, Any]]) -> "ImplementationResult":
        plan = (completed.get("fetch_plan") or {}).get("value", {})
        branch = (completed.get("create_branch") or {}).get("value", {})
        # Tests can complete via run_tests (first pass) or run_tests_final.
        tests = (
            (completed.get("run_tests_final") or {}).get("value")
            or (completed.get("run_tests") or {}).get("value")
            or {}
        )
        commit = (completed.get("commit_changes") or {}).get("value", {})
        pr = (completed.get("create_pr") or {}).get("value", {})
        return cls(
            item_id=int(plan.get("item_id", 0)),
            branch_name=str(branch.get("branch_name", "")),
            pr_number=pr.get("pr_number"),
            pr_url=pr.get("pr_url"),
            files_changed=[Path(p) for p in commit.get("files_changed", [])],
            tests_passed=tests.get("passed"),
            test_output_summary=str(tests.get("summary", "")),
            dry_run=bool(plan.get("dry_run", False)),
        )


# ---- coder-agent contract --------------------------------------------


class FileChange(BaseModel):
    """One edit the coder agent asks us to apply."""

    path: str = Field(
        ...,
        description="Path relative to the repo root; no '..', no absolute paths.",
    )
    operation: Literal["create", "modify", "delete"]
    content: str | None = Field(
        None,
        description="Full file content for create/modify; None for delete.",
    )


class CoderOutput(BaseModel):
    """Structured response we ask the coder agent to emit.

    The agent is free to narrate inside ``intent_summary`` and ``notes``
    but the load-bearing field is ``file_changes`` — every entry there
    becomes a real disk mutation when ``apply_changes`` runs. The whole
    payload is validated by pydantic at the agent boundary; a malformed
    response surfaces as ``BadOutput`` and routes to the human gate.
    """

    intent_summary: str
    file_changes: list[FileChange] = Field(default_factory=list)
    notes: str = ""


CODER_SPEC = AgentSpec(
    name="coder",
    charter=(
        "You implement a leaf work-item plan against an existing codebase. "
        "Read the plan. Make the smallest set of file changes that satisfies "
        "it. Return a CoderOutput. Prefer modify over create. Never touch "
        "files outside the repo. If the plan is ambiguous, encode your "
        "interpretation in `notes` rather than guessing silently."
    ),
    response_model=CoderOutput,
)

CODER_REVISION_SPEC = AgentSpec(
    name="coder_revision",
    charter=(
        "Your prior attempt left tests red. You will be shown the test "
        "failure output and the prior intent_summary. Produce a new "
        "CoderOutput that addresses the failure. Do not regress the "
        "plan's other requirements. The next test run is final — if it "
        "fails again the workflow surrenders to a human reviewer."
    ),
    response_model=CoderOutput,
)


# ---- closed error_kind enum (ADR 0004 §4.2) ---------------------------


ERROR_KINDS: frozenset[str] = frozenset({
    "plan.not_found",
    "plan.fetch_failed",
    "workspace.dirty",
    "workspace.unreadable",
    "branch.create_failed",
    "branch.exists_foreign",
    "branch.probe_failed",
    "coder.no_changes",
    "coder.invalid_path",
    "coder.apply_failed",
    "tests.failed",
    "tests.error",
    "tests.undetected",
    "commit.failed",
    "push.failed",
    "pr.create_failed",
    "pr.search_failed",
    "pr.link_failed",
    "toolbelt.missing_client",
})
"""Every ``error_kind`` this workflow's verbs can emit, frozen at the
module level so the test suite can exhaustively cover them and the UI
has a finite vocabulary to render. Adding a new kind here is a
deliberate act (per ADR 0004 §4.2: amend the enum + ADR).

Note on ``NeedsHuman``: the brief calls for ``NeedsHuman`` from several
verbs (branch-already-exists, fetch-plan-unknown, etc.). The kernel
shipped at this seat has a known limitation: it reads ``.prompt`` /
``.options`` off the *node* in ``_AwaitingGate`` rather than off the
emitted ``gate_opened`` event, so a ``ScriptNode`` returning
``NeedsHuman`` crashes the engine. Until that is fixed (out of scope
for the implementation seat), we emit ``PermanentFailure`` with a
descriptive error_kind and route to ``end_handoff``. Operationally
identical: the workflow halts and a human owns the next step."""


# ---- path-safety: the wall between the agent and the filesystem -------


def _validate_relative_path(p: str) -> Path | None:
    """Return a safe ``Path`` (relative, no ``..`` components) or ``None``.

    Refuses absolute paths (POSIX-style ``/abs`` AND Windows-style
    ``C:\\abs``), paths starting with a path separator (which Python's
    ``Path.is_absolute()`` treats as "not absolute" on Windows even
    though they clearly escape the relative-path contract), paths
    containing ``..`` segments, and paths that empty out after
    normalization. The agent only ever names files *inside* the repo
    root; anything else is a protocol violation that must reach a
    human, not silently land on disk.
    """
    if not p or not p.strip():
        return None
    # Cross-platform absolute-prefix check: leading `/` or `\` is "rooted"
    # even on Windows, where Path.is_absolute() returns False for them.
    if p[0] in ("/", "\\"):
        return None
    candidate = Path(p)
    if candidate.is_absolute():
        return None
    parts = candidate.parts
    if any(part == ".." for part in parts):
        return None
    if any(part.strip() == "" for part in parts):
        return None
    return candidate


# ---- test-command auto-detection -------------------------------------


def detect_test_command(repo_path: Path) -> str | None:
    """Best-effort guess at the project's test command.

    Detection rules (first match wins):

    * ``pyproject.toml`` or ``setup.py`` or ``setup.cfg`` → ``"pytest -q"``
    * ``package.json``                                    → ``"npm test"``
    * any ``*.csproj`` or ``*.sln`` at the root           → ``"dotnet test --no-build"``

    Returns ``None`` if nothing matched — the caller should then refuse
    to run, since running an unguessed test surface "best-effortly"
    would violate INV-NO-CORRUPT-FORWARD.
    """
    if (repo_path / "pyproject.toml").exists():
        return "pytest -q"
    if (repo_path / "setup.py").exists():
        return "pytest -q"
    if (repo_path / "setup.cfg").exists():
        return "pytest -q"
    if (repo_path / "package.json").exists():
        return "npm test"
    if any(repo_path.glob("*.csproj")) or any(repo_path.glob("*.sln")):
        return "dotnet test --no-build"
    return None


def _summarize_test_output(out: str, *, max_lines: int = 50) -> str:
    """Last ``max_lines`` of combined stdout+stderr, joined with newlines."""
    lines = out.splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(["… (truncated)"] + lines[-max_lines:])


# ---- workflow-input value object -------------------------------------


@dataclass(frozen=True, slots=True)
class ImplementationInputs:
    """Bundle of `requiem run` parameters that don't fit on the CLI.

    Carried via closure into the verb registry — verbs reach in for the
    item id, target repo, etc. Frozen so a partial run can't mutate its
    own inputs out from under itself on resume.
    """

    item_id: int
    repo: str
    repo_path: Path
    base_branch: str = "main"
    coder_agent_id: str = "coder"
    test_command: str | None = None
    dry_run: bool = False


# ---- test runner (deliberately not a Toolbelt client for v0) ----------
#
# Running an arbitrary shell command is a different security model from
# the other Toolbelt clients (twig/gh/git all speak narrow argv shapes).
# Keeping it inline as a workflow-private helper makes the blast radius
# explicit and lets the test suite swap it out with a function override
# (``build_engine(..., test_runner=fake)``).


@dataclass(frozen=True, slots=True)
class TestRunResult:
    # pytest tries to collect any top-level class whose name starts
    # with "Test" — opt out, this is a data carrier, not a test class.
    __test__ = False

    passed: bool
    summary: str
    full_output: str


def _default_test_runner(command: str, cwd: Path) -> TestRunResult:
    """Run ``command`` in ``cwd`` and classify on exit code.

    Uses ``shell=True`` because the brief specifies test commands as
    free-form strings (``"pytest -q"``, ``"dotnet test --no-build"``).
    The blast radius is "anything the operator's shell can do as the
    operator" — this is acceptable because the operator authored the
    workflow input. We do not log the command to the event log in raw
    form to keep secrets-in-env from leaking.
    """
    completed = subprocess.run(  # noqa: S602
        command,
        cwd=str(cwd),
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )
    out = (completed.stdout or "") + (completed.stderr or "")
    return TestRunResult(
        passed=completed.returncode == 0,
        summary=_summarize_test_output(out),
        full_output=out,
    )


# ---- verb registry ---------------------------------------------------


def build_verb_registry(
    inputs: ImplementationInputs,
    *,
    test_runner=_default_test_runner,
) -> VerbRegistry:
    verbs = VerbRegistry()

    branch_name = f"feature/{inputs.item_id}"

    # ---- helpers (closure-shared so verbs read consistent state) -----

    def _require_fs(ctx) -> FilesystemClient | PermanentFailure:
        fs = ctx.toolbelt.fs
        if fs is None:
            return PermanentFailure(
                error_kind="toolbelt.missing_client",
                message="implementation workflow requires toolbelt.fs",
            )
        return fs

    def _require_twig(ctx) -> TwigClient | PermanentFailure:
        twig = ctx.toolbelt.twig
        if twig is None:
            return PermanentFailure(
                error_kind="toolbelt.missing_client",
                message="implementation workflow requires toolbelt.twig",
            )
        return twig

    def _require_gh(ctx) -> GhClient | PermanentFailure:
        gh = ctx.toolbelt.gh
        if gh is None:
            return PermanentFailure(
                error_kind="toolbelt.missing_client",
                message="implementation workflow requires toolbelt.gh",
            )
        return gh

    # ---- start --------------------------------------------------------

    @verbs.register("start_run")
    def _start(ctx):
        return Success(value={
            "intent": "implementation",
            "item_id": inputs.item_id,
            "repo": inputs.repo,
            "repo_path": str(inputs.repo_path),
            "base_branch": inputs.base_branch,
            "dry_run": inputs.dry_run,
        })

    # ---- fetch_plan ---------------------------------------------------

    @verbs.register("fetch_plan")
    async def _fetch_plan(ctx):
        twig = _require_twig(ctx)
        if isinstance(twig, PermanentFailure):
            return twig
        try:
            item = await twig.show_async(inputs.item_id)
        except TwigItemNotFoundError as e:
            return PermanentFailure(
                error_kind="plan.not_found",
                message=f"twig item {inputs.item_id}: {e}",
            )
        except TwigClientError as e:
            # Ravel L-1: anything we couldn't classify must reach a human.
            # We route via PermanentFailure → end_handoff (see ERROR_KINDS
            # docstring on why we don't return NeedsHuman from a script).
            return PermanentFailure(
                error_kind="plan.fetch_failed",
                message=f"twig.show({inputs.item_id}) failed unexpectedly: {e}",
                details={"error": str(e)},
            )
        # The plan content is, in order of preference, an inline
        # description on the item, the literal title, or "(empty plan)".
        plan_text = (
            item.raw.get("description")
            or item.raw.get("plan")
            or item.title
            or "(empty plan)"
        )
        return Success(
            value={
                "item_id": item.id,
                "title": item.title,
                "state": item.state,
                "plan_text": plan_text,
                "repo": inputs.repo,
                "repo_path": str(inputs.repo_path),
                "dry_run": inputs.dry_run,
            },
            inspected_artifacts=(f"twig:item:{item.id}",),
        )

    # ---- assert_clean_workspace --------------------------------------

    @verbs.register("assert_clean_workspace")
    async def _assert_clean(ctx):
        fs = _require_fs(ctx)
        if isinstance(fs, PermanentFailure):
            return fs
        try:
            dirty_lines = await fs.git_status_porcelain()
        except FsClientError as e:
            return PermanentFailure(
                error_kind="workspace.unreadable",
                message=f"could not read git status: {e}",
                details={"error": str(e)},
            )
        if dirty_lines:
            return PermanentFailure(
                error_kind="workspace.dirty",
                message=(
                    f"workspace has {len(dirty_lines)} uncommitted change(s); "
                    "implementation refuses to run over dirty state"
                ),
                details={"porcelain": dirty_lines[:20]},
            )
        return Success(
            value={"clean": True},
            inspected_artifacts=(f"git:{inputs.repo_path}:status",),
        )

    # ---- create_branch ------------------------------------------------

    @verbs.register("create_branch")
    async def _create_branch(ctx):
        if inputs.dry_run:
            return Success(value={
                "branch_name": branch_name,
                "created": False,
                "dry_run": True,
            })
        fs = _require_fs(ctx)
        if isinstance(fs, PermanentFailure):
            return fs
        try:
            exists = await fs.git_branch_exists(branch_name)
            current = await fs.git_current_branch()
        except FsGitError as e:
            return PermanentFailure(
                error_kind="branch.probe_failed",
                message=f"git probe failed: {e.stderr.strip() or e}",
                details={"stderr": e.stderr},
            )
        # Idempotency: on resume we may already be on the branch.
        if exists and current == branch_name:
            return Success(value={
                "branch_name": branch_name,
                "created": False,
                "already_on_branch": True,
            })
        # An existing branch we are not on means a prior run (or a
        # human) left state behind. We refuse to auto-checkout to
        # protect INV-NO-CORRUPT-FORWARD — the human decides whether
        # to delete-and-recreate or resume the prior attempt.
        if exists:
            return PermanentFailure(
                error_kind="branch.exists_foreign",
                message=(
                    f"branch {branch_name!r} already exists locally but "
                    f"HEAD is on {current!r}. Resolve manually before retry."
                ),
                details={"branch": branch_name, "current": current},
            )
        try:
            await fs.git_create_branch(branch_name, inputs.base_branch)
        except FsGitError as e:
            return PermanentFailure(
                error_kind="branch.create_failed",
                message=f"git checkout -b {branch_name} {inputs.base_branch} failed: {e.stderr.strip() or e}",
                details={"stderr": e.stderr},
            )
        return Success(
            value={"branch_name": branch_name, "created": True},
            inspected_artifacts=(f"git:{inputs.repo_path}:branch:{branch_name}",),
        )

    # ---- prompts (agent inputs) --------------------------------------

    @verbs.register("coder_prompt")
    def _coder_prompt(ctx):
        plan = ctx.completed["fetch_plan"]["value"]
        return (
            f"# Work item AB#{plan['item_id']}: {plan['title']}\n\n"
            f"## Plan\n\n{plan['plan_text']}\n\n"
            f"## Repository\n\n"
            f"Local path: {plan['repo_path']}\n"
            f"GitHub: {plan['repo']}\n\n"
            "Return a CoderOutput with the minimal set of file_changes "
            "that satisfies the plan."
        )

    @verbs.register("coder_revision_prompt")
    def _coder_revision_prompt(ctx):
        plan = ctx.completed["fetch_plan"]["value"]
        prior = ctx.completed.get("invoke_coder", {}).get("value", {})
        tests = ctx.completed.get("run_tests", {}).get("value", {})
        prior_summary = (prior.get("parsed") or {}).get(
            "intent_summary", "(no prior intent)"
        )
        return (
            f"# Work item AB#{plan['item_id']}: {plan['title']}\n\n"
            f"## Plan\n\n{plan['plan_text']}\n\n"
            f"## Prior attempt\n\n{prior_summary}\n\n"
            f"## Test failure (last 50 lines)\n\n```\n{tests.get('summary', '')}\n```\n\n"
            "Produce a CoderOutput that fixes the failure without regressing "
            "the plan."
        )

    # ---- apply_changes (both first and revision use this) ------------

    def _apply_changes_impl(
        ctx,
        coder_node: str,
        *,
        dry_run_passthrough: bool,
    ) -> Outcome:
        if inputs.dry_run and dry_run_passthrough:
            parsed = (
                ctx.completed.get(coder_node, {})
                .get("value", {})
                .get("parsed", {})
            )
            return Success(value={
                "applied_paths": [],
                "dry_run": True,
                "change_count": len(parsed.get("file_changes", [])),
            })
        fs = _require_fs(ctx)
        if isinstance(fs, PermanentFailure):
            return fs
        parsed = ctx.completed[coder_node]["value"]["parsed"]
        raw_changes = parsed.get("file_changes") or []
        if not raw_changes:
            return PermanentFailure(
                error_kind="coder.no_changes",
                message=f"coder agent returned 0 file_changes ({coder_node})",
            )
        applied: list[str] = []
        for entry in raw_changes:
            rel = _validate_relative_path(entry.get("path", ""))
            if rel is None:
                return PermanentFailure(
                    error_kind="coder.invalid_path",
                    message=(
                        f"refusing to apply path {entry.get('path')!r}: "
                        "must be a relative path without '..' segments"
                    ),
                    details={"path": entry.get("path")},
                )
            target = (inputs.repo_path / rel).resolve()
            try:
                # Belt-and-brace: the resolved path must still live inside
                # the repo. Catches cleverness like symlinks that point
                # outside the worktree.
                target.relative_to(inputs.repo_path.resolve())
            except ValueError:
                return PermanentFailure(
                    error_kind="coder.invalid_path",
                    message=f"path escapes repo root: {entry.get('path')!r}",
                    details={"path": entry.get("path")},
                )
            op = entry.get("operation")
            content = entry.get("content")
            try:
                if op == "delete":
                    if target.exists():
                        target.unlink()
                elif op in ("create", "modify"):
                    if content is None:
                        return PermanentFailure(
                            error_kind="coder.apply_failed",
                            message=(
                                f"operation {op!r} on {rel} requires content"
                            ),
                        )
                    fs.write_text(target, content)
                else:
                    return PermanentFailure(
                        error_kind="coder.apply_failed",
                        message=f"unknown operation {op!r} on {rel}",
                    )
            except FsClientError as e:
                return PermanentFailure(
                    error_kind="coder.apply_failed",
                    message=f"writing {rel}: {e}",
                    details={"path": str(rel)},
                )
            applied.append(str(rel))
        return Success(
            value={"applied_paths": applied, "change_count": len(applied)},
            inspected_artifacts=tuple(f"file:{p}" for p in applied),
        )

    @verbs.register("apply_changes")
    def _apply_changes(ctx):
        return _apply_changes_impl(
            ctx, "invoke_coder", dry_run_passthrough=True
        )

    @verbs.register("apply_changes_revision")
    def _apply_changes_revision(ctx):
        return _apply_changes_impl(
            ctx, "invoke_coder_revision", dry_run_passthrough=False
        )

    # ---- run_tests ----------------------------------------------------

    def _run_tests_impl(ctx) -> Outcome:
        if inputs.dry_run:
            return Success(value={
                "passed": True,
                "summary": "(dry-run: tests not executed)",
                "command": inputs.test_command or "(auto-detect skipped)",
            })
        cmd = inputs.test_command or detect_test_command(inputs.repo_path)
        if cmd is None:
            return PermanentFailure(
                error_kind="tests.undetected",
                message=(
                    f"could not auto-detect a test command in {inputs.repo_path}; "
                    "pass test_command explicitly"
                ),
            )
        try:
            result = test_runner(cmd, inputs.repo_path)
        except Exception as e:  # noqa: BLE001
            # A crashed runner (binary not found, etc.) is distinct from
            # tests failing: it's "we don't know what the truth is".
            # Per INV-NO-CORRUPT-FORWARD the human decides.
            return PermanentFailure(
                error_kind="tests.error",
                message=f"test runner crashed: {type(e).__name__}: {e}",
            )
        if result.passed:
            return Success(value={
                "passed": True,
                "summary": result.summary,
                "command": cmd,
            })
        return PermanentFailure(
            error_kind="tests.failed",
            message=f"tests failed via {cmd!r}",
            details={
                "passed": False,
                "summary": result.summary,
                "command": cmd,
            },
        )

    @verbs.register("run_tests")
    def _run_tests(ctx):
        out = _run_tests_impl(ctx)
        # The router consumes outcome variants; we additionally surface
        # `summary` in the *failure details* so the revision prompt can
        # quote it. The route helper `run_tests` reads `value` for the
        # pass path and `details` for the fail path.
        return out

    @verbs.register("run_tests_final")
    def _run_tests_final(ctx):
        # Same logic as run_tests, but lives as a distinct node so the
        # graph is acyclic and the revision branch can't loop.
        return _run_tests_impl(ctx)

    # ---- commit_changes ----------------------------------------------

    @verbs.register("commit_changes")
    async def _commit_changes(ctx):
        if inputs.dry_run:
            return Success(value={
                "sha": None,
                "files_changed": [],
                "dry_run": True,
            })
        fs = _require_fs(ctx)
        if isinstance(fs, PermanentFailure):
            return fs
        plan = ctx.completed["fetch_plan"]["value"]
        try:
            if await fs.git_is_clean():
                # Idempotent resume: a prior commit already landed.
                return Success(value={
                    "sha": None,
                    "files_changed": [
                        str(p) for p in await fs.git_diff_name_only(inputs.base_branch)
                    ],
                    "already_committed": True,
                })
            await fs._git("add", "-A")  # noqa: SLF001 — staging shortcut
            message = f"impl: AB#{plan['item_id']} {plan['title']}"
            sha = await fs.git_commit(message)
            changed = await fs.git_diff_name_only(inputs.base_branch)
            numstat = await fs.git_diff_numstat(inputs.base_branch)
        except FsGitError as e:
            return PermanentFailure(
                error_kind="commit.failed",
                message=f"git commit failed: {e.stderr.strip() or e}",
                details={"stderr": e.stderr},
            )
        return Success(
            value={
                "sha": sha,
                "files_changed": [str(p) for p in changed],
                "numstat": [
                    {"path": str(p), "additions": a, "deletions": d}
                    for p, a, d in numstat
                ],
            },
            inspected_artifacts=(f"git:commit:{sha}",),
        )

    # ---- push_branch --------------------------------------------------

    @verbs.register("push_branch")
    async def _push_branch(ctx):
        if inputs.dry_run:
            return Success(value={"pushed": False, "dry_run": True})
        fs = _require_fs(ctx)
        if isinstance(fs, PermanentFailure):
            return fs
        try:
            await fs.git_push("origin", branch_name)
        except FsGitError as e:
            return PermanentFailure(
                error_kind="push.failed",
                message=f"git push origin {branch_name} failed: {e.stderr.strip() or e}",
                details={"stderr": e.stderr},
            )
        return Success(value={"pushed": True, "remote": "origin", "branch": branch_name})

    # ---- create_pr ----------------------------------------------------

    @verbs.register("create_pr")
    async def _create_pr(ctx):
        if inputs.dry_run:
            return Success(value={
                "pr_number": None,
                "pr_url": None,
                "dry_run": True,
            })
        gh = _require_gh(ctx)
        if isinstance(gh, PermanentFailure):
            return gh

        plan = ctx.completed["fetch_plan"]["value"]
        commit = ctx.completed.get("commit_changes", {}).get("value", {})
        title = f"AB#{plan['item_id']}: {plan['title']}"
        body_lines = [
            f"Implements AB#{plan['item_id']} — {plan['title']}.",
            "",
            "## Plan",
            "",
            plan.get("plan_text", "(empty)"),
            "",
            "## Files changed",
        ]
        for p in commit.get("files_changed", []):
            body_lines.append(f"- `{p}`")
        body_lines.extend([
            "",
            "Generated by the Requiem implementation workflow. The PR-lifecycle",
            "workflow (Gluck) takes over from here.",
        ])
        body = "\n".join(body_lines)

        # Idempotency: if a PR already exists on this head branch
        # (we crashed between create_pr and link_pr_to_item on a
        # prior run) reuse it instead of creating a duplicate.
        try:
            existing = await gh.pr_search(
                inputs.repo,
                query=f"head:{branch_name} state:open",
                limit=5,
            )
        except GhClientError as e:
            # Search failing is not the same as "no PR" — escalate.
            return PermanentFailure(
                error_kind="pr.search_failed",
                message=f"gh pr list failed: {e}",
                details={"error": str(e)},
            )
        for pr in existing:
            if pr.head == branch_name:
                return Success(
                    value={
                        "pr_number": pr.number,
                        "pr_url": pr.url,
                        "title": pr.title,
                        "reused_existing": True,
                    },
                    inspected_artifacts=(f"gh:pr:{inputs.repo}#{pr.number}",),
                )
        try:
            pr = await gh.pr_create(
                inputs.repo,
                title=title,
                body=body,
                head=branch_name,
                base=inputs.base_branch,
            )
        except GhClientError as e:
            return PermanentFailure(
                error_kind="pr.create_failed",
                message=f"gh pr create failed: {e}",
                details={"error": str(e)},
            )
        return Success(
            value={
                "pr_number": pr.number,
                "pr_url": pr.url,
                "title": pr.title,
                "reused_existing": False,
            },
            inspected_artifacts=(f"gh:pr:{inputs.repo}#{pr.number}",),
        )

    # ---- link_pr_to_item ---------------------------------------------

    @verbs.register("link_pr_to_item")
    async def _link_pr(ctx):
        if inputs.dry_run:
            return Success(value={"linked": False, "dry_run": True})
        pr = ctx.completed.get("create_pr", {}).get("value", {})
        url = pr.get("pr_url")
        if not url:
            return Success(value={"linked": False, "reason": "no PR url to link"})
        twig = _require_twig(ctx)
        if isinstance(twig, PermanentFailure):
            return twig
        try:
            await twig.comment_async(
                inputs.item_id,
                f"PR opened by Requiem implementation workflow: {url}",
            )
        except TwigClientError as e:
            # Best-effort: a failed link does NOT block the PR from
            # going to human review. The verdict card records the gap;
            # the human can re-link manually. We surface as a
            # PermanentFailure, but the workflow's edge from
            # link_pr_to_item on permanent_failure routes to
            # end_handoff anyway (the PR is already open).
            return PermanentFailure(
                error_kind="pr.link_failed",
                message=f"twig comment failed: {e}",
                details={"pr_url": url, "error": str(e)},
            )
        return Success(
            value={"linked": True, "pr_url": url},
            inspected_artifacts=(f"twig:item:{inputs.item_id}",),
        )

    return verbs


# ---- agent registry & FakeProvider scripts ----------------------------


def build_agent_registry() -> AgentRegistry:
    reg = AgentRegistry()
    reg.register(CODER_SPEC)
    reg.register(CODER_REVISION_SPEC)
    return reg


def happy_path_provider() -> FakeProvider:
    """The canonical demo path: one file change, tests pass first time.

    Used by ``build_engine`` when no provider is supplied. The brief's
    sample workflow ships a single trivial edit; the real implementation
    workflow's value comes from running this against a real coder agent
    via Mozart's provider.
    """
    return FakeProvider(scripts={
        "coder": [
            {
                "intent_summary": "Add a docstring to outcomes.py",
                "file_changes": [
                    {
                        "path": "REQUIEM_IMPLEMENTATION_DEMO.md",
                        "operation": "create",
                        "content": "# Requiem implementation-workflow demo\n\nGenerated by the happy-path FakeProvider.\n",
                    }
                ],
                "notes": "",
            }
        ],
        "coder_revision": [],
    })


# ---- workflow topology -----------------------------------------------


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder(
            "implementation",
            module="requiem.workflows.implementation",
            version="0.1",
        )
            .entry("start")
            .script("start", verb="start_run")
                .edge("start", on="success", to="fetch_plan")
            .script("fetch_plan", verb="fetch_plan")
                .edge("fetch_plan", on="success", to="assert_clean_workspace")
                .edge("fetch_plan", on="permanent_failure", to="end_failed")
            .script("assert_clean_workspace", verb="assert_clean_workspace")
                .edge("assert_clean_workspace", on="success", to="create_branch")
                .edge("assert_clean_workspace", on="permanent_failure", to="end_failed")
            .script("create_branch", verb="create_branch")
                .edge("create_branch", on="success", to="invoke_coder")
                .edge("create_branch", on="permanent_failure:branch.exists_foreign", to="end_handoff")
                .edge("create_branch", on="permanent_failure:branch.probe_failed", to="end_handoff")
                .edge("create_branch", on="permanent_failure", to="end_failed")
            .agent("invoke_coder", agent="coder", prompt_verb="coder_prompt")
                .edge("invoke_coder", on="success", to="apply_changes")
                .edge("invoke_coder", on="bad_output", to="end_handoff")
                .edge("invoke_coder", on="permanent_failure", to="end_failed")
            .script("apply_changes", verb="apply_changes")
                .edge("apply_changes", on="success", to="run_tests")
                .edge("apply_changes", on="permanent_failure:coder.no_changes", to="end_failed")
                .edge("apply_changes", on="permanent_failure", to="end_handoff")
            .script("run_tests", verb="run_tests")
                .edge("run_tests", on="success", to="commit_changes")
                .edge("run_tests", on="permanent_failure:tests.failed", to="invoke_coder_revision")
                .edge("run_tests", on="permanent_failure", to="end_handoff")
            .agent(
                "invoke_coder_revision",
                agent="coder_revision",
                prompt_verb="coder_revision_prompt",
            )
                .edge("invoke_coder_revision", on="success", to="apply_changes_revision")
                .edge("invoke_coder_revision", on="bad_output", to="end_handoff")
                .edge("invoke_coder_revision", on="permanent_failure", to="end_handoff")
            .script("apply_changes_revision", verb="apply_changes_revision")
                .edge("apply_changes_revision", on="success", to="run_tests_final")
                .edge("apply_changes_revision", on="permanent_failure", to="end_handoff")
            .script("run_tests_final", verb="run_tests_final")
                .edge("run_tests_final", on="success", to="commit_changes")
                .edge("run_tests_final", on="permanent_failure", to="end_handoff")
            .script("commit_changes", verb="commit_changes")
                .edge("commit_changes", on="success", to="push_branch")
                .edge("commit_changes", on="permanent_failure", to="end_handoff")
            .script("push_branch", verb="push_branch")
                .edge("push_branch", on="success", to="create_pr")
                .edge("push_branch", on="permanent_failure", to="end_handoff")
            .script("create_pr", verb="create_pr")
                .edge("create_pr", on="success", to="link_pr_to_item")
                .edge("create_pr", on="permanent_failure", to="end_handoff")
            .script("link_pr_to_item", verb="link_pr_to_item")
                # pr.link_failed is best-effort: still hand off to the
                # reviewer (the PR already exists), don't fail the run.
                .edge("link_pr_to_item", on="success", to="end_handoff")
                .edge("link_pr_to_item", on="permanent_failure", to="end_handoff")
            .terminate("end_handoff", disposition="completed")
            .terminate("end_failed", disposition="failed")
            .humanize({
                "start":                   "Starting implementation",
                "fetch_plan":              "Fetched plan from twig",
                "assert_clean_workspace":  "Workspace clean",
                "create_branch":           "Created feature branch",
                "invoke_coder":            "Coder agent (first pass)",
                "apply_changes":           "Applied file changes",
                "run_tests":               "Ran tests",
                "invoke_coder_revision":   "Coder agent (revision)",
                "apply_changes_revision":  "Applied revised file changes",
                "run_tests_final":         "Re-ran tests after revision",
                "commit_changes":          "Committed changes",
                "push_branch":             "Pushed feature branch",
                "create_pr":               "Opened pull request",
                "link_pr_to_item":         "Linked PR to work item",
                "end_handoff":             "implementation",
                "end_failed":              "implementation",
            })
            .build()
    )


# ---- render hints + verdict card -------------------------------------


def _detail_fetch_plan(value: dict) -> str:
    return f"AB#{value.get('item_id', '?')} — {value.get('title', '?')}"


def _detail_create_branch(value: dict) -> str:
    if value.get("already_on_branch"):
        return f"already on {value.get('branch_name', '?')}"
    if value.get("dry_run"):
        return f"(dry-run) would create {value.get('branch_name', '?')}"
    return f"cut {value.get('branch_name', '?')}"


def _detail_apply_changes(value: dict) -> str:
    n = value.get("change_count", 0)
    if value.get("dry_run"):
        return f"(dry-run) would apply {n} change(s)"
    return f"{n} file(s)"


def _detail_run_tests(value: dict) -> str:
    cmd = value.get("command", "?")
    if value.get("passed"):
        return f"✓ {cmd}"
    return cmd


def _detail_commit(value: dict) -> str:
    files = value.get("files_changed", [])
    sha = value.get("sha")
    if value.get("dry_run"):
        return "(dry-run)"
    if value.get("already_committed"):
        return f"already committed ({len(files)} file(s))"
    short = (sha or "")[:7]
    return f"{short} · {len(files)} file(s)"


def _detail_push(value: dict) -> str:
    if value.get("dry_run"):
        return "(dry-run)"
    return f"{value.get('remote', '?')}/{value.get('branch', '?')}"


def _detail_create_pr(value: dict) -> str:
    if value.get("dry_run"):
        return "(dry-run)"
    n = value.get("pr_number")
    if n is None:
        return "(no PR)"
    if value.get("reused_existing"):
        return f"#{n} (reused)"
    return f"#{n}"


def _detail_link_pr(value: dict) -> str:
    if value.get("dry_run"):
        return "(dry-run)"
    if value.get("linked"):
        return "linked"
    return value.get("reason", "skipped")


def render_hints() -> dict:
    return {
        "artifact_name": "leaf plan",
        "details": {
            "fetch_plan":            _detail_fetch_plan,
            "create_branch":         _detail_create_branch,
            "apply_changes":         _detail_apply_changes,
            "apply_changes_revision": _detail_apply_changes,
            "run_tests":             _detail_run_tests,
            "run_tests_final":       _detail_run_tests,
            "commit_changes":        _detail_commit,
            "push_branch":           _detail_push,
            "create_pr":             _detail_create_pr,
            "link_pr_to_item":       _detail_link_pr,
        },
        # `start` and the terminate nodes are covered by run_started /
        # run_completed; suppressing avoids double narration.
        "silent_nodes": frozenset({"start", "end_handoff", "end_failed"}),
    }


def verdict_card(completed: dict) -> str | None:
    """Post-run summary matching the brief's happy-path verdict card."""
    plan = (completed.get("fetch_plan") or {}).get("value", {})
    if not plan:
        return None
    result = ImplementationResult.from_completed(completed)
    title = plan.get("title", "?")
    item_id = plan.get("item_id", "?")
    branch = result.branch_name or f"feature/{item_id}"
    files = result.files_changed
    numstat = (completed.get("commit_changes") or {}).get("value", {}).get(
        "numstat", []
    )

    # First files_changed line: rich (path +adds -dels). Subsequent
    # lines elided to count only — the full list is in the event log.
    if numstat:
        head = numstat[0]
        first = f"{head['path']} +{head['additions']} -{head['deletions']}"
        if len(numstat) > 1:
            files_summary = (
                f"{len(numstat)} files ({first}, +{len(numstat)-1} more)"
            )
        else:
            files_summary = f"1 file ({first})"
    else:
        files_summary = f"{len(files)} file(s)"

    tests_line = "—"
    tests = (
        (completed.get("run_tests_final") or {}).get("value")
        or (completed.get("run_tests") or {}).get("value")
        or {}
    )
    if result.tests_passed is True:
        tests_line = f"✓ passed ({tests.get('command', '?')})"
    elif result.tests_passed is False:
        tests_line = f"✗ failed ({tests.get('command', '?')})"
    elif result.dry_run:
        tests_line = "(dry-run: not executed)"

    pr_line = "—"
    if result.pr_number and result.pr_url:
        pr_line = f"#{result.pr_number} — {result.pr_url}"
    elif result.dry_run:
        pr_line = "(dry-run: not created)"

    # Did we surrender to a human or land cleanly?
    handed_off = bool(completed.get("link_pr_to_item")) or bool(
        completed.get("create_pr")
    )
    if handed_off and result.pr_number is not None:
        headline = "✓ Ready for review"
        next_line = (
            f"  → run PR lifecycle: requiem run requiem.workflows.pr_lifecycle "
            f"--pr {result.pr_number}"
        )
    elif result.dry_run:
        headline = "✓ Dry-run complete (no changes made)"
        next_line = "  → re-run without --dry-run to push and open the PR"
    else:
        headline = "⚠ Implementation surrendered to human review"
        next_line = "  → inspect the events.jsonl and the local branch"

    lines = [
        f"─── Implementation: AB#{item_id} ──────────────────────────────────────",
        f"  {headline}",
        f"      Item:        {item_id} — {title!r}",
        f"      Branch:      {branch}",
        f"      Changes:     {files_summary}",
        f"      Tests:       {tests_line}",
        f"      PR:          {pr_line}",
        "      Next:        awaiting human review",
        next_line,
        "─────────────────────────────────────────────────────────────────────",
    ]
    return "\n".join(lines)


# ---- engine factory (the `requiem run` contract) ---------------------


# CLI demo defaults. `requiem run requiem.workflows.implementation` with
# no extra inputs runs against a self-contained throwaway repo seeded
# under `log_dir`, against a fake twig client, fake gh client, and the
# happy-path FakeProvider. The real workflow is invoked programmatically
# with explicit ImplementationInputs.

def _make_demo_inputs(log_dir: Path) -> ImplementationInputs:
    repo_path = log_dir / "demo_repo"
    repo_path.mkdir(parents=True, exist_ok=True)
    if not (repo_path / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)
        subprocess.run(
            ["git", "config", "user.email", "demo@requiem.local"],
            cwd=repo_path, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Requiem Demo"],
            cwd=repo_path, check=True,
        )
        (repo_path / "README.md").write_text(
            "# implementation workflow demo repo\n", encoding="utf-8"
        )
        # Anchor a pyproject so detect_test_command picks `pytest -q`.
        (repo_path / "pyproject.toml").write_text(
            "[project]\nname = \"demo\"\nversion = \"0.0.1\"\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial"],
            cwd=repo_path, check=True,
        )
    return ImplementationInputs(
        item_id=12345,
        repo="PolyphonyRequiem/requiem",
        repo_path=repo_path,
        base_branch="master" if _detect_default_branch(repo_path) == "master" else "main",
        dry_run=True,  # The CLI demo is dry-run by default; no real PR.
    )


def _detect_default_branch(repo_path: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return r.stdout.strip() or "main"
    except subprocess.CalledProcessError:
        return "main"


@dataclass
class _DemoTwigClient:
    """In-memory TwigClient stand-in for the CLI demo / unit tests.

    Satisfies the methods our verbs call (``show_async``, ``comment_async``)
    without shelling out to a real `twig` binary. Tests that want to
    assert error paths instantiate this directly or pass their own
    duck-typed fake.
    """
    item_id: int = 12345
    title: str = "Demo: implementation-workflow walking skeleton"
    raise_on_show: Exception | None = None
    raise_on_comment: Exception | None = None
    comments: list[tuple[int, str]] = field(default_factory=list)

    async def show_async(self, item_id: int):
        if self.raise_on_show is not None:
            raise self.raise_on_show
        from requiem.clients.twig import TwigItem
        return TwigItem(
            id=item_id,
            title=self.title,
            state="Active",
            area_path="Demo\\Implementation",
            work_item_type="Task",
            parent_id=None,
            raw={
                "id": item_id,
                "title": self.title,
                "description": "Demo plan: create one marker file and stop.",
            },
        )

    async def comment_async(self, item_id: int, message: str) -> None:
        if self.raise_on_comment is not None:
            raise self.raise_on_comment
        self.comments.append((item_id, message))


@dataclass
class _DemoGhClient:
    """In-memory GhClient stand-in for the CLI demo / unit tests."""

    next_pr_number: int = 19
    existing: list[Any] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    raise_on_create: Exception | None = None
    raise_on_search: Exception | None = None

    async def pr_search(self, repo: str, query: str, limit: int = 30):
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return list(self.existing)

    async def pr_create(self, repo: str, *, title: str, body: str, head: str, base: str):
        if self.raise_on_create is not None:
            raise self.raise_on_create
        from requiem.clients.gh import GhPullRequest
        n = self.next_pr_number
        url = f"https://github.com/{repo}/pull/{n}"
        pr = GhPullRequest(
            number=n,
            title=title,
            state="OPEN",
            merged=False,
            merged_at=None,
            head=head,
            base=base,
            url=url,
            raw={"number": n, "title": title, "url": url},
        )
        self.created.append({
            "title": title, "body": body, "head": head, "base": base, "url": url
        })
        return pr


def build_engine(
    log_dir: Path,
    *,
    inputs: ImplementationInputs | None = None,
    provider: FakeProvider | None = None,
    toolbelt: Toolbelt | None = None,
    test_runner=None,
    gate_handler=None,
) -> Engine:
    """Construct a runnable Engine for the implementation workflow.

    Parameters mirror the brief. When called by the CLI with no extras,
    we synthesize a self-contained demo: a throwaway git repo under
    ``log_dir/demo_repo``, in-memory twig/gh clients, the happy-path
    FakeProvider, and ``dry_run=True`` so no PR is opened. Programmatic
    callers (tests, the eventual real production wiring) supply their
    own ``inputs``, ``toolbelt``, and ``provider``.
    """
    if inputs is None:
        inputs = _make_demo_inputs(log_dir)
    if provider is None:
        provider = happy_path_provider()
    if toolbelt is None:
        toolbelt = Toolbelt(
            git=RealGitClient(),
            files=FakeFileClient({}),
            gh=_DemoGhClient(),  # type: ignore[arg-type]
            fs=FilesystemClient(inputs.repo_path),
            twig=_DemoTwigClient(),  # type: ignore[arg-type]
        )
    runner = test_runner or _default_test_runner
    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(inputs, test_runner=runner),
        agents=build_agent_registry(),
        provider=provider,
        toolbelt=toolbelt,
        log_dir=log_dir,
        gate_handler=gate_handler or _default_gate_handler,
    )


def _default_gate_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    """Demo gate handler: picks the first option (typically ``retry`` or ``abort``).

    For the implementation workflow the most likely human gate is the
    ``end_handoff`` Terminate, which never invokes a handler — the
    Terminate node short-circuits in the kernel. But if any verb routes
    to a real NeedsHuman (e.g. branch already exists), we pick the
    safest default for the demo.
    """
    # `abort` exists in most option tuples; fall back to whatever's first.
    if "abort" in options:
        return "abort"
    return options[0] if options else "abort"


_default_gate_handler.__requiem_auto__ = True  # type: ignore[attr-defined]
