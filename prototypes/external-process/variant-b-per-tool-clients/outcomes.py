"""Per-method, per-tool outcome unions.

Contrast with Variant A: there is no shared ProcessOutcome layer. Each tool
method has its own outcome shape — the verb gets domain-typed results directly,
without doing exit-code stratification itself. The classification logic lives
inside the typed client.

Trade-off (Ravel's framing): the client owns the tool-version coupling. If
`gh` changes its stderr wording for rate-limit errors, only `GhClient` changes;
every verb stays correct. But the client surface grows as we need new methods.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


# ---- git rev-parse ------------------------------------------------------

class RevParseResolved(BaseModel):
    kind: Literal["resolved"] = "resolved"
    sha: str


class RevParseUnknownRef(BaseModel):
    kind: Literal["unknown_ref"] = "unknown_ref"
    ref: str
    stderr: str


class RevParseNotARepo(BaseModel):
    kind: Literal["not_a_repo"] = "not_a_repo"
    cwd: str


class RevParseToolMissing(BaseModel):
    kind: Literal["tool_missing"] = "tool_missing"


class RevParseTimeout(BaseModel):
    kind: Literal["timeout"] = "timeout"
    timeout_s: float


class RevParseUnknown(BaseModel):
    """Catch-all — Ravel L-1: never silently re-route to retryable."""
    kind: Literal["unknown"] = "unknown"
    exit_code: int
    stderr: str


GitRevParseOutcome = Annotated[
    Union[
        RevParseResolved,
        RevParseUnknownRef,
        RevParseNotARepo,
        RevParseToolMissing,
        RevParseTimeout,
        RevParseUnknown,
    ],
    Field(discriminator="kind"),
]


# ---- gh pr view ---------------------------------------------------------

class PrViewFound(BaseModel):
    kind: Literal["found"] = "found"
    raw_json: str


class PrViewNotFound(BaseModel):
    kind: Literal["not_found"] = "not_found"
    pr_number: int


class PrViewRateLimited(BaseModel):
    kind: Literal["rate_limited"] = "rate_limited"
    stderr: str


class PrViewServerError(BaseModel):
    kind: Literal["server_error"] = "server_error"
    stderr: str


class PrViewAuthLapse(BaseModel):
    kind: Literal["auth_lapse"] = "auth_lapse"
    stderr: str


class PrViewToolMissing(BaseModel):
    kind: Literal["tool_missing"] = "tool_missing"


class PrViewTimeout(BaseModel):
    kind: Literal["timeout"] = "timeout"
    timeout_s: float


class PrViewUnknown(BaseModel):
    kind: Literal["unknown"] = "unknown"
    exit_code: int
    stderr: str


GhPrViewOutcome = Annotated[
    Union[
        PrViewFound,
        PrViewNotFound,
        PrViewRateLimited,
        PrViewServerError,
        PrViewAuthLapse,
        PrViewToolMissing,
        PrViewTimeout,
        PrViewUnknown,
    ],
    Field(discriminator="kind"),
]


# ---- canonical verb outcome (same as Variant A) -------------------------

class VerbSuccess(BaseModel):
    kind: Literal["success"] = "success"
    value: dict


class RetryableFailure(BaseModel):
    kind: Literal["retryable"] = "retryable"
    reason: str
    retry_key: str


class PermanentFailure(BaseModel):
    kind: Literal["permanent"] = "permanent"
    reason: str


class NeedsHuman(BaseModel):
    kind: Literal["needs_human"] = "needs_human"
    reason: str
    diagnostic: dict


class Cancelled(BaseModel):
    kind: Literal["cancelled"] = "cancelled"


VerbOutcome = Annotated[
    Union[VerbSuccess, RetryableFailure, PermanentFailure, NeedsHuman, Cancelled],
    Field(discriminator="kind"),
]
