#!/usr/bin/env python3
"""Abandon a list of ADO pull requests by id in cloudvault-service-api."""

from __future__ import annotations

import asyncio
import json
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
REPO = "cloudvault-service-api"
API_VERSION = "7.1"


def main() -> int:
    ids = [int(a) for a in sys.argv[1:]]
    if not ids:
        print("usage: abandon_prs.py <pr_id> [<pr_id> ...]", file=sys.stderr)
        return 2

    provider = AdoTokenProvider()
    token = asyncio.run(provider.get_access_token())

    for pr_id in ids:
        url = (
            f"{ORG}/{PROJECT}/_apis/git/repositories/{REPO}/pullrequests/{pr_id}"
            f"?api-version={API_VERSION}"
        )
        body = json.dumps({"status": "abandoned"}).encode()
        req = urllib.request.Request(
            url,
            data=body,
            method="PATCH",
            headers={
                "Authorization": f"Bearer {token.token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
        print(f"{pr_id}: status={data.get('status')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
