"""requiem.branch_model — the Option-D merge-group branch topology (ADR-0006).

Single authority for the four ref-classes that make up a Requiem run's branch
topology, so no workflow hand-rolls a branch-name f-string (and so the
``{root}-{item}`` payload parses back unambiguously for PR attribution)::

    main
     └── feature/<root>                integration trunk (one per run)
          ├── plan/<root>              plan PR → trunk
          ├── impl/<root>-<item>       one per implementable leaf → trunk
          └── evidence/<root>-<item>   evidence branch for a leaf

Per ADR-0006 option D the recursive ``mg/<root>_<path>`` layer is collapsed, so
there are **two delimiters only**: ``/`` separates the ref-class from the
payload, and ``-`` separates ``{root}-{item}`` inside the impl/evidence
payload. There is no ``_`` and no nested ``mg/``. Ids must therefore be
delimiter-free so ``<root>-<item>`` round-trips through :func:`parse_branch`.

Ids that can't be encoded as a safe git-ref segment raise
:class:`BranchModelError` — we fail closed rather than emit a malformed ref
(INV-NO-CORRUPT-FORWARD). ADO work-item ids (integers) and the simple
alphanumeric run roots used in fixtures both satisfy the segment rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

FEATURE = "feature"
PLAN = "plan"
IMPL = "impl"
EVIDENCE = "evidence"

ROOT_REF_CLASSES = (FEATURE, PLAN)
LEAF_REF_CLASSES = (IMPL, EVIDENCE)

# A run-id / item-id segment. ADO work-item ids are integers; simple
# alphanumeric roots (e.g. the ``demo`` fixtures) are also allowed. The set is
# deliberately delimiter-free (no ``/``, ``-`` or ``_``) so the
# ``<root>-<item>`` payload stays unambiguous, and it is a strict subset of
# git's ref-name rules so every produced name is a valid branch.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9]+$")


class BranchModelError(ValueError):
    """Raised when an id cannot be encoded as a safe branch segment."""


def _seg(label: str, value: object) -> str:
    s = str(value)
    if not _SEGMENT_RE.match(s):
        raise BranchModelError(
            f"{label} {value!r} is not a valid branch segment: must be "
            "non-empty alphanumeric (no '/', '-', '_', whitespace, or other "
            "git-ref-unsafe characters)"
        )
    return s


def feature_trunk(root: object) -> str:
    """The integration trunk for a run: ``feature/<root>``."""
    return f"{FEATURE}/{_seg('root', root)}"


def plan_branch(root: object) -> str:
    """The plan-PR branch for a run: ``plan/<root>``."""
    return f"{PLAN}/{_seg('root', root)}"


def impl_branch(root: object, item: object) -> str:
    """The implementation branch for one leaf: ``impl/<root>-<item>``."""
    return f"{IMPL}/{_seg('root', root)}-{_seg('item', item)}"


def evidence_branch(root: object, item: object) -> str:
    """The evidence branch for one leaf: ``evidence/<root>-<item>``."""
    return f"{EVIDENCE}/{_seg('root', root)}-{_seg('item', item)}"


@dataclass(frozen=True, slots=True)
class BranchRef:
    """A parsed Requiem branch name.

    ``item`` is ``None`` for the root-only ref-classes (``feature``/``plan``).
    """

    ref_class: str
    root: str
    item: str | None = None

    @property
    def is_leaf_ref(self) -> bool:
        return self.ref_class in LEAF_REF_CLASSES

    def rebuild(self) -> str:
        """Reconstruct the canonical branch name (round-trip check)."""
        if self.ref_class == FEATURE:
            return feature_trunk(self.root)
        if self.ref_class == PLAN:
            return plan_branch(self.root)
        if self.ref_class == IMPL:
            return impl_branch(self.root, self.item)
        if self.ref_class == EVIDENCE:
            return evidence_branch(self.root, self.item)
        raise BranchModelError(f"unknown ref_class {self.ref_class!r}")


def parse_branch(name: str) -> BranchRef | None:
    """Inverse of the constructors.

    Returns ``None`` for any name that is not a Requiem Option-D ref, so callers
    can cheaply test membership (e.g. PR-attribution back-resolution) without
    catching exceptions.
    """
    if name.count("/") != 1:
        return None
    ref_class, _, payload = name.partition("/")
    if ref_class in ROOT_REF_CLASSES:
        if not _SEGMENT_RE.match(payload):
            return None
        return BranchRef(ref_class=ref_class, root=payload, item=None)
    if ref_class in LEAF_REF_CLASSES:
        root, sep, item = payload.partition("-")
        if not sep or not _SEGMENT_RE.match(root) or not _SEGMENT_RE.match(item):
            return None
        return BranchRef(ref_class=ref_class, root=root, item=item)
    return None
