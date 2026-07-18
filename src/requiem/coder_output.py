"""Shared coder-agent file-change contract + apply helper.

Every coder-role agent (the implementation workflow's ``coder`` /
``coder_revision``, and the PR/leaf-lifecycle workflows'
``comment_addresser``) reports edits the same way: a list of
``FileChange`` entries. *Our* code applies them to disk — never the
agent's own tools. ``CopilotProvider`` deliberately never grants an
agent shell or write-capable builtins (see
``providers/copilot.py``'s ``_BLOCKED_BUILTIN_TOOLS``), so an agent
spec that expects the model to run ``git commit`` itself can never
succeed against the real provider. Centralising the model and the
apply loop here keeps that contract — and its path-safety checks — in
exactly one place instead of drifting across workflows.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from requiem.clients.fs import FilesystemClient, FsClientError
from requiem.outcomes import Outcome, PermanentFailure, Success


class FileChange(BaseModel):
    """One edit a coder-role agent asks us to apply."""

    path: str = Field(
        ...,
        description="Path relative to the repo root; no '..', no absolute paths.",
    )
    operation: Literal["create", "modify", "replace", "delete"]
    content: str | None = Field(
        None,
        description=(
            "Full file content for create/modify, replacement text for replace, "
            "or None for delete."
        ),
    )
    old_content: str | None = Field(
        None,
        description=(
            "Exact text to replace; required for replace and must occur exactly once."
        ),
    )


def validate_relative_path(raw: str) -> Path | None:
    """Reject absolute paths and '..' segments. Returns ``None`` if invalid."""
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute() or ".." in p.parts:
        return None
    return p


def apply_file_changes(
    repo_path: Path,
    fs: FilesystemClient,
    raw_changes: list[dict],
    *,
    no_changes_error_kind: str = "needs_human.no_changes",
    invalid_path_error_kind: str = "needs_human.invalid_path",
    apply_failed_error_kind: str = "needs_human.apply_failed",
) -> Outcome:
    """Apply a coder-role agent's reported ``file_changes`` to disk.

    Same path-safety checks as ``implementation.py``'s
    ``_apply_changes_impl``, parameterised so callers in other
    workflows can supply their own closed ``error_kind`` vocabulary.
    An empty ``raw_changes`` list is treated as a failure — the agent
    found nothing it could safely change — routing to the caller's
    ``no_changes_error_kind``.
    """
    if not raw_changes:
        return PermanentFailure(
            error_kind=no_changes_error_kind,
            message="agent reported 0 file_changes — nothing to apply",
        )
    applied: list[str] = []
    resolved_root = repo_path.resolve()
    for entry in raw_changes:
        rel = validate_relative_path(entry.get("path", ""))
        if rel is None:
            return PermanentFailure(
                error_kind=invalid_path_error_kind,
                message=(
                    f"refusing to apply path {entry.get('path')!r}: "
                    "must be a relative path without '..' segments"
                ),
                details={"path": entry.get("path")},
            )
        target = (repo_path / rel).resolve()
        try:
            # Belt-and-brace: the resolved path must still live inside
            # the repo. Catches cleverness like symlinks that point
            # outside the worktree.
            target.relative_to(resolved_root)
        except ValueError:
            return PermanentFailure(
                error_kind=invalid_path_error_kind,
                message=f"path escapes repo root: {entry.get('path')!r}",
                details={"path": entry.get("path")},
            )
        op = entry.get("operation")
        content = entry.get("content")
        try:
            if op == "delete":
                if target.exists():
                    target.unlink()
            elif op in ("create", "modify"):
                if content is None:
                    return PermanentFailure(
                        error_kind=apply_failed_error_kind,
                        message=f"operation {op!r} on {rel} requires content",
                    )
                fs.write_text(target, content)
            elif op == "replace":
                old_content = entry.get("old_content")
                if not old_content or content is None:
                    return PermanentFailure(
                        error_kind=apply_failed_error_kind,
                        message=(
                            f"operation 'replace' on {rel} requires non-empty "
                            "old_content and replacement content"
                        ),
                    )
                current = fs.read_text(target)
                matches = current.count(old_content)
                if matches != 1:
                    return PermanentFailure(
                        error_kind=apply_failed_error_kind,
                        message=(
                            f"operation 'replace' on {rel} expected exactly one "
                            f"old_content match; found {matches}"
                        ),
                        details={"path": str(rel), "matches": matches},
                    )
                fs.write_text(target, current.replace(old_content, content, 1))
            else:
                return PermanentFailure(
                    error_kind=apply_failed_error_kind,
                    message=f"unknown operation {op!r} on {rel}",
                )
        except FsClientError as e:
            return PermanentFailure(
                error_kind=apply_failed_error_kind,
                message=f"writing {rel}: {e}",
                details={"path": str(rel)},
            )
        applied.append(str(rel))
    return Success(
        value={"applied_paths": applied, "change_count": len(applied)},
        inspected_artifacts=tuple(f"file:{p}" for p in applied),
    )
