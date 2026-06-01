"""Per-tool typed clients -- Liszt B+C hybrid extension.

The Toolbelt (`requiem.toolbelt`) shipped two prototype clients (`files`,
`git`) inline. Real external-tool clients live here so each tool keeps
its exit-code translation, JSON shape, and binary-version coupling in
one module, away from the Toolbelt assembly.

Verbs receive these clients via the Toolbelt and pattern-match on the
typed errors / dataclasses they raise / return. Verbs convert those to
`Outcome` variants -- clients themselves never construct outcomes.
"""
from requiem.clients.twig import (
    TwigClient,
    TwigClientError,
    TwigItem,
    TwigItemNotFoundError,
    TwigRateLimitedError,
    TwigUnknownError,
    is_twig_on_path,
)

__all__ = [
    "TwigClient",
    "TwigClientError",
    "TwigItem",
    "TwigItemNotFoundError",
    "TwigRateLimitedError",
    "TwigUnknownError",
    "is_twig_on_path",
]
