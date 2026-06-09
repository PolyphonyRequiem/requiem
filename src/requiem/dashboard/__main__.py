"""``python -m requiem.dashboard`` — launch the read-only web dashboard.

Also wired as the ``requiem-dashboard`` console script (see pyproject).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from requiem.dashboard.server import serve


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="requiem-dashboard",
        description="Read-only web dashboard over requiem run event logs (ADR-0019). "
                    "Pure projection of *.events.jsonl; mutates nothing.",
    )
    p.add_argument("--log-dir", type=Path, default=Path(".runs"),
                   help="Directory of *.events.jsonl run logs (default: .runs).")
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind host (default: 127.0.0.1 — operator-local).")
    p.add_argument("--port", type=int, default=8770,
                   help="Bind port (default: 8770).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.log_dir.exists():
        print(f"note: log-dir {args.log_dir} does not exist yet — "
              "the dashboard will show no runs until one appears.")
    serve(args.log_dir, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
