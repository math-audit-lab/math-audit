#!/usr/bin/env python3
"""Offline request-wire and legacy-provenance regression checks. No network."""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audit_runtime as runtime
from audit_models import DEFAULT_MODEL, MODEL_CHOICES, model_display_name
from audit_requests import response_request_options, session_request_settings, validate_response_storage
from audit_state import init_state_files, save_json, session_paths

ROOT = Path(__file__).resolve().parents[1]


class StopBeforeAPIResult(RuntimeError):
    """Mock transport stop: no actual model execution occurs."""


def seed_session(root, model="gpt-6.1-sol", effort="xhigh"):
    pdf = root / "paper.pdf"
    pdf.write_bytes(b"offline fixture; never parsed")
    workdir = root / "paper_audit"
    init_state_files(workdir, model, effort)
    session = {
        "pdf_path": str(pdf), "workdir": str(workdir), "model": model,
        "reasoning_effort": effort, "reasoning_mode": "standard",
        "conversation_id": "original-conversation", "pdf_file_id": "offline-file",
        "background": True, "store": True, "pending": None,
        "audit_context_mode": runtime.DEFAULT_AUDIT_CONTEXT_MODE,
        "audit_system_prompt": "Saved original instructions", "developer_prompt_seeded": False,
        "audit_system_prompt_metadata": {"version": "original", "sha256": "original-hash"},
    }
    paths = session_paths(workdir)
    save_json(paths["session"], session)
    save_json(paths["manifest"], {"chunks": [{"chunk_id": "chunk_001", "chunk_index": 1}], "pdf_page_count": 1})
    return session


def file_snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class RequestMigrationChecks(unittest.TestCase):
    def test_sdk_wire_shape_without_network(self):
        import httpx
        from openai import OpenAI
        seen = []

        def handle(request):
            payload = json.loads(request.content)
            seen.append(payload)
            return httpx.Response(200, json={
                "id": "offline-response", "object": "response", "created_at": 0,
                "status": "completed", "model": payload["model"], "output": [],
            })

        with OpenAI(api_key="offline-test-placeholder", base_url="https://offline.invalid/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
            for model in MODEL_CHOICES:
                effort = "none" if model in {"gpt-6.1-terra", "gpt-6-luna"} else "xhigh"
                client.responses.create(**response_request_options(model, effort), input="offline fixture", background=True, store=True,
                    text={"format": {"type": "json_schema", "name": "offline", "strict": True,
                                     "schema": {"type": "object", "properties": {}, "additionalProperties": False, "required": []}}})
        self.assertEqual(len(seen), 4)
        for payload, model in zip(seen, MODEL_CHOICES):
            self.assertEqual(payload["model"], model)
            self.assertEqual(payload["prompt_cache_options"], {"ttl": "30m"})
            self.assertEqual(payload["reasoning"]["mode"], "standard")
            self.assertEqual(payload["reasoning"]["context"], "current_turn")
            self.assertEqual(payload["service_tier"], "default")
            self.assertTrue(payload["background"])
            self.assertTrue(payload["store"])
            self.assertEqual(payload["text"]["format"]["type"], "json_schema")
            for unsupported in ("extra_body", "prompt_cache_retention", "temperature", "top_p", "logprobs", "top_logprobs"):
                self.assertNotIn(unsupported, payload)

    def test_invalid_saved_and_requested_settings_never_migrate(self):
        good = {"model": "gpt-6.1-sol", "reasoning_effort": "max", "reasoning_mode": "pro"}
        self.assertEqual(session_request_settings(good), ("gpt-6.1-sol", "max", "pro"))
        self.assertEqual(session_request_settings(good, "gpt-6-astra"), ("gpt-6-astra", "max", "pro"))
        self.assertEqual(good, {"model": "gpt-6.1-sol", "reasoning_effort": "max", "reasoning_mode": "pro"})
        for invalid in ({"model": "gpt-5.5", "reasoning_effort": "xhigh"},
                        {"model": "gpt-5.6-sol", "reasoning_effort": "max"},
                        {"model": "gpt-6.1-sol"},
                        {"model": "gpt-6.1-sol", "reasoning_effort": "none"},
                        {"model": "gpt-6.1-sol", "reasoning_effort": "high", "reasoning_mode": "invalid"}):
            before = copy.deepcopy(invalid)
            with self.subTest(session=invalid), self.assertRaises(ValueError):
                session_request_settings(invalid, "gpt-6.1-sol", "xhigh")
            self.assertEqual(invalid, before)
        with self.assertRaises(ValueError):
            session_request_settings(good, reasoning_effort="unsupported")
        with self.assertRaises(ValueError):
            validate_response_storage({"background": True, "store": False})
        validate_response_storage({"background": False, "store": False})
        self.assertEqual(response_request_options(model_display_name(DEFAULT_MODEL), "xhigh")["model"], DEFAULT_MODEL)

    def test_all_responses_create_calls_use_request_policy(self):
        encountered = set()
        for path in (ROOT / "audit_runtime.py", ROOT / "scripts/run_issue_family_recheck.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "create"
                        and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "responses"):
                    continue
                function = node
                while not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    function = parents[function]
                encountered.add(function.name)
                helpers = [n for n in ast.walk(function) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "response_request_options"]
                self.assertTrue(helpers, f"{path.name}:{node.lineno} bypasses request compatibility policy")
                self.assertTrue(any(n.lineno <= node.end_lineno for n in helpers))
                forbidden = {"temperature", "top_p", "top_logprobs", "logprobs", "prompt_cache_retention"}
                self.assertFalse(forbidden.intersection(k.arg for k in node.keywords if k.arg))
        self.assertTrue({"process_one_chunk", "_run_qa_turn", "run_verification_finding_rechecks", "run_verification_technical_repairs"}.issubset(encountered))

    def test_legacy_live_entries_reject_before_client_or_disk_mutation(self):
        for model in ("gpt-5.5", "gpt-5.6-sol"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                session = seed_session(root, model)
                calls = [
                    lambda s: runtime.process_one_chunk(s, {"chunk_id": "chunk_001"}),
                    lambda s: runtime.rerun_selected_chunks(s, ["chunk_001"], model=DEFAULT_MODEL, reasoning_effort="xhigh"),
                    lambda s: runtime.rerun_failed_verification_chunks(s),
                    lambda s: runtime.run_verification_finding_rechecks(s),
                    lambda s: runtime.run_verification_technical_repairs(s),
                    lambda s: runtime.run_issue_family_recheck_live(s, "family_001"),
                    lambda s: runtime._run_qa_turn(s, "inspect evidence", "audit"),
                    lambda s: runtime.audit_the_paper(s["pdf_path"], verbose=False),
                    lambda s: runtime.recover_pending_chunk(dict(s, pending={"response_id": "old", "chunk_id": "chunk_001"})),
                ]
                for operation in calls:
                    before = file_snapshot(root)
                    with self.subTest(model=model, operation=operation), patch.object(runtime, "_get_client", side_effect=AssertionError("Client must not initialize")):
                        with self.assertRaises(ValueError):
                            operation(copy.deepcopy(session))
                    self.assertEqual(file_snapshot(root), before)

    def test_historical_status_and_discussion_inspection_preserve_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = seed_session(root, "gpt-5.5")
            before = file_snapshot(root)
            status = runtime.get_audit_status(copy.deepcopy(session), include_manifest=True)
            runtime.list_qa_threads(copy.deepcopy(session))
            self.assertEqual(status["session"]["model"], "gpt-5.5")
            self.assertEqual(status["session"]["audit_system_prompt_metadata"], session["audit_system_prompt_metadata"])
            self.assertEqual(file_snapshot(root), before)

    def test_continuous_fresh_and_rerun_requests_keep_settings(self):
        for context_mode, rerun in [(runtime.DEFAULT_AUDIT_CONTEXT_MODE, False),
                                    (runtime.AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL, False),
                                    (runtime.DEFAULT_AUDIT_CONTEXT_MODE, True)]:
            with tempfile.TemporaryDirectory() as tmp:
                session = seed_session(Path(tmp), "gpt-6-astra", "max")
                session["audit_context_mode"] = context_mode
                captured = []

                def create(**kwargs):
                    captured.append(kwargs)
                    raise StopBeforeAPIResult("offline capture complete")

                client = SimpleNamespace(responses=SimpleNamespace(create=create), conversations=SimpleNamespace(create=lambda: SimpleNamespace(id="fresh-conversation")))
                chunk = {"chunk_id": "chunk_001", "chunk_index": 1, "_fresh_rerun_conversation": rerun}
                builder = lambda *args: [{"role": "user", "content": [{"type": "input_text", "text": "fixture"}]}]
                with patch.object(runtime, "_get_client", return_value=client), patch.object(runtime, "_require_prompt_builder", return_value=builder):
                    with self.assertRaises(StopBeforeAPIResult):
                        runtime.process_one_chunk(session, chunk, display_output=False)
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0]["model"], "gpt-6-astra")
                self.assertEqual(captured[0]["reasoning"]["effort"], "max")
                self.assertEqual(session["model"], "gpt-6-astra")
                self.assertEqual(session["reasoning_effort"], "max")
                self.assertEqual(session["audit_system_prompt"], "Saved original instructions")
                self.assertEqual(captured[0]["conversation"], "fresh-conversation" if rerun or context_mode == runtime.AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL else "original-conversation")

    def test_new_session_omitted_effort_uses_model_default(self):
        for model, expected in [("gpt-6.1-sol", "xhigh"), ("gpt-6.1-terra", "high"), ("gpt-6-luna", "medium")]:
            with tempfile.TemporaryDirectory() as tmp:
                pdf = Path(tmp) / "paper.pdf"
                pdf.write_bytes(b"offline test")
                client = SimpleNamespace(conversations=SimpleNamespace(create=lambda: SimpleNamespace(id="mock-conversation")), files=SimpleNamespace(create=lambda **k: SimpleNamespace(id="mock-file")))
                with patch.object(runtime, "_get_client", return_value=client), patch.object(runtime, "build_auto_chunks", return_value={"chunks": [], "pdf_page_count": 0}):
                    session = runtime.create_new_session(pdf, model=model)
                self.assertEqual(session["reasoning_effort"], expected)
                self.assertEqual(session["reasoning_mode"], "standard")
                self.assertTrue(session["audit_system_prompt_metadata"]["sha256"])
                self.assertTrue(session["prompt_suite_metadata"])


def run_checks():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RequestMigrationChecks)
    # An accidental switch from the mock transport must fail, never reach a host.
    with patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden in offline regression tests")):
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise AssertionError("Request migration checks failed")


if __name__ == "__main__":
    run_checks()
