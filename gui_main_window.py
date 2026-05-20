from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

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

from gui_controller import DEFAULT_MODEL, MODEL_CHOICES, GuiController


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

        self.model_combo.setCurrentText(DEFAULT_MODEL)
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
        self.model_combo.addItems(MODEL_CHOICES)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        layout.addRow("Model", self.model_combo)

        self.reasoning_combo = QComboBox()
        self.reasoning_combo.currentTextChanged.connect(self.controller.set_reasoning_effort)
        layout.addRow("Reasoning effort", self.reasoning_combo)

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
        verification_layout.addWidget(failed_rerun_box)
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
        self._refresh_reasoning_options(model)

    def _on_audit_settings_changed(self, model: str, reasoning_effort: str) -> None:
        self._set_model_effort_controls(model, reasoning_effort)

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
        self.model_combo.setCurrentText(model)
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
            self.reasoning_combo.setToolTip("")
        self.reasoning_combo.blockSignals(False)
        if notify_controller:
            self.controller.set_reasoning_effort(self.reasoning_combo.currentText())

    def _start_fresh_audit(self) -> None:
        self._apply_api_key()
        self._apply_pdf_path()
        self.controller.start_fresh_audit()

    def _resume_audit(self) -> None:
        self._apply_api_key()
        self._apply_pdf_path()
        self.controller.resume_audit()

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

    def _on_status_updated(self, payload: dict[str, Any]) -> None:
        self._last_status_payload = payload
        status = payload.get("status") or {}
        usage = payload.get("usage") or {}
        totals = usage.get("totals") or {}
        discussion_usage = payload.get("discussion_usage") or {}
        pause = payload.get("pause") or {}
        failed_verification = payload.get("failed_verification") or {}
        verification_suite = payload.get("verification_suite") or {}
        status_cost = status.get("cost_usd", None)
        totals_cost = float(totals.get("cost_usd", 0.0) or 0.0)
        if status_cost is None:
            display_cost = totals_cost
        else:
            display_cost = float(status_cost or 0.0)
            if totals_cost > display_cost:
                display_cost = totals_cost

        self.status_value.setText(self._display_status_text(payload, status, pause))
        self.current_chunk_value.setText(str(status.get("current_chunk_id") or "-"))
        self.progress_bar.setValue(int(round(float(status.get("progress_pct", 0.0) or 0.0))))
        self.chunk_progress_value.setText(f"{status.get('chunks_completed', 0)} / {status.get('chunks_total', 0)}")
        self.page_progress_value.setText(
            f"{status.get('estimated_pages_completed', 0)} / {status.get('estimated_pages_total', 0)}"
        )
        self.cost_value.setText(f"${display_cost:.4f}")
        self.tokens_value.setText(str(int(totals.get("total_tokens", 0) or 0)))
        self.elapsed_value.setText(self._format_duration(float(totals.get("audit_seconds", 0.0) or 0.0)))
        self._update_cache_reuse_status(status.get("last_chunk_usage_diagnostics") or {})
        self.discussion_cost_value.setText(f"Cost: ${float(discussion_usage.get('cost_usd', 0.0) or 0.0):.4f}")
        self.discussion_tokens_value.setText(f"Tokens: {int(discussion_usage.get('total_tokens', 0) or 0)}")
        self.discussion_turns_value.setText(f"Turns: {int(discussion_usage.get('turns', 0) or 0)}")

        if pause.get("requested"):
            requested_at = str(pause.get("requested_at") or "").strip()
            self.pause_state_value.setText(f"Requested{f' at {requested_at}' if requested_at else ''}")
        elif str(status.get("status")) == "paused":
            reason = str(status.get("pause_reason") or "paused").strip()
            self.pause_state_value.setText(f"Paused ({reason})")
        else:
            self.pause_state_value.setText("Not requested")

        failed_summary = failed_verification.get("summary") or {}
        failed_count = int(failed_summary.get("failed_chunk_count", 0) or 0)
        failed_result_count = int(failed_summary.get("failed_result_count", 0) or 0)
        failed_chunk_ids = failed_verification.get("chunk_ids") or []
        if failed_count:
            preview = ", ".join(str(chunk_id) for chunk_id in failed_chunk_ids[:6])
            if failed_count > 6:
                preview += ", ..."
            self.failed_verification_summary_value.setPlainText(
                f"{failed_count} chunk(s), {failed_result_count} result(s): {preview}"
            )
        else:
            self.failed_verification_summary_value.setPlainText("No failed verification results")
        self._set_failed_verification_rerun_compact(failed_count)

        scripts_total = int(verification_suite.get("scripts_total", 0) or 0)
        self.verification_scripts_available_value.setText(f"Currently active verification scripts: {scripts_total}")
        last_run = verification_suite.get("last_run")
        if isinstance(last_run, dict) and last_run:
            self.verification_last_run_value.setText(
                "Last run (active scripts): "
                f"passed {int(last_run.get('passed', 0) or 0)}, "
                f"failed {int(last_run.get('failed', 0) or 0)}, "
                f"timed out {int(last_run.get('timeout', 0) or 0)}, "
                f"skipped {int(last_run.get('skipped', 0) or 0)}"
            )
        else:
            self.verification_last_run_value.setText("Last run: none")
        inventory_warning = verification_suite.get("inventory_warning") or {}
        if inventory_warning.get("has_invalidated_obligations"):
            affected_chunks = inventory_warning.get("affected_chunks") or []
            preview = ", ".join(str(chunk_id) for chunk_id in affected_chunks[:6])
            if len(affected_chunks) > 6:
                preview += ", ..."
            self.verification_inventory_warning_value.setText(
                "Warning: "
                f"{int(inventory_warning.get('invalidated_script_count', 0) or 0)} archived/invalidated "
                "verification script(s) from failed chunk rerun(s) are not in the active suite. "
                f"Affected chunks: {preview or 'unknown'}. "
                "Verification is incomplete until those chunks are successfully rerun and verification is regenerated."
            )
        else:
            self.verification_inventory_warning_value.setText("")

        self._update_report_freshness_labels(payload.get("report_freshness") or {})
        self._apply_button_states()

    def _update_cache_reuse_status(self, diagnostics: dict[str, Any]) -> None:
        if not isinstance(diagnostics, dict) or not diagnostics:
            self.cache_reuse_value.setText("-")
            self.cache_reuse_value.setToolTip("")
            self.cache_reuse_value.setStyleSheet("")
            return
        input_tokens = int(diagnostics.get("input_tokens", 0) or 0)
        if input_tokens <= 0:
            self.cache_reuse_value.setText("n/a")
            self.cache_reuse_value.setToolTip("")
            self.cache_reuse_value.setStyleSheet("")
            return
        cached_tokens = int(diagnostics.get("cached_input_tokens", 0) or 0)
        percent = diagnostics.get("cached_input_percent")
        if percent is None:
            percent = float(diagnostics.get("cached_input_ratio", 0.0) or 0.0) * 100.0
        chunk_id = str(diagnostics.get("chunk_id") or "last chunk")
        warning = str(diagnostics.get("warning") or "").strip()
        if warning:
            self.cache_reuse_value.setText(
                f"{chunk_id}: {float(percent):.1f}% cached - cost may be higher"
            )
            self.cache_reuse_value.setToolTip(
                f"{warning}\nInput tokens: {input_tokens:,}\nCached input tokens: {cached_tokens:,}"
            )
            self.cache_reuse_value.setStyleSheet("color: #b06000; font-weight: 600;")
        else:
            self.cache_reuse_value.setText(
                f"{chunk_id}: {float(percent):.1f}% cached ({cached_tokens:,}/{input_tokens:,})"
            )
            self.cache_reuse_value.setToolTip("")
            self.cache_reuse_value.setStyleSheet("")

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
        label.setText(text)
        label.setStyleSheet(style)

    def _on_verification_progress(self, payload: dict[str, Any]) -> None:
        event = str((payload or {}).get("event") or "").strip()
        total = int((payload or {}).get("total", (payload or {}).get("scripts_total", 0)) or 0)
        index = int((payload or {}).get("index", 0) or 0)
        script_name = str((payload or {}).get("script_name") or "").strip()
        if event == "suite_started":
            message = f"Verification scripts available: {total}"
            self.verification_live_progress_value.setText(f"Running verification 0 / {total}")
            self._append_report_output(message)
            return
        if event == "script_started":
            message = f"Running verification {index}/{total}: {script_name}"
            self.verification_live_progress_value.setText(message)
            self._append_report_output(message)
            return
        if event == "script_finished":
            status = str((payload or {}).get("status") or "skipped").lower()
            label = {
                "passed": "PASS",
                "failed": "FAIL",
                "timeout": "TIMEOUT",
                "skipped": "SKIPPED",
            }.get(status, status.upper() or "UNKNOWN")
            message = f"{label}: {script_name}"
            self.verification_live_progress_value.setText(f"Verification {index} / {total}: {label} {script_name}")
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
        self.pause_button.setText("Pause Requested" if pause_requested and running_status else "Pause Audit")
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
        self.rerun_selected_button.setEnabled(session_ready and not pending_response)
        self.select_rerun_chunks_button.setEnabled(session_ready and bool(self._available_rerun_chunks()))
        self._refresh_report_open_buttons()
        self.export_chatgpt_context_pack_button.setEnabled(session_ready)
        self._refresh_chatgpt_export_buttons()
        self.rerun_failed_verification_button.setEnabled(
            session_available
            and not self._task_running
            and status_name in {"completed", "paused"}
            and not pending_response
            and failed_count > 0
        )
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
        self.answer_raw_output.setPlainText(transcript)
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
        if scroll_to_bottom:
            def scroll_after_load(_ok: bool = False) -> None:
                try:
                    self.answer_rendered_output.loadFinished.disconnect(scroll_after_load)
                except TypeError:
                    pass
                QTimer.singleShot(250, self._scroll_rendered_discussion_to_bottom)
                QTimer.singleShot(900, self._scroll_rendered_discussion_to_bottom)

            self.answer_rendered_output.loadFinished.connect(scroll_after_load)
        self.answer_rendered_output.setHtml(
            self._render_discussion_markdown_html(transcript),
            QUrl.fromLocalFile(str(Path(__file__).resolve().parent) + "/"),
        )

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
