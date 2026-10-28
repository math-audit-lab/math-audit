#!/usr/bin/env python3
"""Offline GUI model/provenance checks. No network and no audit writes."""
from __future__ import annotations

import copy
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def run_checks() -> None:
    # Refuse any accidental network action, including during module imports.
    with patch.object(socket.socket, "connect", side_effect=AssertionError("Offline checks forbid networking")):
        from PySide6.QtWidgets import QApplication, QComboBox
        import gui_controller as gui
        from gui_main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        with patch.object(gui, "configure_runtime"):
            controller = gui.GuiController()
        controller._poll_timer.stop()
        messages: list[str] = []
        controller.log_message.connect(messages.append)
        assert (controller.model, controller.reasoning_effort) == ("gpt-6.1-sol", "xhigh")
        controller.set_model("gpt-6.1-terra")
        assert controller.reasoning_effort == "xhigh", "supported effort must survive a model switch"
        controller.set_reasoning_effort("none")
        controller.set_model("gpt-6-astra")
        assert controller.reasoning_effort == "xhigh"
        assert any("none is not supported" in message for message in messages)
        controller.set_model("unavailable-model")
        assert controller.model == "gpt-6-astra", "unknown model selection must not substitute silently"
        for model in ("gpt-5.5", "gpt-5.6-sol"):
            saved = {
                "model": model, "reasoning_effort": "historical-effort", "reasoning_mode": "pro",
                "audit_context_mode": "fresh_context_experimental", "response_ids": ["old-response"],
                "audit_system_prompt_meta": {"sha256": "original"}, "cost_usd": 123.45,
            }
            original = copy.deepcopy(saved)
            controller.pdf_path = "unused-test.pdf"
            with patch.object(gui, "load_session_from_pdf", return_value=saved):
                controller._load_saved_audit_settings()
                assert controller.model == model
                assert controller.reasoning_effort == "historical-effort"
                assert controller.reasoning_mode == "pro"
                assert controller.audit_context_mode == "fresh_context_experimental"
                assert saved == original, "loading must preserve every historical field"
                with patch.object(controller, "_require_pdf_file", return_value=True), patch.object(controller, "_run_backend_task") as run:
                    controller.start_fresh_audit()
                    run.assert_not_called()
                assert not controller._require_api_key_for_live_calls()
                controller.set_model("gpt-6.1-sol")
                assert not controller._require_api_key_for_live_calls(), "a new choice must not unlock legacy continuation"
                with patch.object(controller, "live_api_key_available", return_value=True):
                    assert controller._require_api_key_for_live_calls(allow_historical_session=True)
                # Exercise real Qt controls with the window methods without opening a window.
                holder = SimpleNamespace(controller=controller, model_combo=QComboBox(), reasoning_combo=QComboBox())
                holder.model_combo.setEditable(True)
                holder.model_combo.lineEdit().setReadOnly(True)
                holder.model_combo.addItems(gui.MODEL_DISPLAY_CHOICES)
                holder._refresh_reasoning_options = lambda *args, **kwargs: MainWindow._refresh_reasoning_options(holder, *args, **kwargs)
                MainWindow._set_model_effort_controls(holder, model, "historical-effort")
                assert holder.model_combo.currentText() == model
                assert holder.reasoning_combo.currentText() == "historical-effort"
                assert not holder.reasoning_combo.isEnabled()
                choices = [holder.model_combo.itemText(i) for i in range(holder.model_combo.count())]
                assert choices == gui.MODEL_DISPLAY_CHOICES
                assert all("GPT-5" not in choice for choice in choices)
                holder.model_combo.deleteLater()
                holder.reasoning_combo.deleteLater()
        with patch.object(gui, "load_session_from_pdf", return_value={"model": "gpt-6.1-sol", "reasoning_mode": "pro"}):
            assert "saved mode is preserved" in controller.saved_session_live_warning()
        with patch.object(gui, "load_session_from_pdf", return_value={"model": "gpt-6.1-sol", "reasoning_effort": "xhigh"}):
            assert controller.saved_session_live_warning() == ""
        assert "gpt-6.1-sol" in gui.format_chunk_completion_log_line({}, {"model": "gpt-6.1-sol"})
        assert "~$" in gui.format_chunk_completion_log_line({}, {"cost": {"total_cost": 0.1, "approximate": True}})
        controller.model = "gpt-5.5"
        with patch.object(gui, "load_session_from_pdf", return_value=None):
            controller._load_saved_audit_settings()
            assert (controller.model, controller.reasoning_effort) == ("gpt-6.1-sol", "xhigh")
        controller.deleteLater()
        del app


if __name__ == "__main__":
    run_checks()
    print("PASS GUI model selection, historical provenance, and legacy live-action guards")
