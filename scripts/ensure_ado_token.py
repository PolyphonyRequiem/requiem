#!/usr/bin/env python3
"""Ensure Requiem can mint an Azure DevOps access token.

This script uses the same auth chain as Requiem's live ADO workflows:
1. cached in-memory / file cache
2. refresh-token bootstrap from twig / MSAL cache
3. Azure CLI credential fallback

Pass --force to clear the cached access token first and force a fresh mint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from requiem.clients.auth import AdoTokenProvider
from requiem.clients.auth.ado_token_provider import AdoAuthError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="invalidate the cached access token before requesting a fresh one",
    )
    args = parser.parse_args()

    provider = AdoTokenProvider()
    if args.force:
        provider.invalidate()

    try:
        token = asyncio.run(provider.get_access_token())
    except AdoAuthError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "token_preview": f"{token.token[:12]}...",
                "expires_on": token.expires_on,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
