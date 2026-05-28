#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_state import append_jsonl, save_json  # noqa: E402


SCHEMA_VERSION = 1
REVIEW_METHOD = "llm_issue_family_recheck"
REQUIRED_RESULT_FIELDS = {
    "family_id",
    "verdict",
    "upstream_issue_ids",
    "downstream_issue_ids",
    "false_positive_issue_ids",
    "recommended_severity_by_issue",
    "recommended_status_by_issue",
    "grouping_recommendations",
    "final_report_treatment",
    "evidence_for",
    "evidence_against",
    "confidence",
    "needs_human_review",
    "summary",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _guard_audit_workdir(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.exists():
        raise RuntimeError(f"Audit workdir does not exist: {path}")
    if not path.is_dir():
        raise RuntimeError(f"Audit workdir is not a directory: {path}")
    return path


def _ensure_path_exists(path: Path, label: str, *, directory: bool = False) -> Path:
    path = path.expanduser().resolve()
    if not path.exists():
        raise RuntimeError(f"{label} does not exist: {path}")
    if directory and not path.is_dir():
        raise RuntimeError(f"{label} is not a directory: {path}")
    if not directory and not path.is_file():
        raise RuntimeError(f"{label} is not a file: {path}")
    return path


def _require_string(value: Any, field: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"Recheck result field {field!r} must be a string.")
    if not allow_empty and not value.strip():
        raise RuntimeError(f"Recheck result field {field!r} must not be blank.")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"Recheck result field {field!r} must be a boolean.")
    return value


def _require_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise RuntimeError(f"Recheck result field {field!r} must be a list.")
    out = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise RuntimeError(f"Recheck result field {field!r}[{index}] must be a string.")
        out.append(item)
    return out


def _require_recommendations(value: Any, field: str, recommendation_key: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise RuntimeError(f"Recheck result field {field!r} must be a list.")
    out = []
    required = {"issue_id", recommendation_key, "rationale"}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RuntimeError(f"Recheck result field {field!r}[{index}] must be an object.")
        missing = sorted(required - set(item))
        if missing:
            raise RuntimeError(f"Recheck result field {field!r}[{index}] is missing keys: {missing}")
        out.append(
            {
                "issue_id": _require_string(item.get("issue_id"), f"{field}[{index}].issue_id", allow_empty=False),
                recommendation_key: _require_string(item.get(recommendation_key), f"{field}[{index}].{recommendation_key}"),
                "rationale": _require_string(item.get("rationale"), f"{field}[{index}].rationale"),
            }
        )
    return out


def _require_grouping_recommendations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RuntimeError("Recheck result field 'grouping_recommendations' must be a list.")
    out = []
    required = {"upstream_issue_id", "downstream_issue_ids", "rationale"}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RuntimeError(f"Recheck result field 'grouping_recommendations'[{index}] must be an object.")
        missing = sorted(required - set(item))
        if missing:
            raise RuntimeError(f"Recheck result field 'grouping_recommendations'[{index}] is missing keys: {missing}")
        out.append(
            {
                "upstream_issue_id": _require_string(item.get("upstream_issue_id"), f"grouping_recommendations[{index}].upstream_issue_id", allow_empty=False),
                "downstream_issue_ids": _require_string_list(item.get("downstream_issue_ids"), f"grouping_recommendations[{index}].downstream_issue_ids"),
                "rationale": _require_string(item.get("rationale"), f"grouping_recommendations[{index}].rationale"),
            }
        )
    return out


def validate_recheck_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("Recheck result JSON must be an object.")
    missing = sorted(REQUIRED_RESULT_FIELDS - set(payload))
    if missing:
        raise RuntimeError(f"Recheck result is missing required fields: {missing}")
    return {
        "family_id": _require_string(payload.get("family_id"), "family_id", allow_empty=False),
        "verdict": _require_string(payload.get("verdict"), "verdict"),
        "upstream_issue_ids": _require_string_list(payload.get("upstream_issue_ids"), "upstream_issue_ids"),
        "downstream_issue_ids": _require_string_list(payload.get("downstream_issue_ids"), "downstream_issue_ids"),
        "false_positive_issue_ids": _require_string_list(payload.get("false_positive_issue_ids"), "false_positive_issue_ids"),
        "recommended_severity_by_issue": _require_recommendations(payload.get("recommended_severity_by_issue"), "recommended_severity_by_issue", "severity"),
        "recommended_status_by_issue": _require_recommendations(payload.get("recommended_status_by_issue"), "recommended_status_by_issue", "status"),
        "grouping_recommendations": _require_grouping_recommendations(payload.get("grouping_recommendations")),
        "final_report_treatment": _require_string(payload.get("final_report_treatment"), "final_report_treatment"),
        "evidence_for": _require_string_list(payload.get("evidence_for"), "evidence_for"),
        "evidence_against": _require_string_list(payload.get("evidence_against"), "evidence_against"),
        "confidence": _require_string(payload.get("confidence"), "confidence"),
        "needs_human_review": _require_bool(payload.get("needs_human_review"), "needs_human_review"),
        "summary": _require_string(payload.get("summary"), "summary"),
    }


def _issue_ids_affected(record: dict[str, Any]) -> list[str]:
    ids = set(record.get("upstream_issue_ids") or [])
    ids.update(record.get("downstream_issue_ids") or [])
    ids.update(record.get("false_positive_issue_ids") or [])
    for item in record.get("recommended_severity_by_issue") or []:
        if isinstance(item, dict) and item.get("issue_id"):
            ids.add(str(item["issue_id"]))
    for item in record.get("recommended_status_by_issue") or []:
        if isinstance(item, dict) and item.get("issue_id"):
            ids.add(str(item["issue_id"]))
    for item in record.get("grouping_recommendations") or []:
        if isinstance(item, dict):
            if item.get("upstream_issue_id"):
                ids.add(str(item["upstream_issue_id"]))
            ids.update(str(issue_id) for issue_id in item.get("downstream_issue_ids") or [])
    return sorted(ids)


def _load_sidecar(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "updated_at": None, "rechecks": []}
    payload = _load_json(path, default={})
    if not isinstance(payload, dict):
        raise RuntimeError(f"Existing issue recheck sidecar is not an object: {path}")
    rechecks = payload.get("rechecks")
    if not isinstance(rechecks, list):
        raise RuntimeError(f"Existing issue recheck sidecar has no rechecks list: {path}")
    return {
        "schema_version": int(payload.get("schema_version") or SCHEMA_VERSION),
        "updated_at": payload.get("updated_at"),
        "rechecks": rechecks,
    }


def _recheck_id(family_id: str, accepted_at: str, ordinal: int) -> str:
    safe_family = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in family_id).strip("_") or "family"
    stamp = accepted_at.replace("+00:00", "Z").replace(":", "").replace("-", "").replace(".", "")
    return f"{safe_family}_{stamp}_{ordinal:03d}"


def build_recheck_record(
    result: dict[str, Any],
    *,
    source_result_path: Path,
    source_output_dir: Path,
    accepted_at: str,
    existing_count: int,
) -> dict[str, Any]:
    record = {
        "recheck_id": _recheck_id(result["family_id"], accepted_at, existing_count + 1),
        "family_id": result["family_id"],
        "source_result_path": str(source_result_path),
        "source_output_dir": str(source_output_dir),
        "accepted_at": accepted_at,
        "review_method": REVIEW_METHOD,
    }
    record.update(result)
    return record


def import_issue_family_recheck(
    audit_workdir: Path,
    recheck_result: Path,
    source_output_dir: Path,
    *,
    accept: bool = False,
) -> dict[str, Any]:
    audit_workdir = _guard_audit_workdir(audit_workdir)
    recheck_result = _ensure_path_exists(recheck_result, "Recheck result")
    source_output_dir = _ensure_path_exists(source_output_dir, "Source output directory", directory=True)

    result_payload = _load_json(recheck_result, default=None)
    result = validate_recheck_result(result_payload)
    sidecar_path = audit_workdir / "state" / "issue_rechecks.json"
    log_path = audit_workdir / "logs" / "issue_recheck_decisions.jsonl"
    sidecar = _load_sidecar(sidecar_path)
    accepted_at = _utc_now()
    record = build_recheck_record(
        result,
        source_result_path=recheck_result,
        source_output_dir=source_output_dir,
        accepted_at=accepted_at,
        existing_count=len(sidecar["rechecks"]),
    )
    affected_issue_ids = _issue_ids_affected(record)
    manifest = {
        "dry_run": not accept,
        "would_write": bool(accept),
        "audit_workdir": str(audit_workdir),
        "sidecar_path": str(sidecar_path),
        "decision_log_path": str(log_path),
        "family_id": record["family_id"],
        "recheck_id": record["recheck_id"],
        "affected_issue_ids": affected_issue_ids,
        "existing_recheck_count": len(sidecar["rechecks"]),
        "new_recheck_count": len(sidecar["rechecks"]) + (1 if accept else 0),
        "canonical_issue_mutation": False,
    }
    if not accept:
        return manifest

    sidecar["schema_version"] = SCHEMA_VERSION
    sidecar["updated_at"] = accepted_at
    sidecar["rechecks"].append(record)
    save_json(sidecar_path, sidecar)
    append_jsonl(
        log_path,
        {
            "timestamp": accepted_at,
            "action": "accepted_issue_family_recheck",
            "family_id": record["family_id"],
            "recheck_id": record["recheck_id"],
            "issue_ids_affected": affected_issue_ids,
            "source_result_path": str(recheck_result),
            "source_output_dir": str(source_output_dir),
            "sidecar_path": str(sidecar_path),
            "canonical_issue_mutation": False,
        },
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import an accepted issue-family recheck result into an append-only audit sidecar.")
    parser.add_argument("--audit-workdir", required=True, type=Path)
    parser.add_argument("--recheck-result", required=True, type=Path)
    parser.add_argument("--source-output-dir", required=True, type=Path)
    parser.add_argument("--accept", action="store_true", help="Write state/issue_rechecks.json and append logs/issue_recheck_decisions.jsonl.")
    args = parser.parse_args(argv)
    manifest = import_issue_family_recheck(
        args.audit_workdir,
        args.recheck_result,
        args.source_output_dir,
        accept=bool(args.accept),
    )
    print("Issue family recheck import prepared." if manifest["dry_run"] else "Issue family recheck accepted.")
    print(f"  Family: {manifest['family_id']}")
    print(f"  Recheck id: {manifest['recheck_id']}")
    print(f"  Dry run: {manifest['dry_run']}")
    print(f"  Would write: {manifest['would_write']}")
    print(f"  Sidecar: {manifest['sidecar_path']}")
    print(f"  Decision log: {manifest['decision_log_path']}")
    print(f"  Affected issues: {', '.join(manifest['affected_issue_ids']) or '(none)'}")
    print("  Canonical issue mutation: false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
