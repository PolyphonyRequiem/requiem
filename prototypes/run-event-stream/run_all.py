"""Run all three variants. Used by CI and the seam-review demo."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
VARIANTS = [
    "variant-a-typed-discriminated",
    "variant-b-envelope-loose",
    "variant-c-cloudevents",
]


def main() -> int:
    failures: list[str] = []
    for v in VARIANTS:
        path = HERE / v
        run_dir = path / "_run"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        print(f"\n########## {v} ##########", flush=True)
        result = subprocess.run(
            [sys.executable, "demo.py"],
            cwd=path,
            check=False,
        )
        if result.returncode != 0:
            failures.append(v)
    print("\n" + "=" * 60)
    if failures:
        print(f"FAIL: {', '.join(failures)}")
        return 1
    print("OK: all variants passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
