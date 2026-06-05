"""Cross-workflow fake-surface contract (parity §4.2).

Why this test exists
--------------------
The original Tchaikovsky-class regression (parity-readiness §4.2): a real
client method flipped sync → async (``twig.show`` → ``show_async``) but one
workflow's *local* ``FakeTwigClient`` was not updated in lockstep, so the
divergence only surfaced days later when a different workflow's suite ran.

The workflow test modules each still carry their own small ``Fake*`` clients
(intentionally — they implement only the slice of the surface a given verb
touches). What we must prevent is a fake drifting *out of shape* from the
real client for the methods it *does* implement. The cheapest durable guard
is async-ness parity: every fake method that shares a name with a real client
method must agree on whether it is a coroutine.

Implementation note: we parse the test tree with ``ast`` rather than importing
every test module — importing pulls in heavy fixtures (and the full import of
all suites is known to hang). The AST walk is side-effect-free and catches the
exact drift that bit us.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from requiem.clients.fs import FilesystemClient
from requiem.clients.gh import GhClient
from requiem.clients.twig import TwigClient

TESTS_ROOT = Path(__file__).resolve().parent

# Map a fake class to the real client it stands in for, by a lowercase
# substring of the class name. Order matters: check the more specific tokens
# first so e.g. ``FakeFilesystemClient`` does not match on ``twig``.
_CLIENT_BY_TOKEN: list[tuple[str, type]] = [
    ("twig", TwigClient),
    ("gh", GhClient),
    ("filesystem", FilesystemClient),
    ("file", FilesystemClient),
    ("fs", FilesystemClient),
]


def _real_surface(cls: type) -> dict[str, bool]:
    """Public method name -> is-coroutine, for a real client class."""
    return {
        name: inspect.iscoroutinefunction(member)
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def _match_real_client(class_name: str) -> type | None:
    lowered = class_name.lower()
    for token, client in _CLIENT_BY_TOKEN:
        if token in lowered:
            return client
    return None


def _iter_fake_methods():
    """Yield (file, class_name, method_name, is_async) for every ``Fake*``
    class method across the test tree, via AST (no imports)."""
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not node.name.startswith("Fake"):
                continue
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("_"):
                        continue
                    yield (
                        path,
                        node.name,
                        item.name,
                        isinstance(item, ast.AsyncFunctionDef),
                    )


def test_fake_clients_match_real_async_shape():
    surfaces = {
        TwigClient: _real_surface(TwigClient),
        GhClient: _real_surface(GhClient),
        FilesystemClient: _real_surface(FilesystemClient),
    }

    checked = 0
    mismatches: list[str] = []
    for path, class_name, method, is_async in _iter_fake_methods():
        client = _match_real_client(class_name)
        if client is None:
            continue
        real = surfaces[client]
        if method not in real:
            continue
        checked += 1
        if real[method] != is_async:
            kind = "async" if is_async else "sync"
            want = "async" if real[method] else "sync"
            mismatches.append(
                f"{path.name}::{class_name}.{method} is {kind} but "
                f"{client.__name__}.{method} is {want}"
            )

    assert not mismatches, "fake/real client surface drift:\n" + "\n".join(mismatches)
    # Guard against the discovery silently finding nothing (which would make
    # the assertion vacuously pass and let a real regression slip through).
    assert checked > 0, "no overlapping fake/real client methods were discovered"
