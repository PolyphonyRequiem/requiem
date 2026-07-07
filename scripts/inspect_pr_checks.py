#!/usr/bin/env python3
"""Inspect an ADO PR's status checks / policy evaluations via REST."""

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
    pr_id = int(sys.argv[1])
    provider = AdoTokenProvider()
    token = asyncio.run(provider.get_access_token())
    headers = {"Authorization": f"Bearer {token.token}"}

    # PR statuses (external/build status feed)
    url = f"{ORG}/{PROJECT}/_apis/git/repositories/{REPO}/pullRequests/{pr_id}/statuses?api-version={API_VERSION}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)
    print("=== statuses ===")
    print(json.dumps(data, indent=2)[:2000])

    # Policy evaluations against the target branch
    pr_url = f"{ORG}/{PROJECT}/_apis/git/repositories/{REPO}/pullRequests/{pr_id}?api-version={API_VERSION}"
    req2 = urllib.request.Request(pr_url, headers=headers)
    with urllib.request.urlopen(req2) as resp2:
        pr_data = json.load(resp2)
    artifact_id = f"vstfs:///CodeReview/CodeReviewId/{pr_data['repository']['project']['id']}/{pr_id}"
    eval_url = f"{ORG}/{PROJECT}/_apis/policy/evaluations?artifactId={artifact_id}&api-version={API_VERSION}"
    req3 = urllib.request.Request(eval_url, headers=headers)
    with urllib.request.urlopen(req3) as resp3:
        eval_data = json.load(resp3)
    print("=== policy evaluations ===")
    print(json.dumps(eval_data, indent=2)[:3000])
    print("=== target branch ===", pr_data.get("targetRefName"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
