"""plan_pr workflow — open the approved plan as a reviewable ``plan/<root>`` PR.

This closes the *plan PR* half of v0 non-negotiable #6 ("recursive planning
with child seeding **and PR lifecycle**"). The *child-seeding* half ships as
``commit_plan.py`` (ADR-0011); this module materialises the already-written
planning sidecar artefact as a real GitHub pull request on a ``plan/<root>``
branch, per ADR-0006 Option D (plan-PR-as-aggregator).

Shape (mirrors ``implementation.py``'s open-and-handoff, minus the coder loop)::

    start
      → load_plan          (script · read+validate+render the sidecar)
      → create_branch      (script · plan/<root>, idempotent)
      → write_plan_doc     (script · write the rendered plan into the repo)
      → commit             (script · stage ONLY the plan doc, idempotent)
      → push               (script · origin plan/<root>, idempotent)
      → open_pr            (script · gh.pr_create, idempotent via pr_search)
      → link_pr            (script · twig backlink, best-effort)
      → end_success

``GhClient`` has no ``pr_merge`` — review→merge is owned by ``pr_lifecycle.py``
(Gluck). This workflow opens the PR and hands off, exactly like the
implementation workflow hands its impl PR to the same reviewer.

Design hardening (rubber-duck pass, 2026-06-02):

* ``load_plan`` runs in full even under ``dry_run`` (parse + validate + render);
  only the git/GitHub *mutations* are previewed. A dry run that reports success
  on an artefact that would fail live is a lie we refuse to tell.
* Leaf ``.plan.md`` artefacts are *not* assumed approved — planning writes the
  same filename for ``needs_human`` leaves. We fail closed unless the rendered
  verdict line says ``approved``.
* ``commit`` stages **only** the plan doc (``git_commit(paths=[...])``), never
  ``git add -A`` — a plan PR must not sweep up unrelated local edits.
* The plan-doc path is validated to live *inside* the repo (no ``..``, no
  absolute escape).
* PR reuse matches on ``head`` **and** ``base`` — a stale wrong-base PR escalates
  to a human instead of being silently adopted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from requiem.clients.fs import FilesystemClient
from requiem.clients.gh import GhClientError
from requiem.clients.twig import TwigClientError
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import NeedsHuman, PermanentFailure, Success
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt

# ---- error kinds --------------------------------------------------------

EK_MISSING_ARTIFACT = "plan.artifact_missing"
EK_BAD_ARTIFACT = "plan.artifact_malformed"
EK_UNSUPPORTED_SCHEMA = "plan.schema_unsupported"
EK_NOT_APPROVED = "plan.not_approved"
EK_WRONG_ROOT = "plan.root_mismatch"
EK_BAD_DOC_PATH = "plan.doc_path_invalid"

# ---- gates --------------------------------------------------------------

GATE_BRANCH_FOREIGN = "branch_exists_foreign"
GATE_PR_WRONG_BASE = "pr_exists_wrong_base"

MIN_SCHEMA_VERSION = 2
_MAX_RENDER_DEPTH = 12


# ---- public dataclasses -------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanPrResult:
    """Programmatic projection of a plan_pr run."""

    plan_id: str
    root_item_id: int
    verdict: Literal["opened", "previewed", "needs_human", "failed"]
    branch_name: str
    base_branch: str
    pr_number: int | None
    pr_url: str | None
    plan_doc_path: str
    dry_run: bool
    reused_existing: bool


@dataclass(slots=True)
class PlanPrInputs:
    """Everything the plan_pr workflow needs, stamped once at start_run."""

    plan_artifact_path: Path
    root_item_id: int
    repo: str
    repo_path: Path
    base_branch: str = "main"
    plan_doc_path: str | None = None  # repo-relative; defaults under .requiem/plans/
    dry_run: bool = True

    def resolved_doc_path(self) -> str:
        return self.plan_doc_path or f".requiem/plans/{self.root_item_id}.plan.md"


# ---- in-memory fakes (CLI demo + tests duck-type these) -----------------


@dataclass
class _DemoGhClient:
    """In-memory GhClient stand-in (demo / unit tests)."""

    next_pr_number: int = 42
    existing: list[Any] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    raise_on_search: Exception | None = None
    raise_on_create: Exception | None = None

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
            number=n, title=title, state="OPEN", merged=False, merged_at=None,
            head=head, base=base, url=url, raw={"number": n, "title": title, "url": url},
        )
        self.created.append({"title": title, "head": head, "base": base, "url": url})
        return pr


@dataclass
class _DemoTwigClient:
    """In-memory TwigClient stand-in (demo / unit tests)."""

    raise_on_comment: Exception | None = None
    comments: list[tuple[int, str]] = field(default_factory=list)

    async def comment_async(self, item_id: int, message: str) -> None:
        if self.raise_on_comment is not None:
            raise self.raise_on_comment
        self.comments.append((item_id, message))


# ---- artefact load + render --------------------------------------------


def _validate_doc_path(repo_path: Path, doc_path: str) -> str | PermanentFailure:
    """Require ``doc_path`` to resolve strictly inside ``repo_path``."""
    candidate = Path(doc_path)
    if candidate.is_absolute():
        return PermanentFailure(
            error_kind=EK_BAD_DOC_PATH,
            message=f"plan_doc_path must be repo-relative, got absolute {doc_path!r}",
        )
    try:
        resolved = (repo_path.resolve() / candidate).resolve()
        resolved.relative_to(repo_path.resolve())
    except (ValueError, OSError) as e:
        return PermanentFailure(
            error_kind=EK_BAD_DOC_PATH,
            message=f"plan_doc_path {doc_path!r} escapes the repo: {e}",
        )
    return str(candidate.as_posix())


def _md_is_approved(text: str) -> bool:
    """A leaf ``.plan.md`` is approved iff its verdict line says so.

    planning.py writes ``- **Verdict:** approved`` or ``- **Verdict:** needs
    human``. We fail closed if neither an approved line is present.
    """
    for line in text.splitlines():
        low = line.lower()
        if "verdict" in low:
            return "approved" in low and "needs human" not in low
    return False


def _render_proposals(
    proposals: list[Any],
    children: list[Any],
    *,
    depth: int,
    lines: list[str],
) -> None:
    """Defensive recursive render of proposals + aligned recursive children."""
    if depth > _MAX_RENDER_DEPTH:
        lines.append(f"{'  ' * depth}- … (render depth cap reached)")
        return
    indent = "  " * depth
    for i, prop in enumerate(proposals):
        if not isinstance(prop, dict):
            lines.append(f"{indent}- (malformed proposal at index {i})")
            continue
        title = str(prop.get("title") or "(untitled)")
        wtype = str(prop.get("work_item_type") or "?")
        desc = str(prop.get("description") or "").strip()
        pinned = prop.get("item_id")
        suffix = f" — _reuse AB#{pinned}_" if isinstance(pinned, int) else ""
        lines.append(f"{indent}- **{title}** (`{wtype}`){suffix}")
        if desc:
            lines.append(f"{indent}  {desc}")
        child = children[i] if i < len(children) and isinstance(children[i], dict) else None
        if child and child.get("decomposable"):
            sub_props = child.get("proposals")
            sub_children = child.get("children")
            if isinstance(sub_props, list):
                _render_proposals(
                    sub_props,
                    sub_children if isinstance(sub_children, list) else [],
                    depth=depth + 1,
                    lines=lines,
                )


def _render_tree_md(art: dict, *, source_name: str) -> str:
    """Render a schema-v2 plan tree into a reviewable markdown document."""
    root = art.get("item_id")
    title = str(art.get("item_title") or art.get("title") or "(untitled)")
    plan_id = str(art.get("plan_id") or "")
    proposals = art.get("proposals")
    children = art.get("children")
    lines = [
        f"# Plan: AB#{root} — {title}",
        "",
        "<!-- Generated by the Requiem plan_pr workflow. The event log is",
        "     authoritative; this document is the human review surface. -->",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Plan id | `{plan_id}` |",
        f"| Root work item | AB#{root} |",
        f"| Schema version | {art.get('schema_version')} |",
        f"| Verdict | {art.get('verdict')} |",
        f"| Source artefact | `{source_name}` |",
        "",
        "## Summary",
        "",
        str(art.get("summary") or "(no summary recorded)"),
        "",
        "## Proposed work items",
        "",
    ]
    if isinstance(proposals, list) and proposals:
        _render_proposals(
            proposals,
            children if isinstance(children, list) else [],
            depth=0,
            lines=lines,
        )
    else:
        lines.append("- (leaf plan — no decomposition proposed)")
    lines.append("")
    return "\n".join(lines)


# ---- verb registry ------------------------------------------------------


def build_verb_registry(inputs: PlanPrInputs) -> VerbRegistry:
    verbs = VerbRegistry()
    branch_name = f"plan/{inputs.root_item_id}"

    def _require_fs(ctx) -> FilesystemClient | PermanentFailure:
        fs = ctx.toolbelt.fs
        if fs is None:
            return PermanentFailure(
                error_kind="toolbelt.missing_client",
                message="plan_pr workflow requires toolbelt.fs",
            )
        return fs

    def _require_gh(ctx):
        gh = ctx.toolbelt.gh
        if gh is None:
            return PermanentFailure(
                error_kind="toolbelt.missing_client",
                message="plan_pr workflow requires toolbelt.gh",
            )
        return gh

    # ---- start --------------------------------------------------------

    @verbs.register("start_run")
    def _start(ctx):
        return Success(value={
            "intent": "plan_pr",
            "root_item_id": inputs.root_item_id,
            "repo": inputs.repo,
            "repo_path": str(inputs.repo_path),
            "base_branch": inputs.base_branch,
            "branch_name": branch_name,
            "dry_run": inputs.dry_run,
        })

    # ---- load_plan (always fully validates + renders, even in dry-run) -

    @verbs.register("load_plan")
    def _load_plan(ctx):
        path = inputs.plan_artifact_path
        if not path.exists():
            return PermanentFailure(
                error_kind=EK_MISSING_ARTIFACT,
                message=f"plan artefact not found: {path}",
                details={"path": str(path)},
            )
        doc_rel = _validate_doc_path(inputs.repo_path, inputs.resolved_doc_path())
        if isinstance(doc_rel, PermanentFailure):
            return doc_rel

        text = path.read_text(encoding="utf-8")
        if path.name.endswith(".tree.json"):
            try:
                art = json.loads(text)
            except json.JSONDecodeError as e:
                return PermanentFailure(
                    error_kind=EK_BAD_ARTIFACT,
                    message=f"plan tree is not valid JSON: {e}",
                    details={"path": str(path)},
                )
            if not isinstance(art, dict):
                return PermanentFailure(
                    error_kind=EK_BAD_ARTIFACT,
                    message="plan tree root is not a JSON object",
                )
            schema = art.get("schema_version")
            if not isinstance(schema, int) or schema < MIN_SCHEMA_VERSION:
                return PermanentFailure(
                    error_kind=EK_UNSUPPORTED_SCHEMA,
                    message=(
                        f"plan tree schema_version={schema!r}; need "
                        f">= {MIN_SCHEMA_VERSION}"
                    ),
                )
            if art.get("verdict") != "approved":
                return PermanentFailure(
                    error_kind=EK_NOT_APPROVED,
                    message=f"plan verdict is {art.get('verdict')!r}, not 'approved'",
                )
            art_root = art.get("item_id")
            if art_root is not None and int(art_root) != inputs.root_item_id:
                return PermanentFailure(
                    error_kind=EK_WRONG_ROOT,
                    message=(
                        f"plan tree item_id={art_root} does not match "
                        f"root_item_id={inputs.root_item_id}"
                    ),
                )
            plan_id = str(art.get("plan_id") or "")
            title = str(art.get("item_title") or art.get("title") or "(untitled)")
            rendered = _render_tree_md(art, source_name=path.name)
        else:
            # Leaf .plan.md — fail closed unless the verdict line is approved.
            if not _md_is_approved(text):
                return PermanentFailure(
                    error_kind=EK_NOT_APPROVED,
                    message=(
                        "leaf plan markdown is not marked approved "
                        "(no 'Verdict: approved' line)"
                    ),
                    details={"path": str(path)},
                )
            plan_id = ""
            title = "(leaf plan)"
            for line in text.splitlines():
                if line.startswith("# Plan:"):
                    plan_id = line[len("# Plan:"):].strip()
                    break
            rendered = text

        return Success(
            value={
                "plan_id": plan_id,
                "root_item_id": inputs.root_item_id,
                "title": title,
                "branch_name": branch_name,
                "base_branch": inputs.base_branch,
                "plan_doc_path": doc_rel,
                "rendered_md": rendered,
                "dry_run": inputs.dry_run,
            },
            inspected_artifacts=(f"file:{path}",),
        )

    # ---- create_branch (idempotent + foreign→human) -------------------

    @verbs.register("create_branch")
    async def _create_branch(ctx):
        if inputs.dry_run:
            return Success(value={"branch_name": branch_name, "created": False, "dry_run": True})
        fs = _require_fs(ctx)
        if isinstance(fs, PermanentFailure):
            return fs
        from requiem.clients.fs import FsGitError

        try:
            exists = await fs.git_branch_exists(branch_name)
            current = await fs.git_current_branch()
        except FsGitError as e:
            return PermanentFailure(
                error_kind="branch.probe_failed",
                message=f"git probe failed: {e.stderr.strip() or e}",
                details={"stderr": e.stderr},
            )
        if exists and current == branch_name:
            return Success(value={"branch_name": branch_name, "created": False, "already_on_branch": True})
        if exists:
            return NeedsHuman(
                gate=GATE_BRANCH_FOREIGN,
                prompt=(
                    f"Branch {branch_name!r} already exists locally but HEAD is "
                    f"on {current!r}. A prior run (or a human) left state behind."
                ),
                options=("abort", "delete_and_recreate", "resume_on_branch"),
                context={"branch": branch_name, "current": current},
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

    # ---- write_plan_doc -----------------------------------------------

    @verbs.register("write_plan_doc")
    def _write_plan_doc(ctx):
        load = ctx.completed["load_plan"]["value"]
        doc_rel = load["plan_doc_path"]
        if inputs.dry_run:
            return Success(value={"plan_doc_path": doc_rel, "written": False, "dry_run": True})
        target = inputs.repo_path / doc_rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(load["rendered_md"], encoding="utf-8")
        except OSError as e:
            return PermanentFailure(
                error_kind="plan.doc_write_failed",
                message=f"could not write plan doc {doc_rel}: {e}",
                details={"path": str(target)},
            )
        return Success(
            value={"plan_doc_path": doc_rel, "written": True},
            inspected_artifacts=(f"file:{target}",),
        )

    # ---- commit (stage ONLY the plan doc) -----------------------------

    @verbs.register("commit")
    async def _commit(ctx):
        if inputs.dry_run:
            return Success(value={"sha": None, "dry_run": True})
        fs = _require_fs(ctx)
        if isinstance(fs, PermanentFailure):
            return fs
        from requiem.clients.fs import FsGitError

        load = ctx.completed["load_plan"]["value"]
        doc_rel = load["plan_doc_path"]
        try:
            if await fs.git_is_clean():
                return Success(value={"sha": None, "already_committed": True})
            sha = await fs.git_commit(
                f"plan: AB#{inputs.root_item_id} {load.get('title', '')}".strip(),
                paths=[Path(doc_rel)],
            )
        except FsGitError as e:
            return PermanentFailure(
                error_kind="commit.failed",
                message=f"git commit failed: {e.stderr.strip() or e}",
                details={"stderr": e.stderr},
            )
        return Success(value={"sha": sha, "plan_doc_path": doc_rel}, inspected_artifacts=(f"git:commit:{sha}",))

    # ---- push ---------------------------------------------------------

    @verbs.register("push")
    async def _push(ctx):
        if inputs.dry_run:
            return Success(value={"pushed": False, "dry_run": True})
        fs = _require_fs(ctx)
        if isinstance(fs, PermanentFailure):
            return fs
        from requiem.clients.fs import FsGitError

        try:
            await fs.git_push("origin", branch_name)
        except FsGitError as e:
            return PermanentFailure(
                error_kind="push.failed",
                message=f"git push origin {branch_name} failed: {e.stderr.strip() or e}",
                details={"stderr": e.stderr},
            )
        return Success(value={"pushed": True, "remote": "origin", "branch": branch_name})

    # ---- open_pr (idempotent via head+base match) ---------------------

    @verbs.register("open_pr")
    async def _open_pr(ctx):
        if inputs.dry_run:
            return Success(value={"pr_number": None, "pr_url": None, "dry_run": True})
        gh = _require_gh(ctx)
        if isinstance(gh, PermanentFailure):
            return gh
        load = ctx.completed["load_plan"]["value"]
        title = f"Plan: AB#{inputs.root_item_id} — {load.get('title', '')}".strip()
        body = (
            f"Plan review surface for **AB#{inputs.root_item_id}** "
            f"(plan `{load.get('plan_id', '')}`).\n\n"
            f"The full plan is committed at `{load['plan_doc_path']}` on this "
            f"branch. Approve to let the implementation lifecycle proceed; the "
            f"`pr_lifecycle` (review→merge) workflow takes over from here.\n"
        )
        try:
            existing = await gh.pr_search(inputs.repo, query=f"head:{branch_name} state:open", limit=5)
        except GhClientError as e:
            return PermanentFailure(
                error_kind="pr.search_failed",
                message=f"gh pr list failed: {e}",
                details={"error": str(e)},
            )
        for pr in existing:
            if pr.head != branch_name:
                continue
            if pr.base != inputs.base_branch:
                return NeedsHuman(
                    gate=GATE_PR_WRONG_BASE,
                    prompt=(
                        f"An open PR #{pr.number} already exists from {branch_name!r} "
                        f"but targets {pr.base!r}, not the expected {inputs.base_branch!r}."
                    ),
                    options=("abort", "reuse_anyway"),
                    context={"pr_number": pr.number, "pr_base": pr.base, "expected_base": inputs.base_branch},
                )
            return Success(
                value={"pr_number": pr.number, "pr_url": pr.url, "title": pr.title, "reused_existing": True},
                inspected_artifacts=(f"gh:pr:{inputs.repo}#{pr.number}",),
            )
        try:
            pr = await gh.pr_create(inputs.repo, title=title, body=body, head=branch_name, base=inputs.base_branch)
        except GhClientError as e:
            return PermanentFailure(
                error_kind="pr.create_failed",
                message=f"gh pr create failed: {e}",
                details={"error": str(e)},
            )
        return Success(
            value={"pr_number": pr.number, "pr_url": pr.url, "title": pr.title, "reused_existing": False},
            inspected_artifacts=(f"gh:pr:{inputs.repo}#{pr.number}",),
        )

    # ---- link_pr (best-effort ADO backlink) ---------------------------

    @verbs.register("link_pr")
    async def _link_pr(ctx):
        if inputs.dry_run:
            return Success(value={"linked": False, "dry_run": True})
        pr = ctx.completed.get("open_pr", {}).get("value", {})
        url = pr.get("pr_url")
        if not url:
            return Success(value={"linked": False, "reason": "no PR url to link"})
        twig = ctx.toolbelt.twig
        if twig is None:
            return Success(value={"linked": False, "reason": "no twig client"})
        try:
            await twig.comment_async(
                inputs.root_item_id,
                f"Plan PR opened by Requiem plan_pr workflow: {url}",
            )
        except TwigClientError as e:
            # Best-effort: a failed backlink does not block the PR handoff.
            return PermanentFailure(
                error_kind="pr.link_failed",
                message=f"twig comment failed: {e}",
                details={"pr_url": url, "error": str(e)},
            )
        return Success(value={"linked": True, "pr_url": url})

    return verbs


# ---- workflow topology --------------------------------------------------


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder("plan-pr", module="requiem.workflows.plan_pr", version="0.1")
            .entry("start")
            .script("start", verb="start_run")
                .edge("start", on="success", to="load_plan")
            .script("load_plan", verb="load_plan")
                .edge("load_plan", on="success", to="create_branch")
                .edge("load_plan", on="permanent_failure", to="end_failed")
            .script("create_branch", verb="create_branch")
                .edge("create_branch", on="success", to="write_plan_doc")
                .edge("create_branch", on="needs_human", to="end_human")
                .edge("create_branch", on="permanent_failure", to="end_failed")
            .script("write_plan_doc", verb="write_plan_doc")
                .edge("write_plan_doc", on="success", to="commit")
                .edge("write_plan_doc", on="permanent_failure", to="end_failed")
            .script("commit", verb="commit")
                .edge("commit", on="success", to="push")
                .edge("commit", on="permanent_failure", to="end_failed")
            .script("push", verb="push")
                .edge("push", on="success", to="open_pr")
                .edge("push", on="permanent_failure", to="end_failed")
            .script("open_pr", verb="open_pr")
                .edge("open_pr", on="success", to="link_pr")
                .edge("open_pr", on="needs_human", to="end_human")
                .edge("open_pr", on="permanent_failure", to="end_failed")
            .script("link_pr", verb="link_pr")
                # best-effort: a failed backlink still hands off (PR is open).
                .edge("link_pr", on="success", to="end_success")
                .edge("link_pr", on="permanent_failure", to="end_success")
            .terminate("end_success", disposition="completed")
            .terminate("end_failed", disposition="failed")
            .terminate("end_human", disposition="failed")
            .humanize({
                "start": "Starting plan PR",
                "load_plan": "Loaded + rendered the plan",
                "create_branch": "Created plan branch",
                "write_plan_doc": "Wrote plan document",
                "commit": "Committed plan document",
                "push": "Pushed plan branch",
                "open_pr": "Opened plan PR",
                "link_pr": "Linked PR to work item",
                "end_success": "Plan PR ready for review",
                "end_failed": "Plan PR failed",
                "end_human": "Needs human decision",
            })
            .build()
    )


# ---- engine construction ------------------------------------------------


def _demo_tree(root_id: int = 4242) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "plan_id": "demo-plan",
        "item_id": root_id,
        "item_title": "Demo root",
        "decomposable": True,
        "summary": "Two-part demo plan.",
        "verdict": "approved",
        "proposals": [
            {"title": "Data layer", "description": "schema + migration", "work_item_type": "Task"},
            {"title": "API layer", "description": "endpoints", "work_item_type": "Task"},
        ],
        "children": [
            {
                "item_id": root_id * 100 + 1, "plan_id": "demo-plan", "decomposable": True,
                "summary": "", "final_verdict": "approved",
                "proposals": [
                    {"title": "Define schema", "description": "", "work_item_type": "Task"},
                    {"title": "Write migration", "description": "", "work_item_type": "Task"},
                ],
                "children": [],
            },
            {
                "item_id": root_id * 100 + 2, "plan_id": "demo-plan", "decomposable": False,
                "summary": "", "final_verdict": "approved", "proposals": [], "children": [],
            },
        ],
    }


def _init_demo_repo(repo_path: Path) -> None:
    import subprocess

    repo_path.mkdir(parents=True, exist_ok=True)
    if (repo_path / ".git").exists():
        return

    def g(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo_path), capture_output=True, text=True, check=True)

    g("init", "-q")
    g("config", "user.email", "demo@requiem.local")
    g("config", "user.name", "Requiem Demo")
    g("checkout", "-q", "-b", "main")
    (repo_path / "README.md").write_text("# demo\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "initial")


def build_engine(
    log_dir: Path,
    *,
    plan_artifact_path: Path | None = None,
    root_item_id: int | None = None,
    repo: str | None = None,
    repo_path: Path | None = None,
    base_branch: str | None = None,
    plan_doc_path: str | None = None,
    dry_run: bool | None = None,
    toolbelt: Toolbelt | None = None,
    gate_handler=None,
) -> Engine:
    """Build an Engine for ``plan-pr``.

    Zero-arg (``build_engine(log_dir)``) ships a canned, dry-run,
    side-effect-free demo: a schema-v2 approved tree, a throwaway git repo
    under ``log_dir/plan_pr_demo_repo``, and in-memory gh/twig fakes.

    Environment overrides (read once here):

    * ``REQUIEM_PLAN_PR_ARTIFACT`` — path to a ``.plan.tree.json`` / ``.plan.md``
    * ``REQUIEM_PLAN_PR_ROOT``     — root work item id
    * ``REQUIEM_PLAN_PR_REPO``     — owner/name for gh
    * ``REQUIEM_PLAN_PR_REPO_PATH``— local git worktree
    * ``REQUIEM_PLAN_PR_BASE``     — base branch (default ``main``)
    * ``REQUIEM_PLAN_PR_DRY_RUN``  — "1"/"true"/"yes"
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    plan_artifact_path = plan_artifact_path or _env_path("REQUIEM_PLAN_PR_ARTIFACT")
    if root_item_id is None:
        env_root = os.environ.get("REQUIEM_PLAN_PR_ROOT")
        root_item_id = int(env_root) if env_root else None
    repo = repo or os.environ.get("REQUIEM_PLAN_PR_REPO")
    repo_path = repo_path or _env_path("REQUIEM_PLAN_PR_REPO_PATH")
    if base_branch is None:
        base_branch = os.environ.get("REQUIEM_PLAN_PR_BASE") or "main"
    if dry_run is None:
        env = os.environ.get("REQUIEM_PLAN_PR_DRY_RUN")
        dry_run = (env or "").strip().lower() in ("1", "true", "yes") if env else True

    demo = plan_artifact_path is None
    if demo:
        root_item_id = root_item_id or 4242
        repo = repo or "Owner/Repo"
        repo_path = repo_path or (log_dir / "plan_pr_demo_repo")
        _init_demo_repo(repo_path)
        plan_artifact_path = log_dir / "plan-pr-demo.plan.tree.json"
        plan_artifact_path.write_text(json.dumps(_demo_tree(root_item_id), indent=2) + "\n", encoding="utf-8")
        if toolbelt is None:
            toolbelt = Toolbelt(
                git=RealGitClient(),
                files=FakeFileClient({}),
                gh=_DemoGhClient(),  # type: ignore[arg-type]
                fs=FilesystemClient(repo_path),
                twig=_DemoTwigClient(),  # type: ignore[arg-type]
            )

    if root_item_id is None or repo is None or repo_path is None:
        raise ValueError(
            "plan_pr.build_engine requires root_item_id, repo, and repo_path "
            "(or the REQUIEM_PLAN_PR_* env vars / demo mode)."
        )

    inputs = PlanPrInputs(
        plan_artifact_path=plan_artifact_path,
        root_item_id=root_item_id,
        repo=repo,
        repo_path=repo_path,
        base_branch=base_branch,
        plan_doc_path=plan_doc_path,
        dry_run=bool(dry_run),
    )
    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(inputs),
        agents=AgentRegistry(),
        provider=None,
        toolbelt=toolbelt or Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=gate_handler or _default_gate_handler,
    )


def _default_gate_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    """Auto-abort gate handler for non-interactive (CLI / demo) runs.

    Plan-PR gates (foreign branch, existing PR on the wrong base) are
    always recoverable by a human re-running after cleanup, so the safe
    default is to abort rather than mutate further. Interactive callers
    override this via ``build_engine(..., gate_handler=...)``.
    """
    return "abort" if "abort" in options else options[-1]


_default_gate_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def _env_path(name: str) -> Path | None:
    v = os.environ.get(name)
    return Path(v) if v else None


# ---- result projection --------------------------------------------------


def plan_pr_result(completed: dict, final_node: str) -> PlanPrResult:
    load = (completed.get("load_plan") or {}).get("value") or {}
    pr = (completed.get("open_pr") or {}).get("value") or {}
    doc = (completed.get("write_plan_doc") or {}).get("value") or {}
    dry_run = bool(load.get("dry_run"))
    if final_node == "end_success":
        verdict: Literal["opened", "previewed", "needs_human", "failed"] = (
            "previewed" if dry_run else "opened"
        )
    elif final_node == "end_human":
        verdict = "needs_human"
    else:
        verdict = "failed"
    pr_number = pr.get("pr_number")
    return PlanPrResult(
        plan_id=str(load.get("plan_id") or ""),
        root_item_id=int(load.get("root_item_id") or 0),
        verdict=verdict,
        branch_name=str(load.get("branch_name") or ""),
        base_branch=str(load.get("base_branch") or ""),
        pr_number=int(pr_number) if pr_number else None,
        pr_url=pr.get("pr_url"),
        plan_doc_path=str(doc.get("plan_doc_path") or load.get("plan_doc_path") or ""),
        dry_run=dry_run,
        reused_existing=bool(pr.get("reused_existing")),
    )


def verdict_card(completed: dict) -> str | None:
    load = (completed.get("load_plan") or {}).get("value")
    if not load:
        return None
    pr = (completed.get("open_pr") or {}).get("value") or {}
    dry = load.get("dry_run")
    if dry:
        head = "  ◐ Dry run (preview)"
        tail = f"would open plan PR from {load.get('branch_name')} → {load.get('base_branch')}"
    elif pr.get("pr_url"):
        verb = "reused" if pr.get("reused_existing") else "opened"
        head = "  ✓ Plan PR ready"
        tail = f"{verb} #{pr.get('pr_number')} — {pr.get('pr_url')}"
    else:
        head = "  ⚠ Plan PR not opened"
        tail = "see verdict"
    return f"{head}\n  plan {load.get('plan_id')} for AB#{load.get('root_item_id')} — {tail}"


# ---- __main__ -----------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Open an approved plan as a plan/<root> PR.")
    p.add_argument("--artifact", type=Path, default=None, help="path to a .plan.tree.json / .plan.md")
    p.add_argument("--root", type=int, default=None, help="root work item id")
    p.add_argument("--repo", default=None, help="owner/name for gh")
    p.add_argument("--repo-path", type=Path, default=None)
    p.add_argument("--base", default=None, help="base branch (default main)")
    p.add_argument("--doc-path", default=None, help="repo-relative path for the plan doc")
    p.add_argument("--run-id", default="plan-pr")
    p.add_argument("--log-dir", type=Path, default=Path("runs"))
    mx = p.add_mutually_exclusive_group()
    mx.add_argument("--dry-run", dest="dry_run", action="store_true")
    mx.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    p.set_defaults(dry_run=None)
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_arg_parser().parse_args(argv)
    engine = build_engine(
        args.log_dir,
        plan_artifact_path=args.artifact,
        root_item_id=args.root,
        repo=args.repo,
        repo_path=args.repo_path,
        base_branch=args.base,
        plan_doc_path=args.doc_path,
        dry_run=args.dry_run,
    )
    result = await engine.run(args.run_id)
    completed = {}
    try:
        from requiem.workflows.planning import completed_from_log

        completed = completed_from_log(engine.log_path(args.run_id))
    except Exception:  # pragma: no cover — best-effort verdict card
        pass
    card = verdict_card(completed)
    if card:
        print(card)
    return 0 if type(result).__name__ == "Completed" else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
