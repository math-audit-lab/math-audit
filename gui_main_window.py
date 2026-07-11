from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QApplication,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gui_controller import AUDIT_CONTEXT_MODE_CHOICES, AUDIT_CONTEXT_MODE_LABELS, DEFAULT_MODEL, MODEL_DISPLAY_CHOICES, GuiController


def review_tab_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get("MATH_AUDIT_ENABLE_REVIEW_TAB") or "") == "1"


def _set_text_if_changed(widget: Any, text: str) -> bool:
    clean = str(text or "")
    if getattr(widget, "text")() == clean:
        return False
    widget.setText(clean)
    return True


def _set_tooltip_if_changed(widget: Any, text: str) -> bool:
    clean = str(text or "")
    if widget.toolTip() == clean:
        return False
    widget.setToolTip(clean)
    return True


def _set_stylesheet_if_changed(widget: Any, style: str) -> bool:
    clean = str(style or "")
    if widget.styleSheet() == clean:
        return False
    widget.setStyleSheet(clean)
    return True


def _set_plain_text_preserving_scroll(widget: QPlainTextEdit, text: str) -> bool:
    clean = str(text or "")
    if widget.toPlainText() == clean:
        return False
    scrollbar = widget.verticalScrollBar()
    previous_value = scrollbar.value()
    previous_maximum = scrollbar.maximum()
    was_at_bottom = previous_maximum > 0 and previous_value >= previous_maximum - 2
    widget.setPlainText(clean)
    if was_at_bottom:
        scrollbar.setValue(scrollbar.maximum())
    else:
        scrollbar.setValue(min(previous_value, scrollbar.maximum()))
    return True


def _counterexample_recheck_view(
    inventory: dict[str, Any],
    history_summary: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    candidates = [
        item
        for item in (inventory.get("candidates") or [])
        if isinstance(item, dict) and item.get("eligible")
    ]
    if not candidates:
        return {
            "summary_text": "No active counterexample findings require recheck.",
            "entries": [],
            "eligible_chunk_count": 0,
            "eligible_finding_count": 0,
        }

    finding_count = sum(int(item.get("finding_count", 0) or 0) for item in candidates)
    chunk_word = "chunk" if len(candidates) == 1 else "chunks"
    finding_word = "finding" if finding_count == 1 else "findings"
    lines = [f"{len(candidates)} eligible {chunk_word}, {finding_count} {finding_word}"]
    entries = [{"label": f"All eligible chunks ({len(candidates)})", "selection": "all"}]

    for candidate in candidates:
        chunk_id = str(candidate.get("chunk_id") or "unknown chunk").strip()
        finding_ids = [str(item).strip() for item in candidate.get("finding_ids") or [] if str(item).strip()]
        targets = []
        for target in candidate.get("targets") or []:
            if isinstance(target, dict):
                label = str(target.get("label") or target.get("name") or "").strip()
            else:
                label = str(target or "").strip()
            if label and label not in targets:
                targets.append(label)
        target_text = ", ".join(targets) or "target not reported"
        counterexample_count = int(candidate.get("counterexample_count", 0) or 0)
        finding_label = "Finding" if len(finding_ids) == 1 else "Findings"
        case_label = "case" if counterexample_count == 1 else "cases"
        lines.extend(
            [
                "",
                f"{chunk_id} - {target_text}",
                f"  {finding_label}: {', '.join(finding_ids) or 'not reported'}",
                f"  Counterexamples: {counterexample_count} {case_label}",
            ]
        )
        combo_finding_text = ", ".join(finding_ids) or "finding not reported"
        entries.append(
            {
                "label": f"{chunk_id} - {target_text} - {combo_finding_text}",
                "selection": chunk_id,
            }
        )

    history = history_summary if isinstance(history_summary, dict) else {}
    if int(history.get("rechecked_finding_count", 0) or 0):
        lines.extend(
            [
                "",
                (
                    "Previous rechecks: "
                    f"confirmed {int(history.get('confirmed_count', 0) or 0)}, "
                    f"challenged {int(history.get('challenged_count', 0) or 0)}, "
                    f"inconclusive {int(history.get('inconclusive_count', 0) or 0)}"
                ),
            ]
        )
    return {
        "summary_text": "\n".join(lines),
        "entries": entries,
        "eligible_chunk_count": len(candidates),
        "eligible_finding_count": finding_count,
    }


def _counterexample_recheck_selection_index(entries: list[dict[str, Any]], previous_selection: Any) -> int:
    clean_previous = str(previous_selection or "").strip()
    for index, entry in enumerate(entries):
        if str(entry.get("selection") or "").strip() == clean_previous:
            return index
    return 0 if entries else -1


def _counterexample_recheck_action_state(
    *,
    session_available: bool,
    status_name: str,
    pending_response: bool,
    task_running: bool,
    api_key_available: bool,
    entries: list[dict[str, Any]],
    selected_token: Any,
) -> dict[str, Any]:
    candidate_count = max(0, len(entries) - 1) if entries else 0
    clean_selection = str(selected_token or "").strip()
    valid_selections = {str(item.get("selection") or "").strip() for item in entries}
    if clean_selection == "all" and candidate_count:
        chunk_word = "Chunk" if candidate_count == 1 else "Chunks"
        button_text = f"Recheck {candidate_count} Counterexample {chunk_word}"
    elif clean_selection:
        button_text = "Recheck Selected Counterexample"
    else:
        button_text = "Recheck Counterexample Findings"

    reason = "Ready to run a focused counterexample recheck."
    enabled = True
    if not session_available:
        enabled, reason = False, "Audit state unavailable. Select a completed or paused audit."
    elif task_running:
        enabled, reason = False, "Another audit action is currently running."
    elif status_name not in {"completed", "paused"}:
        enabled, reason = False, "The audit must be completed or paused before rechecking findings."
    elif pending_response:
        enabled, reason = False, "A chunk request is currently pending."
    elif candidate_count <= 0:
        enabled, reason = False, "No eligible counterexample findings."
    elif clean_selection not in valid_selections:
        enabled, reason = False, "The selected candidate is no longer active."
    elif not api_key_available:
        enabled, reason = False, "API key required to run the recheck."
    return {"enabled": enabled, "reason": reason, "button_text": button_text}


def _verification_replacement_view(inventory: dict[str, Any]) -> dict[str, Any]:
    groups = [item for item in inventory.get("groups") or [] if isinstance(item, dict)]
    if not groups:
        return {
            "summary_text": "No replacement verification scripts have been proposed.",
            "entries": [],
        }
    lines = []
    entries = []
    for group in groups:
        recheck_id = str(group.get("recheck_id") or "")
        chunk_id = str(group.get("chunk_id") or "unknown chunk")
        finding_ids = ", ".join(str(item) for item in group.get("finding_ids") or []) or "unknown finding"
        lines.extend(
            [
                f"{chunk_id} - {finding_ids}",
                "  Original script error: " + str(group.get("script_error_explanation") or "not specified"),
            ]
        )
        ready_ids = []
        ready_paths = []
        for check in group.get("checks") or []:
            if not isinstance(check, dict):
                continue
            check_id = str(check.get("check_id") or "")
            validation = check.get("validation") or {}
            validation_status = str(validation.get("status") or "unknown")
            latest = check.get("latest_execution") if isinstance(check.get("latest_execution"), dict) else {}
            execution_text = "not run"
            if latest:
                execution_text = (
                    f"{latest.get('execution_status') or 'unknown'} / "
                    f"{latest.get('mathematical_outcome') or 'outcome not reported'}"
                )
            lines.extend(
                [
                    f"  {check_id} ({check.get('relationship_to_original') or 'replacement'})",
                    f"    Validation: {validation_status}; execution: {execution_text}",
                    f"    Purpose: {check.get('purpose') or 'not specified'}",
                ]
            )
            entry_data = {
                "recheck_id": recheck_id,
                "check_ids": [check_id],
                "script_paths": [str(check.get("script_path") or "")],
            }
            entries.append(
                {
                    "label": f"{chunk_id} - {check_id} - {validation_status}",
                    "data": entry_data,
                    "ready": validation_status == "ready",
                }
            )
            if validation_status == "ready":
                ready_ids.append(check_id)
                ready_paths.append(str(check.get("script_path") or ""))
        if ready_ids:
            entries.insert(
                len(entries) - len(group.get("checks") or []),
                {
                    "label": f"{chunk_id} - All ready replacement checks ({len(ready_ids)})",
                    "data": {
                        "recheck_id": recheck_id,
                        "check_ids": ready_ids,
                        "script_paths": ready_paths,
                    },
                    "ready": True,
                },
            )
        reconciliation = group.get("reconciliation") or {}
        lines.append(
            "  Status: " + str(reconciliation.get("status") or "unresolved").replace("_", " ")
        )
        lines.append("")
    return {"summary_text": "\n".join(lines).rstrip(), "entries": entries}


def _verification_technical_repair_view(
    candidates: dict[str, Any], repairs: dict[str, Any]
) -> dict[str, Any]:
    eligible = [
        item for item in candidates.get("candidates") or []
        if isinstance(item, dict) and item.get("eligible")
    ]
    candidate_entries = [{"label": f"All eligible scripts ({len(eligible)})", "selection": "all"}] if eligible else []
    lines = [
        f"{len(eligible)} script(s) require technical repair.",
        "Repair script first - this preserves the original chunk audit.",
    ] if eligible else ["No active technical verification failures require repair."]
    by_chunk: dict[str, int] = {}
    for item in eligible:
        chunk_id = str(item.get("chunk_id") or "")
        if chunk_id:
            by_chunk[chunk_id] = by_chunk.get(chunk_id, 0) + 1
    for chunk_id, count in sorted(by_chunk.items()):
        if count > 1:
            candidate_entries.append(
                {"label": f"{chunk_id} - all failed scripts ({count})", "selection": chunk_id}
            )
    for item in eligible:
        script_id = str(item.get("script_id") or "unknown script")
        status = str(item.get("execution_status") or "unknown")
        reason = str(item.get("failure_reason") or "not recorded")
        lines.append(f"{script_id} - {status}: {reason}")
        candidate_entries.append({"label": f"{script_id} - {status}", "selection": script_id})
    replacement_entries = []
    for group in repairs.get("groups") or []:
        if not isinstance(group, dict):
            continue
        repair_id = str(group.get("repair_id") or "")
        script_id = str(group.get("script_id") or "unknown script")
        for check in group.get("checks") or []:
            if not isinstance(check, dict):
                continue
            validation = str((check.get("validation") or {}).get("status") or "unknown")
            check_id = str(check.get("check_id") or "")
            replacement_entries.append(
                {
                    "label": f"{script_id} - {check_id} - {validation}",
                    "data": {
                        "repair_id": repair_id,
                        "check_ids": [check_id],
                        "script_paths": [str(check.get("script_path") or "")],
                        "ready": validation == "ready",
                    },
                }
            )
    return {
        "summary_text": "\n".join(lines),
        "candidate_entries": candidate_entries,
        "replacement_entries": replacement_entries,
    }


class AuditPromptDialog(QDialog):
    def __init__(self, controller: GuiController, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Audit Prompt Profiles")
        self.setMinimumSize(760, 560)

        layout = QVBoxLayout(self)
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Target"))
        self.target_combo = QComboBox()
        self.target_combo.addItems(self.controller.prompt_profile_targets())
        self.target_combo.currentTextChanged.connect(self._load_target)
        target_row.addWidget(self.target_combo, 1)
        layout.addLayout(target_row)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        self.prompt_editor = QTextEdit()
        self.prompt_editor.setAcceptRichText(False)
        layout.addWidget(self.prompt_editor, 1)

        button_row = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self._save_current)
        self.reset_button = QPushButton("Reset to shipped default")
        self.reset_button.clicked.connect(self._reset_current)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)
        button_row.addWidget(self.save_button)
        button_row.addWidget(self.reset_button)
        button_row.addStretch(1)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)

        self._load_target(self.target_combo.currentText())

    def _load_target(self, target: str) -> None:
        self.prompt_editor.setPlainText(self.controller.prompt_text_for_target(target))
        self.status_label.setText(self.controller.prompt_status_for_target(target))

    def _save_current(self) -> None:
        target = self.target_combo.currentText()
        self.controller.save_prompt_for_target(target, self.prompt_editor.toPlainText())
        self._load_target(target)

    def _reset_current(self) -> None:
        target = self.target_combo.currentText()
        self.controller.reset_prompt_for_target(target)
        self._load_target(target)


class ChunkSelectionDialog(QDialog):
    def __init__(self, chunks: list[dict[str, Any]], selected_ids: set[str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Chunks")
        self.setMinimumSize(640, 520)

        layout = QVBoxLayout(self)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Search by chunk id, page, or label")
        self.filter_input.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_input)

        self.chunk_list = QListWidget()
        for chunk in chunks:
            chunk_id = str(chunk.get("chunk_id") or "").strip()
            if not chunk_id:
                continue
            item = QListWidgetItem(self._chunk_row_text(chunk))
            item.setData(Qt.ItemDataRole.UserRole, chunk_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if chunk_id in selected_ids else Qt.CheckState.Unchecked)
            self.chunk_list.addItem(item)
        layout.addWidget(self.chunk_list, 1)

        buttons = QDialogButtonBox()
        self.apply_button = buttons.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
        self.clear_button = buttons.addButton("Clear selection", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.clear_button.clicked.connect(self._clear_selection)
        layout.addWidget(buttons)

    def selected_chunk_ids(self) -> list[str]:
        selected: list[str] = []
        for row in range(self.chunk_list.count()):
            item = self.chunk_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                selected.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return selected

    def _apply_filter(self, text: str) -> None:
        terms = [term for term in str(text or "").lower().split() if term]
        for row in range(self.chunk_list.count()):
            item = self.chunk_list.item(row)
            haystack = item.text().lower()
            item.setHidden(bool(terms) and not all(term in haystack for term in terms))

    def _clear_selection(self) -> None:
        for row in range(self.chunk_list.count()):
            self.chunk_list.item(row).setCheckState(Qt.CheckState.Unchecked)

    @staticmethod
    def _chunk_row_text(chunk: dict[str, Any]) -> str:
        chunk_id = str(chunk.get("chunk_id") or "").strip()
        page_start = chunk.get("page_start")
        page_end = chunk.get("page_end")
        if page_start and page_end and page_start != page_end:
            page_text = f"pages {page_start}-{page_end}"
        elif page_start:
            page_text = f"page {page_start}"
        else:
            page_text = "pages unknown"
        display_label = str(chunk.get("display_label") or "").strip()
        if display_label:
            if len(display_label) > 100:
                display_label = display_label[:97].rstrip() + "..."
            return " | ".join(part for part in [chunk_id, display_label] if part)
        label = str(chunk.get("label") or "").strip()
        if len(label) > 80:
            label = label[:77].rstrip() + "..."
        return " | ".join(part for part in [chunk_id, page_text, label] if part)


class ConciseReportOptionsDialog(QDialog):
    _PRESETS: dict[str, dict[str, Any]] = {
        "Strict concise": {
            "preset": "strict_concise",
            "include_critical": True,
            "include_high": True,
            "include_medium": False,
            "include_low": False,
            "include_typographical_issues": True,
            "include_audit_summary": True,
            "include_verification_summary": True,
            "include_omitted_material_note": True,
            "only_open_issues": True,
        },
        "Balanced concise": {
            "preset": "balanced_concise",
            "include_critical": True,
            "include_high": True,
            "include_medium": True,
            "include_low": False,
            "include_typographical_issues": True,
            "include_audit_summary": True,
            "include_verification_summary": True,
            "include_omitted_material_note": True,
            "only_open_issues": True,
        },
        "Minimal referee version": {
            "preset": "minimal_referee",
            "include_critical": True,
            "include_high": True,
            "include_medium": False,
            "include_low": False,
            "include_typographical_issues": False,
            "include_audit_summary": True,
            "include_verification_summary": False,
            "include_omitted_material_note": True,
            "only_open_issues": True,
        },
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Concise Report Options")
        self.setMinimumWidth(420)
        self._updating = False
        self._selected_options: Optional[dict[str, Any]] = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(["Strict concise", "Balanced concise", "Minimal referee version", "Custom"])
        self.preset_combo.currentTextChanged.connect(self._on_preset_changed)
        form.addRow("Preset", self.preset_combo)

        severity_box = QGroupBox("Severity inclusion")
        severity_layout = QVBoxLayout(severity_box)
        self.include_critical_checkbox = QCheckBox("critical")
        self.include_high_checkbox = QCheckBox("high")
        self.include_medium_checkbox = QCheckBox("medium")
        self.include_low_checkbox = QCheckBox("low")
        for checkbox in [
            self.include_critical_checkbox,
            self.include_high_checkbox,
            self.include_medium_checkbox,
            self.include_low_checkbox,
        ]:
            checkbox.stateChanged.connect(self._mark_custom)
            severity_layout.addWidget(checkbox)

        toggles_box = QGroupBox("Sections and filters")
        toggles_layout = QVBoxLayout(toggles_box)
        self.include_typographical_checkbox = QCheckBox("Include typographical/copyediting issues")
        self.include_audit_summary_checkbox = QCheckBox("Include audit summary")
        self.include_verification_summary_checkbox = QCheckBox("Include verification summary")
        self.include_omitted_note_checkbox = QCheckBox("Include omitted-material note")
        self.only_open_issues_checkbox = QCheckBox("Only open issues")
        for checkbox in [
            self.include_typographical_checkbox,
            self.include_audit_summary_checkbox,
            self.include_verification_summary_checkbox,
            self.include_omitted_note_checkbox,
            self.only_open_issues_checkbox,
        ]:
            checkbox.stateChanged.connect(self._mark_custom)
            toggles_layout.addWidget(checkbox)

        layout.addLayout(form)
        layout.addWidget(severity_box)
        layout.addWidget(toggles_box)

        button_row = QHBoxLayout()
        self.reset_button = QPushButton("Reset to default")
        self.reset_button.clicked.connect(self._reset_to_default)
        self.build_button = QPushButton("Build")
        self.build_button.clicked.connect(self._accept_options)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        button_row.addWidget(self.reset_button)
        button_row.addStretch(1)
        button_row.addWidget(self.build_button)
        button_row.addWidget(self.cancel_button)
        layout.addLayout(button_row)

        self._set_options(self._PRESETS["Strict concise"])

    def options(self) -> dict[str, Any]:
        return dict(self._selected_options or self._current_options())

    def _on_preset_changed(self, label: str) -> None:
        if self._updating or label == "Custom":
            return
        self._set_options(self._PRESETS.get(label, self._PRESETS["Strict concise"]))

    def _reset_to_default(self) -> None:
        self.preset_combo.setCurrentText("Strict concise")
        self._set_options(self._PRESETS["Strict concise"])

    def _set_options(self, options: dict[str, Any]) -> None:
        self._updating = True
        try:
            self.include_critical_checkbox.setChecked(bool(options.get("include_critical", True)))
            self.include_high_checkbox.setChecked(bool(options.get("include_high", True)))
            self.include_medium_checkbox.setChecked(bool(options.get("include_medium", False)))
            self.include_low_checkbox.setChecked(bool(options.get("include_low", False)))
            self.include_typographical_checkbox.setChecked(bool(options.get("include_typographical_issues", True)))
            self.include_audit_summary_checkbox.setChecked(bool(options.get("include_audit_summary", True)))
            self.include_verification_summary_checkbox.setChecked(bool(options.get("include_verification_summary", True)))
            self.include_omitted_note_checkbox.setChecked(bool(options.get("include_omitted_material_note", True)))
            self.only_open_issues_checkbox.setChecked(bool(options.get("only_open_issues", True)))
        finally:
            self._updating = False

    def _mark_custom(self, _state: Optional[int] = None) -> None:
        if self._updating:
            return
        if self.preset_combo.currentText() != "Custom":
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentText("Custom")
            self.preset_combo.blockSignals(False)

    def _current_options(self) -> dict[str, Any]:
        preset_labels = {
            "Strict concise": "strict_concise",
            "Balanced concise": "balanced_concise",
            "Minimal referee version": "minimal_referee",
            "Custom": "custom",
        }
        return {
            "preset": preset_labels.get(self.preset_combo.currentText(), "custom"),
            "include_critical": self.include_critical_checkbox.isChecked(),
            "include_high": self.include_high_checkbox.isChecked(),
            "include_medium": self.include_medium_checkbox.isChecked(),
            "include_low": self.include_low_checkbox.isChecked(),
            "include_typographical_issues": self.include_typographical_checkbox.isChecked(),
            "include_audit_summary": self.include_audit_summary_checkbox.isChecked(),
            "include_verification_summary": self.include_verification_summary_checkbox.isChecked(),
            "include_omitted_material_note": self.include_omitted_note_checkbox.isChecked(),
            "only_open_issues": self.only_open_issues_checkbox.isChecked(),
        }

    def _accept_options(self) -> None:
        self._selected_options = self._current_options()
        self.accept()


class ChatGPTContextPackDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export ChatGPT Context Pack")
        self.setMinimumWidth(420)
        self._selected_options: Optional[dict[str, Any]] = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(["ChatGPT handoff", "Full archive"])
        self.preset_combo.currentTextChanged.connect(self._apply_preset)
        form.addRow("Preset", self.preset_combo)

        self.context_depth_combo = QComboBox()
        self.context_depth_combo.addItems(["Full audit context", "Reduced audit context"])
        form.addRow("Context depth", self.context_depth_combo)

        options_box = QGroupBox("Files to include")
        options_layout = QVBoxLayout(options_box)
        self.include_pdf_checkbox = QCheckBox("Include paper PDF")
        self.include_pdf_checkbox.setChecked(True)
        self.include_tex_checkbox = QCheckBox("Include TeX source if available")
        self.include_tex_checkbox.setChecked(True)
        self.include_concise_report_checkbox = QCheckBox("Include concise report .md if present")
        self.include_concise_report_checkbox.setChecked(True)
        self.include_full_report_checkbox = QCheckBox("Include full report")
        self.include_full_report_checkbox.setChecked(False)
        self.include_verification_report_checkbox = QCheckBox("Include verification report .md if present")
        self.include_verification_report_checkbox.setChecked(True)
        for checkbox in [
            self.include_pdf_checkbox,
            self.include_tex_checkbox,
            self.include_concise_report_checkbox,
            self.include_full_report_checkbox,
            self.include_verification_report_checkbox,
        ]:
            options_layout.addWidget(checkbox)

        button_row = QHBoxLayout()
        build_button = QPushButton("Export")
        build_button.clicked.connect(self._accept_options)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        button_row.addStretch(1)
        button_row.addWidget(build_button)
        button_row.addWidget(cancel_button)

        layout.addLayout(form)
        layout.addWidget(options_box)
        layout.addLayout(button_row)

    def options(self) -> dict[str, Any]:
        return dict(self._selected_options or self._current_options())

    def _current_options(self) -> dict[str, Any]:
        preset = "full_archive" if self.preset_combo.currentText() == "Full archive" else "chatgpt_handoff"
        depth = "reduced_audit_context" if self.context_depth_combo.currentText() == "Reduced audit context" else "full_audit_context"
        report_file_formats = ["md", "tex", "pdf", "json"] if preset == "full_archive" else ["md"]
        return {
            "preset": preset,
            "include_pdf": self.include_pdf_checkbox.isChecked(),
            "include_tex": self.include_tex_checkbox.isChecked(),
            "include_concise_report": self.include_concise_report_checkbox.isChecked(),
            "include_full_report": self.include_full_report_checkbox.isChecked(),
            "include_verification_report": self.include_verification_report_checkbox.isChecked(),
            "context_depth": depth,
            "report_file_formats": report_file_formats,
        }

    def _apply_preset(self, preset_label: str) -> None:
        full_archive = preset_label == "Full archive"
        self.include_pdf_checkbox.setChecked(True)
        self.include_tex_checkbox.setChecked(True)
        self.include_concise_report_checkbox.setChecked(True)
        self.include_full_report_checkbox.setChecked(full_archive)
        self.include_verification_report_checkbox.setChecked(True)

    def _accept_options(self) -> None:
        self._selected_options = self._current_options()
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, controller: GuiController, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._task_running = False
        self._close_after_task = False
        self._last_status_payload: dict[str, Any] = {}
        self._latest_chatgpt_context_pack: dict[str, Any] = {}
        self._discussion_transcript_parts: list[str] = []
        self._review_family_ids: list[str] = []
        self._counterexample_recheck_inventory_signature: Optional[tuple[Any, ...]] = None
        self._verification_replacement_inventory_signature: Optional[tuple[Any, ...]] = None
        self._verification_technical_repair_signature: Optional[tuple[Any, ...]] = None

        self.setWindowTitle("Math Audit Control Panel (V1)")
        self.resize(980, 820)

        central = QWidget(self)
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_top_pane())
        splitter.addWidget(self._build_tabs())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([175, 645])
        main_layout.addWidget(splitter)

        self.controller.status_updated.connect(self._on_status_updated)
        self.controller.log_message.connect(self._append_log)
        self.controller.report_output.connect(self._append_report_output)
        self.controller.report_paths_updated.connect(lambda _paths: self._refresh_report_open_buttons())
        self.controller.chatgpt_context_pack_exported.connect(self._on_chatgpt_context_pack_exported)
        self.controller.verification_progress.connect(self._on_verification_progress)
        self.controller.discussion_output.connect(self._set_discussion_output)
        self.controller.discussion_history_loaded.connect(self._load_discussion_history)
        self.controller.discussion_threads_loaded.connect(self._on_discussion_threads_loaded)
        self.controller.task_running_changed.connect(self._on_task_running_changed)
        self.controller.cancel_task_running_changed.connect(lambda _running: self._apply_button_states())
        self.controller.audit_settings_changed.connect(self._on_audit_settings_changed)
        self.controller.audit_context_mode_changed.connect(self._on_audit_context_mode_changed)
        self.controller.review_summary_updated.connect(self._update_review_summary)

        self.model_combo.setCurrentText(self.controller.model_display_name(DEFAULT_MODEL))
        self._refresh_reasoning_options(DEFAULT_MODEL)
        self._apply_button_states()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.controller.has_active_task():
            event.accept()
            return

        event.ignore()
        self._close_after_task = True
        self.controller.prepare_for_shutdown()
        self._apply_button_states()

    def _build_top_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(4)
        controls_row.addWidget(self._build_setup_group(), 3)
        controls_row.addWidget(self._build_controls_group(), 2)
        layout.addLayout(controls_row)
        layout.addWidget(self._build_status_group())

        return pane

    def _build_setup_group(self) -> QGroupBox:
        box = QGroupBox("Audit Setup")
        layout = QFormLayout(box)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setVerticalSpacing(2)

        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText("OpenAI API key (kept in memory only)")
        self.api_key_input.editingFinished.connect(self._apply_api_key)
        layout.addRow("API key", self.api_key_input)

        pdf_row = QHBoxLayout()
        self.pdf_path_input = QLineEdit()
        self.pdf_path_input.setPlaceholderText("/absolute/path/to/paper.pdf")
        self.pdf_path_input.editingFinished.connect(self._apply_pdf_path)
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self._browse_pdf)
        pdf_row.addWidget(self.pdf_path_input, 1)
        pdf_row.addWidget(self.browse_button)
        layout.addRow("PDF", pdf_row)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems(MODEL_DISPLAY_CHOICES)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        layout.addRow("Model", self.model_combo)

        self.reasoning_combo = QComboBox()
        self.reasoning_combo.currentTextChanged.connect(self.controller.set_reasoning_effort)
        layout.addRow("Reasoning effort", self.reasoning_combo)

        self.audit_context_mode_combo = QComboBox()
        self.audit_context_mode_combo.addItems(AUDIT_CONTEXT_MODE_CHOICES)
        self.audit_context_mode_combo.setToolTip(
            "Advanced experimental setting. Continuous conversation preserves current behavior. "
            "Fresh-context mode uses a new Responses conversation per chunk with retrieved saved context; "
            "it may reduce accumulated-context/file-service fragility but can change behavior and cost."
        )
        self.audit_context_mode_combo.currentTextChanged.connect(self.controller.set_audit_context_mode)
        layout.addRow("Context mode", self.audit_context_mode_combo)

        self.audit_prompt_button = QPushButton("Advanced Prompt Settings...")
        self.audit_prompt_button.setToolTip(
            "Optional: customize the audit system prompt for advanced or specialized audits. "
            "Most users can leave this unchanged."
        )
        self.audit_prompt_button.clicked.connect(self._open_audit_prompt_dialog)
        layout.addRow("Advanced", self.audit_prompt_button)

        return box

    def _build_controls_group(self) -> QGroupBox:
        box = QGroupBox("Run Controls")
        layout = QHBoxLayout(box)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

        self.start_button = QPushButton("Start Fresh Audit")
        self.start_button.clicked.connect(self._start_fresh_audit)
        self.resume_button = QPushButton("Resume Audit")
        self.resume_button.clicked.connect(self._resume_audit)
        self.pause_button = QPushButton("Pause Audit")
        self.pause_button.clicked.connect(self.controller.pause_audit)
        self.cancel_current_button = QPushButton("Cancel Current Chunk")
        self.cancel_current_button.clicked.connect(self.controller.cancel_current_chunk)

        layout.addWidget(self.start_button)
        layout.addWidget(self.resume_button)
        layout.addWidget(self.pause_button)
        layout.addWidget(self.cancel_current_button)
        layout.addStretch(1)
        return box

    def _build_status_group(self) -> QGroupBox:
        box = QGroupBox("Status")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(1)

        self.status_value = QLabel("No PDF selected")
        self.current_chunk_value = QLabel("-")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.chunk_progress_value = QLabel("0 / 0")
        self.page_progress_value = QLabel("0 / 0")
        self.cost_value = QLabel("$0.0000")
        self.tokens_value = QLabel("0")
        self.elapsed_value = QLabel("0s")
        self.cache_reuse_value = QLabel("-")
        self.pause_state_value = QLabel("Not requested")
        self.discussion_cost_value = QLabel("Cost: $0.0000")
        self.discussion_tokens_value = QLabel("Tokens: 0")
        self.discussion_turns_value = QLabel("Turns: 0")

        prominent_style = "font-weight: 600;"

        status_label = QLabel("Status")
        status_label.setStyleSheet(prominent_style)
        progress_label = QLabel("Progress")
        progress_label.setStyleSheet(prominent_style)
        pages_label = QLabel("Pages")
        pages_label.setStyleSheet(prominent_style)
        cost_label = QLabel("Cost")
        cost_label.setStyleSheet(prominent_style)
        elapsed_label = QLabel("Elapsed")
        elapsed_label.setStyleSheet(prominent_style)
        for value in [
            self.status_value,
            self.page_progress_value,
            self.cost_value,
            self.elapsed_value,
        ]:
            value.setStyleSheet(prominent_style)

        grid.addWidget(status_label, 0, 0)
        grid.addWidget(self.status_value, 0, 1)
        grid.addWidget(QLabel("Current chunk"), 0, 2)
        grid.addWidget(self.current_chunk_value, 0, 3)
        grid.addWidget(progress_label, 1, 0)
        grid.addWidget(self.progress_bar, 1, 1)
        grid.addWidget(QLabel("Chunks"), 1, 2)
        grid.addWidget(self.chunk_progress_value, 1, 3)
        grid.addWidget(pages_label, 2, 0)
        grid.addWidget(self.page_progress_value, 2, 1)
        grid.addWidget(cost_label, 2, 2)
        grid.addWidget(self.cost_value, 2, 3)
        grid.addWidget(QLabel("Tokens"), 3, 0)
        grid.addWidget(self.tokens_value, 3, 1)
        grid.addWidget(elapsed_label, 3, 2)
        grid.addWidget(self.elapsed_value, 3, 3)
        grid.addWidget(QLabel("Pause"), 4, 0)
        grid.addWidget(self.pause_state_value, 4, 1, 1, 3)
        grid.addWidget(QLabel("Last chunk cache"), 5, 0)
        grid.addWidget(self.cache_reuse_value, 5, 1, 1, 3)
        grid.addWidget(QLabel("Discussion usage"), 6, 0)
        grid.addWidget(self.discussion_cost_value, 6, 1)
        grid.addWidget(self.discussion_tokens_value, 6, 2)
        grid.addWidget(self.discussion_turns_value, 6, 3)

        layout.addLayout(grid)
        return box

    def _build_reports_section(self, title: str, help_text: str) -> tuple[QGroupBox, QVBoxLayout]:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(5)
        hint = QLabel(help_text)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555;")
        layout.addWidget(hint)
        return box, layout

    def _build_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        self.tabs = tabs

        reports_tab = QWidget()
        reports_tab_layout = QVBoxLayout(reports_tab)
        reports_tab_layout.setContentsMargins(0, 0, 0, 0)
        reports_scroll = QScrollArea()
        reports_scroll.setWidgetResizable(True)
        reports_content = QWidget()
        reports_layout = QVBoxLayout(reports_content)
        reports_layout.setContentsMargins(8, 8, 8, 8)
        reports_layout.setSpacing(8)
        reports_scroll.setWidget(reports_content)
        reports_tab_layout.addWidget(reports_scroll)

        quick_box, quick_layout = self._build_reports_section(
            "Quick review",
            "Quickest way to inspect the audit outcome.",
        )
        self.rebuild_final_button = QPushButton("Rebuild Final Report")
        self.rebuild_final_button.clicked.connect(self.controller.rebuild_final_report)
        self.build_concise_button = QPushButton("Build Concise Report")
        self.build_concise_button.clicked.connect(self._open_concise_report_options_dialog)
        self.run_verification_button = QPushButton("Run Verification Suite")
        self.run_verification_button.clicked.connect(self.controller.run_verification_suite)
        self.rebuild_verification_button = QPushButton("Rebuild Verification Report")
        self.rebuild_verification_button.clicked.connect(self.controller.rebuild_verification_report)
        self.open_full_report_button = QPushButton("Open Full Report")
        self.open_full_report_button.clicked.connect(lambda: self._open_report_tex("full"))
        self.open_concise_report_button = QPushButton("Open Concise Report")
        self.open_concise_report_button.clicked.connect(lambda: self._open_report_tex("concise"))
        self.open_verification_report_button = QPushButton("Open Verification Report")
        self.open_verification_report_button.clicked.connect(lambda: self._open_report_tex("verification"))
        self.open_reports_folder_button = QPushButton("Open Reports Folder")
        self.open_reports_folder_button.clicked.connect(self._open_reports_folder)

        self.export_chatgpt_context_pack_button = QPushButton("Export ChatGPT Context Pack")
        self.export_chatgpt_context_pack_button.clicked.connect(self._open_chatgpt_context_pack_dialog)
        self.open_chatgpt_export_folder_button = QPushButton("Open Export Folder")
        self.open_chatgpt_export_folder_button.clicked.connect(self._open_chatgpt_export_folder)
        self.copy_chatgpt_starter_prompt_button = QPushButton("Copy Starter Prompt")
        self.copy_chatgpt_starter_prompt_button.clicked.connect(self._copy_chatgpt_starter_prompt)
        self.open_chatgpt_website_button = QPushButton("Open ChatGPT Website")
        self.open_chatgpt_website_button.clicked.connect(self._open_chatgpt_website)

        rerun_box = QGroupBox("Selective Chunk Rerun")
        rerun_box.setMinimumHeight(220)
        rerun_layout = QFormLayout(rerun_box)
        rerun_chunk_row = QWidget()
        rerun_chunk_row_layout = QHBoxLayout(rerun_chunk_row)
        rerun_chunk_row_layout.setContentsMargins(0, 0, 0, 0)
        self.rerun_chunk_input = QLineEdit()
        self.rerun_chunk_input.setPlaceholderText("chunk_012 or 12,15,18")
        self.select_rerun_chunks_button = QPushButton("Select Chunks...")
        self.select_rerun_chunks_button.clicked.connect(self._open_chunk_selection_dialog)
        rerun_chunk_row_layout.addWidget(self.rerun_chunk_input, 1)
        rerun_chunk_row_layout.addWidget(self.select_rerun_chunks_button)
        rerun_layout.addRow("Chunks", rerun_chunk_row)
        self.rerun_instruction_input = QTextEdit()
        self.rerun_instruction_input.setPlaceholderText("Extra instruction for this rerun only (optional)")
        self.rerun_instruction_input.setFixedHeight(80)
        rerun_layout.addRow("Extra instruction", self.rerun_instruction_input)
        rerun_controls = QHBoxLayout()
        self.rerun_rebuild_checkbox = QCheckBox("Rebuild reports after rerun")
        self.rerun_rebuild_checkbox.setChecked(True)
        self.rerun_selected_button = QPushButton("Rerun Selected Chunks")
        self.rerun_selected_button.clicked.connect(self._rerun_selected_chunks)
        rerun_controls.addWidget(self.rerun_rebuild_checkbox)
        rerun_controls.addWidget(self.rerun_selected_button)
        rerun_controls.addStretch(1)
        rerun_layout.addRow("", rerun_controls)

        failed_rerun_box = QGroupBox("Failed Verification Rerun")
        self.failed_verification_rerun_box = failed_rerun_box
        failed_rerun_layout = QVBoxLayout(failed_rerun_box)
        failed_rerun_layout.setContentsMargins(8, 6, 8, 8)
        self.failed_verification_stack = QStackedWidget()
        self.failed_verification_compact_view = QWidget()
        compact_layout = QVBoxLayout(self.failed_verification_compact_view)
        compact_layout.setContentsMargins(0, 0, 0, 0)
        compact_layout.setSpacing(4)
        self.failed_verification_compact_status = QLabel("Failed chunks: none")
        self.failed_verification_compact_hint = QLabel(
            "No failed verification results. Run the verification suite to populate this repair tool."
        )
        self.failed_verification_compact_hint.setWordWrap(True)
        self.failed_verification_compact_hint.setStyleSheet("color: #555;")
        compact_layout.addWidget(self.failed_verification_compact_status)
        compact_layout.addWidget(self.failed_verification_compact_hint)
        compact_layout.addStretch(1)

        self.failed_verification_expanded_view = QWidget()
        failed_rerun_expanded_layout = QVBoxLayout(self.failed_verification_expanded_view)
        failed_rerun_expanded_layout.setContentsMargins(0, 0, 0, 0)
        failed_rerun_expanded_layout.setSpacing(5)
        failed_rerun_expanded_layout.addWidget(QLabel("Failed chunks"))
        self.failed_verification_summary_value = QPlainTextEdit()
        self.failed_verification_summary_value.setReadOnly(True)
        self.failed_verification_summary_value.setMinimumHeight(75)
        self.failed_verification_summary_value.setMaximumHeight(115)
        self.failed_verification_summary_value.setPlainText("No failed verification results")
        failed_rerun_expanded_layout.addWidget(self.failed_verification_summary_value)
        failed_rerun_form = QFormLayout()
        failed_rerun_form.setContentsMargins(0, 0, 0, 0)
        self.failed_verification_chunk_input = QLineEdit()
        self.failed_verification_chunk_input.setPlaceholderText("blank = all failed; or chunk_012, 12,15")
        failed_rerun_form.addRow("Chunks", self.failed_verification_chunk_input)
        self.failed_verification_options_widget = QWidget()
        failed_rerun_options = QHBoxLayout(self.failed_verification_options_widget)
        failed_rerun_options.setContentsMargins(0, 0, 0, 0)
        self.failed_verification_include_output_checkbox = QCheckBox("Include verification failure output in rerun prompt")
        self.failed_verification_include_output_checkbox.setChecked(True)
        self.failed_verification_rebuild_checkbox = QCheckBox("Rebuild reports after rerun")
        self.failed_verification_rebuild_checkbox.setChecked(True)
        failed_rerun_options.addWidget(self.failed_verification_include_output_checkbox)
        failed_rerun_options.addWidget(self.failed_verification_rebuild_checkbox)
        failed_rerun_options.addStretch(1)
        failed_rerun_form.addRow("Options", self.failed_verification_options_widget)
        self.failed_verification_action_widget = QWidget()
        failed_rerun_actions = QHBoxLayout(self.failed_verification_action_widget)
        failed_rerun_actions.setContentsMargins(0, 0, 0, 0)
        self.rerun_failed_verification_button = QPushButton("Rerun Selected Failed-Verification Chunks")
        self.rerun_failed_verification_button.clicked.connect(self._rerun_failed_verification_chunks)
        failed_rerun_actions.addWidget(self.rerun_failed_verification_button)
        failed_rerun_actions.addStretch(1)
        failed_rerun_form.addRow("", self.failed_verification_action_widget)
        failed_rerun_expanded_layout.addLayout(failed_rerun_form)
        self.failed_verification_stack.addWidget(self.failed_verification_compact_view)
        self.failed_verification_stack.addWidget(self.failed_verification_expanded_view)
        failed_rerun_layout.addWidget(self.failed_verification_stack)
        self._set_failed_verification_rerun_compact(0)

        technical_repair_box = QGroupBox("Technical Verification Repairs")
        technical_repair_layout = QVBoxLayout(technical_repair_box)
        technical_repair_hint = QLabel(
            "Repair a failed script first, preserving the original chunk audit. Generation uses the API; reviewed "
            "replacement execution is local and makes no API call. Full chunk re-audit remains a separate fallback."
        )
        technical_repair_hint.setWordWrap(True)
        technical_repair_hint.setStyleSheet("color: #555;")
        technical_repair_layout.addWidget(technical_repair_hint)
        self.verification_technical_repair_summary = QPlainTextEdit()
        self.verification_technical_repair_summary.setReadOnly(True)
        self.verification_technical_repair_summary.setMaximumHeight(145)
        self.verification_technical_repair_summary.setPlainText(
            "No active technical verification failures require repair."
        )
        technical_repair_layout.addWidget(self.verification_technical_repair_summary)
        self.verification_technical_repair_combo = QComboBox()
        self.verification_technical_repair_combo.currentIndexChanged.connect(self._apply_button_states)
        technical_repair_layout.addWidget(self.verification_technical_repair_combo)
        self.generate_verification_technical_repair_button = QPushButton("Generate Repair Scripts")
        self.generate_verification_technical_repair_button.clicked.connect(
            self._generate_verification_technical_repairs
        )
        technical_repair_layout.addWidget(
            self.generate_verification_technical_repair_button, 0, Qt.AlignmentFlag.AlignLeft
        )
        replacement_row = QHBoxLayout()
        self.verification_technical_replacement_combo = QComboBox()
        self.verification_technical_replacement_combo.currentIndexChanged.connect(self._apply_button_states)
        replacement_row.addWidget(self.verification_technical_replacement_combo, 1)
        self.review_verification_technical_replacement_button = QPushButton("Review Complete Code")
        self.review_verification_technical_replacement_button.clicked.connect(
            self._review_verification_technical_replacement
        )
        self.run_verification_technical_replacement_button = QPushButton("Run Safe Replacement Checks")
        self.run_verification_technical_replacement_button.clicked.connect(
            self._run_verification_technical_replacements
        )
        replacement_row.addWidget(self.review_verification_technical_replacement_button)
        replacement_row.addWidget(self.run_verification_technical_replacement_button)
        technical_repair_layout.addLayout(replacement_row)
        self.verification_technical_repair_status = QLabel("No repair action available.")
        self.verification_technical_repair_status.setWordWrap(True)
        self.verification_technical_repair_status.setStyleSheet("color: #8a4b00; font-weight: 600;")
        technical_repair_layout.addWidget(self.verification_technical_repair_status)

        counterexample_recheck_box = QGroupBox("Counterexample Finding Recheck")
        counterexample_recheck_layout = QVBoxLayout(counterexample_recheck_box)
        counterexample_recheck_layout.setContentsMargins(8, 8, 8, 8)
        counterexample_recheck_layout.setSpacing(6)
        self.counterexample_recheck_summary_value = QPlainTextEdit()
        self.counterexample_recheck_summary_value.setReadOnly(True)
        self.counterexample_recheck_summary_value.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.counterexample_recheck_summary_value.setMinimumHeight(92)
        self.counterexample_recheck_summary_value.setMaximumHeight(220)
        self.counterexample_recheck_summary_value.setPlainText(
            "No active counterexample findings require recheck."
        )
        counterexample_recheck_layout.addWidget(self.counterexample_recheck_summary_value)
        selection_row = QHBoxLayout()
        selection_row.setContentsMargins(0, 0, 0, 0)
        selection_row.addWidget(QLabel("Selection"))
        self.counterexample_recheck_combo = QComboBox()
        self.counterexample_recheck_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.counterexample_recheck_combo.setMinimumContentsLength(34)
        self.counterexample_recheck_combo.currentIndexChanged.connect(self._apply_button_states)
        selection_row.addWidget(self.counterexample_recheck_combo, 1)
        counterexample_recheck_layout.addLayout(selection_row)
        counterexample_hint = QLabel(
            "The recheck sends the complete manuscript chunk, verification script, output, and counterexample "
            "evidence for focused review. It preserves the original audit and incurs API cost."
        )
        counterexample_hint.setWordWrap(True)
        counterexample_hint.setStyleSheet("color: #555;")
        counterexample_recheck_layout.addWidget(counterexample_hint)
        self.counterexample_recheck_status_value = QLabel("No eligible counterexample findings.")
        self.counterexample_recheck_status_value.setWordWrap(True)
        self.counterexample_recheck_status_value.setStyleSheet("color: #8a4b00; font-weight: 600;")
        counterexample_recheck_layout.addWidget(self.counterexample_recheck_status_value)
        self.recheck_counterexample_button = QPushButton("Recheck Counterexample Findings")
        self.recheck_counterexample_button.clicked.connect(self._recheck_counterexample_findings)
        counterexample_recheck_layout.addWidget(self.recheck_counterexample_button, 0, Qt.AlignmentFlag.AlignLeft)
        replacement_heading = QLabel("Replacement verification scripts")
        replacement_heading.setStyleSheet("font-weight: 600; margin-top: 6px;")
        counterexample_recheck_layout.addWidget(replacement_heading)
        self.verification_replacement_summary_value = QPlainTextEdit()
        self.verification_replacement_summary_value.setReadOnly(True)
        self.verification_replacement_summary_value.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.verification_replacement_summary_value.setMinimumHeight(78)
        self.verification_replacement_summary_value.setMaximumHeight(190)
        self.verification_replacement_summary_value.setPlainText(
            "No replacement verification scripts have been proposed."
        )
        counterexample_recheck_layout.addWidget(self.verification_replacement_summary_value)
        self.verification_replacement_combo = QComboBox()
        self.verification_replacement_combo.currentIndexChanged.connect(self._apply_button_states)
        counterexample_recheck_layout.addWidget(self.verification_replacement_combo)
        replacement_actions = QHBoxLayout()
        self.review_verification_replacement_button = QPushButton("Review Script")
        self.review_verification_replacement_button.clicked.connect(self._review_verification_replacement_script)
        self.run_verification_replacement_button = QPushButton("Run Safe Replacement Checks")
        self.run_verification_replacement_button.clicked.connect(self._run_verification_replacement_checks)
        replacement_actions.addWidget(self.review_verification_replacement_button)
        replacement_actions.addWidget(self.run_verification_replacement_button)
        replacement_actions.addStretch(1)
        counterexample_recheck_layout.addLayout(replacement_actions)
        replacement_hint = QLabel(
            "Review generated code before running it. Execution is local, uses safe-mode validation, and makes no API call."
        )
        replacement_hint.setWordWrap(True)
        replacement_hint.setStyleSheet("color: #555;")
        counterexample_recheck_layout.addWidget(replacement_hint)
        self.verification_replacement_status_value = QLabel("No runnable replacement checks.")
        self.verification_replacement_status_value.setWordWrap(True)
        self.verification_replacement_status_value.setStyleSheet("color: #8a4b00; font-weight: 600;")
        counterexample_recheck_layout.addWidget(self.verification_replacement_status_value)

        quick_buttons = QHBoxLayout()
        quick_buttons.addWidget(self.build_concise_button)
        quick_buttons.addWidget(self.open_concise_report_button)
        quick_buttons.addStretch(1)
        quick_layout.addLayout(quick_buttons)
        self.concise_report_freshness_value = QLabel("Concise Report: freshness unknown")
        self.concise_report_freshness_value.setWordWrap(True)
        quick_layout.addWidget(self.concise_report_freshness_value)
        reports_layout.addWidget(quick_box)

        verification_box, verification_layout = self._build_reports_section(
            "Verification and repair",
            "Run verification before finalizing the audit; rerun failed chunks here if needed.",
        )
        self.verification_scripts_available_value = QLabel("Verification scripts available: 0")
        self.verification_last_run_value = QLabel("Last run: none")
        self.verification_inventory_warning_value = QLabel("")
        self.verification_inventory_warning_value.setWordWrap(True)
        self.verification_inventory_warning_value.setStyleSheet("color: #8a4b00; font-weight: 600;")
        self.verification_live_progress_value = QLabel("Verification progress: idle")
        verification_layout.addWidget(self.verification_scripts_available_value)
        verification_layout.addWidget(self.verification_last_run_value)
        verification_layout.addWidget(self.verification_inventory_warning_value)
        verification_layout.addWidget(self.verification_live_progress_value)
        verification_buttons = QHBoxLayout()
        verification_buttons.addWidget(self.run_verification_button)
        verification_buttons.addWidget(self.rebuild_verification_button)
        verification_buttons.addWidget(self.open_verification_report_button)
        verification_buttons.addStretch(1)
        verification_layout.addLayout(verification_buttons)
        self.verification_report_freshness_value = QLabel("Verification Report: freshness unknown")
        self.verification_report_freshness_value.setWordWrap(True)
        verification_layout.addWidget(self.verification_report_freshness_value)
        verification_layout.addWidget(technical_repair_box)
        verification_layout.addWidget(failed_rerun_box)
        verification_layout.addWidget(counterexample_recheck_box)
        reports_layout.addWidget(verification_box)

        finalize_box, finalize_layout = self._build_reports_section(
            "Finalize reports",
            "Polished report output after verification and repairs.",
        )
        finalize_buttons = QHBoxLayout()
        finalize_buttons.addWidget(self.rebuild_final_button)
        finalize_buttons.addWidget(self.open_full_report_button)
        finalize_buttons.addWidget(self.open_reports_folder_button)
        finalize_buttons.addStretch(1)
        finalize_layout.addLayout(finalize_buttons)
        self.full_report_freshness_value = QLabel("Full Report: freshness unknown")
        self.full_report_freshness_value.setWordWrap(True)
        finalize_layout.addWidget(self.full_report_freshness_value)
        reports_layout.addWidget(finalize_box)

        export_box, export_layout = self._build_reports_section(
            "Export / handoff",
            "Continue work outside the app with a ChatGPT handoff pack.",
        )
        export_buttons = QHBoxLayout()
        export_buttons.addWidget(self.export_chatgpt_context_pack_button)
        export_buttons.addWidget(self.open_chatgpt_export_folder_button)
        export_buttons.addWidget(self.copy_chatgpt_starter_prompt_button)
        export_buttons.addWidget(self.open_chatgpt_website_button)
        export_buttons.addStretch(1)
        export_layout.addLayout(export_buttons)
        reports_layout.addWidget(export_box)

        output_box, output_layout = self._build_reports_section(
            "Report output",
            "Recent report, verification, export, and repair messages appear here.",
        )
        self.report_output = QPlainTextEdit()
        self.report_output.setReadOnly(True)
        self.report_output.setPlaceholderText("Report rebuild output")
        self.report_output.setMinimumHeight(100)
        self.report_output.setMaximumHeight(150)
        output_layout.addWidget(self.report_output)
        output_box.setMaximumHeight(215)
        reports_layout.addWidget(output_box)

        advanced_box, advanced_layout = self._build_reports_section(
            "Advanced manual repair",
            "Manual tool for rerunning specific chunks when you know exactly what needs another pass.",
        )
        advanced_layout.addWidget(rerun_box)
        reports_layout.addWidget(advanced_box)
        tabs.addTab(reports_tab, "Reports")
        self._review_tab = self._build_review_tab()
        if review_tab_enabled():
            tabs.addTab(self._review_tab, "Review")

        discussion_tab = QWidget()
        discussion_layout = QVBoxLayout(discussion_tab)
        discussion_controls = QHBoxLayout()
        self.discussion_mode_combo = QComboBox()
        self.discussion_mode_combo.addItems(["Ask about paper", "Ask about audit"])
        discussion_controls.addWidget(self.discussion_mode_combo)
        self.new_discussion_thread_button = QPushButton("New Discussion Thread")
        self.new_discussion_thread_button.clicked.connect(self.controller.start_new_discussion_thread)
        discussion_controls.addWidget(self.new_discussion_thread_button)
        discussion_controls.addWidget(QLabel("Discussion thread"))
        self.discussion_thread_combo = QComboBox()
        self.discussion_thread_combo.currentIndexChanged.connect(self._on_discussion_thread_changed)
        discussion_controls.addWidget(self.discussion_thread_combo)
        discussion_controls.addWidget(QLabel("View"))
        self.discussion_view_combo = QComboBox()
        self.discussion_view_combo.addItems(["Raw", "Rendered"])
        self.discussion_view_combo.currentTextChanged.connect(self._on_discussion_view_mode_changed)
        discussion_controls.addWidget(self.discussion_view_combo)
        discussion_controls.addStretch(1)
        discussion_layout.addLayout(discussion_controls)

        self.question_input = QTextEdit()
        self.question_input.setPlaceholderText("Enter a post-audit question.")
        self.question_input.setFixedHeight(100)
        discussion_layout.addWidget(self.question_input)

        ask_row = QHBoxLayout()
        self.ask_button = QPushButton("Ask")
        self.ask_button.clicked.connect(self._submit_question)
        ask_row.addWidget(self.ask_button)
        ask_row.addStretch(1)
        discussion_layout.addLayout(ask_row)

        self.answer_stack = QStackedWidget()
        self.answer_raw_output = QPlainTextEdit()
        self.answer_raw_output.setReadOnly(True)
        self.answer_raw_output.setPlaceholderText("Discussion output")
        self.answer_output = self.answer_raw_output
        self.answer_rendered_output = QWebEngineView()
        self.answer_rendered_output.settings().setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        self.answer_stack.addWidget(self.answer_raw_output)
        self.answer_stack.addWidget(self.answer_rendered_output)
        discussion_layout.addWidget(self.answer_stack)
        tabs.addTab(discussion_tab, "Discussion")

        logs_tab = QWidget()
        logs_layout = QVBoxLayout(logs_tab)
        logs_layout.setContentsMargins(8, 8, 8, 8)
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText("Event log")
        logs_layout.addWidget(self.log_output)
        tabs.addTab(logs_tab, "Logs")

        return tabs

    def _build_review_tab(self) -> QWidget:
        review_tab = QWidget()
        layout = QVBoxLayout(review_tab)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        review_layout = QVBoxLayout(content)
        review_layout.setContentsMargins(8, 8, 8, 8)
        review_layout.setSpacing(8)
        scroll.setWidget(content)
        layout.addWidget(scroll)

        overview_box, overview_layout = self._build_reports_section(
            "Post-audit review state",
            "Review/recheck candidates are planning artifacts. Candidate for review/recheck does not imply full chunk rerun.",
        )
        self.review_status_value = QLabel("No audit session selected.")
        self.review_status_value.setWordWrap(True)
        overview_layout.addWidget(self.review_status_value)
        overview_buttons = QHBoxLayout()
        self.refresh_review_summary_button = QPushButton("Refresh Review Summary")
        self.refresh_review_summary_button.clicked.connect(self.controller.refresh_review_summary)
        self.prepare_review_summary_button = QPushButton("Prepare Review Summary")
        self.prepare_review_summary_button.clicked.connect(self.controller.prepare_review_summary)
        self.open_review_folder_button = QPushButton("Open Review Files Folder")
        self.open_review_folder_button.clicked.connect(self._open_review_folder)
        self.open_issue_rechecks_button = QPushButton("Open issue_rechecks.json")
        self.open_issue_rechecks_button.clicked.connect(self._open_issue_rechecks_sidecar)
        overview_buttons.addWidget(self.refresh_review_summary_button)
        overview_buttons.addWidget(self.prepare_review_summary_button)
        overview_buttons.addWidget(self.open_review_folder_button)
        overview_buttons.addWidget(self.open_issue_rechecks_button)
        overview_buttons.addStretch(1)
        overview_layout.addLayout(overview_buttons)
        review_layout.addWidget(overview_box)

        accepted_box, accepted_layout = self._build_reports_section(
            "Accepted recheck overlays",
            "Accepted family rechecks are advisory report overlays; canonical issue records are not modified.",
        )
        self.review_accepted_rechecks_value = QPlainTextEdit()
        self.review_accepted_rechecks_value.setReadOnly(True)
        self.review_accepted_rechecks_value.setMinimumHeight(120)
        self.review_accepted_rechecks_value.setPlainText("No accepted issue-family rechecks found.")
        accepted_layout.addWidget(self.review_accepted_rechecks_value)
        review_layout.addWidget(accepted_box)

        candidate_box, candidate_layout = self._build_reports_section(
            "Candidate/action inventory",
            "Prepared sidecars summarize issue-level rechecks, grouping reviews, verification-script checks, and true chunk reruns separately.",
        )
        self.review_candidate_summary_value = QPlainTextEdit()
        self.review_candidate_summary_value.setReadOnly(True)
        self.review_candidate_summary_value.setMinimumHeight(130)
        self.review_candidate_summary_value.setPlainText("No candidate inventory prepared yet.")
        candidate_layout.addWidget(self.review_candidate_summary_value)
        review_layout.addWidget(candidate_box)

        family_box, family_layout = self._build_reports_section(
            "Dependency families",
            "Prepared family sidecars consolidate overlapping dependency-propagation groups for human review.",
        )
        family_select_row = QHBoxLayout()
        family_select_row.addWidget(QLabel("Selected family"))
        self.review_family_combo = QComboBox()
        self.review_family_combo.currentIndexChanged.connect(self._on_review_family_selected)
        family_select_row.addWidget(self.review_family_combo, 1)
        self.prepare_family_recheck_dry_run_button = QPushButton("Prepare Selected Family Recheck Dry Run")
        self.prepare_family_recheck_dry_run_button.clicked.connect(self._prepare_selected_family_recheck_dry_run)
        family_select_row.addWidget(self.prepare_family_recheck_dry_run_button)
        self.run_live_family_recheck_button = QPushButton("Run Live Recheck for Selected Family")
        self.run_live_family_recheck_button.clicked.connect(self._run_live_family_recheck)
        family_select_row.addWidget(self.run_live_family_recheck_button)
        family_layout.addLayout(family_select_row)
        self.review_family_details_value = QPlainTextEdit()
        self.review_family_details_value.setReadOnly(True)
        self.review_family_details_value.setMinimumHeight(140)
        self.review_family_details_value.setPlainText("No issue family selected.")
        family_layout.addWidget(self.review_family_details_value)
        family_artifact_buttons = QHBoxLayout()
        self.open_family_recheck_folder_button = QPushButton("Open Family Recheck Folder")
        self.open_family_recheck_folder_button.clicked.connect(self._open_family_recheck_folder)
        self.open_family_recheck_prompt_button = QPushButton("Open Prompt")
        self.open_family_recheck_prompt_button.clicked.connect(self._open_family_recheck_prompt)
        self.open_family_recheck_evidence_button = QPushButton("Open Evidence")
        self.open_family_recheck_evidence_button.clicked.connect(self._open_family_recheck_evidence)
        self.open_family_recheck_result_button = QPushButton("Open Result")
        self.open_family_recheck_result_button.clicked.connect(self._open_family_recheck_result)
        self.import_accepted_recheck_button = QPushButton("Import Accepted Recheck Result...")
        self.import_accepted_recheck_button.clicked.connect(self._import_accepted_recheck_result)
        family_artifact_buttons.addWidget(self.open_family_recheck_folder_button)
        family_artifact_buttons.addWidget(self.open_family_recheck_prompt_button)
        family_artifact_buttons.addWidget(self.open_family_recheck_evidence_button)
        family_artifact_buttons.addWidget(self.open_family_recheck_result_button)
        family_artifact_buttons.addWidget(self.import_accepted_recheck_button)
        family_artifact_buttons.addStretch(1)
        family_layout.addLayout(family_artifact_buttons)
        family_hint = QLabel(
            "Live issue-family rechecks require confirmation, recheck one selected family only, "
            "and do not import/apply results automatically."
        )
        family_hint.setWordWrap(True)
        family_hint.setStyleSheet("color: #555;")
        family_layout.addWidget(family_hint)
        self.review_family_summary_value = QPlainTextEdit()
        self.review_family_summary_value.setReadOnly(True)
        self.review_family_summary_value.setMinimumHeight(150)
        self.review_family_summary_value.setPlainText("No issue-family summary prepared yet.")
        family_layout.addWidget(self.review_family_summary_value)
        review_layout.addWidget(family_box)

        review_layout.addStretch(1)
        return review_tab

    def _show_logs_tab(self) -> None:
        tabs = getattr(self, "tabs", None)
        if tabs is None:
            return
        for index in range(tabs.count()):
            if tabs.tabText(index) == "Logs":
                tabs.setCurrentIndex(index)
                return

    def _set_failed_verification_rerun_compact(self, failed_count: int) -> None:
        if not hasattr(self, "failed_verification_rerun_box"):
            return
        expanded = int(failed_count or 0) > 0
        self.failed_verification_stack.setCurrentWidget(
            self.failed_verification_expanded_view if expanded else self.failed_verification_compact_view
        )
        if expanded:
            self.failed_verification_rerun_box.setMinimumHeight(245)
            self.failed_verification_rerun_box.setMaximumHeight(340)
        else:
            self.failed_verification_rerun_box.setMinimumHeight(85)
            self.failed_verification_rerun_box.setMaximumHeight(125)

    def _browse_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose PDF",
            str(Path.home()),
            "PDF Files (*.pdf)",
        )
        if not path:
            return
        self.pdf_path_input.setText(path)
        self._apply_pdf_path()

    def _apply_api_key(self) -> None:
        self.controller.set_api_key(self.api_key_input.text())
        self._apply_button_states()

    def _apply_pdf_path(self) -> None:
        self.controller.set_pdf_path(self.pdf_path_input.text())

    def _open_audit_prompt_dialog(self) -> None:
        dialog = AuditPromptDialog(self.controller, self)
        dialog.exec()

    def _open_concise_report_options_dialog(self) -> None:
        dialog = ConciseReportOptionsDialog(self)
        if dialog.exec() == QDialog.Accepted:
            self.controller.build_concise_report(options=dialog.options())

    def _open_chatgpt_context_pack_dialog(self) -> None:
        dialog = ChatGPTContextPackDialog(self)
        if dialog.exec() == QDialog.Accepted:
            self.controller.export_chatgpt_context_pack(options=dialog.options())

    def _available_rerun_chunks(self) -> list[dict[str, Any]]:
        manifest = (self._last_status_payload or {}).get("manifest") or {}
        chunks = manifest.get("chunks") if isinstance(manifest, dict) else []
        if not isinstance(chunks, list):
            return []
        return [chunk for chunk in chunks if isinstance(chunk, dict) and str(chunk.get("chunk_id") or "").strip()]

    def _canonical_rerun_chunk_ids_from_text(self, text: str, known_ids: set[str]) -> set[str]:
        known_by_lower = {chunk_id.lower(): chunk_id for chunk_id in known_ids}
        selected: set[str] = set()
        for token in re.split(r"[,;\s]+", str(text or "")):
            raw = token.strip()
            if not raw:
                continue
            candidates = [raw.lower()]
            match = re.fullmatch(r"(?:chunk[_-]?)?0*(\d+)", raw.lower())
            if match:
                candidates.append(f"chunk_{int(match.group(1)):03d}")
            for candidate in candidates:
                if candidate in known_by_lower:
                    selected.add(known_by_lower[candidate])
                    break
        return selected

    def _open_chunk_selection_dialog(self) -> None:
        chunks = self._available_rerun_chunks()
        if not chunks:
            self._append_report_output("No chunk manifest is available for this session yet.")
            return
        known_ids = {str(chunk.get("chunk_id") or "").strip() for chunk in chunks}
        selected_ids = self._canonical_rerun_chunk_ids_from_text(self.rerun_chunk_input.text(), known_ids)
        dialog = ChunkSelectionDialog(chunks, selected_ids, self)
        if dialog.exec() == QDialog.Accepted:
            self.rerun_chunk_input.setText(", ".join(dialog.selected_chunk_ids()))

    def _on_model_changed(self, model: str) -> None:
        self.controller.set_model(model)
        self._refresh_reasoning_options(
            self.controller.model,
            selected_effort=self.controller.reasoning_effort,
            notify_controller=False,
        )

    def _on_audit_settings_changed(self, model: str, reasoning_effort: str) -> None:
        self._set_model_effort_controls(model, reasoning_effort)

    def _on_audit_context_mode_changed(self, mode: str) -> None:
        label = next((label for label, value in AUDIT_CONTEXT_MODE_LABELS.items() if value == mode), None)
        if not label:
            return
        self.audit_context_mode_combo.blockSignals(True)
        self.audit_context_mode_combo.setCurrentText(label)
        self.audit_context_mode_combo.blockSignals(False)

    def _on_discussion_threads_loaded(self, threads: list[dict[str, Any]], active_thread_id: str) -> None:
        self.discussion_thread_combo.blockSignals(True)
        self.discussion_thread_combo.clear()
        active_index = -1
        for item in threads:
            if not isinstance(item, dict):
                continue
            thread_id = str(item.get("thread_id") or "").strip()
            if not thread_id:
                continue
            label = str(item.get("label") or thread_id).strip()
            self.discussion_thread_combo.addItem(label, thread_id)
            if thread_id == active_thread_id or item.get("is_active"):
                active_index = self.discussion_thread_combo.count() - 1
        if active_index >= 0:
            self.discussion_thread_combo.setCurrentIndex(active_index)
        self.discussion_thread_combo.blockSignals(False)
        self._apply_button_states()

    def _on_discussion_thread_changed(self, index: int) -> None:
        if index < 0:
            return
        thread_id = self.discussion_thread_combo.itemData(index)
        if thread_id:
            self.controller.set_active_discussion_thread(str(thread_id))

    def _set_model_effort_controls(self, model: str, reasoning_effort: str) -> None:
        self.model_combo.blockSignals(True)
        self.model_combo.setCurrentText(self.controller.model_display_name(model))
        self.model_combo.blockSignals(False)
        self._refresh_reasoning_options(model, selected_effort=reasoning_effort, notify_controller=False)

    def _refresh_reasoning_options(
        self,
        model: str,
        selected_effort: Optional[str] = None,
        notify_controller: bool = True,
    ) -> None:
        options = self.controller.reasoning_effort_options(model)
        default_effort = self.controller.default_reasoning_effort(model)
        effort = selected_effort if selected_effort in options else default_effort
        self.reasoning_combo.blockSignals(True)
        self.reasoning_combo.clear()
        self.reasoning_combo.addItems(options)
        self.reasoning_combo.setCurrentText(effort)
        self.reasoning_combo.setEnabled(len(options) > 1)
        if len(options) == 1:
            self.reasoning_combo.setToolTip("Reasoning effort is fixed for this model.")
        else:
            guidance = self.controller.reasoning_effort_guidance(model)
            if guidance:
                self.reasoning_combo.setToolTip("\n".join(f"{effort} — {guidance.get(effort, '')}" for effort in options))
            else:
                self.reasoning_combo.setToolTip("")
        self.reasoning_combo.blockSignals(False)
        if notify_controller:
            self.controller.set_reasoning_effort(self.reasoning_combo.currentText())

    def _start_fresh_audit(self) -> None:
        self._apply_api_key()
        self._apply_pdf_path()
        mismatch = self.controller.fresh_start_context_mode_mismatch_info()
        if mismatch:
            message = (
                "This will archive the existing "
                f"{mismatch.get('saved_label', mismatch.get('saved_mode', 'saved'))} audit folder "
                "and create a new "
                f"{mismatch.get('selected_label', mismatch.get('selected_mode', 'selected'))} audit in:\n\n"
                f"{mismatch.get('workdir', '')}\n\n"
                "Continue?"
            )
            choice = QMessageBox.question(
                self,
                "Start Fresh Audit With Different Context Mode",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if choice != QMessageBox.StandardButton.Yes:
                self.controller.log_message.emit("Start Fresh Audit cancelled before replacing audit context mode.")
                return
        self.controller.start_fresh_audit()
        if self.controller.active_task_name() == "Start Fresh Audit":
            self._show_logs_tab()

    def _resume_audit(self) -> None:
        self._apply_api_key()
        self._apply_pdf_path()
        self.controller.resume_audit()
        if self.controller.active_task_name() == "Resume Audit":
            self._show_logs_tab()

    def _submit_question(self) -> None:
        self._apply_api_key()
        self._apply_pdf_path()
        question = self.question_input.toPlainText().strip()
        if self.discussion_mode_combo.currentText() == "Ask about audit":
            self.controller.ask_about_audit(question)
        else:
            self.controller.ask_about_paper(question)

    def _rerun_selected_chunks(self) -> None:
        self._apply_api_key()
        self._apply_pdf_path()
        self.controller.rerun_selected_chunks(
            self.rerun_chunk_input.text(),
            self.rerun_instruction_input.toPlainText(),
            rebuild_reports=self.rerun_rebuild_checkbox.isChecked(),
        )

    def _rerun_failed_verification_chunks(self) -> None:
        self._apply_api_key()
        self._apply_pdf_path()
        self.controller.rerun_failed_verification_chunks(
            self.failed_verification_chunk_input.text(),
            include_verification_output=self.failed_verification_include_output_checkbox.isChecked(),
            rebuild_reports=self.failed_verification_rebuild_checkbox.isChecked(),
        )

    def _selected_counterexample_recheck_token(self) -> str:
        token = str(self.counterexample_recheck_combo.currentData() or "").strip()
        return "" if token == "all" else token

    def _update_counterexample_recheck_inventory(
        self,
        inventory: dict[str, Any],
        history_summary: Optional[dict[str, Any]] = None,
    ) -> None:
        view = _counterexample_recheck_view(inventory, history_summary)
        entries = view.get("entries") or []
        signature = (
            str(view.get("summary_text") or ""),
            tuple(
                (str(item.get("label") or ""), str(item.get("selection") or ""))
                for item in entries
                if isinstance(item, dict)
            ),
        )
        if signature == self._counterexample_recheck_inventory_signature:
            return

        previous_selection = self.counterexample_recheck_combo.currentData()
        _set_plain_text_preserving_scroll(
            self.counterexample_recheck_summary_value,
            str(view.get("summary_text") or ""),
        )
        self.counterexample_recheck_combo.blockSignals(True)
        try:
            self.counterexample_recheck_combo.clear()
            for entry in entries:
                self.counterexample_recheck_combo.addItem(
                    str(entry.get("label") or ""),
                    str(entry.get("selection") or ""),
                )
            selected_index = _counterexample_recheck_selection_index(entries, previous_selection)
            if selected_index >= 0:
                self.counterexample_recheck_combo.setCurrentIndex(selected_index)
        finally:
            self.counterexample_recheck_combo.blockSignals(False)
        self.counterexample_recheck_combo.setEnabled(bool(entries))
        self._counterexample_recheck_inventory_signature = signature

    def _update_verification_replacement_inventory(self, inventory: dict[str, Any]) -> None:
        view = _verification_replacement_view(inventory)
        entries = view.get("entries") or []
        signature = (
            str(view.get("summary_text") or ""),
            tuple(
                (
                    str(item.get("label") or ""),
                    json.dumps(item.get("data") or {}, sort_keys=True),
                    bool(item.get("ready")),
                )
                for item in entries
                if isinstance(item, dict)
            ),
        )
        if signature == self._verification_replacement_inventory_signature:
            return
        previous = self.verification_replacement_combo.currentData()
        previous_key = json.dumps(previous or {}, sort_keys=True)
        _set_plain_text_preserving_scroll(
            self.verification_replacement_summary_value,
            str(view.get("summary_text") or ""),
        )
        self.verification_replacement_combo.blockSignals(True)
        try:
            self.verification_replacement_combo.clear()
            selected_index = 0
            for index, entry in enumerate(entries):
                data = dict(entry.get("data") or {})
                data["ready"] = bool(entry.get("ready"))
                self.verification_replacement_combo.addItem(str(entry.get("label") or ""), data)
                if json.dumps(entry.get("data") or {}, sort_keys=True) == previous_key:
                    selected_index = index
            if entries:
                self.verification_replacement_combo.setCurrentIndex(selected_index)
        finally:
            self.verification_replacement_combo.blockSignals(False)
        self.verification_replacement_combo.setEnabled(bool(entries))
        self._verification_replacement_inventory_signature = signature

    def _selected_verification_replacement(self) -> dict[str, Any]:
        data = self.verification_replacement_combo.currentData()
        return dict(data) if isinstance(data, dict) else {}

    def _update_verification_technical_repairs(
        self, candidates: dict[str, Any], repairs: dict[str, Any]
    ) -> None:
        view = _verification_technical_repair_view(candidates, repairs)
        signature = (
            str(view.get("summary_text") or ""),
            json.dumps(view.get("candidate_entries") or [], sort_keys=True),
            json.dumps(view.get("replacement_entries") or [], sort_keys=True),
        )
        if signature == self._verification_technical_repair_signature:
            return
        prior_candidate = str(self.verification_technical_repair_combo.currentData() or "")
        prior_replacement = json.dumps(
            self.verification_technical_replacement_combo.currentData() or {}, sort_keys=True
        )
        _set_plain_text_preserving_scroll(
            self.verification_technical_repair_summary, str(view.get("summary_text") or "")
        )
        self.verification_technical_repair_combo.blockSignals(True)
        try:
            self.verification_technical_repair_combo.clear()
            candidate_index = 0
            for index, entry in enumerate(view.get("candidate_entries") or []):
                selection = str(entry.get("selection") or "")
                self.verification_technical_repair_combo.addItem(str(entry.get("label") or ""), selection)
                if selection == prior_candidate:
                    candidate_index = index
            if self.verification_technical_repair_combo.count():
                self.verification_technical_repair_combo.setCurrentIndex(candidate_index)
        finally:
            self.verification_technical_repair_combo.blockSignals(False)
        self.verification_technical_replacement_combo.blockSignals(True)
        try:
            self.verification_technical_replacement_combo.clear()
            replacement_index = 0
            for index, entry in enumerate(view.get("replacement_entries") or []):
                data = dict(entry.get("data") or {})
                self.verification_technical_replacement_combo.addItem(str(entry.get("label") or ""), data)
                if json.dumps(data, sort_keys=True) == prior_replacement:
                    replacement_index = index
            if self.verification_technical_replacement_combo.count():
                self.verification_technical_replacement_combo.setCurrentIndex(replacement_index)
        finally:
            self.verification_technical_replacement_combo.blockSignals(False)
        self.verification_technical_repair_combo.setEnabled(bool(view.get("candidate_entries")))
        self.verification_technical_replacement_combo.setEnabled(bool(view.get("replacement_entries")))
        self._verification_technical_repair_signature = signature

    def _generate_verification_technical_repairs(self) -> None:
        self._apply_api_key()
        self._apply_pdf_path()
        selection = str(self.verification_technical_repair_combo.currentData() or "")
        payload = self._last_status_payload or {}
        session = payload.get("session") or {}
        model = self.controller.model or session.get("model")
        effort = self.controller.reasoning_effort or session.get("reasoning_effort")
        choice = QMessageBox.question(
            self,
            "Generate Repair Scripts",
            (
                "Send the complete failed script, exact failure evidence, complete manuscript chunk, linked issues, "
                "and bounded context to the OpenAI API?\n\n"
                f"Model/reasoning: {model or 'saved session model'} / {effort or 'saved session effort'}\n"
                "This incurs API cost. The original script, result, chunk issues, and chunk context remain unchanged."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice == QMessageBox.StandardButton.Yes:
            self.controller.repair_verification_scripts("" if selection == "all" else selection)
        else:
            self.controller.log_message.emit("Technical verification repair cancelled.")

    def _selected_verification_technical_replacement(self) -> dict[str, Any]:
        data = self.verification_technical_replacement_combo.currentData()
        return dict(data) if isinstance(data, dict) else {}

    def _review_verification_technical_replacement(self) -> None:
        selected = self._selected_verification_technical_replacement()
        session = (self._last_status_payload or {}).get("session") or {}
        workdir = Path(str(session.get("workdir") or "")).resolve()
        blocks = []
        for relative in selected.get("script_paths") or []:
            path = (workdir / str(relative)).resolve()
            try:
                path.relative_to(workdir)
            except ValueError:
                continue
            if path.is_file():
                blocks.append(f"# {path.name}\n\n{path.read_text(encoding='utf-8')}")
        if not blocks:
            QMessageBox.information(self, "Technical Replacement", "No replacement code is selected.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Review Complete Technical Replacement Code")
        dialog.resize(820, 620)
        layout = QVBoxLayout(dialog)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText("\n\n".join(blocks))
        layout.addWidget(editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _run_verification_technical_replacements(self) -> None:
        self._apply_pdf_path()
        selected = self._selected_verification_technical_replacement()
        repair_id = str(selected.get("repair_id") or "")
        check_ids = [str(item) for item in selected.get("check_ids") or [] if str(item).strip()]
        if not repair_id or not check_ids or not selected.get("ready"):
            QMessageBox.information(self, "Technical Replacement", "Select a safe, ready replacement first.")
            return
        choice = QMessageBox.question(
            self,
            "Run Safe Technical Replacement",
            (
                "Run the reviewed replacement locally in safe mode?\n\n"
                "This makes no API call and does not overwrite the original script or verification result. "
                "The mathematical outcome remains provisional."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice == QMessageBox.StandardButton.Yes:
            self.controller.run_verification_technical_replacement_checks(repair_id, check_ids)
        else:
            self.controller.log_message.emit("Technical replacement execution cancelled.")

    def _review_verification_replacement_script(self) -> None:
        selected = self._selected_verification_replacement()
        script_paths = [str(item) for item in selected.get("script_paths") or [] if str(item).strip()]
        session = (self._last_status_payload or {}).get("session") or {}
        workdir_value = str(session.get("workdir") or "").strip()
        if not script_paths or not workdir_value:
            QMessageBox.information(self, "Replacement Script", "No replacement script is selected.")
            return
        workdir = Path(workdir_value).resolve()
        blocks = []
        for relative in script_paths:
            path = (workdir / relative).resolve()
            try:
                path.relative_to(workdir)
            except ValueError:
                continue
            if path.is_file():
                blocks.append(f"# {path.name}\n\n{path.read_text(encoding='utf-8')}")
        if not blocks:
            QMessageBox.warning(self, "Replacement Script", "The selected replacement script is unavailable.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Review Replacement Verification Script")
        dialog.resize(820, 620)
        layout = QVBoxLayout(dialog)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText("\n\n".join(blocks))
        layout.addWidget(editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def _run_verification_replacement_checks(self) -> None:
        self._apply_pdf_path()
        selected = self._selected_verification_replacement()
        recheck_id = str(selected.get("recheck_id") or "")
        check_ids = [str(item) for item in selected.get("check_ids") or [] if str(item).strip()]
        if not recheck_id or not check_ids or not selected.get("ready"):
            QMessageBox.information(self, "Replacement Checks", "Select a safe, ready replacement check first.")
            return
        check_text = ", ".join(check_ids)
        choice = QMessageBox.question(
            self,
            "Run Safe Replacement Checks",
            (
                f"Run the following locally in safe mode?\n\n{check_text}\n\n"
                "The original scripts and results will remain unchanged. This uses local computation and makes no API "
                "call. Results remain provisional and require human review."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Yes:
            self.controller.log_message.emit("Replacement verification execution cancelled.")
            return
        self.controller.run_verification_replacement_checks(recheck_id, check_ids)

    def _recheck_counterexample_findings(self) -> None:
        self._apply_api_key()
        self._apply_pdf_path()
        payload = self._last_status_payload or {}
        inventory = payload.get("verification_finding_rechecks") or {}
        summary = inventory.get("summary") or {}
        eligible_chunks = int(summary.get("eligible_chunk_count", 0) or 0)
        eligible_findings = int(summary.get("eligible_finding_count", 0) or 0)
        selection = self._selected_counterexample_recheck_token()
        session = payload.get("session") or {}
        model = self.controller.model or session.get("model")
        effort = self.controller.reasoning_effort or session.get("reasoning_effort")
        scope_text = (
            f"selected candidate {selection}"
            if selection
            else f"all {eligible_chunks} eligible chunk(s) / {eligible_findings} finding(s)"
        )
        message = (
            f"Recheck {scope_text}?\n\n"
            "The complete manuscript chunk, verification script, exact execution output, parsed counterexample, "
            "linked issues, labels, and compact neighboring context will be sent to the OpenAI API.\n\n"
            f"Model/reasoning: {model or 'saved session model'} / {effort or 'saved session effort'}\n"
            "This action incurs API cost. The result is provisional and will not erase the original deterministic finding."
        )
        choice = QMessageBox.question(
            self,
            "Recheck Counterexample Findings",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Yes:
            self.controller.log_message.emit("Counterexample finding recheck cancelled.")
            return
        self.controller.recheck_counterexample_findings(selection)

    def _on_status_updated(self, payload: dict[str, Any]) -> None:
        self._last_status_payload = payload
        status = payload.get("status") or {}
        usage = payload.get("usage") or {}
        totals = usage.get("totals") or {}
        discussion_usage = payload.get("discussion_usage") or {}
        pause = payload.get("pause") or {}
        failed_verification = payload.get("failed_verification") or {}
        verification_suite = payload.get("verification_suite") or {}
        verification_finding_rechecks = payload.get("verification_finding_rechecks") or {}
        status_cost = status.get("cost_usd", None)
        totals_cost = float(totals.get("cost_usd", 0.0) or 0.0)
        if status_cost is None:
            display_cost = totals_cost
        else:
            display_cost = float(status_cost or 0.0)
            if totals_cost > display_cost:
                display_cost = totals_cost

        _set_text_if_changed(self.status_value, self._display_status_text(payload, status, pause))
        _set_text_if_changed(self.current_chunk_value, str(status.get("current_chunk_id") or "-"))
        self.progress_bar.setValue(int(round(float(status.get("progress_pct", 0.0) or 0.0))))
        _set_text_if_changed(self.chunk_progress_value, f"{status.get('chunks_completed', 0)} / {status.get('chunks_total', 0)}")
        _set_text_if_changed(
            self.page_progress_value,
            f"{status.get('estimated_pages_completed', 0)} / {status.get('estimated_pages_total', 0)}"
        )
        _set_text_if_changed(self.cost_value, f"${display_cost:.4f}")
        _set_text_if_changed(self.tokens_value, str(int(totals.get("total_tokens", 0) or 0)))
        _set_text_if_changed(self.elapsed_value, self._format_duration(float(totals.get("audit_seconds", 0.0) or 0.0)))
        self._update_cache_reuse_status(status.get("last_chunk_usage_diagnostics") or {})
        _set_text_if_changed(self.discussion_cost_value, f"Cost: ${float(discussion_usage.get('cost_usd', 0.0) or 0.0):.4f}")
        _set_text_if_changed(self.discussion_tokens_value, f"Tokens: {int(discussion_usage.get('total_tokens', 0) or 0)}")
        _set_text_if_changed(self.discussion_turns_value, f"Turns: {int(discussion_usage.get('turns', 0) or 0)}")

        if pause.get("requested"):
            requested_at = str(pause.get("requested_at") or "").strip()
            _set_text_if_changed(self.pause_state_value, f"Requested{f' at {requested_at}' if requested_at else ''}")
        elif str(status.get("status")) == "paused":
            reason = str(status.get("pause_reason") or "paused").strip()
            _set_text_if_changed(self.pause_state_value, f"Paused ({reason})")
        else:
            _set_text_if_changed(self.pause_state_value, "Not requested")

        failed_summary = failed_verification.get("summary") or {}
        failed_count = int(failed_summary.get("failed_chunk_count", 0) or 0)
        failed_result_count = int(failed_summary.get("failed_result_count", 0) or 0)
        failed_chunk_ids = failed_verification.get("chunk_ids") or []
        if failed_count:
            preview = ", ".join(str(chunk_id) for chunk_id in failed_chunk_ids[:6])
            if failed_count > 6:
                preview += ", ..."
            _set_plain_text_preserving_scroll(
                self.failed_verification_summary_value,
                f"{failed_count} chunk(s), {failed_result_count} result(s): {preview}"
            )
        else:
            _set_plain_text_preserving_scroll(
                self.failed_verification_summary_value,
                "No failed verification results",
            )
        self._set_failed_verification_rerun_compact(failed_count)

        scripts_total = int(verification_suite.get("scripts_total", 0) or 0)
        _set_text_if_changed(self.verification_scripts_available_value, f"Currently active verification scripts: {scripts_total}")
        last_run = verification_suite.get("last_run")
        if isinstance(last_run, dict) and last_run:
            execution = last_run.get("execution_summary") or {}
            outcomes = last_run.get("mathematical_outcome_summary") or {}
            _set_text_if_changed(
                self.verification_last_run_value,
                "Last run (active scripts): execution "
                f"completed {int(execution.get('completed', 0) or 0)}, "
                f"errors {int(execution.get('runtime_error', 0) or 0) + int(execution.get('parse_error', 0) or 0)}, "
                f"timed out {int(execution.get('timeout', 0) or 0)}; outcomes "
                f"counterexamples {int(outcomes.get('counterexample_found', 0) or 0)}, "
                f"claim failures {int(outcomes.get('claim_failed', 0) or 0)}, "
                f"not reported {int(outcomes.get('not_reported', 0) or 0)}"
            )
        else:
            _set_text_if_changed(self.verification_last_run_value, "Last run: none")
        recheck_history_summary = verification_suite.get("verification_finding_recheck_summary") or {}
        self._update_counterexample_recheck_inventory(
            verification_finding_rechecks,
            recheck_history_summary,
        )
        self._update_verification_replacement_inventory(
            verification_suite.get("verification_replacement_checks") or {}
        )
        self._update_verification_technical_repairs(
            verification_suite.get("verification_technical_repair_candidates") or {},
            verification_suite.get("verification_technical_repairs") or {},
        )
        inventory_warning = verification_suite.get("inventory_warning") or {}
        if inventory_warning.get("has_invalidated_obligations"):
            affected_chunks = inventory_warning.get("affected_chunks") or []
            preview = ", ".join(str(chunk_id) for chunk_id in affected_chunks[:6])
            if len(affected_chunks) > 6:
                preview += ", ..."
            message = str(inventory_warning.get("message") or "").strip()
            fallback = (
                f"{int(inventory_warning.get('invalidated_script_count', 0) or 0)} archived/invalidated "
                "verification script(s) are not represented in the currently active verification suite. "
                f"Affected chunks: {preview or 'unknown'}."
            )
            _set_text_if_changed(self.verification_inventory_warning_value, "Warning: " + (message or fallback))
        else:
            _set_text_if_changed(self.verification_inventory_warning_value, "")

        self._update_report_freshness_labels(payload.get("report_freshness") or {})
        self._update_review_summary(payload.get("review_summary") or {})
        self._apply_button_states()

    def _update_cache_reuse_status(self, diagnostics: dict[str, Any]) -> None:
        if not isinstance(diagnostics, dict) or not diagnostics:
            _set_text_if_changed(self.cache_reuse_value, "-")
            _set_tooltip_if_changed(self.cache_reuse_value, "")
            _set_stylesheet_if_changed(self.cache_reuse_value, "")
            return
        input_tokens = int(diagnostics.get("input_tokens", 0) or 0)
        if input_tokens <= 0:
            _set_text_if_changed(self.cache_reuse_value, "n/a")
            _set_tooltip_if_changed(self.cache_reuse_value, "")
            _set_stylesheet_if_changed(self.cache_reuse_value, "")
            return
        cached_tokens = int(diagnostics.get("cached_input_tokens", 0) or 0)
        percent = diagnostics.get("cached_input_percent")
        if percent is None:
            percent = float(diagnostics.get("cached_input_ratio", 0.0) or 0.0) * 100.0
        chunk_id = str(diagnostics.get("chunk_id") or "last chunk")
        warning = str(diagnostics.get("warning") or "").strip()
        if warning:
            _set_text_if_changed(
                self.cache_reuse_value,
                f"{chunk_id}: {float(percent):.1f}% cached - cost may be higher"
            )
            _set_tooltip_if_changed(
                self.cache_reuse_value,
                f"{warning}\nInput tokens: {input_tokens:,}\nCached input tokens: {cached_tokens:,}"
            )
            _set_stylesheet_if_changed(self.cache_reuse_value, "color: #b06000; font-weight: 600;")
        else:
            _set_text_if_changed(
                self.cache_reuse_value,
                f"{chunk_id}: {float(percent):.1f}% cached ({cached_tokens:,}/{input_tokens:,})"
            )
            _set_tooltip_if_changed(self.cache_reuse_value, "")
            _set_stylesheet_if_changed(self.cache_reuse_value, "")

    def _update_report_freshness_labels(self, freshness: dict[str, Any]) -> None:
        reports = freshness.get("reports") if isinstance(freshness, dict) else {}
        if not isinstance(reports, dict):
            reports = {}
        self._set_report_freshness_label(
            self.full_report_freshness_value,
            "Full Report",
            reports.get("full") if isinstance(reports.get("full"), dict) else {},
        )
        self._set_report_freshness_label(
            self.concise_report_freshness_value,
            "Concise Report",
            reports.get("concise") if isinstance(reports.get("concise"), dict) else {},
        )
        self._set_report_freshness_label(
            self.verification_report_freshness_value,
            "Verification Report",
            reports.get("verification") if isinstance(reports.get("verification"), dict) else {},
        )

    def _update_review_summary(self, summary: dict[str, Any]) -> None:
        if not hasattr(self, "review_status_value"):
            return
        if isinstance(summary, dict):
            self._last_status_payload.setdefault("review_summary", summary)
            self._last_status_payload["review_summary"] = summary
        if not isinstance(summary, dict) or not summary.get("available"):
            message = str((summary or {}).get("message") or (summary or {}).get("error") or "No audit review state available.")
            _set_text_if_changed(self.review_status_value, message)
            _set_plain_text_preserving_scroll(
                self.review_accepted_rechecks_value,
                "No accepted issue-family rechecks found.",
            )
            _set_plain_text_preserving_scroll(
                self.review_candidate_summary_value,
                "No candidate inventory prepared yet.",
            )
            _set_plain_text_preserving_scroll(
                self.review_family_summary_value,
                "No issue-family summary prepared yet.",
            )
            self._refresh_review_open_buttons()
            self._update_review_family_selector({})
            return

        issues = summary.get("issue_inventory") or {}
        accepted = summary.get("accepted_rechecks") or {}
        candidates = summary.get("candidate_inventory") or {}
        families = summary.get("issue_families") or {}
        stale_reports = summary.get("reports_stale_due_to_issue_rechecks") or []
        status_lines = [
            f"Open issues: {int(issues.get('open', 0) or 0)} "
            f"({int(issues.get('high_or_critical_open', 0) or 0)} high/critical)",
            f"Accepted issue-family rechecks: {int(accepted.get('accepted_recheck_count', 0) or 0)}",
            f"Prepared review candidates: {int(candidates.get('candidate_count', 0) or 0)}",
            f"Prepared issue families: {int(families.get('total_families', 0) or 0)}",
        ]
        if stale_reports:
            status_lines.append("Reports stale due to issue rechecks: " + ", ".join(str(item) for item in stale_reports))
        warnings = summary.get("warnings") or []
        if warnings:
            status_lines.append("Warnings: " + "; ".join(str(item) for item in warnings[:3]))
        _set_text_if_changed(self.review_status_value, " | ".join(status_lines))
        _set_plain_text_preserving_scroll(
            self.review_accepted_rechecks_value,
            self._format_review_rechecks(accepted),
        )
        _set_plain_text_preserving_scroll(
            self.review_candidate_summary_value,
            self._format_review_candidates(candidates),
        )
        _set_plain_text_preserving_scroll(
            self.review_family_summary_value,
            self._format_review_families(families),
        )
        self._update_review_family_selector(summary)
        self._refresh_review_open_buttons()

    def _current_review_families(self) -> list[dict[str, Any]]:
        summary = (self._last_status_payload or {}).get("review_summary") or {}
        issue_families = summary.get("issue_families") if isinstance(summary, dict) else {}
        families = issue_families.get("families") if isinstance(issue_families, dict) else []
        return [family for family in families if isinstance(family, dict)]

    def _selected_review_family_id(self) -> str:
        if not hasattr(self, "review_family_combo"):
            return ""
        data = self.review_family_combo.currentData()
        return str(data or "").strip()

    def _selected_review_family(self) -> dict[str, Any]:
        family_id = self._selected_review_family_id()
        if not family_id:
            return {}
        for family in self._current_review_families():
            if str(family.get("family_id") or "").strip() == family_id:
                return family
        return {}

    def _update_review_family_selector(self, summary: dict[str, Any]) -> None:
        if not hasattr(self, "review_family_combo"):
            return
        issue_families = summary.get("issue_families") if isinstance(summary, dict) else {}
        families = issue_families.get("families") if isinstance(issue_families, dict) else []
        families = [family for family in families if isinstance(family, dict) and str(family.get("family_id") or "").strip()]
        family_ids = [str(family.get("family_id") or "").strip() for family in families]
        previous = self._selected_review_family_id()
        if family_ids != self._review_family_ids:
            self.review_family_combo.blockSignals(True)
            self.review_family_combo.clear()
            for family in families:
                family_id = str(family.get("family_id") or "").strip()
                title = str(family.get("title") or "").strip()
                self.review_family_combo.addItem(f"{family_id}: {title}" if title else family_id, family_id)
            self.review_family_combo.blockSignals(False)
            self._review_family_ids = family_ids
        if family_ids:
            target = previous if previous in family_ids else family_ids[0]
            index = self.review_family_combo.findData(target)
            if index >= 0 and self.review_family_combo.currentIndex() != index:
                self.review_family_combo.setCurrentIndex(index)
            self.review_family_combo.setEnabled(True)
        else:
            self.review_family_combo.blockSignals(True)
            self.review_family_combo.clear()
            self.review_family_combo.addItem("No issue families prepared", "")
            self.review_family_combo.blockSignals(False)
            self._review_family_ids = []
            self.review_family_combo.setEnabled(False)
        self._update_review_family_details()

    def _on_review_family_selected(self, _index: int) -> None:
        self._update_review_family_details()
        self._refresh_review_open_buttons()

    def _update_review_family_details(self) -> None:
        if not hasattr(self, "review_family_details_value"):
            return
        _set_plain_text_preserving_scroll(
            self.review_family_details_value,
            self._format_review_family_details(self._selected_review_family()),
        )

    def _format_review_family_details(self, family: dict[str, Any]) -> str:
        if not family:
            return "No issue family selected. Click Prepare Review Summary to generate family sidecars."
        chunks = family.get("chunks") or []
        chunk_ids = [
            str(item.get("chunk_id") or "").strip()
            for item in chunks
            if isinstance(item, dict) and str(item.get("chunk_id") or "").strip()
        ]
        lines = [
            f"Family id: {family.get('family_id') or ''}",
            f"Title: {family.get('title') or ''}",
            f"Priority: {family.get('priority') or 'unknown'}",
            f"Recommended action: {family.get('recommended_action') or 'unknown'}",
            f"Accepted recheck exists: {'yes' if family.get('accepted_recheck_exists') else 'no'}",
            f"Upstream issues: {', '.join(family.get('primary_upstream_issue_ids') or []) or 'none'}",
            f"Downstream issues: {', '.join(family.get('downstream_issue_ids') or []) or 'none'}",
            f"Related issues: {', '.join(family.get('related_issue_ids') or []) or 'none'}",
            f"Source chunks: {', '.join(chunk_ids) or 'none'}",
            f"Main references: {', '.join(family.get('main_references') or []) or 'none'}",
            f"Main symbols: {', '.join(family.get('main_symbols') or []) or 'none'}",
        ]
        note = str(family.get("review_notes") or "").strip()
        if note:
            lines.extend(["", "Review note:", note])
        output_dir = str(family.get("dry_run_output_dir") or "").strip()
        if output_dir:
            lines.extend(["", f"Dry-run folder: {output_dir}"])
        live_pattern = str(family.get("live_output_dir_pattern") or "").strip()
        if live_pattern:
            lines.append(f"Live output folder pattern: {live_pattern}")
        latest_dir = str(family.get("latest_recheck_output_dir") or "").strip()
        if latest_dir and latest_dir != output_dir:
            lines.append(f"Latest recheck folder: {latest_dir}")
        return "\n".join(lines)

    def _format_review_rechecks(self, accepted: dict[str, Any]) -> str:
        if not isinstance(accepted, dict) or not accepted.get("available"):
            return "No accepted issue-family rechecks found."
        lines = [
            f"Accepted rechecks: {int(accepted.get('accepted_recheck_count', 0) or 0)}",
            f"Families: {int(accepted.get('family_count', 0) or 0)}",
            f"Downstream-covered issues: {int(accepted.get('downstream_covered_issue_count', 0) or 0)}",
            f"Human-review issue flags: {int(accepted.get('human_review_issue_count', 0) or 0)}",
        ]
        for family in accepted.get("families") or []:
            if not isinstance(family, dict):
                continue
            lines.append("")
            lines.append(f"{family.get('family_id') or '(family)'}")
            upstream = ", ".join(family.get("upstream_issue_ids") or []) or "none"
            downstream = ", ".join(family.get("downstream_issue_ids") or []) or "none"
            lines.append(f"- Upstream: {upstream}")
            lines.append(f"- Downstream-covered: {downstream}")
            if family.get("needs_human_review"):
                lines.append("- Needs human review: yes")
            treatment = str(family.get("final_report_treatment") or "").strip()
            if treatment:
                lines.append(f"- Final-report treatment: {treatment}")
            note = str(family.get("summary") or "").strip()
            if note:
                lines.append(f"- Summary: {note[:500]}")
        return "\n".join(lines)

    def _format_review_candidates(self, candidates: dict[str, Any]) -> str:
        if not isinstance(candidates, dict) or not candidates.get("available"):
            return "No candidate inventory prepared yet. Click Prepare Review Summary to create review/rerun_recheck_candidates.json."
        type_summary = candidates.get("candidate_type_summary") or {}
        action_counts = candidates.get("recommended_action_kind_counts") or {}
        lines = [
            f"Prepared at: {candidates.get('generated_at') or 'unknown'}",
            f"Total candidates: {int(candidates.get('candidate_count', 0) or 0)}",
            f"Dependency groups: {int(candidates.get('group_count', 0) or 0)}",
            "",
            "Candidate type summary",
            f"- Full chunk rerun candidates: {int(type_summary.get('full_chunk_rerun_candidates', 0) or 0)}",
            f"- Issue-level recheck candidates: {int(type_summary.get('issue_level_recheck_candidates', 0) or 0)}",
            f"- Dependency grouping candidates: {int(type_summary.get('dependency_grouping_candidates', 0) or 0)}",
            f"- Verification script/claim recheck candidates: {int(type_summary.get('verification_script_claim_recheck_candidates', 0) or 0)}",
            f"- Technical recovery candidates: {int(type_summary.get('technical_recovery_candidates', 0) or 0)}",
            f"- Notation/regime clarification candidates: {int(type_summary.get('notation_regime_clarification_candidates', 0) or 0)}",
            "",
            "Recommended action counts",
        ]
        for key in ("issue_recheck", "dependency_group_review", "script_recheck", "chunk_rerun", "technical_retry", "human_review"):
            lines.append(f"- {key}: {int(action_counts.get(key, 0) or 0)}")
        return "\n".join(lines)

    def _format_review_families(self, families: dict[str, Any]) -> str:
        if not isinstance(families, dict) or not families.get("available"):
            return "No issue-family summary prepared yet. Click Prepare Review Summary to create review/issue_recheck_families.json."
        lines = [
            f"Families: {int(families.get('total_families', 0) or 0)}",
            f"Issue IDs covered: {int(families.get('issue_ids_covered', 0) or 0)}",
            f"High/critical unassigned: {len(families.get('high_critical_unassigned') or [])}",
        ]
        overlaps = families.get("issues_appearing_in_multiple_families") or []
        if overlaps:
            issue_ids = []
            for item in overlaps[:8]:
                if isinstance(item, dict):
                    issue_ids.append(str(item.get("issue_id") or ""))
                else:
                    issue_ids.append(str(item))
            lines.append("Overlaps needing review: " + ", ".join(item for item in issue_ids if item))
        for family in families.get("families") or []:
            if not isinstance(family, dict):
                continue
            lines.append("")
            lines.append(f"{family.get('family_id')}: {family.get('title')}")
            lines.append(f"- Priority: {family.get('priority') or 'unknown'}")
            lines.append(f"- Recommended action: {family.get('recommended_action') or 'unknown'}")
            upstream = ", ".join(family.get("primary_upstream_issue_ids") or []) or "none"
            downstream = ", ".join(family.get("downstream_issue_ids") or []) or "none"
            references = ", ".join(family.get("main_references") or []) or "none"
            lines.append(f"- Upstream: {upstream}")
            lines.append(f"- Downstream: {downstream}")
            lines.append(f"- Main references: {references}")
        return "\n".join(lines)

    def _set_report_freshness_label(self, label: QLabel, report_label: str, info: dict[str, Any]) -> None:
        status = str((info or {}).get("status") or "unknown").strip().lower()
        latest_source = (info or {}).get("latest_source") or {}
        source_name = str(latest_source.get("name") or "").strip() if isinstance(latest_source, dict) else ""
        if status == "current":
            text = f"{report_label}: current"
            style = "color: #137333; font-weight: 600;"
        elif status == "stale":
            suffix = f" (latest: {source_name})" if source_name else ""
            text = f"{report_label}: stale - rebuild recommended{suffix}"
            style = "color: #b06000; font-weight: 600;"
        elif status == "missing":
            text = f"{report_label}: missing"
            style = "color: #666;"
        else:
            text = f"{report_label}: freshness unknown"
            style = "color: #666;"
        _set_text_if_changed(label, text)
        _set_stylesheet_if_changed(label, style)

    def _on_verification_progress(self, payload: dict[str, Any]) -> None:
        event = str((payload or {}).get("event") or "").strip()
        total = int((payload or {}).get("total", (payload or {}).get("scripts_total", 0)) or 0)
        index = int((payload or {}).get("index", 0) or 0)
        script_name = str((payload or {}).get("script_name") or "").strip()
        if event == "suite_started":
            message = f"Verification scripts available: {total}"
            _set_text_if_changed(self.verification_live_progress_value, f"Running verification 0 / {total}")
            self._append_report_output(message)
            return
        if event == "script_started":
            message = f"Running verification {index}/{total}: {script_name}"
            _set_text_if_changed(self.verification_live_progress_value, message)
            self._append_report_output(message)
            return
        if event == "script_finished":
            execution_status = str((payload or {}).get("execution_status") or "not_run").lower()
            outcome = str((payload or {}).get("mathematical_outcome") or "not_reported").lower()
            if outcome == "counterexample_found":
                label = "COUNTEREXAMPLE FOUND"
            elif outcome == "claim_failed":
                label = "CLAIM FAILED"
            elif execution_status != "completed":
                label = execution_status.replace("_", " ").upper()
            else:
                label = outcome.replace("_", " ").upper()
            message = f"{label}: {script_name}"
            _set_text_if_changed(self.verification_live_progress_value, f"Verification {index} / {total}: {label} {script_name}")
            self._append_report_output(message)

    def _on_task_running_changed(self, running: bool) -> None:
        self._task_running = bool(running)
        self._apply_button_states()
        if not self._task_running and self._close_after_task:
            self._close_after_task = False
            self.close()

    def _apply_button_states(self) -> None:
        payload = self._last_status_payload or {}
        status = payload.get("status") or {}
        pause = payload.get("pause") or {}
        session = payload.get("session") or {}
        pending = session.get("pending") or {}
        failed_verification = payload.get("failed_verification") or {}
        session_available = bool(payload.get("session_available"))
        pdf_selected = bool(self.pdf_path_input.text().strip())
        status_name = str(status.get("status") or "")
        running_status = status_name == "running"
        pause_requested = bool(pause.get("requested"))
        pending_response = bool(pending.get("response_id"))
        active_audit_task = (
            self._task_running
            and self.controller.active_task_name() in {"Start Fresh Audit", "Resume Audit"}
        )
        cancel_in_progress = self.controller.cancel_current_chunk_in_progress()
        failed_count = int((failed_verification.get("summary") or {}).get("failed_chunk_count", 0) or 0)
        resumable_statuses = {"paused", "initialized"}

        self.start_button.setEnabled(pdf_selected and not self._task_running and not running_status)
        self.resume_button.setEnabled(session_available and not self._task_running and status_name in resumable_statuses)
        self.pause_button.setEnabled(running_status and not pause_requested)
        _set_text_if_changed(self.pause_button, "Pause Requested" if pause_requested and running_status else "Pause Audit")
        self.cancel_current_button.setEnabled(
            session_available
            and pending_response
            and not cancel_in_progress
            and (not self._task_running or active_audit_task)
        )

        session_ready = session_available and not self._task_running and not running_status
        self.rebuild_final_button.setEnabled(session_ready)
        self.build_concise_button.setEnabled(session_ready)
        self.run_verification_button.setEnabled(session_ready)
        self.rebuild_verification_button.setEnabled(session_ready)
        self.refresh_review_summary_button.setEnabled(session_available and not self._task_running)
        self.prepare_review_summary_button.setEnabled(session_ready)
        self.prepare_family_recheck_dry_run_button.setEnabled(session_ready and bool(self._selected_review_family_id()))
        self.run_live_family_recheck_button.setEnabled(
            session_ready and bool(self._selected_review_family_id()) and self.controller.live_api_key_available()
        )
        self.import_accepted_recheck_button.setEnabled(session_ready)
        self.rerun_selected_button.setEnabled(session_ready and not pending_response)
        self.select_rerun_chunks_button.setEnabled(session_ready and bool(self._available_rerun_chunks()))
        self._refresh_report_open_buttons()
        self._refresh_review_open_buttons()
        self.export_chatgpt_context_pack_button.setEnabled(session_ready)
        self._refresh_chatgpt_export_buttons()
        self.rerun_failed_verification_button.setEnabled(
            session_available
            and not self._task_running
            and status_name in {"completed", "paused"}
            and not pending_response
            and failed_count > 0
        )
        recheck_entries = [
            {
                "label": self.counterexample_recheck_combo.itemText(index),
                "selection": self.counterexample_recheck_combo.itemData(index),
            }
            for index in range(self.counterexample_recheck_combo.count())
        ]
        recheck_action = _counterexample_recheck_action_state(
            session_available=session_available,
            status_name=status_name,
            pending_response=pending_response,
            task_running=self._task_running,
            api_key_available=self.controller.live_api_key_available(),
            entries=recheck_entries,
            selected_token=self.counterexample_recheck_combo.currentData(),
        )
        self.recheck_counterexample_button.setEnabled(bool(recheck_action["enabled"]))
        _set_text_if_changed(self.recheck_counterexample_button, str(recheck_action["button_text"]))
        _set_text_if_changed(self.counterexample_recheck_status_value, str(recheck_action["reason"]))
        _set_stylesheet_if_changed(
            self.counterexample_recheck_status_value,
            "color: #2f6b3a; font-weight: 600;"
            if recheck_action["enabled"]
            else "color: #8a4b00; font-weight: 600;",
        )
        replacement_selection = self._selected_verification_replacement()
        replacement_available = bool(replacement_selection.get("recheck_id"))
        replacement_ready = bool(replacement_selection.get("ready"))
        self.review_verification_replacement_button.setEnabled(
            replacement_available and not self._task_running
        )
        can_run_replacement = (
            replacement_available
            and replacement_ready
            and session_available
            and not self._task_running
            and status_name in {"completed", "paused"}
            and not pending_response
        )
        self.run_verification_replacement_button.setEnabled(can_run_replacement)
        if not replacement_available:
            replacement_reason = "No runnable replacement checks."
        elif self._task_running:
            replacement_reason = "Another audit action is currently running."
        elif not replacement_ready:
            replacement_reason = "Selected replacement failed validation and cannot be executed."
        elif status_name not in {"completed", "paused"} or pending_response:
            replacement_reason = "Replacement checks require a completed or paused audit with no pending response."
        else:
            replacement_reason = "Ready for confirmed local safe-mode execution; no API call is made."
        _set_text_if_changed(self.verification_replacement_status_value, replacement_reason)
        _set_stylesheet_if_changed(
            self.verification_replacement_status_value,
            "color: #2f6b3a; font-weight: 600;"
            if can_run_replacement
            else "color: #8a4b00; font-weight: 600;",
        )
        technical_candidate_available = self.verification_technical_repair_combo.count() > 0
        can_generate_technical = (
            technical_candidate_available and session_available and not self._task_running
            and status_name in {"completed", "paused"} and not pending_response
            and self.controller.live_api_key_available()
        )
        self.generate_verification_technical_repair_button.setEnabled(can_generate_technical)
        technical_replacement = self._selected_verification_technical_replacement()
        technical_replacement_available = bool(technical_replacement.get("repair_id"))
        technical_replacement_ready = bool(technical_replacement.get("ready"))
        self.review_verification_technical_replacement_button.setEnabled(
            technical_replacement_available and not self._task_running
        )
        can_run_technical = (
            technical_replacement_available and technical_replacement_ready and session_available
            and not self._task_running and status_name in {"completed", "paused"} and not pending_response
        )
        self.run_verification_technical_replacement_button.setEnabled(can_run_technical)
        if can_generate_technical:
            technical_reason = "Ready to generate evidence-rich repair scripts; this API action may incur cost."
        elif can_run_technical:
            technical_reason = "Ready for confirmed local safe-mode execution; no API call is made."
        elif self._task_running:
            technical_reason = "Another audit action is currently running."
        elif not technical_candidate_available and not technical_replacement_available:
            technical_reason = "No active technical repair candidate or replacement is available."
        elif technical_replacement_available and not technical_replacement_ready:
            technical_reason = "Selected replacement failed validation and cannot be executed."
        elif not self.controller.live_api_key_available() and technical_candidate_available:
            technical_reason = "API key required to generate repair scripts."
        else:
            technical_reason = "Technical repair requires a completed or paused audit with no pending response."
        _set_text_if_changed(self.verification_technical_repair_status, technical_reason)
        self.ask_button.setEnabled(session_ready)
        self.new_discussion_thread_button.setEnabled(session_ready)
        self.discussion_thread_combo.setEnabled(session_ready and self.discussion_thread_combo.count() > 0)

    def _append_log(self, message: str) -> None:
        if not message:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_output.appendPlainText(f"[{stamp}] {message}")

    def _append_report_output(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        if self.report_output.toPlainText().strip():
            self.report_output.appendPlainText("")
        self.report_output.appendPlainText(text)
        self._scroll_report_output_to_bottom()
        QTimer.singleShot(0, self._scroll_report_output_to_bottom)

    def _scroll_report_output_to_bottom(self) -> None:
        if not hasattr(self, "report_output"):
            return
        scrollbar = self.report_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _selected_report_paths(self) -> dict[str, Path]:
        session = (self._last_status_payload or {}).get("session") or {}
        workdir = str(session.get("workdir") or "").strip()
        pdf_path = str(session.get("pdf_path") or self.pdf_path_input.text() or "").strip()
        if not workdir and pdf_path:
            workdir = str(Path(pdf_path).with_name(Path(pdf_path).stem + "_audit"))
        if not workdir or not pdf_path:
            return {}
        reports_dir = Path(workdir) / "reports"
        stem = Path(pdf_path).stem
        return {
            "folder": reports_dir,
            "full": reports_dir / f"{stem}_audit_report.tex",
            "concise": reports_dir / f"{stem}_concise_audit_report.tex",
            "verification": reports_dir / f"{stem}_verification_report.tex",
        }

    def _selected_review_paths(self) -> dict[str, Path]:
        summary = (self._last_status_payload or {}).get("review_summary") or {}
        paths = summary.get("paths") if isinstance(summary, dict) else {}
        if isinstance(paths, dict) and paths:
            return {
                key: Path(value)
                for key, value in paths.items()
                if isinstance(value, str) and value.strip()
            }
        session = (self._last_status_payload or {}).get("session") or {}
        workdir = str(session.get("workdir") or "").strip()
        if not workdir:
            return {}
        root = Path(workdir)
        review_dir = root / "review"
        return {
            "review_dir": review_dir,
            "family_rechecks_dir": review_dir / "family_rechecks",
            "candidate_json": review_dir / "rerun_recheck_candidates.json",
            "candidate_markdown": review_dir / "rerun_recheck_candidates.md",
            "families_json": review_dir / "issue_recheck_families.json",
            "families_markdown": review_dir / "issue_recheck_families.md",
            "issue_rechecks_json": root / "state" / "issue_rechecks.json",
        }

    def _refresh_report_open_buttons(self) -> None:
        if not hasattr(self, "open_full_report_button"):
            return
        paths = self._selected_report_paths()
        self.open_full_report_button.setEnabled(bool(paths.get("full") and paths["full"].is_file()))
        self.open_concise_report_button.setEnabled(bool(paths.get("concise") and paths["concise"].is_file()))
        self.open_verification_report_button.setEnabled(
            bool(paths.get("verification") and paths["verification"].is_file())
        )
        self.open_reports_folder_button.setEnabled(bool(paths.get("folder") and paths["folder"].is_dir()))

    def _refresh_review_open_buttons(self) -> None:
        if not hasattr(self, "open_review_folder_button"):
            return
        paths = self._selected_review_paths()
        family = self._selected_review_family()
        self.open_review_folder_button.setEnabled(bool(paths.get("review_dir") and paths["review_dir"].is_dir()))
        self.open_issue_rechecks_button.setEnabled(
            bool(paths.get("issue_rechecks_json") and paths["issue_rechecks_json"].is_file())
        )
        self.open_family_recheck_folder_button.setEnabled(
            bool(self._selected_family_recheck_path("latest_recheck_output_dir", "dry_run_output_dir", directory=True))
        )
        self.open_family_recheck_prompt_button.setEnabled(
            bool(self._selected_family_recheck_path("latest_recheck_prompt_path", "dry_run_prompt_path", require_exists=True))
        )
        self.open_family_recheck_evidence_button.setEnabled(
            bool(self._selected_family_recheck_path("latest_recheck_evidence_path", "dry_run_evidence_path", require_exists=True))
        )
        self.open_family_recheck_result_button.setEnabled(
            bool(self._selected_family_recheck_path("latest_recheck_result_path", require_exists=True))
        )

    def _open_path_in_default_app(self, path: Path, description: str) -> None:
        if not path.exists():
            message = f"Cannot open {description}; path no longer exists: {path}"
            self._append_log(message)
            self._append_report_output(message)
            self._refresh_report_open_buttons()
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            message = f"Could not open {description} with the system default application: {path}"
            self._append_log(message)
            self._append_report_output(message)

    def _open_review_folder(self) -> None:
        paths = self._selected_review_paths()
        folder = paths.get("review_dir")
        if folder is None:
            self._append_log("No selected audit session is available for opening the review folder.")
            return
        if not folder.is_dir():
            message = "Review folder has not been created yet. Click Prepare Review Summary first."
            self._append_log(message)
            self._append_report_output(message)
            self._refresh_review_open_buttons()
            return
        self._open_path_in_default_app(folder, "review files folder")

    def _open_issue_rechecks_sidecar(self) -> None:
        paths = self._selected_review_paths()
        path = paths.get("issue_rechecks_json")
        if path is None:
            self._append_log("No selected audit session is available for opening issue_rechecks.json.")
            return
        if not path.is_file():
            message = "No accepted issue-family recheck sidecar exists yet."
            self._append_log(message)
            self._append_report_output(message)
            self._refresh_review_open_buttons()
            return
        self._open_path_in_default_app(path, "issue rechecks sidecar")

    def _prepare_selected_family_recheck_dry_run(self) -> None:
        family_id = self._selected_review_family_id()
        if not family_id:
            self._append_log("Select an issue family before preparing a recheck dry run.")
            return
        self.controller.prepare_selected_family_recheck_dry_run(family_id)

    def _review_family_prompt_size_text(self, family: dict[str, Any]) -> str:
        for label, key in (
            ("latest prompt artifact", "latest_recheck_prompt_path"),
            ("dry-run prompt artifact", "dry_run_prompt_path"),
        ):
            path_text = str(family.get(key) or "").strip()
            if not path_text:
                continue
            path = Path(path_text)
            if not path.is_file():
                continue
            try:
                chars = len(path.read_text(encoding="utf-8"))
            except Exception:
                return f"available from {label}, but could not read size"
            return f"{chars:,} chars from {label}"
        return "not available yet; prepare a dry run first for an exact prompt size"

    def _run_live_family_recheck(self) -> None:
        self._apply_api_key()
        family = self._selected_review_family()
        family_id = str(family.get("family_id") or "").strip()
        if not family_id:
            self._append_log("Select an issue family before running a live recheck.")
            return
        if not self.controller.live_api_key_available():
            self._append_log("Enter an API key before running a live family recheck.")
            return
        session = (self._last_status_payload or {}).get("session") or {}
        model = str(session.get("model") or self.controller.model or "unknown")
        effort = str(session.get("reasoning_effort") or self.controller.reasoning_effort or "unknown")
        upstream = ", ".join(family.get("primary_upstream_issue_ids") or []) or "none"
        downstream = ", ".join(family.get("downstream_issue_ids") or []) or "none"
        live_folder = str(family.get("live_output_dir_pattern") or "review/family_rechecks/<family>_live_<timestamp>")
        prompt_size = self._review_family_prompt_size_text(family)
        message = (
            "Run a live issue-family recheck?\n\n"
            "This will call the OpenAI API and may incur cost.\n\n"
            f"Family: {family_id} - {family.get('title') or ''}\n"
            f"Upstream issues: {upstream}\n"
            f"Downstream issues: {downstream}\n"
            f"Prompt size estimate: {prompt_size}\n"
            f"Model: {model}\n"
            f"Reasoning effort: {effort}\n"
            f"Output folder: {live_folder}\n\n"
            "Safety boundaries:\n"
            "- Rechecks only this selected issue family.\n"
            "- Does not modify state/issues.json.\n"
            "- Does not rerun chunks.\n"
            "- Does not run verification.\n"
            "- Does not automatically import or accept the result."
        )
        reply = QMessageBox.question(
            self,
            "Run Live Family Recheck",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._append_log("Live issue-family recheck cancelled.")
            return
        self.controller.run_live_family_recheck(family_id)

    def _selected_family_recheck_path(
        self,
        *keys: str,
        require_exists: bool = False,
        directory: bool = False,
    ) -> Optional[Path]:
        family = self._selected_review_family()
        if not isinstance(family, dict):
            return None
        for key in keys:
            value = family.get(key)
            if not value:
                continue
            path = Path(str(value))
            if directory:
                if path.is_dir():
                    return path
                continue
            if require_exists:
                if path.is_file():
                    return path
                continue
            return path
        return None

    def _open_family_recheck_folder(self) -> None:
        path = self._selected_family_recheck_path("latest_recheck_output_dir", "dry_run_output_dir")
        if path is None:
            self._append_log("Select an issue family before opening its dry-run folder.")
            return
        if not path.is_dir():
            message = "Family recheck folder does not exist yet. Prepare a dry run or run a live recheck first."
            self._append_log(message)
            self._append_report_output(message)
            self._refresh_review_open_buttons()
            return
        self._open_path_in_default_app(path, "family recheck folder")

    def _open_family_recheck_prompt(self) -> None:
        path = self._selected_family_recheck_path("latest_recheck_prompt_path", "dry_run_prompt_path")
        if path is None:
            self._append_log("Select an issue family before opening its dry-run prompt.")
            return
        if not path.is_file():
            message = "Family recheck prompt does not exist yet. Prepare a dry run or run a live recheck first."
            self._append_log(message)
            self._append_report_output(message)
            self._refresh_review_open_buttons()
            return
        self._open_path_in_default_app(path, "family recheck prompt")

    def _open_family_recheck_evidence(self) -> None:
        path = self._selected_family_recheck_path("latest_recheck_evidence_path", "dry_run_evidence_path")
        if path is None:
            self._append_log("Select an issue family before opening its dry-run evidence.")
            return
        if not path.is_file():
            message = "Family recheck evidence does not exist yet. Prepare a dry run or run a live recheck first."
            self._append_log(message)
            self._append_report_output(message)
            self._refresh_review_open_buttons()
            return
        self._open_path_in_default_app(path, "family recheck evidence")

    def _open_family_recheck_result(self) -> None:
        path = self._selected_family_recheck_path("latest_recheck_result_path")
        if path is None:
            self._append_log("Select an issue family before opening its live recheck result.")
            return
        if not path.is_file():
            message = "No live family recheck result exists for the selected family yet."
            self._append_log(message)
            self._append_report_output(message)
            self._refresh_review_open_buttons()
            return
        self._open_path_in_default_app(path, "family recheck result")

    def _import_accepted_recheck_result(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose accepted family_recheck_result.json",
            str(Path.home()),
            "JSON Files (*.json)",
        )
        if not path:
            return
        result_path = Path(path)
        reply = QMessageBox.question(
            self,
            "Import Accepted Recheck Result",
            "Import this accepted issue-family recheck result?\n\n"
            "This writes only state/issue_rechecks.json and logs/issue_recheck_decisions.jsonl. "
            "Canonical issue records are not modified.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._append_log("Issue-family recheck import cancelled.")
            return
        self.controller.import_accepted_recheck_result(str(result_path))

    def _open_report_tex(self, kind: str) -> None:
        paths = self._selected_report_paths()
        path = paths.get(kind)
        labels = {
            "full": "full report",
            "concise": "concise report",
            "verification": "verification report",
        }
        label = labels.get(kind, "report")
        if path is None:
            self._append_log(f"No selected audit session is available for opening the {label}.")
            return
        if not path.is_file():
            message = f"Cannot open {label}; .tex file does not exist: {path}"
            self._append_log(message)
            self._append_report_output(message)
            self._refresh_report_open_buttons()
            return
        self._open_path_in_default_app(path, label)

    def _open_reports_folder(self) -> None:
        paths = self._selected_report_paths()
        folder = paths.get("folder")
        if folder is None:
            self._append_log("No selected audit session is available for opening the reports folder.")
            return
        if not folder.is_dir():
            message = f"Cannot open reports folder; folder does not exist: {folder}"
            self._append_log(message)
            self._append_report_output(message)
            self._refresh_report_open_buttons()
            return
        self._open_path_in_default_app(folder, "reports folder")

    def _on_chatgpt_context_pack_exported(self, result: dict[str, Any]) -> None:
        self._latest_chatgpt_context_pack = dict(result or {})
        self._refresh_chatgpt_export_buttons()

    def _refresh_chatgpt_export_buttons(self) -> None:
        if not hasattr(self, "open_chatgpt_export_folder_button"):
            return
        export_folder_text = str(self._latest_chatgpt_context_pack.get("export_folder") or "").strip()
        starter_prompt_text = str(self._latest_chatgpt_context_pack.get("starter_prompt_text") or "").strip()
        self.open_chatgpt_export_folder_button.setEnabled(bool(export_folder_text and Path(export_folder_text).is_dir()))
        self.copy_chatgpt_starter_prompt_button.setEnabled(bool(starter_prompt_text))
        self.open_chatgpt_website_button.setEnabled(bool(self._latest_chatgpt_context_pack))

    def _open_chatgpt_export_folder(self) -> None:
        export_folder = Path(str(self._latest_chatgpt_context_pack.get("export_folder") or ""))
        if not export_folder.is_dir():
            message = f"Cannot open ChatGPT export folder; folder does not exist: {export_folder}"
            self._append_log(message)
            self._append_report_output(message)
            self._refresh_chatgpt_export_buttons()
            return
        self._open_path_in_default_app(export_folder, "ChatGPT export folder")

    def _copy_chatgpt_starter_prompt(self) -> None:
        starter_prompt_text = str(self._latest_chatgpt_context_pack.get("starter_prompt_text") or "").strip()
        if not starter_prompt_text:
            message = "Cannot copy starter prompt; no starter prompt is available from the latest export."
            self._append_log(message)
            self._append_report_output(message)
            self._refresh_chatgpt_export_buttons()
            return
        QApplication.clipboard().setText(starter_prompt_text)
        self._append_log("Starter prompt copied to clipboard.")

    def _open_chatgpt_website(self) -> None:
        if not QDesktopServices.openUrl(QUrl("https://chatgpt.com/")):
            message = "Could not open ChatGPT website with the system default browser."
            self._append_log(message)
            self._append_report_output(message)

    def _set_discussion_output(self, message: str) -> None:
        transcript_part, is_successful_turn = self._format_discussion_turn_for_transcript(str(message or ""))
        new_turn_index = len(self._discussion_transcript_parts)
        self._discussion_transcript_parts.append(transcript_part)
        self._refresh_raw_discussion_output(scroll_to_turn_index=new_turn_index)
        if is_successful_turn:
            self.question_input.clear()
        self._refresh_rendered_discussion_output(scroll_to_bottom=True)
        self._refresh_discussion_view(refresh_rendered=False)

    def _load_discussion_history(self, turns: list[dict[str, Any]]) -> None:
        self._discussion_transcript_parts = [
            self._format_saved_discussion_turn_for_transcript(turn)
            for turn in turns
            if isinstance(turn, dict)
        ]
        self._refresh_raw_discussion_output()
        self._refresh_rendered_discussion_output()
        self._refresh_discussion_view()

    def _discussion_transcript_text(self) -> str:
        return "\n\n---\n\n".join(self._discussion_transcript_parts)

    def _refresh_raw_discussion_output(self, scroll_to_turn_index: Optional[int] = None) -> None:
        transcript = self._discussion_transcript_text()
        _set_plain_text_preserving_scroll(self.answer_raw_output, transcript)
        if scroll_to_turn_index is None:
            return
        turn_index = max(0, min(int(scroll_to_turn_index), len(self._discussion_transcript_parts) - 1))
        separator = "\n\n---\n\n"
        if turn_index == 0:
            start_offset = 0
        else:
            start_offset = len(separator.join(self._discussion_transcript_parts[:turn_index])) + len(separator)
        cursor = self.answer_raw_output.textCursor()
        cursor.setPosition(min(start_offset, len(transcript)))
        self.answer_raw_output.setTextCursor(cursor)
        self.answer_raw_output.ensureCursorVisible()

    @staticmethod
    def _format_saved_discussion_turn_for_transcript(turn: dict[str, Any]) -> str:
        mode = str(turn.get("mode") or "discussion").strip()
        response_id = str(turn.get("response_id") or "n/a").strip()
        question = str(turn.get("question") or "").strip()
        answer = str(turn.get("answer") or "").strip() or "(empty answer)"
        return "\n".join(
            [
                f"Mode: {mode}",
                f"Response ID: {response_id}",
                "",
                "### Question",
                question,
                "",
                "### Answer",
                answer,
            ]
        ).strip()

    @staticmethod
    def _format_discussion_turn_for_transcript(message: str) -> tuple[str, bool]:
        text = str(message or "").strip()
        if not text:
            return "", False

        match = re.match(
            r"\AMode:\s*(?P<mode>[^\n]*)\n"
            r"Question:\s*(?P<question>.*?)\n"
            r"Response ID:\s*(?P<response_id>[^\n]*)\n"
            r"\s*\n"
            r"(?P<answer>.*)\Z",
            text,
            flags=re.DOTALL,
        )
        if not match:
            return text, False

        mode = match.group("mode").strip()
        question = match.group("question").strip()
        response_id = match.group("response_id").strip()
        answer = match.group("answer").strip() or "(empty answer)"
        return (
            "\n".join(
                [
                    f"Mode: {mode}",
                    f"Response ID: {response_id}",
                    "",
                    "### Question",
                    question,
                    "",
                    "### Answer",
                    answer,
                ]
            ).strip(),
            True,
        )

    def _refresh_rendered_discussion_output(self, scroll_to_bottom: bool = False) -> None:
        transcript = self._discussion_transcript_text()
        rendered_html = self._render_discussion_markdown_html(transcript)
        base_url = QUrl.fromLocalFile(str(Path(__file__).resolve().parent) + "/")
        html_unchanged = getattr(self, "_last_rendered_discussion_html", None) == rendered_html
        base_url_unchanged = getattr(self, "_last_rendered_discussion_base_url", None) == base_url.toString()
        if html_unchanged and base_url_unchanged:
            if scroll_to_bottom:
                QTimer.singleShot(0, self._scroll_rendered_discussion_to_bottom)
            return
        if scroll_to_bottom:
            def scroll_after_load(_ok: bool = False) -> None:
                try:
                    self.answer_rendered_output.loadFinished.disconnect(scroll_after_load)
                except TypeError:
                    pass
                QTimer.singleShot(250, self._scroll_rendered_discussion_to_bottom)
                QTimer.singleShot(900, self._scroll_rendered_discussion_to_bottom)

            self.answer_rendered_output.loadFinished.connect(scroll_after_load)
        self._last_rendered_discussion_html = rendered_html
        self._last_rendered_discussion_base_url = base_url.toString()
        self.answer_rendered_output.setHtml(rendered_html, base_url)

    def _scroll_rendered_discussion_to_bottom(self) -> None:
        self.answer_rendered_output.page().runJavaScript(
            "(function () {"
            "var scrollTarget = document.scrollingElement || document.documentElement || document.body;"
            "if (scrollTarget) { scrollTarget.scrollTop = scrollTarget.scrollHeight; }"
            "})();"
        )

    def _on_discussion_view_mode_changed(self, _mode: str) -> None:
        self._refresh_discussion_view()

    def _refresh_discussion_view(self, refresh_rendered: bool = True) -> None:
        if self.discussion_view_combo.currentText() == "Rendered":
            if refresh_rendered:
                self._refresh_rendered_discussion_output()
            self.answer_stack.setCurrentWidget(self.answer_rendered_output)
        else:
            self.answer_stack.setCurrentWidget(self.answer_raw_output)

    @staticmethod
    def _render_discussion_markdown_html(text: str) -> str:
        raw = str(text or "")
        assets_root = Path(__file__).resolve().parent / "gui_assets"
        mathjax_path = assets_root / "mathjax" / "es5" / "tex-mml-svg.js"
        mathjax_fonts_path = assets_root / "mathjax-fonts"
        mathjax_font_script_path = mathjax_fonts_path / "mathjax-newcm-font" / "svg" / "dynamic" / "script.js"
        missing_assets = [path for path in (mathjax_path, mathjax_font_script_path) if not path.exists()]
        if missing_assets:
            safe_paths = "".join(f"<li><code>{html.escape(str(path))}</code></li>" for path in missing_assets)
            return (
                "<html><head><style>"
                "body { font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif; "
                "font-size: 14px; line-height: 1.45; color: #202124; padding: 12px; }"
                ".notice { border: 1px solid #d8dee4; background: #fff8dc; border-radius: 8px; padding: 12px; }"
                "code { font-family: Menlo, Consolas, monospace; }"
                "</style></head><body>"
                "<div class=\"notice\">"
                "<strong>Rendered math view unavailable: local MathJax asset not found.</strong>"
                f"<p>Missing local asset(s):</p><ul>{safe_paths}</ul>"
                "<p>Use Raw mode for the exact answer text, or add the local MathJax bundle and font files at those paths.</p>"
                "</div></body></html>"
            )

        math_fragments: dict[str, tuple[str, str]] = {}

        def stash_math(kind: str, fragment: str) -> str:
            token = f"@@MATH{len(math_fragments)}@@"
            math_fragments[token] = (kind, fragment.strip())
            return token

        def protect_math(segment: str) -> str:
            math_envs = r"(?:equation\*?|align\*?|gather\*?|multline\*?)"
            segment = re.sub(
                rf"\\begin\{{({math_envs})\}}.*?\\end\{{\1\}}",
                lambda m: stash_math("block", m.group(0)),
                segment,
                flags=re.DOTALL,
            )
            segment = re.sub(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", lambda m: stash_math("block", m.group(0)), segment, flags=re.DOTALL)
            segment = re.sub(r"\\\[(.+?)\\\]", lambda m: stash_math("block", m.group(0)), segment, flags=re.DOTALL)
            segment = re.sub(r"\\\((.+?)\\\)", lambda m: stash_math("inline", m.group(0)), segment, flags=re.DOTALL)
            segment = re.sub(r"(?<!\\)\$(?!\$)(.+?)(?<!\\)\$", lambda m: stash_math("inline", m.group(0)), segment, flags=re.DOTALL)
            return segment

        def convert_itemize_blocks(segment: str) -> str:
            def render_itemize(match: re.Match[str]) -> str:
                body = match.group(1)
                items = [part.strip() for part in re.split(r"\\item\b", body) if part.strip()]
                if not items:
                    return "<ul></ul>"
                return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"

            segment = re.sub(r"\\begin\{itemize\}(.*?)\\end\{itemize\}", render_itemize, segment, flags=re.DOTALL)
            return re.sub(r"(?m)^(\s*)\\item\b\s*", r"\1- ", segment)

        parts = re.split(r"(```.*?```)", raw, flags=re.DOTALL)
        escaped_parts = []
        for part in parts:
            escaped_part = html.escape(part)
            if not (part.startswith("```") and part.endswith("```")):
                escaped_part = convert_itemize_blocks(escaped_part)
                escaped_part = protect_math(escaped_part)
            escaped_parts.append(escaped_part)
        prepared = "".join(escaped_parts)

        try:
            import markdown

            body = markdown.markdown(
                prepared,
                extensions=["fenced_code", "tables", "sane_lists"],
                output_format="html5",
            )
        except Exception:
            body = "<pre>" + prepared + "</pre>"

        for token, (kind, fragment) in math_fragments.items():
            if kind == "block":
                replacement = f'<div class="math-display">{fragment}</div>'
                body = re.sub(rf"<p>\s*{re.escape(token)}\s*</p>", lambda _m: replacement, body)
            else:
                replacement = f'<span class="math-inline">{fragment}</span>'
            body = body.replace(token, replacement)

        return (
            "<html><head>"
            "<script>"
            "window.MathJax = {"
            "loader: {"
            f"paths: {{ fonts: '{mathjax_fonts_path.as_uri()}' }}"
            "},"
            "tex: {"
            "inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],"
            "displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],"
            "processEscapes: true,"
            "processEnvironments: true"
            "},"
            "svg: {"
            "fontCache: 'global',"
            "dynamicPrefix: '[mathjax-newcm]/svg/dynamic'"
            "},"
            "options: { skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'] }"
            "};"
            "</script>"
            f"<script defer src=\"{mathjax_path.as_uri()}\"></script>"
            "<style>"
            "body { font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif; "
            "font-size: 14px; line-height: 1.65; color: #202124; padding: 12px; overflow: visible; }"
            "h1, h2, h3 { margin-top: 1.0em; margin-bottom: 0.35em; }"
            "h3 { border-top: 1px solid #d8dee4; padding-top: 0.75em; color: #1f5f8b; }"
            "h3 + p { background: #f6f8fa; border-radius: 8px; padding: 0.65em 0.8em; }"
            "p { margin: 0.45em 0; line-height: 1.65; overflow: visible; }"
            "li { line-height: 1.65; overflow: visible; }"
            "ul, ol { margin-top: 0.35em; margin-bottom: 0.6em; padding-left: 1.6em; }"
            "pre { background: #f6f8fa; border: 1px solid #d8dee4; border-radius: 6px; "
            "padding: 10px; white-space: pre-wrap; }"
            "code { font-family: Menlo, Consolas, monospace; background: #f6f8fa; padding: 1px 3px; border-radius: 3px; }"
            "mjx-container { overflow: visible !important; padding: 0.08em 0; }"
            "mjx-container[jax=\"SVG\"] { overflow: visible !important; padding: 0.12em 0; }"
            "mjx-container[jax=\"SVG\"] > svg { overflow: visible !important; }"
            ".math-display mjx-container[jax=\"SVG\"] { display: block; margin: 0.25em 0; }"
            ".math-inline { display: inline-block; overflow: visible; line-height: normal; padding: 0.08em 0; }"
            ".math-display { display: block; overflow: visible; margin: 1.15em 0; padding: 0.45em 0; line-height: normal; }"
            "</style></head><body>"
            + body
            + "</body></html>"
        )

    @staticmethod
    def _display_status_text(payload: dict[str, Any], status: dict[str, Any], pause: dict[str, Any]) -> str:
        status_name = str(status.get("status") or "unknown").strip()
        pause_reason = str(status.get("pause_reason") or "").strip()
        message = str(payload.get("message") or "").strip()

        if status_name == "no_pdf":
            return "No PDF selected"
        if status_name == "no_session":
            return "No existing audit session"
        if status_name == "error":
            return message or "Status unavailable"
        if status_name == "initialized":
            return "Ready to start"
        if status_name == "running":
            if pause.get("requested"):
                return "Running, pause requested after current chunk"
            return "Running"
        if status_name == "paused":
            if pause_reason == "requested":
                return "Paused after current chunk"
            if pause_reason:
                return f"Paused ({pause_reason})"
            return "Paused"
        if status_name == "completed":
            return "Completed"
        if message:
            return message
        return status_name or "unknown"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = max(0.0, float(seconds or 0.0))
        minutes, sec = divmod(int(round(seconds)), 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes}m {sec}s"
        if minutes:
            return f"{minutes}m {sec}s"
        return f"{sec}s"
