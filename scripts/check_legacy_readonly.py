#!/usr/bin/env python3
"""Byte-preservation checks for legacy session/status/report/discussion readers."""
from __future__ import annotations

import copy
import json
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None for path in root.rglob("*")}


def run_checks() -> None:
    with patch.object(socket.socket, "connect", side_effect=AssertionError("Offline checks forbid networking")):
        import audit_runtime as runtime
        from audit_state import init_state_files, save_json, session_paths, workdir_from_pdf

        turns = [
            {"model": "gpt-5.5", "usage": {"input_tokens": 1000000, "output_tokens": 1000000}, "cost": {"total_cost": 19.123456}},
            {"model": "gpt-6.1-sol", "usage": {"input_tokens": 1, "output_tokens": 1}, "cost": {"total_cost": 0.0, "approximate": True}},
            {"usage": {"input_tokens": 9000000}},
        ]
        original_turns = copy.deepcopy(turns)
        with patch.object(runtime, "compute_usage_cost", side_effect=AssertionError("Historical costs must not be repriced")):
            summary = runtime._qa_usage_summary_from_turns(turns)
        assert summary["cost_usd"] == 19.123456
        assert summary["cost_approximate"] is True and summary["cost_unavailable_turns"] == 1
        assert "saved charges only" in summary["cost_note"]
        assert turns == original_turns

        for model in ("gpt-5.5", "gpt-5.6-sol"):
            for with_qa in (False, True):
                with tempfile.TemporaryDirectory(prefix="math-audit-legacy-read-") as tmp:
                    pdf = Path(tmp) / "paper.pdf"
                    pdf.write_bytes(b"test fixture; never parsed")
                    root = workdir_from_pdf(pdf)
                    init_state_files(root, model, "historical-effort")
                    paths = session_paths(root)
                    session = {
                        "workdir": str(root), "pdf_path": str(pdf), "created_at": "2025-01-01T00:00:00Z",
                        "model": model, "reasoning_effort": "historical-effort", "reasoning_mode": "pro",
                        "audit_context_mode": "fresh_context_experimental", "last_response_id": "resp_legacy",
                        "conversation_id": "conv_legacy_audit", "audit_system_prompt": "original exact prompt",
                        "audit_system_prompt_meta": {"version": "old", "sha256": "old-digest"},
                        "next_chunk_index": 0, "pending": None,
                    }
                    if with_qa:
                        session["qa_threads"] = {
                            "legacy": {"thread_id": "legacy", "conversation_id": "conv_keep"},
                            "empty": {"thread_id": "empty", "conversation_id": None},
                        }
                        session["active_qa_thread_id"] = "empty"
                        (root / "qa").mkdir()
                        save_json(root / "qa" / "qa_001.json", {"time": "2025-01-01", "answer": "historical answer", "conversation_id": "conv_keep"})
                    usage = {
                        "model": model,
                        "totals": {"cost_usd": 7.123456, "input_tokens": 10},
                        "per_chunk": [{"chunk_id": "chunk_001", "model": model, "cost": {"total_cost": 7.123456}, "usage": {"input_tokens": 10, "output_tokens": 3}}],
                    }
                    save_json(paths["session"], session)
                    save_json(paths["usage"], usage)
                    save_json(paths["status"], {"status": "paused", "cost_usd": 7.123456})
                    save_json(paths["manifest"], {"chunks": [], "pdf_page_count": 0})
                    report_json = root / "reports" / "paper_audit_report.json"
                    save_json(report_json, {"generated_at": "2025-01-01T00:00:00Z", "latex_compile_health": {"status": "historical"}})
                    (root / "reports" / "paper_audit_report.tex").write_text("historical TeX")
                    before = _snapshot(root)
                    supplied = copy.deepcopy(session)
                    payload = runtime.get_audit_status(supplied, include_manifest=True)
                    assert supplied == session, "status inspection mutated its input dict"
                    for key in ("model", "reasoning_effort", "reasoning_mode", "audit_context_mode", "last_response_id", "conversation_id", "audit_system_prompt", "audit_system_prompt_meta"):
                        assert payload["session"][key] == session[key], key
                    assert payload["usage"] == usage, "historical usage/cost changed"
                    runtime.list_qa_threads(supplied)
                    runtime.load_qa_turns(supplied, active_thread_only=True)
                    assert supplied == session, "discussion inspection mutated its input dict"
                    runtime.get_failed_verification_chunks(str(pdf))
                    runtime.get_verification_suite_status(str(pdf))
                    runtime.load_post_audit_review_summary(str(pdf))
                    assert _snapshot(root) == before, "reading legacy status modified audit files or created directories"
                    assert json.loads(report_json.read_text())["latex_compile_health"] == {"status": "historical"}


if __name__ == "__main__":
    run_checks()
    print("PASS legacy readers preserve session, costs, responses, prompts, reports, and discussion bytes")
