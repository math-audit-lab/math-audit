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

from audit_chunking import ensure_chunk_display_labels, pdf_chunk_display_label
from audit_policy_hooks import _build_running_audit_context_for_chunk, build_user_message_for_chunk
from audit_runtime import (
    _retryable_response_failure_reason,
    _should_reattach_pdf_for_chunk_retry,
    get_audit_status,
    get_report_freshness,
)
from audit_state import save_json, session_paths


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
            _should_reattach_pdf_for_chunk_retry(session, chunk),
            "retryable file download timeout did not request PDF reattachment",
        )


def _run_case(name: str, func: Callable[[], None]) -> RegressionResult:
    try:
        func()
    except Exception as exc:
        return RegressionResult(name=name, passed=False, detail=f"{type(exc).__name__}: {exc}")
    return RegressionResult(name=name, passed=True, detail="passed")


def main() -> int:
    cases: list[tuple[str, Callable[[], None]]] = [
        ("report freshness detection", test_report_freshness_detection),
        ("PDF display label heuristics", test_pdf_display_labels),
        ("status backfill does not rewrite manifest", test_status_display_label_backfill_does_not_rewrite_manifest),
        ("running audit context block", test_running_audit_context_block),
        ("retryable file download timeout detection", test_retryable_file_download_timeout_detection),
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
