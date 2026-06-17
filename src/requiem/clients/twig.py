"""TwigClient — wraps the `twig` ADO work-item CLI.

Architecture: Liszt B+C hybrid, per ADR 0002. Per-tool typed client; verbs
receive it via the Toolbelt and translate its typed errors into the
discriminated `Outcome` union. The client itself returns plain dataclasses
on success and raises a typed `TwigClientError` hierarchy on failure.

## Exit-code → error mapping (Ravel's L-1 caveat applies)

We invoke `twig` as a subprocess and classify its termination as follows.
**The cardinal rule:** unknown `twig exit 1` is `TwigUnknownError`, which
verbs MUST convert to `NeedsHuman` — never to `RetryableFailure`.
Auto-retrying an unclassified failure is an INV-NO-CORRUPT-FORWARD
violation; a human decides.

| exit | stderr signal              | raises                      | verb maps to                                  |
|------|----------------------------|-----------------------------|-----------------------------------------------|
| 0    | —                          | (returns value)             | `Success`                                     |
| 1    | "rate limit" / "throttled" | `TwigRateLimitedError`      | `RetryableFailure(error_kind="rate_limited")` |
| 1    | "not found" / "TF40001"    | `TwigItemNotFoundError`     | `PermanentFailure(error_kind="not_found")`    |
| 1    | (anything else)            | `TwigUnknownError`          | `NeedsHuman` — DO NOT auto-retry              |
| >= 2 | (any)                      | `TwigUnknownError`          | `NeedsHuman` — DO NOT auto-retry              |

If you find yourself wanting to add a new auto-retry pattern, write an
ADR. The default is escalation to a human; widening it is a deliberate
decision, not a drive-by patch.

## Subprocess seam

We use `asyncio.create_subprocess_exec` with no shell. The `cwd` is taken
from `TwigClient(cwd=...)` and passed straight through; bad `cwd` raises
`NotADirectoryError` on Windows and `FileNotFoundError` on POSIX -- both
are caught at the runner and re-raised as `TwigClientError("invalid
cwd...")`.

stdin is explicitly set to `DEVNULL`. Per Schumann's filesystem-seat
debrief: on Python 3.14 + Windows + pytest, an inherited (captured)
stdin handle is not inheritable and the spawn fails opaquely. DEVNULL
is the cheap belt-and-brace; twig never reads stdin anyway.

stdout/stderr are read as bytes and decoded UTF-8 with `errors="replace"`
so a stray non-UTF-8 byte never crashes the classifier.

## Out of scope (v0)

No caching, no offline mode, no concurrent backoff. Each call is a real
subprocess. Backoff is the engine's job; this client is the seam.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any


# ---- public dataclass --------------------------------------------------


@dataclass(frozen=True, slots=True)
class TwigItem:
    """Subset of twig's JSON shape that close-out verbs care about.

    `raw` preserves the full twig payload so callers that need more
    fields don't have to re-shell. We promote only the fields with stable
    names across twig versions; everything else stays in `raw`.
    """

    id: int
    title: str
    state: str
    area_path: str
    work_item_type: str
    parent_id: int | None
    raw: dict[str, Any] = field(default_factory=dict)


# ---- error hierarchy ---------------------------------------------------


class TwigClientError(Exception):
    """Base class. Verbs match on the concrete subclass to pick an outcome."""


class TwigRateLimitedError(TwigClientError):
    """ADO/twig reported a rate-limit / throttle. Verb -> `RetryableFailure`."""

    def __init__(self, message: str, retry_after: timedelta | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TwigItemNotFoundError(TwigClientError):
    """Work item does not exist (or is not in scope). Verb -> `PermanentFailure`."""


class TwigUnknownError(TwigClientError):
    """Anything we couldn't classify. Verb -> `NeedsHuman` (Ravel's L-1 caveat).

    Carries `exit_code` and `stderr` so the human gate has the receipts
    it needs to make a call.
    """

    def __init__(self, message: str, *, exit_code: int, stderr: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


# ---- classification ----------------------------------------------------


_RATE_LIMIT_PAT = re.compile(r"rate[\s-]?limit|throttl", re.IGNORECASE)
_NOT_FOUND_PAT = re.compile(r"not\s+found|TF40001", re.IGNORECASE)
# accept "retry after 30", "retry-after: 30s", "in 30 seconds"
_RETRY_AFTER_PAT = re.compile(
    r"(?:retry[\s-]?after[:\s]+|in\s+)(\d+)\s*(?:s|sec|seconds?)?", re.IGNORECASE
)


def _parse_retry_after(stderr: str) -> timedelta | None:
    m = _RETRY_AFTER_PAT.search(stderr)
    if m is None:
        return None
    return timedelta(seconds=int(m.group(1)))


def _classify_failure(exit_code: int, stderr: str) -> TwigClientError:
    """Map a non-zero `twig` exit to the typed error hierarchy.

    Pure function: takes the raw signals, returns the exception to raise.
    Lives apart from the runner so the table above is auditable from one
    site without subprocess noise.
    """
    if exit_code == 1:
        if _RATE_LIMIT_PAT.search(stderr):
            return TwigRateLimitedError(
                stderr.strip() or "twig reported rate limit",
                retry_after=_parse_retry_after(stderr),
            )
        if _NOT_FOUND_PAT.search(stderr):
            return TwigItemNotFoundError(stderr.strip() or "twig item not found")
    return TwigUnknownError(
        stderr.strip() or f"twig exited {exit_code} with no stderr",
        exit_code=exit_code,
        stderr=stderr,
    )


# ---- JSON -> dataclass -------------------------------------------------


def _coerce_item(payload: dict[str, Any]) -> TwigItem:
    """Lift twig's `show --output json` payload into `TwigItem`.

    Defensive about field presence: twig has tightened/loosened these
    keys across versions. Missing optional fields fall back to safe
    defaults; missing required fields raise `TwigUnknownError` so a
    schema drift fails loud rather than producing a half-populated item.
    """
    try:
        return TwigItem(
            id=int(payload["id"]),
            title=str(payload.get("title", "")),
            state=str(payload.get("state", "")),
            area_path=str(payload.get("areaPath", "")),
            work_item_type=str(payload.get("type", "")),
            parent_id=(
                int(payload["parentId"]) if payload.get("parentId") is not None else None
            ),
            raw=payload,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise TwigUnknownError(
            f"twig JSON missing/invalid required field: {e!r}",
            exit_code=0,
            stderr=json.dumps(payload)[:500],
        ) from e


# ---- client ------------------------------------------------------------


class TwigClient:
    """Read-mostly wrapper over the `twig` CLI.

    `cwd` is the working directory the subprocess runs in. twig is
    workspace-scoped (it reads `.twig/` from cwd-ancestry) so callers
    typically pass a repo root where `twig init` has been run.

    `executable` is the binary to invoke; defaults to `"twig"` (resolved
    via PATH). Override for tests or hermetic installs.

    `timeout_s` caps each call so a hung subprocess doesn't wedge the
    engine -- twig itself has internal timeouts but we belt-and-brace.
    """

    def __init__(
        self,
        cwd: Path | None = None,
        *,
        executable: str = "twig",
        timeout_s: float = 30.0,
    ) -> None:
        self._cwd = cwd
        self._executable = executable
        self._timeout_s = timeout_s

    # -- async surface (use these from async verbs) ----------------------

    async def show_async(self, item_id: int) -> TwigItem:
        stdout, _ = await self._run(["show", str(item_id), "--output", "json"])
        return _coerce_item(_parse_json(stdout))

    async def set_state_async(self, item_id: int, new_state: str) -> TwigItem:
        # twig CLI shape: `twig state <name> --id <id>`.
        # The brief sketched `twig state set <id> <state>`; that's the
        # conceptual verb. The literal binary uses positional state + `--id`.
        stdout, _ = await self._run(
            ["state", new_state, "--id", str(item_id), "--output", "json"]
        )
        payload = _parse_json(stdout)
        # `state` may return a payload that isn't a full work-item dump on
        # every twig version. Re-fetch via `show` if the shape looks thin so
        # callers always get a complete `TwigItem` -- one extra subprocess
        # is cheap and keeps the contract honest.
        if {"id", "state", "type"}.issubset(payload):
            return _coerce_item(payload)
        return await self.show_async(item_id)

    async def list_children_async(self, parent_id: int) -> list[TwigItem]:
        parent = await self.show_async(parent_id)
        child_stubs = parent.raw.get("children") or []
        child_ids = [int(c["id"]) for c in child_stubs if "id" in c]
        if not child_ids:
            return []
        # Parallel fetches -- twig hits its local cache for these so the
        # subprocess overhead dominates, and asyncio.gather amortises it.
        return list(await asyncio.gather(*(self.show_async(cid) for cid in child_ids)))

    async def comment_async(self, item_id: int, message: str) -> None:
        """Post ``message`` as a discussion comment on ``item_id``.

        Added for Bizet — the implementation workflow uses this to link
        the freshly-opened PR URL back to the leaf work item so the human
        reviewer can navigate between the two surfaces. We deliberately
        do not parse the output: twig prints a confirmation line on
        success and we trust exit 0. Failure paths flow through the
        normal ``_classify_failure`` table.
        """
        await self._run(["comment", "--id", str(item_id), "--message", message])

    async def create_child_async(
        self,
        *,
        parent_id: int,
        title: str,
        work_item_type: str,
        area_path: str | None = None,
        description: str | None = None,
    ) -> TwigItem:
        """Create a child work item under ``parent_id`` and return it.

        Added for Wave 6 (Mahler-3 parity audit §2.4) to unblock
        recursive plan child seeding into ADO. The recursive ``planning``
        workflow currently spawns child sub-workflows over synthesised
        item ids; once a topology design lands (Stravinsky's ADR-0006)
        the seeding step calls this method instead.

        ## Inferred CLI contract

        The local twig binary (0.81+) exposes child creation via:

            twig new --parent <id> --title <str> --type <str>
                     [--area <str>]
                     [--description <str>]
                     -o json

        Note the flag rename history (caused dogfood run 8 failure,
        2026-06-17): older twig builds used `create-child` /
        `--work-item-type` / `--area-path` / `--output json`. Current
        twig uses `new` / `--type` / `--area` / `-o json`. We invoke
        the current names here.

        On exit 0, stdout is JSON with at least an ``id`` field; we lift
        it via the same ``_coerce_item`` path as ``show_async``. The
        binary may emit additional fields (``title``, ``state``, etc.)
        — if any are missing we fall back to a follow-up
        ``show_async(new_id)`` so callers always get a complete
        ``TwigItem``. This mirrors ``set_state_async``'s thin-payload
        guard.

        Failure paths route through the same ``_classify_failure`` table
        as every other call — rate limit → ``TwigRateLimitedError``,
        ``parent not found`` → ``TwigItemNotFoundError``, anything else
        → ``TwigUnknownError`` (Ravel's L-1 caveat). Verbs are expected
        to convert ``TwigUnknownError`` to ``NeedsHuman`` rather than
        auto-retry; auto-retrying an unclassified failure would violate
        ``INV-NO-CORRUPT-FORWARD``.
        """
        argv = [
            "new",
            "--parent", str(parent_id),
            "--title", title,
            "--type", work_item_type,
        ]
        if area_path is not None:
            argv.extend(["--area", area_path])
        if description is not None:
            argv.extend(["--description", description])
        argv.extend(["-o", "json"])

        stdout, _ = await self._run(argv)
        payload = _parse_json(stdout)
        if {"id", "state", "type"}.issubset(payload):
            return _coerce_item(payload)
        # Thin payload — twig acknowledged the create but didn't echo
        # the full item. Re-fetch via `show` so callers always get a
        # complete TwigItem (same belt-and-brace as set_state_async).
        try:
            new_id = int(payload["id"])
        except (KeyError, TypeError, ValueError) as e:
            raise TwigUnknownError(
                f"twig create-child JSON missing/invalid 'id': {e!r}",
                exit_code=0,
                stderr=json.dumps(payload)[:500],
            ) from e
        return await self.show_async(new_id)

    # -- sync surface (sugar for the common case) ------------------------

    def show(self, item_id: int) -> TwigItem:
        return asyncio.run(self.show_async(item_id))

    def set_state(self, item_id: int, new_state: str) -> TwigItem:
        return asyncio.run(self.set_state_async(item_id, new_state))

    def list_children(self, parent_id: int) -> list[TwigItem]:
        return asyncio.run(self.list_children_async(parent_id))

    def comment(self, item_id: int, message: str) -> None:
        return asyncio.run(self.comment_async(item_id, message))

    def create_child(
        self,
        *,
        parent_id: int,
        title: str,
        work_item_type: str,
        area_path: str | None = None,
        description: str | None = None,
    ) -> TwigItem:
        return asyncio.run(
            self.create_child_async(
                parent_id=parent_id,
                title=title,
                work_item_type=work_item_type,
                area_path=area_path,
                description=description,
            )
        )

    # -- runner ----------------------------------------------------------

    async def _run(self, argv: list[str]) -> tuple[str, str]:
        """Invoke twig, classify failure, return (stdout, stderr) on success.

        All failures are raised as `TwigClientError` subclasses. Callers
        never see a non-zero exit code directly.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self._executable,
                *argv,
                cwd=str(self._cwd) if self._cwd is not None else None,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, NotADirectoryError) as e:
            # FileNotFoundError on POSIX (or missing binary);
            # NotADirectoryError on Windows when cwd is a file rather than
            # a directory. Both flow to the same diagnosis.
            raise TwigClientError(
                f"invalid cwd or executable: cwd={self._cwd!r} "
                f"exe={self._executable!r}: {e}"
            ) from e

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout_s
            )
        except asyncio.TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise TwigUnknownError(
                f"twig timed out after {self._timeout_s}s",
                exit_code=-1,
                stderr="",
            ) from e

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        rc = proc.returncode if proc.returncode is not None else -1

        if rc == 0:
            return stdout, stderr
        raise _classify_failure(rc, stderr)


def _parse_json(stdout: str) -> dict[str, Any]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise TwigUnknownError(
            f"twig stdout was not valid JSON: {e}",
            exit_code=0,
            stderr=stdout[:500],
        ) from e
    if not isinstance(data, dict):
        raise TwigUnknownError(
            f"twig JSON root was {type(data).__name__}, expected object",
            exit_code=0,
            stderr=stdout[:500],
        )
    return data


def is_twig_on_path(executable: str = "twig") -> bool:
    """Smoke-test helper for tests / `Toolbelt.real()` preflight."""
    return shutil.which(executable) is not None
