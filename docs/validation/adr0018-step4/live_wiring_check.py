#!/usr/bin/env python3
"""Live validation of the ADR-0018 step-4 wiring against the scratch repo.

The stub tests prove the driver's orchestration logic. This proves the SAME
helper functions the driver calls work against a REAL GitHub repo with the REAL
GhClient — the live boundary unit fakes can't cross. It does NOT run planning /
commit_plan (no ADO creds on this box); it drives the three topology engines
exactly as run_pipeline / integrate_pipeline do, via the real build_engine
factories + a gh-bearing toolbelt.

Flow (root 9300, a fresh multi-leaf root):
  1. _resolve_base_branch()      -> resolves the repo's real default (Q2)
  2. trunk_bootstrap (live)      -> creates feature/9300 off the default branch
  3. push two impl/9300-* leaf branches (simulating the Hermes worker)
  4. leaf_pr (live)              -> opens both leaf PRs base=feature/9300
  5. _persist_leaf_pr_map()      -> writes the artifact run_pipeline persists
  6. merge leaf PRs into the trunk (the human-owned step requiem doesn't own)
  7. load_leaf_pr_map() + feature_pr (live) -> opens feature/9300 -> default PR

Idempotency is also checked: a 2nd trunk_bootstrap must no-op (verdict=exists).
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

from requiem import branch_model
from requiem.clients.gh import GhClient
from requiem.end_to_end import (
    _gh_toolbelt,
    _persist_leaf_pr_map,
    _resolve_base_branch,
    load_leaf_pr_map,
)
from requiem.workflows import feature_pr as feature_pr_mod
from requiem.workflows import leaf_pr as leaf_pr_mod
from requiem.workflows import trunk_bootstrap as trunk_bootstrap_mod
from requiem.kernel import Completed

REPO = "PolyphonyRequiem/requiem-scratch-adr0018"
ROOT = 9300
GH_BIN = "gh"  # resolved on PATH (the real GhClient default); PATH is exported


def sh(*args: str, cwd: str | None = None, check: bool = True) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd {args} failed: {r.stderr or r.stdout}")
    return r.stdout.strip()


def gh(*args: str, check: bool = True) -> str:
    return sh(GH_BIN, *args, check=check)


async def main() -> int:
    log_dir = Path(tempfile.mkdtemp()) / "runs"
    log_dir.mkdir(parents=True)
    client = GhClient()  # default binary="gh", resolved on PATH
    toolbelt = _gh_toolbelt(twig=None, gh=client)
    trunk = branch_model.feature_trunk(ROOT)

    print(f"### live wiring validation — root {ROOT} on {REPO}")
    print(f"### log_dir: {log_dir}")

    # Clean any prior run of this root so the check is deterministic.
    for br in (trunk, branch_model.impl_branch(ROOT, 1), branch_model.impl_branch(ROOT, 2)):
        gh("api", "-X", "DELETE", f"repos/{REPO}/git/refs/heads/{br}", check=False)
    # close any leftover PRs from this root
    for n in gh("pr", "list", "--repo", REPO, "--state", "open",
                "--search", f"head:impl/{ROOT}", "--json", "number",
                "-q", ".[].number", check=False).split():
        gh("pr", "close", n, "--repo", REPO, check=False)

    # 1. Q2 — resolve the real default branch.
    base = await _resolve_base_branch(REPO, client)
    print(f"\n[1] _resolve_base_branch -> {base!r}")
    assert base == "main", f"expected main, got {base}"

    # 2. trunk_bootstrap (LIVE create).
    boot_inputs = trunk_bootstrap_mod.TrunkBootstrapInputs(
        root_item_id=ROOT, repo=REPO, base_branch=base, dry_run=False)
    eng = trunk_bootstrap_mod.build_engine(log_dir, inputs=boot_inputs, toolbelt=toolbelt)
    out = await eng.run(f"trunk-{ROOT}")
    completed = _replay(log_dir, f"trunk-{ROOT}")
    res = trunk_bootstrap_mod.trunk_bootstrap_result(completed, _final(out))
    print(f"[2] trunk_bootstrap -> verdict={res.verdict} node={_final(out)} trunk={res.trunk_branch}")
    assert _final(out) == "end_success" and res.verdict == "created", res

    # 2b. idempotency — second run must no-op (exists, never force-move).
    eng2 = trunk_bootstrap_mod.build_engine(log_dir, inputs=boot_inputs, toolbelt=toolbelt)
    out2 = await eng2.run(f"trunk-{ROOT}-again")
    res2 = trunk_bootstrap_mod.trunk_bootstrap_result(
        _replay(log_dir, f"trunk-{ROOT}-again"), _final(out2))
    print(f"[2b] idempotent re-run -> verdict={res2.verdict} (must be 'exists')")
    assert res2.verdict == "exists", res2

    # 3. push two leaf branches cut from the trunk's base (the worker's job).
    base_sha = gh("api", f"repos/{REPO}/git/ref/heads/{base}", "-q", ".object.sha")
    work = Path(tempfile.mkdtemp()) / "leafrepo"
    sh("git", "clone", "--quiet", f"https://github.com/{REPO}.git", str(work))
    sh("git", "config", "user.name", "requiem-live", cwd=str(work))
    sh("git", "config", "user.email", "noreply@polyphonyrequiem.dev", cwd=str(work))
    leaf_ids = ("1", "2")
    for item in leaf_ids:
        br = branch_model.impl_branch(ROOT, item)
        sh("git", "checkout", "--quiet", "-B", br, base_sha, cwd=str(work))
        # disjoint edits (distinct files) so both leaf PRs merge cleanly
        (work / f"leaf_{ROOT}_{item}.txt").write_text(f"leaf {ROOT}-{item}\n")
        sh("git", "add", "-A", cwd=str(work))
        sh("git", "commit", "--quiet", "-m", f"leaf {ROOT}-{item}", cwd=str(work))
        sh("git", "push", "--quiet", "--force", "origin", br, cwd=str(work))
        print(f"[3] pushed {br}")

    # 4. leaf_pr (LIVE open).
    lp_inputs = leaf_pr_mod.LeafPrInputs(
        root_item_id=ROOT, repo=REPO, leaf_ids=leaf_ids, dry_run=False)
    lp_eng = leaf_pr_mod.build_engine(log_dir, inputs=lp_inputs, toolbelt=toolbelt)
    lp_out = await lp_eng.run(f"leafpr-{ROOT}")
    lp_res = leaf_pr_mod.leaf_pr_result(_replay(log_dir, f"leafpr-{ROOT}"), _final(lp_out))
    print(f"[4] leaf_pr -> verdict={lp_res.verdict} node={_final(lp_out)}")
    for lp in lp_res.leaves:
        print(f"      leaf {lp.leaf_id} -> PR #{lp.pr_number}")
    assert _final(lp_out) == "end_success" and lp_res.verdict == "opened", lp_res
    assert all(lp.pr_number for lp in lp_res.leaves), "every leaf PR must have a number"

    # 5. persist the map (exactly as run_pipeline does).
    map_path = _persist_leaf_pr_map(log_dir, ROOT, lp_res.leaves)
    print(f"[5] persisted leaf-PR map -> {map_path}")
    rehydrated = load_leaf_pr_map(map_path)
    assert rehydrated == lp_res.leaves, "map round-trip must be identity"

    # 6. merge the leaf PRs into the trunk (the human/pr_lifecycle-owned step).
    for lp in lp_res.leaves:
        gh("pr", "merge", str(lp.pr_number), "--repo", REPO, "--merge")
        print(f"[6] merged leaf PR #{lp.pr_number} into {trunk}")
    await asyncio.sleep(3)  # let GitHub settle merged state

    # 7. feature_pr (LIVE) reads the persisted map, opens trunk -> base.
    fp_inputs = feature_pr_mod.FeaturePrInputs(
        root_item_id=ROOT, repo=REPO, leaves=load_leaf_pr_map(map_path),
        base_branch=base, dry_run=False)
    fp_eng = feature_pr_mod.build_engine(log_dir, inputs=fp_inputs, toolbelt=toolbelt)
    fp_out = await fp_eng.run(f"featurepr-{ROOT}")
    fp_res = feature_pr_mod.feature_pr_result(
        _replay(log_dir, f"featurepr-{ROOT}"), _final(fp_out))
    print(f"[7] feature_pr -> verdict={fp_res.verdict} node={_final(fp_out)} "
          f"PR #{fp_res.pr_number} ready={fp_res.leaves_ready}/{fp_res.leaves_total}")
    assert _final(fp_out) == "end_success" and fp_res.verdict == "opened", fp_res
    assert fp_res.pr_number, "feature PR must be opened"
    print(f"      {fp_res.pr_url}")

    print("\n### ALL LIVE WIRING ASSERTIONS PASSED ✓")
    print(f"### feature PR #{fp_res.pr_number} ({trunk} -> {base}) left open for inspection (Q3).")
    return 0


def _replay(log_dir: Path, run_id: str) -> dict:
    from requiem.persistence import replay
    completed: dict = {}
    for ev in replay(log_dir / f"{run_id}.events.jsonl"):
        if ev.get("kind") == "verb_completed":
            completed[ev["node_id"]] = ev["payload"]["outcome"]
    return completed


def _final(out) -> str:
    return out.final_node if isinstance(out, Completed) else ""


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
