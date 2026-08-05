#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_issue_clusters import canonical_issue_cluster_view, load_raw_issue_state  # noqa: E402
from audit_state import utc_now  # noqa: E402


SCHEMA_VERSION = 1
_ISSUE_ID_RE = re.compile(r"\bI\d{3,}\b", re.IGNORECASE)
_SPECIFIC_REFERENCE_RE = re.compile(
    r"\b(?:equations?|eq\.?|theorems?|lemmas?|propositions?|corollaries?|definitions?|remarks?|sections?|tables?|figures?)\s*"
    r"(?:\(([A-Za-z]?\.?\d+(?:\.\d+)*)\)|([A-Za-z]?\.?\d+(?:\.\d+)*))",
    re.IGNORECASE,
)
_SOURCE_LABEL_RE = re.compile(
    r"(?:\b(?:labeled|label)\s+|\\(?:eq)?ref\{)\{?([A-Za-z][A-Za-z0-9:._-]+)",
    re.IGNORECASE,
)
_DOWNSTREAM_RE = re.compile(
    r"\b(?:downstream|depends? on|dependent on|inherits?|propagat(?:e|es|ed|ion)|conditional on|if (?:the )?earlier issue)\b",
    re.IGNORECASE,
)
_GENERIC_WORDS = {
    "a", "an", "and", "as", "at", "by", "equation", "error", "estimate", "for", "from", "in",
    "is", "label", "labeled", "lemma", "mathematical", "of", "on", "or", "proof", "proposition",
    "term", "that", "the", "theorem", "this", "to", "uniform", "with", "evidence", "fix", "correctness",
}


def _guard_output(audit_workdir: Path, output_dir: Path) -> None:
    audit = audit_workdir.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    if output == audit or audit in output.parents:
        raise ValueError("Output directory must be outside the source audit workdir")


def _text(issue: dict[str, Any]) -> str:
    values = [
        issue.get("title"), issue.get("location"), issue.get("description"), issue.get("evidence"),
        issue.get("proposed_fix"), " ".join(str(item) for item in issue.get("tags") or []),
    ]
    return " ".join(str(value or "") for value in values)


def _normalized(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _anchors(issue: dict[str, Any]) -> list[str]:
    text = _text(issue)
    anchors = set()
    for match in _SPECIFIC_REFERENCE_RE.finditer(text):
        number = match.group(1) or match.group(2)
        kind = re.match(r"[A-Za-z]+", match.group(0)).group(0).lower()  # type: ignore[union-attr]
        anchors.add(f"{kind}:{number.lower()}")
    anchors.update(f"source_label:{match.group(1).lower()}" for match in _SOURCE_LABEL_RE.finditer(text))
    anchors.update(item.upper() for item in _ISSUE_ID_RE.findall(text))
    return sorted(anchors)


def _tokens(issue: dict[str, Any]) -> set[str]:
    anchor_parts = {
        part for anchor in _anchors(issue)
        for part in re.findall(r"[a-z][a-z0-9_-]{2,}", anchor.lower())
    }
    return {
        token for token in re.findall(
            r"[a-z][a-z0-9_-]{3,}",
            f"{issue.get('title') or ''} {issue.get('description') or ''}".lower(),
        )
        if token not in _GENERIC_WORDS and token not in anchor_parts
    }


class _UnionFind:
    def __init__(self, ids: list[str]) -> None:
        self.parent = {item: item for item in ids}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def _load_recheck_links(workdir: Path) -> list[tuple[str, str, str]]:
    path = workdir / "state" / "issue_rechecks.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    links: list[tuple[str, str, str]] = []
    for recheck in payload.get("rechecks") or []:
        if not isinstance(recheck, dict):
            continue
        for group in recheck.get("grouping_recommendations") or []:
            if not isinstance(group, dict):
                continue
            upstream = str(group.get("upstream_issue_id") or "")
            for downstream in group.get("downstream_issue_ids") or []:
                if upstream and downstream:
                    links.append((upstream, str(downstream), "accepted issue-family recheck grouping"))
    return links


def prepare_canonical_issue_clusters(audit_workdir: str | Path, output_dir: str | Path) -> dict[str, Any]:
    workdir = Path(audit_workdir).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if not workdir.is_dir():
        raise ValueError(f"Audit workdir does not exist: {workdir}")
    _guard_output(workdir, output)
    issues_path = workdir / "state" / "issues.json"
    before = issues_path.read_bytes() if issues_path.exists() else b""
    issues = [item for item in load_raw_issue_state(workdir).get("issues") or [] if isinstance(item, dict)]
    by_id = {str(item.get("issue_id")): item for item in issues if item.get("issue_id")}
    ids = sorted(by_id)
    union = _UnionFind(ids)
    relations: list[dict[str, Any]] = []

    def add_relation(left: str, right: str, relation: str, confidence: str, reason: str, merge: bool) -> None:
        relations.append({
            "issue_id": left, "related_issue_id": right, "proposed_relation": relation,
            "confidence": confidence, "reason": reason, "requires_human_confirmation": True,
        })
        if merge:
            union.union(left, right)

    for index, left_id in enumerate(ids):
        left = by_id[left_id]
        left_title = _normalized(left.get("title"))
        left_location = _normalized(left.get("location"))
        left_anchors = set(_anchors(left))
        left_tokens = _tokens(left)
        for right_id in ids[index + 1:]:
            right = by_id[right_id]
            right_title = _normalized(right.get("title"))
            right_location = _normalized(right.get("location"))
            shared_anchors = sorted(left_anchors & set(_anchors(right)))
            explicit_left = right_id.upper() in {item.upper() for item in _ISSUE_ID_RE.findall(_text(left))}
            explicit_right = left_id.upper() in {item.upper() for item in _ISSUE_ID_RE.findall(_text(right))}
            if left_title and left_title == right_title and left_location and left_location == right_location:
                add_relation(left_id, right_id, "duplicate_of", "high", "same normalized title and location", True)
            elif (explicit_left or explicit_right) and (_DOWNSTREAM_RE.search(_text(left)) or _DOWNSTREAM_RE.search(_text(right))):
                upstream, downstream = (right_id, left_id) if explicit_left else (left_id, right_id)
                add_relation(downstream, upstream, "downstream_consequence_of", "high", "explicit issue reference with downstream wording", True)
            elif shared_anchors:
                right_tokens = _tokens(right)
                overlap = len(left_tokens & right_tokens)
                overlap_ratio = overlap / max(1, min(len(left_tokens), len(right_tokens)))
                strong_overlap = overlap >= 3 and overlap_ratio >= 0.6
                relation = "same_root_cause_as" if strong_overlap else "partially_overlaps"
                add_relation(
                    left_id,
                    right_id,
                    relation,
                    "medium",
                    f"shared specific references: {', '.join(shared_anchors)}; claim-token overlap {overlap}/{min(len(left_tokens), len(right_tokens))}",
                    strong_overlap,
                )

    for upstream, downstream, reason in _load_recheck_links(workdir):
        if upstream in by_id and downstream in by_id:
            add_relation(downstream, upstream, "downstream_consequence_of", "high", reason, True)

    members: dict[str, list[str]] = defaultdict(list)
    for issue_id in ids:
        members[union.find(issue_id)].append(issue_id)
    proposals = []
    for ordinal, member_ids in enumerate(sorted(members.values(), key=lambda values: values[0]), start=1):
        member_set = set(member_ids)
        local_relations = [item for item in relations if item["issue_id"] in member_set and item["related_issue_id"] in member_set]
        representative = by_id[member_ids[0]]
        proposals.append({
            "proposal_id": f"P{ordinal:04d}",
            "suggested_cluster_id": f"C{ordinal:04d}",
            "suggested_title": str(representative.get("title") or f"Issue cluster {ordinal}"),
            "raw_issue_ids": member_ids,
            "specific_references": sorted({anchor for issue_id in member_ids for anchor in _anchors(by_id[issue_id])}),
            "proposed_relations": local_relations,
            "confidence": "high" if any(item["confidence"] == "high" for item in local_relations) else "medium" if local_relations else "low",
            "ambiguity_notes": [] if local_relations else ["Singleton candidate; no deterministic semantic merge was inferred."],
            "requires_human_confirmation": True,
        })
    accepted = canonical_issue_cluster_view(workdir)
    output.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "audit_workdir": str(workdir),
        "raw_issue_count": len(ids),
        "proposal_count": len(proposals),
        "proposals": proposals,
        "cross_proposal_relations": [item for item in relations if union.find(item["issue_id"]) != union.find(item["related_issue_id"])],
        "accepted_canonical_summary": accepted.get("summary", {}),
        "warnings": ["Deterministic proposals are review aids, not accepted mathematical conclusions."],
    }
    json_path = output / "canonical_issue_cluster_candidates.json"
    md_path = output / "canonical_issue_cluster_candidates.md"
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# Canonical issue-cluster candidates", "", "> These deterministic proposals require human confirmation.", ""]
    for item in proposals:
        lines.extend([
            f"## {item['proposal_id']} - {item['suggested_title']}",
            f"- Suggested cluster: {item['suggested_cluster_id']}",
            f"- Raw issues: {', '.join(item['raw_issue_ids'])}",
            f"- Confidence: {item['confidence']}",
            f"- Specific references: {', '.join(item['specific_references']) or 'none'}",
            "",
        ])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    after = issues_path.read_bytes() if issues_path.exists() else b""
    if after != before:
        raise RuntimeError("Source raw issue ledger changed during read-only preparation")
    return {**result, "output_paths": {"json": str(json_path), "markdown": str(md_path)}}


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare deterministic canonical issue-cluster candidates.")
    parser.add_argument("--audit-workdir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    result = prepare_canonical_issue_clusters(args.audit_workdir, args.output_dir)
    print(json.dumps({"proposal_count": result["proposal_count"], **result["output_paths"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
