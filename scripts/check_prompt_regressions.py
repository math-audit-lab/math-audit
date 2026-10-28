#!/usr/bin/env python3
"""Offline prompt contracts and read-only historical evidence checks. No API client."""
from __future__ import annotations
import ast
import gzip
import hashlib
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_prompts import (
    AUDIT_PROMPT_VERSION, SHIPPED_AUDIT_SYSTEM_PROMPT, VERIFICATION_GENERATION_CONTRACT,
    MATHEMATICAL_ADJUDICATION_INSTRUCTIONS, prompt_snapshot_metadata,
    prompt_suite_metadata, request_prompt_metadata,
)

BASELINE = PROJECT_ROOT / "docs/prompt_baselines/production_2026-10-28"


def _assignment(filename: str, name: str, *, as_source: bool = False):
    source = (PROJECT_ROOT / filename).read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.get_source_segment(source, node.value) if as_source else ast.literal_eval(node.value)
    raise AssertionError(f"Missing schema {filename}:{name}")


def test_baseline_and_schema_contracts() -> None:
    manifest = json.loads((BASELINE / "manifest.json").read_text())
    baseline_prompt = (BASELINE / "audit_system_prompt.txt").read_text()
    assert hashlib.sha256(baseline_prompt.encode()).hexdigest() == manifest["main_prompt_sha256"]
    inventory = json.loads(gzip.decompress((BASELINE / manifest["inventory"]).read_bytes()))
    assert inventory["git_revision"] == manifest["git_revision"]
    for filename, module in inventory["modules"].items():
        assert module["source_sha256"] == manifest["modules"][filename]["source_sha256"]
        for item in module["strings"]:
            text = item["text"] if item["text"] is not None else item["source"]
            assert hashlib.sha256(text.encode()).hexdigest() == item["sha256"]
    # AST dump defaults differ across Python releases; exact source is portable.
    assert manifest["schema_hash_kind"] == "exact_expression_source_utf8_sha256"
    for key, expected in manifest["schemas"].items():
        filename, name = key.split(":")
        schema_source = _assignment(filename, name, as_source=True)
        assert hashlib.sha256(schema_source.encode("utf-8")).hexdigest() == expected, key
    schema = _assignment("audit_runtime.py", "AUDIT_RESPONSE_SCHEMA")
    issue = schema["properties"]["issues"]["items"]
    assert {"location", "evidence", "severity", "description"} <= set(issue["required"])
    assert issue["properties"]["severity"]["enum"] == ["low", "medium", "high", "critical"]


def test_mathematical_prompt_safeguards() -> None:
    core = SHIPPED_AUDIT_SYSTEM_PROMPT
    for phrase in (
        "high recall", "formula and line", "quantifiers", "parameter ranges", "equalities", "inequalities",
        "false intermediate assertion", "potentially true theorem", "locally repairable proof gap",
        "missing hypothesis", "PDF extraction artifact", "redundant step", "resolved by earlier context",
        "disprove the objection", "preserve the theorem and claimed rate", "TeX", "AUX", "PDF",
        "external_verification_required", "source label", "page/chunk/line", "verification-derived findings",
        "not location", "provisional candidates", "canonical dispositions", "new/corrected", "chunk_too_large",
    ):
        assert phrase in core, phrase
    assert len(core) < len((BASELINE / "audit_system_prompt.txt").read_text())
    contract = VERIFICATION_GENERATION_CONTRACT
    for field in ("schema_version=1", "check_kind", "outcome", "summary", "counterexamples", "failed_cases", "tested_range", "target", "linked_issue_ids"):
        assert field in contract, field
    for outcome in ("counterexample_found", "claim_failed", "no_counterexample_found", "check_satisfied", "diagnostic_only", "inconclusive", "not_reported"):
        assert outcome in contract, outcome
    for phrase in ("MATH_AUDIT_VERIFICATION_RESULT_JSON=", "exactly one final line", "Nonempty counterexample", "Return code 0", "Finite searches", "heuristic", "no network", "script errors from theorem errors"):
        assert phrase in contract, phrase
    assert "Recommendations remain advisory" in MATHEMATICAL_ADJUDICATION_INSTRUCTIONS
    policy = (PROJECT_ROOT / "audit_policy_hooks.py").read_text()
    assert "{VERIFICATION_GENERATION_CONTRACT}" in policy
    assert "<manuscript_evidence>" in policy


def test_prompt_metadata() -> None:
    metadata = prompt_snapshot_metadata(SHIPPED_AUDIT_SYSTEM_PROMPT, "shipped_default", "gpt-6.1-sol")
    assert metadata["version"] == AUDIT_PROMPT_VERSION
    assert metadata["sha256"] == hashlib.sha256(SHIPPED_AUDIT_SYSTEM_PROMPT.encode()).hexdigest()
    assert prompt_snapshot_metadata("custom", "custom_default", "gpt-6.1-sol")["version"] == "custom-content-hash"
    suite = prompt_suite_metadata()
    for surface in ("verification_generation", "adjudication", "audit_verification.py:build_verification_finding_recheck_prompt", "audit_verification.py:build_verification_technical_repair_prompt", "scripts/run_issue_family_recheck.py:_build_prompt"):
        assert len(suite["surfaces"][surface]["sha256"]) == 64
    payload = [{"role": "developer", "content": "a"}, {"role": "user", "content": "b"}]
    assert request_prompt_metadata(payload)["sha256"] != request_prompt_metadata(list(reversed(payload)))["sha256"]
    assert request_prompt_metadata(payload) == request_prompt_metadata(json.loads(json.dumps(payload)))


def test_recheck_evidence_and_lifecycle() -> None:
    from audit_verification import build_verification_finding_recheck_prompt, build_verification_technical_repair_prompt
    from audit_issue_clusters import validate_cluster_decision
    script = "print('complete original script')\n"
    prompt = build_verification_finding_recheck_prompt({"verification_evidence": [{
        "script_name": "scope.py", "complete_python_script": script,
        "stdout": "negative evidence", "stderr": "exact stderr", "complete_counterexamples": [{"x": 2}],
    }]})
    assert script in prompt and "negative evidence" in prompt and '"x": 2' in prompt
    assert "execution evidence" in prompt and "mathematical interpretation still requires" in prompt
    repair = build_verification_technical_repair_prompt({"verification_failure": {
        "execution_status": "timeout", "complete_original_python_script": script,
    }}, "TR-OFFLINE")
    assert script in repair and "original script and result remain immutable" in repair
    assert "never silently reduce the range" in repair
    # These are schema/semantics fixtures, not adjudications of historical papers.
    cases = [
        ("false intermediate estimate", "verified", "not_repaired"),
        ("local rate-preserving repair", "resolved", "verified_local_repair"),
        ("objection refuted by complete context", "rejected", "not_applicable"),
        ("downstream duplicate", "duplicate", "not_applicable"),
        ("external theorem unchecked", "external_verification_required", "not_assessed"),
        ("valid minor local defect", "supported", "repair_proposed"),
    ]
    known = {"I001": {"issue_id": "I001", "severity": "low", "location": "test fixture", "chunk_id": "chunk_001"}}
    for title, lifecycle, repair_status in cases:
        decision = validate_cluster_decision({"cluster_id": "CL-TEST", "title": title, "root_claim": title,
            "raw_issue_ids": ["I001"], "lifecycle_state": lifecycle, "repair_status": repair_status,
            "final_severity": "low"}, known)
        assert decision["lifecycle_state"] == lifecycle
        assert decision["repair_status"] == repair_status
    candidate = validate_cluster_decision({"cluster_id": "CL-NEW", "title": "new model finding", "root_claim": "claim", "raw_issue_ids": ["I001"]}, known)
    assert candidate["lifecycle_state"] == "suspected" and candidate["verification_status"] == "not_checked"



def test_read_only_historical_evaluation() -> None:
    import tempfile
    from scripts.prepare_model_prompt_evaluation import prepare, inspect_case, CASES
    examples = PROJECT_ROOT / "examples"
    before = {name: inspect_case(examples, path) for name, path in CASES.items()}
    with tempfile.TemporaryDirectory(prefix="math-audit-prompt-eval-") as tmp:
        output = Path(tmp) / "new-evaluation"
        plan = prepare(examples, output)
        assert plan["live_execution_authorized"] is False
        assert len(plan["arms"]) == 3
        assert (output / "review_labels.csv").exists()
        for name, record in plan["cases"].items():
            assert record == before[name]
        try:
            prepare(examples, output)
        except FileExistsError:
            pass
        else:
            raise AssertionError("Existing evaluation was overwritten")
    after = {name: inspect_case(examples, path) for name, path in CASES.items()}
    assert before == after, "Historical evidence was changed during evaluation preparation"
    try:
        prepare(examples, examples / "must-never-be-created")
    except ValueError:
        pass
    else:
        raise AssertionError("Evaluation output inside examples was allowed")


def run_checks() -> None:
    for check in (test_baseline_and_schema_contracts, test_mathematical_prompt_safeguards, test_prompt_metadata, test_recheck_evidence_and_lifecycle, test_read_only_historical_evaluation):
        check()
        print(f"PASS {check.__name__}")


if __name__ == "__main__":
    run_checks()
