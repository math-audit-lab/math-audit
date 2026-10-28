#!/usr/bin/env python3
"""Offline regression checks for model validation and frozen cost provenance."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_models import (
    DEFAULT_MODEL, DEFAULT_REASONING_EFFORT, MODEL_CHOICES,
    canonical_model_id, default_reasoning_effort_for_model, is_current_model,
    model_display_choices, normalize_model_and_reasoning_effort, request_reasoning,
    supported_reasoning_efforts_for_model, validate_new_audit_settings,
)
from audit_state import (
    _ensure_timing_state, compute_usage_cost, init_state_files, load_session_from_pdf, load_usage,
    save_json, session_paths, update_usage_from_usage_obj, usage_cache_diagnostics,
)


def usage(input_tokens=12_000, cached=4_000, writes30=3_000, writes12=4_000, output=100):
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {
            "cached_tokens": cached,
            "cache_write_tokens": writes30 + writes12,
            "cache_write_tokens_details": {"ttl_30m_tokens": writes30, "ttl_12h_tokens": writes12},
        },
        "output_tokens": output,
        "output_tokens_details": {"reasoning_tokens": 80},
        "total_tokens": input_tokens + output,
    }


class ModelMigrationChecks(unittest.TestCase):
    def test_current_registry_and_defaults(self):
        self.assertEqual(DEFAULT_MODEL, "gpt-6.1-sol")
        self.assertEqual(DEFAULT_REASONING_EFFORT, "xhigh")
        self.assertEqual(set(MODEL_CHOICES), {"gpt-6.1-sol", "gpt-6-astra", "gpt-6.1-terra", "gpt-6-luna"})
        self.assertEqual(len(model_display_choices()), 4)
        for model in MODEL_CHOICES:
            expected = ["low", "medium", "high", "xhigh", "max"]
            if model in {"gpt-6.1-terra", "gpt-6-luna"}:
                expected.insert(0, "none")
            self.assertEqual(supported_reasoning_efforts_for_model(model), expected)
            for effort in expected:
                self.assertEqual(request_reasoning(model, effort), {"effort": effort, "mode": "standard"})
        self.assertEqual(default_reasoning_effort_for_model("gpt-6.1-terra"), "high")
        self.assertEqual(default_reasoning_effort_for_model("gpt-6-luna"), "medium")

    def test_request_validation_never_silently_normalizes(self):
        for model, effort, mode in [
            ("gpt-6-astra", "none", "standard"), ("gpt-6.1-sol", "ultra", "standard"),
            ("gpt-6.1-sol", "high", "unrecognized"), ("gpt-5.5", "xhigh", "standard"),
            ("gpt-5.6-sol", "max", "standard"), ("gpt-6-sol", "high", "standard"),
            ("gpt-6-astra-pro", "high", "standard"), ("unknown-model", "high", "standard"),
            (None, "xhigh", "standard"),
        ]:
            with self.subTest(model=model, effort=effort, mode=mode), self.assertRaises(ValueError):
                validate_new_audit_settings(model, effort, mode)
        self.assertFalse(is_current_model(None))
        self.assertEqual(request_reasoning("gpt-6-astra", "xhigh", "pro"), {"effort": "xhigh", "mode": "pro"})

    def test_gui_switch_preserves_supported_effort(self):
        self.assertEqual(normalize_model_and_reasoning_effort("gpt-6-luna", "xhigh"), ("gpt-6-luna", "xhigh"))
        self.assertEqual(normalize_model_and_reasoning_effort("gpt-6.1-sol", "none"), ("gpt-6.1-sol", "xhigh"))
        self.assertEqual(canonical_model_id("gpt-5.5-2026-04-23"), "gpt-5.5-2026-04-23")

    def test_disjoint_cache_billing_and_reasoning_not_double_counted(self):
        result = compute_usage_cost("gpt-6.1-sol", usage())
        expected = (1000 * 2 + 4000 * .1 + 3000 * 2.5 + 4000 * 5 + 100 * 10) / 1_000_000
        self.assertAlmostEqual(result["total_cost"], expected)
        self.assertEqual(result["ordinary_input_tokens"], 1000)
        self.assertEqual(result["cache_write_12h_tokens"], 4000)
        self.assertFalse(result["approximate"])
        self.assertEqual(usage_cache_diagnostics(usage())["uncached_input_tokens"], 1000)
        self.assertAlmostEqual(compute_usage_cost("gpt-6.1-sol", usage(), service_tier="fast")["total_cost"], expected * 2)

    def test_long_context_boundary_and_all_rates(self):
        for model in MODEL_CHOICES:
            short = compute_usage_cost(model, usage(input_tokens=272_000))
            long = compute_usage_cost(model, usage(input_tokens=272_001))
            self.assertEqual(short["pricing_context"], "short")
            self.assertEqual(long["pricing_context"], "long")
            for key, rate in short["pricing_rates_usd_per_1m"].items():
                self.assertEqual(long["pricing_rates_usd_per_1m"][key], rate * (1.5 if key == "output" else 2))
        # Old calculator remains a historical reference, not a current-price source.
        self.assertEqual(compute_usage_cost("gpt-5.5", usage(input_tokens=270_001))["pricing_context"], "long")

    def test_missing_or_inconsistent_cache_counts_are_approximate(self):
        missing = usage()
        missing["input_tokens_details"].pop("cache_write_tokens")
        self.assertTrue(compute_usage_cost("gpt-6.1-sol", missing)["approximate"])
        missing = usage()
        missing["input_tokens_details"].pop("cache_write_tokens_details")
        result = compute_usage_cost("gpt-6.1-sol", missing, cache_ttl="12h")
        self.assertTrue(result["approximate"])
        self.assertEqual(result["cache_write_12h_tokens"], 7000)
        invalid = usage(cached=20_000)
        result = compute_usage_cost("gpt-6.1-sol", invalid)
        self.assertTrue(result["approximate"])
        self.assertGreaterEqual(result["total_cost"], 0)
        self.assertFalse(compute_usage_cost("unknown-model", usage())["pricing_available"])

    def test_historical_read_and_append_preserve_saved_provenance(self):
        for legacy_model in ("gpt-5.5", "gpt-5.6-sol"):
            with self.subTest(model=legacy_model), tempfile.TemporaryDirectory() as tmp:
                pdf = Path(tmp) / "paper.pdf"
                workdir = Path(tmp) / "paper_audit"
                init_state_files(workdir, legacy_model, "xhigh")
                session = {
                    "workdir": str(workdir), "model": legacy_model, "reasoning_effort": "xhigh",
                    "reasoning_mode": "pro", "response_id": "historical-response",
                    "audit_context_mode": "continuous", "audit_system_prompt_metadata": {"sha256": "old-hash", "version": "old"},
                }
                paths = session_paths(workdir)
                save_json(paths["session"], session)
                old_row = {"response_id": "old-id", "usage": {"input_tokens": 4}, "cost": {"total_cost": 9.87654321}}
                saved_usage = load_usage(session)
                saved_usage["totals"]["cost_usd"] = 9.87654321
                saved_usage["per_chunk"] = [copy.deepcopy(old_row)]
                save_json(paths["usage"], saved_usage)
                bytes_before = paths["session"].read_bytes()
                usage_before = paths["usage"].read_bytes()
                self.assertEqual(load_session_from_pdf(pdf), session)
                self.assertEqual(paths["session"].read_bytes(), bytes_before)
                self.assertEqual(paths["usage"].read_bytes(), usage_before)
                status_before = paths["status"].read_bytes()
                _ensure_timing_state(copy.deepcopy(session), persist=False)
                self.assertEqual(paths["session"].read_bytes(), bytes_before)
                self.assertEqual(paths["usage"].read_bytes(), usage_before)
                self.assertEqual(paths["status"].read_bytes(), status_before)
                # Deliberate append with a different current model keeps every older row/cost.
                result = update_usage_from_usage_obj(session, "later", usage(), model="gpt-6.1-sol", reasoning_effort="high", reasoning_mode="standard")
                self.assertEqual(result["usage_state"]["per_chunk"][0], old_row)
                self.assertAlmostEqual(result["usage_state"]["totals"]["cost_usd"], 9.87654321 + result["cost"]["total_cost"])
                self.assertEqual(result["usage_state"]["per_chunk"][-1]["reasoning_effort"], "high")
                self.assertEqual(paths["session"].read_bytes(), bytes_before)


def run_checks():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ModelMigrationChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise AssertionError("Model migration checks failed")


if __name__ == "__main__":
    run_checks()
