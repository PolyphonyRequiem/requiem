"""Requiem CLI package.

The `requiem` console script (declared in pyproject.toml) is
`requiem.cli:main`, so this `__init__` re-exports `main` from
`requiem.cli.main`. Subdivisions:

* `requiem.cli.main`   — argparse plumbing + operational subcommands.
* `requiem.cli.render` — customer-facing renderer registry (Debussy's
                         Demo Contract §3–§4): one renderer per event
                         kind in `requiem.events.EVENT_KINDS`.
"""
from requiem.cli.main import main

__all__ = ["main"]
