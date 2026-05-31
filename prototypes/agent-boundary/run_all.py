"""Runs all 3 variant demos. From repo root:  python prototypes/agent-boundary/run_all.py"""

from __future__ import annotations

import asyncio
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).parent
DEMOS = [
    HERE / "variant-a-protocol-provider" / "demo.py",
    HERE / "variant-b-pydantic-ai" / "demo.py",
    HERE / "variant-c-litellm-direct" / "demo.py",
]


def main() -> int:
    failures: list[str] = []
    for demo in DEMOS:
        print(f"\n{'#' * 70}\n# {demo.parent.name}\n{'#' * 70}")
        r = subprocess.run([sys.executable, str(demo)])
        if r.returncode != 0:
            failures.append(demo.parent.name)
    if failures:
        print(f"\nFAILURES: {failures}")
        return 1
    print("\nAll 3 demos passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
