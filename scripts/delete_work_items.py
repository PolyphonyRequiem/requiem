#!/usr/bin/env python3
"""Soft-delete a list of ADO work items by id."""

from __future__ import annotations

import asyncio
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from requiem.clients.auth import AdoTokenProvider

ORG = "https://dev.azure.com/microsoft"
PROJECT = "CloudVault"
API_VERSION = "7.1"


def main() -> int:
    ids = [int(a) for a in sys.argv[1:]]
    if not ids:
        print("usage: delete_work_items.py <id> [<id> ...]", file=sys.stderr)
        return 2

    provider = AdoTokenProvider()
    token = asyncio.run(provider.get_access_token())
    headers = {"Authorization": f"Bearer {token.token}"}

    for wid in ids:
        url = f"{ORG}/{PROJECT}/_apis/wit/workitems/{wid}?api-version={API_VERSION}"
        req = urllib.request.Request(url, method="DELETE", headers=headers)
        with urllib.request.urlopen(req) as resp:
            print(f"{wid}: {resp.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
