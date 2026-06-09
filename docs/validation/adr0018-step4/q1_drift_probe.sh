#!/usr/bin/env bash
# Q1 — branch-drift probe for ADR-0018 step 4.
#
# Faithful integration harness (ADR-0018 §"Open refinement" blesses this in lieu
# of a full Hermes worker loop): real GitHub branch topology + real 3-way merges.
# Simulates the Hermes worktree-from-HEAD model: every leaf branch is cut from
# main HEAD and only *named* impl/<root>-<item> (requiem cannot make it descend
# from feature/<root>). We then advance the trunk by merging leaf #1 and observe
# whether leaf #2/#3 still merge cleanly.
#
# Two regimes, two roots, one scratch repo:
#   root 9100 = DISJOINT leaves  (each edits a distinct file)
#   root 9200 = OVERLAPPING leaves (each edits the SAME line of one file)
#
# Decision rule (from the briefing):
#   clean    -> wire the straight sequence
#   conflict -> build rebase_onto_target first
set -uo pipefail
export PATH="/c/Program Files/GitHub CLI:$PATH"

REPO="PolyphonyRequiem/requiem-scratch-adr0018"
WORK="$(mktemp -d)/scratch"
echo "### gh: $(gh --version | head -1)"
echo "### work dir: $WORK"
echo "### repo: $REPO"
echo

git clone --quiet "https://github.com/${REPO}.git" "$WORK" || { echo "CLONE FAILED"; exit 1; }
cd "$WORK" || exit 1
git config user.name "requiem-q1-probe"
git config user.email "noreply@polyphonyrequiem.dev"

# Reset main to a known clean seed so re-runs are deterministic.
DEFAULT_BRANCH="$(gh repo view "$REPO" --json defaultBranchRef -q .defaultBranchRef.name)"
echo "### detected default branch: $DEFAULT_BRANCH"
git checkout --quiet "$DEFAULT_BRANCH"

seed_files() {
  printf 'alpha base\n'  > alpha.txt
  printf 'beta base\n'   > beta.txt
  printf 'gamma base\n'  > gamma.txt
  # shared.txt: the overlap target. Line 3 is what overlapping leaves all edit.
  printf 'shared header\n----\nVALUE = 0\n----\nshared footer\n' > shared.txt
  git add alpha.txt beta.txt gamma.txt shared.txt
  git commit --quiet -m "seed: reset probe fixtures" --allow-empty
  git push --quiet --force origin "$DEFAULT_BRANCH"
}
echo "### seeding $DEFAULT_BRANCH fixtures (force, deterministic)"
seed_files
MAIN_SHA="$(git rev-parse HEAD)"
echo "### main HEAD now: $MAIN_SHA"
echo

# ---- helpers ----------------------------------------------------------------

bootstrap_trunk() {  # $1=root  — the SAME GitHub refs API call requiem makes
  local root="$1" trunk="feature/$1"
  echo ">>> trunk_bootstrap: ensure $trunk off $DEFAULT_BRANCH@$MAIN_SHA"
  # mirror gh.branch_sha + gh.ensure_branch_ref (POST /git/refs, never force)
  if gh api "repos/${REPO}/git/ref/heads/${trunk}" >/dev/null 2>&1; then
    echo "    trunk already exists (idempotent no-op)"
  else
    gh api -X POST "repos/${REPO}/git/refs" \
      -f ref="refs/heads/${trunk}" -f sha="${MAIN_SHA}" >/dev/null \
      && echo "    created $trunk" || { echo "    CREATE FAILED"; return 1; }
  fi
}

make_leaf() {  # $1=root $2=item $3=mutator-fn  — cut from MAIN (worktree model)
  local root="$1" item="$2" mut="$3" br="impl/$1-$2"
  git checkout --quiet -B "$br" "$MAIN_SHA"      # <-- rooted at main, NOT trunk
  "$mut" "$item"
  git commit --quiet -am "leaf $root-$item: $mut"
  git push --quiet --force origin "$br"
  echo "    pushed $br (cut from main $MAIN_SHA)"
}

open_leaf_pr() {  # $1=root $2=item  -> echoes PR number
  local root="$1" item="$2" br="impl/$1-$2" trunk="feature/$1"
  gh pr create --repo "$REPO" --base "$trunk" --head "$br" \
    --title "leaf $root-$item -> $trunk" --body "ADR-0018 Q1 probe leaf" \
    >/dev/null 2>&1
  gh pr list --repo "$REPO" --head "$br" --base "$trunk" --state open \
    --json number -q '.[0].number'
}

# mergeable state, polling until GitHub finishes async computation
pr_mergeable() {  # $1=pr-number -> "MERGEABLE|CONFLICTING|UNKNOWN <stateStatus>"
  local n="$1" m ss tries=0
  while :; do
    m="$(gh pr view "$n" --repo "$REPO" --json mergeable -q .mergeable 2>/dev/null)"
    ss="$(gh pr view "$n" --repo "$REPO" --json mergeStateStatus -q .mergeStateStatus 2>/dev/null)"
    [ "$m" != "UNKNOWN" ] && break
    tries=$((tries+1)); [ "$tries" -ge 15 ] && break
    sleep 2
  done
  echo "$m $ss"
}

# mutators
edit_alpha() { printf 'alpha touched by leaf %s\n' "$1" >> alpha.txt; }
edit_beta()  { printf 'beta touched by leaf %s\n'  "$1" >> beta.txt; }
edit_gamma() { printf 'gamma touched by leaf %s\n' "$1" >> gamma.txt; }
edit_shared(){ sed -i "s/^VALUE = .*/VALUE = $1/" shared.txt; }  # SAME line -> overlap

# ---- scenario runner --------------------------------------------------------

run_scenario() {  # $1=root  $2..=mutator per item (item ids are 1..N)
  local root="$1"; shift
  local muts=("$@") trunk="feature/$root"
  echo "==================================================================="
  echo "SCENARIO root=$root  trunk=$trunk  leaves=${#muts[@]}  muts=(${muts[*]})"
  echo "==================================================================="
  bootstrap_trunk "$root" || return 1
  local i=1 nums=()
  for mut in "${muts[@]}"; do
    echo ">>> leaf $root-$i ($mut)"
    make_leaf "$root" "$i" "$mut"
    local n; n="$(open_leaf_pr "$root" "$i")"
    nums+=("$n")
    echo "    leaf PR #$n  ($mut)"
    i=$((i+1))
  done
  echo
  echo ">>> BEFORE any trunk merge — mergeability of every leaf PR:"
  i=1; for n in "${nums[@]}"; do echo "    leaf $root-$i PR#$n : $(pr_mergeable "$n")"; i=$((i+1)); done
  echo
  echo ">>> MERGE leaf #1 (PR#${nums[0]}) into $trunk (merge-commit)..."
  if gh pr merge "${nums[0]}" --repo "$REPO" --merge >/dev/null 2>&1; then
    echo "    leaf #1 merged OK; $trunk advanced."
  else
    echo "    leaf #1 MERGE FAILED:"; gh pr merge "${nums[0]}" --repo "$REPO" --merge 2>&1 | head -5
  fi
  # let GitHub recompute downstream mergeability
  sleep 3
  echo
  echo ">>> AFTER leaf #1 merged — mergeability of remaining leaf PRs:"
  i=2
  for n in "${nums[@]:1}"; do
    local state; state="$(pr_mergeable "$n")"
    echo "    leaf $root-$i PR#$n : $state"
    i=$((i+1))
  done
  echo
  echo ">>> ATTEMPT merge of leaf #2 (PR#${nums[1]}) into advanced $trunk:"
  if gh pr merge "${nums[1]}" --repo "$REPO" --merge >/dev/null 2>&1; then
    echo "    RESULT: leaf #2 merged CLEANLY into advanced trunk  => NO DRIFT"
  else
    echo "    RESULT: leaf #2 REFUSED by GitHub => DRIFT BITES. gh says:"
    gh pr merge "${nums[1]}" --repo "$REPO" --merge 2>&1 | head -6 | sed 's/^/        /'
  fi
  echo
}

# DISJOINT: distinct files -> expect clean even after trunk advances
run_scenario 9100 edit_alpha edit_beta edit_gamma
# OVERLAPPING: same line of shared.txt -> expect conflict after #1 merges
run_scenario 9200 edit_shared edit_shared edit_shared

echo "### probe complete. work dir: $WORK"
