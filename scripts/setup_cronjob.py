#!/usr/bin/env python3
"""Create or update the cron-job.org job that dispatches the archive workflow.

Reads two local, git-ignored files (repo root):
  - "cron-job.org API key.txt"       -> cron-job.org REST API key
  - "github-fine-grained-token.txt"  -> GitHub fine-grained PAT

The PAT is only used to call the GitHub workflow-dispatch API. It is embedded
in the cron-job.org job headers, never in this repository.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.cron-job.org"
ROOT = Path(__file__).resolve().parent.parent


def read_secret(name: str) -> str:
    path = ROOT / name
    if not path.exists():
        raise SystemExit(f"missing {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit(f"{path} is empty")
    return value


def request(method: str, url: str, api_key: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"cron-job.org {method} {url} -> HTTP {exc.code}: {detail}") from exc


def build_job(owner: str, repo: str, token: str) -> dict:
    return {
        "job": {
            "title": f"NBRI rainfall -> {owner}/{repo}",
            "url": (
                f"https://api.github.com/repos/{owner}/{repo}"
                "/actions/workflows/archive.yml/dispatches"
            ),
            "enabled": True,
            "saveResponses": True,
            "requestMethod": 1,  # POST
            "requestTimeout": 30,
            "schedule": {
                "timezone": "UTC",
                "expiresAt": 0,
                "minutes": [2, 7, 12, 17, 22, 27, 32, 37, 42, 47, 52, 57],
                "hours": [-1],
                "mdays": [-1],
                "months": [-1],
                "wdays": [-1],
            },
            "extendedData": {
                "headers": {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json",
                },
                "body": json.dumps({"ref": "main"}),
            },
        }
    }


def redacted(payload: dict) -> dict:
    clone = json.loads(json.dumps(payload))
    headers = clone.get("job", {}).get("extendedData", {}).get("headers", {})
    for key in headers:
        if key.lower() == "authorization":
            headers[key] = "Bearer <redacted>"
    return clone


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", help="GitHub owner/user")
    parser.add_argument("--repo", help="GitHub repository name")
    parser.add_argument("--job-id", type=int, help="update this existing job instead of creating")
    parser.add_argument("--list", action="store_true", help="list existing jobs and exit")
    parser.add_argument("--dry-run", action="store_true", help="print the payload, change nothing")
    args = parser.parse_args()

    api_key = read_secret("cron-job.org API key.txt")

    if args.list:
        result = request("GET", f"{API}/jobs", api_key)
        jobs = result.get("jobs", [])
        for job in jobs:
            print(f"{job.get('jobId')}\t{job.get('enabled')}\t{job.get('title')}\t{job.get('url')}")
        return 0

    if not args.owner or not args.repo:
        raise SystemExit("--owner and --repo are required (or use --list)")

    token = "PLACEHOLDER_TOKEN" if args.dry_run else read_secret("github-fine-grained-token.txt")
    payload = build_job(args.owner, args.repo, token)

    if args.dry_run:
        print(json.dumps(redacted(payload), indent=2))
        return 0

    if args.job_id:
        request("PATCH", f"{API}/jobs/{args.job_id}", api_key, payload)
        print(f"updated job {args.job_id}")
    else:
        result = request("PUT", f"{API}/jobs", api_key, payload)
        print(f"created job {result.get('jobId')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
