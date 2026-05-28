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
    _build_running_audit_context_for_chunk,
    _report_latex_paragraph_local,
    build_concise_report_json,
    build_concise_report_markdown,
    build_concise_report_tex,
    build_user_message_for_chunk,
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
    report_latex_paragraph,
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

        json_escaped_tex = (
            "Recovered commands: $e^{-\\rho j}\\exp(-"
            + "\x0c"
            + "rac{\\rho j^2}{2k})+"
            + "\x08"
            + "lambda$."
        )
        rendered_json_escaped = renderer(json_escaped_tex)
        _assert(r"\frac{\rho j^2}{2k}" in rendered_json_escaped, rendered_json_escaped)
        _assert(r"\blambda" in rendered_json_escaped, rendered_json_escaped)
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

        unsupported_unicode = "Tag: l格range-inversion."
        rendered_unicode = renderer(unsupported_unicode)
        _assert("格" not in rendered_unicode, rendered_unicode)
        _assert("[U+683C]" in rendered_unicode, rendered_unicode)

    verbatim = runtime._verbatim_block('print("max_E≈1")  # l格range')
    _assert("≈" not in verbatim, verbatim)
    _assert("格" not in verbatim, verbatim)
    _assert(r"\approx" in verbatim, verbatim)
    _assert("[U+683C]" in verbatim, verbatim)


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


def _run_case(name: str, func: Callable[[], None]) -> RegressionResult:
    try:
        func()
    except Exception as exc:
        return RegressionResult(name=name, passed=False, detail=f"{type(exc).__name__}: {exc}")
    return RegressionResult(name=name, passed=True, detail="passed")


def main() -> int:
    cases: list[tuple[str, Callable[[], None]]] = [
        ("report freshness detection", test_report_freshness_detection),
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
        ("completed status reconciliation", test_completed_status_reconciles_from_chunk_records),
        ("persistent audit log preview", test_persistent_audit_log_preview),
        ("resume preserves saved audit context mode", test_resume_preserves_saved_audit_context_mode),
        ("discussion legacy thread and context DB safety", test_discussion_legacy_thread_and_context_db_safety),
        ("report LaTeX unicode math safety", test_report_latex_unicode_math_safety),
        ("issue severity summary in audit summary", test_issue_severity_summary_in_audit_summary),
        ("concise report notable incorrect reference issues", test_concise_report_notable_incorrect_reference_issues),
        ("fresh rerun request metadata", test_fresh_rerun_request_metadata),
        ("usage cache diagnostics", test_usage_cache_diagnostics),
        ("retryable file download timeout detection", test_retryable_file_download_timeout_detection),
        ("file download timeout auto-retry decisions", test_file_download_timeout_auto_retry_decisions),
        ("context mode comparison script", test_prepare_context_mode_comparison_script),
        ("context mode A/B dry-run script", test_run_context_mode_ab_test_dry_run),
        ("issue recheck candidate script", test_prepare_issue_recheck_candidates_script),
        ("rerun/recheck candidate inventory script", test_prepare_rerun_recheck_candidates_script),
        ("issue recheck family consolidation script", test_prepare_issue_recheck_families_script),
        ("issue family recheck dry-run script", test_run_issue_family_recheck_dry_run),
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
