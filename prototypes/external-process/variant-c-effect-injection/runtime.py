"""Runtime — inspects verb signatures, injects effect implementations by name.

This is the *whole* DI/effect layer. ~40 lines. The trick: a verb's parameter
name (`git`, `gh`, `clock`) is the binding key. The runtime maintains a
registry of capability-name → implementation and supplies whatever the
function asks for. Unknown capabilities raise at dispatch time, not at runtime.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from effects import VerbOutcome


class Runtime:
    def __init__(self, **effects: Any) -> None:
        # capability name → implementation; e.g. git=RealGit(), gh=RealGh()
        self._effects = effects

    def with_(self, **overrides: Any) -> "Runtime":
        """Return a child runtime with extra/overridden effects (e.g., for tests)."""
        merged = {**self._effects, **overrides}
        return Runtime(**merged)

    def dispatch(self, verb: Callable[..., VerbOutcome], **kwargs: Any) -> VerbOutcome:
        sig = inspect.signature(verb)
        bound: dict[str, Any] = {}
        for name, param in sig.parameters.items():
            if name in kwargs:
                bound[name] = kwargs[name]
            elif name in self._effects:
                bound[name] = self._effects[name]
            elif param.default is inspect.Parameter.empty:
                raise TypeError(
                    f"dispatch({verb.__name__}): no value for parameter {name!r}; "
                    f"either supply via kwargs or register an effect of that name"
                )
        return verb(**bound)
