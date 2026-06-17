#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import audit_runtime as runtime
from audit_chunking import ensure_chunk_display_labels, pdf_chunk_display_label
from audit_policy_hooks import (
    CONTINUOUS_RUNNING_CONTEXT_MAX_CHARS,
    CONTINUOUS_RUNNING_CONTEXT_PROFILE,
    FRESH_CONTEXT_RETRIEVAL_PROFILE,
    _audit_summary_markdown,
    _audit_summary_tex,
    _augment_source_labels_in_text,
    _build_running_audit_context_for_chunk,
    _load_aux_label_map,
    _report_latex_paragraph_local,
    build_concise_report_json,
    build_concise_report_markdown,
    build_concise_report_tex,
    build_final_report as policy_build_final_report,
    build_final_report_markdown,
    build_final_report_tex,
    build_user_message_for_chunk,
    source_ingestion_diagnostics,
)
from audit_runtime import (
    AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL,
    DEFAULT_AUDIT_CONTEXT_MODE,
    FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_REATTACH,
    FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_TEXT_ONLY,
    FRESH_CONTEXT_PRIOR_ISSUE_CAUTION,
    FRESH_CONTEXT_TEXT_FIRST_NOTE,
    PDF_TEXT_ONLY_RETRY_NOTE,
    _append_audit_context_db_entries,
    _audit_request_size_diagnostics,
    _existing_session_audit_context_mode,
    _file_download_timeout_auto_retry_decision,
    _file_download_timeout_retry_mode,
    _retryable_response_failure_reason,
    _save_request_metadata,
    _should_reattach_pdf_for_chunk_retry,
    build_fresh_audit_context_for_chunk,
    get_audit_status,
    get_report_freshness,
    get_verification_suite_status,
    refresh_report_latex_compile_health_sidecar,
    report_latex_paragraph,
    report_latex_compile_health,
)
from audit_state import save_json, session_paths, usage_cache_diagnostics
from gui_controller import (
    format_chunk_completion_log_line,
    format_running_chunk_started_log_line,
    fresh_start_context_mode_mismatch_info,
    persistent_audit_log_preview,
)
from scripts.prepare_context_mode_comparison import prepare_context_mode_comparison
from scripts.prepare_issue_recheck_candidates import prepare_issue_recheck_candidates
from scripts.prepare_issue_recheck_families import prepare_issue_recheck_families
from scripts.prepare_rerun_recheck_candidates import prepare_rerun_recheck_candidates
from scripts.import_issue_family_recheck import import_issue_family_recheck
from scripts.run_issue_family_recheck import RESULT_SCHEMA, run_issue_family_recheck, validate_result_schema
from scripts.run_context_mode_ab_test import run_context_mode_ab_test


OLD = "2026-01-01T00:00:00+00:00"
MID = "2026-01-02T00:00:00+00:00"
NEW = "2026-01-03T00:00:00+00:00"
LATEST = "2026-01-04T00:00:00+00:00"


@dataclass(frozen=True)
class RegressionResult:
    name: str
    passed: bool
    detail: str


class RegressionFailure(AssertionError):
    pass


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _append_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _synthetic_session(workdir: Path) -> dict[str, Any]:
    return {
        "created_at": OLD,
        "updated_at": OLD,
        "workdir": str(workdir),
        "pdf_path": str(workdir.parent / "paper.pdf"),
        "model": "gpt-5.4",
        "reasoning_effort": "high",
        "audit_started_at": OLD,
        "audit_finished_at": OLD,
        "active_qa_thread_id": "thread_legacy",
        "qa_threads": {
            "thread_legacy": {
                "thread_id": "thread_legacy",
                "created_at": OLD,
                "conversation_id": None,
                "pdf_attached_in_conversation": False,
            }
        },
    }


def _seed_state(workdir: Path, verification_finished_at: str = OLD) -> dict[str, Any]:
    session = _synthetic_session(workdir)
    paths = session_paths(workdir)
    _write_json(paths["session"], session)
    _write_json(paths["issues"], {"issues": [], "updated_at": OLD})
    _write_json(paths["status"], {"status": "completed", "updated_at": OLD})
    _write_json(paths["manifest"], {"chunks": [], "updated_at": OLD})
    _write_json(paths["ledger"], {"assumptions": [], "notes": [], "updated_at": OLD})
    _write_json(
        paths["usage"],
        {
            "model": "gpt-5.4",
            "totals": {"total_tokens": 0, "cost_usd": 0.0, "audit_seconds": 0.0},
            "per_chunk": [],
            "updated_at": OLD,
        },
    )
    _write_json(
        paths["verification_state"],
        {
            "updated_at": LATEST,
            "last_run": {
                "started_at": verification_finished_at,
                "finished_at": verification_finished_at,
                "summary": {"passed": 0, "failed": 0, "timed_out": 0, "skipped": 0},
            },
        },
    )
    return session


def _report_json_path(freshness: dict[str, Any], kind: str) -> Path:
    return Path(freshness["reports"][kind]["paths"]["json"])


def _report_tex_path(freshness: dict[str, Any], kind: str) -> Path:
    return Path(freshness["reports"][kind]["paths"]["tex"])


def _write_report_metadata(session: dict[str, Any], kind: str, generated_at: str) -> None:
    freshness = get_report_freshness(session)
    _write_json(_report_json_path(freshness, kind), {"generated_at": generated_at, "kind": kind})


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RegressionFailure(message)


def test_report_freshness_detection() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_freshness_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)

        freshness = get_report_freshness(session)
        statuses = {kind: info["status"] for kind, info in freshness["reports"].items()}
        _assert(statuses == {"full": "missing", "concise": "missing", "verification": "missing"}, str(statuses))

        _write_text(_report_tex_path(freshness, "full"), "% synthetic report without JSON sidecar\n")
        freshness = get_report_freshness(session)
        _assert(freshness["reports"]["full"]["status"] == "unknown", freshness["reports"]["full"]["reason"])

        session = _seed_state(workdir, verification_finished_at=NEW)
        _write_report_metadata(session, "full", MID)
        freshness = get_report_freshness(session)
        _assert(freshness["reports"]["full"]["status"] == "stale", freshness["reports"]["full"]["reason"])

        session = _seed_state(workdir, verification_finished_at=OLD)
        _write_report_metadata(session, "verification", MID)
        _append_jsonl(workdir / "logs" / "selected_chunk_reruns.jsonl", {"time": NEW, "chunks": ["chunk_001"]})
        freshness = get_report_freshness(session)
        verification_info = freshness["reports"]["verification"]
        _assert(verification_info["status"] == "stale", verification_info["reason"])
        _assert(
            (verification_info.get("latest_source") or {}).get("name") == "selected chunk rerun log",
            f"unexpected latest source: {verification_info.get('latest_source')}",
        )

        _write_report_metadata(session, "verification", LATEST)
        freshness = get_report_freshness(session)
        _assert(freshness["reports"]["verification"]["status"] == "current", freshness["reports"]["verification"]["reason"])

        # Report-generation bookkeeping can update verification.json, but the
        # verification report should stay current when last_run is older.
        paths = session_paths(workdir)
        state = json.loads(paths["verification_state"].read_text(encoding="utf-8"))
        state["updated_at"] = LATEST
        state["last_run"]["finished_at"] = OLD
        _write_json(paths["verification_state"], state)
        (workdir / "logs" / "selected_chunk_reruns.jsonl").unlink()
        _write_report_metadata(session, "verification", MID)
        freshness = get_report_freshness(session)
        _assert(freshness["reports"]["verification"]["status"] == "current", freshness["reports"]["verification"]["reason"])


def test_audit_completion_builds_full_and_concise_reports() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_completion_reports_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        old_hook = runtime._FINAL_REPORT_BUILDER_HOOK
        try:
            runtime.set_final_report_builder(policy_build_final_report)
            result = runtime.build_audit_completion_reports(
                session,
                include_verification_summary_in_final_report=True,
                write_separate_verification_report=False,
            )
        finally:
            runtime._FINAL_REPORT_BUILDER_HOOK = old_hook

        full_paths = result.get("full_report_paths") or {}
        concise_paths = result.get("concise_report_paths") or {}
        _assert(full_paths, str(result))
        _assert(concise_paths, str(result))
        _assert(not result.get("report_generation_warnings"), str(result.get("report_generation_warnings")))
        for payload in (full_paths, concise_paths):
            for key in ("markdown", "tex", "json"):
                path = Path(payload.get(key) or "")
                _assert(path.is_file(), f"missing {key}: {path}")
        combined = result.get("report_paths") or {}
        _assert("concise_markdown" in combined, str(combined))
        _assert("concise_tex" in combined, str(combined))
        _assert("concise_json" in combined, str(combined))

        freshness = get_report_freshness(session)
        _assert(freshness["reports"]["full"]["status"] == "current", freshness["reports"]["full"]["reason"])
        _assert(freshness["reports"]["concise"]["status"] == "current", freshness["reports"]["concise"]["reason"])

    with tempfile.TemporaryDirectory(prefix="math_audit_completion_concise_fail_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        old_hook = runtime._FINAL_REPORT_BUILDER_HOOK
        old_concise_builder = runtime.build_concise_report

        def _failing_concise_report(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            raise RuntimeError("synthetic concise failure")

        try:
            runtime.set_final_report_builder(policy_build_final_report)
            runtime.build_concise_report = _failing_concise_report
            result = runtime.build_audit_completion_reports(
                session,
                include_verification_summary_in_final_report=True,
                write_separate_verification_report=False,
            )
        finally:
            runtime.build_concise_report = old_concise_builder
            runtime._FINAL_REPORT_BUILDER_HOOK = old_hook

        _assert(result.get("full_report_paths"), str(result))
        _assert(result.get("concise_report_paths") is None, str(result))
        warnings = result.get("report_generation_warnings") or []
        _assert(any("audit remains completed" in warning for warning in warnings), str(warnings))
        status = json.loads(session_paths(workdir)["status"].read_text(encoding="utf-8"))
        _assert(status.get("status") == "completed", str(status))
        freshness = get_report_freshness(session)
        _assert(freshness["reports"]["full"]["status"] == "current", freshness["reports"]["full"]["reason"])


def test_invalidated_verification_inventory_warning() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_verification_inventory_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        paths = session_paths(workdir)

        _write_text(workdir / "python_checks" / "chunk_001_check_01.py", "print('ok')\n")
        active_result = {
            "time": NEW,
            "chunk_id": "chunk_001",
            "chunk_index": 1,
            "script_name": "chunk_001_check_01.py",
            "script_path": str(workdir / "python_checks" / "chunk_001_check_01.py"),
            "result_path": str(workdir / "verification_results" / "chunk_001_check_01.result.json"),
            "status": "passed",
            "returncode": 0,
            "elapsed_seconds": 0.01,
            "conclusion": "ok",
        }
        _write_json(workdir / "verification_results" / "chunk_001_check_01.result.json", active_result)
        _write_json(
            paths["verification_state"],
            {
                "updated_at": NEW,
                "last_run": {
                    "started_at": NEW,
                    "finished_at": NEW,
                    "scripts_total": 1,
                    "passed": 1,
                    "failed": 0,
                    "timeout": 0,
                    "skipped": 0,
                },
                "results": [active_result],
            },
        )
        _write_text(
            paths["chunk_records"],
            json.dumps(
                {
                    "chunk_id": "chunk_001",
                    "chunk_index": 1,
                    "python_paths": [str(workdir / "python_checks" / "chunk_001_check_01.py")],
                },
                ensure_ascii=False,
            )
            + "\n",
        )
        removed_result = {
            "chunk_id": "chunk_002",
            "chunk_index": 2,
            "script_name": "chunk_002_check_01.py",
            "status": "failed",
            "result_path": str(workdir / "verification_results" / "chunk_002_check_01.result.json"),
            "conclusion": "synthetic failure before rerun",
        }
        removed_result_still_needs_rerun = {
            "chunk_id": "chunk_003",
            "chunk_index": 3,
            "script_name": "chunk_003_check_01.py",
            "status": "failed",
            "result_path": str(workdir / "verification_results" / "chunk_003_check_01.result.json"),
            "conclusion": "synthetic failure before rerun",
        }
        _append_jsonl(
            workdir / "logs" / "selected_chunk_reruns.jsonl",
            {
                "time": NEW,
                "action": "failed",
                "rerun_id": "rerun_failed",
                "chunk_ids": ["chunk_002", "chunk_003"],
                "replacement_summary": {
                    "removed_verification_results": {
                        "removed_result_count": 2,
                        "removed_results": [removed_result, removed_result_still_needs_rerun],
                    }
                },
                "error": "RuntimeError('replacement chunk failed')",
            },
        )
        _append_jsonl(
            workdir / "logs" / "selected_chunk_reruns.jsonl",
            {
                "time": LATEST,
                "action": "finished",
                "rerun_id": "rerun_repaired_chunk_002",
                "chunk_ids": ["chunk_002"],
                "replacement_summary": {},
            },
        )

        status = get_verification_suite_status(session)
        _assert(status["scripts_total"] == 1, status)
        warning = status.get("inventory_warning") or {}
        _assert(warning.get("has_invalidated_obligations"), warning)
        _assert(warning.get("invalidated_script_count") == 2, warning)
        _assert(warning.get("affected_chunks") == ["chunk_002", "chunk_003"], warning)
        _assert(warning.get("rerun_missing_script_count") == 1, warning)
        _assert(warning.get("rerun_missing_chunks") == ["chunk_002"], warning)
        _assert(warning.get("needs_rerun_script_count") == 1, warning)
        _assert(warning.get("needs_rerun_chunks") == ["chunk_003"], warning)
        _assert("currently active verification scripts" in warning.get("message", ""), warning)
        _assert("were rerun but did not regenerate" in warning.get("message", ""), warning)
        _assert("script still needs a successful replacement chunk rerun" in warning.get("message", ""), warning)

        _write_text(workdir / "python_checks" / "chunk_002_check_01.py", "print('repaired')\n")
        _write_json(workdir / "verification_results" / "chunk_002_check_01.result.json", {**removed_result, "status": "passed"})
        _write_text(workdir / "python_checks" / "chunk_003_check_01.py", "print('repaired')\n")
        _write_json(
            workdir / "verification_results" / "chunk_003_check_01.result.json",
            {**removed_result_still_needs_rerun, "status": "passed"},
        )
        status = get_verification_suite_status(session)
        warning = status.get("inventory_warning") or {}
        _assert(not warning.get("has_invalidated_obligations"), warning)


def test_successful_selected_rerun_restores_completed_status() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_selected_rerun_status_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        paths = session_paths(workdir)
        chunks = [
            {
                "chunk_id": "chunk_001",
                "chunk_index": 1,
                "label": "PDF pages 1-1",
                "source_kind": "pdf",
                "page_start": 1,
                "page_end": 1,
                "chunk_text": "Chunk one.",
            },
            {
                "chunk_id": "chunk_002",
                "chunk_index": 2,
                "label": "PDF pages 2-2",
                "source_kind": "pdf",
                "page_start": 2,
                "page_end": 2,
                "chunk_text": "Chunk two.",
            },
        ]
        save_json(paths["manifest"], {"chunks": chunks, "pdf_page_count": 2, "updated_at": OLD})
        session["audit_finished_at"] = MID
        session["next_chunk_index"] = 1
        _write_json(paths["session"], session)
        _write_json(
            paths["status"],
            {
                "status": "paused",
                "current_chunk_id": "chunk_001",
                "chunks_completed": 1,
                "chunks_total": 2,
                "progress_pct": 50.0,
                "estimated_pages_completed": 1,
                "estimated_pages_total": 2,
                "audit_finished_at": MID,
                "updated_at": NEW,
            },
        )
        _write_text(
            paths["chunk_records"],
            "\n".join(
                json.dumps(
                    {
                        "time": OLD,
                        "chunk_id": chunk["chunk_id"],
                        "chunk_index": chunk["chunk_index"],
                        "label": chunk["label"],
                        "structured_response_path": "",
                        "issue_ids": [],
                    },
                    ensure_ascii=False,
                )
                for chunk in chunks
            )
            + "\n",
        )

        original_process_one_chunk = runtime.process_one_chunk

        def fake_process_one_chunk(session_arg: dict[str, Any], chunk: dict[str, Any], **_: Any) -> dict[str, Any]:
            record = {
                "time": LATEST,
                "chunk_id": chunk["chunk_id"],
                "chunk_index": chunk["chunk_index"],
                "label": chunk.get("label"),
                "structured_response_path": "",
                "issue_ids": [],
                "response_id": "resp_synthetic_rerun",
            }
            _append_jsonl(Path(session_arg["workdir"]) / "state" / "chunks.jsonl", record)
            return {"record": record}

        runtime.process_one_chunk = fake_process_one_chunk
        try:
            result = runtime.rerun_selected_chunks(session, ["chunk_001"], rebuild_reports=False)
        finally:
            runtime.process_one_chunk = original_process_one_chunk

        saved_session = json.loads(paths["session"].read_text(encoding="utf-8"))
        saved_status = json.loads(paths["status"].read_text(encoding="utf-8"))
        _assert(result["status"]["status"] == "completed", result["status"])
        _assert(saved_status["status"] == "completed", saved_status)
        _assert(saved_status["chunks_completed"] == 2, saved_status)
        _assert(saved_status["chunks_total"] == 2, saved_status)
        _assert(saved_status["current_chunk_id"] is None, saved_status)
        _assert(saved_session["next_chunk_index"] == 3, saved_session)
        _assert(saved_session.get("audit_finished_at") == MID, saved_session)


def _old_manifest_chunks() -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": "chunk_001",
            "chunk_index": 1,
            "source_kind": "pdf",
            "page_start": 1,
            "page_end": 1,
            "chunk_text": "0 <= F(x) <= 1\nThis paragraph discusses distribution functions in the proof.",
        },
        {
            "chunk_id": "chunk_002",
            "chunk_index": 2,
            "source_kind": "pdf",
            "page_start": 8,
            "page_end": 9,
            "chunk_text": "Lemma 8. Let X be compact. Then every open cover has a finite subcover.",
        },
    ]


def test_pdf_display_labels() -> None:
    manifest = {"chunks": _old_manifest_chunks()}
    original_ids = [chunk["chunk_id"] for chunk in manifest["chunks"]]
    labeled = ensure_chunk_display_labels(copy.deepcopy(manifest))
    labeled_ids = [chunk["chunk_id"] for chunk in labeled["chunks"]]
    _assert(labeled_ids == original_ids, f"chunk ids changed: {labeled_ids}")
    _assert(all(chunk.get("display_label") for chunk in labeled["chunks"]), "missing display_label backfill")

    formula_label = pdf_chunk_display_label(
        3,
        3,
        "0 <= F(x) <= 1\nThis paragraph discusses distribution functions in the proof.",
    )
    _assert("0 <= F(x) <= 1" not in formula_label, formula_label)
    _assert(formula_label.startswith("PDF pages 3-3"), formula_label)

    lemma_label = pdf_chunk_display_label(
        8,
        9,
        "Lemma 8. Let X be compact. Then every open cover has a finite subcover.",
    )
    _assert("PDF pages 8-9" in lemma_label and "Lemma 8" in lemma_label, lemma_label)


def test_status_display_label_backfill_does_not_rewrite_manifest() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_manifest_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        paths = session_paths(workdir)
        manifest = {"chunks": _old_manifest_chunks(), "updated_at": OLD}
        save_json(paths["manifest"], manifest)
        before_bytes = paths["manifest"].read_bytes()
        before_mtime_ns = paths["manifest"].stat().st_mtime_ns

        status_payload = get_audit_status(session, include_manifest=True)

        after_bytes = paths["manifest"].read_bytes()
        after_mtime_ns = paths["manifest"].stat().st_mtime_ns
        _assert(after_bytes == before_bytes, "chunk_manifest.json content was rewritten")
        _assert(after_mtime_ns == before_mtime_ns, "chunk_manifest.json mtime changed")

        payload_chunks = status_payload["manifest"]["chunks"]
        _assert([chunk["chunk_id"] for chunk in payload_chunks] == ["chunk_001", "chunk_002"], "chunk ids changed")
        _assert(all(chunk.get("display_label") for chunk in payload_chunks), "status payload did not backfill labels")


def test_running_audit_context_block() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_context_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        session["pdf_attached_in_conversation"] = True
        session["tex_path"] = None
        paths = session_paths(workdir)
        _write_json(paths["session"], session)
        _write_json(
            paths["ledger"],
            {
                "assumptions": [
                    "Throughout the previous argument, $n$ tends to infinity with $r$ fixed.",
                    "The function $G$ denotes the normalized generating function introduced in chunk_001.",
                ],
                "notes": [
                    "Definition note: admissible weights are assumed nonnegative unless explicitly stated otherwise.",
                    "Dependency note: Lemma 1 is used to justify the saddle-point localization.",
                ],
                "updated_at": NEW,
            },
        )
        _write_json(
            paths["issues"],
            {
                "next_issue_id": 2,
                "issues": [
                    {
                        "issue_id": "ISSUE-001",
                        "status": "open",
                        "severity": "critical",
                        "chunk_id": "chunk_001",
                        "title": "Uniformity gap in Lemma 1",
                        "location": "Lemma 1",
                        "description": "The bound is used later outside the stated compact regime.",
                        "evidence": "The proof only treats bounded $t$.",
                        "proposed_fix": "State and prove the uniform range.",
                        "tags": ["dependency-gap"],
                    }
                ],
                "updated_at": NEW,
            },
        )
        structured_path = workdir / "responses" / "chunk_001.structured.json"
        _write_json(
            structured_path,
            {
                "label": "chunk_001",
                "boundary": "pages 1-2",
                "chunk_too_large": False,
                "chunk_split_suggestions": [],
                "assumptions_and_notation": ["Notation: $G(z)$ is the normalized generating function."],
                "verified_steps": ["Lemma 1 is used as the localization input for later asymptotics."],
                "issues": [],
                "python_checks": [],
                "latex_patch": "",
                "ledger_updates": {"assumptions": [], "notes": []},
                "next_boundary_hint": "Next chunk begins the main saddle-point estimate.",
                "confidence": "medium",
            },
        )
        _append_jsonl(
            paths["chunk_records"],
            {
                "time": OLD,
                "chunk_id": "chunk_001",
                "chunk_index": 1,
                "label": "PDF pages 1-2: Lemma 1",
                "boundary": "pages 1-2",
                "source_kind": "pdf",
                "page_start": 1,
                "page_end": 2,
                "structured_response_path": str(structured_path),
                "issue_ids": ["ISSUE-001"],
            },
        )

        chunk = {
            "chunk_id": "chunk_002",
            "chunk_index": 2,
            "label": "PDF pages 3-4: Main estimate",
            "boundary": "pages 3-4",
            "source_kind": "pdf",
            "page_start": 3,
            "page_end": 4,
            "chunk_text": "The main estimate now applies Lemma 1 to the sequence under study.",
        }
        message = build_user_message_for_chunk(session, chunk)
        text_parts = [
            part.get("text", "")
            for part in message[0]["content"]
            if isinstance(part, dict) and part.get("type") == "input_text"
        ]
        prompt_text = "\n".join(text_parts)
        context_index = prompt_text.find("Running audit context from earlier chunks:")
        chunk_text_index = prompt_text.find("Chunk text:")
        _assert(context_index >= 0, "running context block was not inserted")
        _assert(chunk_text_index > context_index, "running context block does not appear before chunk text")
        context_text = prompt_text[context_index:chunk_text_index].strip()
        _assert(
            len(context_text) <= CONTINUOUS_RUNNING_CONTEXT_MAX_CHARS + 4,
            f"continuous running context exceeded cap: {len(context_text)}",
        )
        _assert(chunk.get("_running_context_mode") == CONTINUOUS_RUNNING_CONTEXT_PROFILE, chunk)
        _assert(chunk.get("_running_context_cap_chars") == CONTINUOUS_RUNNING_CONTEXT_MAX_CHARS, chunk)
        _assert("Throughout the previous argument" in prompt_text, "ledger assumption missing from context")
        _assert("Notation: $G(z)$" in prompt_text, "recent chunk notation missing from context")
        _assert("ISSUE-001" in prompt_text and "Uniformity gap" in prompt_text, "priority issue missing from context")
        _assert("Do not overclaim exact theorem/equation labels" in prompt_text, "PDF-only precision caution missing")

        capped_context = _build_running_audit_context_for_chunk(session, chunk, max_chars=500)
        _assert(len(capped_context) <= 504, f"context cap not respected: {len(capped_context)}")


def test_tex_macro_glossary_in_chunk_prompt() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_macros_") as tmp:
        tmp_path = Path(tmp)
        workdir = tmp_path / "paper_audit"
        session = _seed_state(workdir)
        tex_path = tmp_path / "paper.tex"
        _write_text(
            tex_path,
            r"""
\documentclass{article}
\newcommand{\Lpa}[1]{\left(#1\right)}
\newcommand{\lpa}[1]{(#1)}
\newcommand{\Unused}[1]{\mathbf{#1}}
\DeclareRobustCommand{\Stirling}[2]{\left\{#1\atop #2\right\}}
\DeclareMathOperator{\Var}{Var}
\def\tr#1{\lfloor #1\rfloor}
\newcommand{\unsafe}[1]{\begin{tikzpicture}#1\end{tikzpicture}}
\begin{document}
""",
        )
        session["tex_path"] = str(tex_path)
        session["pdf_attached_in_conversation"] = True
        paths = session_paths(workdir)
        _write_json(
            paths["ledger"],
            {
                "assumptions": ["Previous chunks use $\\tr{x}$ for the floor of $x$."],
                "notes": [],
                "updated_at": NEW,
            },
        )

        chunk = {
            "chunk_id": "chunk_002",
            "chunk_index": 2,
            "label": "TeX chunk 2",
            "boundary": "Approx. pages 2-2 based on TeX order",
            "source_kind": "tex",
            "page_start": 2,
            "page_end": 2,
            "chunk_text": r"The estimate uses $\Lpa{1+x}$, $\lpa{1+y}$, and $\Stirling{n}{k}$.",
        }
        message = build_user_message_for_chunk(session, chunk)
        prompt_text = "\n".join(
            str(part.get("text") or "")
            for part in message[0]["content"]
            if isinstance(part, dict) and part.get("type") == "input_text"
        )
        glossary_index = prompt_text.find("Paper macro glossary for this chunk:")
        chunk_text_index = prompt_text.find("Chunk text:")
        _assert(glossary_index >= 0, "macro glossary block was not inserted")
        _assert(chunk_text_index > glossary_index, "macro glossary does not appear before chunk text")
        _assert(r"\newcommand{\Lpa}" in prompt_text, "used macro \\Lpa missing from glossary")
        _assert(r"\newcommand{\lpa}" in prompt_text, "used macro \\lpa missing from glossary")
        _assert(r"\DeclareRobustCommand{\Stirling}" in prompt_text, "used macro \\Stirling missing from glossary")
        _assert(r"\def\tr" in prompt_text, "running-context macro \\tr missing from glossary")
        _assert(r"\Unused" not in prompt_text, "unused macro leaked into glossary")
        _assert("tikzpicture" not in prompt_text, "unsafe macro leaked into glossary")

        pdf_chunk = dict(chunk)
        pdf_chunk["source_kind"] = "pdf"
        pdf_prompt = "\n".join(
            str(part.get("text") or "")
            for part in build_user_message_for_chunk(session, pdf_chunk)[0]["content"]
            if isinstance(part, dict) and part.get("type") == "input_text"
        )
        _assert("Paper macro glossary for this chunk:" not in pdf_prompt, "PDF chunk unexpectedly received macro glossary")

        no_tex_session = dict(session)
        no_tex_session["tex_path"] = None
        no_tex_prompt = "\n".join(
            str(part.get("text") or "")
            for part in build_user_message_for_chunk(no_tex_session, chunk)[0]["content"]
            if isinstance(part, dict) and part.get("type") == "input_text"
        )
        _assert("Paper macro glossary for this chunk:" not in no_tex_prompt, "no-TeX session received macro glossary")


def test_request_size_diagnostics() -> None:
    session = {
        "audit_system_prompt": "Developer audit instructions.",
        "conversation_id": "conv-existing",
        "pdf_attached_in_conversation": False,
    }
    chunk = {
        "chunk_id": "chunk_002",
        "chunk_index": 2,
        "chunk_text": "Chunk body with $x$.",
    }
    user_prompt = "\n".join(
        [
            "Audit this mathematics-paper chunk rigorously.",
            "",
            "Running audit context from earlier chunks:",
            "- Prior notation: $G(z)$.",
            "End running audit context.",
            "Paper macro glossary for this chunk:",
            "- \\Lpa: \\newcommand{\\Lpa}[1]{(#1)}",
            "End paper macro glossary.",
            "",
            "Chunk text:",
            chunk["chunk_text"],
        ]
    )
    request_kwargs = {
        "conversation": "conv-existing",
        "input": [
            {"role": "developer", "content": [{"type": "input_text", "text": session["audit_system_prompt"]}]},
            {
                "role": "user",
                "content": [
                    {"type": "input_file", "file_id": "file-paper"},
                    {"type": "input_text", "text": user_prompt},
                ],
            },
        ],
    }
    diagnostics = _audit_request_size_diagnostics(session, chunk, request_kwargs)
    _assert(diagnostics["audit_system_prompt_length"] == len(session["audit_system_prompt"]), diagnostics)
    _assert(diagnostics["developer_prompt_included"], diagnostics)
    _assert(diagnostics["developer_prompt_payload_length"] == len(session["audit_system_prompt"]), diagnostics)
    _assert(diagnostics["user_prompt_length"] == len(user_prompt), diagnostics)
    _assert(diagnostics["chunk_text_length"] == len(chunk["chunk_text"]), diagnostics)
    _assert(diagnostics["running_audit_context_length"] > 0, diagnostics)
    _assert(diagnostics["tex_macro_glossary_length"] > 0, diagnostics)
    _assert(diagnostics["pdf_attachment_included"], diagnostics)
    _assert(diagnostics["conversation_state"] == "unseeded_or_new_conversation", diagnostics)

    text_only_chunk = dict(chunk)
    text_only_chunk["_pdf_text_only_retry"] = True
    text_only_session = dict(session)
    text_only_session["last_text_only_file_timeout_retry"] = {
        "chunk_id": chunk["chunk_id"],
        "previous_conversation_id": "conv-old",
    }
    text_only_request = {
        "conversation": "conv-fresh",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": user_prompt}]}],
    }
    text_only_diagnostics = _audit_request_size_diagnostics(text_only_session, text_only_chunk, text_only_request)
    _assert(not text_only_diagnostics["pdf_attachment_included"], text_only_diagnostics)
    _assert(text_only_diagnostics["text_only_fallback_active"], text_only_diagnostics)
    _assert(text_only_diagnostics["fresh_conversation_for_text_only_retry"], text_only_diagnostics)
    _assert(text_only_diagnostics["previous_conversation_id"] == "conv-old", text_only_diagnostics)
    _assert(text_only_diagnostics["conversation_state"] == "fresh_text_only_retry_conversation", text_only_diagnostics)


def test_fresh_context_mode_scaffolding() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_fresh_context_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        session["pdf_file_id"] = "file-paper"
        session["tex_path"] = None
        chunk_1 = {
            "chunk_id": "chunk_001",
            "chunk_index": 1,
            "label": "PDF pages 1-1",
            "boundary": "pages 1-1",
            "source_kind": "pdf",
            "page_start": 1,
            "page_end": 1,
            "chunk_text": "Definition 1. Let alpha be a parameter. Lemma 1 proves stability.",
        }
        audit = {
            "assumptions_and_notation": ["Definition: alpha denotes the main stability parameter."],
            "verified_steps": ["Lemma 1 depends on Definition 1 and is locally consistent."],
            "issues": [],
            "ledger_updates": {
                "assumptions": ["Assume alpha > 0 throughout the stability argument."],
                "notes": ["Lemma 1 is a dependency for the next theorem."],
            },
            "next_boundary_hint": "The next chunk starts applying Lemma 1.",
            "confidence": "high",
        }
        created_issues = [
            {
                "issue_id": "I001",
                "severity": "high",
                "status": "open",
                "title": "Check dependency on Lemma 1",
                "location": "chunk_001",
                "description": "Later chunks may rely on this stability dependency.",
            }
        ]
        entries = _append_audit_context_db_entries(session, chunk_1, audit, created_issues)
        kinds = {entry.get("kind") for entry in entries}
        _assert("chunk_summary" in kinds, kinds)
        _assert("definition" in kinds or "notation" in kinds, kinds)
        _assert("verified_step" in kinds, kinds)
        _assert("issue" in kinds, kinds)
        _assert("next_boundary_hint" in kinds, kinds)
        context_path = session_paths(workdir)["audit_context_db"]
        _assert(context_path.exists(), "context DB was not written")

        chunk_2 = {
            "chunk_id": "chunk_002",
            "chunk_index": 2,
            "label": "PDF pages 2-2",
            "boundary": "pages 2-2; Theorem 2",
            "source_kind": "pdf",
            "page_start": 2,
            "page_end": 2,
            "chunk_text": "Theorem 2 uses alpha and Lemma 1.",
        }
        retrieved = build_fresh_audit_context_for_chunk(session, chunk_2)
        _assert(retrieved["entry_count"] > 0, retrieved)
        _assert("Retrieved fresh-context audit database context:" in retrieved["block"], retrieved["block"])

        continuous_session = dict(session)
        continuous_session.pop("audit_context_mode", None)
        continuous_prompt = build_user_message_for_chunk(continuous_session, dict(chunk_2))
        explicit_continuous = dict(session)
        explicit_continuous["audit_context_mode"] = DEFAULT_AUDIT_CONTEXT_MODE
        explicit_prompt = build_user_message_for_chunk(explicit_continuous, dict(chunk_2))
        _assert(continuous_prompt == explicit_prompt, "explicit continuous mode changed prompt construction")

        fresh_session = dict(session)
        fresh_session["audit_context_mode"] = AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL
        fresh_chunk = dict(chunk_2)
        fresh_prompt = build_user_message_for_chunk(fresh_session, fresh_chunk)
        prompt_text = "\n".join(
            str(part.get("text") or "")
            for part in fresh_prompt[0]["content"]
            if isinstance(part, dict) and part.get("type") == "input_text"
        )
        _assert("Retrieved fresh-context audit database context:" in prompt_text, prompt_text)
        _assert(FRESH_CONTEXT_PRIOR_ISSUE_CAUTION in prompt_text, prompt_text)
        _assert("prior audit issue (provisional)" in prompt_text, prompt_text)
        _assert("Fresh-context verification reminder:" in prompt_text, prompt_text)
        _assert("include python_checks when a local symbolic or numerical sanity check can materially test" in prompt_text, prompt_text)
        _assert(prompt_text.index("Retrieved fresh-context audit database context:") < prompt_text.index("Chunk text:"), prompt_text)
        _assert(fresh_chunk.get("_retrieved_context_entry_count", 0) > 0, fresh_chunk)
        _assert(fresh_chunk.get("_running_context_mode") == FRESH_CONTEXT_RETRIEVAL_PROFILE, fresh_chunk)
        _assert(int(fresh_chunk.get("_retrieved_context_cap_chars") or 0) >= 10000, fresh_chunk)

        fresh_chunk["_fresh_context_conversation"] = True
        fresh_chunk["_fresh_context_conversation_id"] = "conv-fresh"
        fresh_chunk["_suppress_pdf_attachment"] = True
        fresh_chunk["_pdf_attachment_disabled_note"] = FRESH_CONTEXT_TEXT_FIRST_NOTE
        request_kwargs = {
            "conversation": "conv-fresh",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt_text}]}],
        }
        diagnostics = _audit_request_size_diagnostics(fresh_session, fresh_chunk, request_kwargs)
        _assert(diagnostics["audit_context_mode"] == AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL, diagnostics)
        _assert(diagnostics["fresh_context_conversation"], diagnostics)
        _assert(diagnostics["pdf_attachment_suppressed"], diagnostics)
        _assert(diagnostics["retrieved_context_entry_count"] > 0, diagnostics)
        _assert(diagnostics["running_context_mode"] == FRESH_CONTEXT_RETRIEVAL_PROFILE, diagnostics)
        _assert(diagnostics["retrieved_context_cap_chars"] >= 10000, diagnostics)
        request_path = _save_request_metadata(
            fresh_session,
            fresh_chunk,
            request_kwargs,
            verification_mode="local_python_only",
            used_code_interpreter_tool=False,
        )
        saved_request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        _assert(saved_request["audit_context_mode"] == AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL, saved_request)
        _assert(saved_request["fresh_context"]["retrieved_context_entry_count"] > 0, saved_request)


def test_fresh_context_issue_retrieval_downweights_generic_terms() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_fresh_issue_scoring_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        session["pdf_file_id"] = "file-paper"
        session["audit_context_mode"] = AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL
        source_chunk = {
            "chunk_id": "chunk_001",
            "chunk_index": 1,
            "label": "PDF pages 1-1",
            "boundary": "pages 1-1",
            "source_kind": "pdf",
            "page_start": 1,
            "page_end": 1,
            "chunk_text": "Initial estimates.",
        }
        audit = {
            "assumptions_and_notation": [],
            "verified_steps": [],
            "issues": [],
            "ledger_updates": {"assumptions": [], "notes": []},
            "next_boundary_hint": "",
        }
        _append_audit_context_db_entries(
            session,
            source_chunk,
            audit,
            [
                {
                    "issue_id": "I999",
                    "severity": "critical",
                    "status": "open",
                    "title": "Old generic estimate concern",
                    "description": "Equation lambda k n error bound estimate term expression asymptotic.",
                },
                {
                    "issue_id": "I100",
                    "severity": "high",
                    "status": "open",
                    "title": "Bessel transform dependency",
                    "description": "The Bessel transform kernel identity may affect later Bessel transform estimates.",
                },
            ],
        )
        current_chunk = {
            "chunk_id": "chunk_020",
            "chunk_index": 20,
            "label": "PDF pages 20-20",
            "boundary": "pages 20-20; Proposition 20",
            "source_kind": "pdf",
            "page_start": 20,
            "page_end": 20,
            "chunk_text": (
                "Proposition 20 revisits a Bessel transform identity. "
                "The equation has an asymptotic error bound estimate term expression."
            ),
        }
        retrieved = build_fresh_audit_context_for_chunk(session, current_chunk)
        _assert("I100" in retrieved["block"], retrieved["block"])
        _assert("I999" not in retrieved["block"], retrieved["block"])


def test_context_mode_mixing_guardrails() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_context_mode_guard_") as tmp:
        root = Path(tmp)
        pdf_path = root / "paper.pdf"
        _write_text(pdf_path, "%PDF synthetic placeholder")
        workdir = pdf_path.with_name(pdf_path.stem + "_audit")
        session = _seed_state(workdir)
        session["pdf_path"] = str(pdf_path)
        session["audit_context_mode"] = DEFAULT_AUDIT_CONTEXT_MODE
        _write_json(session_paths(workdir)["session"], session)

        _assert(
            _existing_session_audit_context_mode(session, None) == DEFAULT_AUDIT_CONTEXT_MODE,
            session,
        )
        _assert(
            _existing_session_audit_context_mode(session, DEFAULT_AUDIT_CONTEXT_MODE) == DEFAULT_AUDIT_CONTEXT_MODE,
            session,
        )
        try:
            _existing_session_audit_context_mode(session, AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL)
        except RuntimeError as exc:
            _assert("Cannot change audit_context_mode" in str(exc), str(exc))
        else:
            raise RegressionFailure("Runtime helper allowed context-mode change on existing session")

        try:
            runtime.audit_the_paper(
                pdf_path,
                continue_existing=True,
                audit_context_mode=AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL,
                verbose=False,
            )
        except RuntimeError as exc:
            _assert("Cannot change audit_context_mode" in str(exc), str(exc))
        else:
            raise RegressionFailure("audit_the_paper allowed context-mode change on existing session")

        saved = json.loads(session_paths(workdir)["session"].read_text(encoding="utf-8"))
        _assert(saved["audit_context_mode"] == DEFAULT_AUDIT_CONTEXT_MODE, saved)

        mismatch = fresh_start_context_mode_mismatch_info(str(pdf_path), AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL)
        _assert(mismatch.get("saved_mode") == DEFAULT_AUDIT_CONTEXT_MODE, mismatch)
        _assert(mismatch.get("selected_mode") == AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL, mismatch)
        _assert(Path(str(mismatch.get("workdir") or "")).resolve() == workdir.resolve(), mismatch)
        _assert(
            not fresh_start_context_mode_mismatch_info(str(pdf_path), DEFAULT_AUDIT_CONTEXT_MODE),
            "same-mode fresh start should not need a mode-mismatch warning",
        )


def test_chunk_completion_log_line_formatting() -> None:
    payload = {
        "status": {
            "current_chunk_id": "chunk_012",
            "chunks_completed": 12,
            "chunks_total": 81,
            "estimated_pages_completed": 7,
            "estimated_pages_total": 46,
            "cost_usd": 12.3456,
            "total_audit_seconds": 1450.0,
        },
        "usage": {
            "totals": {
                "cost_usd": 12.3456,
                "audit_seconds": 1450.0,
                "total_tokens": 1234567,
            }
        },
    }
    usage_entry = {
        "time": NEW,
        "chunk_id": "chunk_012",
        "usage": {"total_tokens": 43210},
        "elapsed_seconds": 102.0,
        "cost": {"total_cost": 0.8421},
    }
    line = format_chunk_completion_log_line(payload, usage_entry)
    _assert(line.startswith("[chunk_012] completed"), line)
    _assert("Progress: 12/81" in line, line)
    _assert("Pages: 7/46" in line, line)
    _assert("Chunk time: 1m 42s" in line, line)
    _assert("Chunk cost: $0.8421" in line, line)
    _assert("Cumulative cost: $12.3456" in line, line)
    _assert("Total audit time: 24m 10s" in line, line)
    _assert("Chunk tokens: 43210" in line, line)
    _assert("Cumulative tokens: 1234567" in line, line)
    _assert("Total tokens:" not in line, line)

    started = format_running_chunk_started_log_line(
        {
            "current_chunk_id": "chunk_013",
            "chunks_completed": 12,
            "chunks_total": 81,
        }
    )
    _assert(started == "[chunk_013] started | Progress: 12/81", started)


def test_plain_text_scroll_preservation_helper() -> None:
    from gui_main_window import _set_plain_text_preserving_scroll

    class FakeScrollBar:
        def __init__(self, value: int, maximum: int) -> None:
            self._value = value
            self._maximum = maximum

        def value(self) -> int:
            return self._value

        def maximum(self) -> int:
            return self._maximum

        def setValue(self, value: int) -> None:
            self._value = int(value)

    class FakePlainText:
        def __init__(self, text: str, value: int, maximum: int) -> None:
            self._text = text
            self.scrollbar = FakeScrollBar(value, maximum)
            self.set_count = 0
            self.next_maximum = maximum

        def toPlainText(self) -> str:
            return self._text

        def verticalScrollBar(self) -> FakeScrollBar:
            return self.scrollbar

        def setPlainText(self, text: str) -> None:
            self.set_count += 1
            self._text = text
            self.scrollbar._maximum = self.next_maximum

    unchanged = FakePlainText("same", value=30, maximum=100)
    _assert(not _set_plain_text_preserving_scroll(unchanged, "same"), "unchanged text was rewritten")
    _assert(unchanged.set_count == 0, "unchanged text reset the widget")
    _assert(unchanged.scrollbar.value() == 30, "unchanged text moved the scrollbar")

    changed = FakePlainText("old", value=40, maximum=100)
    changed.next_maximum = 90
    _assert(_set_plain_text_preserving_scroll(changed, "new"), "changed text was not written")
    _assert(changed.set_count == 1, "changed text was not set exactly once")
    _assert(changed.scrollbar.value() == 40, "scrollbar position was not preserved")

    bottom = FakePlainText("old", value=100, maximum=100)
    bottom.next_maximum = 180
    _assert(_set_plain_text_preserving_scroll(bottom, "new longer text"), "bottom text was not written")
    _assert(bottom.scrollbar.value() == 180, "bottom scroll position did not stay at bottom")


def test_review_tab_feature_flag() -> None:
    from gui_main_window import review_tab_enabled

    _assert(not review_tab_enabled({}), "Review tab should be hidden by default")
    _assert(not review_tab_enabled({"MATH_AUDIT_ENABLE_REVIEW_TAB": "0"}), "Only exact value 1 should enable Review tab")
    _assert(review_tab_enabled({"MATH_AUDIT_ENABLE_REVIEW_TAB": "1"}), "Review tab env flag did not enable")
    _assert(not review_tab_enabled({"MATH_AUDIT_ENABLE_REVIEW_TAB": "true"}), "Non-1 flag should not enable Review tab")


def test_completed_status_reconciles_from_chunk_records() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_status_reconcile_") as tmp:
        root = Path(tmp)
        pdf_path = root / "paper.pdf"
        _write_text(pdf_path, "%PDF synthetic placeholder")
        workdir = pdf_path.with_name(pdf_path.stem + "_audit")
        session = _seed_state(workdir)
        session["pdf_path"] = str(pdf_path)
        session["audit_finished_at"] = NEW
        _write_json(session_paths(workdir)["session"], session)
        manifest = {
            "chunks": [
                {"chunk_id": "chunk_001", "chunk_index": 1, "page_end": 1, "paper_progress_end": 0.5},
                {"chunk_id": "chunk_002", "chunk_index": 2, "page_end": 2, "paper_progress_end": 1.0},
            ],
            "pdf_page_count": 2,
        }
        _write_json(session_paths(workdir)["manifest"], manifest)
        _write_json(
            session_paths(workdir)["status"],
            {
                "status": "paused",
                "pause_reason": "chunk_failed",
                "current_chunk_id": None,
                "chunks_completed": 1,
                "chunks_total": 2,
                "estimated_pages_completed": 1,
                "estimated_pages_total": 2,
                "progress_pct": 50.0,
                "cost_usd": 0.0,
                "updated_at": OLD,
            },
        )
        _append_jsonl(session_paths(workdir)["chunk_records"], {"chunk_id": "chunk_001", "chunk_index": 1})
        _append_jsonl(session_paths(workdir)["chunk_records"], {"chunk_id": "chunk_002", "chunk_index": 2})
        payload = get_audit_status(pdf_path)
        status = payload["status"]
        _assert(status["status"] == "completed", status)
        _assert(status["chunks_completed"] == 2, status)
        _assert(status["estimated_pages_completed"] == 2, status)
        _assert(status.get("reconciled_from_chunk_records"), status)
        saved_status = json.loads(session_paths(workdir)["status"].read_text(encoding="utf-8"))
        _assert(saved_status["status"] == "paused", saved_status)


def test_persistent_audit_log_preview() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_log_preview_") as tmp:
        root = Path(tmp)
        pdf_path = root / "paper.pdf"
        _write_text(pdf_path, "%PDF synthetic placeholder")
        workdir = pdf_path.with_name(pdf_path.stem + "_audit")
        session = _seed_state(workdir)
        session["pdf_path"] = str(pdf_path)
        _write_json(session_paths(workdir)["session"], session)
        _append_jsonl(workdir / "logs" / "selected_chunk_reruns.jsonl", {"time": OLD, "action": "started", "chunk_ids": ["chunk_001"]})
        _append_jsonl(workdir / "logs" / "selected_chunk_reruns.jsonl", {"time": NEW, "action": "finished", "chunk_ids": ["chunk_001", "chunk_002"]})
        _append_jsonl(workdir / "logs" / "failed_chunks.jsonl", {"time": MID, "action": "failed", "chunk_id": "chunk_002", "error": "boom"})
        preview = persistent_audit_log_preview(str(pdf_path), max_entries=2)
        text = "\n".join(preview)
        _assert("Historical audit logs found:" in text, text)
        _assert("Logs folder:" in text, text)
        _assert("Recent persistent audit events" in text, text)
        _assert("failed_chunks.jsonl: failed (chunk_002) - error recorded" in text, text)
        _assert("selected_chunk_reruns.jsonl: finished (chunk_001, chunk_002)" in text, text)


def test_resume_preserves_saved_audit_context_mode() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_resume_context_") as tmp:
        root = Path(tmp)
        pdf_path = root / "paper.pdf"
        _write_text(pdf_path, "%PDF synthetic placeholder")
        workdir = pdf_path.with_name(pdf_path.stem + "_audit")
        session = _seed_state(workdir)
        session["pdf_path"] = str(pdf_path)
        session["reasoning_effort"] = "xhigh"
        session["audit_context_mode"] = AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL
        session["pause_requested_at"] = MID
        _write_json(session_paths(workdir)["session"], session)
        _write_json(
            session_paths(workdir)["manifest"],
            {
                "chunks": [
                    {
                        "chunk_id": "chunk_001",
                        "chunk_index": 1,
                        "label": "Synthetic chunk",
                        "boundary": "pages 1-1",
                        "source_kind": "pdf",
                        "page_start": 1,
                        "page_end": 1,
                        "paper_progress_end": 1.0,
                        "chunk_text": "Synthetic text.",
                    }
                ],
                "pdf_page_count": 1,
            },
        )
        result = runtime.resume_audit(pdf_path, verbose=False)
        saved = json.loads(session_paths(workdir)["session"].read_text(encoding="utf-8"))
        _assert(result["pause_result"]["reason"] == "requested", result)
        _assert(saved["audit_context_mode"] == AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL, saved)


def test_discussion_legacy_thread_and_context_db_safety() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_discussion_safety_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        paths = session_paths(workdir)

        empty_legacy = dict(session)
        empty_legacy["conversation_id"] = "conv-audit-main"
        empty_legacy["pdf_attached_in_conversation"] = True
        empty_legacy.pop("qa_threads", None)
        _write_json(paths["session"], empty_legacy)
        ensured = runtime._ensure_qa_thread_state(empty_legacy)
        legacy = ensured["qa_threads"]["thread_legacy"]
        _assert(legacy.get("conversation_id") is None, legacy)
        _assert(legacy.get("pdf_attached_in_conversation") is False, legacy)

        with_history = dict(session)
        with_history["conversation_id"] = "conv-audit-main"
        with_history.pop("qa_threads", None)
        _write_json(paths["session"], with_history)
        _write_json(
            workdir / "qa" / "qa_001.json",
            {
                "time": OLD,
                "turn_id": "qa_001",
                "thread_id": "thread_legacy",
                "conversation_id": "conv-discussion-legacy",
                "question": "What is the main issue?",
                "answer": "Saved answer.",
            },
        )
        ensured_history = runtime._ensure_qa_thread_state(with_history)
        legacy_history = ensured_history["qa_threads"]["thread_legacy"]
        _assert(legacy_history.get("conversation_id") == "conv-discussion-legacy", legacy_history)

        existing_history = copy.deepcopy(ensured_history)
        existing_history["qa_threads"]["thread_legacy"]["conversation_id"] = "conv-existing-discussion"
        ensured_existing = runtime._ensure_qa_thread_state(existing_history)
        _assert(
            ensured_existing["qa_threads"]["thread_legacy"].get("conversation_id") == "conv-existing-discussion",
            ensured_existing["qa_threads"]["thread_legacy"],
        )

        context_path = paths["audit_context_db"]
        _append_jsonl(
            context_path,
            {
                "entry_id": "chunk_001:def:001",
                "kind": "definition",
                "text": "Definition: alpha denotes the stability parameter used in Theorem 2.",
                "source_chunk_id": "chunk_001",
                "source_chunk_index": 1,
                "page_start": 1,
                "page_end": 2,
                "confidence": "source-derived",
            },
        )
        _append_jsonl(
            context_path,
            {
                "entry_id": "chunk_002:dependency:001",
                "kind": "dependency",
                "text": "Theorem 2 depends on the alpha stability estimate from Lemma 1.",
                "source_chunk_id": "chunk_002",
                "source_chunk_index": 2,
                "page_start": 2,
                "page_end": 3,
                "confidence": "source-derived",
            },
        )
        _append_jsonl(
            context_path,
            {
                "entry_id": "chunk_003:issue:001",
                "kind": "issue",
                "text": "Potential high-impact dependency gap involving Theorem 2 and alpha.",
                "source_chunk_id": "chunk_003",
                "source_chunk_index": 3,
                "page_start": 3,
                "page_end": 4,
                "issue_id": "I001",
                "severity": "high",
                "status": "open",
                "confidence": "source-derived",
            },
        )

        full_context = runtime._build_full_audit_qa_context(session, "Does Theorem 2 depend on alpha?")
        _assert("Audit context database summary (compact):" in full_context, full_context)
        _assert("definition=1" in full_context, full_context)
        _assert("dependency=1" in full_context, full_context)
        _assert("Prior audit issue entries are provisional findings" in full_context, full_context)
        _assert("prior audit issue (provisional)" in full_context, full_context)

        paper_structure = runtime._build_paper_structure_context(session)
        context_db = paper_structure.get("audit_context_db") or {}
        _assert(context_db.get("available") is True, context_db)
        _assert(context_db.get("counts_by_kind", {}).get("definition") == 1, context_db)
        _assert(context_db.get("counts_by_kind", {}).get("dependency") == 1, context_db)
        selected = context_db.get("selected_entries") or []
        _assert(selected, context_db)
        issue_entries = [entry for entry in selected if entry.get("kind") == "issue"]
        _assert(issue_entries and issue_entries[0].get("provisional") is True, selected)

    with tempfile.TemporaryDirectory(prefix="math_audit_discussion_no_context_db_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        full_context = runtime._build_full_audit_qa_context(session, "Any context DB?")
        _assert("Audit context database summary (compact):" not in full_context, full_context)
        paper_structure = runtime._build_paper_structure_context(session)
        context_db = paper_structure.get("audit_context_db") or {}
        _assert(context_db.get("available") is False, context_db)
        _assert(context_db.get("total_entries") == 0, context_db)


def test_report_latex_unicode_math_safety() -> None:
    text = (
        "The bound is $c_2√nΛ≤1/2$ and $√(n+1)≥λ$. "
        "Literal control-escape artifacts: $\\rho k\\u000b\\lambda$ and $0le l\\u0007le j$."
    )
    for renderer in (_report_latex_paragraph_local, report_latex_paragraph):
        rendered = renderer(text)
        _assert("√" not in rendered, rendered)
        _assert(r"\sqrt{n}" in rendered, rendered)
        _assert(r"\sqrt{n+1}" in rendered, rendered)
        _assert(r"\Lambda" in rendered, rendered)
        _assert(r"\lambda" in rendered, rendered)
        _assert(r"\le" in rendered, rendered)
        _assert(r"\ge" in rendered, rendered)
        _assert(r"\u000b" not in rendered, rendered)
        _assert(r"\u0007" not in rendered, rendered)
        _assert("\\\\Lambda" not in rendered, rendered)
        _assert("\\\\lambda" not in rendered, rendered)

        lmj_unicode_math = (
            "Lemma 11 gives an unconditional $11$-adic valuation that requires $5∣a+1$. "
            "Take $a=13$ with $a+1=14=2·7$ and $10∤14$; "
            "the formula $ϑ_{11}(σ(13^a))$ involves $Ω(σ(n))$."
        )
        rendered_lmj = renderer(lmj_unicode_math)
        _assert(r"\$11\$" not in rendered_lmj, rendered_lmj)
        _assert("[U+" not in rendered_lmj, rendered_lmj)
        _assert("$11$-adic" in rendered_lmj, rendered_lmj)
        _assert(r"$5\mid a+1$" in rendered_lmj, rendered_lmj)
        _assert(r"$10\nmid 14$" in rendered_lmj, rendered_lmj)
        _assert(r"\cdot 7" in rendered_lmj, rendered_lmj)
        _assert(r"\vartheta " in rendered_lmj, rendered_lmj)
        _assert(r"\sigma " in rendered_lmj, rendered_lmj)
        _assert(r"\Omega " in rendered_lmj, rendered_lmj)

        jnt_pdf_artifacts = (
            r"The parsed text produced $1\wed\ge 0$, $k\bi\ge0$, "
            r"$p\bm\le m$, and $2\bm\delta$."
        )
        rendered_jnt = renderer(jnt_pdf_artifacts)
        _assert(r"\wed" not in rendered_jnt, rendered_jnt)
        _assert(r"\bi" not in rendered_jnt, rendered_jnt)
        _assert(r"\bm\le" not in rendered_jnt, rendered_jnt)
        _assert(r"\bm\delta" not in rendered_jnt, rendered_jnt)
        _assert(r"\textbackslash{}wed" in rendered_jnt, rendered_jnt)
        _assert(r"\textbackslash{}bi" in rendered_jnt, rendered_jnt)

        lmj_dangling_subscript = r"LMJ extracted notation produced $S^+_$ in a ledger item."
        rendered_lmj_dangling = renderer(lmj_dangling_subscript)
        _assert("$S^+_$" not in rendered_lmj_dangling, rendered_lmj_dangling)
        _assert(r"\texttt{" in rendered_lmj_dangling, rendered_lmj_dangling)
        _assert(r"\textasciicircum{}+\_" in rendered_lmj_dangling, rendered_lmj_dangling)

        lmj_double_subscript = r"LMJ extracted notation produced $\sum_{p\mid N}_p(N)=\Omega(N)$."
        rendered_lmj_double = renderer(lmj_double_subscript)
        _assert(r"$\sum_{p\mid N}_p(N)" not in rendered_lmj_double, rendered_lmj_double)
        _assert(r"\texttt{" in rendered_lmj_double, rendered_lmj_double)
        _assert(r"\textbackslash{}sum" in rendered_lmj_double, rendered_lmj_double)

        lmj_nested_double_subscript = (
            r"LMJ extracted notation produced "
            r"$\sum_{p\mid 2^{a+1}-1}_p(2^{a+1}-1)\le a-1$."
        )
        rendered_lmj_nested = renderer(lmj_nested_double_subscript)
        _assert(r"$\sum_{p\mid 2^{a+1}-1}_p" not in rendered_lmj_nested, rendered_lmj_nested)
        _assert(r"\texttt{" in rendered_lmj_nested, rendered_lmj_nested)
        _assert(r"\textbackslash{}sum" in rendered_lmj_nested, rendered_lmj_nested)

        legitimate_math = (
            r"Legitimate math should stay live: "
            r"$\frac{\rho+\lambda}{\prod_{p\le n}p}\ge\sqrt{\delta+\alpha}$ "
            r"and $\bm{\lambda}\le\mathbf{x}$, with $S^-_\sigma$ allowed "
            r"and $a+1=2^u3^v l$ allowed."
        )
        rendered_legitimate = renderer(legitimate_math)
        for token in (
            r"\frac",
            r"\rho",
            r"\lambda",
            r"\prod",
            r"\le",
            r"\ge",
            r"\sqrt",
            r"\delta",
            r"\bm{\lambda}",
            r"S^-_\sigma",
            r"a+1=2^u3^v l",
        ):
            _assert(token in rendered_legitimate, rendered_legitimate)
        _assert(r"\textbackslash{}frac" not in rendered_legitimate, rendered_legitimate)

        i080_valid_math = (
            r"Example: $n=p^s\prod_{i=1}^s(2q_i-1)$, "
            r"$\sigma(p^{s+1})$, $1+p+\cdots+p^{s+1}$, "
            r"$\Omega(n)$, and $3\times13$."
        )
        rendered_i080_valid = renderer(i080_valid_math)
        _assert(r"\prod_{i=1}^s" in rendered_i080_valid, rendered_i080_valid)
        _assert(r"\sigma(p^{s+1})" in rendered_i080_valid, rendered_i080_valid)
        _assert(r"\cdots" in rendered_i080_valid, rendered_i080_valid)
        _assert(r"\Omega(n)" in rendered_i080_valid, rendered_i080_valid)
        _assert(r"\times13" in rendered_i080_valid, rendered_i080_valid)

        i080_persisted_artifacts = (
            "Artifact: $n=p^s"
            + "\x04"
            + "prod_{i=1}^s(2q_i-1)$, $1+p+cdots+p^{s+1}=sigma(p^{s+1})$, "
            + "$n\n"
            + "i S_sigma^{s+1}$, $p\n"
            + "e 13$, and $n=3\n"
            + "times13=39$."
        )
        rendered_i080_artifacts = renderer(i080_persisted_artifacts)
        _assert(r"\prod_{i=1}^s" in rendered_i080_artifacts, rendered_i080_artifacts)
        _assert(r"\cdots" in rendered_i080_artifacts, rendered_i080_artifacts)
        _assert(r"\sigma(p^{s+1})" in rendered_i080_artifacts, rendered_i080_artifacts)
        _assert(r"\ni S_\sigma" in rendered_i080_artifacts, rendered_i080_artifacts)
        _assert(r"\ne 13" in rendered_i080_artifacts, rendered_i080_artifacts)
        _assert(r"\times13" in rendered_i080_artifacts, rendered_i080_artifacts)
        _assert("[U+" not in rendered_i080_artifacts, rendered_i080_artifacts)

        json_escaped_tex = (
            "Recovered commands: $e^{-\\rho j}\\exp(-"
            + "\x0c"
            + "rac{\\rho j^2}{2k})+"
            + "\x08"
            + "lambda$."
        )
        rendered_json_escaped = renderer(json_escaped_tex)
        _assert(r"\frac{\rho j^2}{2k}" in rendered_json_escaped, rendered_json_escaped)
        _assert(r"\lambda" in rendered_json_escaped, rendered_json_escaped)
        _assert(r"\blambda" not in rendered_json_escaped, rendered_json_escaped)
        _assert(r"\\frac" not in rendered_json_escaped, rendered_json_escaped)
        _assert(r"\\blambda" not in rendered_json_escaped, rendered_json_escaped)

        persisted_json_escaped_tex = r"Persisted commands: $-\exp(-\\frac{\rho j^2}{2k})+\\beta$."
        rendered_persisted = renderer(persisted_json_escaped_tex)
        _assert(r"\frac{\rho j^2}{2k}" in rendered_persisted, rendered_persisted)
        _assert(r"\beta" in rendered_persisted, rendered_persisted)
        _assert(r"\\frac" not in rendered_persisted, rendered_persisted)
        _assert(r"\\beta" not in rendered_persisted, rendered_persisted)

        persisted_hat_artifact = r"Recovered hat artifact: $hat\\blambda=lambda+a/k$ and $\u0007hat\blambda$."
        rendered_hat_artifact = renderer(persisted_hat_artifact)
        _assert(r"\hat\lambda=\lambda+a/k" in rendered_hat_artifact, rendered_hat_artifact)
        _assert(r"\hat\lambda" in rendered_hat_artifact, rendered_hat_artifact)
        _assert(r"\blambda" not in rendered_hat_artifact, rendered_hat_artifact)
        _assert(r"\u0007" not in rendered_hat_artifact, rendered_hat_artifact)

        decoded_hat_artifact = (
            "Recovered decoded hat: $"
            + "\x07"
            + "hat"
            + "\x08"
            + "lambda=lambda$."
        )
        rendered_decoded_hat = renderer(decoded_hat_artifact)
        _assert(r"\hat\lambda=\lambda" in rendered_decoded_hat, rendered_decoded_hat)
        _assert(r"\blambda" not in rendered_decoded_hat, rendered_decoded_hat)

        malformed = r"Replace $for any fixed $s\ne1$$ by $for each fixed integer $s\ne1$$."
        rendered_malformed = renderer(malformed)
        _assert("$$" not in rendered_malformed, rendered_malformed)
        _assert(r"\$for any fixed" in rendered_malformed, rendered_malformed)

        mathjax_only = r"$B_n$ uses $e^{e^z-1}=\nobreak\require{cancel}\notag\text{bad}$."
        rendered_mathjax = renderer(mathjax_only)
        _assert(r"\require{cancel}" not in rendered_mathjax, rendered_mathjax)
        _assert(r"\textbackslash{}require" in rendered_mathjax, rendered_mathjax)

        escaped_dollar_artifacts = (
            r'The page says "where $\$ : \mathbb N\to\mathbb R$ is given by '
            r'$\$(n,m):=\log(n)/\log(m)$." '
            r"The constant depends on $x=\$(n,m)$."
        )
        rendered_escaped_dollars = renderer(escaped_dollar_artifacts)
        _assert(r"$\textbackslash{}$" not in rendered_escaped_dollars, rendered_escaped_dollars)
        _assert(r"\textbackslash{}$(n,m)" not in rendered_escaped_dollars, rendered_escaped_dollars)
        _assert(r"\$\textbackslash{}\$" in rendered_escaped_dollars, rendered_escaped_dollars)

        unsupported_unicode = "Tag: l格range-inversion."
        rendered_unicode = renderer(unsupported_unicode)
        _assert("格" not in rendered_unicode, rendered_unicode)
        _assert("[U+683C]" in rendered_unicode, rendered_unicode)

    verbatim = runtime._verbatim_block('print("max_E≈1")  # l格range')
    _assert("≈" not in verbatim, verbatim)
    _assert("格" not in verbatim, verbatim)
    _assert(r"\approx" in verbatim, verbatim)
    _assert("[U+683C]" in verbatim, verbatim)

    with tempfile.TemporaryDirectory(prefix="math_audit_latex_log_health_") as tmp:
        tex_path = Path(tmp) / "report.tex"
        json_path = Path(tmp) / "report.json"
        log_path = Path(tmp) / "report.log"
        _write_text(tex_path, r"\documentclass{article}\begin{document}Synthetic\end{document}" + "\n")
        _write_json(json_path, {"generated_at": OLD})
        health = refresh_report_latex_compile_health_sidecar(tex_path, json_path)
        _assert(health.get("status") == "not_compiled", str(health))
        sidecar = json.loads(json_path.read_text(encoding="utf-8"))
        _assert(sidecar.get("latex_compile_health", {}).get("status") == "not_compiled", str(sidecar))

        _write_text(
            log_path,
            "./report.tex:12: Undefined control sequence.\n"
            r"l.12 $1\wed" + "\n"
            "! Missing $ inserted.\n"
            "! Missing { inserted.\n"
            "! Double subscript.\n",
        )
        health = report_latex_compile_health(tex_path)
        _assert(health.get("status") == "compile_errors", str(health))
        _assert(health.get("serious_error_count") == 4, str(health))
        _assert("Undefined control sequence" in (health.get("serious_errors") or [{}])[0].get("text", ""), str(health))
        refreshed = refresh_report_latex_compile_health_sidecar(tex_path, json_path)
        _assert(refreshed.get("status") == "compile_errors", str(refreshed))
        sidecar = json.loads(json_path.read_text(encoding="utf-8"))
        _assert(sidecar.get("latex_compile_health", {}).get("status") == "compile_errors", str(sidecar))

        _write_text(log_path, "Output written on report.pdf (1 page).\n")
        health = report_latex_compile_health(tex_path)
        _assert(health.get("status") == "clean", str(health))
        refreshed = refresh_report_latex_compile_health_sidecar(tex_path, json_path)
        _assert(refreshed.get("status") == "clean", str(refreshed))
        sidecar = json.loads(json_path.read_text(encoding="utf-8"))
        _assert(sidecar.get("latex_compile_health", {}).get("status") == "clean", str(sidecar))

    with tempfile.TemporaryDirectory(prefix="math_audit_report_health_freshness_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        reports_dir = workdir / "reports"
        tex_path = reports_dir / "paper_audit_report.tex"
        json_path = reports_dir / "paper_audit_report.json"
        log_path = reports_dir / "paper_audit_report.log"
        _write_text(tex_path, r"\documentclass{article}\begin{document}Synthetic\end{document}" + "\n")
        _write_text(log_path, "Output written on paper_audit_report.pdf (1 page).\n")
        _write_json(
            json_path,
            {
                "generated_at": NEW,
                "latex_compile_health": {
                    "tex_path": str(tex_path),
                    "log_path": str(log_path),
                    "log_available": False,
                    "status": "not_compiled",
                    "serious_error_count": 0,
                    "serious_errors": [],
                    "warning": "",
                },
            },
        )
        freshness = get_report_freshness(session)
        _assert(freshness.get("reports", {}).get("full", {}).get("generated_at") == NEW, str(freshness))
        sidecar = json.loads(json_path.read_text(encoding="utf-8"))
        _assert(sidecar.get("latex_compile_health", {}).get("status") == "clean", str(sidecar))


def test_issue_severity_summary_in_audit_summary() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_issue_summary_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        paths = session_paths(workdir)
        _write_json(
            paths["issues"],
            {
                "next_issue_id": 7,
                "issues": [
                    {"issue_id": "I001", "severity": "critical", "status": "open"},
                    {"issue_id": "I002", "severity": "high", "status": "open"},
                    {"issue_id": "I003", "severity": "medium", "status": "resolved"},
                    {"issue_id": "I004", "severity": "low", "status": "open"},
                    {"issue_id": "I005", "severity": "severe", "status": "open"},
                    {"issue_id": "I006", "severity": "high", "status": "closed"},
                ],
                "updated_at": NEW,
            },
        )

        full_summary = _audit_summary_markdown(session)
        _assert("- Issue severity summary: all saved issues" in full_summary, full_summary)
        _assert("- Critical: 1" in full_summary, full_summary)
        _assert("- High: 2" in full_summary, full_summary)
        _assert("- Medium: 1" in full_summary, full_summary)
        _assert("- Low: 1" in full_summary, full_summary)
        _assert("- Unknown severity: 1" in full_summary, full_summary)
        _assert("- Total issues: 6" in full_summary, full_summary)

        concise_summary = _audit_summary_markdown(session, issue_summary_open_only=True)
        _assert("- Open issue severity summary: open issues only" in concise_summary, concise_summary)
        _assert("- Critical: 1" in concise_summary, concise_summary)
        _assert("- High: 1" in concise_summary, concise_summary)
        _assert("- Medium: 0" in concise_summary, concise_summary)
        _assert("- Low: 1" in concise_summary, concise_summary)
        _assert("- Unknown severity: 1" in concise_summary, concise_summary)
        _assert("- Total open issues: 4" in concise_summary, concise_summary)

        concise_tex = _audit_summary_tex(session, issue_summary_open_only=True)
        _assert(r"\item Open issue severity summary: open issues only" in concise_tex, concise_tex)
        _assert(r"\item Total open issues: 4" in concise_tex, concise_tex)


def test_source_ingestion_diagnostics_in_reports() -> None:
    def seed_source_case(
        root: Path,
        *,
        tex_supplied: bool,
        source_kinds: list[str],
        label_map: dict[str, Any],
    ) -> dict[str, Any]:
        workdir = root / "paper_audit"
        session = _seed_state(workdir)
        if tex_supplied:
            session["tex_path"] = str(root / "paper.tex")
        paths = session_paths(workdir)
        _write_json(paths["session"], session)
        _write_json(
            paths["status"],
            {
                "status": "completed",
                "chunks_completed": len(source_kinds),
                "chunks_total": len(source_kinds),
                "estimated_pages_completed": len(source_kinds),
                "estimated_pages_total": len(source_kinds),
                "updated_at": NEW,
            },
        )
        _write_json(
            paths["manifest"],
            {
                "chunking_mode": "synthetic",
                "chunks": [
                    {
                        "chunk_id": f"chunk_{idx:03d}",
                        "chunk_index": idx,
                        "page_start": idx,
                        "page_end": idx,
                        "source_kind": source_kind,
                    }
                    for idx, source_kind in enumerate(source_kinds, start=1)
                ],
                "updated_at": NEW,
            },
        )
        _write_json(
            workdir / "state" / "reference_map.json",
            {
                "label_map": label_map,
                "source_aux_path": str(root / "paper.aux") if label_map else None,
                "map_source": "aux" if label_map else "none",
                "updated_at": NEW,
            },
        )
        return session

    with tempfile.TemporaryDirectory(prefix="math_audit_source_diag_pdf_") as tmp:
        session = seed_source_case(Path(tmp), tex_supplied=False, source_kinds=["pdf", "pdf"], label_map={})
        diag = source_ingestion_diagnostics(session)
        _assert(not diag["tex_supplied"], diag)
        _assert(diag["status"] == "pdf_only", diag)
        _assert(diag["warnings"] == [], diag)
        markdown = build_concise_report_markdown(session)
        _assert("## Source ingestion status" in markdown, markdown)
        _assert("PDF-only audit; no LaTeX source was supplied." in markdown, markdown)
        _assert("structural recovery was partial" not in markdown, markdown)

    with tempfile.TemporaryDirectory(prefix="math_audit_source_diag_healthy_") as tmp:
        session = seed_source_case(
            Path(tmp),
            tex_supplied=True,
            source_kinds=["tex", "tex", "tex", "tex-gap"],
            label_map={
                "eq:main": {"kind": "equation", "number": "1", "display": "equation (1)"},
                "thm:main": {"kind": "theorem", "number": "1.1", "display": "Theorem 1.1"},
            },
        )
        diag = source_ingestion_diagnostics(session)
        _assert(diag["tex_supplied"], diag)
        _assert(diag["status"] == "tex_structural_recovery_good", diag)
        _assert(diag["recovered_label_count"] == 2, diag)
        _assert(diag["warnings"] == [], diag)
        report_json = build_concise_report_json(session)
        _assert(report_json["source_ingestion_diagnostics"]["recovered_label_count"] == 2, report_json)
        markdown = build_concise_report_markdown(session)
        _assert("Recovered reference labels: 2" in markdown, markdown)
        _assert("Warning:" not in markdown.split("## Source ingestion status", 1)[1].split("##", 1)[0], markdown)

    with tempfile.TemporaryDirectory(prefix="math_audit_source_diag_partial_") as tmp:
        session = seed_source_case(
            Path(tmp),
            tex_supplied=True,
            source_kinds=["tex", "tex-gap", "tex-gap", "tex-gap"],
            label_map={},
        )
        diag = source_ingestion_diagnostics(session)
        _assert(diag["status"] == "tex_partial_structural_recovery", diag)
        _assert(diag["fallback_gap_chunk_count"] == 3, diag)
        _assert(diag["label_map_empty_despite_tex"], diag)
        _assert(len(diag["warnings"]) == 3, diag)
        markdown = build_concise_report_markdown(session)
        _assert("many chunks used fallback/gap source" in markdown, markdown)
        _assert("no labels were recovered" in markdown, markdown)
        _assert("no compiled .aux label map was available" in markdown, markdown)
        tex = build_concise_report_tex(session)
        _assert(r"\section*{Source ingestion status}" in tex, tex)
        _assert("many chunks used fallback/gap source" in tex, tex)
        full_markdown = build_final_report_markdown(session)
        _assert("## Source ingestion status" in full_markdown, full_markdown)

    with tempfile.TemporaryDirectory(prefix="math_audit_source_diag_empty_labels_") as tmp:
        session = seed_source_case(
            Path(tmp),
            tex_supplied=True,
            source_kinds=["tex", "tex", "tex"],
            label_map={},
        )
        diag = source_ingestion_diagnostics(session)
        _assert(diag["fallback_gap_chunk_count"] == 0, diag)
        _assert(diag["label_map_empty_despite_tex"], diag)
        _assert(len(diag["warnings"]) == 2, diag)
        _assert(any("no labels were recovered" in warning for warning in diag["warnings"]), diag)
        _assert(any("no compiled .aux label map" in warning for warning in diag["warnings"]), diag)


def test_aux_printed_label_display_in_reports() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_aux_label_display_") as tmp:
        root = Path(tmp)
        workdir = root / "paper_audit"
        session = _seed_state(workdir)
        session["tex_path"] = str(root / "paper.tex")
        paths = session_paths(workdir)
        _write_json(paths["session"], session)
        _write_text(
            root / "paper.aux",
            "\n".join(
                [
                    r"\newlabel{eq:test}{{3.5}{8}}",
                    r"\newlabel{eq:main}{{3.5}{8}}",
                    r"\newlabel{lemma:sizebiasgeom}{{4.3}{11}{Size bias}{theorem.4.3}{}}",
                    r"\newlabel{lem:technicalf1}{{4.1}{10}{Technical lemma}{lemma.4.1}{}}",
                    r"\newlabel{lem:kolmmain}{{5.2}{12}{Main theorem}{theorem.5.2}{}}",
                    r"\newlabel{lem:main}{{5.2}{12}{Main lemma}{lemma.5.2}{}}",
                    r"\newlabel{foo:bar}{{A.1}{20}}",
                ]
            )
            + "\n",
        )
        label_map = _load_aux_label_map(root / "paper.aux")
        _assert(label_map["eq:test"]["printed_label"] == "Equation (3.5)", label_map)
        _assert(label_map["eq:test"]["printed_number"] == "3.5", label_map)
        _assert(label_map["eq:test"]["page"] == "8", label_map)
        _assert(label_map["lemma:sizebiasgeom"]["printed_label"] == "Theorem 4.3", label_map)
        _assert(label_map["lem:technicalf1"]["printed_label"] == "Lemma 4.1", label_map)
        _assert(label_map["lem:kolmmain"]["printed_label"] == "Theorem 5.2", label_map)
        _assert(label_map["lem:main"]["printed_label"] == "Lemma 5.2", label_map)
        _assert(label_map["lem:main"]["anchor"] == "lemma.5.2", label_map)
        _assert(label_map["foo:bar"]["printed_label"] == "A.1", label_map)
        ref_state = {"label_map": label_map}
        _assert(
            _augment_source_labels_in_text("By Lemma lemma:sizebiasgeom", ref_state)
            == "By Theorem 4.3 [source label: lemma:sizebiasgeom]",
            label_map,
        )
        _assert(
            "By Theorem 4.3 [source label: lemma:sizebiasgeom]"
            in _augment_source_labels_in_text("The proof says 'By Lemma lemma:sizebiasgeom'.", ref_state),
            label_map,
        )
        _assert(
            _augment_source_labels_in_text("the lemma labeled lemma:sizebiasgeom", ref_state)
            == "the result labeled Theorem 4.3 [source label: lemma:sizebiasgeom]",
            label_map,
        )
        _assert(
            _augment_source_labels_in_text("see eq:main", ref_state)
            == "see Equation (3.5) [source label: eq:main]",
            label_map,
        )
        singly_enriched = _augment_source_labels_in_text(
            "Theorem 4.3 [source label: lemma:sizebiasgeom]",
            ref_state,
        )
        _assert(singly_enriched.count("[source label: lemma:sizebiasgeom]") == 1, singly_enriched)
        _assert(_augment_source_labels_in_text("lemma:unknown", ref_state) == "lemma:unknown", label_map)
        _assert(
            _augment_source_labels_in_text("prefixlemma:sizebiasgeomsuffix", ref_state)
            == "prefixlemma:sizebiasgeomsuffix",
            label_map,
        )
        _write_json(
            paths["status"],
            {
                "status": "completed",
                "chunks_completed": 1,
                "chunks_total": 1,
                "estimated_pages_completed": 1,
                "estimated_pages_total": 1,
                "updated_at": NEW,
            },
        )
        _write_json(
            paths["manifest"],
            {
                "chunking_mode": "tex",
                "chunks": [
                    {
                        "chunk_id": "chunk_001",
                        "chunk_index": 1,
                        "page_start": 1,
                        "page_end": 1,
                        "source_kind": "tex",
                    }
                ],
                "updated_at": NEW,
            },
        )
        _write_json(
            paths["issues"],
            {
                "next_issue_id": 4,
                "issues": [
                    {
                        "issue_id": "I001",
                        "chunk_id": "chunk_001",
                        "severity": "high",
                        "status": "open",
                        "title": "Equation reference needs checking",
                        "location": "eq:test",
                        "description": (
                            "By Lemma lemma:sizebiasgeom and see eq:test. Unknown lemma:unknown and "
                            "prefixlemma:sizebiasgeomsuffix remain raw."
                        ),
                        "evidence": (
                            "Theorem 4.3 [source label: lemma:sizebiasgeom] should not be double-enriched."
                        ),
                        "proposed_fix": "Replace the lemma labeled lem:technicalf1 reference if needed.",
                        "tags": ["reference"],
                    },
                    {
                        "issue_id": "I002",
                        "chunk_id": "chunk_001",
                        "severity": "high",
                        "status": "open",
                        "title": "Lemma reference needs checking",
                        "location": "lem:main",
                        "description": "The proof also cites the lemma labeled lem:kolmmain in prose.",
                        "evidence": "The source label should be displayed with the printed lemma number.",
                        "proposed_fix": "Check the printed lemma.",
                        "tags": ["reference"],
                    },
                    {
                        "issue_id": "I003",
                        "chunk_id": "chunk_001",
                        "severity": "high",
                        "status": "open",
                        "title": "Unknown reference kind is still readable",
                        "location": "foo:bar",
                        "description": "Unknown label kinds should still show the printed number.",
                        "evidence": "The source label should not be lost.",
                        "proposed_fix": "Keep both identifiers.",
                        "tags": ["reference"],
                    },
                ],
                "updated_at": NEW,
            },
        )

        concise_markdown = build_concise_report_markdown(session)
        _assert("Location detail: Equation (3.5) [source label: eq:test]" in concise_markdown, concise_markdown)
        _assert("Location detail: Lemma 5.2 [source label: lem:main]" in concise_markdown, concise_markdown)
        _assert("Location detail: A.1 [source label: foo:bar]" in concise_markdown, concise_markdown)
        _assert(
            "Description: By Theorem 4.3 [source label: lemma:sizebiasgeom] and see Equation (3.5) [source label: eq:test]."
            in concise_markdown,
            concise_markdown,
        )
        _assert("lemma:unknown" in concise_markdown, concise_markdown)
        _assert("prefixlemma:sizebiasgeomsuffix remain raw" in concise_markdown, concise_markdown)
        _assert(
            "Evidence: Theorem 4.3 [source label: lemma:sizebiasgeom] should not be double-enriched."
            in concise_markdown,
            concise_markdown,
        )
        _assert(
            "Proposed fix: Replace the result labeled Lemma 4.1 [source label: lem:technicalf1] reference if needed."
            in concise_markdown,
            concise_markdown,
        )
        _assert("the result labeled Theorem 5.2 [source label: lem:kolmmain]" in concise_markdown, concise_markdown)
        _assert("Compiled AUX label recovery: 1 .aux file(s) found; 7 printed label(s) recovered; available: True." in concise_markdown, concise_markdown)

        concise_tex = build_concise_report_tex(session)
        _assert("Location detail: Equation (3.5) [source label: eq:test]" in concise_tex, concise_tex)
        _assert("Location detail: Lemma 5.2 [source label: lem:main]" in concise_tex, concise_tex)
        _assert("By Theorem 4.3 [source label: lemma:sizebiasgeom]" in concise_tex, concise_tex)
        _assert("Replace the result labeled Lemma 4.1 [source label: lem:technicalf1]" in concise_tex, concise_tex)

        full_markdown = build_final_report_markdown(session)
        _assert("- Location: Equation (3.5) [source label: eq:test]" in full_markdown, full_markdown)
        _assert("- Location: Lemma 5.2 [source label: lem:main]" in full_markdown, full_markdown)
        _assert("- Description: By Theorem 4.3 [source label: lemma:sizebiasgeom]" in full_markdown, full_markdown)
        _assert("- Proposed fix: Replace the result labeled Lemma 4.1 [source label: lem:technicalf1]" in full_markdown, full_markdown)

        report_json = build_concise_report_json(session)
        _assert(report_json["source_ingestion_diagnostics"]["printed_label_recovery_available"], report_json)
        location_details = [entry["location_detail"] for entry in report_json["high_issue_entries"]]
        _assert("Equation (3.5) [source label: eq:test]" in location_details, location_details)
        _assert("Lemma 5.2 [source label: lem:main]" in location_details, location_details)
        descriptions = [entry["description_display"] for entry in report_json["high_issue_entries"]]
        _assert(any("By Theorem 4.3 [source label: lemma:sizebiasgeom]" in item for item in descriptions), descriptions)

    with tempfile.TemporaryDirectory(prefix="math_audit_old_reference_map_display_") as tmp:
        root = Path(tmp)
        workdir = root / "paper_audit"
        session = _seed_state(workdir)
        session["tex_path"] = str(root / "paper.tex")
        paths = session_paths(workdir)
        _write_json(paths["session"], session)
        _write_json(
            workdir / "state" / "reference_map.json",
            {
                "label_map": {
                    "eq:old": {"number": "7.1", "kind": "equation", "display": "Equation (7.1)"}
                },
                "map_source": "cached",
                "updated_at": OLD,
            },
        )
        _write_json(
            paths["manifest"],
            {
                "chunking_mode": "tex",
                "chunks": [
                    {
                        "chunk_id": "chunk_001",
                        "chunk_index": 1,
                        "page_start": 1,
                        "page_end": 1,
                        "source_kind": "tex",
                    }
                ],
            },
        )
        _write_json(
            paths["issues"],
            {
                "issues": [
                    {
                        "issue_id": "I001",
                        "chunk_id": "chunk_001",
                        "severity": "high",
                        "status": "open",
                        "title": "Old cached label maps still display",
                        "location": "eq:old",
                        "description": "Old maps lack printed_label but have display.",
                        "evidence": "Reports should not crash.",
                        "proposed_fix": "Use the display field.",
                        "tags": ["reference"],
                    }
                ],
                "updated_at": NEW,
            },
        )
        markdown = build_concise_report_markdown(session)
        _assert("Equation (7.1) [source label: eq:old]" in markdown, markdown)


def test_concise_report_notable_incorrect_reference_issues() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_concise_notable_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        paths = session_paths(workdir)
        _write_json(
            paths["manifest"],
            {
                "chunking_mode": "tex-full-coverage",
                "chunks": [
                    {"chunk_id": "chunk_001", "chunk_index": 1, "page_start": 1, "page_end": 2},
                    {"chunk_id": "chunk_002", "chunk_index": 2, "page_start": 3, "page_end": 4},
                    {"chunk_id": "chunk_003", "chunk_index": 3, "page_start": 5, "page_end": 6},
                    {"chunk_id": "chunk_004", "chunk_index": 4, "page_start": 7, "page_end": 7},
                    {"chunk_id": "chunk_005", "chunk_index": 5, "page_start": 8, "page_end": 9},
                    {"chunk_id": "chunk_006", "chunk_index": 6, "page_start": 10, "page_end": 11},
                ],
            },
        )
        for chunk_id, index, start, end in [
            ("chunk_001", 1, 1, 2),
            ("chunk_002", 2, 3, 4),
            ("chunk_003", 3, 5, 6),
            ("chunk_004", 4, 7, 7),
            ("chunk_005", 5, 8, 9),
            ("chunk_006", 6, 10, 11),
        ]:
            _append_jsonl(
                paths["chunk_records"],
                {"chunk_id": chunk_id, "chunk_index": index, "page_start": start, "page_end": end},
            )
        _write_json(
            paths["issues"],
            {
                "next_issue_id": 7,
                "issues": [
                    {
                        "issue_id": "I001",
                        "chunk_id": "chunk_001",
                        "severity": "high",
                        "status": "open",
                        "title": "Main theorem bound fails",
                        "location": "Theorem 1",
                        "description": "A high-priority correctness issue.",
                        "evidence": "The displayed estimate is too small.",
                        "proposed_fix": "Repair the estimate.",
                        "tags": ["proof-gap"],
                    },
                    {
                        "issue_id": "I002",
                        "chunk_id": "chunk_002",
                        "severity": "medium",
                        "status": "open",
                        "title": "Proof cites the identity being proved",
                        "location": "Proposition proof, opening sentence",
                        "description": "The proof cites equation (34), the identity being proved, instead of the prior finite-difference representation.",
                        "evidence": "The cited equation is the displayed proposition identity.",
                        "proposed_fix": "Replace the citation with the earlier finite-difference formula and Leibniz rule.",
                        "tags": ["reference-error", "proof-structure"],
                    },
                    {
                        "issue_id": "I003",
                        "chunk_id": "chunk_003",
                        "severity": "medium",
                        "status": "open",
                        "title": "An ordinary medium issue",
                        "location": "Middle paragraph",
                        "description": "This is mathematically relevant but concerns only a local estimate.",
                        "evidence": "A local estimate could be clearer.",
                        "proposed_fix": "Clarify the estimate.",
                        "tags": ["asymptotics"],
                    },
                    {
                        "issue_id": "I004",
                        "chunk_id": "chunk_004",
                        "severity": "low",
                        "status": "open",
                        "title": "Typographical spelling issue",
                        "location": "Caption",
                        "description": "A typographical spelling issue.",
                        "proposed_fix": "Fix the spelling.",
                        "tags": ["typo"],
                    },
                    {
                        "issue_id": "I005",
                        "chunk_id": "chunk_005",
                        "severity": "medium",
                        "status": "open",
                        "title": "Mislabeled equation reference points to the wrong formula",
                        "location": "Lemma 2 proof",
                        "description": "The proof cites equation (19), but the required identity is equation (18).",
                        "evidence": "Equation (19) is a different recurrence and cannot justify this line.",
                        "proposed_fix": "Replace the citation with equation (18).",
                        "tags": ["wrong-reference"],
                    },
                    {
                        "issue_id": "I006",
                        "chunk_id": "chunk_006",
                        "severity": "medium",
                        "status": "open",
                        "title": "Replacement of the exponential factor in equation (15) needs an explicit uniform estimate",
                        "location": "Equation (15)",
                        "description": "The proof should justify the uniform error term before replacing the exponential factor.",
                        "evidence": "The estimate is plausible but not written out.",
                        "proposed_fix": "Add the missing uniform estimate.",
                        "tags": ["uniformity", "asymptotics", "proof-gap"],
                    },
                ],
            },
        )

        markdown = build_concise_report_markdown(session)
        _assert("## High-priority mathematical/correctness issues" in markdown, markdown)
        _assert("### I001 — Main theorem bound fails [high]" in markdown, markdown)
        _assert("## Notable incorrect or circular references" in markdown, markdown)
        _assert("### I002 — Proof cites the identity being proved [medium]" in markdown, markdown)
        _assert("### I005 — Mislabeled equation reference points to the wrong formula [medium]" in markdown, markdown)
        _assert("I003 — An ordinary medium issue" not in markdown, markdown)
        _assert("I006 — Replacement of the exponential factor in equation (15)" not in markdown, markdown)
        _assert("## Typographical errors" in markdown, markdown)
        _assert("### I004 — Typographical spelling issue [low]" in markdown, markdown)
        notable_section = markdown.split("## Notable incorrect or circular references", 1)[1].split("## Typographical errors", 1)[0]
        _assert("I001 — Main theorem bound fails" not in notable_section, notable_section)
        _assert("I004 — Typographical spelling issue" not in notable_section, notable_section)
        _assert("I006 — Replacement of the exponential factor" not in notable_section, notable_section)

        tex = build_concise_report_tex(session)
        _assert(r"\section*{Notable incorrect or circular references}" in tex, tex)
        _assert("I002 -- Proof cites the identity being proved [medium]" in tex, tex)
        _assert("I005 -- Mislabeled equation reference points to the wrong formula [medium]" in tex, tex)
        _assert("I006 -- Replacement of the exponential factor" not in tex, tex)

        report_json = build_concise_report_json(session)
        notable_ids = [
            item.get("issue_id")
            for item in report_json.get("notable_incorrect_or_circular_references", [])
        ]
        _assert(set(notable_ids) == {"I002", "I005"}, notable_ids)
        _assert(len(notable_ids) == 2, notable_ids)
        legacy_notable_ids = [
            item.get("issue_id")
            for item in report_json.get("notable_proof_reference_and_dependency_issues", [])
        ]
        _assert(legacy_notable_ids == notable_ids, report_json)
        _assert([item.get("issue_id") for item in report_json.get("main_issues", [])] == ["I001"], report_json["main_issues"])
        _assert("notable_incorrect_or_circular_references" in report_json["selection_rules"], report_json["selection_rules"])
        _assert("notable_proof_reference_and_dependency_issues" in report_json["selection_rules"], report_json["selection_rules"])

        balanced_markdown = build_concise_report_markdown(session, options={"preset": "balanced_concise"})
        _assert("## Notable incorrect or circular references" not in balanced_markdown, balanced_markdown)
        _assert("### I002 — Proof cites the identity being proved [medium]" in balanced_markdown, balanced_markdown)


def test_issue_recheck_overlay_in_reports() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_recheck_overlay_report_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        session["reasoning_effort"] = "xhigh"
        _write_json(session_paths(workdir)["session"], session)
        paths = session_paths(workdir)
        issues = [
            {
                "issue_id": "I057",
                "chunk_id": "chunk_026",
                "severity": "high",
                "status": "open",
                "title": "Proof cites the identity being proved",
                "location": "Proposition 4.1",
                "description": "The proof cites equation (34), the identity being proved.",
                "evidence": "The cited line is the displayed identity itself.",
                "proposed_fix": "Cite the finite-difference representation and Leibniz rule.",
                "tags": ["circular-citation"],
            },
            {
                "issue_id": "I062",
                "chunk_id": "chunk_028",
                "severity": "medium",
                "status": "open",
                "title": "Theorem 4.1 needs explicit uniformity assumptions",
                "location": "Theorem 4.1",
                "description": "The permitted range and O-constant dependence should be stated.",
                "evidence": "Later uses vary k.",
                "proposed_fix": "State the range and uniformity.",
                "tags": ["uniformity"],
            },
            {
                "issue_id": "I071",
                "chunk_id": "chunk_033",
                "severity": "high",
                "status": "open",
                "title": "Theorem 4.1 inherits equation (34) dependency",
                "location": "Theorem 4.1",
                "description": "The theorem depends on equation (34).",
                "evidence": "Downstream dependency.",
                "proposed_fix": "Group under the upstream issue.",
                "tags": ["dependency"],
            },
            {
                "issue_id": "I099",
                "chunk_id": "chunk_047",
                "severity": "high",
                "status": "open",
                "title": "Theorem 5.1 inherits Theorem 4.1 dependency",
                "location": "Theorem 5.1",
                "description": "The theorem uses Theorem 4.1.",
                "evidence": "Downstream dependency.",
                "proposed_fix": "Group under the upstream issue.",
                "tags": ["dependency"],
            },
            {
                "issue_id": "I186",
                "chunk_id": "chunk_072",
                "severity": "high",
                "status": "open",
                "title": "Coefficient comparison needs separate review",
                "location": "Appendix B",
                "description": "A coefficient-level concern may be independent.",
                "evidence": "The provided family recheck did not settle it.",
                "proposed_fix": "Review separately.",
                "tags": ["human-review"],
            },
        ]
        _write_json(paths["issues"], {"next_issue_id": 200, "issues": issues, "updated_at": NEW})
        _write_json(
            workdir / "state" / "issue_rechecks.json",
            {
                "schema_version": 1,
                "updated_at": NEW,
                "rechecks": [
                    {
                        "recheck_id": "F004_001",
                        "family_id": "F004",
                        "source_result_path": "/tmp/family_recheck_result.json",
                        "source_output_dir": "/tmp/family_recheck",
                        "accepted_at": NEW,
                        "review_method": "llm_issue_family_recheck",
                        "verdict": "Group downstream consequences.",
                        "upstream_issue_ids": ["I057", "I062"],
                        "downstream_issue_ids": ["I071", "I099"],
                        "false_positive_issue_ids": [],
                        "recommended_severity_by_issue": [
                            {"issue_id": "I057", "severity": "medium", "rationale": "Repairable reference issue."},
                            {"issue_id": "I071", "severity": "low/downstream", "rationale": "Covered by I057."},
                            {"issue_id": "I099", "severity": "low/downstream", "rationale": "Covered by I057."},
                            {"issue_id": "I186", "severity": "human-review/possibly separate", "rationale": "Separate coefficient review needed."},
                        ],
                        "recommended_status_by_issue": [
                            {"issue_id": "I057", "status": "open-upstream", "rationale": "Main reportable issue."},
                            {"issue_id": "I071", "status": "downstream-covered", "rationale": "Do not count independently."},
                            {"issue_id": "I099", "status": "downstream-covered", "rationale": "Do not count independently."},
                            {"issue_id": "I186", "status": "needs-human-review-separate", "rationale": "Not an F004 downstream consequence."},
                        ],
                        "grouping_recommendations": [
                            {"upstream_issue_id": "I057", "downstream_issue_ids": ["I071", "I099"], "rationale": "Equation (34) dependency chain."}
                        ],
                        "final_report_treatment": "Report I057/I062; group I071/I099 downstream; review I186 separately.",
                        "evidence_for": ["Equation (34) is the upstream reference issue."],
                        "evidence_against": ["The algebra appears repairable."],
                        "confidence": "medium",
                        "needs_human_review": True,
                        "summary": "Advisory family recheck.",
                    }
                ],
            },
        )

        concise_markdown = build_concise_report_markdown(session)
        _assert("### I057 — Proof cites the identity being proved [high]" in concise_markdown, concise_markdown)
        _assert("### I071 — Theorem 4.1 inherits equation (34) dependency" not in concise_markdown, concise_markdown)
        _assert("### I099 — Theorem 5.1 inherits Theorem 4.1 dependency" not in concise_markdown, concise_markdown)
        _assert("- Downstream-covered issues: I071, I099" in concise_markdown, concise_markdown)
        _assert("### I186 — Coefficient comparison needs separate review [high]" in concise_markdown, concise_markdown)
        _assert("needs separate human review" in concise_markdown, concise_markdown)

        full_markdown = build_final_report_markdown(session)
        _assert("### I071 — Theorem 4.1 inherits equation (34) dependency [high]" in full_markdown, full_markdown)
        _assert("Rechecked status/treatment: downstream-covered" in full_markdown, full_markdown)
        _assert("canonical issue record unchanged" in full_markdown, full_markdown)

        full_tex = build_final_report_tex(session)
        _assert("I071 -- Theorem 4.1 inherits equation (34) dependency [high]" in full_tex, full_tex)
        _assert("Rechecked status/treatment: downstream-covered" in full_tex, full_tex)

        report_json = build_concise_report_json(session)
        _assert(report_json["recheck_applied"], report_json)
        main_ids = [issue.get("issue_id") for issue in report_json["main_issues"]]
        _assert("I071" not in main_ids and "I099" not in main_ids, report_json["main_issues"])
        _assert(report_json["issue_recheck_summary"]["downstream_covered_issue_count"] == 2, report_json["issue_recheck_summary"])
        _assert(report_json["grouped_downstream_issues"]["I057"] == ["I071", "I099"], report_json["grouped_downstream_issues"])

    with tempfile.TemporaryDirectory(prefix="math_audit_no_recheck_overlay_report_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        report_json = build_concise_report_json(session)
        _assert(not report_json["recheck_applied"], report_json)
        _assert(report_json["issue_recheck_summary"]["accepted_recheck_count"] == 0, report_json["issue_recheck_summary"])


def test_fresh_rerun_request_metadata() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_fresh_rerun_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        session["audit_system_prompt"] = "Developer audit instructions."
        session["conversation_id"] = "conv-main"
        session["pdf_file_id"] = "file-paper"
        session["pdf_attached_in_conversation"] = True
        chunk = {
            "chunk_id": "chunk_002",
            "chunk_index": 2,
            "label": "TeX chunk 2",
            "boundary": "Approx. pages 2-2 based on TeX order",
            "source_kind": "tex",
            "page_start": 2,
            "page_end": 2,
            "paper_progress_end": 0.2,
            "chunk_text": "Chunk body with $x$.",
            "_rerun_id": "rerun_001",
            "_rerun_kind": "failed_verification",
            "_rerun_requested_at": NEW,
            "_extra_rerun_instruction": "Rerun because a verification script failed.",
            "_fresh_rerun_conversation": True,
            "_main_conversation_id": "conv-main",
            "_fresh_rerun_conversation_id": "conv-rerun",
        }
        request_kwargs = {
            "model": "gpt-5.4",
            "reasoning": {"effort": "high"},
            "conversation": "conv-rerun",
            "input": [
                {"role": "developer", "content": [{"type": "input_text", "text": session["audit_system_prompt"]}]},
                {"role": "user", "content": [{"type": "input_text", "text": "Chunk text:\n" + chunk["chunk_text"]}]},
            ],
        }
        request_path = Path(
            _save_request_metadata(
                session,
                chunk,
                request_kwargs,
                verification_mode="local_python_only",
                used_code_interpreter_tool=False,
                attempt_label="synthetic",
            )
        )
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        diagnostics = payload.get("request_size_diagnostics") or {}
        rerun = payload.get("rerun") or {}
        _assert(diagnostics.get("fresh_rerun_conversation"), diagnostics)
        _assert(diagnostics.get("conversation_state") == "fresh_rerun_conversation", diagnostics)
        _assert(diagnostics.get("conversation_id") == "conv-rerun", diagnostics)
        _assert(diagnostics.get("original_conversation_id") == "conv-main", diagnostics)
        _assert(diagnostics.get("rerun_kind") == "failed_verification", diagnostics)
        _assert(rerun.get("fresh_rerun_conversation"), rerun)
        _assert(rerun.get("main_conversation_id") == "conv-main", rerun)
        _assert(rerun.get("rerun_conversation_id") == "conv-rerun", rerun)
        _assert(session["conversation_id"] == "conv-main", "metadata save mutated main conversation id")


def test_usage_cache_diagnostics() -> None:
    low_cache = usage_cache_diagnostics(
        {
            "input_tokens": 759_574,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 10_069,
            "output_tokens_details": {"reasoning_tokens": 6_212},
            "total_tokens": 769_643,
        }
    )
    _assert(low_cache["cached_input_tokens"] == 0, low_cache)
    _assert(low_cache["uncached_input_tokens"] == 759_574, low_cache)
    _assert(low_cache["cached_input_percent"] == 0.0, low_cache)
    _assert(low_cache["output_tokens"] == 10_069, low_cache)
    _assert(low_cache["reasoning_tokens"] == 6_212, low_cache)
    _assert(low_cache["low_cached_input_reuse"], low_cache)
    _assert("resume/relaunch" in low_cache.get("warning", ""), low_cache)

    healthy_cache = usage_cache_diagnostics(
        {
            "input_tokens": 768_079,
            "input_tokens_details": {"cached_tokens": 749_824},
            "output_tokens": 8_300,
            "output_tokens_details": {"reasoning_tokens": 4_137},
            "total_tokens": 776_379,
        }
    )
    _assert(healthy_cache["cached_input_percent"] == 97.6, healthy_cache)
    _assert(not healthy_cache["low_cached_input_reuse"], healthy_cache)
    _assert("warning" not in healthy_cache, healthy_cache)


def test_retryable_file_download_timeout_detection() -> None:
    failure = {
        "chunk_id": "chunk_005",
        "status": "failed",
        "error": {
            "code": "invalid_value",
            "message": "Timeout while downloading https://fileserviceuploadsperm.blob.core.windows.net/files/file-example",
        },
    }
    _assert(
        _retryable_response_failure_reason(failure) == "file_download_timeout",
        "file download timeout was not classified as retryable",
    )

    with tempfile.TemporaryDirectory(prefix="math_audit_retryable_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        session["pdf_file_id"] = "file-example"
        _append_jsonl(workdir / "logs" / "failed_chunks.jsonl", failure)

        chunk = {"chunk_id": "chunk_005", "chunk_index": 5}
        _assert(
            _file_download_timeout_retry_mode(session, chunk) == FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_REATTACH,
            "first file download timeout did not select PDF reattachment",
        )
        _assert(
            _should_reattach_pdf_for_chunk_retry(session, chunk),
            "retryable file download timeout did not request PDF reattachment",
        )
        second_failure = dict(failure)
        second_failure["response_id"] = "resp-second-timeout"
        _append_jsonl(workdir / "logs" / "failed_chunks.jsonl", second_failure)
        _assert(
            _file_download_timeout_retry_mode(session, chunk) == FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_TEXT_ONLY,
            "repeated file download timeout did not select text-only fallback",
        )

        text_only_chunk = {
            "chunk_id": "chunk_005",
            "chunk_index": 5,
            "label": "PDF pages 5-5",
            "boundary": "pages 5-5",
            "source_kind": "pdf",
            "page_start": 5,
            "page_end": 5,
            "chunk_text": "Lemma 5. This is extracted chunk text for a text-only retry.",
            "_suppress_pdf_attachment": True,
            "_pdf_text_only_retry": True,
            "_pdf_attachment_disabled_note": PDF_TEXT_ONLY_RETRY_NOTE,
        }
        message = build_user_message_for_chunk(session, text_only_chunk)
        content = message[0]["content"]
        _assert(
            not any(isinstance(part, dict) and part.get("type") == "input_file" for part in content),
            "text-only retry still attached the PDF",
        )
        prompt_text = "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "input_text"
        )
        _assert(PDF_TEXT_ONLY_RETRY_NOTE in prompt_text, "text-only retry caution note missing from prompt")


def test_file_download_timeout_auto_retry_decisions() -> None:
    failure = {
        "chunk_id": "chunk_006",
        "status": "failed",
        "error": {
            "code": "invalid_value",
            "message": "Timeout while downloading https://fileserviceuploadsperm.blob.core.windows.net/files/file-example",
        },
    }
    with tempfile.TemporaryDirectory(prefix="math_audit_auto_retry_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _seed_state(workdir)
        session["pdf_file_id"] = "file-example"
        chunk = {"chunk_id": "chunk_006", "chunk_index": 6}

        no_failure = _file_download_timeout_auto_retry_decision(session, chunk, attempts_used=0)
        _assert(not no_failure["auto_retry"], no_failure)

        _append_jsonl(workdir / "logs" / "failed_chunks.jsonl", failure)
        first_retry = _file_download_timeout_auto_retry_decision(session, chunk, attempts_used=0)
        _assert(first_retry["auto_retry"], first_retry)
        _assert(first_retry["attempt"] == 1, first_retry)
        _assert(first_retry["retry_mode"] == FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_REATTACH, first_retry)

        second_failure = dict(failure)
        second_failure["response_id"] = "resp-second-timeout"
        _append_jsonl(workdir / "logs" / "failed_chunks.jsonl", second_failure)
        second_retry = _file_download_timeout_auto_retry_decision(session, chunk, attempts_used=1)
        _assert(second_retry["auto_retry"], second_retry)
        _assert(second_retry["attempt"] == 2, second_retry)
        _assert(second_retry["retry_mode"] == FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_TEXT_ONLY, second_retry)

        exhausted = _file_download_timeout_auto_retry_decision(session, chunk, attempts_used=2)
        _assert(not exhausted["auto_retry"], exhausted)
        _assert(exhausted["reason"] == "max_auto_retries_exhausted", exhausted)


def test_prepare_context_mode_comparison_script() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_context_compare_regression_") as tmp:
        root = Path(tmp)
        source = root / "source_audit"
        output = root / "comparison_out"
        session = _seed_state(source)
        session["conversation_id"] = "conv-main"
        session["pdf_file_id"] = "file-paper"
        session["pdf_path"] = str(root / "paper.pdf")
        _write_json(session_paths(source)["session"], session)
        chunks = [
            {
                "chunk_id": "chunk_001",
                "chunk_index": 1,
                "label": "Synthetic definition",
                "boundary": "pages 1-1",
                "source_kind": "pdf",
                "page_start": 1,
                "page_end": 1,
                "chunk_text": "Definition 1. Let alpha be the main parameter.",
            },
            {
                "chunk_id": "chunk_002",
                "chunk_index": 2,
                "label": "Synthetic theorem",
                "boundary": "pages 2-2; Theorem 2",
                "source_kind": "pdf",
                "page_start": 2,
                "page_end": 2,
                "chunk_text": "Theorem 2 uses alpha from Definition 1.",
            },
        ]
        _write_json(session_paths(source)["manifest"], {"chunks": chunks})
        _write_json(
            session_paths(source)["issues"],
            {
                "issues": [
                    {
                        "issue_id": "I001",
                        "chunk_id": "chunk_001",
                        "severity": "high",
                        "status": "open",
                        "title": "Dependency needs care",
                        "location": "chunk_001",
                        "description": "Later chunks depend on alpha.",
                    }
                ]
            },
        )
        structured = {
            "assumptions_and_notation": ["Definition: alpha is the main parameter."],
            "verified_steps": ["Definition 1 is used by later theorem statements."],
            "issues": [],
            "ledger_updates": {
                "assumptions": ["Assume alpha > 0 where stability is discussed."],
                "notes": ["Theorem 2 depends on Definition 1."],
            },
            "next_boundary_hint": "Next chunk states Theorem 2.",
            "confidence": "high",
        }
        structured_path = source / "responses" / "chunk_001.structured.json"
        _write_json(structured_path, structured)
        _append_jsonl(
            session_paths(source)["chunk_records"],
            {"chunk_id": "chunk_001", "structured_response_path": str(structured_path)},
        )
        baseline_request = source / "requests" / "chunk_002_baseline.request.json"
        _write_json(
            baseline_request,
            {
                "chunk_id": "chunk_002",
                "request_size_diagnostics": {
                    "user_prompt_length": 123,
                    "chunk_text_length": 44,
                    "running_audit_context_length": 22,
                    "pdf_attachment_included": False,
                },
            },
        )

        before_files = {
            path: path.read_text(encoding="utf-8")
            for path in [
                session_paths(source)["session"],
                session_paths(source)["manifest"],
                session_paths(source)["issues"],
                session_paths(source)["chunk_records"],
                structured_path,
                baseline_request,
            ]
        }
        manifest = prepare_context_mode_comparison(source, ["chunk_002"], output)
        _assert(manifest["source_unmodified_by_script"], manifest)
        _assert(manifest["total_backfilled_context_entries"] > 0, manifest)
        _assert(not session_paths(source)["audit_context_db"].exists(), "source context DB was mutated")
        for path, text in before_files.items():
            _assert(path.read_text(encoding="utf-8") == text, f"source file changed: {path}")
        prompt = (output / "chunk_002" / "fresh_context_prompt.txt").read_text(encoding="utf-8")
        _assert("Retrieved fresh-context audit database context:" in prompt, prompt)
        _assert(prompt.index("Retrieved fresh-context audit database context:") < prompt.index("Chunk text:"), prompt)
        summary = (output / "chunk_002" / "context_summary.md").read_text(encoding="utf-8")
        _assert("Prior audit issues are provisional findings" in summary, summary)
        _assert("I001 | high | status: open | source: chunk_001" in summary, summary)
        _assert("reasons:" in summary, summary)
        _assert("Reason Counts" in summary, summary)
        metadata = json.loads((output / "chunk_002" / "fresh_context_request_metadata.json").read_text(encoding="utf-8"))
        _assert(metadata["dry_run"] is True, metadata)
        _assert(metadata["would_call_api"] is False, metadata)
        _assert(metadata["request_size_diagnostics"]["fresh_context_conversation"], metadata)
        _assert(metadata["priority_issues"][0]["issue_id"] == "I001", metadata)
        _assert(metadata["priority_issue_reason_counts"]["recent"] >= 1, metadata)
        _assert((output / "comparison_manifest.json").exists(), "comparison manifest missing")


def test_run_context_mode_ab_test_dry_run() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_ab_test_regression_") as tmp:
        root = Path(tmp)
        source = root / "source_audit"
        output = root / "ab_output"
        session = _seed_state(source)
        session["conversation_id"] = "conv-main"
        session["pdf_file_id"] = "file-paper"
        session["pdf_path"] = str(root / "paper.pdf")
        session["model"] = "gpt-5.5"
        session["reasoning_effort"] = "xhigh"
        _write_json(session_paths(source)["session"], session)
        chunks = [
            {
                "chunk_id": "chunk_001",
                "chunk_index": 1,
                "label": "Synthetic definition",
                "boundary": "pages 1-1",
                "source_kind": "pdf",
                "page_start": 1,
                "page_end": 1,
                "paper_progress_end": 0.5,
                "chunk_text": "Definition 1. Let alpha be the main parameter.",
            },
            {
                "chunk_id": "chunk_002",
                "chunk_index": 2,
                "label": "Synthetic theorem",
                "boundary": "pages 2-2; Theorem 2",
                "source_kind": "pdf",
                "page_start": 2,
                "page_end": 2,
                "paper_progress_end": 1.0,
                "chunk_text": "Theorem 2 uses alpha from Definition 1.",
            },
        ]
        _write_json(session_paths(source)["manifest"], {"chunks": chunks, "pdf_page_count": 2})
        _write_json(
            session_paths(source)["issues"],
            {
                "issues": [
                    {
                        "issue_id": "I001",
                        "chunk_id": "chunk_001",
                        "severity": "high",
                        "status": "open",
                        "title": "Dependency needs care",
                        "location": "chunk_001",
                        "description": "Later chunks depend on alpha.",
                    }
                ]
            },
        )
        structured_1 = {
            "assumptions_and_notation": ["Definition: alpha is the main parameter."],
            "verified_steps": ["Definition 1 is used by later theorem statements."],
            "issues": [],
            "python_checks": [],
            "ledger_updates": {
                "assumptions": ["Assume alpha > 0 where stability is discussed."],
                "notes": ["Theorem 2 depends on Definition 1."],
            },
            "next_boundary_hint": "Next chunk states Theorem 2.",
            "confidence": "high",
        }
        structured_1_path = source / "responses" / "chunk_001.structured.json"
        _write_json(structured_1_path, structured_1)
        structured_2_path = source / "responses" / "chunk_002.structured.json"
        _write_json(
            structured_2_path,
            {
                "assumptions_and_notation": [],
                "verified_steps": [],
                "issues": [],
                "python_checks": [],
                "ledger_updates": {"assumptions": [], "notes": []},
                "next_boundary_hint": "",
                "confidence": "medium",
            },
        )
        _append_jsonl(
            session_paths(source)["chunk_records"],
            {"chunk_id": "chunk_001", "structured_response_path": str(structured_1_path)},
        )
        baseline_request = source / "requests" / "chunk_002_baseline.request.json"
        _write_json(
            baseline_request,
            {
                "chunk_id": "chunk_002",
                "request_size_diagnostics": {
                    "user_prompt_length": 123,
                    "chunk_text_length": 44,
                    "running_audit_context_length": 22,
                    "pdf_attachment_included": False,
                },
            },
        )

        watched = [
            session_paths(source)["session"],
            session_paths(source)["manifest"],
            session_paths(source)["issues"],
            session_paths(source)["chunk_records"],
            structured_1_path,
            structured_2_path,
            baseline_request,
        ]
        before_files = {path: path.read_text(encoding="utf-8") for path in watched}
        manifest = run_context_mode_ab_test(source, ["chunk_002"], output, live=False)
        _assert(manifest["dry_run"] is True, manifest)
        _assert(manifest["source_mutation_guard"]["source_unmodified_by_script"], manifest)
        _assert((output / "ab_test_manifest.json").exists(), "A/B manifest missing")
        _assert((output / "chunk_002" / "fresh_context_prompt.txt").exists(), "fresh prompt missing")
        _assert((output / "chunk_002" / "fresh_context_request_metadata.json").exists(), "fresh metadata missing")
        _assert((output / "chunk_002" / "baseline_continuous_structured.json").exists(), "baseline structured missing")
        _assert(not (output / "chunk_002" / "fresh_context_raw_response.json").exists(), "dry-run called API")
        _assert(not session_paths(source)["audit_context_db"].exists(), "source context DB was mutated")
        for path, text in before_files.items():
            _assert(path.read_text(encoding="utf-8") == text, f"source file changed: {path}")
        try:
            run_context_mode_ab_test(source, ["chunk_002"], source / "nested_output", live=False)
        except RuntimeError as exc:
            _assert("inside the source audit workdir" in str(exc), str(exc))
        else:
            raise RegressionFailure("A/B script allowed output inside source audit workdir")


def test_prepare_issue_recheck_candidates_script() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_issue_recheck_regression_") as tmp:
        root = Path(tmp)
        source = root / "source_audit"
        output = root / "issue_recheck_out"
        _seed_state(source)
        chunks = [
            {
                "chunk_id": "chunk_048",
                "chunk_index": 48,
                "label": "Synthetic saddle point chunk",
                "boundary": "pages 20-20; equation (50)",
                "page_start": 20,
                "page_end": 20,
                "chunk_text": "The formula defines $V(R)$ near equation (50).",
            },
            {
                "chunk_id": "chunk_049",
                "chunk_index": 49,
                "label": "Synthetic downstream theorem chunk",
                "boundary": "pages 21-21; Theorem 4.1",
                "page_start": 21,
                "page_end": 21,
                "chunk_text": "Theorem 4.1 depends on the earlier $V(R)$ curvature calculation.",
            },
        ]
        _write_json(session_paths(source)["manifest"], {"chunks": chunks})
        _write_json(
            session_paths(source)["issues"],
            {
                "issues": [
                    {
                        "issue_id": "I122",
                        "chunk_id": "chunk_048",
                        "status": "open",
                        "severity": "critical",
                        "title": "Possible sign error in the definition of $V(R)$",
                        "location": "equation (50)",
                        "description": "The formula for $V(R)$ may have the wrong sign in the curvature term.",
                        "evidence": "The later saddle-point estimate appears to require positive $V(R)$.",
                        "proposed_fix": "Recheck the sign convention around equation (50).",
                        "tags": ["sign", "variance", "curvature"],
                    },
                    {
                        "issue_id": "I129",
                        "chunk_id": "chunk_049",
                        "status": "open",
                        "severity": "high",
                        "title": "Earlier potential issue with $V(R)$ remains unresolved",
                        "location": "Theorem 4.1",
                        "description": "This theorem depends on the earlier $V(R)$ sign issue from equation (50) and may propagate it downstream.",
                        "evidence": "The proof assumes the unresolved curvature factor.",
                        "proposed_fix": "Resolve the upstream $V(R)$ issue before relying on this theorem.",
                        "tags": ["dependency", "propagation", "curvature"],
                    },
                    {
                        "issue_id": "I130",
                        "chunk_id": "chunk_049",
                        "status": "open",
                        "severity": "low",
                        "title": "Minor wording issue",
                        "description": "A sentence is stylistically awkward.",
                        "tags": ["style"],
                    },
                ]
            },
        )
        _write_json(
            session_paths(source)["ledger"],
            {
                "assumptions": ["Equation (50) introduces the saddle-point variance factor $V(R)$."],
                "notes": ["Theorem 4.1 later uses the same $V(R)$ curvature quantity."],
                "updated_at": NEW,
            },
        )
        structured_path = source / "responses" / "chunk_049.structured.json"
        _write_json(
            structured_path,
            {
                "assumptions_and_notation": ["Theorem 4.1 uses $V(R)$ from equation (50)."],
                "verified_steps": [],
                "ledger_updates": {"assumptions": [], "notes": ["The downstream theorem depends on $V(R)$."]},
                "next_boundary_hint": "",
            },
        )
        _append_jsonl(
            session_paths(source)["chunk_records"],
            {"chunk_id": "chunk_049", "structured_response_path": str(structured_path)},
        )
        watched = [
            session_paths(source)["manifest"],
            session_paths(source)["issues"],
            session_paths(source)["ledger"],
            session_paths(source)["chunk_records"],
            structured_path,
        ]
        before_files = {path: path.read_text(encoding="utf-8") for path in watched}

        manifest = prepare_issue_recheck_candidates(source, output)
        _assert(manifest["source_unmodified_by_script"], manifest)
        _assert((output / "issue_recheck_candidates.json").exists(), "JSON output missing")
        _assert((output / "issue_recheck_candidates.md").exists(), "Markdown output missing")
        candidate_ids = {candidate["issue_id"] for candidate in manifest["candidates"]}
        _assert({"I122", "I129"} <= candidate_ids, candidate_ids)
        _assert("I130" not in candidate_ids, candidate_ids)
        grouped = [
            group
            for group in manifest["groups"]
            if {"I122", "I129"} <= {member["issue_id"] for member in group.get("members", [])}
        ]
        _assert(grouped, manifest["groups"])
        group = grouped[0]
        _assert(group["upstream_issue_id"] == "I122", group)
        roles = {member["issue_id"]: member["role"] for member in group["members"]}
        _assert(roles.get("I122") == "candidate_upstream", roles)
        _assert(roles.get("I129") == "possible_downstream", roles)
        for path, text in before_files.items():
            _assert(path.read_text(encoding="utf-8") == text, f"source file changed: {path}")
        try:
            prepare_issue_recheck_candidates(source, source / "nested_output")
        except RuntimeError as exc:
            _assert("inside the source audit workdir" in str(exc), str(exc))
        else:
            raise RegressionFailure("Issue recheck script allowed output inside source audit workdir")


def test_prepare_rerun_recheck_candidates_script() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_rerun_recheck_regression_") as tmp:
        root = Path(tmp)
        source = root / "source_audit"
        output = root / "rerun_recheck_out"
        _seed_state(source)
        chunks = [
            {
                "chunk_id": "chunk_026",
                "chunk_index": 26,
                "label": "Proposition 4.1",
                "boundary": "pages 12-13; equation (34)",
                "page_start": 12,
                "page_end": 13,
                "chunk_text": "Proposition 4.1 proves equation (34).",
            },
            {
                "chunk_id": "chunk_030",
                "chunk_index": 30,
                "label": "Theorem 4.1",
                "boundary": "pages 15-16; Theorem 4.1",
                "page_start": 15,
                "page_end": 16,
                "chunk_text": "Theorem 4.1 depends on equation (34).",
            },
            {
                "chunk_id": "chunk_048",
                "chunk_index": 48,
                "label": "Saddle point chunk",
                "boundary": "pages 20-20; equation (50)",
                "page_start": 20,
                "page_end": 20,
                "chunk_text": "The formula defines $V(R)$ near equation (50).",
            },
            {
                "chunk_id": "chunk_049",
                "chunk_index": 49,
                "label": "Downstream theorem",
                "boundary": "pages 21-21; Theorem 5.1",
                "page_start": 21,
                "page_end": 21,
                "chunk_text": "Theorem 5.1 depends on the earlier $V(R)$ curvature calculation.",
            },
            {
                "chunk_id": "chunk_055",
                "chunk_index": 55,
                "label": "Verification chunk",
                "boundary": "pages 24-24",
                "page_start": 24,
                "page_end": 24,
            },
            {
                "chunk_id": "chunk_056",
                "chunk_index": 56,
                "label": "Technical failure chunk",
                "boundary": "pages 24-25",
                "page_start": 24,
                "page_end": 25,
            },
        ]
        _write_json(session_paths(source)["manifest"], {"chunks": chunks})
        _write_json(
            session_paths(source)["issues"],
            {
                "issues": [
                    {
                        "issue_id": "I057",
                        "chunk_id": "chunk_026",
                        "status": "open",
                        "severity": "medium",
                        "title": "Proof cites equation (34), the identity being proved",
                        "location": "Proposition 4.1 proof",
                        "description": "The proof cites equation (34) while proving equation (34), so this is a circular citation.",
                        "evidence": "The intended reference is the finite-difference identity plus Leibniz rule.",
                        "proposed_fix": "Replace the citation with the earlier identity and Leibniz rule.",
                        "tags": ["circular-citation", "reference-error"],
                    },
                    {
                        "issue_id": "I071",
                        "chunk_id": "chunk_030",
                        "status": "open",
                        "severity": "high",
                        "title": "Theorem 4.1 depends on unresolved equation (34)",
                        "location": "Theorem 4.1",
                        "description": "This theorem depends on equation (34); if the earlier circular citation is not repaired, this downstream theorem inherits the gap.",
                        "evidence": "The proof invokes equation (34) without an independent derivation.",
                        "proposed_fix": "Repair Proposition 4.1 first, then recheck this dependency.",
                        "tags": ["dependency", "propagation"],
                    },
                    {
                        "issue_id": "I122",
                        "chunk_id": "chunk_048",
                        "status": "open",
                        "severity": "critical",
                        "title": "Possible sign error in the definition of $V(R)$",
                        "location": "equation (50)",
                        "description": "The formula for $V(R)$ may have the wrong sign in the curvature term.",
                        "evidence": "The later saddle-point estimate appears to require positive $V(R)$.",
                        "proposed_fix": "Recheck the sign convention around equation (50).",
                        "tags": ["sign", "variance", "curvature"],
                    },
                    {
                        "issue_id": "I129",
                        "chunk_id": "chunk_049",
                        "status": "open",
                        "severity": "high",
                        "title": "Earlier potential issue with $V(R)$ remains unresolved",
                        "location": "Theorem 5.1",
                        "description": "This theorem depends on the earlier $V(R)$ sign issue from equation (50) and may propagate it downstream.",
                        "evidence": "The proof assumes the unresolved curvature factor.",
                        "proposed_fix": "Resolve the upstream $V(R)$ issue before relying on this theorem.",
                        "tags": ["dependency", "propagation", "curvature"],
                    },
                    {
                        "issue_id": "I140",
                        "chunk_id": "chunk_049",
                        "status": "open",
                        "severity": "medium",
                        "title": "Notation regime for $\rho$ is unclear",
                        "location": "Theorem 5.1 setup",
                        "description": "The range assumption for the parameter regime is ambiguous.",
                        "evidence": "Later text appears to clarify the regime.",
                        "proposed_fix": "Recheck against later regime notation.",
                        "tags": ["notation", "regime"],
                    },
                    {
                        "issue_id": "I141",
                        "chunk_id": "chunk_049",
                        "status": "open",
                        "severity": "high",
                        "title": "Undefined notation q in the local regime",
                        "location": "Theorem 5.1 setup",
                        "description": "The symbol q is not defined before it is used in the regime statement.",
                        "evidence": "The variable appears without an introduced convention.",
                        "proposed_fix": "Define q or replace it with the intended parameter.",
                        "tags": ["notation"],
                    },
                    {
                        "issue_id": "I150",
                        "chunk_id": "chunk_049",
                        "status": "open",
                        "severity": "high",
                        "title": "Uniform asymptotic estimate needs a sharper error bound",
                        "location": "Theorem 5.1 proof",
                        "description": "The proof invokes a uniform asymptotic estimate with an error term that is not justified.",
                        "evidence": "This is a substantive estimate issue, not a notation or regime ambiguity.",
                        "proposed_fix": "Add the missing estimate or restrict the range.",
                        "tags": ["uniformity", "asymptotics", "proof-gap"],
                    },
                ]
            },
        )
        _write_json(
            session_paths(source)["ledger"],
            {
                "assumptions": ["Equation (34) is used by Theorem 4.1.", "Equation (50) defines $V(R)$."],
                "notes": ["Later chunks clarify the parameter regime for $\\rho$."],
                "updated_at": NEW,
            },
        )
        structured_path = source / "responses" / "chunk_049.structured.json"
        _write_json(
            structured_path,
            {
                "assumptions_and_notation": ["Theorem 5.1 uses $V(R)$ from equation (50)."],
                "verified_steps": [],
                "ledger_updates": {"assumptions": [], "notes": ["The downstream theorem depends on $V(R)$."]},
                "next_boundary_hint": "",
            },
        )
        _append_jsonl(
            session_paths(source)["chunk_records"],
            {"chunk_id": "chunk_049", "time": OLD, "structured_response_path": str(structured_path)},
        )
        check_path = source / "python_checks" / "chunk_055_check_01.py"
        _write_text(check_path, "assert False, 'synthetic failure'\n")
        result_path = source / "verification_results" / "chunk_055_check_01.result.json"
        _write_json(
            result_path,
            {
                "time": NEW,
                "chunk_id": "chunk_055",
                "chunk_index": 55,
                "script_name": "chunk_055_check_01.py",
                "script_path": str(check_path),
                "result_path": str(result_path),
                "status": "failed",
                "returncode": 1,
                "stdout": "",
                "stderr": "AssertionError: synthetic failure",
                "conclusion": "Traceback (most recent call last):",
            },
        )
        _write_json(
            session_paths(source)["verification_state"],
            {
                "updated_at": NEW,
                "last_run": {"scripts_total": 1, "passed": 0, "failed": 1, "timeout": 0, "skipped": 0},
                "results": [
                    {
                        "chunk_id": "chunk_055",
                        "chunk_index": 55,
                        "script_name": "chunk_055_check_01.py",
                        "script_path": str(check_path),
                        "result_path": str(result_path),
                        "status": "failed",
                    }
                ],
            },
        )
        failed_log = {
            "time": NEW,
            "chunk_id": "chunk_056",
            "chunk_index": 56,
            "response_id": "resp_failed_context",
            "status": "failed",
            "error": {"code": "context_length_exceeded", "message": "too long"},
            "request_path": str(source / "requests" / "chunk_056.request.json"),
            "failure_summary_path": str(source / "responses" / "chunk_056_resp_failed_context.failure.json"),
        }
        _append_jsonl(source / "logs" / "failed_chunks.jsonl", failed_log)
        _write_json(source / "responses" / "chunk_056_resp_failed_context.failure.json", failed_log)

        watched = [
            session_paths(source)["manifest"],
            session_paths(source)["issues"],
            session_paths(source)["ledger"],
            session_paths(source)["chunk_records"],
            session_paths(source)["verification_state"],
            structured_path,
            check_path,
            result_path,
            source / "logs" / "failed_chunks.jsonl",
        ]
        before_files = {path: path.read_text(encoding="utf-8") for path in watched}

        manifest = prepare_rerun_recheck_candidates(source, output, include_medium=True)
        _assert(manifest["source_unmodified_by_script"], manifest)
        _assert((output / "rerun_recheck_candidates.json").exists(), "JSON output missing")
        _assert((output / "rerun_recheck_candidates.md").exists(), "Markdown output missing")
        categories = manifest["category_counts"]
        _assert(categories.get("verification_failure", 0) >= 1, categories)
        _assert(categories.get("technical_failure_recovery", 0) >= 1, categories)
        _assert(categories.get("high_critical_issue_recheck", 0) >= 6, categories)
        _assert(categories.get("dependency_propagation", 0) >= 2, categories)
        _assert(categories.get("notation_regime_clarification", 0) == 0, categories)
        secondary_categories = manifest["secondary_category_counts"]
        _assert(secondary_categories.get("notation_regime_clarification", 0) >= 2, secondary_categories)
        action_counts = manifest["recommended_action_kind_counts"]
        _assert(action_counts.get("chunk_rerun", 0) == 0, action_counts)
        _assert(action_counts.get("script_recheck", 0) >= 1, action_counts)
        _assert(action_counts.get("technical_retry", 0) >= 1, action_counts)
        _assert(action_counts.get("issue_recheck", 0) >= 6, action_counts)
        _assert(action_counts.get("dependency_group_review", 0) >= 2, action_counts)
        type_summary = manifest["candidate_type_summary"]
        _assert(type_summary.get("full_chunk_rerun_candidates") == 0, type_summary)
        _assert(type_summary.get("notation_regime_clarification_candidates", 0) >= 2, type_summary)

        groups = manifest["groups"]
        grouped_sets = [{member["issue_id"] for member in group.get("members", [])} for group in groups]
        _assert(any({"I122", "I129"} <= issue_ids for issue_ids in grouped_sets), groups)
        _assert(any({"I057", "I071"} <= issue_ids for issue_ids in grouped_sets), groups)
        _assert(all("I150" not in issue_ids for issue_ids in grouped_sets), groups)
        _assert(all(len(issue_ids) <= 3 for issue_ids in grouped_sets), groups)
        candidates = manifest["candidates"]
        _assert(
            any(candidate["category"] == "verification_failure" and "chunk_055_check_01.py" in candidate["source_ids"] for candidate in candidates),
            candidates,
        )
        _assert(
            any(candidate["category"] == "technical_failure_recovery" and "chunk_056" in candidate["source_ids"] for candidate in candidates),
            candidates,
        )
        issue_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("item_type") == "issue" and candidate.get("source_ids")
        ]
        by_issue = {candidate["source_ids"][0]: candidate for candidate in issue_candidates}
        _assert("notation_regime_clarification" in by_issue["I141"].get("secondary_categories", []), by_issue["I141"])
        _assert("notation_regime_clarification" not in by_issue["I150"].get("secondary_categories", []), by_issue["I150"])
        _assert(sum(1 for candidate in candidates if "I141" in candidate.get("source_ids", [])) == 1, candidates)
        _assert(
            "Candidate for review/recheck does not imply full chunk rerun."
            in (output / "rerun_recheck_candidates.md").read_text(encoding="utf-8"),
            "Markdown safety note missing",
        )
        for path, text in before_files.items():
            _assert(path.read_text(encoding="utf-8") == text, f"source file changed: {path}")
        try:
            prepare_rerun_recheck_candidates(source, source / "nested_output")
        except RuntimeError as exc:
            _assert("inside the source audit workdir" in str(exc), str(exc))
        else:
            raise RegressionFailure("Rerun/recheck script allowed output inside source audit workdir")


def test_prepare_issue_recheck_families_script() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_issue_families_regression_") as tmp:
        root = Path(tmp)
        source = root / "source_audit"
        output = root / "families_out"
        candidates_path = root / "candidates.json"
        _seed_state(source)

        issue_specs = [
            ("I057", "medium", "chunk_026", "Proof cites equation (34), the identity being proved"),
            ("I062", "high", "chunk_028", "Asymptotic uniformity of the $O$-term is not stated"),
            ("I071", "high", "chunk_033", "Theorem 4.1 inherits any unresolved gap in the proof of equation (34)"),
            ("I099", "high", "chunk_047", "The proof is conditional on Theorem 4.1 and inherits equation (34)"),
            ("I137", "high", "chunk_060", "Second displayed correction in equation (63) is missing a coefficient"),
            ("I138", "high", "chunk_060", "The coefficient-sum $O$ estimates after equations (63) and (64) conflict"),
            ("I166", "high", "chunk_066", "Later numerical comparison inherits unresolved equation (63) and equation (82) questions"),
            ("I186", "high", "chunk_072", "Appendix B use inherits equation (64) coefficient uncertainty"),
            ("I157", "high", "chunk_064", "Equation (82) mixes the approximate tree-defined parameter"),
            ("I162", "high", "chunk_065", "Equation (83) expansion does not match the stated approximation"),
            ("I171", "high", "chunk_067", "Figure comparison depends on equations (82) and (83)"),
            ("I203", "high", "chunk_079", "Proposition D.2 depends on unverified Appendix D hypotheses"),
            ("I204", "high", "chunk_079", "Proposition D.2 proof omits a dependency condition"),
            ("I209", "high", "chunk_080", "Theorem D.1 depends on Proposition D.2"),
            ("I210", "high", "chunk_080", "Equation D.4 consequence depends on Theorem D.1"),
            ("I300", "high", "chunk_055", "Uniform asymptotic estimate needs a sharper error bound"),
        ]
        chunks = [
            {"chunk_id": f"chunk_{idx:03d}", "chunk_index": idx, "label": f"Chunk {idx}", "page_start": idx, "page_end": idx}
            for idx in {26, 28, 33, 47, 55, 60, 64, 65, 66, 67, 72, 79, 80}
        ]
        _write_json(session_paths(source)["manifest"], {"chunks": chunks})
        _write_json(
            session_paths(source)["issues"],
            {
                "issues": [
                    {
                        "issue_id": issue_id,
                        "severity": severity,
                        "status": "open",
                        "chunk_id": chunk_id,
                        "title": title,
                        "description": title,
                        "evidence": title,
                        "proposed_fix": "Review the dependency family.",
                        "tags": ["dependency"] if issue_id != "I300" else ["uniformity", "asymptotics"],
                    }
                    for issue_id, severity, chunk_id, title in issue_specs
                ]
            },
        )

        def issue_candidate(issue_id: str, severity: str, chunk_id: str, title: str) -> dict[str, Any]:
            return {
                "candidate_id": f"RR-{issue_id}",
                "category": "high_critical_issue_recheck",
                "item_type": "issue",
                "recommended_action_kind": "issue_recheck",
                "source_ids": [issue_id, chunk_id],
                "context_refs": {"issue_id": issue_id, "chunk_id": chunk_id},
                "evidence_summary": {"severity": severity, "title": title},
            }

        def dep_group(group_id: str, ids: list[str], shared: list[str], upstream: str) -> dict[str, Any]:
            members = []
            for issue_id in ids:
                spec = next(item for item in issue_specs if item[0] == issue_id)
                members.append(
                    {
                        "issue_id": issue_id,
                        "chunk_id": spec[2],
                        "severity": spec[1],
                        "role": "candidate_upstream" if issue_id == upstream else "possible_downstream",
                        "title": spec[3],
                    }
                )
            return {
                "group_id": group_id,
                "upstream_issue_id": upstream,
                "classification": "synthetic",
                "members": members,
                "shared_features": shared,
                "link_reasons": ["synthetic dependency"],
            }

        candidates_manifest = {
            "schema_version": "1.0",
            "audit_workdir": str(source),
            "candidates": [issue_candidate(*item) for item in issue_specs],
            "groups": [
                dep_group("G001", ["I057", "I071", "I099"], ["34"], "I057"),
                dep_group("G002", ["I062", "I071", "I099"], ["theorem 4.1"], "I062"),
                dep_group("G003", ["I137", "I138", "I166"], ["63"], "I137"),
                dep_group("G004", ["I138", "I166", "I186"], ["64"], "I138"),
                dep_group("G005", ["I157", "I166", "I171"], ["82"], "I157"),
                dep_group("G006", ["I162", "I171"], ["83"], "I162"),
                dep_group("G007", ["I203", "I204", "I209"], ["proposition d.2"], "I203"),
                dep_group("G008", ["I209", "I210"], ["theorem d.1"], "I209"),
                dep_group("G009", ["I300"], ["uniform", "asymptotic"], "I300"),
            ],
        }
        _write_json(candidates_path, candidates_manifest)
        watched = [session_paths(source)["manifest"], session_paths(source)["issues"], candidates_path]
        before_files = {path: path.read_text(encoding="utf-8") for path in watched}

        manifest = prepare_issue_recheck_families(source, output, candidates_json=candidates_path)
        _assert(manifest["source_unmodified_by_script"], manifest)
        _assert((output / "issue_recheck_families.json").exists(), "JSON output missing")
        _assert((output / "issue_recheck_families.md").exists(), "Markdown output missing")
        families = manifest["families"]
        family_sets = [set(family["all_issue_ids"]) for family in families]
        _assert(any({"I057", "I062", "I071", "I099"} <= issue_ids for issue_ids in family_sets), families)
        _assert(any({"I137", "I138", "I166", "I186"} <= issue_ids for issue_ids in family_sets), families)
        _assert(any({"I157", "I162", "I171"} <= issue_ids for issue_ids in family_sets), families)
        _assert(any({"I203", "I204", "I209", "I210"} <= issue_ids for issue_ids in family_sets), families)
        _assert(not any({"I137", "I157"} <= issue_ids for issue_ids in family_sets), families)
        _assert(not any("I300" in issue_ids and len(issue_ids) > 1 for issue_ids in family_sets), families)
        _assert("I300" in manifest["summary"]["high_critical_issue_ids_not_assigned_to_family"], manifest["summary"])
        _assert(any(item["issue_id"] == "I166" for item in manifest["summary"]["issues_appearing_in_multiple_families"]), manifest["summary"])
        for path, text in before_files.items():
            _assert(path.read_text(encoding="utf-8") == text, f"source file changed: {path}")
        try:
            prepare_issue_recheck_families(source, source / "nested_output", candidates_json=candidates_path)
        except RuntimeError as exc:
            _assert("inside the source audit workdir" in str(exc), str(exc))
        else:
            raise RegressionFailure("Issue family script allowed output inside source audit workdir")


def test_post_audit_review_summary_helpers() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_review_summary_regression_") as tmp:
        root = Path(tmp)
        source = root / "source_audit"
        session = _seed_state(source)
        _write_json(
            session_paths(source)["issues"],
            {
                "updated_at": NEW,
                "issues": [
                    {
                        "issue_id": "I057",
                        "chunk_id": "chunk_026",
                        "status": "open",
                        "severity": "medium",
                        "title": "Proof cites equation (34), the identity being proved",
                        "description": "Reference should point to the finite-difference identity.",
                        "evidence": "The proof cites the formula it is proving.",
                        "proposed_fix": "Cite the earlier identity plus Leibniz rule.",
                        "tags": ["circular-citation", "wrong-reference"],
                    },
                    {
                        "issue_id": "I071",
                        "chunk_id": "chunk_033",
                        "status": "open",
                        "severity": "high",
                        "title": "Theorem 4.1 inherits equation (34) dependence",
                        "description": "Downstream warning about equation (34).",
                        "evidence": "The theorem uses equation (34).",
                        "proposed_fix": "Group under I057 after review.",
                        "tags": ["dependency", "downstream"],
                    },
                ],
            },
        )
        _write_json(
            session_paths(source)["manifest"],
            {
                "chunks": [
                    {"chunk_id": "chunk_026", "chunk_index": 26, "page_start": 10, "page_end": 10},
                    {"chunk_id": "chunk_033", "chunk_index": 33, "page_start": 14, "page_end": 14},
                ]
            },
        )
        issue_rechecks_path = source / "state" / "issue_rechecks.json"
        _write_json(
            issue_rechecks_path,
            {
                "schema_version": 1,
                "updated_at": NEW,
                "rechecks": [
                    {
                        "recheck_id": "recheck_F004_001",
                        "family_id": "F004",
                        "source_result_path": "/tmp/family_recheck_result.json",
                        "source_output_dir": "/tmp/family_recheck",
                        "accepted_at": NEW,
                        "review_method": "llm_issue_family_recheck",
                        "verdict": "I057 is upstream; I071 is downstream-covered.",
                        "upstream_issue_ids": ["I057"],
                        "downstream_issue_ids": ["I071"],
                        "false_positive_issue_ids": [],
                        "recommended_severity_by_issue": [
                            {"issue_id": "I057", "severity": "medium", "rationale": "repairable reference issue"},
                            {"issue_id": "I071", "severity": "downstream-covered", "rationale": "not independent"},
                        ],
                        "recommended_status_by_issue": [
                            {"issue_id": "I071", "status": "downstream-covered", "rationale": "group under I057"}
                        ],
                        "grouping_recommendations": [
                            {
                                "upstream_issue_id": "I057",
                                "downstream_issue_ids": ["I071"],
                                "rationale": "The later issue is a consequence of the same equation (34) citation.",
                            }
                        ],
                        "final_report_treatment": "Report I057 and mention I071 as downstream-covered.",
                        "evidence_for": ["The proof cites equation (34)."],
                        "evidence_against": [],
                        "confidence": "medium",
                        "needs_human_review": False,
                        "summary": "Group downstream issue under upstream citation issue.",
                    }
                ],
            },
        )
        before_issues = session_paths(source)["issues"].read_text(encoding="utf-8")
        before_rechecks = issue_rechecks_path.read_text(encoding="utf-8")

        summary = runtime.load_post_audit_review_summary(session)
        _assert(summary["accepted_rechecks"]["accepted_recheck_count"] == 1, summary)
        _assert(summary["accepted_rechecks"]["downstream_covered_issue_count"] == 1, summary)
        _assert(not summary["candidate_inventory"]["available"], summary["candidate_inventory"])

        prepared = runtime.prepare_post_audit_review_summary(session)
        review_dir = source / "review"
        _assert((review_dir / "rerun_recheck_candidates.json").exists(), "candidate sidecar missing")
        _assert((review_dir / "issue_recheck_families.json").exists(), "family sidecar missing")
        _assert(prepared["candidate_inventory"]["available"], prepared["candidate_inventory"])
        _assert(prepared["issue_families"]["available"], prepared["issue_families"])
        _assert(session_paths(source)["issues"].read_text(encoding="utf-8") == before_issues, "issues state changed")
        _assert(issue_rechecks_path.read_text(encoding="utf-8") == before_rechecks, "issue recheck sidecar changed")

        family_payload = {
            "schema_version": "1.0",
            "generated_at": NEW,
            "audit_workdir": str(source),
            "summary": {
                "total_families": 1,
                "total_issue_ids_covered_by_families": 2,
                "high_critical_issue_ids_not_assigned_to_family": [],
            },
            "families": [
                {
                    "family_id": "F004",
                    "title": "Equation (34) / Theorem 4.1 dependency chain",
                    "primary_upstream_issue_ids": ["I057"],
                    "downstream_issue_ids": ["I071"],
                    "related_issue_ids": [],
                    "all_issue_ids": ["I057", "I071"],
                    "main_references": ["34", "Theorem 4.1"],
                    "main_symbols": [],
                    "chunks": [{"chunk_id": "chunk_026"}, {"chunk_id": "chunk_033"}],
                    "recommended_action": "group_downstream_under_upstream",
                    "priority": "high",
                    "review_notes": "Synthetic review family.",
                    "source_group_ids": ["G001"],
                }
            ],
        }
        _write_json(review_dir / "issue_recheck_families.json", family_payload)
        family_summary = runtime.load_post_audit_review_summary(session)
        family = family_summary["issue_families"]["families"][0]
        _assert(family["family_id"] == "F004", family)
        _assert(family["accepted_recheck_exists"], family)
        _assert(family["chunks"][0]["chunk_id"] == "chunk_026", family)
        _assert(family["review_notes"] == "Synthetic review family.", family)
        _assert(family["dry_run_output_dir"].endswith("review/family_rechecks/F004_dryrun"), family)

        dry_run = runtime.prepare_issue_family_recheck_dry_run(session, "F004", max_context_chars=6000)
        dry_manifest = dry_run["manifest"]
        dry_dir = review_dir / "family_rechecks" / "F004_dryrun"
        _assert(dry_manifest["dry_run"], dry_manifest)
        _assert(not dry_manifest["would_call_api"], dry_manifest)
        for name in ("family_recheck_manifest.json", "family_recheck_prompt.txt", "family_recheck_evidence.json", "family_recheck_notes.md"):
            _assert((dry_dir / name).exists(), f"missing review dry-run artifact {name}")
        _assert(not (dry_dir / "family_recheck_result.json").exists(), "GUI dry-run wrote live result")
        _assert(session_paths(source)["issues"].read_text(encoding="utf-8") == before_issues, "dry run changed issues")
        _assert(issue_rechecks_path.read_text(encoding="utf-8") == before_rechecks, "dry run changed recheck sidecar")

        import scripts.run_issue_family_recheck as family_recheck_runner

        original_live_runner = family_recheck_runner.run_issue_family_recheck

        def fake_live_runner(
            audit_workdir: Path,
            families_json: Path,
            family_id: str,
            output_dir: Path,
            **kwargs: Any,
        ) -> dict[str, Any]:
            _assert(kwargs.get("live") is True, kwargs)
            _assert(kwargs.get("allow_output_inside_audit") is True, kwargs)
            output_dir.mkdir(parents=True, exist_ok=True)
            _write_json(output_dir / "family_recheck_evidence.json", {"family_id": family_id})
            _write_text(output_dir / "family_recheck_prompt.txt", "synthetic live prompt")
            _write_text(output_dir / "family_recheck_notes.md", "synthetic live notes")
            _write_json(output_dir / "family_recheck_raw_response.json", {"id": "resp_synthetic"})
            _write_text(output_dir / "family_recheck_raw_response.txt", "{}")
            _write_json(output_dir / "family_recheck_result.json", {"family_id": family_id, "summary": "synthetic"})
            _write_text(output_dir / "family_recheck_result.md", "{}")
            _write_json(output_dir / "usage_cost.json", {"cost": {"total_cost": 0.0}})
            manifest = {
                "schema_version": "1.0",
                "audit_workdir": str(audit_workdir),
                "families_json": str(families_json),
                "family_id": family_id,
                "output_dir": str(output_dir),
                "live": True,
                "dry_run": False,
                "would_call_api": True,
                "live_result": {"response_id": "resp_synthetic", "status": "completed"},
                "source_unmodified_by_script": True,
            }
            _write_json(output_dir / "family_recheck_manifest.json", manifest)
            return manifest

        try:
            family_recheck_runner.run_issue_family_recheck = fake_live_runner
            live = runtime.run_issue_family_recheck_live(session, "F004", timestamp="20260601T123456Z")
        finally:
            family_recheck_runner.run_issue_family_recheck = original_live_runner
        live_manifest = live["manifest"]
        live_dir = review_dir / "family_rechecks" / "F004_live_20260601T123456Z"
        _assert(live_manifest["live"], live_manifest)
        _assert(live_manifest["would_call_api"], live_manifest)
        _assert((live_dir / "family_recheck_result.json").exists(), "live result missing")
        _assert(session_paths(source)["issues"].read_text(encoding="utf-8") == before_issues, "live run changed issues")
        _assert(issue_rechecks_path.read_text(encoding="utf-8") == before_rechecks, "live run auto-imported recheck sidecar")
        live_summary = live["review_summary"]
        live_family = live_summary["issue_families"]["families"][0]
        _assert(live_family["latest_recheck_output_dir"] == str(live_dir), live_family)
        _assert(live_family["latest_recheck_result_path"] == str(live_dir / "family_recheck_result.json"), live_family)

        accepted_result_dir = root / "accepted_family_recheck"
        accepted_result_path = accepted_result_dir / "family_recheck_result.json"
        _write_json(
            accepted_result_path,
            {
                "family_id": "F004",
                "verdict": "Keep I057 upstream and group I071 downstream.",
                "upstream_issue_ids": ["I057"],
                "downstream_issue_ids": ["I071"],
                "false_positive_issue_ids": [],
                "recommended_severity_by_issue": [
                    {"issue_id": "I057", "severity": "medium", "rationale": "repairable citation issue"},
                    {"issue_id": "I071", "severity": "downstream-covered", "rationale": "covered by I057"},
                ],
                "recommended_status_by_issue": [
                    {"issue_id": "I071", "status": "downstream-covered", "rationale": "group under I057"}
                ],
                "grouping_recommendations": [
                    {"upstream_issue_id": "I057", "downstream_issue_ids": ["I071"], "rationale": "same dependency chain"}
                ],
                "final_report_treatment": "Report I057; mention I071 as downstream-covered.",
                "evidence_for": ["Synthetic evidence."],
                "evidence_against": [],
                "confidence": "medium",
                "needs_human_review": False,
                "summary": "Synthetic accepted recheck.",
            },
        )
        imported = runtime.import_accepted_issue_family_recheck(session, accepted_result_path)
        _assert(imported["manifest"]["canonical_issue_mutation"] is False, imported)
        _assert((source / "logs" / "issue_recheck_decisions.jsonl").exists(), "import log missing")
        imported_sidecar = json.loads(issue_rechecks_path.read_text(encoding="utf-8"))
        _assert(len(imported_sidecar["rechecks"]) == 2, imported_sidecar)
        _assert(session_paths(source)["issues"].read_text(encoding="utf-8") == before_issues, "import changed issues")

        source_without_sidecar = root / "source_without_sidecar_audit"
        session_without_sidecar = _seed_state(source_without_sidecar)
        summary_without_sidecar = runtime.load_post_audit_review_summary(session_without_sidecar)
        _assert(summary_without_sidecar["available"], summary_without_sidecar)
        _assert(not summary_without_sidecar["accepted_rechecks"]["available"], summary_without_sidecar)


def test_run_issue_family_recheck_dry_run() -> None:
    validate_result_schema(RESULT_SCHEMA)
    bad_schema = json.loads(json.dumps(RESULT_SCHEMA))
    bad_schema["required"].append("missing_top_level_field")
    try:
        validate_result_schema(bad_schema)
    except ValueError as exc:
        _assert("extra_required" in str(exc), str(exc))
    else:
        raise RegressionFailure("Issue family recheck schema validation allowed an extra required key")

    with tempfile.TemporaryDirectory(prefix="math_audit_family_recheck_regression_") as tmp:
        root = Path(tmp)
        source = root / "source_audit"
        output = root / "family_recheck_out"
        family_json = root / "families.json"
        _seed_state(source)
        session = _synthetic_session(source)
        session["model"] = "gpt-5.5"
        session["reasoning_effort"] = "xhigh"
        session["tex_path"] = str(root / "paper.tex")
        _write_json(session_paths(source)["session"], session)
        _write_text(
            root / "paper.tex",
            r"""
\section{Finite differences}
\begin{equation}\label{E:finite-diff-k}
  \Delta^k f(0)=\sum_j (-1)^j \binom{k}{j} f(k-j).
\end{equation}
\begin{equation}\label{E:Leibniz}
  \Delta^k(fg)(0)=\sum_j \binom{k}{j}\Delta^j f(0)\Delta^{k-j}g(j).
\end{equation}
\begin{equation}\label{E:fg}
  e^{nx/k}(1-x/k)^n.
\end{equation}
\begin{prop}[Identity]\label{P:st2-lbnz-prop}
\begin{align}\label{E:st2-lbnz}
  S(n,k)=\sum_j \Lambda_j D_{n,k}(j).
\end{align}
By equation \eqref{E:st2-lbnz} and equation \eqref{E:Leibniz} applied to \eqref{E:fg}, we obtain the identity.
\end{prop}
\begin{thm}\label{T:finite-diff-exp}
The identity \eqref{E:st2-lbnz} yields an expansion.
\end{thm}
""",
        )
        _write_text(
            root / "paper.aux",
            "\n".join(
                [
                    r"\newlabel{E:finite-diff-k}{{29}{11}{A finite difference expansion}{equation.29}{}}",
                    r"\newlabel{E:Leibniz}{{31}{11}{Leibniz's formula}{equation.31}{}}",
                    r"\newlabel{E:fg}{{30}{11}{A finite difference expansion}{equation.30}{}}",
                    r"\newlabel{P:st2-lbnz-prop}{{4.1}{12}{Identity}{prop.4.1}{}}",
                    r"\newlabel{E:st2-lbnz}{{34}{12}{Identity}{equation.34}{}}",
                    r"\newlabel{T:finite-diff-exp}{{4.1}{12}{}{thm.4.1}{}}",
                ]
            ),
        )
        _write_json(
            session_paths(source)["manifest"],
            {
                "chunks": [
                    {"chunk_id": "chunk_026", "chunk_index": 26, "label": "Proposition 4.1", "page_start": 12, "page_end": 13},
                    {"chunk_id": "chunk_033", "chunk_index": 33, "label": "Theorem 4.1", "page_start": 14, "page_end": 15},
                ]
            },
        )
        issues = [
            {
                "issue_id": "I057",
                "severity": "high",
                "status": "open",
                "chunk_id": "chunk_026",
                "title": "The proof cites equation (34), the identity being proved",
                "location": "Proposition 4.1",
                "description": "The proof cites equation (34) while proving equation (34).",
                "evidence": "Use finite-difference representation and Leibniz rule instead.",
                "proposed_fix": "Cite equations (29), (31), and the factorization.",
                "tags": ["circular-citation"],
            },
            {
                "issue_id": "I071",
                "severity": "high",
                "status": "open",
                "chunk_id": "chunk_033",
                "title": "Theorem 4.1 inherits any unresolved gap in equation (34)",
                "location": "Theorem 4.1",
                "description": "The theorem uses equation (34).",
                "evidence": "Downstream dependency on the identity.",
                "proposed_fix": "Group under the upstream issue if it is real.",
                "tags": ["dependency"],
            },
        ]
        _write_json(session_paths(source)["issues"], {"issues": issues, "updated_at": NEW})
        _write_json(session_paths(source)["ledger"], {"assumptions": ["Equation (34) supports Theorem 4.1."], "notes": ["Leibniz rule is equation (31)."], "updated_at": NEW})
        structured_path = source / "responses" / "chunk_026.structured.json"
        _write_json(
            structured_path,
            {
                "assumptions_and_notation": ["Proposition 4.1 states equation (34)."],
                "verified_steps": ["The local proof invokes Leibniz's formula."],
                "ledger_updates": {"assumptions": [], "notes": ["Equation (34) is later used by Theorem 4.1."]},
                "issues": issues[:1],
            },
        )
        _append_jsonl(session_paths(source)["chunk_records"], {"chunk_id": "chunk_026", "chunk_index": 26, "time": NEW, "structured_response_path": str(structured_path)})
        check_path = source / "python_checks" / "chunk_026_check.py"
        _write_text(check_path, "print('synthetic check')\n")
        result_path = source / "verification_results" / "chunk_026_check.result.json"
        _write_json(result_path, {"chunk_id": "chunk_026", "status": "passed", "stdout": "ok"})
        family_payload = {
            "schema_version": "1.0",
            "families": [
                {
                    "family_id": "F004",
                    "title": "Equation (34) / Theorem 4.1 dependency chain",
                    "primary_upstream_issue_ids": ["I057"],
                    "downstream_issue_ids": ["I071"],
                    "related_issue_ids": [],
                    "all_issue_ids": ["I057", "I071"],
                    "main_references": ["34", "theorem 4.1"],
                    "main_symbols": [],
                    "chunks": [{"chunk_id": "chunk_026"}, {"chunk_id": "chunk_033"}],
                    "recommended_action": "group_downstream_under_upstream",
                    "priority": "high",
                    "review_notes": "Synthetic family.",
                    "source_group_ids": ["G001"],
                }
            ],
        }
        _write_json(family_json, family_payload)
        watched = [
            session_paths(source)["session"],
            session_paths(source)["manifest"],
            session_paths(source)["issues"],
            session_paths(source)["ledger"],
            session_paths(source)["chunk_records"],
            structured_path,
            check_path,
            result_path,
            family_json,
            root / "paper.tex",
            root / "paper.aux",
        ]
        before_files = {path: path.read_text(encoding="utf-8") for path in watched}

        manifest = run_issue_family_recheck(source, family_json, "F004", output)
        _assert(manifest["dry_run"], manifest)
        _assert(not manifest["would_call_api"], manifest)
        _assert(manifest["source_unmodified_by_script"], manifest)
        for name in ("family_recheck_manifest.json", "family_recheck_prompt.txt", "family_recheck_evidence.json", "family_recheck_notes.md"):
            _assert((output / name).exists(), f"missing {name}")
        for name in ("family_recheck_raw_response.json", "family_recheck_raw_response.txt", "family_recheck_result.json", "family_recheck_result.md", "usage_cost.json"):
            _assert(not (output / name).exists(), f"dry-run wrote live artifact {name}")
        evidence = json.loads((output / "family_recheck_evidence.json").read_text(encoding="utf-8"))
        prompt = (output / "family_recheck_prompt.txt").read_text(encoding="utf-8")
        _assert({"I057", "I071"} <= {item["issue_id"] for item in evidence["issues"]["all"]}, evidence)
        _assert(any("E:st2-lbnz" in item.get("excerpt", "") for item in evidence["tex_excerpts"]), evidence["tex_excerpts"])
        _assert("Treat all prior audit issues as provisional findings" in prompt, prompt)
        for path, text in before_files.items():
            _assert(path.read_text(encoding="utf-8") == text, f"source file changed: {path}")
        try:
            run_issue_family_recheck(source, family_json, "F004", source / "nested_output")
        except RuntimeError as exc:
            _assert("inside the source audit workdir" in str(exc), str(exc))
        else:
            raise RegressionFailure("Issue family recheck allowed output inside source audit workdir")


def test_import_issue_family_recheck_script() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_import_family_recheck_regression_") as tmp:
        root = Path(tmp)
        source = root / "source_audit"
        result_dir = root / "family_recheck_live_output"
        result_path = result_dir / "family_recheck_result.json"
        _seed_state(source)
        issues_before = session_paths(source)["issues"].read_text(encoding="utf-8")
        result = {
            "family_id": "F004",
            "verdict": "Group downstream issues under upstream repairable citation issue.",
            "upstream_issue_ids": ["I057", "I062"],
            "downstream_issue_ids": ["I071", "I099", "I183", "I191"],
            "false_positive_issue_ids": [],
            "recommended_severity_by_issue": [
                {"issue_id": "I057", "severity": "medium", "rationale": "Repairable self-citation/reference issue."},
                {"issue_id": "I071", "severity": "low/downstream", "rationale": "Covered by upstream issue."},
            ],
            "recommended_status_by_issue": [
                {"issue_id": "I057", "status": "open-upstream", "rationale": "Keep as main issue."},
                {"issue_id": "I071", "status": "downstream-covered", "rationale": "Do not count independently."},
            ],
            "grouping_recommendations": [
                {"upstream_issue_id": "I057", "downstream_issue_ids": ["I071", "I099"], "rationale": "Same equation (34) dependency chain."}
            ],
            "final_report_treatment": "Report I057 and I062, group downstream consequences.",
            "evidence_for": ["The proof cites equation (34) while proving it."],
            "evidence_against": ["The algebra appears repairable after citation correction."],
            "confidence": "medium",
            "needs_human_review": True,
            "summary": "Advisory result only; do not mutate canonical issue records.",
        }
        _write_json(result_path, result)
        sidecar_path = source / "state" / "issue_rechecks.json"
        log_path = source / "logs" / "issue_recheck_decisions.jsonl"

        dry_run = import_issue_family_recheck(source, result_path, result_dir)
        _assert(dry_run["dry_run"], dry_run)
        _assert(dry_run["family_id"] == "F004", dry_run)
        _assert("I071" in dry_run["affected_issue_ids"], dry_run)
        _assert(not sidecar_path.exists(), "dry-run wrote issue_rechecks.json")
        _assert(not log_path.exists(), "dry-run wrote issue_recheck_decisions.jsonl")
        _assert(session_paths(source)["issues"].read_text(encoding="utf-8") == issues_before, "dry-run changed issues.json")

        accepted = import_issue_family_recheck(source, result_path, result_dir, accept=True)
        _assert(not accepted["dry_run"], accepted)
        _assert(sidecar_path.exists(), "accept did not write sidecar")
        _assert(log_path.exists(), "accept did not write decision log")
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        _assert(sidecar["schema_version"] == 1, sidecar)
        _assert(len(sidecar["rechecks"]) == 1, sidecar)
        _assert(sidecar["rechecks"][0]["family_id"] == "F004", sidecar)
        _assert(sidecar["rechecks"][0]["review_method"] == "llm_issue_family_recheck", sidecar)
        _assert(sidecar["rechecks"][0]["source_result_path"] == str(result_path.resolve()), sidecar)
        log_rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        _assert(len(log_rows) == 1, log_rows)
        _assert(log_rows[0]["action"] == "accepted_issue_family_recheck", log_rows)
        _assert(log_rows[0]["canonical_issue_mutation"] is False, log_rows)
        _assert(session_paths(source)["issues"].read_text(encoding="utf-8") == issues_before, "accept changed issues.json")

        accepted_again = import_issue_family_recheck(source, result_path, result_dir, accept=True)
        _assert(accepted_again["existing_recheck_count"] == 1, accepted_again)
        sidecar_again = json.loads(sidecar_path.read_text(encoding="utf-8"))
        _assert(len(sidecar_again["rechecks"]) == 2, sidecar_again)
        _assert(sidecar_again["rechecks"][0]["recheck_id"] != sidecar_again["rechecks"][1]["recheck_id"], sidecar_again)
        log_rows_again = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        _assert(len(log_rows_again) == 2, log_rows_again)

        invalid_result = dict(result)
        invalid_result.pop("family_id")
        invalid_path = result_dir / "invalid_family_recheck_result.json"
        _write_json(invalid_path, invalid_result)
        try:
            import_issue_family_recheck(source, invalid_path, result_dir)
        except RuntimeError as exc:
            _assert("missing required fields" in str(exc), str(exc))
        else:
            raise RegressionFailure("Issue family recheck import accepted a result missing family_id")


def test_python_check_literal_newline_escape_repair() -> None:
    with tempfile.TemporaryDirectory(prefix="math_audit_python_check_escape_") as tmp:
        workdir = Path(tmp) / "paper_audit"
        session = _synthetic_session(workdir)
        (workdir / "python_checks").mkdir(parents=True)
        (workdir / "latex_patches").mkdir(parents=True)

        malformed_code = "def value():\\n    return 3\\n\\nprint(value())"
        valid_code_with_literal_escape = "print('literal \\\\n marker stays in string')"
        audit = runtime._coerce_audit_payload(
            {
                "python_checks": [
                    {
                        "purpose": "Repair double-escaped code",
                        "description": "Synthetic check",
                        "expected_outcome": "The generated file parses.",
                        "code": malformed_code,
                    },
                    {
                        "purpose": "Preserve valid code",
                        "description": "Synthetic check",
                        "expected_outcome": "The literal escape remains literal.",
                        "code": valid_code_with_literal_escape,
                    },
                ]
            }
        )

        repaired = audit["python_checks"][0]["code"]
        preserved = audit["python_checks"][1]["code"]
        _assert(repaired.count("\n") >= 3, repr(repaired))
        _assert(r"\n" not in repaired, repr(repaired))
        _assert(preserved == valid_code_with_literal_escape, repr(preserved))
        compile(repaired, "<repaired_python_check>", "exec")
        compile(preserved, "<preserved_python_check>", "exec")

        paths = runtime.save_patch_and_code_files(session, "chunk_001", audit)
        _assert(len(paths["python_paths"]) == 2, paths)
        repaired_file = (workdir / "python_checks" / "chunk_001_check_01.py").read_text(encoding="utf-8")
        preserved_file = (workdir / "python_checks" / "chunk_001_check_02.py").read_text(encoding="utf-8")
        _assert(repaired_file.count("\n") >= 4, repr(repaired_file))
        _assert(r"\n" in preserved_file, repr(preserved_file))
        compile(repaired_file, "chunk_001_check_01.py", "exec")
        compile(preserved_file, "chunk_001_check_02.py", "exec")


def test_python_check_trailing_json_artifact_repair() -> None:
    valid = "data = {'values': [1, {'x': 2}], 'marker': '},{'}\nprint(data)\n"
    _assert(runtime._repair_python_check_code_escapes(valid) == valid, "valid Python was changed")
    compile(runtime._repair_python_check_code_escapes(valid), "<valid_python_check>", "exec")

    cases = [
        ("print('ok')},{", "print('ok')"),
        ("print('ok')},", "print('ok')"),
        ("print('ok')}, {", "print('ok')"),
        ("print('ok')}]", "print('ok')"),
        (
            "import math\nprint('1/log(2) =', 1 / math.log(2))},{",
            "import math\nprint('1/log(2) =', 1 / math.log(2))",
        ),
    ]
    for source, expected in cases:
        repaired = runtime._repair_python_check_code_escapes(source)
        _assert(repaired == expected, repr((source, repaired, expected)))
        compile(repaired, "<repaired_trailing_json_artifact>", "exec")

    malformed = "for x in range(3)\n    print(x)},"
    repaired_malformed = runtime._repair_python_check_code_escapes(malformed)
    try:
        compile(repaired_malformed, "<still_malformed_python_check>", "exec")
    except SyntaxError:
        pass
    else:
        raise RegressionFailure("Nontrivial malformed Python became parseable unexpectedly")

    audit = runtime._coerce_audit_payload(
        {
            "python_checks": [
                {
                    "purpose": "Synthetic trailing artifact",
                    "description": "Synthetic check",
                    "expected_outcome": "The generated file parses.",
                    "code": "print('ok')},{",
                }
            ]
        }
    )
    check = audit["python_checks"][0]
    _assert(check["code"] == "print('ok')", repr(check))
    _assert(check.get("normalization_applied") == "trailing_json_separator_trim", repr(check))


def _run_case(name: str, func: Callable[[], None]) -> RegressionResult:
    try:
        func()
    except Exception as exc:
        return RegressionResult(name=name, passed=False, detail=f"{type(exc).__name__}: {exc}")
    return RegressionResult(name=name, passed=True, detail="passed")


def main() -> int:
    cases: list[tuple[str, Callable[[], None]]] = [
        ("report freshness detection", test_report_freshness_detection),
        ("audit completion builds full and concise reports", test_audit_completion_builds_full_and_concise_reports),
        ("invalidated verification inventory warning", test_invalidated_verification_inventory_warning),
        ("successful selected rerun restores completed status", test_successful_selected_rerun_restores_completed_status),
        ("PDF display label heuristics", test_pdf_display_labels),
        ("status backfill does not rewrite manifest", test_status_display_label_backfill_does_not_rewrite_manifest),
        ("running audit context block", test_running_audit_context_block),
        ("TeX macro glossary prompt block", test_tex_macro_glossary_in_chunk_prompt),
        ("request size diagnostics", test_request_size_diagnostics),
        ("fresh context mode scaffolding", test_fresh_context_mode_scaffolding),
        ("fresh context issue generic-term downweighting", test_fresh_context_issue_retrieval_downweights_generic_terms),
        ("context mode mixing guardrails", test_context_mode_mixing_guardrails),
        ("chunk completion log line formatting", test_chunk_completion_log_line_formatting),
        ("plain text scroll preservation helper", test_plain_text_scroll_preservation_helper),
        ("review tab feature flag", test_review_tab_feature_flag),
        ("completed status reconciliation", test_completed_status_reconciles_from_chunk_records),
        ("persistent audit log preview", test_persistent_audit_log_preview),
        ("resume preserves saved audit context mode", test_resume_preserves_saved_audit_context_mode),
        ("discussion legacy thread and context DB safety", test_discussion_legacy_thread_and_context_db_safety),
        ("report LaTeX unicode math safety", test_report_latex_unicode_math_safety),
        ("issue severity summary in audit summary", test_issue_severity_summary_in_audit_summary),
        ("source ingestion diagnostics in reports", test_source_ingestion_diagnostics_in_reports),
        ("AUX printed label display in reports", test_aux_printed_label_display_in_reports),
        ("concise report notable incorrect reference issues", test_concise_report_notable_incorrect_reference_issues),
        ("issue recheck overlay in reports", test_issue_recheck_overlay_in_reports),
        ("fresh rerun request metadata", test_fresh_rerun_request_metadata),
        ("usage cache diagnostics", test_usage_cache_diagnostics),
        ("retryable file download timeout detection", test_retryable_file_download_timeout_detection),
        ("file download timeout auto-retry decisions", test_file_download_timeout_auto_retry_decisions),
        ("context mode comparison script", test_prepare_context_mode_comparison_script),
        ("context mode A/B dry-run script", test_run_context_mode_ab_test_dry_run),
        ("issue recheck candidate script", test_prepare_issue_recheck_candidates_script),
        ("rerun/recheck candidate inventory script", test_prepare_rerun_recheck_candidates_script),
        ("issue recheck family consolidation script", test_prepare_issue_recheck_families_script),
        ("post-audit review summary helpers", test_post_audit_review_summary_helpers),
        ("issue family recheck dry-run script", test_run_issue_family_recheck_dry_run),
        ("issue family recheck import script", test_import_issue_family_recheck_script),
        ("python check literal newline escape repair", test_python_check_literal_newline_escape_repair),
        ("python check trailing JSON artifact repair", test_python_check_trailing_json_artifact_repair),
    ]
    results = [_run_case(name, func) for name, func in cases]

    print("Math Audit regression check")
    print(f"Project root: {PROJECT_ROOT}")
    print()
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.name}")
        print(f"       {result.detail}")

    failed = [result for result in results if not result.passed]
    print()
    if failed:
        print(f"Result: FAILED ({len(failed)} regression check(s) failed).")
        return 1
    print(f"Result: OK ({len(results)} regression check(s) passed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
