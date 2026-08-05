from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from audit_state import append_jsonl, load_json, save_json, session_paths, utc_now


SCHEMA_VERSION = 1
LIFECYCLE_STATES = {
    "suspected",
    "verification_pending",
    "supported",
    "verified",
    "rejected",
    "resolved",
    "duplicate",
    "superseded",
    "external_verification_required",
}
LIFECYCLE_SEMANTICS = {
    "suspected": "Initial candidate not yet adjudicated.",
    "verification_pending": "Evidence or computational/adversarial checking is still required.",
    "supported": "Evidence supports a concern, but its final consequence or repair is unsettled.",
    "verified": "The defect and its consequence have been checked for report admission.",
    "rejected": "The objection did not survive full-context or independent checking.",
    "resolved": "A genuine issue has a verified repair or clarification while provenance is retained.",
    "duplicate": "The record is not an independent issue and belongs with another cluster.",
    "superseded": "A later accepted formulation replaces this cluster formulation.",
    "external_verification_required": "A cited external source is required before adjudication.",
}
RELATION_TYPES = {
    "duplicate_of",
    "same_root_cause_as",
    "downstream_consequence_of",
    "supersedes",
    "partially_overlaps",
    "contradicts",
    "independent_of",
}
VERIFICATION_STATUSES = {
    "not_checked", "pending", "supported", "verified", "contradicted", "inconclusive", "not_applicable"
}
REPAIR_STATUSES = {
    "not_assessed", "not_repaired", "repair_proposed", "verified_local_repair",
    "clarification_required", "not_applicable",
}
SEVERITIES = {"none", "unknown", "low", "medium", "high", "critical"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}
SERIOUS_RECHECK_STATUSES = {"not_required", "pending", "accepted"}
DECISION_TYPES = {"upsert", "merge", "split", "reconcile"}
REPORT_READINESS_STATES = {
    "raw_only", "clustering_incomplete", "adjudication_incomplete",
    "serious_issues_pending", "canonical_report_ready",
}
FINAL_LIFECYCLE_STATES = {"verified", "rejected", "resolved", "duplicate"}
HEADLINE_LIFECYCLE_STATES = {
    "suspected", "verification_pending", "supported", "verified", "resolved",
    "external_verification_required",
}
_CLUSTER_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]{2,63}$")
_MEANINGFUL_UPSTREAM_FIELDS = {
    "lifecycle_state", "verification_status", "final_severity", "repair_status", "raw_issue_ids"
}


def _workdir(value: dict[str, Any] | str | Path) -> Path:
    if isinstance(value, dict):
        value = value.get("workdir") or ""
    return Path(value).expanduser().resolve()


def canonical_cluster_paths(value: dict[str, Any] | str | Path) -> dict[str, Path]:
    root = _workdir(value)
    return {
        "snapshot": root / "state" / "canonical_issue_clusters.json",
        "events": root / "state" / "issue_cluster_events.jsonl",
        "decisions_log": root / "logs" / "issue_cluster_decisions.jsonl",
    }


def _json_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _string(value: Any, *, required: bool = False, field: str = "value") -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{field} must be a non-empty string")
    return text


def _string_list(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    out: list[str] = []
    for index, item in enumerate(value):
        text = _string(item, required=True, field=f"{field}[{index}]")
        if text not in out:
            out.append(text)
    return out


def _choice(value: Any, choices: set[str], *, field: str, default: str) -> str:
    text = _string(value or default).lower().replace("-", "_")
    if text not in choices:
        raise ValueError(f"{field} must be one of {sorted(choices)}; got {text!r}")
    return text


def load_raw_issue_state(value: dict[str, Any] | str | Path) -> dict[str, Any]:
    path = session_paths(_workdir(value))["issues"]
    if not path.exists():
        return {"next_issue_id": 1, "issues": [], "updated_at": None}
    payload = load_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("issues"), list):
        raise ValueError(f"Raw issue state is malformed: {path}")
    return payload


def raw_issue_view(value: dict[str, Any] | str | Path) -> dict[str, Any]:
    state = load_raw_issue_state(value)
    return {"source": "state/issues.json", "raw_issue_count": len(state.get("issues") or []), "issues_state": deepcopy(state)}


def _raw_issue_map(value: dict[str, Any] | str | Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for issue in load_raw_issue_state(value).get("issues") or []:
        if isinstance(issue, dict) and _string(issue.get("issue_id")):
            result[_string(issue["issue_id"])] = issue
    return result


def _normalize_location(value: Any) -> dict[str, str]:
    value = value if isinstance(value, dict) else {}
    return {
        "chunk_id": _string(value.get("chunk_id")),
        "page": _string(value.get("page")),
        "source_label": _string(value.get("source_label")),
        "printed_label": _string(value.get("printed_label")),
        "detail": _string(value.get("detail")),
    }


def _normalize_relations(value: Any, raw_issue_ids: set[str], known_issue_ids: set[str]) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("relations must be a list")
    out: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"relations[{index}] must be an object")
        issue_id = _string(item.get("issue_id"), required=True, field=f"relations[{index}].issue_id")
        if issue_id not in raw_issue_ids:
            raise ValueError(f"relations[{index}].issue_id {issue_id!r} is not a cluster member")
        relation = _choice(item.get("relation"), RELATION_TYPES, field=f"relations[{index}].relation", default="")
        target_issue_id = _string(item.get("target_issue_id"))
        if target_issue_id and target_issue_id not in known_issue_ids:
            raise ValueError(f"relations[{index}].target_issue_id {target_issue_id!r} does not exist")
        target_cluster_id = _string(item.get("target_cluster_id"))
        if target_cluster_id and not _CLUSTER_ID_RE.fullmatch(target_cluster_id):
            raise ValueError(f"relations[{index}].target_cluster_id is invalid: {target_cluster_id!r}")
        out.append({
            "issue_id": issue_id,
            "relation": relation,
            "target_issue_id": target_issue_id,
            "target_cluster_id": target_cluster_id,
            "rationale": _string(item.get("rationale")),
        })
    return out


def validate_cluster_decision(decision: Any, known_issues: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(decision, dict):
        raise ValueError("Each cluster decision must be an object")
    cluster_id = _string(decision.get("cluster_id"), required=True, field="cluster_id")
    if not _CLUSTER_ID_RE.fullmatch(cluster_id):
        raise ValueError(f"cluster_id is invalid: {cluster_id!r}")
    raw_issue_ids = _string_list(decision.get("raw_issue_ids"), field="raw_issue_ids")
    if not raw_issue_ids:
        raise ValueError(f"{cluster_id}: raw_issue_ids must not be empty")
    missing = sorted(set(raw_issue_ids) - set(known_issues))
    if missing:
        raise ValueError(f"{cluster_id}: unknown raw issue IDs: {missing}")
    original_severities: list[str] = []
    for issue_id in raw_issue_ids:
        severity = _string(known_issues[issue_id].get("severity") or "unknown").lower()
        if severity not in original_severities:
            original_severities.append(severity)
    final_severity = _choice(decision.get("final_severity"), SEVERITIES, field="final_severity", default="unknown")
    proposed_severity = _choice(
        decision.get("proposed_severity"), SEVERITIES, field="proposed_severity", default=final_severity
    )
    primary_location = _normalize_location(decision.get("primary_location"))
    if not primary_location["chunk_id"]:
        primary_location["chunk_id"] = _string(known_issues[raw_issue_ids[0]].get("chunk_id"))
    if not primary_location["detail"]:
        primary_location["detail"] = _string(known_issues[raw_issue_ids[0]].get("location"))
    report_issue_id = _string(decision.get("report_issue_id")) or raw_issue_ids[0]
    if report_issue_id not in raw_issue_ids:
        raise ValueError(f"{cluster_id}: report_issue_id must be one of raw_issue_ids")
    return {
        "cluster_id": cluster_id,
        "title": _string(decision.get("title"), required=True, field="title"),
        "root_claim": _string(decision.get("root_claim"), required=True, field="root_claim"),
        "primary_location": primary_location,
        "raw_issue_ids": raw_issue_ids,
        "relations": _normalize_relations(decision.get("relations"), set(raw_issue_ids), set(known_issues)),
        "lifecycle_state": _choice(decision.get("lifecycle_state"), LIFECYCLE_STATES, field="lifecycle_state", default="suspected"),
        "verification_status": _choice(
            decision.get("verification_status"), VERIFICATION_STATUSES, field="verification_status", default="not_checked"
        ),
        "proposed_severity": proposed_severity,
        "final_severity": final_severity,
        "original_severities": original_severities,
        "repair_status": _choice(decision.get("repair_status"), REPAIR_STATUSES, field="repair_status", default="not_assessed"),
        "upstream_cluster_ids": _string_list(decision.get("upstream_cluster_ids"), field="upstream_cluster_ids"),
        "downstream_cluster_ids": _string_list(decision.get("downstream_cluster_ids"), field="downstream_cluster_ids"),
        "dependent_cluster_ids": _string_list(decision.get("dependent_cluster_ids"), field="dependent_cluster_ids"),
        "external_source_requirements": _string_list(
            decision.get("external_source_requirements"), field="external_source_requirements"
        ),
        "decisive_evidence": _string_list(decision.get("decisive_evidence"), field="decisive_evidence"),
        "limitations": _string_list(decision.get("limitations"), field="limitations"),
        "confidence": _choice(decision.get("confidence"), CONFIDENCE_LEVELS, field="confidence", default="medium"),
        "serious_issue_recheck_status": _choice(
            decision.get("serious_issue_recheck_status"), SERIOUS_RECHECK_STATUSES,
            field="serious_issue_recheck_status", default="not_required"
        ),
        "include_in_concise_report": bool(decision.get("include_in_concise_report", False)),
        "report_issue_id": report_issue_id,
        "decision_type": _choice(decision.get("decision_type"), DECISION_TYPES, field="decision_type", default="upsert"),
        "supersedes_cluster_ids": _string_list(decision.get("supersedes_cluster_ids"), field="supersedes_cluster_ids"),
        "clear_reconciliation": bool(decision.get("clear_reconciliation", False)),
        "allow_shared_raw_issue_ids": bool(decision.get("allow_shared_raw_issue_ids", False)),
        "human_note": _string(decision.get("human_note")),
    }


def read_issue_cluster_events(value: dict[str, Any] | str | Path) -> dict[str, Any]:
    path = canonical_cluster_paths(value)["events"]
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not path.exists():
        return {"path": str(path), "events": events, "warnings": warnings}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except Exception as exc:
            warnings.append(f"Malformed cluster event line {line_number}: {exc}")
            continue
        if not isinstance(event, dict) or not _string(event.get("event_id")):
            warnings.append(f"Malformed cluster event line {line_number}: object/event_id required")
            continue
        events.append(event)
    return {"path": str(path), "events": events, "warnings": warnings}


def issue_cluster_history(value: dict[str, Any] | str | Path) -> dict[str, Any]:
    loaded = read_issue_cluster_events(value)
    return {
        "schema_version": SCHEMA_VERSION,
        "event_count": len(loaded["events"]),
        "events": deepcopy(loaded["events"]),
        "warnings": list(loaded["warnings"]),
    }


def _reduce_cluster_events(events: Iterable[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    current: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for event in events:
        event_type = _string(event.get("event_type"))
        cluster_id = _string(event.get("cluster_id"))
        if not cluster_id:
            warnings.append(f"Event {event.get('event_id')} has no cluster_id")
            continue
        if event_type == "cluster_decision":
            cluster = event.get("cluster")
            if not isinstance(cluster, dict):
                warnings.append(f"Event {event.get('event_id')} has no cluster object")
                continue
            previous = current.get(cluster_id, {})
            updated = deepcopy(cluster)
            updated["created_at"] = previous.get("created_at") or event.get("accepted_at")
            updated["updated_at"] = event.get("accepted_at")
            updated["latest_event_id"] = event.get("event_id")
            updated["transition_count"] = int(previous.get("transition_count", 0) or 0) + 1
            if previous.get("reconciliation_required") and not event.get("clear_reconciliation"):
                for key in ("reconciliation_required", "stale_reason", "stale_since", "stale_causes"):
                    updated[key] = deepcopy(previous.get(key))
            current[cluster_id] = updated
            for superseded_id in event.get("supersedes_cluster_ids") or []:
                if superseded_id == cluster_id:
                    continue
                prior = current.get(superseded_id)
                if prior:
                    replacements = list(prior.get("superseded_by_cluster_ids") or [])
                    if cluster_id not in replacements:
                        replacements.append(cluster_id)
                    prior["superseded_by_cluster_ids"] = replacements
                    prior["superseded_by_cluster_id"] = cluster_id
                    prior["superseded_at"] = event.get("accepted_at")
        elif event_type == "reconciliation_required":
            cluster = current.get(cluster_id)
            if not cluster:
                warnings.append(f"Stale event {event.get('event_id')} references unknown cluster {cluster_id}")
                continue
            cluster["reconciliation_required"] = True
            cluster["stale_since"] = event.get("accepted_at")
            cluster["stale_reason"] = _string(event.get("stale_reason"))
            causes = list(cluster.get("stale_causes") or [])
            cause = {
                "cluster_id": _string(event.get("caused_by_cluster_id")),
                "event_id": _string(event.get("caused_by_event_id")),
                "reason": _string(event.get("stale_reason")),
            }
            if cause not in causes:
                causes.append(cause)
            cluster["stale_causes"] = causes
        elif event_type == "reconciliation_cleared":
            cluster = current.get(cluster_id)
            if not cluster:
                warnings.append(f"Reconciliation event {event.get('event_id')} references unknown cluster {cluster_id}")
                continue
            cluster.update({
                "reconciliation_required": False,
                "stale_reason": "",
                "stale_since": None,
                "stale_causes": [],
                "reconciled_at": event.get("accepted_at"),
                "reconciliation_event_id": event.get("event_id"),
            })
        else:
            warnings.append(f"Unknown cluster event type {event_type!r} in {event.get('event_id')}")
    return current, warnings


def _headline_clusters(clusters: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        cluster for cluster in clusters
        if cluster.get("lifecycle_state") in HEADLINE_LIFECYCLE_STATES
        and cluster.get("final_severity") not in {"none", "unknown", ""}
    ]


def report_readiness(
    value: dict[str, Any] | str | Path | None = None,
    *,
    view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if view is None:
        if value is None:
            raise ValueError("value or view is required")
        view = canonical_issue_cluster_view(value, include_readiness=False)
    raw_count = int(view.get("raw_issue_count", 0) or 0)
    clusters = list(view.get("clusters") or [])
    unclustered = list(view.get("unclustered_raw_issue_ids") or [])
    stale = list(view.get("stale_cluster_ids") or [])
    warnings = list(view.get("warnings") or [])
    blockers: list[str] = []
    if raw_count and not view.get("event_count"):
        state = "raw_only"
        blockers.append("Raw issue candidates exist but no accepted canonical cluster decisions exist.")
    elif unclustered:
        state = "clustering_incomplete"
        blockers.append(f"{len(unclustered)} raw issue candidate(s) are not assigned to an accepted cluster.")
    else:
        unsettled = [
            cluster["cluster_id"] for cluster in clusters
            if cluster.get("lifecycle_state") not in FINAL_LIFECYCLE_STATES
        ]
        if stale:
            blockers.append(f"{len(stale)} cluster(s) require dependency reconciliation.")
        if unsettled:
            blockers.append(f"{len(unsettled)} cluster(s) lack a final lifecycle disposition.")
        if warnings:
            blockers.append("Canonical cluster history contains warnings that require review.")
        if stale or unsettled or warnings:
            state = "adjudication_incomplete"
        else:
            serious_pending = [
                cluster["cluster_id"] for cluster in clusters
                if cluster.get("final_severity") in {"high", "critical"}
                and cluster.get("lifecycle_state") in {"verified", "resolved"}
                and cluster.get("serious_issue_recheck_status") != "accepted"
            ]
            if serious_pending:
                state = "serious_issues_pending"
                blockers.append(
                    f"{len(serious_pending)} high/critical cluster(s) lack an accepted serious-issue recheck."
                )
            else:
                state = "canonical_report_ready"
    return {
        "status": state,
        "canonical_report_ready": state == "canonical_report_ready",
        "blockers": blockers,
        "strict_final_consolidated_report_allowed": state == "canonical_report_ready",
    }


def canonical_issue_cluster_view(
    value: dict[str, Any] | str | Path,
    *,
    include_readiness: bool = True,
) -> dict[str, Any]:
    root = _workdir(value)
    raw_state = load_raw_issue_state(root)
    raw_ids = [
        _string(issue.get("issue_id")) for issue in raw_state.get("issues") or []
        if isinstance(issue, dict) and _string(issue.get("issue_id"))
    ]
    loaded = read_issue_cluster_events(root)
    current, reduce_warnings = _reduce_cluster_events(loaded["events"])
    active: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    for cluster_id in sorted(current):
        cluster = deepcopy(current[cluster_id])
        if cluster.get("lifecycle_state") == "superseded" or cluster.get("superseded_by_cluster_id"):
            superseded.append(cluster)
        else:
            active.append(cluster)
    assignments: dict[str, list[str]] = defaultdict(list)
    for cluster in active:
        for issue_id in cluster.get("raw_issue_ids") or []:
            assignments[str(issue_id)].append(cluster["cluster_id"])
    conflicts = {issue_id: ids for issue_id, ids in assignments.items() if len(ids) > 1}
    warnings = list(loaded["warnings"]) + reduce_warnings
    if conflicts:
        warnings.append(f"Conflicting active cluster assignments for {len(conflicts)} raw issue(s).")
    unclustered = [issue_id for issue_id in raw_ids if issue_id not in assignments]
    lifecycle_counts = Counter(str(cluster.get("lifecycle_state") or "unknown") for cluster in active)
    headline = _headline_clusters(active)
    severity_counts = Counter(str(cluster.get("final_severity") or "unknown") for cluster in headline)
    stale_ids = [cluster["cluster_id"] for cluster in active if cluster.get("reconciliation_required")]
    updated_at = ""
    if loaded["events"]:
        updated_at = max(_string(event.get("accepted_at")) for event in loaded["events"])
    view = {
        "schema_version": SCHEMA_VERSION,
        "workdir": str(root),
        "event_count": len(loaded["events"]),
        "updated_at": updated_at or raw_state.get("updated_at"),
        "raw_issue_count": len(raw_ids),
        "clusters": active,
        "superseded_clusters": superseded,
        "accepted_cluster_count": len(active),
        "unclustered_raw_issue_ids": unclustered,
        "conflicting_assignments": conflicts,
        "stale_cluster_ids": stale_ids,
        "warnings": warnings,
        "summary": {
            "raw_issue_count": len(raw_ids),
            "accepted_cluster_count": len(active),
            "superseded_cluster_count": len(superseded),
            "unclustered_raw_issue_count": len(unclustered),
            "stale_reconciliation_count": len(stale_ids),
            "lifecycle_state_counts": dict(sorted(lifecycle_counts.items())),
            "headline_cluster_count": len(headline),
            "headline_severity_counts": dict(sorted(severity_counts.items())),
            "raw_records_consolidated_count": max(0, len(raw_ids) - len(active)),
        },
    }
    view["canonical_counts_applicable"] = bool(active and not unclustered and not conflicts and not warnings)
    if include_readiness:
        view["report_readiness"] = report_readiness(view=view)
    return view


def clusters_requiring_reconciliation(value: dict[str, Any] | str | Path) -> list[dict[str, Any]]:
    return [
        cluster for cluster in canonical_issue_cluster_view(value).get("clusters") or []
        if cluster.get("reconciliation_required")
    ]


def _cluster_changed(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    if not previous:
        return []
    return sorted(
        field for field in _MEANINGFUL_UPSTREAM_FIELDS
        if previous.get(field) != current.get(field)
    )


def _dependent_cluster_ids(clusters: dict[str, dict[str, Any]], cluster_id: str) -> list[str]:
    dependents: set[str] = set()
    source = clusters.get(cluster_id) or {}
    dependents.update(str(item) for item in source.get("downstream_cluster_ids") or [])
    dependents.update(str(item) for item in source.get("dependent_cluster_ids") or [])
    for candidate_id, candidate in clusters.items():
        if cluster_id in set(candidate.get("upstream_cluster_ids") or []):
            dependents.add(candidate_id)
        for relation in candidate.get("relations") or []:
            if (
                isinstance(relation, dict)
                and relation.get("target_cluster_id") == cluster_id
                and relation.get("relation") == "downstream_consequence_of"
            ):
                dependents.add(candidate_id)
    dependents.discard(cluster_id)
    return sorted(dependents)


def _event_id(event_type: str, cluster_id: str, accepted_at: str, ordinal: int) -> str:
    digest = hashlib.sha256(f"{event_type}|{cluster_id}|{accepted_at}|{ordinal}".encode("utf-8")).hexdigest()[:12]
    return f"ICE-{digest}"


def _append_event(path: Path, event: dict[str, Any]) -> None:
    append_jsonl(path, event)


def _snapshot_from_view(view: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": view.get("updated_at"),
        "source": "state/issue_cluster_events.jsonl",
        "event_count": view.get("event_count", 0),
        "report_readiness": view.get("report_readiness", {}),
        "summary": view.get("summary", {}),
        "clusters": view.get("clusters", []),
        "superseded_clusters": view.get("superseded_clusters", []),
        "unclustered_raw_issue_ids": view.get("unclustered_raw_issue_ids", []),
        "conflicting_assignments": view.get("conflicting_assignments", {}),
        "warnings": view.get("warnings", []),
    }


def import_canonical_cluster_decisions(
    value: dict[str, Any] | str | Path,
    payload: Any,
    *,
    accept: bool = False,
    allow_overlap: bool = False,
) -> dict[str, Any]:
    """Validate and optionally append canonical issue-cluster decisions.

    The raw issue ledger is never written. Re-importing an identical accepted
    decision is idempotent; a changed decision appends a new transition event.
    """
    root = _workdir(value)
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
        raise ValueError("Decision payload must be an object containing a decisions list")
    known_issues = _raw_issue_map(root)
    decisions = [validate_cluster_decision(item, known_issues) for item in payload["decisions"]]
    ids = [item["cluster_id"] for item in decisions]
    if len(ids) != len(set(ids)):
        raise ValueError("A decision batch may contain each cluster_id only once")

    prior_loaded = read_issue_cluster_events(root)
    previous, reduce_warnings = _reduce_cluster_events(prior_loaded["events"])
    if reduce_warnings:
        raise ValueError("Existing cluster history is not safe to update: " + "; ".join(reduce_warnings))
    known_cluster_ids = set(previous) | set(ids)
    for decision in decisions:
        for field in ("upstream_cluster_ids", "downstream_cluster_ids", "dependent_cluster_ids", "supersedes_cluster_ids"):
            missing = sorted(set(decision.get(field) or []) - known_cluster_ids)
            if missing:
                raise ValueError(f"{decision['cluster_id']}: unknown {field}: {missing}")
        for relation in decision.get("relations") or []:
            target = relation.get("target_cluster_id")
            if target and target not in known_cluster_ids:
                raise ValueError(f"{decision['cluster_id']}: relation references unknown cluster {target}")

    future = {key: deepcopy(item) for key, item in previous.items()}
    superseded_ids = {item for decision in decisions for item in decision.get("supersedes_cluster_ids") or []}
    for decision in decisions:
        future[decision["cluster_id"]] = deepcopy(decision)
    assignments: dict[str, list[str]] = defaultdict(list)
    for cluster_id, cluster in future.items():
        if cluster_id in superseded_ids or cluster.get("superseded_by_cluster_id") or cluster.get("lifecycle_state") == "superseded":
            continue
        for issue_id in cluster.get("raw_issue_ids") or []:
            assignments[str(issue_id)].append(cluster_id)
    conflicts = {issue_id: values for issue_id, values in assignments.items() if len(values) > 1}
    permitted = all(
        allow_overlap or any(
            decision["cluster_id"] in cluster_ids and decision.get("allow_shared_raw_issue_ids")
            for decision in decisions
        )
        for cluster_ids in conflicts.values()
    )
    if conflicts and not permitted:
        raise ValueError(f"Conflicting active raw-issue assignments: {conflicts}")

    existing_hashes = {
        str(event.get("decision_hash")) for event in prior_loaded["events"]
        if event.get("event_type") == "cluster_decision"
    }
    prepared: list[dict[str, Any]] = []
    skipped: list[str] = []
    accepted_at = utc_now()
    created_by = _string(payload.get("created_by")) or "manual_import"
    for decision in decisions:
        decision_hash = _json_hash(decision)
        if decision_hash in existing_hashes:
            skipped.append(decision["cluster_id"])
            continue
        prepared.append({
            "schema_version": SCHEMA_VERSION,
            "event_id": _event_id("cluster_decision", decision["cluster_id"], accepted_at, len(prepared)),
            "event_type": "cluster_decision",
            "cluster_id": decision["cluster_id"],
            "accepted_at": accepted_at,
            "created_by": created_by,
            "decision_hash": decision_hash,
            "decision_type": decision["decision_type"],
            "supersedes_cluster_ids": decision.get("supersedes_cluster_ids", []),
            "clear_reconciliation": decision.get("clear_reconciliation", False),
            "cluster": decision,
        })

    preview_events = list(prior_loaded["events"])
    stale_events: list[dict[str, Any]] = []
    for event in prepared:
        cluster_id = event["cluster_id"]
        previous_cluster = previous.get(cluster_id)
        changed_fields = _cluster_changed(previous_cluster, event["cluster"])
        if previous_cluster and changed_fields:
            reason = f"Upstream cluster {cluster_id} changed: {', '.join(changed_fields)}"
            for dependent_id in _dependent_cluster_ids(previous, cluster_id):
                stale_events.append({
                    "schema_version": SCHEMA_VERSION,
                    "event_id": _event_id("reconciliation_required", dependent_id, accepted_at, len(stale_events)),
                    "event_type": "reconciliation_required",
                    "cluster_id": dependent_id,
                    "accepted_at": accepted_at,
                    "caused_by_cluster_id": cluster_id,
                    "caused_by_event_id": event["event_id"],
                    "stale_reason": reason,
                })
        for superseded_id in event.get("supersedes_cluster_ids") or []:
            reason = f"Upstream cluster {superseded_id} was superseded by {cluster_id}"
            for dependent_id in _dependent_cluster_ids(previous, superseded_id):
                if dependent_id == cluster_id:
                    continue
                stale_events.append({
                    "schema_version": SCHEMA_VERSION,
                    "event_id": _event_id("reconciliation_required", dependent_id, accepted_at, len(stale_events)),
                    "event_type": "reconciliation_required",
                    "cluster_id": dependent_id,
                    "accepted_at": accepted_at,
                    "caused_by_cluster_id": cluster_id,
                    "caused_by_event_id": event["event_id"],
                    "stale_reason": reason,
                })
        if event.get("clear_reconciliation"):
            stale_events.append({
                "schema_version": SCHEMA_VERSION,
                "event_id": _event_id("reconciliation_cleared", cluster_id, accepted_at, len(stale_events)),
                "event_type": "reconciliation_cleared",
                "cluster_id": cluster_id,
                "accepted_at": accepted_at,
                "caused_by_event_id": event["event_id"],
            })
    preview_events.extend(prepared)
    preview_events.extend(stale_events)
    reduced, preview_warnings = _reduce_cluster_events(preview_events)
    preview = {
        "accepted": bool(accept),
        "dry_run": not accept,
        "prepared_decision_count": len(prepared),
        "skipped_idempotent_cluster_ids": skipped,
        "prepared_events": prepared + stale_events,
        "current_cluster_ids": sorted(reduced),
        "warnings": preview_warnings,
        "raw_issue_mutation": False,
    }
    if not accept:
        return preview

    paths = canonical_cluster_paths(root)
    for event in prepared + stale_events:
        _append_event(paths["events"], event)
    view = canonical_issue_cluster_view(root)
    save_json(paths["snapshot"], _snapshot_from_view(view))
    append_jsonl(paths["decisions_log"], {
        "timestamp": accepted_at,
        "action": "accepted_canonical_issue_cluster_decisions",
        "cluster_ids": [event["cluster_id"] for event in prepared],
        "event_ids": [event["event_id"] for event in prepared + stale_events],
        "raw_issue_mutation": False,
        "snapshot_path": str(paths["snapshot"]),
    })
    ledger_paths = build_canonical_issue_ledger(root)
    preview.update({
        "view": view,
        "snapshot_path": str(paths["snapshot"]),
        "events_path": str(paths["events"]),
        "ledger_paths": ledger_paths,
    })
    return preview


def canonical_concise_issue_records(value: dict[str, Any] | str | Path) -> dict[str, Any]:
    view = canonical_issue_cluster_view(value)
    raw = _raw_issue_map(value)
    reportable: list[dict[str, Any]] = []
    external: list[dict[str, Any]] = []
    excluded: dict[str, str] = {}
    for cluster in view.get("clusters") or []:
        cluster_id = str(cluster.get("cluster_id") or "")
        lifecycle = str(cluster.get("lifecycle_state") or "")
        if cluster.get("reconciliation_required"):
            excluded[cluster_id] = "dependency_reconciliation_required"
            continue
        if lifecycle in {"rejected", "duplicate", "superseded"}:
            excluded[cluster_id] = lifecycle
            continue
        if lifecycle == "external_verification_required":
            external.append(deepcopy(cluster))
            excluded[cluster_id] = lifecycle
            continue
        if (
            cluster.get("final_severity") in {"high", "critical"}
            and cluster.get("serious_issue_recheck_status") != "accepted"
        ):
            excluded[cluster_id] = "serious_issue_recheck_pending"
            continue
        representative_id = str(cluster.get("report_issue_id") or "")
        representative = deepcopy(raw.get(representative_id) or raw.get((cluster.get("raw_issue_ids") or [""])[0]) or {})
        if not representative:
            excluded[cluster_id] = "missing_representative_issue"
            continue
        representative["issue_id"] = representative_id or representative.get("issue_id")
        representative["severity"] = cluster.get("final_severity") or representative.get("severity")
        representative["canonical_cluster_id"] = cluster_id
        representative["canonical_lifecycle_state"] = lifecycle
        representative["canonical_raw_issue_ids"] = list(cluster.get("raw_issue_ids") or [])
        representative["canonical_root_claim"] = cluster.get("root_claim")
        representative["canonical_include_in_concise_report"] = bool(cluster.get("include_in_concise_report"))
        representative["canonical_cluster"] = deepcopy(cluster)
        reportable.append(representative)
    canonical_applied = bool(view.get("canonical_counts_applicable"))
    return {
        "canonical_applied": canonical_applied,
        "issues": reportable,
        "external_verification_required": external if canonical_applied else [],
        "excluded_cluster_reasons": excluded,
        "view": view,
    }


def canonical_issue_status_summary(value: dict[str, Any] | str | Path) -> dict[str, Any]:
    view = canonical_issue_cluster_view(value)
    return {
        "report_readiness": view.get("report_readiness", {}),
        "canonical_counts_applicable": view.get("canonical_counts_applicable", False),
        **deepcopy(view.get("summary") or {}),
    }


def canonical_issue_ledger_markdown(value: dict[str, Any] | str | Path) -> str:
    view = canonical_issue_cluster_view(value)
    readiness = view.get("report_readiness") or {}
    lines = [
        "# Canonical issue ledger",
        "",
        f"- Report readiness: {readiness.get('status', 'raw_only')}",
        f"- Raw candidate records: {view.get('raw_issue_count', 0)}",
        f"- Current canonical clusters: {view.get('accepted_cluster_count', 0)}",
        f"- Unclustered raw candidates: {len(view.get('unclustered_raw_issue_ids') or [])}",
        "",
        "This ledger is derived from append-only accepted decisions. Raw candidates remain in `state/issues.json`.",
        "",
    ]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cluster in view.get("clusters") or []:
        grouped[str(cluster.get("lifecycle_state") or "unknown")].append(cluster)
    if view.get("superseded_clusters"):
        grouped["superseded"].extend(view["superseded_clusters"])
    state_order = [
        "verified", "supported", "resolved", "rejected", "duplicate", "superseded",
        "external_verification_required", "verification_pending", "suspected",
    ]
    for state in state_order + sorted(set(grouped) - set(state_order)):
        if not grouped.get(state):
            continue
        lines.extend([f"## {state.replace('_', ' ').title()}", ""])
        for cluster in grouped[state]:
            location = cluster.get("primary_location") or {}
            location_text = ", ".join(
                str(location.get(key)) for key in ("printed_label", "source_label", "page", "chunk_id", "detail")
                if location.get(key)
            ) or "not recorded"
            lines.extend([
                f"### {cluster.get('cluster_id')} - {cluster.get('title')}",
                f"- Raw issue IDs: {', '.join(cluster.get('raw_issue_ids') or [])}",
                f"- Primary location: {location_text}",
                f"- Final severity: {cluster.get('final_severity')}",
                f"- Root claim: {cluster.get('root_claim')}",
                f"- Verification: {cluster.get('verification_status')}",
                f"- Repair: {cluster.get('repair_status')}",
                f"- Confidence: {cluster.get('confidence')}",
                f"- Downstream clusters: {', '.join(cluster.get('downstream_cluster_ids') or []) or 'none'}",
                f"- Decisive evidence: {'; '.join(cluster.get('decisive_evidence') or []) or 'not recorded'}",
                f"- Limitations: {'; '.join(cluster.get('limitations') or []) or 'none recorded'}",
            ])
            if cluster.get("reconciliation_required"):
                lines.append(f"- Dependency reconciliation required: {cluster.get('stale_reason')}")
            if cluster.get("external_source_requirements"):
                lines.append("- External source requirements: " + "; ".join(cluster["external_source_requirements"]))
            lines.append("")
    stale_clusters = [item for item in view.get("clusters") or [] if item.get("reconciliation_required")]
    if stale_clusters:
        lines.extend(["## Stale clusters requiring reconciliation", ""])
        for cluster in stale_clusters:
            lines.append(f"- {cluster.get('cluster_id')}: {cluster.get('stale_reason') or 'upstream decision changed'}")
        lines.append("")
    if view.get("unclustered_raw_issue_ids"):
        lines.extend(["## Unclustered raw candidates", "", ", ".join(view["unclustered_raw_issue_ids"]), ""])
    return "\n".join(lines).rstrip() + "\n"


def build_canonical_issue_ledger(value: dict[str, Any] | str | Path) -> dict[str, str]:
    root = _workdir(value)
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    stem = "audit"
    session_path = root / "state" / "session.json"
    if session_path.exists():
        try:
            session = load_json(session_path)
            stem = Path(str(session.get("pdf_path") or "audit")).stem or "audit"
        except Exception:
            pass
    json_path = reports / f"{stem}_canonical_issue_ledger.json"
    markdown_path = reports / f"{stem}_canonical_issue_ledger.md"
    view = canonical_issue_cluster_view(root)
    save_json(json_path, {
        "schema_version": SCHEMA_VERSION,
        "generated_at": view.get("updated_at"),
        "raw_issue_source": "state/issues.json",
        "canonical_view": view,
        "history": issue_cluster_history(root),
    })
    markdown_path.write_text(canonical_issue_ledger_markdown(root), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


__all__ = [
    "CONFIDENCE_LEVELS", "DECISION_TYPES", "LIFECYCLE_SEMANTICS", "LIFECYCLE_STATES",
    "RELATION_TYPES", "REPAIR_STATUSES", "REPORT_READINESS_STATES", "SCHEMA_VERSION",
    "SERIOUS_RECHECK_STATUSES", "SEVERITIES", "VERIFICATION_STATUSES",
    "build_canonical_issue_ledger", "canonical_cluster_paths", "canonical_concise_issue_records",
    "canonical_issue_cluster_view", "canonical_issue_ledger_markdown", "canonical_issue_status_summary",
    "clusters_requiring_reconciliation", "import_canonical_cluster_decisions", "issue_cluster_history",
    "load_raw_issue_state", "raw_issue_view", "read_issue_cluster_events", "report_readiness",
    "validate_cluster_decision",
]
