#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.prepare_rerun_recheck_candidates import prepare_rerun_recheck_candidates  # noqa: E402


SCHEMA_VERSION = "1.0"
GENERIC_ANCHORS = {
    "asymptotic",
    "dependency",
    "equation",
    "error",
    "estimate",
    "expansion",
    "lemma",
    "proof",
    "theorem",
    "uniform",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _path_stat(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _snapshot_paths(paths: list[Path]) -> dict[str, tuple[int, int] | None]:
    return {str(path): _path_stat(path) for path in paths}


def _short_text(text: Any, limit: int = 220) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)].rstrip() + "..."


def _load_issues(workdir: Path) -> dict[str, dict[str, Any]]:
    payload = _load_json(workdir / "state" / "issues.json", default={})
    issues = payload.get("issues") if isinstance(payload, dict) else payload
    if not isinstance(issues, list):
        return {}
    return {str(item.get("issue_id")): item for item in issues if isinstance(item, dict) and item.get("issue_id")}


def _load_chunks(workdir: Path) -> dict[str, dict[str, Any]]:
    manifest = _load_json(workdir / "state" / "chunk_manifest.json", default={})
    chunks: dict[str, dict[str, Any]] = {}
    if isinstance(manifest, dict):
        for item in manifest.get("chunks") or []:
            if isinstance(item, dict) and item.get("chunk_id"):
                chunks[str(item["chunk_id"])] = dict(item)
    return chunks


def _source_fingerprint(paths: list[Path]) -> dict[str, Any]:
    total_size = 0
    latest_mtime_ns = 0
    existing = []
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if not path.is_file():
            continue
        existing.append(str(path))
        total_size += int(stat.st_size)
        latest_mtime_ns = max(latest_mtime_ns, int(stat.st_mtime_ns))
    return {
        "file_count": len(existing),
        "total_size_bytes": total_size,
        "latest_mtime_ns": latest_mtime_ns,
        "files": existing[:80],
        "truncated": len(existing) > 80,
    }


def _issue_severity(issue: dict[str, Any], candidates_by_issue: dict[str, dict[str, Any]]) -> str:
    issue_id = str(issue.get("issue_id") or "")
    candidate = candidates_by_issue.get(issue_id) or {}
    summary = candidate.get("evidence_summary") if isinstance(candidate.get("evidence_summary"), dict) else {}
    return str(issue.get("severity") or summary.get("severity") or "unknown").lower()


def _issue_title(issue_id: str, issues_by_id: dict[str, dict[str, Any]], candidates_by_issue: dict[str, dict[str, Any]]) -> str:
    issue = issues_by_id.get(issue_id) or {}
    candidate = candidates_by_issue.get(issue_id) or {}
    summary = candidate.get("evidence_summary") if isinstance(candidate.get("evidence_summary"), dict) else {}
    return str(issue.get("title") or summary.get("title") or issue_id)


def _issue_chunk(issue_id: str, issues_by_id: dict[str, dict[str, Any]], candidates_by_issue: dict[str, dict[str, Any]]) -> str:
    issue = issues_by_id.get(issue_id) or {}
    candidate = candidates_by_issue.get(issue_id) or {}
    refs = candidate.get("context_refs") if isinstance(candidate.get("context_refs"), dict) else {}
    source_ids = candidate.get("source_ids") if isinstance(candidate.get("source_ids"), list) else []
    return str(issue.get("chunk_id") or refs.get("chunk_id") or (source_ids[1] if len(source_ids) > 1 else ""))


def _candidate_issue_map(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_issue: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if candidate.get("item_type") != "issue":
            continue
        source_ids = candidate.get("source_ids") if isinstance(candidate.get("source_ids"), list) else []
        if source_ids:
            by_issue[str(source_ids[0])] = candidate
    return by_issue


def _group_issue_ids(group: dict[str, Any]) -> set[str]:
    ids = {str(item) for item in group.get("source_ids") or [] if item}
    for member in group.get("members") or []:
        if isinstance(member, dict) and member.get("issue_id"):
            ids.add(str(member["issue_id"]))
    context = group.get("context_refs") if isinstance(group.get("context_refs"), dict) else {}
    for member in context.get("members") or []:
        if isinstance(member, dict) and member.get("issue_id"):
            ids.add(str(member["issue_id"]))
    return ids


def _group_members(group: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(group.get("members"), list):
        return [item for item in group["members"] if isinstance(item, dict)]
    context = group.get("context_refs") if isinstance(group.get("context_refs"), dict) else {}
    return [item for item in context.get("members") or [] if isinstance(item, dict)]


def _group_id(group: dict[str, Any]) -> str:
    context = group.get("context_refs") if isinstance(group.get("context_refs"), dict) else {}
    return str(group.get("group_id") or context.get("group_id") or "")


def _normalize_anchor(value: Any) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    return text.strip("[]{}.,;:")


def _group_anchors(group: dict[str, Any]) -> set[str]:
    anchors: set[str] = set()
    for value in group.get("shared_features") or []:
        anchor = _normalize_anchor(value)
        if anchor and anchor not in GENERIC_ANCHORS:
            anchors.add(anchor)
    evidence = group.get("evidence_summary") if isinstance(group.get("evidence_summary"), dict) else {}
    for value in evidence.get("shared_features") or []:
        anchor = _normalize_anchor(value)
        if anchor and anchor not in GENERIC_ANCHORS:
            anchors.add(anchor)
    context = group.get("context_refs") if isinstance(group.get("context_refs"), dict) else {}
    for link in context.get("links") or []:
        shared = link.get("shared_features") if isinstance(link, dict) and isinstance(link.get("shared_features"), dict) else {}
        for values in shared.values():
            for value in values or []:
                anchor = _normalize_anchor(value)
                if anchor and anchor not in GENERIC_ANCHORS:
                    anchors.add(anchor)
    return anchors


def _equation_number(anchor: str) -> int | None:
    match = re.fullmatch(r"(?:equation\s*)?(\d+)", anchor)
    if match:
        return int(match.group(1))
    return None


def _appendix_prefix(anchor: str) -> str | None:
    match = re.search(r"\b([a-z])\.\d+\b", anchor)
    if match:
        return match.group(1)
    match = re.search(r"\b(?:theorem|proposition|lemma|corollary)\s+([a-z])\.\d+\b", anchor)
    if match:
        return match.group(1)
    return None


def _anchors_are_adjacent(left: set[str], right: set[str]) -> bool:
    left_numbers = {value for value in (_equation_number(anchor) for anchor in left) if value is not None}
    right_numbers = {value for value in (_equation_number(anchor) for anchor in right) if value is not None}
    return any(abs(left_number - right_number) <= 1 for left_number in left_numbers for right_number in right_numbers)


def _same_appendix(left: set[str], right: set[str]) -> bool:
    left_prefixes = {value for value in (_appendix_prefix(anchor) for anchor in left) if value}
    right_prefixes = {value for value in (_appendix_prefix(anchor) for anchor in right) if value}
    return bool(left_prefixes & right_prefixes)


def _should_merge_groups(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, str]:
    left_ids = _group_issue_ids(left)
    right_ids = _group_issue_ids(right)
    shared_ids = left_ids & right_ids
    left_anchors = _group_anchors(left)
    right_anchors = _group_anchors(right)
    shared_anchors = left_anchors & right_anchors
    if shared_anchors:
        return True, "shared specific reference/symbol: " + ", ".join(sorted(shared_anchors)[:4])
    if len(shared_ids) >= 2:
        return True, "shared multiple issue ids: " + ", ".join(sorted(shared_ids))
    if shared_ids and _anchors_are_adjacent(left_anchors, right_anchors):
        return True, "shared issue id and adjacent equation references"
    if shared_ids and _same_appendix(left_anchors, right_anchors):
        return True, "shared issue id and same appendix reference family"
    return False, ""


class _UnionFind:
    def __init__(self, values: list[int]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: int) -> int:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _dependency_group_candidates(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    groups = [group for group in manifest.get("groups") or [] if isinstance(group, dict)]
    candidates = [
        candidate
        for candidate in manifest.get("candidates") or []
        if isinstance(candidate, dict) and candidate.get("category") == "dependency_propagation"
    ]
    if groups:
        return [group for group in groups if len(_group_issue_ids(group)) >= 2]
    return [candidate for candidate in candidates if len(_group_issue_ids(candidate)) >= 2]


def _high_critical_issue_ids(manifest: dict[str, Any], issues_by_id: dict[str, dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for candidate in manifest.get("candidates") or []:
        if not isinstance(candidate, dict) or candidate.get("item_type") != "issue":
            continue
        source_ids = candidate.get("source_ids") if isinstance(candidate.get("source_ids"), list) else []
        if not source_ids:
            continue
        issue_id = str(source_ids[0])
        severity = str((candidate.get("evidence_summary") or {}).get("severity") or "").lower()
        if severity in {"critical", "high"}:
            ids.add(issue_id)
    for issue_id, issue in issues_by_id.items():
        if str(issue.get("status") or "open").lower() in {"closed", "resolved"}:
            continue
        if str(issue.get("severity") or "").lower() in {"critical", "high"}:
            ids.add(issue_id)
    return ids


def _main_refs_and_symbols(anchors: set[str]) -> tuple[list[str], list[str]]:
    references = []
    symbols = []
    for anchor in sorted(anchors):
        if _equation_number(anchor) is not None or _appendix_prefix(anchor) or re.search(
            r"\b(theorem|proposition|lemma|corollary|definition|remark)\b", anchor
        ):
            references.append(anchor)
        else:
            symbols.append(anchor)
    return references, symbols


def _display_reference(anchor: str) -> str:
    number = _equation_number(anchor)
    if number is not None:
        return f"Equation ({number})"
    return anchor[:1].upper() + anchor[1:]


def _display_symbol(symbol: str) -> str:
    match = re.fullmatch(r"([a-z])\(([a-z])\)", symbol)
    if match:
        return f"{match.group(1).upper()}({match.group(2).upper()})"
    return symbol


def _family_title(references: list[str], symbols: list[str], upstream_ids: list[str], issues_by_id: dict[str, dict[str, Any]], candidates_by_issue: dict[str, dict[str, Any]]) -> str:
    equation_numbers = [str(_equation_number(ref)) for ref in references if _equation_number(ref) is not None]
    theorem_refs = [ref for ref in references if re.search(r"\b(theorem|proposition|lemma|corollary|definition|remark)\b", ref)]
    appendix_refs = [ref for ref in references if _appendix_prefix(ref)]
    if equation_numbers and theorem_refs:
        return f"Equation ({equation_numbers[0]}) / {_display_reference(theorem_refs[0])} dependency chain"
    if len(equation_numbers) >= 2:
        joined = "/".join(f"({item})" for item in equation_numbers[:4])
        return f"Equations {joined} dependency family"
    if equation_numbers:
        return f"Equation ({equation_numbers[0]}) dependency family"
    if appendix_refs:
        prefix = _appendix_prefix(appendix_refs[0])
        label = f"Appendix {prefix.upper()}" if prefix else "Appendix"
        return f"{label} dependency family"
    if theorem_refs:
        return f"{_display_reference(theorem_refs[0])} dependency family"
    if symbols:
        return f"{_display_symbol(symbols[0])} dependency family"
    if upstream_ids:
        return _short_text(_issue_title(upstream_ids[0], issues_by_id, candidates_by_issue), 90)
    return "Dependency issue family"


def _priority(issue_ids: set[str], issues_by_id: dict[str, dict[str, Any]], candidates_by_issue: dict[str, dict[str, Any]]) -> str:
    ranks = {"critical": 3, "high": 2, "medium": 1, "low": 0, "unknown": 0}
    best = "unknown"
    for issue_id in issue_ids:
        severity = _issue_severity(issues_by_id.get(issue_id) or {"issue_id": issue_id}, candidates_by_issue)
        if ranks.get(severity, 0) > ranks.get(best, 0):
            best = severity
    if best == "critical":
        return "high"
    if best == "high":
        return "high"
    if best == "medium":
        return "medium"
    return "low"


def _build_family(
    raw_family_id: int,
    family_groups: list[dict[str, Any]],
    issues_by_id: dict[str, dict[str, Any]],
    chunks_by_id: dict[str, dict[str, Any]],
    candidates_by_issue: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    issue_ids: set[str] = set()
    source_group_ids = []
    anchors: set[str] = set()
    upstream_ids: set[str] = set()
    downstream_ids: set[str] = set()
    for group in family_groups:
        group_id = _group_id(group)
        if group_id:
            source_group_ids.append(group_id)
        issue_ids.update(_group_issue_ids(group))
        anchors.update(_group_anchors(group))
        context = group.get("context_refs") if isinstance(group.get("context_refs"), dict) else {}
        if context.get("upstream_issue_id"):
            upstream_ids.add(str(context["upstream_issue_id"]))
        if group.get("upstream_issue_id"):
            upstream_ids.add(str(group["upstream_issue_id"]))
        for member in _group_members(group):
            issue_id = str(member.get("issue_id") or "")
            role = str(member.get("role") or "")
            if not issue_id:
                continue
            if role == "candidate_upstream":
                upstream_ids.add(issue_id)
            elif role == "possible_downstream":
                downstream_ids.add(issue_id)

    downstream_ids -= upstream_ids
    related_ids = issue_ids - upstream_ids - downstream_ids
    references, symbols = _main_refs_and_symbols(anchors)
    chunks = []
    for issue_id in sorted(issue_ids):
        chunk_id = _issue_chunk(issue_id, issues_by_id, candidates_by_issue)
        if not chunk_id:
            continue
        chunk = chunks_by_id.get(chunk_id) or {}
        chunks.append(
            {
                "chunk_id": chunk_id,
                "label": chunk.get("display_label") or chunk.get("label") or "",
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
            }
        )
    seen_chunks = []
    seen_chunk_ids = set()
    for chunk in chunks:
        if chunk["chunk_id"] in seen_chunk_ids:
            continue
        seen_chunk_ids.add(chunk["chunk_id"])
        seen_chunks.append(chunk)
    priority = _priority(issue_ids, issues_by_id, candidates_by_issue)
    recommended_action = "family_issue_recheck" if upstream_ids and downstream_ids else "human_review"
    if downstream_ids:
        recommended_action = "group_downstream_under_upstream"
    title = _family_title(references, symbols, sorted(upstream_ids), issues_by_id, candidates_by_issue)
    return {
        "family_id": f"F{raw_family_id:03d}",
        "title": title,
        "primary_upstream_issue_ids": sorted(upstream_ids),
        "downstream_issue_ids": sorted(downstream_ids),
        "related_issue_ids": sorted(related_ids),
        "all_issue_ids": sorted(issue_ids),
        "main_references": references,
        "main_symbols": symbols,
        "chunks": seen_chunks,
        "recommended_action": recommended_action,
        "priority": priority,
        "review_notes": (
            "Review this family as a dependency unit. The script does not resolve, suppress, or downgrade any issue."
        ),
        "source_group_ids": sorted(set(source_group_ids)),
        "issue_summaries": [
            {
                "issue_id": issue_id,
                "severity": _issue_severity(issues_by_id.get(issue_id) or {"issue_id": issue_id}, candidates_by_issue),
                "chunk_id": _issue_chunk(issue_id, issues_by_id, candidates_by_issue),
                "title": _issue_title(issue_id, issues_by_id, candidates_by_issue),
            }
            for issue_id in sorted(issue_ids)
        ],
    }


def _consolidate_families(
    candidate_manifest: dict[str, Any],
    issues_by_id: dict[str, dict[str, Any]],
    chunks_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    groups = _dependency_group_candidates(candidate_manifest)
    candidates_by_issue = _candidate_issue_map(candidate_manifest.get("candidates") or [])
    uf = _UnionFind(list(range(len(groups))))
    merge_reasons: list[dict[str, Any]] = []
    for left_index, left in enumerate(groups):
        for right_index, right in enumerate(groups[left_index + 1 :], start=left_index + 1):
            should_merge, reason = _should_merge_groups(left, right)
            if not should_merge:
                continue
            uf.union(left_index, right_index)
            merge_reasons.append(
                {
                    "left_group_id": _group_id(left),
                    "right_group_id": _group_id(right),
                    "reason": reason,
                }
            )

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, group in enumerate(groups):
        grouped[uf.find(index)].append(group)

    families = [
        _build_family(index, family_groups, issues_by_id, chunks_by_id, candidates_by_issue)
        for index, family_groups in enumerate(grouped.values(), start=1)
    ]
    families.sort(key=lambda item: (item["chunks"][0]["chunk_id"] if item.get("chunks") else "", item["family_id"]))
    for index, family in enumerate(families, start=1):
        family["family_id"] = f"F{index:03d}"

    assigned = {issue_id for family in families for issue_id in family["all_issue_ids"]}
    high_critical = _high_critical_issue_ids(candidate_manifest, issues_by_id)
    membership: Counter[str] = Counter()
    for family in families:
        for issue_id in family["all_issue_ids"]:
            membership[issue_id] += 1
    overlap_warnings = [
        {
            "issue_id": issue_id,
            "family_count": count,
            "family_ids": [family["family_id"] for family in families if issue_id in family["all_issue_ids"]],
        }
        for issue_id, count in sorted(membership.items())
        if count > 1
    ]
    summary = {
        "total_families": len(families),
        "total_issue_ids_covered_by_families": len(assigned),
        "high_critical_issue_ids": sorted(high_critical),
        "high_critical_issue_ids_not_assigned_to_family": sorted(high_critical - assigned),
        "issues_appearing_in_multiple_families": overlap_warnings,
        "merge_reasons": merge_reasons,
        "source_dependency_group_count": len(groups),
    }
    return families, summary


def _markdown_report(manifest: dict[str, Any]) -> str:
    summary = manifest.get("summary") or {}
    lines = [
        "# Issue Recheck Families",
        "",
        "This deterministic preparation pass consolidates dependency-group candidates for review planning only.",
        "It does not call the API, run verification, rerun chunks, resolve issues, or mutate audit state.",
        "",
        "## Summary",
        f"- Source audit: {manifest.get('audit_workdir')}",
        f"- Families: {summary.get('total_families', 0)}",
        f"- Issue IDs covered by families: {summary.get('total_issue_ids_covered_by_families', 0)}",
        f"- Source dependency groups: {summary.get('source_dependency_group_count', 0)}",
        f"- High/critical issues not assigned: {len(summary.get('high_critical_issue_ids_not_assigned_to_family') or [])}",
        f"- Source unmodified by script: {manifest.get('source_unmodified_by_script')}",
        "",
    ]
    unassigned = summary.get("high_critical_issue_ids_not_assigned_to_family") or []
    if unassigned:
        lines.extend(["## High/Critical Issues Not Assigned To A Family", ""])
        lines.append("- " + ", ".join(unassigned))
        lines.append("")

    overlaps = summary.get("issues_appearing_in_multiple_families") or []
    if overlaps:
        lines.extend(["## Possible Overlap Warnings", ""])
        for item in overlaps:
            lines.append(f"- {item.get('issue_id')}: {', '.join(item.get('family_ids') or [])}")
        lines.append("")

    lines.extend(["## Families", ""])
    for family in manifest.get("families") or []:
        lines.append(f"### {family.get('family_id')} - {family.get('title')}")
        lines.append(f"- Priority: {family.get('priority')}")
        lines.append(f"- Recommended action: {family.get('recommended_action')}")
        lines.append(f"- Upstream issues: {', '.join(family.get('primary_upstream_issue_ids') or []) or 'none'}")
        lines.append(f"- Downstream issues: {', '.join(family.get('downstream_issue_ids') or []) or 'none'}")
        lines.append(f"- Related issues: {', '.join(family.get('related_issue_ids') or []) or 'none'}")
        lines.append(f"- Source groups: {', '.join(family.get('source_group_ids') or []) or 'none'}")
        lines.append(f"- Main references: {', '.join(family.get('main_references') or []) or 'none'}")
        lines.append(f"- Main symbols: {', '.join(family.get('main_symbols') or []) or 'none'}")
        chunks = [chunk.get("chunk_id") for chunk in family.get("chunks") or [] if chunk.get("chunk_id")]
        lines.append(f"- Source chunks: {', '.join(chunks) or 'none'}")
        lines.append(f"- Review note: {family.get('review_notes')}")
        lines.append("")
        for issue in family.get("issue_summaries") or []:
            lines.append(
                f"- {issue.get('issue_id')} [{issue.get('severity')}] {issue.get('chunk_id')}: "
                f"{_short_text(issue.get('title'), 140)}"
            )
        lines.append("")
    return "\n".join(lines)


def prepare_issue_recheck_families(
    audit_workdir: Path,
    output_dir: Path,
    *,
    candidates_json: Path | None = None,
    allow_output_inside_audit: bool = False,
) -> dict[str, Any]:
    audit_workdir = audit_workdir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not audit_workdir.exists():
        raise RuntimeError(f"Audit workdir does not exist: {audit_workdir}")
    if not allow_output_inside_audit and (output_dir == audit_workdir or audit_workdir in output_dir.parents):
        raise RuntimeError("Output directory must not be inside the source audit workdir.")

    if candidates_json is None:
        candidate_dir = output_dir / "candidate_inventory"
        candidate_manifest = prepare_rerun_recheck_candidates(
            audit_workdir,
            candidate_dir,
            allow_output_inside_audit=allow_output_inside_audit,
        )
        candidates_json_path = candidate_dir / "rerun_recheck_candidates.json"
    else:
        candidates_json_path = candidates_json.expanduser().resolve()
        candidate_manifest = _load_json(candidates_json_path, default={})
    if not isinstance(candidate_manifest, dict):
        raise RuntimeError(f"Candidate JSON is not a JSON object: {candidates_json_path}")

    issues_path = audit_workdir / "state" / "issues.json"
    chunks_path = audit_workdir / "state" / "chunk_manifest.json"
    source_paths = [issues_path, chunks_path, candidates_json_path]
    before = _snapshot_paths(source_paths)

    issues_by_id = _load_issues(audit_workdir)
    chunks_by_id = _load_chunks(audit_workdir)
    families, summary = _consolidate_families(candidate_manifest, issues_by_id, chunks_by_id)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "audit_workdir": str(audit_workdir),
        "candidates_json": str(candidates_json_path),
        "output_dir": str(output_dir),
        "source_mutation_policy": (
            "read-only; canonical audit state and candidate JSON are never modified"
            if allow_output_inside_audit
            else "read-only; source audit folder and candidate JSON are never modified"
        ),
        "source_fingerprint": _source_fingerprint(source_paths),
        "summary": summary,
        "families": families,
        "warnings": [],
        "source_unmodified_by_script": None,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "issue_recheck_families.json", manifest)
    (output_dir / "issue_recheck_families.md").write_text(_markdown_report(manifest), encoding="utf-8")

    after = _snapshot_paths(source_paths)
    manifest["source_unmodified_by_script"] = before == after
    _write_json(output_dir / "issue_recheck_families.json", manifest)
    (output_dir / "issue_recheck_families.md").write_text(_markdown_report(manifest), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Consolidate rerun/recheck dependency groups into read-only issue families."
    )
    parser.add_argument("--audit-workdir", required=True, type=Path)
    parser.add_argument("--candidates-json", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    manifest = prepare_issue_recheck_families(
        args.audit_workdir,
        args.output_dir,
        candidates_json=args.candidates_json,
    )
    summary = manifest.get("summary") or {}
    print("Issue recheck families prepared.")
    print(f"  Source audit: {manifest['audit_workdir']}")
    print(f"  Candidates JSON: {manifest['candidates_json']}")
    print(f"  Output dir: {manifest['output_dir']}")
    print(f"  Families: {summary.get('total_families', 0)}")
    print(f"  Issue IDs covered: {summary.get('total_issue_ids_covered_by_families', 0)}")
    print(f"  High/critical unassigned: {len(summary.get('high_critical_issue_ids_not_assigned_to_family') or [])}")
    print(f"  Source unmodified by script: {manifest['source_unmodified_by_script']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
