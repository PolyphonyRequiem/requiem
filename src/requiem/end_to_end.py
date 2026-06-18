"""End-to-end driver: one ADO work item → plan → seed → dispatch.

This is the thin operator command that makes "run Requiem against any ADO
work item" real. It is deliberately **not** a workflow: it runs the three
existing workflows as independent *top-level* engines and threads concrete
artifact paths between them. Running each as a top-level engine (rather than
nesting them as in-process sub-workflows) sidesteps ADR-0013 §B1 — every
stage gets its own real provider/toolbelt, nothing falls back to a fake.

Pipeline::

    planning(item)                          # recursive plan → .plan.tree.json | .plan.md
      ├─ leaf root (decomposable=False) ───→ dispatch the *root item itself*
      │                                       as the single implementable leaf
      └─ decomposable ──→ commit_plan(tree) # seed ADO children → .plan.committed.json
                            └─→ kanban_executor(tree, committed)  # fan-out to Hermes

Safety defaults mirror the workflows: ``commit`` (actually seed ADO children)
and ``live`` (actually spawn Hermes workers) are **off** by default. A
decomposable root with ``commit=False`` stops after planning with a "planned
only" verdict — it never invents real ids or dispatches. A real fan-out needs
a *committed* (non-dry-run) manifest, so the driver always seeds for real when
it proceeds to dispatch; ``live`` then governs only whether workers actually
spawn (the executor's own dispatch dry-run).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from requiem import branch_model
from requiem.kernel import Completed, Engine
from requiem.persistence import replay
from requiem.toolbelt import Toolbelt
from requiem.workflows import commit_plan as commit_plan_mod
from requiem.workflows import fanout as fanout_mod
from requiem.workflows import feature_pr as feature_pr_mod
from requiem.workflows import kanban_executor as executor_mod
from requiem.workflows import leaf_pr as leaf_pr_mod
from requiem.workflows import planning as planning_mod
from requiem.workflows import trunk_bootstrap as trunk_bootstrap_mod
from requiem.workflows.feature_pr import ItemDisposition, LeafPr
from requiem.workflows.kanban_executor import ExecInputs, LeafSpec

# Engine factories are injected (defaulting to the real ones) so the pipeline
# orchestration is unit-testable with stub engines that don't need an LLM/ADO.
PlanningFactory = Callable[..., Engine]
CommitFactory = Callable[..., Engine]
ExecutorFactory = Callable[..., Engine]
# ADR-0018 step 4: the three trunk-topology workflows the driver owns. Injected
# the same way (real build_engine by default) so the wiring is stub-testable.
TrunkBootstrapFactory = Callable[..., Engine]
LeafPrFactory = Callable[..., Engine]
FeaturePrFactory = Callable[..., Engine]


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Outcome of an end-to-end run.

    ``stage`` is the furthest stage reached (``planning`` / ``commit`` /
    ``executor``). ``status`` is one of ``delivered`` (executor finished at
    ``end``), ``planned`` (stopped after planning — leaf preview or a
    decomposable root without ``commit``), ``paused`` (a stage needed a human
    / failed). ``detail`` is human-readable.
    """

    item_id: int
    stage: str
    status: str
    detail: str
    decomposable: bool | None = None
    leaf_ids: tuple[str, ...] = ()
    plan_artifact: str | None = None
    committed_path: str | None = None
    executor_final_node: str | None = None
    # -- ADR-0018 step 4: trunk-topology projections (populated only when a
    #    github_repo OR ado_repo is threaded; all None/empty on the legacy
    #    creds-light path) --
    github_repo: str | None = None
    ado_repo: str | None = None               # ADR-0024 step 5
    base_branch: str | None = None
    trunk_branch: str | None = None
    trunk_verdict: str | None = None          # created | exists | previewed | failed
    leaf_pr_verdict: str | None = None        # opened | previewed | needs_human | failed
    leaf_pr_map: tuple[tuple[str, int | None], ...] = ()  # (leaf_id, pr_number)
    leaf_pr_map_path: str | None = None       # persisted {leaf_id: pr_number} artifact
    feature_pr_verdict: str | None = None     # opened | previewed | needs_human | failed
    feature_pr_number: int | None = None
    feature_pr_url: str | None = None
    # In-process fan-out backend (ADR-0021) roll-up.
    fanout_verdict: str | None = None         # all_landed | previewed | needs_human | failed | no_leaves
    fanout_leaves_total: int = 0
    fanout_leaves_landed: int = 0


def _completed_map(log_dir: Path, run_id: str) -> dict[str, dict]:
    """Reconstruct the node→outcome map from a finished engine's durable log."""
    completed: dict[str, dict] = {}
    for ev in replay(log_dir / f"{run_id}.events.jsonl"):
        if ev.get("kind") == "verb_completed":
            completed[ev["node_id"]] = ev["payload"]["outcome"]
    return completed


def _gate_opened(log_dir: Path, run_id: str) -> str | None:
    """Return the node_id of the last gate_opened event in a run's log, if any.

    A ``NeedsHuman`` gate records a ``gate_opened`` event (not a Success outcome),
    so this is how the driver learns *which* node fired the human gate — e.g.
    ``verify_readiness`` (a laggard leaf) vs ``verify_dispositions`` (an
    unsatisfied requirement). Returns None if no gate opened.
    """
    node_id: str | None = None
    for ev in replay(log_dir / f"{run_id}.events.jsonl"):
        if ev.get("kind") == "gate_opened":
            node_id = ev.get("node_id")
    return node_id


def _plan_record(completed: dict[str, dict]) -> dict[str, Any] | None:
    """Pull the planning record value (approved or needs-human), if present."""
    for node in ("record_plan", "record_needs_human"):
        block = completed.get(node)
        if block and block.get("kind") == "success":
            return block.get("value")
    return None


# ---- ADR-0018 step 4: trunk-topology helpers -------------------------------
#
# These run only when the driver is given a `github_repo` ("Owner/Repo"). On the
# legacy creds-light path (no github_repo) they are never reached, so the
# executor-only pipeline behaves exactly as before. Each honours the driver's
# `live` flag by threading it as the workflow's `dry_run = not live`, keeping a
# dry run genuinely side-effect-free (no ref create, no PR open).


def _resolve_repo_target(
    *,
    github_repo: str | None,
    ado_repo: str | None,
    gh: Any | None,
) -> tuple[str | None, Any | None]:
    """ADR-0024 step 5: resolve the operator's choice of (--github-repo |
    --ado-repo) into a single internal (repo_id, repo_client) pair.

    Mutually exclusive — passing both raises a ValueError so the caller
    fails closed instead of silently routing one half to one platform and
    the other half to the other.

    Returns ``(repo_id, repo_client)``:
    - ``(None, None)`` when neither is set → executor-only path, the
      legacy creds-light behaviour.
    - ``(github_repo, gh-or-real-GhClient)`` when ``github_repo`` is set.
    - ``(ado_repo, AdoClient())`` when ``ado_repo`` is set. The credential
      chain (explicit → PAT → env → AzureCliCredential) is the one from
      ADR-0024 step 1; the operator needs ``az login`` for the default
      path.
    """
    if github_repo is not None and ado_repo is not None:
        raise ValueError(
            "github_repo and ado_repo are mutually exclusive — pass one or "
            "the other (or neither, for the executor-only path)"
        )
    if github_repo is not None:
        # Reuse the injected gh client when given; otherwise fall through
        # to the real GhClient that Toolbelt.real() returns. (Mirrors the
        # existing _gh_toolbelt() behaviour.)
        if gh is not None:
            return github_repo, gh
        return github_repo, Toolbelt.real().gh
    if ado_repo is not None:
        # Lazy import — AdoClient lives in clients/azuredevops.py and
        # importing it at module top would pull azure-identity into every
        # end_to_end import even when the operator never uses ADO. Step 1
        # made azure-identity an optional dep; keep the import lazy so
        # GitHub-only operators don't see surprising ImportErrors.
        from requiem.clients.azuredevops import AdoClient
        return ado_repo, AdoClient()
    return None, None


def _topology_toolbelt(
    twig: Any | None, repo_client: Any | None,
) -> Toolbelt:
    """A toolbelt carrying a real (or injected) RepoPlatform client for
    the trunk-topology steps.

    Replaces the pre-step-5 ``_gh_toolbelt``: same intent, but the
    client is now wired via ``toolbelt.repo`` instead of
    ``toolbelt.gh``. The trunk-topology workflows after step 4 read
    from ``toolbelt.repo`` first and fall back to ``toolbelt.gh``, so
    the back-compat path is preserved for any caller still wiring via
    the old field name.
    """
    real = Toolbelt.real()
    return Toolbelt(
        git=real.git,
        files=real.files,
        # repo_client may be a GhClient or an AdoClient; both implement
        # RepoPlatform per ADR-0024 step 3.
        repo=repo_client if repo_client is not None else real.repo,
        # Keep gh wired too — the legacy back-compat path still resolves
        # through it, AND GhClient-specific callers (close_out etc.) read
        # it directly. When the caller is on ADO, gh stays the real
        # GhClient from Toolbelt.real() (separate instance from `repo`).
        gh=real.gh,
        twig=twig if twig is not None else real.twig,
        kanban=real.kanban,
    )


# Back-compat alias — older internal callers still imported _gh_toolbelt.
# Keep as a thin wrapper that ignores any explicit `gh` arg and routes
# through the new platform-agnostic helper. The new helper is the
# preferred name; this alias may be removed in a fast-follow once
# nothing in tree references it.
def _gh_toolbelt(twig: Any | None, gh: Any | None) -> Toolbelt:
    return _topology_toolbelt(twig, gh)


async def _resolve_base_branch_via_platform(
    repo_id: str, repo_client: Any | None, fallback: str = "main",
) -> str:
    """ADR-0024 step 5: resolve the repo's default branch via the
    RepoPlatform Protocol method ``default_branch``.

    Falls back to ``fallback`` if the client is absent or the probe
    fails for any reason: trunk_bootstrap's ``branch_sha`` will re-
    validate fail-closed against any incorrect guess, so the cost of
    falling through to ``"main"`` and being wrong is a clean failure
    one stage later (not a corrupt-forward).

    The pre-step-5 helper ``_resolve_base_branch`` reached into
    ``gh.api("repos/<repo>")`` directly — a GitHub-only surface. The
    new path goes through ``RepoPlatform.default_branch`` which both
    GhClient and AdoClient implement uniformly.
    """
    if repo_client is None:
        # No client wired — fall back to the real Toolbelt's GhClient
        # only if the repo_id looks like a GitHub identifier (two slashes
        # means ADO; one slash means GitHub).
        if "/" not in repo_id:
            return fallback
        if repo_id.count("/") >= 2:
            # ADO without a client wired — caller didn't pass one; no
            # safe fallback. Use the static fallback rather than guess.
            return fallback
        repo_client = Toolbelt.real().repo
    if repo_client is None:
        return fallback
    try:
        return await repo_client.default_branch(repo_id)
    except Exception:
        return fallback


# Back-compat alias for tests that import _resolve_base_branch directly.
async def _resolve_base_branch(
    github_repo: str, gh: Any | None, fallback: str = "main",
) -> str:
    return await _resolve_base_branch_via_platform(github_repo, gh, fallback)


def _persist_leaf_pr_map(
    log_dir: Path, item_id: int, leaves: tuple[LeafPr, ...],
) -> Path:
    """Persist the authoritative {leaf_id: pr_number} map as a stage artifact.

    The briefing + ADR-0018 step 2 call this out explicitly: a default
    `gh pr list` is open-only and cannot re-derive merged leaf PR numbers, so
    feature_pr's input must come from this persisted map, not a re-query. We
    follow the per-stage artifact pattern the rest of the driver already uses.
    """
    path = log_dir / f"leaf-pr-map-{item_id}.json"
    payload = {
        "item_id": item_id,
        "leaves": [
            {"leaf_id": lp.leaf_id, "pr_number": lp.pr_number} for lp in leaves
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


async def _dispatch_in_process(
    *,
    item_id: int,
    log_dir: Path,
    decomposable: bool,
    plan_artifact: Any,
    committed_path: Path | None,
    plan_record: dict,
    repo_path: Path | None,
    github_repo: str | None,         # unified repo_id, either GH or ADO
    ado_repo_arg: str | None = None, # operator-supplied ado_repo (for PipelineResult)
    github_repo_arg: str | None = None, # operator-supplied github_repo (for PipelineResult)
    repo_client: Any | None = None,  # ADR-0024: RepoPlatform impl for the topology toolbelt
    base_branch: str | None,
    twig: Any,
    gh: Any,
    provider: Any,
    gate_handler: Any,
    live: bool,
    fanout_parallel: bool,
    trunk_branch: str | None,
    trunk_verdict: str | None,
    fanout_factory: Any,
) -> "PipelineResult":
    """Phase 3 (in-process backend): dispatch leaves via requiem.workflows.fanout.

    A sibling to the kanban_executor path. Requires ``repo_path`` (the working
    tree the children mutate). Resolves the leaf source the same way the executor
    path does — inline single leaf for an atomic root, else the committed plan
    artifacts — then runs the in-process fan-out and rolls its outcome up into a
    ``PipelineResult``. ``live=False`` ⇒ ``dry_run`` ⇒ no PRs, no pushes.
    """
    if repo_path is None:
        return PipelineResult(
            item_id=item_id, stage="dispatch", status="paused",
            detail="dispatch_backend='fanout' needs repo_path (the working tree "
                   "the in-process implementation children mutate).",
            decomposable=decomposable, plan_artifact=plan_artifact,
            committed_path=str(committed_path) if committed_path else None,
        )

    # Resolve the leaf source: an atomic root is one inline leaf; a decomposable
    # root reads the committed plan tree's real-id leaves.
    inline_leaves: tuple[fanout_mod.FanoutLeaf, ...] = ()
    if not decomposable:
        inline_leaves = (fanout_mod.FanoutLeaf(
            real_id=item_id,
            title=str(plan_record.get("item_title") or f"item {item_id}"),
            body=str(plan_record.get("summary") or ""),
        ),)

    fo_inputs = fanout_mod.FanoutInputs(
        root_item_id=item_id,
        repo=github_repo or str(item_id),
        repo_path=repo_path,
        log_dir=log_dir,
        base_branch=base_branch or "main",
        dry_run=not live,
        parallel=fanout_parallel,
        leaves=inline_leaves,
        plan_tree_path=Path(plan_artifact) if (decomposable and plan_artifact) else None,
        committed_path=committed_path if decomposable else None,
    )
    fo_run = f"fanout-{item_id}"
    fo_engine = fanout_factory(
        log_dir, inputs=fo_inputs,
        toolbelt=_topology_toolbelt(twig, repo_client), provider=provider,
        **({"gate_handler": gate_handler} if gate_handler is not None else {}),
    )
    fo_outcome = await fo_engine.run(fo_run)
    fo_final = fo_outcome.final_node if isinstance(fo_outcome, Completed) else None
    fo_result = fanout_mod.fanout_result(_completed_map(log_dir, fo_run), fo_final or "")
    leaf_ids = tuple(str(o.real_id) for o in fo_result.outcomes)

    if fo_final != "end_success":
        return PipelineResult(
            item_id=item_id, stage="fanout", status="paused",
            detail=(f"in-process fan-out did not complete cleanly "
                    f"(verdict={fo_result.verdict!r}, node={fo_final!r}); "
                    "inspect the per-leaf child logs."),
            decomposable=decomposable, leaf_ids=leaf_ids,
            plan_artifact=plan_artifact,
            committed_path=str(committed_path) if committed_path else None,
            executor_final_node=fo_final,
            github_repo=github_repo_arg, ado_repo=ado_repo_arg,
            base_branch=base_branch,
            trunk_branch=trunk_branch, trunk_verdict=trunk_verdict,
            fanout_verdict=fo_result.verdict,
            fanout_leaves_total=fo_result.leaves_total,
            fanout_leaves_landed=fo_result.leaves_landed,
        )

    # A surrendered/failed leaf means the fan-out is not cleanly delivered even
    # though the orchestrator itself reached end_success — surface to a human.
    if fo_result.leaves_needs_human > 0 or fo_result.leaves_failed > 0:
        return PipelineResult(
            item_id=item_id, stage="fanout", status="paused",
            detail=(f"in-process fan-out: {fo_result.leaves_landed}/"
                    f"{fo_result.leaves_total} leaves landed; "
                    f"{fo_result.leaves_needs_human} need a human, "
                    f"{fo_result.leaves_failed} failed."),
            decomposable=decomposable, leaf_ids=leaf_ids,
            plan_artifact=plan_artifact,
            committed_path=str(committed_path) if committed_path else None,
            executor_final_node=fo_final,
            github_repo=github_repo_arg, ado_repo=ado_repo_arg,
            base_branch=base_branch,
            trunk_branch=trunk_branch, trunk_verdict=trunk_verdict,
            fanout_verdict=fo_result.verdict,
            fanout_leaves_total=fo_result.leaves_total,
            fanout_leaves_landed=fo_result.leaves_landed,
        )

    # All leaves landed. The in-process implementation children already opened
    # their own leaf PRs (impl/<root>-<leaf> → base), so unlike the kanban path
    # there is no separate leaf-PR leg here. Report delivery.
    return PipelineResult(
        item_id=item_id, stage="fanout", status="delivered",
        detail=(f"in-process fan-out delivered {fo_result.leaves_landed} "
                f"leaf/leaves" + (" (previewed; dry-run)" if not live else "")),
        decomposable=decomposable, leaf_ids=leaf_ids,
        plan_artifact=plan_artifact,
        committed_path=str(committed_path) if committed_path else None,
        executor_final_node=fo_final,
        github_repo=github_repo_arg, ado_repo=ado_repo_arg,
        base_branch=base_branch,
        trunk_branch=trunk_branch, trunk_verdict=trunk_verdict,
        fanout_verdict=fo_result.verdict,
        fanout_leaves_total=fo_result.leaves_total,
        fanout_leaves_landed=fo_result.leaves_landed,
    )


def load_leaf_pr_map(path: Path) -> tuple[LeafPr, ...]:
    """Re-hydrate the persisted leaf-PR map into feature_pr's input element type.

    The inverse of :func:`_persist_leaf_pr_map`; used by :func:`integrate_pipeline`
    to feed the trunk-readiness gate after the human has merged the leaf PRs.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return tuple(
        LeafPr(leaf_id=str(e["leaf_id"]), pr_number=e.get("pr_number"))
        for e in (payload.get("leaves") or [])
    )


async def run_pipeline(
    item_id: int,
    *,
    log_dir: Path,
    board: str,
    assignee: str | None = None,
    commit: bool = False,
    live: bool = False,
    skills: tuple[str, ...] = (),
    github_repo: str | None = None,
    ado_repo: str | None = None,
    base_branch: str | None = None,
    twig: Any | None = None,
    provider: Any | None = None,
    kanban: Any | None = None,
    gh: Any | None = None,
    repo_client: Any | None = None,
    gate_handler: Any | None = None,
    process_config: Any | None = None,
    poll_interval_s: float = 5.0,
    max_polls: int = 120,
    dispatch_backend: str = "kanban",
    repo_path: Path | None = None,
    fanout_parallel: bool = False,
    escalation_policy: str = "escalate",
    planning_factory: PlanningFactory = planning_mod.build_engine,
    commit_factory: CommitFactory = commit_plan_mod.build_engine,
    executor_factory: ExecutorFactory = executor_mod.build_engine,
    trunk_bootstrap_factory: TrunkBootstrapFactory = trunk_bootstrap_mod.build_engine,
    leaf_pr_factory: LeafPrFactory = leaf_pr_mod.build_engine,
    fanout_factory: Any = fanout_mod.build_engine,
) -> PipelineResult:
    log_dir.mkdir(parents=True, exist_ok=True)

    # -- ADR-0024 step 5: resolve the operator's repo choice early -----
    #
    # github_repo + ado_repo are mutually exclusive. Resolve once into
    # a single (repo_id, repo_client) pair that downstream stages use
    # uniformly. Tests can inject `repo_client` directly to bypass real
    # GhClient / AdoClient construction.
    #
    # Internally we keep the name `github_repo` as the repo-identifier
    # variable (so the existing downstream call chains compile unchanged);
    # it now carries EITHER a GitHub "Owner/Repo" string OR an ADO
    # "org/project/repo" string, with `repo_client` discriminating which.
    # The original args are preserved separately to populate the
    # `github_repo` vs `ado_repo` projection on `PipelineResult`.
    _github_repo_arg = github_repo
    _ado_repo_arg = ado_repo
    if repo_client is None:
        repo_id, repo_client = _resolve_repo_target(
            github_repo=github_repo, ado_repo=ado_repo, gh=gh,
        )
    else:
        if github_repo is not None and ado_repo is not None:
            raise ValueError(
                "github_repo and ado_repo are mutually exclusive — pass one or "
                "the other (or neither, for the executor-only path)"
            )
        repo_id = github_repo or ado_repo
    # github_repo is now the unified internal repo-identifier (either
    # platform). Downstream code reads `github_repo`; the original
    # operator-supplied args live in `_github_repo_arg` / `_ado_repo_arg`
    # for use in PipelineResult construction.
    github_repo = repo_id

    # -- Phase 1: plan -------------------------------------------------
    plan_run = f"plan-{item_id}"
    plan_engine = planning_factory(
        log_dir, item_id=item_id, twig=twig, provider=provider,
        gate_handler=gate_handler, process_config=process_config,
    )
    plan_outcome = await plan_engine.run(plan_run)
    plan_record = _plan_record(_completed_map(log_dir, plan_run))

    # ADR-0027: which final verdicts are allowed past the planning
    # phase. Default `escalate` allows only `approved` (today's
    # behavior). `accept-last` ALSO allows `needs_human` — the
    # workflow's record_needs_human path captures the last planner
    # output AND writes the escalation-feedback sidecar, so the
    # operator has both the plan and the reviewer's open questions
    # in a durable form.
    allowed_verdicts: set[str] = {"approved"}
    if escalation_policy == "accept-last":
        allowed_verdicts.add("needs_human")

    verdict = (plan_record or {}).get("final_verdict")
    if plan_record is None or verdict not in allowed_verdicts:
        verdict_repr = verdict or "unknown"
        return PipelineResult(
            item_id=item_id, stage="planning", status="paused",
            detail=(
                f"planning did not approve a plan (verdict={verdict_repr!r}); "
                "resolve the plan before dispatching."
                + (
                    " (run with --on-escalate=accept-last to ship as needs-human)"
                    if verdict == "needs_human" and escalation_policy == "escalate"
                    else ""
                )
            ),
            decomposable=(plan_record or {}).get("decomposable"),
            plan_artifact=(plan_record or {}).get("plan_artifact"),
        )

    decomposable = bool(plan_record.get("decomposable"))
    plan_artifact = plan_record.get("plan_artifact")

    # -- Phase 2: resolve the leaf source ------------------------------
    if not decomposable:
        # Atomic root: the item itself is the single implementable leaf. There
        # is no tree to seed; dispatch it directly (inline-leaves path).
        leaf = LeafSpec(
            leaf_id=str(item_id),
            title=str(plan_record.get("item_title") or f"item {item_id}"),
            body=str(plan_record.get("summary") or ""),
            branch=branch_model.impl_branch(item_id, item_id),
            skills=skills,
        )
        exec_inputs = ExecInputs(
            root_item=str(item_id), board=board, assignee=assignee, live=live,
            leaves=(leaf,), poll_interval_s=poll_interval_s, max_polls=max_polls,
            skills=skills,
        )
        committed_path = None
    else:
        if not commit:
            return PipelineResult(
                item_id=item_id, stage="planning", status="planned",
                detail="plan is decomposable; re-run with commit=True to seed "
                       "ADO children and dispatch the implementable leaves.",
                decomposable=True, plan_artifact=plan_artifact,
            )
        # Seed for real — a faithful fan-out needs real ADO ids, so the manifest
        # must be committed (not a dry-run preview).
        committed_path = log_dir / f"commit-{item_id}.plan.committed.json"
        commit_run = f"commit-{item_id}"
        commit_engine = commit_factory(
            log_dir, plan_tree_path=Path(plan_artifact), dry_run=False,
            twig=twig, manifest_path=committed_path, gate_handler=gate_handler,
        )
        commit_outcome = await commit_engine.run(commit_run)
        if not (isinstance(commit_outcome, Completed)
                and commit_outcome.disposition == "completed"):
            return PipelineResult(
                item_id=item_id, stage="commit", status="paused",
                detail="seeding ADO children did not complete cleanly; "
                       "inspect the commit-plan run before dispatching.",
                decomposable=True, plan_artifact=plan_artifact,
                committed_path=str(committed_path),
            )
        exec_inputs = ExecInputs(
            root_item=str(item_id), board=board, assignee=assignee, live=live,
            plan_tree_path=Path(plan_artifact), committed_path=committed_path,
            poll_interval_s=poll_interval_s, max_polls=max_polls, skills=skills,
        )

    # -- Phase 2.5: trunk bootstrap (ADR-0018 step 4 — BEFORE dispatch) -
    #
    # The integration trunk feature/<root> must exist *before* the leaves are
    # dispatched, because requiem opens each leaf PR with base=feature/<root>
    # right after delivery. This runs only when a github_repo is threaded; the
    # creds-light, executor-only path (no github_repo) skips topology entirely
    # and behaves exactly as before. `live=False` ⇒ dry_run ⇒ read-only probe,
    # no ref create.
    resolved_base: str | None = None
    trunk_verdict: str | None = None
    trunk_branch: str | None = None
    if github_repo is not None:
        resolved_base = base_branch or await _resolve_base_branch_via_platform(
            github_repo, repo_client,
        )
        boot_inputs = trunk_bootstrap_mod.TrunkBootstrapInputs(
            root_item_id=item_id, repo=github_repo,
            base_branch=resolved_base, dry_run=not live,
        )
        boot_run = f"trunk-{item_id}"
        boot_engine = trunk_bootstrap_factory(
            log_dir, inputs=boot_inputs, toolbelt=_topology_toolbelt(twig, repo_client),
            **({"gate_handler": gate_handler} if gate_handler is not None else {}),
        )
        boot_outcome = await boot_engine.run(boot_run)
        boot_final = (
            boot_outcome.final_node if isinstance(boot_outcome, Completed) else None
        )
        boot_result = trunk_bootstrap_mod.trunk_bootstrap_result(
            _completed_map(log_dir, boot_run), boot_final or "",
        )
        trunk_verdict = boot_result.verdict
        trunk_branch = boot_result.trunk_branch
        if boot_final != "end_success":
            # Fail closed: never fan out onto a trunk we could not establish.
            return PipelineResult(
                item_id=item_id, stage="trunk_bootstrap", status="paused",
                detail=(f"trunk bootstrap did not succeed "
                        f"(verdict={trunk_verdict!r}, node={boot_final!r}); "
                        "resolve before dispatching leaves."),
                decomposable=decomposable, plan_artifact=plan_artifact,
                committed_path=str(committed_path) if committed_path else None,
                github_repo=_github_repo_arg, ado_repo=_ado_repo_arg,
                base_branch=resolved_base,
                trunk_branch=trunk_branch, trunk_verdict=trunk_verdict,
            )

    # -- Phase 3: dispatch ---------------------------------------------
    #
    # Two sibling backends (ADR-0021/0014):
    #   * "kanban"  (default) — fan each leaf out to an external Hermes worker via
    #     kanban_executor. Unchanged legacy path.
    #   * "fanout"  — dispatch leaves IN-PROCESS via requiem.workflows.fanout
    #     (single process, no Hermes fleet). Needs repo_path (the working tree)
    #     and a provider; honours fanout_parallel for per-leaf worktree isolation.
    if dispatch_backend == "fanout":
        return await _dispatch_in_process(
            item_id=item_id, log_dir=log_dir, decomposable=decomposable,
            plan_artifact=plan_artifact, committed_path=committed_path,
            plan_record=plan_record, repo_path=repo_path, github_repo=github_repo,
            github_repo_arg=_github_repo_arg, ado_repo_arg=_ado_repo_arg,
            repo_client=repo_client,
            base_branch=resolved_base, twig=twig, gh=gh, provider=provider,
            gate_handler=gate_handler, live=live, fanout_parallel=fanout_parallel,
            trunk_branch=trunk_branch, trunk_verdict=trunk_verdict,
            fanout_factory=fanout_factory,
        )

    real = Toolbelt.real()
    exec_toolbelt = Toolbelt(
        git=real.git, files=real.files, twig=twig if twig is not None else real.twig,
        kanban=kanban if kanban is not None else real.kanban,
    )
    exec_run = f"exec-{item_id}"
    exec_engine = executor_factory(
        log_dir, inputs=exec_inputs, toolbelt=exec_toolbelt,
        **({"gate_handler": gate_handler} if gate_handler is not None else {}),
    )
    exec_outcome = await exec_engine.run(exec_run)
    exec_completed = _completed_map(log_dir, exec_run)
    leaf_ids = tuple(
        leaf["leaf_id"]
        for leaf in (exec_completed.get("resolve_leaves", {}).get("value", {})
                     .get("leaves") or [])
    )
    final_node = exec_outcome.final_node if isinstance(exec_outcome, Completed) else None
    delivered = final_node == "end"

    if not delivered:
        return PipelineResult(
            item_id=item_id, stage="executor", status="paused",
            detail=f"executor stopped at {final_node!r}",
            decomposable=decomposable, leaf_ids=leaf_ids,
            plan_artifact=plan_artifact,
            committed_path=str(committed_path) if committed_path else None,
            executor_final_node=final_node,
            github_repo=_github_repo_arg, ado_repo=_ado_repo_arg,
            base_branch=resolved_base,
            trunk_branch=trunk_branch, trunk_verdict=trunk_verdict,
        )

    # -- Phase 4: leaf PRs (ADR-0018 step 4 — AFTER delivery) ----------
    #
    # The worker has pushed each impl/<root>-<item> branch; now requiem opens
    # (or reuses) the leaf PR base=feature/<root>. This sidesteps Hermes' missing
    # `--base`: requiem owns the PR. The {leaf_id: pr_number} map is PERSISTED —
    # a default `gh pr list` is open-only and can't re-derive merged numbers, so
    # feature_pr (a separate invocation, after the human merges) reads the
    # artifact, never a re-query. Skipped on the creds-light path.
    leaf_pr_verdict: str | None = None
    leaf_pr_leaves: tuple[LeafPr, ...] = ()
    leaf_pr_map_path: Path | None = None
    if github_repo is not None:
        lp_inputs = leaf_pr_mod.LeafPrInputs(
            root_item_id=item_id, repo=github_repo,
            leaf_ids=leaf_ids, dry_run=not live,
        )
        lp_run = f"leafpr-{item_id}"
        lp_engine = leaf_pr_factory(
            log_dir, inputs=lp_inputs, toolbelt=_topology_toolbelt(twig, repo_client),
            **({"gate_handler": gate_handler} if gate_handler is not None else {}),
        )
        lp_outcome = await lp_engine.run(lp_run)
        lp_final = lp_outcome.final_node if isinstance(lp_outcome, Completed) else None
        lp_result = leaf_pr_mod.leaf_pr_result(
            _completed_map(log_dir, lp_run), lp_final or "",
        )
        leaf_pr_verdict = lp_result.verdict
        leaf_pr_leaves = lp_result.leaves
        # Persist the authoritative map for the (later) feature_pr invocation.
        leaf_pr_map_path = _persist_leaf_pr_map(log_dir, item_id, leaf_pr_leaves)
        if lp_final != "end_success":
            return PipelineResult(
                item_id=item_id, stage="leaf_pr", status="paused",
                detail=(f"leaf PRs did not all open "
                        f"(verdict={leaf_pr_verdict!r}, node={lp_final!r}); "
                        "resolve the offending leaf and re-run."),
                decomposable=decomposable, leaf_ids=leaf_ids,
                plan_artifact=plan_artifact,
                committed_path=str(committed_path) if committed_path else None,
                executor_final_node=final_node,
                github_repo=_github_repo_arg, ado_repo=_ado_repo_arg,
                base_branch=resolved_base,
                trunk_branch=trunk_branch, trunk_verdict=trunk_verdict,
                leaf_pr_verdict=leaf_pr_verdict,
                leaf_pr_map=tuple((lp.leaf_id, lp.pr_number) for lp in leaf_pr_leaves),
                leaf_pr_map_path=str(leaf_pr_map_path),
            )

    detail = "all implementable leaves dispatched"
    if github_repo is not None:
        detail = (
            f"leaves dispatched + leaf PRs {leaf_pr_verdict} onto "
            f"{trunk_branch}; merge them, then run integrate_pipeline for the "
            "trunk→base PR."
        )
    return PipelineResult(
        item_id=item_id, stage="executor",
        status="delivered",
        detail=detail,
        decomposable=decomposable, leaf_ids=leaf_ids,
        plan_artifact=plan_artifact,
        committed_path=str(committed_path) if committed_path else None,
        executor_final_node=final_node,
        github_repo=_github_repo_arg, ado_repo=_ado_repo_arg,
        base_branch=resolved_base,
        trunk_branch=trunk_branch, trunk_verdict=trunk_verdict,
        leaf_pr_verdict=leaf_pr_verdict,
        leaf_pr_map=tuple((lp.leaf_id, lp.pr_number) for lp in leaf_pr_leaves),
        leaf_pr_map_path=str(leaf_pr_map_path) if leaf_pr_map_path else None,
    )


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    """Outcome of the trunk→base integration leg (ADR-0018 step 4, phase 5).

    Run *after* a human / pr_lifecycle has merged the leaf PRs into the trunk.
    ``status`` is ``opened`` (trunk→base PR opened/reused), ``previewed`` (dry
    run), ``not_ready`` (the readiness gate found a leaf not yet merged — drift
    or laggard), or ``failed``.
    """

    item_id: int
    status: str
    detail: str
    # Exactly one of github_repo / ado_repo is populated, mirroring the
    # operator's choice on the invocation. ADR-0024 step 5.
    github_repo: str | None = None
    ado_repo: str | None = None
    base_branch: str = ""
    trunk_branch: str = ""
    feature_pr_verdict: str | None = None
    feature_pr_number: int | None = None
    feature_pr_url: str | None = None
    leaves_total: int = 0
    leaves_ready: int = 0
    dispositions_total: int = 0
    dispositions_satisfied: int = 0


async def integrate_pipeline(
    item_id: int,
    *,
    log_dir: Path,
    github_repo: str | None = None,
    ado_repo: str | None = None,
    leaf_pr_map_path: Path | None = None,
    leaves: tuple[LeafPr, ...] | None = None,
    base_branch: str | None = None,
    dispositions: tuple[ItemDisposition, ...] = (),
    live: bool = False,
    twig: Any | None = None,
    gh: Any | None = None,
    repo_client: Any | None = None,
    gate_handler: Any | None = None,
    feature_pr_factory: FeaturePrFactory = feature_pr_mod.build_engine,
) -> IntegrationResult:
    """Phase 5 (ADR-0018 step 4): open the trunk→base PR once leaves are merged.

    This is a SEPARATE invocation from :func:`run_pipeline` because feature_pr's
    readiness gate requires every leaf PR to be ``merged==true`` — a state only
    a human / pr_lifecycle can produce, between the two calls (requiem has no
    ``pr_merge``; INV: no self-merge). The expected-leaf set is read from the
    PERSISTED leaf-PR map (``leaf_pr_map_path``), never re-queried, because a
    default ``gh pr list`` is open-only and can't see merged numbers.

    ``dispositions`` carries the in-scope items' requirement dispositions
    (ADR-0006 INV-DRIVER-GATES-FEATURE-MERGE). The caller sources these from the
    committed plan / Twig item states; feature_pr's gate refuses to open the
    feature→base PR while any is unsatisfied (fail-closed). An empty set leaves
    the gate a no-op (the pre-gate behaviour), so a creds-light caller that does
    not track dispositions is unaffected.

    Drift policy (ratified ADR-0018, confirmed live 2026-06-09): feature_pr only
    *opens* the trunk→base PR; an unmergeable (drifted) PR is surfaced to the
    human by pr_lifecycle. No auto-rebase here.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    # ADR-0024 step 5: resolve operator's repo choice; integrate_pipeline
    # requires a repo (no creds-light path here — feature_pr is the
    # platform-touching step).
    if repo_client is None:
        repo_id, repo_client = _resolve_repo_target(
            github_repo=github_repo, ado_repo=ado_repo, gh=gh,
        )
    else:
        if github_repo is not None and ado_repo is not None:
            raise ValueError(
                "github_repo and ado_repo are mutually exclusive"
            )
        repo_id = github_repo or ado_repo
    if repo_id is None:
        raise ValueError(
            "integrate_pipeline requires either github_repo or ado_repo"
        )
    _github_repo_arg = github_repo
    _ado_repo_arg = ado_repo
    github_repo = repo_id   # unified internal identifier
    if leaves is None:
        if leaf_pr_map_path is None:
            raise ValueError(
                "integrate_pipeline needs either `leaves` or `leaf_pr_map_path` "
                "(the persisted {leaf_id: pr_number} map from run_pipeline)"
            )
        leaves = load_leaf_pr_map(Path(leaf_pr_map_path))

    resolved_base = base_branch or await _resolve_base_branch_via_platform(
        github_repo, repo_client,
    )
    fp_inputs = feature_pr_mod.FeaturePrInputs(
        root_item_id=item_id, repo=github_repo, leaves=leaves,
        base_branch=resolved_base, dry_run=not live, dispositions=dispositions,
    )
    fp_run = f"featurepr-{item_id}"
    fp_engine = feature_pr_factory(
        log_dir, inputs=fp_inputs, toolbelt=_topology_toolbelt(twig, repo_client),
        **({"gate_handler": gate_handler} if gate_handler is not None else {}),
    )
    fp_outcome = await fp_engine.run(fp_run)
    fp_final = fp_outcome.final_node if isinstance(fp_outcome, Completed) else None
    fp_result = feature_pr_mod.feature_pr_result(
        _completed_map(log_dir, fp_run), fp_final or "",
    )
    # The completed-map only carries Success values, so a NeedsHuman gate's
    # identity (which gate fired) is read from the durable gate_opened event.
    fp_gate = _gate_opened(log_dir, fp_run)

    if fp_final == "end_success":
        status = "previewed" if fp_result.dry_run else "opened"
        detail = (
            f"would open {fp_result.trunk_branch} → {resolved_base}"
            if fp_result.dry_run
            else f"integration PR #{fp_result.pr_number} opened "
                 f"({fp_result.trunk_branch} → {resolved_base})"
        )
    elif fp_final == "end_human":
        status = "not_ready"
        # Distinguish the two gate causes via the gate that actually fired: a
        # laggard leaf PR (verify_readiness) vs an unsatisfied in-scope
        # requirement disposition (verify_dispositions — ADR-0006
        # INV-DRIVER-GATES-FEATURE-MERGE).
        if fp_gate == "verify_dispositions":
            detail = (
                f"requirement dispositions not satisfied "
                f"({len(dispositions)} in-scope item(s) gated; resolve the "
                "unsatisfied item(s) and re-run)."
            )
        else:
            detail = (
                f"trunk not ready: {fp_result.leaves_ready}/{fp_result.leaves_total} "
                "expected leaf PRs merged into the trunk. Merge the laggards (or "
                "resolve a drifted/unmergeable leaf PR) and re-run."
            )
    else:
        status = "failed"
        detail = f"feature_pr failed (node={fp_final!r})"

    return IntegrationResult(
        item_id=item_id, status=status, detail=detail,
        github_repo=_github_repo_arg, ado_repo=_ado_repo_arg,
        base_branch=resolved_base,
        trunk_branch=fp_result.trunk_branch,
        feature_pr_verdict=fp_result.verdict,
        feature_pr_number=fp_result.pr_number,
        feature_pr_url=fp_result.pr_url,
        leaves_total=fp_result.leaves_total,
        leaves_ready=fp_result.leaves_ready,
        dispositions_total=fp_result.dispositions_total,
        dispositions_satisfied=fp_result.dispositions_satisfied,
    )


# ---- operator CLI ----------------------------------------------------


def _resolve_process_config(
    *, explicit_path: Path | None, repo_path: Path,
) -> Any:
    """Resolve the ProcessConfig requiem will use for this run.

    Two paths:

    1. ``explicit_path`` set (``--process-config`` on the CLI) — load
       that file directly. Bypasses walking-up discovery. A missing
       file or malformed YAML raises ``ProcessConfigError`` LOUDLY
       so the operator sees their typo instead of silently running
       with polyphony defaults (which is exactly how the 2026-06-17
       SKU-fallback dogfood ended up recursing 4 levels deep on
       Tasks — discovery found nothing, defaults applied, neither
       implementable nor decomposable was set, planner had full
       LLM discretion at every level).
    2. ``explicit_path is None`` — walk up from ``repo_path`` for a
       ``.requiem-config/process.yaml`` exactly as
       ``discover_process_config`` does. Returns
       ``default_process_config()`` when nothing is found.

    Centralising this in one helper lets the run and integrate
    entrypoints share identical resolution semantics, and gives the
    test suite a single seam to pin the precedence behaviour at
    (see ``tests/test_process_config_cli.py``).
    """
    from requiem.process_config import (
        discover_process_config,
        load_process_config,
    )

    if explicit_path is not None:
        # load_process_config already raises ProcessConfigError with
        # a path-bearing message on missing/malformed files.
        return load_process_config(explicit_path)
    return discover_process_config(repo_path)


def _build_arg_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="requiem-end-to-end",
        description="Run Requiem end-to-end against one ADO work item: "
                    "plan → seed → dispatch implementable leaves to Hermes.",
    )
    p.add_argument("--item", type=int, required=True,
                   help="ADO work-item id to deliver.")
    p.add_argument("--board", required=True,
                   help="Dedicated Hermes kanban board (NEVER 'default').")
    p.add_argument("--assignee", default=None,
                   help="Worker profile to assign dispatched leaf tasks to.")
    p.add_argument("--commit", action="store_true",
                   help="Actually seed ADO children for a decomposable plan "
                        "(default: stop after planning).")
    p.add_argument("--live", action="store_true",
                   help="Actually spawn Hermes workers (default: dispatch dry-run).")
    p.add_argument("--log-dir", type=Path, default=Path(".runs"),
                   help="Durable run-log directory (default: .runs).")
    p.add_argument("--repo", type=Path, default=Path("."),
                   help="Repo root to discover .requiem-config/process.yaml from "
                        "(drives the type-agnostic tier policy; default: cwd).")
    p.add_argument("--process-config", type=Path, default=None,
                   help="Path to an explicit process.yaml. When set, overrides "
                        "the --repo-based walking-up discovery. Use this when "
                        "the per-machine config should NOT live in the target "
                        "repo (e.g. early dogfood). Missing file or malformed "
                        "YAML raises a clear error instead of silently falling "
                        "back to defaults.")
    p.add_argument("--github-repo", default=None,
                   help="GitHub repo identity 'Owner/Repo' for trunk topology "
                        "(ADR-0018 step 4). When set, the driver bootstraps "
                        "feature/<root> before dispatch and opens leaf PRs after "
                        "delivery. Omit to run the legacy executor-only pipeline. "
                        "Mutually exclusive with --ado-repo.")
    p.add_argument("--ado-repo", default=None,
                   help="Azure DevOps repo identity 'org/project/repo' for trunk "
                        "topology (ADR-0024). Authenticates via the credential "
                        "chain: explicit → ADO_PAT env → AzureCliCredential "
                        "(operator needs `az login` once on the host). Mutually "
                        "exclusive with --github-repo.")
    p.add_argument("--base-branch", default=None,
                   help="Override the trunk's base branch. Default: resolve the "
                        "repo's real default branch via the RepoPlatform Protocol.")
    p.add_argument("--poll-interval", type=float, default=5.0)
    p.add_argument("--max-polls", type=int, default=120)
    # ADR-0027: reviewer escalation handling. Default `escalate`
    # preserves today's behavior (interactive prompt at escalation_gate);
    # `accept-last` auto-answers `proceed` so a good-enough plan ships
    # as needs_human and the run continues; `abort` auto-terminates.
    # The escalation-feedback sidecar is written on every escalation
    # regardless of this flag (when record_needs_human fires).
    p.add_argument(
        "--on-escalate",
        choices=["escalate", "accept-last", "abort"],
        default="escalate",
        help=(
            "What to do when the planner/reviewer loop hits escalation_gate. "
            "escalate (default): operator must answer interactively. "
            "accept-last: ship the last planner output as needs_human and "
            "continue past the planning phase. "
            "abort: terminate the run. "
            "See ADR-0027 for the failure-mode taxonomy this policy maps to."
        ),
    )
    return p


def _build_integrate_arg_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="requiem-integrate",
        description="ADR-0018 step 4 phase 5: open the trunk→base integration PR "
                    "AFTER the leaf PRs have been merged into feature/<root>. "
                    "Reads the persisted {leaf_id: pr_number} map from run_pipeline.",
    )
    p.add_argument("--item", type=int, required=True,
                   help="Root ADO work-item id (the run root).")
    p.add_argument("--github-repo", default=None,
                   help="GitHub repo identity 'Owner/Repo'. Mutually exclusive "
                        "with --ado-repo; exactly one is required.")
    p.add_argument("--ado-repo", default=None,
                   help="Azure DevOps repo identity 'org/project/repo' "
                        "(ADR-0024). Mutually exclusive with --github-repo; "
                        "exactly one is required.")
    p.add_argument("--leaf-pr-map", type=Path, default=None,
                   help="Path to the persisted leaf-PR map "
                        "(default: <log-dir>/leaf-pr-map-<item>.json).")
    p.add_argument("--base-branch", default=None,
                   help="Override the base branch (default: resolve the repo's "
                        "real default branch).")
    p.add_argument("--live", action="store_true",
                   help="Actually open the trunk→base PR (default: dry-run preview).")
    p.add_argument("--log-dir", type=Path, default=Path(".runs"),
                   help="Durable run-log directory (default: .runs).")
    p.add_argument("--repo", type=Path, default=Path("."),
                   help="Repo root to discover .requiem-config/process.yaml from "
                        "(default: cwd).")
    p.add_argument("--process-config", type=Path, default=None,
                   help="Path to an explicit process.yaml. When set, overrides "
                        "the --repo-based walking-up discovery. Same semantics "
                        "as requiem-end-to-end's --process-config.")
    return p


def main(argv: list[str] | None = None) -> int:
    import asyncio

    from requiem.clients.kanban import KanbanClient
    from requiem.clients.twig import TwigClient
    from requiem.providers import default_provider

    args = _build_arg_parser().parse_args(argv)
    if args.board == "default":
        print("refusing to use the 'default' Hermes board; pass a dedicated "
              "--board (e.g. requiem-<item>).")
        return 2

    # Resolve the repo's tier policy. With --process-config <path>,
    # that file is loaded directly; otherwise we walk up from --repo
    # looking for .requiem-config/process.yaml. See ADR-0025 §1.
    process_config = _resolve_process_config(
        explicit_path=args.process_config, repo_path=args.repo,
    )

    # ADR-0027: build the gate handler from the --on-escalate policy.
    # The handler ONLY auto-responds at escalation_gate; other gates
    # fall through to the default (today: abort, the safe choice for
    # batch context — operators can override via a custom handler
    # passed programmatically). The factory raises ValueError on an
    # unknown policy; argparse `choices=...` prevents that path.
    from requiem.workflows.planning import make_escalation_policy_handler
    gate_handler = make_escalation_policy_handler(args.on_escalate)

    result = asyncio.run(run_pipeline(
        args.item,
        log_dir=args.log_dir,
        board=args.board,
        assignee=args.assignee,
        commit=args.commit,
        live=args.live,
        github_repo=args.github_repo,
        ado_repo=args.ado_repo,
        base_branch=args.base_branch,
        twig=TwigClient(),
        provider=default_provider(),
        kanban=KanbanClient(),
        process_config=process_config,
        poll_interval_s=args.poll_interval,
        max_polls=args.max_polls,
        gate_handler=gate_handler,
        escalation_policy=args.on_escalate,
    ))

    print(f"[{result.stage}] {result.status}: {result.detail}")
    if result.leaf_ids:
        print(f"  leaves: {', '.join(result.leaf_ids)}")
    if result.plan_artifact:
        print(f"  plan:   {result.plan_artifact}")
    if result.committed_path:
        print(f"  seeded: {result.committed_path}")
    if result.trunk_branch:
        print(f"  trunk:  {result.trunk_branch} ({result.trunk_verdict}) "
              f"off {result.base_branch}")
    if result.leaf_pr_map:
        rendered = ", ".join(
            f"{lid}#{n}" if n is not None else f"{lid}#?" for lid, n in result.leaf_pr_map
        )
        print(f"  leafPR: {result.leaf_pr_verdict} — {rendered}")
    if result.leaf_pr_map_path:
        print(f"  map:    {result.leaf_pr_map_path}")
    return 0 if result.status in ("delivered", "planned") else 1


def integrate_main(argv: list[str] | None = None) -> int:
    """Entrypoint for the trunk→base integration leg (phase 5)."""
    import asyncio

    from requiem.clients.twig import TwigClient

    args = _build_integrate_arg_parser().parse_args(argv)
    if args.github_repo is None and args.ado_repo is None:
        print("must pass either --github-repo or --ado-repo")
        return 2
    if args.github_repo is not None and args.ado_repo is not None:
        print("--github-repo and --ado-repo are mutually exclusive")
        return 2
    map_path = args.leaf_pr_map or (args.log_dir / f"leaf-pr-map-{args.item}.json")
    if not Path(map_path).exists():
        print(f"leaf-PR map not found: {map_path}\n"
              "Run the dispatch pipeline first (it persists the map), or pass "
              "--leaf-pr-map explicitly.")
        return 2

    result = asyncio.run(integrate_pipeline(
        args.item,
        log_dir=args.log_dir,
        github_repo=args.github_repo,
        ado_repo=args.ado_repo,
        leaf_pr_map_path=Path(map_path),
        base_branch=args.base_branch,
        live=args.live,
        twig=TwigClient(),
    ))

    print(f"[integrate] {result.status}: {result.detail}")
    if result.feature_pr_url:
        print(f"  PR:     {result.feature_pr_url}")
    print(f"  trunk:  {result.trunk_branch} → {result.base_branch}")
    print(f"  ready:  {result.leaves_ready}/{result.leaves_total} leaves merged")
    return 0 if result.status in ("opened", "previewed") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
