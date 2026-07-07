#!/usr/bin/env python3
"""List active ADO pull requests in cloudvault-service-api via REST API.

Uses Requiem's own AdoTokenProvider auth chain (az CLI extension commands
are unreliable in this environment; this in-process + direct-REST path is
the proven workaround, see docs/decisions/0028).
"""

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
    provider = AdoTokenProvider()
    token = asyncio.run(provider.get_access_token())

    url = (
        f"{ORG}/{PROJECT}/_apis/git/repositories/{REPO}/pullrequests"
        f"?searchCriteria.status=active&$top=200&api-version={API_VERSION}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token.token}"})
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)

    prs = data.get("value", [])
    print(f"active PR count: {len(prs)}")
    for pr in prs:
        print(
            f"{pr['pullRequestId']}\t{pr['sourceRefName']}\t->\t{pr['targetRefName']}\t"
            f"{pr['title'][:70]}\tcreated={pr['creationDate']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
