"""requiem.seam — in-process runtime-seam propagation for dispatched children.

ADR-0020 (ADR-0013 blocker B1). When a parent workflow invokes a sub-workflow,
the kernel reconstructs the child engine from the parent's **recorded inputs** —
a JSON-flat dict (INV-EVENT-LOG-AUTHORITATIVE). The live runtime seams (the LLM
``provider``, the real ``toolbelt`` of git/gh/twig/fs, and the ``gate_handler``)
are not JSON-serialisable, so they were dropped — and a child ``build_engine``
that treats missing seams as "synthesize a demo" would silently run fakes over
real git and report success. That was the B1 footgun.

This module generalises the contextvar idiom ``planning.py`` already used for its
own recursive children into one shared seam the **kernel installs once per run**.
A child ``build_engine`` resolves each seam as **explicit arg → active seam →
demo fallback**, so:

* an in-process dispatched child inherits the parent's real provider/toolbelt/
  gate_handler instead of faking them (the footgun closes);
* a caller that passes explicit seams still wins (tests are unaffected);
* a bare demo invocation (no seam installed, no explicit args) still gets its
  canned demo, so the CLI demo paths keep working.

Restart safety: the seam is **in-process only**. On resume the kernel
reconstructs children from recorded inputs exactly as before and re-installs the
seam from the resuming root engine; the contextvar is never consulted for
correctness across a restart (recorded inputs + the ``start_run`` snapshot remain
authoritative — INV-RESTART). For a future *parallel* fork each branch must
capture and re-``install`` the snapshot in its own task (contextvars are
per-task); the current sub-workflow topology is sequential, so inheritance within
the same task is sufficient.
"""
from __future__ import annotations

import contextlib
import contextvars
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:  # avoid import cycles at module load (kernel imports light)
    from requiem.agent import AgentProvider
    from requiem.toolbelt import Toolbelt


# Module-level seams, inherited by any child engine constructed in the same
# asyncio task after `install(...)`.
active_provider: contextvars.ContextVar["AgentProvider | None"] = (
    contextvars.ContextVar("requiem.seam.active_provider", default=None)
)
active_toolbelt: contextvars.ContextVar["Toolbelt | None"] = (
    contextvars.ContextVar("requiem.seam.active_toolbelt", default=None)
)
active_gate_handler: contextvars.ContextVar[Any] = (
    contextvars.ContextVar("requiem.seam.active_gate_handler", default=None)
)


def get_provider() -> "AgentProvider | None":
    return active_provider.get()


def get_toolbelt() -> "Toolbelt | None":
    return active_toolbelt.get()


def get_gate_handler() -> Any:
    return active_gate_handler.get()


def set_seams(
    *,
    provider: "AgentProvider | None" = None,
    toolbelt: "Toolbelt | None" = None,
    gate_handler: Any = None,
) -> None:
    """Set the active seams for the current task (no automatic restore).

    Used by the kernel just before constructing a child engine so the child's
    ``build_engine`` can inherit them. Only non-None values overwrite — passing
    ``None`` for a seam leaves any already-installed value in place, so a parent
    that itself only has a provider doesn't wipe an outer toolbelt.
    """
    if provider is not None:
        active_provider.set(provider)
    if toolbelt is not None:
        active_toolbelt.set(toolbelt)
    if gate_handler is not None:
        active_gate_handler.set(gate_handler)


@contextlib.contextmanager
def install(
    *,
    provider: "AgentProvider | None" = None,
    toolbelt: "Toolbelt | None" = None,
    gate_handler: Any = None,
) -> Iterator[None]:
    """Install seams for the duration of the ``with`` block, restoring prior
    tokens on exit. Use this when you want scoped propagation rather than a
    task-lifetime ``set_seams``.
    """
    tokens = []
    if provider is not None:
        tokens.append((active_provider, active_provider.set(provider)))
    if toolbelt is not None:
        tokens.append((active_toolbelt, active_toolbelt.set(toolbelt)))
    if gate_handler is not None:
        tokens.append((active_gate_handler, active_gate_handler.set(gate_handler)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
