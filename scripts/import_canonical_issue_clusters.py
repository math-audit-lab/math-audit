#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_issue_clusters import import_canonical_cluster_decisions  # noqa: E402


def import_canonical_issue_clusters(
    audit_workdir: str | Path,
    decisions_json: str | Path,
    *,
    accept: bool = False,
    allow_overlap: bool = False,
) -> dict:
    workdir = Path(audit_workdir).expanduser().resolve()
    source = Path(decisions_json).expanduser().resolve()
    if not workdir.is_dir():
        raise ValueError(f"Audit workdir does not exist: {workdir}")
    if not source.is_file():
        raise ValueError(f"Decision JSON does not exist: {source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    return import_canonical_cluster_decisions(workdir, payload, accept=accept, allow_overlap=allow_overlap)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or accept canonical issue-cluster decisions.")
    parser.add_argument("--audit-workdir", required=True)
    parser.add_argument("--decisions-json", required=True)
    parser.add_argument("--accept", action="store_true", help="Append accepted decisions; default is dry-run.")
    parser.add_argument("--allow-overlap", action="store_true", help="Permit explicitly reviewed shared raw issue IDs.")
    args = parser.parse_args()
    result = import_canonical_issue_clusters(
        args.audit_workdir, args.decisions_json, accept=args.accept, allow_overlap=args.allow_overlap
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
