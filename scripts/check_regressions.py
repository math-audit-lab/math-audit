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
    _audit_summary_markdown,
    _audit_summary_tex,
    _build_running_audit_context_for_chunk,
    _report_latex_paragraph_local,
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
from scripts.prepare_context_mode_comparison import prepare_context_mode_comparison
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
        _assert(prompt_text.index("Retrieved fresh-context audit database context:") < prompt_text.index("Chunk text:"), prompt_text)
        _assert(fresh_chunk.get("_retrieved_context_entry_count", 0) > 0, fresh_chunk)

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


def test_report_latex_unicode_math_safety() -> None:
    text = (
        "The bound is $c_2√nΛ≤1/2$ and $√(n+1)≥λ$. "
        "Literal control-escape artifact: $\\rho k\\u000b\\lambda$."
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
        _assert("\\\\Lambda" not in rendered, rendered)
        _assert("\\\\lambda" not in rendered, rendered)


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
        ("resume preserves saved audit context mode", test_resume_preserves_saved_audit_context_mode),
        ("report LaTeX unicode math safety", test_report_latex_unicode_math_safety),
        ("issue severity summary in audit summary", test_issue_severity_summary_in_audit_summary),
        ("fresh rerun request metadata", test_fresh_rerun_request_metadata),
        ("usage cache diagnostics", test_usage_cache_diagnostics),
        ("retryable file download timeout detection", test_retryable_file_download_timeout_detection),
        ("file download timeout auto-retry decisions", test_file_download_timeout_auto_retry_decisions),
        ("context mode comparison script", test_prepare_context_mode_comparison_script),
        ("context mode A/B dry-run script", test_run_context_mode_ab_test_dry_run),
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
