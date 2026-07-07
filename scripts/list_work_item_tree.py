#!/usr/bin/env python3
"""List all descendant work items under a root ADO work item (BFS)."""

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
API_VERSION = "7.1"


def main() -> int:
    root_id = int(sys.argv[1])
    provider = AdoTokenProvider()
    token = asyncio.run(provider.get_access_token())
    headers = {"Authorization": f"Bearer {token.token}"}

    seen = set()
    queue = [root_id]
    rows = []
    while queue:
        wid = queue.pop(0)
        if wid in seen:
            continue
        seen.add(wid)
        url = (
            f"{ORG}/{PROJECT}/_apis/wit/workitems/{wid}"
            f"?$expand=relations&api-version={API_VERSION}"
        )
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
        fields = data.get("fields", {})
        rows.append((wid, fields.get("System.WorkItemType"), fields.get("System.State"), fields.get("System.Title", "")[:60]))
        for rel in data.get("relations", []) or []:
            if rel.get("rel") == "System.LinkTypes.Hierarchy-Forward":
                child_url = rel["url"]
                child_id = int(child_url.rsplit("/", 1)[-1])
                queue.append(child_id)

    print(f"total work items: {len(rows)}")
    for wid, wtype, state, title in rows:
        print(f"{wid}\t{wtype}\t{state}\t{title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
