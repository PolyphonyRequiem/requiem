"""Rachmaninov resume-fidelity fixture workflows.

These are deliberately *minimal* shapes (a pure gate-in-middle workflow and
a multi-node edge-loop workflow) used by ``tests/test_resume_fidelity_matrix.py``
to exhaustively truncate the event log at every event and assert resume parity.

Why minimal? ``code_review_demo`` already exercises script + agent + team +
gate composition (and is the canonical run for ``tests/test_resume_fidelity.py``).
The fixtures here isolate two crash-point classes the brief calls out
separately:

* **gate-in-middle** — pins the kernel's ``_AwaitingGate`` /
  ``_RouteAfterGate`` cursor states around ``gate_opened`` and
  ``gate_resolved`` truncations.
* **edge-loop** — distinct from the single-node retry loop that
  ``flaky_lint`` exercises; an edge-loop re-enters a *different* node and
  so re-binds ``completed`` slots, which is a different fold input for
  ``_reconstruct``.
"""
