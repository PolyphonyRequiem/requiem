from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LeafReviewComment(BaseModel):
    file: str
    line: int | None = None
    body: str
    severity: Literal["blocker", "major", "minor", "nit"]


class LeafReviewReport(BaseModel):
    verdict: Literal["approve", "request_changes", "needs_human"]
    comments: list[LeafReviewComment] = Field(default_factory=list)
    summary: str
