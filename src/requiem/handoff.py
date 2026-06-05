"""Handoff metadata — the kanban worker→requiem wire contract (ADR-0017 §4).

When a fleet worker finishes a leaf it calls ``kanban_complete(summary=...,
metadata={...})``. That ``metadata`` blob is Requiem's **read-side API**: the
structured evidence the executor reads to decide what happened and to drive
close_out. Because it crosses a process boundary (and is produced by a model in
a profile Requiem does not control at run time), it is treated as an *untrusted
wire protocol*, not as truth:

* It is **schema-versioned**. An unknown/future ``schema_version`` fails closed —
  Requiem never guesses at evidence shaped by a contract it doesn't speak.
* **Identity fields are required and strict** (``leaf_id``, ``root_item``,
  ``plan_hash``, ``worker_profile``) so evidence can never be misattributed to
  the wrong leaf or a stale plan.
* **Evidence fields are optional** (``branch``, ``commit_sha``, ``pr_url``,
  ``changed_files``, ``tests_run``, ``worker_profile_version``). Their *absence*
  is itself information the verifier/close_out adjudicates; Requiem independently
  verifies the claims it can (branch exists, PR is real, tests ran) rather than
  trusting the self-report.

This module is the single owner of the contract. The golden fixture in
``tests/`` is co-owned by the kanban-delivery and profile-distribution tracks:
neither side changes the emitted/consumed shape without updating the fixture
(prevents the Tchaikovsky-class drift, ADR-0017 "Division of labor").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

HANDOFF_SCHEMA_VERSION = 1

_REQUIRED_STR_FIELDS = ("leaf_id", "root_item", "plan_hash", "worker_profile")


class HandoffError(Exception):
    """Raised when a handoff metadata payload violates the wire contract."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        self.field = field
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class HandoffMetadata:
    """Parsed, validated worker evidence for one delivered leaf."""

    schema_version: int
    leaf_id: str
    root_item: str
    plan_hash: str
    worker_profile: str
    branch: str | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    changed_files: tuple[str, ...] = ()
    tests_run: tuple[str, ...] = ()
    worker_profile_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe payload symmetric with :func:`parse_handoff`."""
        return {
            "schema_version": self.schema_version,
            "leaf_id": self.leaf_id,
            "root_item": self.root_item,
            "plan_hash": self.plan_hash,
            "worker_profile": self.worker_profile,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "pr_url": self.pr_url,
            "changed_files": list(self.changed_files),
            "tests_run": list(self.tests_run),
            "worker_profile_version": self.worker_profile_version,
        }


def _require_str(raw: Mapping[str, Any], key: str) -> str:
    val = raw.get(key)
    if not isinstance(val, str) or val.strip() == "":
        raise HandoffError(
            f"handoff field '{key}' must be a non-empty string", field=key
        )
    return val


def _optional_str(raw: Mapping[str, Any], key: str) -> str | None:
    val = raw.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise HandoffError(f"handoff field '{key}' must be a string", field=key)
    return val


def _str_tuple(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    val = raw.get(key)
    if val is None:
        return ()
    if isinstance(val, str) or not isinstance(val, Sequence):
        raise HandoffError(
            f"handoff field '{key}' must be a list of strings", field=key
        )
    out: list[str] = []
    for elem in val:
        if not isinstance(elem, str):
            raise HandoffError(
                f"handoff field '{key}' entries must be strings", field=key
            )
        out.append(elem)
    return tuple(out)


def parse_handoff(raw: Mapping[str, Any]) -> HandoffMetadata:
    """Validate a worker ``metadata`` blob into a :class:`HandoffMetadata`.

    Raises :class:`HandoffError` on a missing/unknown ``schema_version``, a
    missing/empty identity field, or a wrongly-typed field. Unknown extra keys
    are tolerated for forward compatibility.
    """
    if not isinstance(raw, Mapping):
        raise HandoffError(
            f"handoff metadata must be a mapping, got {type(raw).__name__}"
        )

    version = raw.get("schema_version")
    if version is None:
        raise HandoffError("handoff metadata missing 'schema_version'",
                           field="schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise HandoffError("handoff 'schema_version' must be an integer",
                           field="schema_version")
    if version != HANDOFF_SCHEMA_VERSION:
        # Fail closed: an unrecognised version is a contract Requiem doesn't
        # speak — never partially interpret it.
        raise HandoffError(
            f"unsupported handoff schema_version {version} "
            f"(this build speaks v{HANDOFF_SCHEMA_VERSION})",
            field="schema_version",
        )

    fields = {key: _require_str(raw, key) for key in _REQUIRED_STR_FIELDS}
    return HandoffMetadata(
        schema_version=version,
        leaf_id=fields["leaf_id"],
        root_item=fields["root_item"],
        plan_hash=fields["plan_hash"],
        worker_profile=fields["worker_profile"],
        branch=_optional_str(raw, "branch"),
        commit_sha=_optional_str(raw, "commit_sha"),
        pr_url=_optional_str(raw, "pr_url"),
        changed_files=_str_tuple(raw, "changed_files"),
        tests_run=_str_tuple(raw, "tests_run"),
        worker_profile_version=_optional_str(raw, "worker_profile_version"),
    )


def extract_handoff(run_raw: Mapping[str, Any]) -> HandoffMetadata | None:
    """Pull the handoff out of a ``KanbanRun.raw`` payload, if present.

    Hermes stores the worker's ``kanban_complete`` metadata under a ``metadata``
    key on the run row. This is the one place the Hermes-side JSON shape couples
    in; isolate it here so a Hermes change is a one-line edit. Returns ``None``
    when the run carries no metadata blob at all (an evidence-less completion the
    verifier must treat as weak).
    """
    blob = run_raw.get("metadata")
    if blob is None:
        return None
    return parse_handoff(blob)
