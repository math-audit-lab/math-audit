#!/usr/bin/env python3
"""Prepare a reproducible, read-only evaluation manifest. Never calls an API.

The generated matrix is a plan, not a live runner or an adjudication. Keep the
source audits immutable; an approved live evaluation must use new output dirs.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from audit_prompts import SHIPPED_AUDIT_SYSTEM_PROMPT, prompt_suite_metadata
from audit_models import DEFAULT_MODEL, DEFAULT_REASONING_EFFORT

CASES = {
    "jnt_pdf_only": "JNT/IJNT-D-25-00223_R1_audit",
    "dickman_tex_aware": "JNT_TEX/Dickman_revised_audit",
    "lmj_verification": "LMJ/LMJ2604-004RA0_audit",
    "stirling_large_k_tex": "stirling_large_k/gaussian_quadrature_audit",
    "elementary_depoissonization": "elementary_depoissonization/elementary_depoissonization_RJ_revised_audit",
}
DICKMAN_REVIEW_ANCHORS = {
    "false_intermediate_estimate_and_rate_preserving_repair": ["I075"],
    "full_context_and_hypothesis_reconstruction": ["I059", "I060", "I065", "I078", "I081"],
    "duplicates_or_downstream": ["I033", "I067", "I069", "I071", "I077", "I082", "I085", "I086"],
    "external_source_uncertainty": ["I017", "I022", "I039"],
    "validity_of_minor_local_candidates": ["I028", "I032", "I051", "I072"],
    "redundant_step_vs_false_theorem": ["I080"],
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_case(examples: Path, relative: str) -> dict:
    root = examples / relative
    if not root.is_dir():
        return {"source": relative, "available": False, "limitation": "fixture not installed"}
    files = []
    # Existing JSON and prompt/check evidence are only read, never executed.
    for directory in ("state", "prompts", "verification"):
        location = root / directory
        if not location.exists():
            continue
        for path in sorted(location.rglob("*")):
            if path.is_file() and path.suffix in {".json", ".jsonl", ".txt", ".py"}:
                files.append({"path": path.relative_to(root).as_posix(), "sha256": _sha(path)})
    source_files = [
        {"path": path.name, "sha256": _sha(path)}
        for path in sorted(root.parent.iterdir())
        if path.is_file() and path.suffix.lower() in {".pdf", ".tex", ".aux", ".bib", ".sty"}
    ]
    session_path = root / "state/session.json"
    session = json.loads(session_path.read_text()) if session_path.exists() else {}
    issues_path = root / "state/issues.json"
    issues = json.loads(issues_path.read_text()) if issues_path.exists() else {}
    records = issues.get("issues", []) if isinstance(issues, dict) else issues
    canonical_path = root / "state/canonical_issue_clusters.json"
    return {
        "source": relative, "available": True, "files": files, "source_files": source_files,
        "historical_configuration": {key: session.get(key) for key in (
            "model", "reasoning_effort", "reasoning_mode", "audit_context_mode", "audit_system_prompt_metadata")},
        "raw_issue_count_observed": len(records),
        "issue_ids": [str(item.get("issue_id")) for item in records if isinstance(item, dict)],
        "canonical_artifact_available": canonical_path.exists(),
        "ordered_prompt_files": [item["path"] for item in files if item["path"].startswith("prompts/")],
    }


def prepare(examples: Path, output: Path) -> dict:
    examples = examples.resolve()
    output = output.resolve()
    if output == examples or examples in output.parents:
        raise ValueError("Evaluation output must be outside examples and every historical audit directory")
    # Refuse reuse to avoid overwriting a previous evaluation or reviewer labels.
    output.mkdir(parents=True, exist_ok=False)
    cases = {name: inspect_case(examples, relative) for name, relative in CASES.items()}
    baseline = PROJECT_ROOT / "docs/prompt_baselines/production_2026-10-28"
    (output / "baseline_audit_system_prompt.txt").write_bytes((baseline / "audit_system_prompt.txt").read_bytes())
    (output / "modern_audit_system_prompt.txt").write_text(SHIPPED_AUDIT_SYSTEM_PROMPT, encoding="utf-8")
    plan = {
        "schema_version": 1, "created_for": "GPT-6 model/prompt comparison",
        "live_execution_authorized": False,
        "warning": "Offline preparation only. No findings are independently validated by this manifest.",
        "model": DEFAULT_MODEL, "reasoning_effort": DEFAULT_REASONING_EFFORT, "reasoning_mode": "standard",
        "prompt_suite": prompt_suite_metadata(), "cases": cases,
        "arms": [
            {"id": "A", "model": "saved historical model", "prompts": "saved historical requests and results", "paid_replay": False},
            {"id": "B", "model": DEFAULT_MODEL, "prompts": "frozen pre-migration core and task templates", "purpose": "model-only comparison"},
            {"id": "C", "model": DEFAULT_MODEL, "prompts": "modern core and task templates", "purpose": "prompt-only comparison against B"},
        ],
        "controls": [
            "Use identical manuscript bytes, chunk manifests, source labels, schema, sampling order, and context mode.",
            "Replay each arm in a new conversation with its own ledger; never copy one arm's findings into another.",
            "Continuous mode requires replay of the ordered preceding context, not isolated late-chunk prompts.",
            "Keep supported reasoning effort equal between B and C; record mode, TTL, tier, usage, and exact request hashes.",
            "Do not infer comparative quality from historical candidate counts or unequal manuscript revisions.",
            "Use independent blinded mathematical adjudication, report recall and false positives separately, and retain valid minor issues.",
            "Compare cost per accepted independent issue, counterexample precision, rate-preserving repairs, latency, tokens and cache writes.",
            "Run at least three approved repetitions for uncertain comparisons; keep every original output and reviewer rationale.",
        ],
        "dickman_review_anchors": DICKMAN_REVIEW_ANCHORS,
        "dickman_gold_limitation": "Raw candidates are present; the independent 86-to-clusters comparison and accepted golden dispositions are not bundled. Anchors are review questions, not asserted truth labels.",
    }
    (output / "evaluation_plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    with (output / "review_labels.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case", "arm", "issue_id", "independent_cluster", "mathematical_category", "disposition", "severity", "rate_preserved", "evidence", "reviewer"])
        for name, case in cases.items():
            for issue_id in case.get("issue_ids", []):
                writer.writerow([name, "A", issue_id, "", "", "", "", "", "", ""])
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--examples", type=Path, default=PROJECT_ROOT / "examples")
    parser.add_argument("--output", type=Path, required=True, help="New directory outside examples")
    args = parser.parse_args()
    plan = prepare(args.examples, args.output)
    print(f"Prepared {sum(case.get('available', False) for case in plan['cases'].values())} historical cases; no API requests made.")


if __name__ == "__main__":
    main()
