"""ProcessOutcome — the discriminated union returned by every ProcessRunner.

Layer 1 of the two-layer model:
  Layer 1 (here): "what happened to the process" — completed, timed out, never started
  Layer 2 (verb): "what does it mean in domain terms" — stratify exit codes into VerbOutcome

Ravel's L-1 caveat (error-deep-dive-ravel-review.md §Liszt-2): exit codes are
tool-specific. The runner MUST NOT pre-classify exit 1 as transient. It returns
the raw exit code; the verb decides what it means.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class Success(BaseModel):
    kind: Literal["success"] = "success"
    stdout: str
    stderr: str
    duration_s: float


class NonZeroExit(BaseModel):
    kind: Literal["non_zero_exit"] = "non_zero_exit"
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


class Timeout(BaseModel):
    kind: Literal["timeout"] = "timeout"
    timeout_s: float
    partial_stdout: str
    partial_stderr: str


class NotFound(BaseModel):
    kind: Literal["not_found"] = "not_found"
    binary: str


ProcessOutcome = Annotated[
    Union[Success, NonZeroExit, Timeout, NotFound],
    Field(discriminator="kind"),
]
