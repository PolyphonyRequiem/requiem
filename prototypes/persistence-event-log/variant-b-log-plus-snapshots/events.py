"""Event schema shared by all variants (copied per-variant for self-containment).

Bach's rule: every persisted shape is a discriminated union by `kind`.
Unknown kinds are PRESERVED on read (forward-compat) but REFUSE to alter
state — see `Projection.apply` in each variant.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _EventBase(BaseModel):
    model_config = ConfigDict(extra="allow")  # forward-compat: keep unknown fields
    event_id: int
    run_id: str
    ts: datetime = Field(default_factory=_now)


class RunStarted(_EventBase):
    kind: Literal["run_started"] = "run_started"
    root_id: int
    platform_project: str
    created_by: str
    branch_model_version: int = 1


class NodeEntered(_EventBase):
    kind: Literal["node_entered"] = "node_entered"
    node: str


class NodeCompleted(_EventBase):
    kind: Literal["node_completed"] = "node_completed"
    node: str
    outcome: str  # Success | RetryableFailure | PermanentFailure | NeedsHuman | Cancelled


class MgDeclared(_EventBase):
    kind: Literal["mg_declared"] = "mg_declared"
    mg_id: str
    mg_path: str
    parent_mg_path: Optional[str] = None
    items: list[int] = Field(default_factory=list)
    nesting: Literal["top", "nested"] = "top"
    isolation: Literal["per-merge-group", "per-item"] = "per-merge-group"


class PlanGenerationBumped(_EventBase):
    kind: Literal["plan_generation_bumped"] = "plan_generation_bumped"
    item_key: str  # "root" or numeric id-as-string
    cause: str    # e.g. "plan_pr_merged:#42"
    pr_number: Optional[int] = None
    merge_commit: Optional[str] = None


class HumanApprovalRecorded(_EventBase):
    kind: Literal["human_approval_recorded"] = "human_approval_recorded"
    gate: str
    approved_by: str
    detail: Optional[str] = None


class MgRetired(_EventBase):
    kind: Literal["mg_retired"] = "mg_retired"
    mg_id: str
    reason: Optional[str] = None


class SubworkflowInvoked(_EventBase):
    kind: Literal["subworkflow_invoked"] = "subworkflow_invoked"
    parent_node: str
    sub_run_id: str
    workflow: str


class SubworkflowCompleted(_EventBase):
    kind: Literal["subworkflow_completed"] = "subworkflow_completed"
    sub_run_id: str
    outcome: str


class RunEnded(_EventBase):
    kind: Literal["run_ended"] = "run_ended"
    outcome: str


# Discriminated union. Adding a new kind = add the class + extend the union
# in your own copy. Pure-log designs MUST round-trip unknown kinds (see
# `read_event_lenient` below).
Event = Annotated[
    Union[
        RunStarted,
        NodeEntered,
        NodeCompleted,
        MgDeclared,
        PlanGenerationBumped,
        HumanApprovalRecorded,
        MgRetired,
        SubworkflowInvoked,
        SubworkflowCompleted,
        RunEnded,
    ],
    Field(discriminator="kind"),
]

KNOWN_KINDS: frozenset[str] = frozenset(
    {
        "run_started",
        "node_entered",
        "node_completed",
        "mg_declared",
        "plan_generation_bumped",
        "human_approval_recorded",
        "mg_retired",
        "subworkflow_invoked",
        "subworkflow_completed",
        "run_ended",
    }
)
