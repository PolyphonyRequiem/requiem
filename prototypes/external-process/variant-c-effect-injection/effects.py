"""Effect protocols and outcomes — the capability surface a verb may declare.

Variant C's distinguishing move: a verb is a plain function whose parameters
declare which capabilities it needs. The runtime inspects the signature and
injects either real or fake implementations.

This is the most explicit about side effects (every effect is visible in the
signature) and the most testable (no globals; fakes are passed in by name).
It's also the least familiar idiom in Python, so the runtime needs to earn
its keep — see README §VARIANT COMPARISON.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Protocol, Union

from pydantic import BaseModel, Field


# ---- typed outcomes (shape mirrors Variant B; same Ravel L-1 discipline) ----

class GitSha(BaseModel):
    kind: Literal["sha"] = "sha"
    sha: str


class GitMissingRef(BaseModel):
    kind: Literal["missing_ref"] = "missing_ref"
    ref: str


class GitNotARepo(BaseModel):
    kind: Literal["not_a_repo"] = "not_a_repo"


class GitFailure(BaseModel):
    """Catch-all — never silently retryable."""
    kind: Literal["failure"] = "failure"
    detail: str
    is_timeout: bool = False
    is_missing_tool: bool = False


GitRevParseOutcome = Annotated[
    Union[GitSha, GitMissingRef, GitNotARepo, GitFailure],
    Field(discriminator="kind"),
]


class GhPrFound(BaseModel):
    kind: Literal["found"] = "found"
    raw_json: str


class GhPrMissing(BaseModel):
    kind: Literal["missing"] = "missing"
    pr_number: int


class GhTransient(BaseModel):
    kind: Literal["transient"] = "transient"
    reason: str  # 'rate_limit' | 'server_error' | 'timeout'


class GhAuthLapse(BaseModel):
    kind: Literal["auth_lapse"] = "auth_lapse"
    stderr: str


class GhFailure(BaseModel):
    kind: Literal["failure"] = "failure"
    detail: str
    is_missing_tool: bool = False


GhPrViewOutcome = Annotated[
    Union[GhPrFound, GhPrMissing, GhTransient, GhAuthLapse, GhFailure],
    Field(discriminator="kind"),
]


# ---- effect protocols ----

class Git(Protocol):
    def rev_parse(self, repo: Path, ref: str, *, timeout_s: float = 5.0) -> GitRevParseOutcome: ...


class Gh(Protocol):
    def pr_view(self, repo: Path, pr_number: int, *, timeout_s: float = 15.0) -> GhPrViewOutcome: ...


class Clock(Protocol):
    """Wall-clock effect — kept here as a demonstration that non-process side effects
    fit the same injection pattern (helpful for retry_key generation, telemetry, etc.)."""

    def now_unix(self) -> float: ...


# ---- canonical verb outcome ----

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
