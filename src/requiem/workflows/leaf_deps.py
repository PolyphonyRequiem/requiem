"""Shared inter-leaf dependency validation and wave scheduling.

Both fan-out backends — the in-process ``fanout`` workflow (ADR-0021) and the
Hermes ``kanban_executor`` (ADR-0014) — dispatch a flat list of leaves that
MAY declare ``deps`` on sibling leaf ids. Before this module existed,
``kanban_executor`` carried its own inline dependency-graph validation and
wave-release logic while ``fanout`` had none at all — two implementations
(one real, one absent) of what should be a single seam. This module is that
seam: both backends call the same pure functions, so a fix or a bug lands
(or is caught) in exactly one place.

Callers own all I/O (dispatching a leaf, merging a PR, polling a board) —
everything here is a pure function over plain dicts/sets of leaf ids
(``str``), making it trivial to unit test without a kanban board, a git repo,
or an LLM in the loop.
"""

from __future__ import annotations


def validate_dep_graph(
    deps_of: dict[str, tuple[str, ...]],
) -> tuple[str | None, tuple[str, ...]]:
    """Validate a dependency graph and compute its ready frontier.

    Returns ``(error, ready_frontier)``. ``error`` is set (and
    ``ready_frontier`` empty) for an unknown dep, a self-dep, or a cycle —
    callers MUST fail closed on a non-``None`` error rather than dispatch
    anything. ``ready_frontier`` is the ids with no dependencies at all —
    the only leaves safe to release before any leaf has settled.
    """
    ids = set(deps_of)
    for lid, deps in deps_of.items():
        for dep in deps:
            if dep == lid:
                return f"leaf {lid!r} depends on itself", ()
            if dep not in ids:
                return f"leaf {lid!r} depends on unknown leaf {dep!r}", ()

    # Kahn's algorithm: if we cannot topologically order the graph there is a
    # cycle, which would leave dependent leaves permanently held (deadlock).
    pending = {lid: set(deps) for lid, deps in deps_of.items()}
    ordered: list[str] = []
    frontier = [lid for lid, d in pending.items() if not d]
    while frontier:
        lid = frontier.pop()
        ordered.append(lid)
        for other, d in pending.items():
            if lid in d:
                d.discard(lid)
                if not d and other not in ordered and other not in frontier:
                    frontier.append(other)
    if len(ordered) != len(pending):
        cyclic = sorted(lid for lid, d in pending.items() if d)
        return f"dependency cycle among leaves {cyclic}", ()

    ready = tuple(lid for lid, deps in deps_of.items() if not deps)
    return None, ready


def compute_blocked(
    deps_of: dict[str, tuple[str, ...]],
    *,
    nondelivered: set[str],
    settled: set[str],
    already_blocked: frozenset[str] = frozenset(),
) -> set[str]:
    """Propagate blocking transitively through the dependency graph.

    A leaf is blocked when ANY of its dependencies settled non-delivered
    (``needs_human`` / ``failed`` / itself blocked) — it can never become
    dispatchable, so it must be reported, not silently retried forever.
    ``settled`` is every leaf that has reached a terminal state (delivered
    or not); a leaf already in ``settled`` is never (re)marked blocked.
    """
    blocked = set(already_blocked)
    changed = True
    while changed:
        changed = False
        for lid, deps in deps_of.items():
            if lid in settled or lid in blocked:
                continue
            if any(d in nondelivered or d in blocked for d in deps):
                blocked.add(lid)
                changed = True
    return blocked


def releasable_leaves(
    deps_of: dict[str, tuple[str, ...]],
    *,
    delivered: set[str],
    settled: set[str],
    blocked: set[str],
) -> set[str]:
    """Leaves not yet settled/blocked whose dependencies are ALL delivered.

    Safe to dispatch (or release to a worker) right now. Includes leaves
    with no dependencies at all — callers that already released those at
    an earlier step should intersect with their own "not yet started" set.
    """
    return {
        lid for lid, deps in deps_of.items()
        if lid not in settled and lid not in blocked
        and all(d in delivered for d in deps)
    }
