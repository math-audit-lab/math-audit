#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_verification import (  # noqa: E402
    TECHNICAL_VERIFICATION_FAILURE_STATUSES,
    _normalize_verification_result,
    verification_result_needs_technical_retry,
)
from scripts.prepare_issue_recheck_candidates import (  # noqa: E402
    SEVERITY_RANK,
    _build_evidence,
    _candidate_from_issue,
    _collect_structured_summaries,
    _extract_risk_terms,
    _flatten_ledger_items,
    _issue_text,
    _load_chunks,
    _load_issues,
    _load_json,
    _read_jsonl,
    _short_text,
    _snapshot_paths,
    _verification_index,
    _write_json,
)


SCHEMA_VERSION = "1.0"
FAILED_VERIFICATION_STATUSES = set(TECHNICAL_VERIFICATION_FAILURE_STATUSES)
ACTION_KIND_LABELS = {
    "issue_recheck": "Issue-level recheck candidates",
    "chunk_rerun": "Full chunk rerun candidates",
    "script_recheck": "Verification script/claim recheck candidates",
    "dependency_group_review": "Dependency grouping candidates",
    "human_review": "Human review candidates",
    "technical_retry": "Technical recovery candidates",
}
TECHNICAL_FAILURE_LOGS = (
    "failed_chunks.jsonl",
    "parse_failures.jsonl",
    "pending_response_cancellations.jsonl",
    "file_download_timeout_auto_retries.jsonl",
)
NOTATION_REGIME_TAGS = {
    "ambiguous-notation",
    "quantifier",
    "undefined-notation",
    "variable-domain",
}
NOTATION_REGIME_PHRASES = {
    "ambiguous notation",
    "convention not stated",
    "domain of variable unclear",
    "epsilon not fixed",
    "integer vs real",
    "k=0 endpoint",
    "notation not defined",
    "parameter not fixed",
    "quantifier missing",
    "range not specified",
    "symbol not introduced",
    "undefined notation",
    "variable ambiguity",
    "variable is integer",
    "variable is real",
    "varepsilon not fixed",
}
GENERIC_DEPENDENCY_FEATURES = {
    "asymptotic",
    "chunk",
    "context",
    "depend",
    "dependence",
    "depends",
    "earlier",
    "equation",
    "error",
    "estimate",
    "expansion",
    "explicit",
    "finite-difference",
    "lambda",
    "lemma",
    "o(1)",
    "proof",
    "proposition",
    "rho",
    "term",
    "theorem",
    "uniform",
    "\\lambda",
    "\\rho",
}
DEPENDENCY_TERMS = {
    "conditional on",
    "depend",
    "dependency",
    "downstream",
    "earlier issue",
    "if the earlier issue",
    "inherits",
    "propagat",
    "relies on",
    "unresolved",
}
REFERENCE_TERMS = {
    "circular citation",
    "circular reference",
    "identity being proved",
    "proof cites",
    "wrong reference",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _safe_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    return _read_jsonl(path)


def _source_fingerprint(paths: list[Path]) -> dict[str, Any]:
    existing = []
    total_size = 0
    latest_mtime_ns = 0
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


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    return (
        str(candidate.get("recommended_action_kind") or ""),
        str(candidate.get("category") or ""),
        priority_rank.get(str(candidate.get("priority") or ""), 9),
        ",".join(str(item) for item in candidate.get("source_ids") or []),
    )


def _assign_candidate_ids(candidates: list[dict[str, Any]]) -> None:
    candidates.sort(key=_candidate_sort_key)
    for index, candidate in enumerate(candidates, start=1):
        candidate["candidate_id"] = f"RR{index:03d}"


def _issue_is_open(issue: dict[str, Any]) -> bool:
    return str(issue.get("status") or "open").strip().lower() not in {"resolved", "closed"}


def _issue_severity(issue: dict[str, Any]) -> str:
    severity = str(issue.get("severity") or "unknown").strip().lower()
    return severity if severity in SEVERITY_RANK else "unknown"


def _notation_regime_reasons(issue: dict[str, Any]) -> list[str]:
    text = _issue_text(issue).lower()
    tags = {str(tag).lower() for tag in issue.get("tags") or []}
    reasons = [f"tag:{tag}" for tag in sorted(tags & NOTATION_REGIME_TAGS)]
    for phrase in sorted(NOTATION_REGIME_PHRASES):
        if phrase in text:
            reasons.append(f"phrase:{phrase}")
    for match in re.finditer(r"\b([a-z]|[A-Z][A-Za-z0-9_*]*)\s+(?:is\s+)?not\s+defined\b", text):
        reasons.append(f"undefined-symbol:{match.group(1)}")
    for match in re.finditer(
        r"\b(domain|range|regime|parameter|quantifier|convention)\b.{0,60}\b(ambiguous|unclear|not specified|not stated|not fixed|missing)\b",
        text,
    ):
        reasons.append(f"explicit-{match.group(1)}-{match.group(2).replace(' ', '-')}")
    return sorted(set(reasons))


def _issue_has_notation_regime_terms(issue: dict[str, Any]) -> bool:
    return bool(_notation_regime_reasons(issue))


def _issue_has_dependency_terms(issue: dict[str, Any]) -> bool:
    text = _issue_text(issue).lower()
    tags = {str(tag).lower() for tag in issue.get("tags") or []}
    return bool(tags & {"dependency", "downstream", "propagation", "inherited"}) or any(
        term in text for term in DEPENDENCY_TERMS
    )


def _issue_has_reference_terms(issue: dict[str, Any]) -> bool:
    text = _issue_text(issue).lower()
    tags = {str(tag).lower() for tag in issue.get("tags") or []}
    return bool(tags & {"circular-citation", "identity-being-proved", "reference-error", "wrong-reference"}) or any(
        term in text for term in REFERENCE_TERMS
    )


def _candidate_text(candidate: dict[str, Any]) -> str:
    return "\n".join(
        str(item)
        for item in [
            candidate.get("issue_id"),
            candidate.get("title"),
            candidate.get("location"),
            candidate.get("short_description"),
            candidate.get("proposed_fix"),
            " ".join(str(tag) for tag in candidate.get("tags") or []),
        ]
        if item
    )


def _candidate_has_dependency_or_reference_terms(candidate: dict[str, Any]) -> bool:
    text = _candidate_text(candidate).lower()
    tags = {str(tag).lower() for tag in candidate.get("tags") or []}
    if tags & {"circular-citation", "dependency", "downstream", "identity-being-proved", "propagation", "reference-error", "wrong-reference"}:
        return True
    return any(term in text for term in DEPENDENCY_TERMS | REFERENCE_TERMS)


def _explicit_issue_refs(candidate: dict[str, Any], known_issue_ids: set[str]) -> set[str]:
    self_id = str(candidate.get("issue_id") or "")
    refs = set(re.findall(r"\bI\d{2,5}\b", _candidate_text(candidate)))
    return {ref for ref in refs if ref in known_issue_ids and ref != self_id}


def _specific_feature_values(candidate: dict[str, Any]) -> dict[str, set[str]]:
    features = candidate.get("features") or {}
    equations = {
        str(item).strip().lower()
        for item in features.get("equation_refs") or []
        if str(item).strip() and str(item).strip().lower() not in GENERIC_DEPENDENCY_FEATURES
    }
    theorem_refs = {
        str(item).strip().lower()
        for item in features.get("theorem_refs") or []
        if str(item).strip() and str(item).strip().lower() not in GENERIC_DEPENDENCY_FEATURES
    }
    symbols: set[str] = set()
    for raw in features.get("symbols") or []:
        symbol = str(raw).strip()
        lower = symbol.lower()
        if not symbol or lower in GENERIC_DEPENDENCY_FEATURES:
            continue
        if re.fullmatch(r"[A-Z]\s*\([A-Za-z0-9_*+\-/,\s]{1,24}\)", symbol) and lower not in {"s(n,k)"}:
            symbols.add(lower)
        elif re.search(r"\b[A-Z]\.\d+\b", symbol):
            symbols.add(lower)
    return {
        "equation_refs": equations,
        "theorem_refs": theorem_refs,
        "symbols": symbols,
    }


def _strict_dependency_groups(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    known_issue_ids = {str(candidate.get("issue_id") or "") for candidate in candidates if candidate.get("issue_id")}
    by_id = {str(candidate.get("issue_id")): candidate for candidate in candidates if candidate.get("issue_id")}
    group_sets: dict[tuple[str, ...], dict[str, Any]] = {}
    links: list[dict[str, Any]] = []

    def add_group(members: list[dict[str, Any]], reason: str, feature: str, link_kind: str) -> None:
        member_ids = tuple(sorted(str(member.get("issue_id") or "") for member in members if member.get("issue_id")))
        if len(member_ids) < 2:
            return
        payload = group_sets.setdefault(member_ids, {"members": [by_id[item] for item in member_ids], "reasons": set(), "features": set(), "links": []})
        payload["reasons"].add(reason)
        payload["features"].add(feature)
        for left_index, left_id in enumerate(member_ids):
            for right_id in member_ids[left_index + 1 :]:
                payload["links"].append(
                    {
                        "left_issue_id": left_id,
                        "right_issue_id": right_id,
                        "shared_features": {link_kind: [feature]},
                        "reasons": [reason],
                    }
                )

    for candidate in candidates:
        for ref in _explicit_issue_refs(candidate, known_issue_ids):
            add_group(
                [candidate, by_id[ref]],
                "explicit issue-id reference",
                ref,
                "issue_refs",
            )

    keyed: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for candidate in candidates:
        features = _specific_feature_values(candidate)
        for kind, values in features.items():
            for value in values:
                keyed.setdefault((kind, value), []).append(candidate)

    for (kind, value), members in sorted(keyed.items()):
        unique_members = {str(member.get("issue_id") or ""): member for member in members if member.get("issue_id")}
        if len(unique_members) < 2:
            continue
        member_list = list(unique_members.values())
        if not any(_candidate_has_dependency_or_reference_terms(member) for member in member_list):
            continue
        if kind == "theorem_refs" and not all(_candidate_has_dependency_or_reference_terms(member) for member in member_list):
            continue
        reason = {
            "equation_refs": "shared specific equation/reference label with dependency/reference wording",
            "theorem_refs": "shared theorem-like label with dependency/reference wording",
            "symbols": "shared distinctive symbol with dependency/reference wording",
        }.get(kind, "shared specific feature with dependency/reference wording")
        add_group(member_list, reason, value, kind)

    groups: list[dict[str, Any]] = []
    for member_ids, payload in sorted(group_sets.items(), key=lambda item: (min((by_id[issue_id].get("chunk_index") or 0) for issue_id in item[0]), item[0])):
        members = list(payload["members"])
        members.sort(key=lambda item: (item.get("chunk_index") or 0, item.get("issue_id") or ""))
        upstream = next((item for item in members if not _candidate_has_dependency_or_reference_terms(item)), members[0])
        group_id = f"G{len(groups) + 1:03d}"
        member_payload = []
        for member in members:
            if member["issue_id"] == upstream["issue_id"]:
                role = "candidate_upstream"
            elif _candidate_has_dependency_or_reference_terms(member):
                role = "possible_downstream"
            else:
                role = "related_same_topic"
            member["group_ids"].append(group_id)
            member["group_role"] = role
            member_payload.append(
                {
                    "issue_id": member["issue_id"],
                    "chunk_id": member["chunk_id"],
                    "severity": member["severity"],
                    "role": role,
                    "title": member["title"],
                }
            )
        groups.append(
            {
                "group_id": group_id,
                "upstream_issue_id": upstream["issue_id"],
                "classification": "tentative_dependency_group_strict",
                "members": member_payload,
                "link_reasons": sorted(payload["reasons"]),
                "shared_features": sorted(payload["features"]),
                "links": payload["links"],
            }
        )
        links.extend(payload["links"])
    return groups, links


def _issue_selection_reasons(issue: dict[str, Any], include_medium: bool) -> list[str]:
    if not _issue_is_open(issue):
        return []
    severity = _issue_severity(issue)
    if severity in {"critical", "high"}:
        reasons = [f"open {severity} issue"]
    elif include_medium and severity == "medium" and (_extract_risk_terms(issue) or _issue_has_dependency_terms(issue) or _issue_has_notation_regime_terms(issue)):
        reasons = ["open medium issue with recheck-risk wording/tags"]
    else:
        return []
    risk_terms = _extract_risk_terms(issue)
    if risk_terms:
        reasons.append("risk terms: " + ", ".join(risk_terms))
    return reasons


def _group_selection_reasons(issue: dict[str, Any]) -> list[str]:
    if not _issue_is_open(issue):
        return []
    severity = _issue_severity(issue)
    if severity in {"critical", "high"}:
        reasons = [f"open {severity} issue"]
    elif severity == "medium" and (_issue_has_dependency_terms(issue) or _issue_has_reference_terms(issue)):
        reasons = ["open medium issue with dependency/reference wording"]
    else:
        return []
    if _issue_has_dependency_terms(issue):
        reasons.append("dependency/downstream wording")
    if _issue_has_reference_terms(issue):
        reasons.append("reference/circular-citation wording")
    return reasons


def _verification_result_paths(workdir: Path, verification_state: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for item in verification_state.get("results", []) or []:
        raw = item.get("result_path")
        if raw:
            paths.append(Path(str(raw)))
    results_dir = workdir / "verification_results"
    if results_dir.exists():
        paths.extend(sorted(results_dir.glob("*.result.json")))
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        if not path.is_absolute():
            path = workdir / path
        key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _load_verification_results(workdir: Path, verification_state: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Path]]:
    results: list[dict[str, Any]] = []
    source_paths: list[Path] = []
    seen: set[str] = set()
    for path in _verification_result_paths(workdir, verification_state):
        if not path.exists() or not path.is_file():
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        source_paths.append(path)
        payload = _load_json(path, default={})
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.setdefault("result_path", str(path))
            results.append(payload)
    return results, source_paths


def _script_excerpt(path_text: Any, limit: int = 500) -> str:
    if not path_text:
        return ""
    path = Path(str(path_text))
    if not path.exists() or not path.is_file():
        return ""
    try:
        return _short_text(path.read_text(encoding="utf-8"), limit)
    except OSError:
        return ""


def _verification_failure_candidates(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for raw_result in results:
        result = _normalize_verification_result(raw_result)
        status = str(result.get("execution_status") or "").strip().lower()
        if not verification_result_needs_technical_retry(result):
            continue
        script_name = str(result.get("script_name") or Path(str(result.get("script_path") or "")).name)
        chunk_id = str(result.get("chunk_id") or "")
        candidates.append(
            {
                "candidate_id": "",
                "category": "verification_failure",
                "secondary_categories": [],
                "secondary_reasons": [],
                "item_type": "verification_script",
                "recommended_action_kind": "script_recheck",
                "source_ids": [item for item in [script_name, chunk_id, str(result.get("result_path") or "")] if item],
                "trigger_reason": f"verification execution status is {status}",
                "recommended_action": "Use the dedicated evidence-rich technical script repair first; run a full chunk re-audit only if repair is unavailable, inconclusive, or reveals a broader audit problem.",
                "priority": "high" if status in {"parse_error", "runtime_error", "unsafe"} else "medium",
                "status": "candidate",
                "context_refs": {
                    "chunk_id": chunk_id,
                    "script_name": script_name,
                    "script_path": result.get("script_path"),
                    "result_path": result.get("result_path"),
                },
                "estimated_cost_band": "low for script/claim review; medium if escalated to chunk rerun",
                "requires_user_confirmation": True,
                "evidence_summary": {
                    "returncode": result.get("returncode"),
                    "execution_status": result.get("execution_status"),
                    "mathematical_outcome": result.get("mathematical_outcome"),
                    "conclusion": _short_text(result.get("conclusion"), 500),
                    "stdout_excerpt": _short_text(result.get("stdout"), 700),
                    "stderr_excerpt": _short_text(result.get("stderr"), 700),
                    "script_excerpt": _script_excerpt(result.get("script_path"), 700),
                },
                "outcome_ref": None,
            }
        )
    return candidates


def _issue_candidate_payload(
    category: str,
    issue_candidate: dict[str, Any],
    recommended_action: str,
    trigger_reason: str,
    priority: str,
    recommended_action_kind: str,
    estimated_cost_band: str = "low",
) -> dict[str, Any]:
    issue_id = str(issue_candidate.get("issue_id") or "")
    chunk_id = str(issue_candidate.get("chunk_id") or "")
    return {
        "candidate_id": "",
        "category": category,
        "secondary_categories": [],
        "secondary_reasons": [],
        "item_type": "issue",
        "recommended_action_kind": recommended_action_kind,
        "source_ids": [item for item in [issue_id, chunk_id] if item],
        "trigger_reason": trigger_reason,
        "recommended_action": recommended_action,
        "priority": priority,
        "status": "candidate",
        "context_refs": {
            "issue_id": issue_id,
            "chunk_id": chunk_id,
            "chunk_index": issue_candidate.get("chunk_index"),
            "location": issue_candidate.get("location"),
            "tags": issue_candidate.get("tags") or [],
            "features": issue_candidate.get("features") or {},
            "verification": issue_candidate.get("verification") or {},
        },
        "estimated_cost_band": estimated_cost_band,
        "requires_user_confirmation": True,
        "evidence_summary": {
            "title": issue_candidate.get("title"),
            "severity": issue_candidate.get("severity"),
            "status": issue_candidate.get("status"),
            "description": issue_candidate.get("short_description"),
            "proposed_fix": issue_candidate.get("proposed_fix"),
            "selection_reasons": issue_candidate.get("selection_reasons") or [],
            "risk_terms": issue_candidate.get("risk_terms") or [],
            "later_issue_overlap": (issue_candidate.get("evidence") or {}).get("later_issue_snippets", [])[:4],
            "later_context_overlap": (issue_candidate.get("evidence") or {}).get("later_chunk_snippets", [])[:4],
            "ledger_overlap": (issue_candidate.get("evidence") or {}).get("ledger_snippets", [])[:4],
        },
        "outcome_ref": None,
    }


def _prepare_issue_candidates(
    issues: list[dict[str, Any]],
    chunks_by_id: dict[str, dict[str, Any]],
    verification_by_chunk: dict[str, list[dict[str, str]]],
    structured_by_chunk: dict[str, str],
    ledger_items: list[str],
    include_medium: bool,
    max_context_chars: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    recheck_candidates: list[dict[str, Any]] = []
    notation_candidates: list[dict[str, Any]] = []
    group_pool: list[dict[str, Any]] = []

    for issue in issues:
        reasons = _issue_selection_reasons(issue, include_medium=include_medium)
        notation_reasons = _notation_regime_reasons(issue)
        if reasons:
            candidate = _candidate_from_issue(issue, chunks_by_id, verification_by_chunk, reasons)
            candidate["evidence"] = _build_evidence(
                candidate,
                issues,
                chunks_by_id,
                structured_by_chunk,
                ledger_items,
                max_context_chars=max_context_chars,
            )
            severity = str(candidate.get("severity") or "unknown")
            priority = "high" if severity in {"critical", "high"} else "medium"
            recheck_candidates.append(
                _issue_candidate_payload(
                    "high_critical_issue_recheck",
                    candidate,
                    "Run an issue-level recheck with source chunk, nearby/later context, ledger notes, and verification references; do not rerun the whole chunk by default.",
                    "; ".join(reasons),
                    priority,
                    "issue_recheck",
                    estimated_cost_band="low",
                )
            )
            if notation_reasons:
                recheck_candidates[-1]["secondary_categories"].append("notation_regime_clarification")
                recheck_candidates[-1]["secondary_reasons"].extend(notation_reasons)
                recheck_candidates[-1]["evidence_summary"]["secondary_reasons"] = notation_reasons
        elif notation_reasons:
            candidate = _candidate_from_issue(issue, chunks_by_id, verification_by_chunk, notation_reasons)
            candidate["evidence"] = _build_evidence(
                candidate,
                issues,
                chunks_by_id,
                structured_by_chunk,
                ledger_items,
                max_context_chars=max_context_chars,
            )
            notation_candidates.append(
                _issue_candidate_payload(
                    "notation_regime_clarification",
                    candidate,
                    "Use human/issue-level review against later notation, regime, domain, or quantifier context; do not rerun a chunk unless the review shows regenerated output is needed.",
                    "explicit notation/regime ambiguity: " + ", ".join(notation_reasons),
                    "medium",
                    "issue_recheck",
                    estimated_cost_band="low",
                )
            )

        group_reasons = _group_selection_reasons(issue)
        if group_reasons:
            group_pool.append(_candidate_from_issue(issue, chunks_by_id, verification_by_chunk, group_reasons))

    groups, links = _strict_dependency_groups(group_pool)
    dependency_candidates = []
    for group in groups:
        members = group.get("members") or []
        source_ids = [str(member.get("issue_id") or "") for member in members if member.get("issue_id")]
        severities = {str(member.get("severity") or "") for member in members}
        priority = "high" if severities & {"critical", "high"} else "medium"
        dependency_candidates.append(
            {
                "candidate_id": "",
                "category": "dependency_propagation",
                "secondary_categories": [],
                "secondary_reasons": [],
                "item_type": "dependency_group",
                "recommended_action_kind": "dependency_group_review",
                "source_ids": source_ids,
                "trigger_reason": "tentative dependency/downstream group from shared labels, symbols, or dependency wording",
                "recommended_action": "Inspect whether downstream issues should be grouped under an upstream cause or rechecked issue-by-issue; do not suppress issues automatically.",
                "priority": priority,
                "status": "candidate",
                "context_refs": {
                    "group_id": group.get("group_id"),
                    "upstream_issue_id": group.get("upstream_issue_id"),
                    "members": members,
                    "links": group.get("links") or [],
                },
                "estimated_cost_band": "low for grouping review; low/medium if issue-level rechecks are requested",
                "requires_user_confirmation": True,
                "evidence_summary": {
                    "classification": group.get("classification"),
                    "link_reasons": group.get("link_reasons") or [],
                    "shared_features": group.get("shared_features") or [],
                    "members": members,
                },
                "outcome_ref": None,
            }
        )

    return recheck_candidates, notation_candidates, dependency_candidates + [{"_group_payload": group} for group in groups]


def _chunk_success_time(chunks_by_id: dict[str, dict[str, Any]], chunk_id: str) -> datetime | None:
    chunk = chunks_by_id.get(chunk_id) or {}
    return _parse_time(chunk.get("time"))


def _failure_is_still_active(chunks_by_id: dict[str, dict[str, Any]], chunk_id: str, failure_time: Any) -> bool:
    success_time = _chunk_success_time(chunks_by_id, chunk_id)
    parsed_failure = _parse_time(failure_time)
    if success_time is None:
        return True
    if parsed_failure is None:
        return False
    return parsed_failure > success_time


def _technical_failure_candidates(workdir: Path, chunks_by_id: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[Path]]:
    candidates: list[dict[str, Any]] = []
    source_paths: list[Path] = []
    seen: set[tuple[str, str, str]] = set()
    logs_dir = workdir / "logs"
    for name in TECHNICAL_FAILURE_LOGS:
        path = logs_dir / name
        if path.exists():
            source_paths.append(path)
        for entry in _read_jsonl_dicts(path):
            action = str(entry.get("action") or "").strip()
            if name == "file_download_timeout_auto_retries.jsonl" and action not in {"giving_up", "scheduled"}:
                continue
            if name == "file_download_timeout_auto_retries.jsonl" and action == "scheduled":
                # Scheduled retries are historical unless the chunk still lacks a later successful record.
                pass
            chunk_id = str(entry.get("chunk_id") or "")
            if not chunk_id and isinstance(entry.get("chunk_ids"), list) and entry["chunk_ids"]:
                chunk_id = str(entry["chunk_ids"][0])
            if not chunk_id:
                continue
            if not _failure_is_still_active(chunks_by_id, chunk_id, entry.get("time")):
                continue
            response_id = str(entry.get("response_id") or "")
            key = (name, chunk_id, response_id or str(entry.get("time") or ""))
            if key in seen:
                continue
            seen.add(key)
            error = entry.get("error") if isinstance(entry.get("error"), dict) else {}
            code = str(error.get("code") or entry.get("retryable_reason") or entry.get("status") or action or "technical_failure")
            priority = "high" if code in {"context_length_exceeded", "failed", "parse_failure", "schema_failure"} else "medium"
            candidates.append(
                {
                    "candidate_id": "",
                    "category": "technical_failure_recovery",
                    "secondary_categories": [],
                    "secondary_reasons": [],
                    "item_type": "technical_failure",
                    "recommended_action_kind": "technical_retry",
                    "source_ids": [item for item in [chunk_id, response_id, name] if item],
                    "trigger_reason": f"{name}: {code}",
                    "recommended_action": "Use technical chunk retry/rerun recovery; this is regeneration of a valid chunk audit, not mathematical re-evaluation.",
                    "priority": priority,
                    "status": "candidate",
                    "context_refs": {
                        "chunk_id": chunk_id,
                        "chunk_index": entry.get("chunk_index"),
                        "response_id": response_id,
                        "log_path": str(path),
                        "request_path": entry.get("request_path"),
                        "raw_response_path": entry.get("raw_response_path"),
                        "failure_summary_path": entry.get("failure_summary_path"),
                    },
                    "estimated_cost_band": "medium",
                    "requires_user_confirmation": True,
                    "evidence_summary": {
                        "status": entry.get("status"),
                        "action": action,
                        "error": error,
                        "retryable": entry.get("retryable"),
                        "retryable_reason": entry.get("retryable_reason"),
                        "note": _short_text(entry.get("note") or entry.get("message"), 500),
                    },
                    "outcome_ref": None,
                }
            )

    for path in sorted((workdir / "responses").glob("*.failure.json")):
        source_paths.append(path)
        entry = _load_json(path, default={})
        if not isinstance(entry, dict):
            continue
        chunk_id = str(entry.get("chunk_id") or "")
        if not chunk_id or not _failure_is_still_active(chunks_by_id, chunk_id, entry.get("time")):
            continue
        response_id = str(entry.get("response_id") or "")
        key = ("responses/*.failure.json", chunk_id, response_id or str(path))
        if key in seen:
            continue
        seen.add(key)
        error = entry.get("error") if isinstance(entry.get("error"), dict) else {}
        code = str(error.get("code") or entry.get("retryable_reason") or entry.get("status") or "technical_failure")
        candidates.append(
            {
                "candidate_id": "",
                "category": "technical_failure_recovery",
                "secondary_categories": [],
                "secondary_reasons": [],
                "item_type": "technical_failure",
                "recommended_action_kind": "technical_retry",
                "source_ids": [item for item in [chunk_id, response_id, path.name] if item],
                "trigger_reason": f"failure response sidecar: {code}",
                "recommended_action": "Use technical chunk retry/rerun recovery; this is regeneration of a valid chunk audit, not mathematical re-evaluation.",
                "priority": "high" if code == "context_length_exceeded" else "medium",
                "status": "candidate",
                "context_refs": {
                    "chunk_id": chunk_id,
                    "chunk_index": entry.get("chunk_index"),
                    "response_id": response_id,
                    "failure_summary_path": str(path),
                    "request_path": entry.get("request_path"),
                    "raw_response_path": entry.get("raw_response_path"),
                },
                "estimated_cost_band": "medium",
                "requires_user_confirmation": True,
                "evidence_summary": {
                    "status": entry.get("status"),
                    "error": error,
                    "retryable": entry.get("retryable"),
                    "retryable_reason": entry.get("retryable_reason"),
                    "note": _short_text(entry.get("note"), 500),
                },
                "outcome_ref": None,
            }
        )
    return candidates, source_paths


def _category_definitions() -> dict[str, dict[str, str]]:
    return {
        "verification_failure": {
            "item_type": "verification_script",
            "recommended_action_kind": "script_recheck",
            "recommended_action": "generate and review a technical script repair first; rerun chunk only as fallback",
        },
        "high_critical_issue_recheck": {
            "item_type": "issue",
            "recommended_action_kind": "issue_recheck",
            "recommended_action": "issue-level recheck, not full chunk rerun by default",
        },
        "dependency_propagation": {
            "item_type": "dependency_group",
            "recommended_action_kind": "dependency_group_review",
            "recommended_action": "group downstream consequences under upstream causes or recheck issue-level links",
        },
        "notation_regime_clarification": {
            "item_type": "issue",
            "recommended_action_kind": "issue_recheck",
            "recommended_action": "recheck against later notation/regime/domain context",
        },
        "technical_failure_recovery": {
            "item_type": "technical_failure",
            "recommended_action_kind": "technical_retry",
            "recommended_action": "full chunk retry/rerun only to regenerate valid output",
        },
        "manual_user_selected": {
            "item_type": "chunk",
            "recommended_action_kind": "chunk_rerun",
            "recommended_action": "placeholder for explicit user-selected low-level chunk reruns",
        },
    }


def _secondary_category_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for candidate in candidates:
        for category in candidate.get("secondary_categories") or []:
            counts[str(category)] += 1
    for category in _category_definitions():
        counts.setdefault(category, 0)
    return dict(counts)


def _action_kind_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(candidate.get("recommended_action_kind") or "human_review") for candidate in candidates)
    for action_kind in ACTION_KIND_LABELS:
        counts.setdefault(action_kind, 0)
    return dict(counts)


def _candidate_type_summary(candidates: list[dict[str, Any]]) -> dict[str, int]:
    action_counts = _action_kind_counts(candidates)
    secondary_counts = _secondary_category_counts(candidates)
    category_counts = Counter(str(candidate.get("category") or "unknown") for candidate in candidates)
    return {
        "full_chunk_rerun_candidates": action_counts.get("chunk_rerun", 0),
        "issue_level_recheck_candidates": action_counts.get("issue_recheck", 0),
        "dependency_grouping_candidates": action_counts.get("dependency_group_review", 0),
        "verification_script_claim_recheck_candidates": action_counts.get("script_recheck", 0),
        "technical_recovery_candidates": action_counts.get("technical_retry", 0),
        "notation_regime_clarification_candidates": category_counts.get("notation_regime_clarification", 0)
        + secondary_counts.get("notation_regime_clarification", 0),
    }


def _markdown_report(manifest: dict[str, Any]) -> str:
    lines = [
        "# Rerun / Recheck Candidates",
        "",
        "This deterministic preparation pass does not call the API, run verification, rerun chunks, close issues, or mutate audit state.",
        "",
        "Candidate for review/recheck does not imply full chunk rerun.",
        "",
        "## Summary",
        f"- Source audit: {manifest['audit_workdir']}",
        f"- Candidates: {len(manifest.get('candidates') or [])}",
        f"- Groups: {len(manifest.get('groups') or [])}",
        f"- Source unmodified by script: {manifest.get('source_unmodified_by_script')}",
        "",
        "## Candidate Type Summary",
    ]
    type_summary = manifest.get("candidate_type_summary") or {}
    for key, label in [
        ("full_chunk_rerun_candidates", "Full chunk rerun candidates"),
        ("issue_level_recheck_candidates", "Issue-level recheck candidates"),
        ("dependency_grouping_candidates", "Dependency grouping candidates"),
        ("verification_script_claim_recheck_candidates", "Verification script/claim recheck candidates"),
        ("technical_recovery_candidates", "Technical recovery candidates"),
        ("notation_regime_clarification_candidates", "Notation/regime clarification candidates"),
    ]:
        lines.append(f"- {label}: {type_summary.get(key, 0)}")

    lines.extend([
        "",
        "## Recommended Action Kind Counts",
    ])
    action_counts = manifest.get("recommended_action_kind_counts") or {}
    for action_kind, label in ACTION_KIND_LABELS.items():
        lines.append(f"- {label}: {action_counts.get(action_kind, 0)}")

    lines.extend([
        "",
        "## Category Counts",
    ])
    for category in _category_definitions():
        lines.append(f"- {category}: {manifest.get('category_counts', {}).get(category, 0)}")
    secondary_counts = manifest.get("secondary_category_counts") or {}
    if any(secondary_counts.get(category, 0) for category in _category_definitions()):
        lines.extend(["", "## Secondary Category Counts"])
        for category in _category_definitions():
            lines.append(f"- {category}: {secondary_counts.get(category, 0)}")

    lines.extend(["", "## Dependency Groups"])
    groups = manifest.get("groups") or []
    if not groups:
        lines.append("- none")
    for group in groups:
        lines.append("")
        lines.append(f"### {group.get('group_id')} upstream candidate: {group.get('upstream_issue_id')}")
        lines.append(f"- Link reasons: {', '.join(group.get('link_reasons') or []) or 'shared features'}")
        lines.append(f"- Shared features: {', '.join(group.get('shared_features') or []) or 'n/a'}")
        for member in group.get("members") or []:
            lines.append(
                f"- {member.get('issue_id')} | {member.get('severity')} | {member.get('role')} | "
                f"{member.get('chunk_id')} | {member.get('title')}"
            )

    lines.extend(["", "## Candidate Details"])
    candidates = manifest.get("candidates") or []
    if not candidates:
        lines.append("- none")
    for candidate in candidates:
        lines.append("")
        lines.append(f"### {candidate.get('candidate_id')} — {candidate.get('category')} [{candidate.get('priority')}]")
        lines.append(f"- Recommended action kind: {candidate.get('recommended_action_kind')}")
        lines.append(f"- Item type: {candidate.get('item_type')}")
        if candidate.get("secondary_categories"):
            lines.append(f"- Secondary categories: {', '.join(str(item) for item in candidate.get('secondary_categories') or [])}")
        lines.append(f"- Source ids: {', '.join(str(item) for item in candidate.get('source_ids') or []) or 'none'}")
        lines.append(f"- Trigger: {candidate.get('trigger_reason')}")
        lines.append(f"- Recommended action: {candidate.get('recommended_action')}")
        lines.append(f"- Estimated cost band: {candidate.get('estimated_cost_band')}")
        summary = candidate.get("evidence_summary") or {}
        if summary.get("title"):
            lines.append(f"- Title: {summary.get('title')}")
        if summary.get("severity"):
            lines.append(f"- Severity: {summary.get('severity')}")
        if summary.get("description"):
            lines.append(f"- Description: {summary.get('description')}")
        if summary.get("conclusion"):
            lines.append(f"- Verification conclusion: {summary.get('conclusion')}")
        if summary.get("note"):
            lines.append(f"- Note: {summary.get('note')}")

    if manifest.get("warnings"):
        lines.extend(["", "## Warnings"])
        for warning in manifest["warnings"]:
            lines.append(f"- {warning}")
    lines.append("")
    return "\n".join(lines)


def prepare_rerun_recheck_candidates(
    audit_workdir: Path,
    output_dir: Path,
    *,
    include_medium: bool = False,
    max_context_chars: int = 2200,
    allow_output_inside_audit: bool = False,
) -> dict[str, Any]:
    audit_workdir = audit_workdir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not audit_workdir.exists():
        raise RuntimeError(f"Audit workdir does not exist: {audit_workdir}")
    if not allow_output_inside_audit and (output_dir == audit_workdir or audit_workdir in output_dir.parents):
        raise RuntimeError("Output directory must not be inside the source audit workdir.")

    state_dir = audit_workdir / "state"
    issues_path = state_dir / "issues.json"
    chunks_path = state_dir / "chunks.jsonl"
    manifest_path = state_dir / "chunk_manifest.json"
    ledger_path = state_dir / "ledger.json"
    verification_path = state_dir / "verification.json"

    issues = _load_issues(issues_path)
    chunks_by_id = _load_chunks(audit_workdir)
    ledger = _load_json(ledger_path, default={})
    ledger_items = _flatten_ledger_items(ledger)
    verification_state = _load_json(verification_path, default={})
    if not isinstance(verification_state, dict):
        verification_state = {}
    verification_results, verification_result_paths = _load_verification_results(audit_workdir, verification_state)
    verification_by_chunk, verification_index_paths = _verification_index(audit_workdir)
    structured_by_chunk, structured_paths, structured_warnings = _collect_structured_summaries(audit_workdir, chunks_by_id)

    technical_candidates, technical_source_paths = _technical_failure_candidates(audit_workdir, chunks_by_id)
    verification_candidates = _verification_failure_candidates(verification_results)
    issue_candidates, notation_candidates, dependency_and_groups = _prepare_issue_candidates(
        issues,
        chunks_by_id,
        verification_by_chunk,
        structured_by_chunk,
        ledger_items,
        include_medium=include_medium,
        max_context_chars=max_context_chars,
    )
    groups = [item["_group_payload"] for item in dependency_and_groups if "_group_payload" in item]
    dependency_candidates = [item for item in dependency_and_groups if "_group_payload" not in item]

    candidates = [
        *verification_candidates,
        *issue_candidates,
        *dependency_candidates,
        *notation_candidates,
        *technical_candidates,
    ]
    _assign_candidate_ids(candidates)
    category_counts = dict(Counter(str(candidate.get("category") or "unknown") for candidate in candidates))
    for category in _category_definitions():
        category_counts.setdefault(category, 0)
    secondary_category_counts = _secondary_category_counts(candidates)
    recommended_action_kind_counts = _action_kind_counts(candidates)
    candidate_type_summary = _candidate_type_summary(candidates)

    source_paths = [
        issues_path,
        chunks_path,
        manifest_path,
        ledger_path,
        verification_path,
        *verification_result_paths,
        *verification_index_paths,
        *structured_paths,
        *technical_source_paths,
    ]
    # Add rerun logs even when they only document resolved history.
    logs_dir = audit_workdir / "logs"
    for name in ("selected_chunk_reruns.jsonl", "failed_verification_chunk_reruns.jsonl"):
        path = logs_dir / name
        if path.exists():
            source_paths.append(path)
    before = _snapshot_paths(source_paths)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "audit_workdir": str(audit_workdir),
        "output_dir": str(output_dir),
        "source_mutation_policy": (
            "read-only; canonical audit state is never modified"
            if allow_output_inside_audit
            else "read-only; source audit folder is never written"
        ),
        "source_fingerprint": _source_fingerprint(source_paths),
        "category_definitions": _category_definitions(),
        "selection": {
            "include_medium": bool(include_medium),
            "max_context_chars": int(max_context_chars),
            "note": "Candidates are triage suggestions only; all audit/model issues remain provisional until reviewed.",
        },
        "category_counts": category_counts,
        "secondary_category_counts": secondary_category_counts,
        "recommended_action_kind_counts": recommended_action_kind_counts,
        "candidate_type_summary": candidate_type_summary,
        "candidate_count": len(candidates),
        "group_count": len(groups),
        "candidates": candidates,
        "groups": groups,
        "warnings": structured_warnings,
        "source_unmodified_by_script": None,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "rerun_recheck_candidates.json", manifest)
    (output_dir / "rerun_recheck_candidates.md").write_text(_markdown_report(manifest), encoding="utf-8")

    after = _snapshot_paths(source_paths)
    manifest["source_unmodified_by_script"] = before == after
    _write_json(output_dir / "rerun_recheck_candidates.json", manifest)
    (output_dir / "rerun_recheck_candidates.md").write_text(_markdown_report(manifest), encoding="utf-8")
    return manifest


def _parse_chunks(text: str) -> list[str]:
    return [item.strip() for item in str(text or "").split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare categorized rerun/recheck candidates from saved audit artifacts without mutating the audit folder."
    )
    parser.add_argument("--audit-workdir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--include-medium", action="store_true")
    parser.add_argument("--max-context-chars", type=int, default=2200)
    args = parser.parse_args(argv)

    manifest = prepare_rerun_recheck_candidates(
        args.audit_workdir,
        args.output_dir,
        include_medium=bool(args.include_medium),
        max_context_chars=int(args.max_context_chars),
    )
    print("Rerun/recheck candidates prepared.")
    print(f"  Source audit: {manifest['audit_workdir']}")
    print(f"  Output dir: {manifest['output_dir']}")
    print(f"  Candidates: {manifest['candidate_count']}")
    print(f"  Groups: {manifest['group_count']}")
    for key, label in [
        ("full_chunk_rerun_candidates", "Full chunk rerun candidates"),
        ("issue_level_recheck_candidates", "Issue-level recheck candidates"),
        ("dependency_grouping_candidates", "Dependency grouping candidates"),
        ("verification_script_claim_recheck_candidates", "Verification script/claim recheck candidates"),
        ("technical_recovery_candidates", "Technical recovery candidates"),
        ("notation_regime_clarification_candidates", "Notation/regime clarification candidates"),
    ]:
        print(f"  {label}: {manifest['candidate_type_summary'].get(key, 0)}")
    for category in _category_definitions():
        print(f"  {category}: {manifest['category_counts'].get(category, 0)}")
    print(f"  Source unmodified by script: {manifest['source_unmodified_by_script']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
