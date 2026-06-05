"""Doctrine — the repo-resident house-style artifact (ADR-0016 / ADR-0017).

Where ``process.yaml`` carries *policy* (type routing, role→profile mapping),
the doctrine carries *house-style*: the conventions a returning contributor
"just knows" — how tests are run, branch/commit rules, directory layout, the
load-bearing don'ts. It is free-form markdown at ``.requiem-config/doctrine.md``
so it reviews like any other repo document (ADR-0016: *learning is legitimate
only when it is legible*).

Two deliberate contrasts with :mod:`requiem.process_config`:

* **Absent is normal, not a fallback wart.** A repo with no doctrine simply has
  none; :func:`default_doctrine` is the empty doctrine. (A *present* file that
  cannot be read still fails loud — INV-NO-CORRUPT-FORWARD: we never proceed on
  a half-read artifact we were told exists.)
* **There is no "malformed" doctrine.** Any text is valid house-style; the
  loader's whole job is read → hash → make snapshottable. Structure (sections,
  rules) is deferred until a consumer needs it.

The effective doctrine's ``sha256`` is snapshotted into the run's event log so
the fleet that was hydrated from it is auditable and a resume re-reads the
durable identity rather than ambient disk state (same durability pattern as the
process-config snapshot, ADR-0015).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from requiem.process_config import CONFIG_DIRNAME

DOCTRINE_FILENAME = "doctrine.md"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DoctrineError(Exception):
    """Raised when a doctrine file is present but cannot be read.

    A *missing* doctrine is never an error (callers get :func:`default_doctrine`);
    only a present-but-unreadable file fails loud.
    """

    def __init__(self, message: str, *, path: Path | None = None) -> None:
        self.path = path
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Doctrine:
    """The house-style text a run hydrates its fleet from, plus provenance.

    ``text`` is the raw markdown (empty when no doctrine exists). ``sha256`` is
    always the digest of ``text`` — deterministic and order-free — so two runs
    over identical doctrine record an identical identity regardless of where the
    file lived.
    """

    text: str = ""
    source: Path | None = None
    sha256: str = _sha256("")

    @property
    def is_empty(self) -> bool:
        """True when there is no house-style content (whitespace-only counts)."""
        return self.text.strip() == ""

    def to_snapshot(self) -> dict[str, Any]:
        """A JSON-safe, order-stable snapshot for the event log / manifest.

        The full ``text`` is included so a resume reconstructs the exact
        doctrine the run was hydrated from without re-reading disk.
        """
        return {
            "text": self.text,
            "source": str(self.source) if self.source is not None else None,
            "sha256": self.sha256,
        }

    @classmethod
    def from_snapshot(cls, snap: Mapping[str, Any]) -> "Doctrine":
        """Reconstruct a doctrine from a :meth:`to_snapshot` payload.

        ``sha256`` is recomputed from ``text`` rather than trusted from the
        payload, so a tampered or truncated snapshot cannot misrepresent the
        identity of the content it carries.
        """
        text = snap.get("text") or ""
        src = snap.get("source")
        return cls(text=text, source=Path(src) if src else None, sha256=_sha256(text))


def default_doctrine() -> Doctrine:
    """The empty doctrine used when no ``doctrine.md`` exists."""
    return Doctrine()


def load_doctrine(path: Path | str) -> Doctrine:
    """Load a doctrine from an explicit path.

    Raises :class:`DoctrineError` only if the file cannot be read. Any content
    that reads is valid.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise DoctrineError(f"cannot read doctrine {p}: {exc}", path=p) from exc
    return Doctrine(text=text, source=p, sha256=_sha256(text))


def find_doctrine_path(start_dir: Path | str) -> Path | None:
    """Walk up from ``start_dir`` for a ``.requiem-config/doctrine.md`` file."""
    start = Path(start_dir).resolve()
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_DIRNAME / DOCTRINE_FILENAME
        if candidate.is_file():
            return candidate
    return None


def discover_doctrine(start_dir: Path | str, *, default: bool = True) -> Doctrine | None:
    """Discover the nearest ``doctrine.md`` at or above ``start_dir``.

    Returns the loaded doctrine, or :func:`default_doctrine` when none is found
    and ``default`` is True, else ``None``.
    """
    found = find_doctrine_path(start_dir)
    if found is not None:
        return load_doctrine(found)
    return default_doctrine() if default else None


def resolve_doctrine(explicit: Doctrine | None, repo_path: Path | str) -> Doctrine:
    """Resolve the effective doctrine: explicit > discovered > empty default.

    Discovery is anchored to ``repo_path`` (not ambient cwd) so a run never
    silently hydrates from an unrelated repo's house-style.
    """
    if explicit is not None:
        return explicit
    return discover_doctrine(repo_path, default=True) or default_doctrine()
