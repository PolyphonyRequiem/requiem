"""Git-aware filesystem client — Schumann (Phase B seat 3/8).

Wraps the small set of filesystem + `git` operations the close-out
workflow needs, plus a few obvious-extension points the Phase C verbs
will reach for. Two design notes worth surfacing:

* **Atomic writes** use temp-file + `os.replace` in the destination
  directory. `os.replace` is atomic on both POSIX and Windows *as long
  as source and destination live on the same volume*. We always write
  the temp file alongside the destination, so this holds by
  construction, but we still assert the same-volume invariant before
  the replace and raise `FsCrossVolumeError` if it is ever violated
  (e.g. if a future caller threads a separate temp dir through).

* **`git` subprocesses** use `asyncio.create_subprocess_exec` (the
  shape Mendelssohn-twig and Chopin-gh are also adopting for Phase B),
  always with `cwd=self.repo_root` — *not* the process PWD. The repo
  root is the worktree the client was bound to; the calling code's
  current directory is irrelevant. Workflow nodes that need to write
  outside the repo (e.g. log files in `~/.requiem`) should be doing
  pure filesystem ops, not git ops, and so will sidestep this
  entirely.

The client raises from its own typed hierarchy (`FsClientError` and
subclasses). Verb authors convert those to outcomes per
`src/requiem/outcomes.py`; the mapping suggested in the Phase B fleet
brief is:

    FsNotFoundError       -> PermanentFailure(error_kind="not_found")
    FsPermissionError     -> NeedsHuman
    FsCrossVolumeError    -> PermanentFailure(error_kind="cross_volume")
    FsGitError(stderr=..) -> NeedsHuman  (Ravel L-1: unknown git failure)
    FsClientError         -> NeedsHuman  (catchall)

INV-NO-CORRUPT-FORWARD: this client never silently retries, never
swallows a partial write, never papers over an unexpected git exit
code. It surfaces; the verb decides.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


# ---- errors ----------------------------------------------------------


class FsClientError(Exception):
    """Base class — verbs map this to ``NeedsHuman`` by default."""


class FsNotFoundError(FsClientError):
    """A required file or directory does not exist."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"path not found: {path}")
        self.path = path


class FsPermissionError(FsClientError):
    """OS refused the operation (EACCES, EPERM, locked file, etc.)."""

    def __init__(self, path: Path, cause: BaseException | None = None) -> None:
        super().__init__(f"permission denied: {path}")
        self.path = path
        self.cause = cause


class FsCrossVolumeError(FsClientError):
    """Atomic-write contract violated: temp and dst on different volumes."""

    def __init__(self, src: Path, dst: Path) -> None:
        super().__init__(
            f"cross-volume rename refused: {src} -> {dst} "
            "(atomic rename requires same volume)"
        )
        self.src = src
        self.dst = dst


class FsGitError(FsClientError):
    """`git` exited non-zero. Stderr preserved verbatim for the human gate."""

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"git {' '.join(argv)!r} exited {returncode}: {stderr.strip()}"
        )
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr


class FsNotAGitRepoError(FsClientError):
    """The configured repo_root is not a git working tree."""

    def __init__(self, repo_root: Path) -> None:
        super().__init__(f"not a git repository: {repo_root}")
        self.repo_root = repo_root


# ---- client ----------------------------------------------------------


class FilesystemClient:
    """Git-aware filesystem operations with atomic writes.

    Bind to a worktree once at construction; every git op runs with
    ``cwd=self.repo_root`` regardless of the process PWD.

    Parameters
    ----------
    repo_root:
        The worktree the client should operate within. Defaults to the
        current working directory. The directory must exist; it need
        not be a git repo (file ops still work; git ops will raise
        ``FsNotAGitRepoError``).
    """

    def __init__(self, repo_root: Path | None = None) -> None:
        root = Path(repo_root) if repo_root is not None else Path.cwd()
        if not root.exists():
            raise FsNotFoundError(root)
        self.repo_root = root.resolve()

    # ---- atomic writes ----------------------------------------------

    def write_text(self, path: Path, content: str) -> None:
        """Atomically write ``content`` (UTF-8) to ``path``."""
        self.write_bytes(path, content.encode("utf-8"))

    def write_bytes(self, path: Path, content: bytes) -> None:
        """Atomically write ``content`` to ``path``.

        Writes to a sibling temp file in the destination's parent
        directory, ``fsync``s, then ``os.replace``s into place. The
        sibling-temp-file approach guarantees same-volume rename on
        both POSIX and Windows.

        Symlinks are out of scope for v0 — see brief. A symlink dst
        raises ``NotImplementedError``.
        """
        path = Path(path)
        if path.is_symlink():
            raise NotImplementedError(
                f"symlink writes not supported in v0: {path}"
            )
        parent = path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            raise FsPermissionError(parent, e) from e

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=parent
        )
        tmp = Path(tmp_name)
        try:
            self._assert_same_volume(tmp, path)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
            except PermissionError as e:
                raise FsPermissionError(tmp, e) from e
            try:
                os.replace(tmp, path)
            except PermissionError as e:
                raise FsPermissionError(path, e) from e
        except BaseException:
            # Best-effort cleanup; if the replace already happened the
            # temp is gone and unlink will raise FileNotFoundError,
            # which is fine to swallow here.
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise

    # ---- reads ------------------------------------------------------

    def read_text(self, path: Path) -> str:
        path = Path(path)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as e:
            raise FsNotFoundError(path) from e
        except PermissionError as e:
            raise FsPermissionError(path, e) from e

    def exists(self, path: Path) -> bool:
        return Path(path).exists()

    # ---- git ops ----------------------------------------------------

    async def git_mv(self, src: Path, dst: Path) -> None:
        """Move ``src`` to ``dst``, preferring ``git mv``.

        Falls back to ``shutil.move`` + a warning log if the bound
        worktree is not a git repository. The fallback intentionally
        does *not* try to be clever about staging — if the caller
        wants the move tracked, they should be in a git tree.
        """
        src = Path(src)
        dst = Path(dst)
        if not src.exists():
            raise FsNotFoundError(src)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            raise FsPermissionError(dst.parent, e) from e

        if not self._is_git_tree():
            log.warning(
                "git_mv: %s is not a git repo; falling back to shutil.move",
                self.repo_root,
            )
            try:
                shutil.move(str(src), str(dst))
            except PermissionError as e:
                raise FsPermissionError(dst, e) from e
            return

        await self._git("mv", str(src), str(dst))

    async def git_status_porcelain(self) -> list[str]:
        """Return the non-empty lines of ``git status --porcelain``."""
        out = await self._git("status", "--porcelain")
        return [line for line in out.splitlines() if line]

    async def git_current_branch(self) -> str:
        """Return the current branch name (``git rev-parse --abbrev-ref HEAD``).

        On a detached HEAD this returns the literal ``"HEAD"`` — same
        as git itself. Callers that care should compare and surface
        their own gate.
        """
        out = await self._git("rev-parse", "--abbrev-ref", "HEAD")
        return out.strip()

    async def git_is_clean(self) -> bool:
        """True iff ``git status --porcelain`` is empty."""
        return not await self.git_status_porcelain()

    async def git_commit(
        self, message: str, paths: list[Path] | None = None
    ) -> str:
        """Stage ``paths`` (if given) and commit. Returns the new HEAD SHA.

        If ``paths`` is None, commits whatever is currently staged.
        Raises ``FsGitError`` if there is nothing to commit — close-out
        is expected to check ``git_is_clean()`` first.
        """
        if paths:
            await self._git("add", "--", *[str(p) for p in paths])
        await self._git("commit", "-m", message)
        sha = await self._git("rev-parse", "HEAD")
        return sha.strip()

    # ---- internals --------------------------------------------------

    def _is_git_tree(self) -> bool:
        return (self.repo_root / ".git").exists()

    @staticmethod
    def _assert_same_volume(src: Path, dst: Path) -> None:
        """Raise FsCrossVolumeError if src and dst are on different volumes.

        On Windows this compares drive letters via ``os.path.splitdrive``;
        on POSIX both drives are empty strings and the check is a no-op.
        The same-volume invariant matters because ``os.replace`` is only
        atomic within a single filesystem.
        """
        src_drive = os.path.splitdrive(os.path.abspath(src))[0].lower()
        dst_drive = os.path.splitdrive(os.path.abspath(dst))[0].lower()
        if src_drive != dst_drive:
            raise FsCrossVolumeError(src, dst)

    async def _git(self, *args: str) -> str:
        """Run ``git *args`` in ``self.repo_root``; return stdout (UTF-8).

        Raises ``FsNotAGitRepoError`` if the bound worktree has no
        ``.git`` directory, and ``FsGitError`` for any non-zero exit.
        """
        if not self._is_git_tree():
            raise FsNotAGitRepoError(self.repo_root)
        argv = ["git", *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.repo_root),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise FsGitError(argv, -1, "git binary not found") from e
        stdout_b, stderr_b = await proc.communicate()
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise FsGitError(argv, proc.returncode or -1, stderr)
        return stdout
