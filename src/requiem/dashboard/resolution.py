"""requiem.dashboard.resolution — the guarded gate-resolution write path (phase 2).

ADR-0019 §4. The read-only dashboard observes the pending-gate queue; this module
lets an operator *resolve* a gate from the browser. It is the one place the
dashboard mutates run state, so every safety property lives here:

* **Append-only.** It never rewrites the log — it appends one ``gate_resolved``
  event via the **same** ``EventStore`` + ``EventEmitter`` the kernel uses, so the
  envelope is byte-identical (event_id sequencing, ts, payload shape) to a
  resolution the engine itself would have written. The kernel's resume path
  already consumes a pre-recorded ``gate_resolved`` (kernel.py: ``_RouteAfterGate``)
  — so a separate ``requiem resume`` continues the run. The dashboard does **not**
  run the engine itself.
* **State-guarded.** It refuses unless the run is genuinely parked at an open
  gate (the last gate-relevant event is ``gate_opened`` with no following
  ``gate_resolved``/``run_completed``). No double-resolve, no resolving a finished
  or running run.
* **Choice-guarded.** The choice must be one of the gate's *offered* options.
  The kernel routes on ``needs_human:{choice}``; an unoffered choice would
  ``Failed(route.missing)`` the run on resume — so we reject it here, before the
  append, with the offered set echoed back.

The result is a small, auditable surface: a bad request can at worst be refused;
it can never half-write or corrupt the authoritative log.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from requiem.dashboard.projection import LOG_SUFFIX, run_detail
from requiem.events import EventEmitter
from requiem.persistence import EventStore


class GateResolutionError(RuntimeError):
    """A gate resolution was refused. ``reason`` is a machine-stable tag."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class GateResolution:
    """The outcome of a successful resolution append."""

    run_id: str
    node: str
    choice: str
    event_id: int

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "node": self.node,
            "choice": self.choice,
            "event_id": self.event_id,
            "resolved": True,
        }


def resolve_gate(log_dir: Path, run_id: str, choice: str) -> GateResolution:
    """Append a validated ``gate_resolved`` for ``run_id``'s open gate.

    Raises :class:`GateResolutionError` (without writing anything) if:

    * ``run_not_found`` — no log for ``run_id``.
    * ``not_at_gate`` — the run is not currently suspended on an open gate.
    * ``invalid_choice`` — ``choice`` is not one of the gate's offered options.

    On success, returns the :class:`GateResolution` and the run is left for a
    separate ``requiem resume`` to continue (the dashboard never runs the engine).
    """
    log_path = log_dir / f"{run_id}{LOG_SUFFIX}"
    if not log_path.exists():
        raise GateResolutionError("run_not_found", f"no such run {run_id!r}")

    detail = run_detail(log_dir, run_id)
    if detail is None:
        raise GateResolutionError("run_not_found", f"no such run {run_id!r}")
    if detail.corrupt is not None:
        raise GateResolutionError(
            "corrupt_log", f"run {run_id!r} log is corrupt; refusing to write")

    gate = detail.gate
    if gate is None:
        raise GateResolutionError(
            "not_at_gate",
            f"run {run_id!r} is not awaiting a human gate (status {detail.status!r})",
        )

    node = gate.get("node")
    options = list(gate.get("options") or [])
    if choice not in options:
        raise GateResolutionError(
            "invalid_choice",
            f"choice {choice!r} is not offered; valid options: {options}",
        )

    # Append via the kernel's own emitter so the envelope is byte-identical.
    store = EventStore(log_path)
    emitter = EventEmitter(run_id, store.append)
    before = store._next_id  # the id this append will receive
    emitter.emit_gate_resolved(node, choice, auto=False)
    return GateResolution(
        run_id=run_id, node=str(node), choice=choice, event_id=before,
    )
