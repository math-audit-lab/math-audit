from __future__ import annotations

import json
import os
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from openai import OpenAI
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from audit_hooks import configure_runtime
from audit_policy_hooks import build_final_report, build_user_message_for_chunk
from audit_prompts import (
    SHIPPED_AUDIT_SYSTEM_PROMPT,
    effective_audit_system_prompt_with_source,
    load_prompt_profiles,
    save_prompt_profiles,
)
from audit_runtime import (
    AUDIT_CONTEXT_MODES,
    DEFAULT_AUDIT_CONTEXT_MODE,
    DEFAULT_MODEL as RUNTIME_DEFAULT_MODEL,
    DEFAULT_QA_CONTEXT_MODE,
    QA_CONTEXT_MODES,
    ask_about_audit,
    ask_about_paper,
    build_concise_report as runtime_build_concise_report,
    build_final_report as runtime_build_final_report,
    build_verification_report,
    cancel_pending_response_for_retry,
    default_reasoning_effort_for_model,
    export_chatgpt_context_pack as runtime_export_chatgpt_context_pack,
    get_failed_verification_chunks,
    get_audit_status,
    get_verification_suite_status,
    import_accepted_issue_family_recheck,
    load_post_audit_review_summary,
    list_qa_threads,
    load_qa_turns,
    model_display_choices,
    model_display_name,
    model_guidance,
    model_choices,
    normalize_model_and_reasoning_effort,
    prepare_issue_family_recheck_dry_run,
    prepare_post_audit_review_summary,
    request_pause,
    rerun_failed_verification_chunks as runtime_rerun_failed_verification_chunks,
    rerun_selected_chunks as runtime_rerun_selected_chunks,
    resume_audit,
    run_issue_family_recheck_live,
    run_verification_suite_and_build_report,
    reasoning_effort_guidance_for_model,
    set_openai_client,
    set_active_qa_thread,
    start_fresh_audit,
    start_new_qa_thread,
    supported_reasoning_efforts_for_model,
)
from audit_state import load_session_from_pdf, load_usage, workdir_from_pdf


DEFAULT_MODEL = RUNTIME_DEFAULT_MODEL
DEFAULT_REASONING_EFFORT = default_reasoning_effort_for_model(DEFAULT_MODEL)
MODEL_CHOICES = model_choices()
MODEL_DISPLAY_CHOICES = model_display_choices()
DISCUSSION_CONTEXT_MODE_LABELS = {
    "Reduced audit context": "reduced_audit_context",
    "Full audit context": "full_audit_context",
}
DISCUSSION_CONTEXT_MODE_CHOICES = list(DISCUSSION_CONTEXT_MODE_LABELS)
AUDIT_CONTEXT_MODE_LABELS = {
    "Continuous conversation (default)": "continuous",
    "Experimental fresh-context per chunk": "fresh_context_experimental",
}
AUDIT_CONTEXT_MODE_CHOICES = list(AUDIT_CONTEXT_MODE_LABELS)
REVIEW_TAB_ENV_VAR = "MATH_AUDIT_ENABLE_REVIEW_TAB"


def review_summary_polling_enabled(environ: Optional[dict[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(REVIEW_TAB_ENV_VAR) or "") == "1"


def _format_duration_for_log(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    minutes, sec = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def format_chunk_completion_log_line(payload: dict[str, Any], usage_entry: dict[str, Any]) -> str:
    status = payload.get("status") or {}
    usage = payload.get("usage") or {}
    totals = usage.get("totals") or {}
    chunk_id = str(usage_entry.get("chunk_id") or status.get("current_chunk_id") or "chunk")
    parts = [f"[{chunk_id}] completed"]

    chunks_completed = status.get("chunks_completed")
    chunks_total = status.get("chunks_total")
    if chunks_completed is not None and chunks_total is not None:
        parts.append(f"Progress: {int(chunks_completed or 0)}/{int(chunks_total or 0)}")

    pages_completed = status.get("estimated_pages_completed")
    pages_total = status.get("estimated_pages_total")
    if pages_completed is not None and pages_total:
        parts.append(f"Pages: {int(pages_completed or 0)}/{int(pages_total or 0)}")

    if usage_entry.get("elapsed_seconds") is not None:
        parts.append(f"Chunk time: {_format_duration_for_log(float(usage_entry.get('elapsed_seconds') or 0.0))}")

    cost = usage_entry.get("cost") or {}
    chunk_cost = cost.get("total_cost", usage_entry.get("cost_usd"))
    if chunk_cost is not None:
        parts.append(f"Chunk cost: ${float(chunk_cost or 0.0):.4f}")

    cumulative_cost = totals.get("cost_usd", status.get("cost_usd"))
    if cumulative_cost is not None:
        parts.append(f"Cumulative cost: ${float(cumulative_cost or 0.0):.4f}")

    total_seconds = totals.get("audit_seconds", status.get("total_audit_seconds"))
    if total_seconds is not None:
        parts.append(f"Total audit time: {_format_duration_for_log(float(total_seconds or 0.0))}")

    chunk_usage = usage_entry.get("usage") or {}
    usage_diagnostics = usage_entry.get("usage_diagnostics") or {}
    chunk_tokens = chunk_usage.get("total_tokens", usage_diagnostics.get("total_tokens"))
    if chunk_tokens is not None:
        parts.append(f"Chunk tokens: {int(chunk_tokens or 0)}")

    cumulative_tokens = totals.get("total_tokens")
    if cumulative_tokens is not None:
        parts.append(f"Cumulative tokens: {int(cumulative_tokens or 0)}")

    return " | ".join(parts)


def format_running_chunk_started_log_line(status: dict[str, Any]) -> str:
    chunk_id = str(status.get("current_chunk_id") or "chunk")
    parts = [f"[{chunk_id}] started"]
    chunks_completed = status.get("chunks_completed")
    chunks_total = status.get("chunks_total")
    if chunks_completed is not None and chunks_total is not None:
        parts.append(f"Progress: {int(chunks_completed or 0)}/{int(chunks_total or 0)}")
    return " | ".join(parts)


def persistent_audit_log_preview(pdf_path: str, max_entries: int = 8) -> list[str]:
    if not str(pdf_path or "").strip():
        return []
    session = load_session_from_pdf(pdf_path)
    if not session:
        return []
    logs_dir = Path(session["workdir"]) / "logs"
    if not logs_dir.exists():
        return []

    counts: list[str] = []
    events: list[tuple[str, str, dict[str, Any]]] = []
    for path in sorted(logs_dir.glob("*.jsonl")):
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        if not raw_lines:
            continue
        counts.append(f"{path.name}: {len(raw_lines)}")
        for raw in raw_lines[-max(1, int(max_entries)):]:
            try:
                item = json.loads(raw)
            except Exception:
                continue
            if isinstance(item, dict):
                events.append((str(item.get("time") or ""), path.name, item))

    if not counts:
        return []

    lines = [
        "Historical audit logs found: " + "; ".join(counts),
        f"Logs folder: {logs_dir}",
    ]
    events.sort(key=lambda event: event[0])
    recent = events[-max(1, int(max_entries)):]
    if recent:
        lines.append(f"Recent persistent audit events (last {len(recent)}):")
    for event_time, filename, event in recent:
        action = str(event.get("action") or "").strip()
        chunk_id = str(event.get("chunk_id") or "").strip()
        chunk_ids = event.get("chunk_ids")
        if not chunk_id and isinstance(chunk_ids, list) and chunk_ids:
            preview = ", ".join(str(item) for item in chunk_ids[:4])
            if len(chunk_ids) > 4:
                preview += ", ..."
            chunk_id = preview
        label = f"{filename}: {action or 'event'}"
        if chunk_id:
            label += f" ({chunk_id})"
        if event.get("error"):
            label += " - error recorded"
        lines.append(f"- {event_time or 'unknown time'} | {label}")
    return lines


def audit_context_mode_display_label(mode: str) -> str:
    clean = str(mode or DEFAULT_AUDIT_CONTEXT_MODE).strip()
    return next((label for label, value in AUDIT_CONTEXT_MODE_LABELS.items() if value == clean), clean)


def fresh_start_context_mode_mismatch_info(pdf_path: str, selected_mode: str) -> dict[str, str]:
    if not str(pdf_path or "").strip():
        return {}
    session = load_session_from_pdf(pdf_path)
    if not session:
        return {}
    saved_mode = str(session.get("audit_context_mode") or DEFAULT_AUDIT_CONTEXT_MODE).strip()
    if saved_mode not in AUDIT_CONTEXT_MODES:
        saved_mode = DEFAULT_AUDIT_CONTEXT_MODE
    requested = str(selected_mode or DEFAULT_AUDIT_CONTEXT_MODE).strip()
    if requested not in AUDIT_CONTEXT_MODES:
        requested = DEFAULT_AUDIT_CONTEXT_MODE
    if saved_mode == requested:
        return {}
    return {
        "saved_mode": saved_mode,
        "selected_mode": requested,
        "saved_label": audit_context_mode_display_label(saved_mode),
        "selected_label": audit_context_mode_display_label(requested),
        "workdir": str(workdir_from_pdf(pdf_path)),
    }


class BackendWorker(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    @Slot()
    def run(self) -> None:
        try:
            value = self._fn(*self._args, **self._kwargs)
        except Exception:
            self.error.emit(traceback.format_exc())
        else:
            self.result.emit(value)
        finally:
            self.finished.emit()


class GuiController(QObject):
    status_updated = Signal(dict)
    log_message = Signal(str)
    report_output = Signal(str)
    report_paths_updated = Signal(dict)
    chatgpt_context_pack_exported = Signal(dict)
    verification_progress = Signal(dict)
    discussion_output = Signal(str)
    discussion_history_loaded = Signal(list)
    discussion_threads_loaded = Signal(list, str)
    task_running_changed = Signal(bool)
    cancel_task_running_changed = Signal(bool)
    audit_settings_changed = Signal(str, str)
    audit_context_mode_changed = Signal(str)
    review_summary_updated = Signal(dict)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.api_key = ""
        self.pdf_path = ""
        self.model = DEFAULT_MODEL
        self.reasoning_effort = DEFAULT_REASONING_EFFORT
        self.qa_context_mode = DEFAULT_QA_CONTEXT_MODE
        self.audit_context_mode = DEFAULT_AUDIT_CONTEXT_MODE

        self._active_thread: Optional[QThread] = None
        self._active_worker: Optional[BackendWorker] = None
        self._active_task_name: Optional[str] = None
        self._cancel_thread: Optional[QThread] = None
        self._cancel_worker: Optional[BackendWorker] = None
        self._last_status_signature: Optional[tuple[Any, ...]] = None
        self._last_auto_retry_signature: Optional[tuple[Any, ...]] = None
        self._shutdown_prepared = False
        self._saved_session_model: Optional[str] = None
        self._saved_session_reasoning_effort: Optional[str] = None
        self._model_effort_override_active = False
        self._logged_chunk_completion_signatures: set[tuple[str, str]] = set()
        self._chunk_completion_log_pdf_path = ""

        configure_runtime(
            prompt_builder=build_user_message_for_chunk,
            final_report_builder=build_final_report,
        )

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1500)
        self._poll_timer.timeout.connect(self.poll_status)
        self._poll_timer.start()

    def set_api_key(self, api_key: str) -> None:
        previous_api_key = self.api_key
        self.api_key = str(api_key or "").strip()
        changed = self.api_key != previous_api_key
        try:
            if self.api_key:
                client = OpenAI(api_key=self.api_key)
                configure_runtime(
                    client=client,
                    prompt_builder=build_user_message_for_chunk,
                    final_report_builder=build_final_report,
                )
                if changed:
                    message = "API key configured for backend calls."
                    if previous_api_key:
                        message = "API key updated for backend calls."
                    self.log_message.emit(message)
            else:
                set_openai_client(None)
                configure_runtime(
                    prompt_builder=build_user_message_for_chunk,
                    final_report_builder=build_final_report,
                )
                if changed and previous_api_key:
                    self.log_message.emit(
                        "API key cleared. Report/status actions still work; live API calls now require a new key or environment configuration."
                    )
        except Exception as exc:
            self.log_message.emit(f"Failed to configure API client: {type(exc).__name__}: {exc}")

    def set_pdf_path(self, pdf_path: str) -> None:
        previous_pdf_path = self.pdf_path
        self.pdf_path = str(pdf_path or "").strip()
        path_changed = self.pdf_path != previous_pdf_path
        if path_changed:
            if self.pdf_path:
                self.log_message.emit(f"Selected PDF: {self.pdf_path}")
            self._load_saved_audit_settings()
            self._load_saved_discussion_history()
            self._load_saved_discussion_threads()
            self._prime_chunk_completion_log_state()
            for line in persistent_audit_log_preview(self.pdf_path):
                self.log_message.emit(line)
        self.poll_status()

    def set_model(self, model: str) -> None:
        previous_effort = self.reasoning_effort
        self.model, self.reasoning_effort = normalize_model_and_reasoning_effort(model, previous_effort)
        if previous_effort and self.reasoning_effort != previous_effort:
            self.log_message.emit(
                f"Reasoning effort adjusted to {self.reasoning_effort} for model {self.model}; "
                f"{previous_effort} is not supported by this model."
            )
        self._mark_model_effort_override_if_needed()

    def set_reasoning_effort(self, reasoning_effort: str) -> None:
        self.model, self.reasoning_effort = normalize_model_and_reasoning_effort(self.model, reasoning_effort)
        self._mark_model_effort_override_if_needed()

    def reasoning_effort_options(self, model: Optional[str] = None) -> list[str]:
        return supported_reasoning_efforts_for_model(model or self.model)

    def default_reasoning_effort(self, model: Optional[str] = None) -> str:
        return default_reasoning_effort_for_model(model or self.model)

    def model_display_name(self, model: Optional[str] = None) -> str:
        return model_display_name(model or self.model)

    def model_guidance(self, model: Optional[str] = None) -> str:
        return model_guidance(model or self.model)

    def reasoning_effort_guidance(self, model: Optional[str] = None) -> dict[str, str]:
        return reasoning_effort_guidance_for_model(model or self.model)

    def prompt_profile_targets(self) -> list[str]:
        return ["Default prompt"] + list(MODEL_CHOICES)

    def prompt_text_for_target(self, target: str) -> str:
        profiles = load_prompt_profiles()
        clean = str(target or "").strip()
        if clean == "Default prompt":
            return str(profiles.get("default_prompt") or SHIPPED_AUDIT_SYSTEM_PROMPT)
        overrides = profiles.get("model_overrides") or {}
        if isinstance(overrides, dict) and str(overrides.get(clean) or "").strip():
            return str(overrides.get(clean))
        prompt, _source = effective_audit_system_prompt_with_source(clean)
        return prompt

    def prompt_status_for_target(self, target: str) -> str:
        profiles = load_prompt_profiles()
        clean = str(target or "").strip()
        if clean == "Default prompt":
            return "Custom default prompt" if str(profiles.get("default_prompt") or "").strip() else "Shipped default prompt"
        overrides = profiles.get("model_overrides") or {}
        if isinstance(overrides, dict) and str(overrides.get(clean) or "").strip():
            return f"Model-specific override for {clean}"
        _prompt, source = effective_audit_system_prompt_with_source(clean)
        return f"Inherits {source}"

    def save_prompt_for_target(self, target: str, prompt: str) -> None:
        profiles = load_prompt_profiles()
        clean = str(target or "").strip()
        text = str(prompt or "")
        if clean == "Default prompt":
            profiles["default_prompt"] = text
        else:
            overrides = profiles.get("model_overrides")
            if not isinstance(overrides, dict):
                overrides = {}
                profiles["model_overrides"] = overrides
            overrides[clean] = text
        save_prompt_profiles(profiles)
        self.log_message.emit(f"Saved audit prompt profile: {clean}")

    def reset_prompt_for_target(self, target: str) -> None:
        profiles = load_prompt_profiles()
        clean = str(target or "").strip()
        if clean == "Default prompt":
            profiles["default_prompt"] = ""
        else:
            overrides = profiles.get("model_overrides")
            if isinstance(overrides, dict):
                overrides.pop(clean, None)
        save_prompt_profiles(profiles)
        self.log_message.emit(f"Reset audit prompt profile: {clean}")

    def set_discussion_context_mode(self, label_or_mode: str) -> None:
        requested = str(label_or_mode or "").strip()
        mode = DISCUSSION_CONTEXT_MODE_LABELS.get(requested, requested)
        if mode not in QA_CONTEXT_MODES:
            self.log_message.emit(f"Unsupported discussion context mode: {requested}")
            return
        if mode != self.qa_context_mode:
            self.qa_context_mode = mode
            display = next((label for label, value in DISCUSSION_CONTEXT_MODE_LABELS.items() if value == mode), mode)
            self.log_message.emit(f"Discussion context mode: {display}")

    def set_audit_context_mode(self, label_or_mode: str) -> None:
        requested = str(label_or_mode or "").strip()
        mode = AUDIT_CONTEXT_MODE_LABELS.get(requested, requested)
        if mode not in AUDIT_CONTEXT_MODES:
            self.log_message.emit(f"Unsupported audit context mode: {requested}")
            return
        if mode != self.audit_context_mode:
            self.audit_context_mode = mode
            self.log_message.emit(f"Audit context mode: {audit_context_mode_display_label(mode)}")

    def _load_saved_audit_settings(self) -> None:
        self._saved_session_model = None
        self._saved_session_reasoning_effort = None
        self._model_effort_override_active = False
        if not self.pdf_path:
            return
        session = load_session_from_pdf(self.pdf_path)
        if not session:
            return
        model, effort = normalize_model_and_reasoning_effort(
            session.get("model"),
            session.get("reasoning_effort"),
        )
        self.model = model
        self.reasoning_effort = effort
        saved_context_mode = str(session.get("audit_context_mode") or DEFAULT_AUDIT_CONTEXT_MODE)
        self.audit_context_mode = saved_context_mode if saved_context_mode in AUDIT_CONTEXT_MODES else DEFAULT_AUDIT_CONTEXT_MODE
        self._saved_session_model = model
        self._saved_session_reasoning_effort = effort
        self.audit_settings_changed.emit(model, effort)
        self.audit_context_mode_changed.emit(self.audit_context_mode)
        self.log_message.emit(f"Loaded saved audit settings: {model} / {effort} / context={self.audit_context_mode}")

    def fresh_start_context_mode_mismatch_info(self) -> dict[str, str]:
        return fresh_start_context_mode_mismatch_info(self.pdf_path, self.audit_context_mode)

    def _restore_saved_context_mode_for_resume(self) -> None:
        session = load_session_from_pdf(self.pdf_path) if self.pdf_path else None
        if not session:
            return
        saved_context_mode = str(session.get("audit_context_mode") or DEFAULT_AUDIT_CONTEXT_MODE).strip()
        if saved_context_mode not in AUDIT_CONTEXT_MODES:
            saved_context_mode = DEFAULT_AUDIT_CONTEXT_MODE
        if saved_context_mode == self.audit_context_mode:
            return
        self.audit_context_mode = saved_context_mode
        self.audit_context_mode_changed.emit(saved_context_mode)
        self.log_message.emit(
            "Resume uses the saved audit context mode: "
            f"{audit_context_mode_display_label(saved_context_mode)}."
        )

    def _load_saved_discussion_history(self) -> None:
        if not self.pdf_path:
            self.discussion_history_loaded.emit([])
            return
        session = load_session_from_pdf(self.pdf_path)
        if not session:
            self.discussion_history_loaded.emit([])
            return
        try:
            turns = load_qa_turns(session, active_thread_only=True)
        except Exception as exc:
            self.discussion_history_loaded.emit([])
            self.log_message.emit(f"Could not load previous discussion: {type(exc).__name__}: {exc}")
            return
        self.discussion_history_loaded.emit(turns)
        if turns:
            self.log_message.emit(f"Loaded previous discussion: {len(turns)} turns.")

    def _load_saved_discussion_threads(self) -> None:
        if not self.pdf_path:
            self.discussion_threads_loaded.emit([], "")
            return
        session = load_session_from_pdf(self.pdf_path)
        if not session:
            self.discussion_threads_loaded.emit([], "")
            return
        try:
            threads = list_qa_threads(session)
        except Exception as exc:
            self.discussion_threads_loaded.emit([], "")
            self.log_message.emit(f"Could not load discussion threads: {type(exc).__name__}: {exc}")
            return
        active_thread_id = next((str(item.get("thread_id") or "") for item in threads if item.get("is_active")), "")
        self.discussion_threads_loaded.emit(threads, active_thread_id)

    def start_new_discussion_thread(self) -> None:
        if self.has_active_task():
            self.log_message.emit("Cannot start a new discussion thread while another task is running.")
            return
        if not self._require_session():
            return
        try:
            result = start_new_qa_thread(self.pdf_path)
        except Exception as exc:
            self.log_message.emit(f"Could not start a new discussion thread: {type(exc).__name__}: {exc}")
            return
        self.discussion_history_loaded.emit([])
        thread_id = str(result.get("thread_id") or "new thread")
        self._load_saved_discussion_threads()
        self.log_message.emit(f"Started new discussion thread: {thread_id}")
        self.poll_status()

    def set_active_discussion_thread(self, thread_id: str) -> None:
        if self.has_active_task():
            self.log_message.emit("Cannot switch discussion threads while another task is running.")
            return
        if not self._require_session():
            return
        clean_thread_id = str(thread_id or "").strip()
        if not clean_thread_id:
            return
        try:
            result = set_active_qa_thread(self.pdf_path, clean_thread_id)
        except Exception as exc:
            self.log_message.emit(f"Could not switch discussion thread: {type(exc).__name__}: {exc}")
            return
        turns = result.get("turns") or []
        threads = result.get("threads") or []
        selected = result.get("thread") or {}
        active_thread_id = str(selected.get("thread_id") or clean_thread_id)
        self.discussion_history_loaded.emit(turns)
        self.discussion_threads_loaded.emit(threads, active_thread_id)
        self.log_message.emit(f"Switched discussion thread: {selected.get('label') or active_thread_id}")
        self.poll_status()

    def _mark_model_effort_override_if_needed(self) -> None:
        if self._saved_session_model is None or self._saved_session_reasoning_effort is None:
            return
        differs = (
            self.model != self._saved_session_model
            or self.reasoning_effort != self._saved_session_reasoning_effort
        )
        was_active = self._model_effort_override_active
        self._model_effort_override_active = differs
        if differs and not was_active:
            self.log_message.emit(f"Overriding saved audit settings: {self.model} / {self.reasoning_effort}")

    def has_active_task(self) -> bool:
        return self._active_thread is not None

    def active_task_name(self) -> str:
        return self._active_task_name or ""

    def cancel_current_chunk_in_progress(self) -> bool:
        return self._cancel_thread is not None

    def _active_task_allows_concurrent_cancel(self) -> bool:
        return self.active_task_name() in {"Start Fresh Audit", "Resume Audit"}

    def prepare_for_shutdown(self) -> None:
        if not self.has_active_task():
            return
        if self._shutdown_prepared:
            return

        self._shutdown_prepared = True
        task_name = self.active_task_name() or "active task"
        if task_name in {"Start Fresh Audit", "Resume Audit"} and self.pdf_path:
            try:
                request_pause(self.pdf_path, include_manifest=False)
                cancel_started = self._run_concurrent_cancel_current_chunk_task(
                    include_manifest=False,
                    quiet_missing=True,
                    prefix="Close requested. ",
                )
                if cancel_started:
                    self.log_message.emit(
                        "Close requested. Pause and current chunk cancellation requested; shutdown will wait for cleanup."
                    )
                else:
                    self.log_message.emit(
                        "Close requested. Pause requested; shutdown will wait for the current chunk to finish."
                    )
            except Exception as exc:
                self.log_message.emit(
                    f"Close requested. Could not request pause ({type(exc).__name__}: {exc}); shutdown will wait for '{task_name}' to finish."
                )
                return
        else:
            self.log_message.emit(f"Close requested. Shutdown will wait for '{task_name}' to finish.")

    def start_fresh_audit(self) -> None:
        if not self._require_pdf_file():
            return
        if not self._require_api_key_for_live_calls():
            return
        self._reset_chunk_completion_log_state_for_new_audit()
        audit_prompt, audit_prompt_source = effective_audit_system_prompt_with_source(self.model)
        self._run_backend_task(
            "Start Fresh Audit",
            start_fresh_audit,
            self._handle_audit_result,
            self.pdf_path,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            audit_system_prompt=audit_prompt,
            audit_system_prompt_source=audit_prompt_source,
            audit_context_mode=self.audit_context_mode,
            verbose=False,
        )

    def resume_audit(self) -> None:
        if not self._require_pdf_file():
            return
        if not self._require_session():
            return
        self._restore_saved_context_mode_for_resume()
        self._prime_chunk_completion_log_state()
        if not self._require_api_key_for_live_calls():
            return
        model = self.model if self._model_effort_override_active else None
        reasoning_effort = self.reasoning_effort if self._model_effort_override_active else None
        self._run_backend_task(
            "Resume Audit",
            resume_audit,
            self._handle_audit_result,
            self.pdf_path,
            model=model,
            reasoning_effort=reasoning_effort,
            verbose=False,
        )

    def pause_audit(self) -> None:
        if not self._require_session():
            return
        try:
            info = request_pause(self.pdf_path, include_manifest=True)
        except Exception as exc:
            self.log_message.emit(f"Pause request failed: {type(exc).__name__}: {exc}")
            return
        self.log_message.emit("Pause requested. The running audit will stop after the current chunk finishes.")
        self.status_updated.emit(self._normalize_status_payload(info))

    def cancel_current_chunk(self) -> None:
        if self.has_active_task():
            if not self._active_task_allows_concurrent_cancel():
                self.log_message.emit("Cancel Current Chunk is only available during an active audit run or when no local GUI task is active.")
                return
            if not self._require_session():
                return
            if not self._require_api_key_for_live_calls():
                return
            self._run_concurrent_cancel_current_chunk_task(include_manifest=True)
            return
        if not self._require_session():
            return
        if not self._require_api_key_for_live_calls():
            return
        payload = self._load_status_payload()
        pending = ((payload.get("session") or {}).get("pending") or {})
        if not pending.get("response_id"):
            self.log_message.emit("No saved pending response was found for the current audit.")
            return
        self._run_backend_task(
            "Cancel Current Chunk",
            cancel_pending_response_for_retry,
            self._handle_cancel_current_chunk_result,
            self.pdf_path,
            include_manifest=True,
        )

    @Slot()
    def poll_status(self) -> None:
        payload = self._load_status_payload()
        self.status_updated.emit(payload)
        self._log_status_change(payload)

    def rebuild_final_report(self) -> None:
        if not self._require_session():
            return
        self._run_backend_task(
            "Rebuild Final Report",
            runtime_build_final_report,
            self._handle_report_result,
            self.pdf_path,
        )

    def build_concise_report(self, options: Optional[dict[str, Any]] = None) -> None:
        if not self._require_session():
            return
        self._run_backend_task(
            "Build Concise Report",
            runtime_build_concise_report,
            self._handle_concise_report_result,
            self.pdf_path,
            options=options,
        )

    def rebuild_verification_report(self) -> None:
        if not self._require_session():
            return
        self._run_backend_task(
            "Rebuild Verification Report",
            build_verification_report,
            self._handle_verification_report_result,
            self.pdf_path,
        )

    def refresh_review_summary(self) -> None:
        if not self._require_session():
            return
        try:
            summary = load_post_audit_review_summary(self.pdf_path)
        except Exception as exc:
            summary = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
            self.log_message.emit(f"Review summary refresh failed: {summary['error']}")
        self.review_summary_updated.emit(summary)

    def prepare_review_summary(self) -> None:
        if not self._require_session():
            return
        self._run_backend_task(
            "Prepare Review Summary",
            prepare_post_audit_review_summary,
            self._handle_review_summary_result,
            self.pdf_path,
        )

    def prepare_selected_family_recheck_dry_run(self, family_id: str) -> None:
        clean = str(family_id or "").strip()
        if not clean:
            self.log_message.emit("Select an issue family before preparing a recheck dry run.")
            return
        if not self._require_session():
            return
        self._run_backend_task(
            "Prepare Family Recheck Dry Run",
            prepare_issue_family_recheck_dry_run,
            self._handle_family_recheck_dry_run_result,
            self.pdf_path,
            clean,
        )

    def run_live_family_recheck(self, family_id: str) -> None:
        clean = str(family_id or "").strip()
        if not clean:
            self.log_message.emit("Select an issue family before running a live recheck.")
            return
        if not self._require_session():
            return
        if not self._require_api_key_for_live_calls():
            return
        self._run_backend_task(
            "Run Live Family Recheck",
            run_issue_family_recheck_live,
            self._handle_live_family_recheck_result,
            self.pdf_path,
            clean,
        )

    def import_accepted_recheck_result(self, recheck_result_path: str) -> None:
        clean = str(recheck_result_path or "").strip()
        if not clean:
            self.log_message.emit("Choose a family_recheck_result.json file to import.")
            return
        if not self._require_session():
            return
        self._run_backend_task(
            "Import Accepted Recheck Result",
            import_accepted_issue_family_recheck,
            self._handle_imported_recheck_result,
            self.pdf_path,
            clean,
        )

    def run_verification_suite(self) -> None:
        if not self._require_session():
            return

        def progress_callback(payload: dict[str, Any]) -> None:
            self.verification_progress.emit(dict(payload or {}))

        self._run_backend_task(
            "Run Verification Suite",
            run_verification_suite_and_build_report,
            self._handle_verification_suite_result,
            self.pdf_path,
            progress_callback=progress_callback,
        )

    def export_chatgpt_context_pack(self, options: Optional[dict[str, Any]] = None) -> None:
        if not self._require_session():
            return
        self._run_backend_task(
            "Export ChatGPT Context Pack",
            runtime_export_chatgpt_context_pack,
            self._handle_chatgpt_context_pack_result,
            self.pdf_path,
            options=options,
        )

    def rerun_selected_chunks(
        self,
        chunk_ids: str,
        extra_rerun_instruction: str = "",
        rebuild_reports: bool = True,
    ) -> None:
        clean_chunk_ids = str(chunk_ids or "").strip()
        if not clean_chunk_ids:
            self.log_message.emit("Enter one or more chunk IDs to rerun, for example: chunk_012 or 12,15,18.")
            return
        if self.has_active_task():
            self.log_message.emit("Rerun Selected Chunks is only available when no local GUI task is active.")
            return
        if not self._require_session():
            return
        if not self._require_api_key_for_live_calls():
            return
        payload = self._load_status_payload()
        session = payload.get("session") or {}
        status = payload.get("status") or {}
        if session.get("pending"):
            self.log_message.emit("Cannot rerun chunks while the audit has a pending response.")
            return
        if str(status.get("status") or "") == "running":
            self.log_message.emit("Cannot rerun chunks while the audit is running.")
            return
        model = self.model if self._model_effort_override_active else None
        reasoning_effort = self.reasoning_effort if self._model_effort_override_active else None
        self._run_backend_task(
            "Rerun Selected Chunks",
            runtime_rerun_selected_chunks,
            self._handle_rerun_selected_chunks_result,
            self.pdf_path,
            clean_chunk_ids,
            extra_rerun_instruction=str(extra_rerun_instruction or "").strip() or None,
            model=model,
            reasoning_effort=reasoning_effort,
            rebuild_reports=bool(rebuild_reports),
        )

    def rerun_failed_verification_chunks(
        self,
        chunk_ids: str = "",
        include_verification_output: bool = True,
        rebuild_reports: bool = True,
    ) -> None:
        if self.has_active_task():
            self.log_message.emit("Failed-verification rerun is only available when no local GUI task is active.")
            return
        if not self._require_session():
            return
        if not self._require_api_key_for_live_calls():
            return
        payload = self._load_status_payload()
        session = payload.get("session") or {}
        status = payload.get("status") or {}
        failed_verification = payload.get("failed_verification") or {}
        failed_count = int((failed_verification.get("summary") or {}).get("failed_chunk_count", 0) or 0)
        status_name = str(status.get("status") or "")
        if session.get("pending"):
            self.log_message.emit("Cannot rerun failed-verification chunks while the audit has a pending response.")
            return
        if status_name not in {"completed", "paused"}:
            self.log_message.emit("Failed-verification rerun is available only for completed or paused audits.")
            return
        if failed_count <= 0:
            self.log_message.emit("No failed or timed-out verification results were found.")
            return
        model = self.model if self._model_effort_override_active else None
        reasoning_effort = self.reasoning_effort if self._model_effort_override_active else None
        clean_chunk_ids = str(chunk_ids or "").strip() or None
        self._run_backend_task(
            "Rerun Failed Verification Chunks",
            runtime_rerun_failed_verification_chunks,
            self._handle_failed_verification_rerun_result,
            self.pdf_path,
            chunk_ids=clean_chunk_ids,
            include_verification_output=bool(include_verification_output),
            model=model,
            reasoning_effort=reasoning_effort,
            rebuild_reports=bool(rebuild_reports),
        )

    def ask_about_paper(self, question: str) -> None:
        clean = str(question or "").strip()
        if not clean:
            self.log_message.emit("Enter a question before asking about the paper.")
            return
        if not self._require_session():
            return
        if not self._require_api_key_for_live_calls():
            return
        self._run_backend_task(
            "Ask About Paper",
            ask_about_paper,
            self._handle_discussion_result,
            self.pdf_path,
            clean,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            qa_context_mode=self.qa_context_mode,
        )

    def ask_about_audit(self, question: str) -> None:
        clean = str(question or "").strip()
        if not clean:
            self.log_message.emit("Enter a question before asking about the audit.")
            return
        if not self._require_session():
            return
        if not self._require_api_key_for_live_calls():
            return
        self._run_backend_task(
            "Ask About Audit",
            ask_about_audit,
            self._handle_discussion_result,
            self.pdf_path,
            clean,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            qa_context_mode=self.qa_context_mode,
        )

    def _run_backend_task(
        self,
        task_name: str,
        fn: Callable[..., Any],
        on_result: Callable[[Any], None],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if self._active_thread is not None:
            self.log_message.emit(f"Cannot start '{task_name}' while '{self._active_task_name or 'another task'}' is still running.")
            return

        thread = QThread(self)
        worker = BackendWorker(fn, *args, **kwargs)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.result.connect(on_result)
        worker.error.connect(lambda message, name=task_name: self._handle_task_error(name, message))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_active_task)

        self._active_thread = thread
        self._active_worker = worker
        self._active_task_name = task_name
        self._shutdown_prepared = False
        self.task_running_changed.emit(True)
        self.log_message.emit(f"{task_name} started.")
        thread.start()

    def _run_concurrent_cancel_current_chunk_task(
        self,
        include_manifest: bool = True,
        quiet_missing: bool = False,
        prefix: str = "",
    ) -> bool:
        if self._cancel_thread is not None:
            if not quiet_missing:
                self.log_message.emit("Cancel Current Chunk is already in progress.")
            return False
        if not self.pdf_path:
            if not quiet_missing:
                self.log_message.emit("Choose a PDF first.")
            return False
        if not self.api_key:
            if not quiet_missing:
                self.log_message.emit("Enter an API key before cancelling the current chunk.")
            return False
        payload = self._load_status_payload()
        pending = ((payload.get("session") or {}).get("pending") or {})
        if not pending.get("response_id"):
            if not quiet_missing:
                self.log_message.emit("No saved pending response was found for the current audit.")
            return False

        thread = QThread(self)
        worker = BackendWorker(
            cancel_pending_response_for_retry,
            self.pdf_path,
            include_manifest=include_manifest,
        )
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.result.connect(self._handle_cancel_current_chunk_result)
        worker.error.connect(lambda message: self._handle_task_error("Cancel Current Chunk", message))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_cancel_task)

        self._cancel_thread = thread
        self._cancel_worker = worker
        self.cancel_task_running_changed.emit(True)
        self.log_message.emit(f"{prefix}Cancel Current Chunk started.")
        thread.start()
        return True

    @Slot()
    def _clear_active_task(self) -> None:
        self._active_thread = None
        self._active_worker = None
        self._active_task_name = None
        self._shutdown_prepared = False
        self.task_running_changed.emit(False)
        self.poll_status()

    @Slot()
    def _clear_cancel_task(self) -> None:
        self._cancel_thread = None
        self._cancel_worker = None
        self.cancel_task_running_changed.emit(False)
        self.poll_status()

    def _handle_task_error(self, task_name: str, message: str) -> None:
        short = message.strip().splitlines()[-1] if message.strip() else "Unknown error"
        self.log_message.emit(f"{task_name} failed: {short}")
        if task_name == "Build Concise Report":
            self.report_output.emit(f"Build Concise Report failed: {short}")
        elif "Report" in task_name:
            self.report_output.emit(message.strip())
        elif "Ask About" in task_name:
            self.discussion_output.emit(message.strip())

    def _emit_current_status(self) -> None:
        self.status_updated.emit(self._load_status_payload())

    def _handle_cancel_current_chunk_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.log_message.emit("Cancel Current Chunk finished with an unexpected result shape.")
            return
        event = result.get("cancel_event") or {}
        status = result.get("status") or {}
        chunk_id = str(event.get("chunk_id") or status.get("current_chunk_id") or "the current chunk")
        self.log_message.emit(f"Cancelled pending response; Resume Audit will retry {chunk_id}.")
        self.status_updated.emit(self._normalize_status_payload(result))

    def _handle_audit_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.log_message.emit("Audit finished with an unexpected result shape.")
            return
        status = result.get("status") or {}
        usage = result.get("usage") or {}
        totals = usage.get("totals") or {}
        report_paths = result.get("report_paths")
        concise_report_paths = result.get("concise_report_paths")
        report_warnings = result.get("report_generation_warnings") or []
        pause_result = result.get("pause_result")
        recovery_result = result.get("recovery_result")
        chunks_completed = int(status.get("chunks_completed", 0) or 0)
        chunks_total = int(status.get("chunks_total", 0) or 0)
        total_cost = float(totals.get("cost_usd", status.get("cost_usd", 0.0)) or 0.0)
        total_seconds = float(totals.get("audit_seconds", 0.0) or 0.0)
        completion_summary = (
            f"{chunks_completed}/{chunks_total} chunks | "
            f"${total_cost:.4f} | "
            f"{self._format_duration(total_seconds)}"
        )

        if report_paths:
            if concise_report_paths:
                self.log_message.emit(f"Audit completed: {completion_summary}. Full and concise reports generated.")
                self.log_message.emit("Concise report generated with default settings.")
            else:
                self.log_message.emit(f"Audit completed: {completion_summary}. Full report generated.")
            for warning in report_warnings:
                self.log_message.emit(f"Report generation warning: {warning}")
            self.report_output.emit(self._format_path_payload("Audit report outputs", report_paths))
            self.report_paths_updated.emit(dict(report_paths))
            self._emit_current_status()
        elif pause_result:
            self.log_message.emit("Audit paused cleanly after the current chunk.")
        elif recovery_result:
            self.log_message.emit("Audit paused during recovery after a previously submitted request failed remotely.")
        else:
            self.log_message.emit(f"Audit returned with status: {status.get('status', 'unknown')} ({completion_summary}).")

    def _handle_concise_report_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.report_output.emit("Unexpected concise-report result.")
            return
        primary_path = self._primary_output_path(result)
        if primary_path:
            self.log_message.emit(f"Concise report built. Primary output: {primary_path}")
        else:
            self.log_message.emit("Concise report built.")
        self.report_output.emit(self._format_path_payload("Concise report outputs", result))
        self.report_paths_updated.emit(dict(result))
        self._emit_current_status()

    def _handle_report_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.report_output.emit("Unexpected final-report result.")
            return
        primary_path = self._primary_output_path(result)
        if primary_path:
            self.log_message.emit(f"Final report rebuilt. Primary output: {primary_path}")
        else:
            self.log_message.emit("Final report rebuilt.")
        self.report_output.emit(self._format_path_payload("Final report outputs", result))
        self.report_paths_updated.emit(dict(result))
        self._emit_current_status()

    def _handle_verification_report_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.report_output.emit("Unexpected verification-report result.")
            return
        primary_path = self._primary_output_path(result)
        if primary_path:
            self.log_message.emit(f"Verification report rebuilt. Primary output: {primary_path}")
            self.report_output.emit(self._format_path_payload("Verification report outputs", result))
            self.report_paths_updated.emit(dict(result))
            self._emit_current_status()
        else:
            message = "No verification results found; run the verification suite first."
            self.log_message.emit(message)
            self.report_output.emit(message)

    def _handle_review_summary_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.report_output.emit("Unexpected review-summary result.")
            return
        self.review_summary_updated.emit(dict(result))
        review_dir = str(result.get("review_dir") or "").strip()
        candidate = result.get("candidate_inventory") or {}
        families = result.get("issue_families") or {}
        candidate_count = int(candidate.get("candidate_count", 0) or 0)
        family_count = int(families.get("total_families", 0) or 0)
        message = f"Review summary prepared: {candidate_count} candidate(s), {family_count} issue family/families."
        if review_dir:
            message += f"\nReview sidecars: {review_dir}"
        self.log_message.emit(message)
        self.report_output.emit(message)
        self._emit_current_status()

    def _handle_family_recheck_dry_run_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.report_output.emit("Unexpected family recheck dry-run result.")
            return
        manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
        review_summary = result.get("review_summary") if isinstance(result.get("review_summary"), dict) else {}
        if review_summary:
            self.review_summary_updated.emit(dict(review_summary))
        family_id = str(manifest.get("family_id") or "").strip()
        output_dir = str(manifest.get("output_dir") or "").strip()
        message = f"Family recheck dry run prepared for {family_id or 'selected family'}."
        if output_dir:
            message += f"\nDry-run artifacts: {output_dir}"
        self.log_message.emit(message)
        self.report_output.emit(message)
        self._emit_current_status()

    def _handle_live_family_recheck_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.report_output.emit("Unexpected live family recheck result.")
            return
        manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
        review_summary = result.get("review_summary") if isinstance(result.get("review_summary"), dict) else {}
        if review_summary:
            self.review_summary_updated.emit(dict(review_summary))
        family_id = str(manifest.get("family_id") or "").strip()
        output_dir = str(manifest.get("output_dir") or "").strip()
        live_result = manifest.get("live_result") if isinstance(manifest.get("live_result"), dict) else {}
        response_id = str(live_result.get("response_id") or "").strip()
        message = f"Live family recheck completed for {family_id or 'selected family'}."
        if response_id:
            message += f"\nResponse: {response_id}"
        if output_dir:
            message += f"\nLive artifacts: {output_dir}"
        message += "\nResult was not imported or applied; review it before using Import Accepted Recheck Result."
        self.log_message.emit(message)
        self.report_output.emit(message)
        self._emit_current_status()

    def _handle_imported_recheck_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.report_output.emit("Unexpected issue-family recheck import result.")
            return
        manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
        review_summary = result.get("review_summary") if isinstance(result.get("review_summary"), dict) else {}
        if review_summary:
            self.review_summary_updated.emit(dict(review_summary))
        family_id = str(manifest.get("family_id") or "").strip()
        affected = manifest.get("affected_issue_ids") or []
        issue_preview = ", ".join(str(item) for item in affected[:8])
        if len(affected) > 8:
            issue_preview += ", ..."
        message = f"Accepted issue-family recheck imported for {family_id or 'family'}."
        if issue_preview:
            message += f"\nAffected issues: {issue_preview}"
        message += "\nCanonical issue records were not modified."
        self.log_message.emit(message)
        self.report_output.emit(message)
        self._emit_current_status()

    def _handle_verification_suite_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.report_output.emit("Unexpected verification-suite result.")
            return
        summary = result.get("summary") or {}
        total = int(summary.get("scripts_total", 0) or 0)
        execution = summary.get("execution_summary") or {}
        outcomes = summary.get("mathematical_outcome_summary") or {}
        completed = int(execution.get("completed", 0) or 0)
        technical_errors = int(execution.get("runtime_error", 0) or 0) + int(execution.get("parse_error", 0) or 0)
        timed_out = int(execution.get("timeout", 0) or 0)
        skipped = sum(int(execution.get(key, 0) or 0) for key in ("skipped", "unsafe", "not_run"))
        counterexamples = int(outcomes.get("counterexample_found", 0) or 0)
        claim_failures = int(outcomes.get("claim_failed", 0) or 0)
        unresolved = sum(int(outcomes.get(key, 0) or 0) for key in ("diagnostic_only", "inconclusive", "not_reported"))
        self.log_message.emit(
            "Verification suite finished for currently active scripts. "
            f"Execution: {total} scripts, {completed} completed, {technical_errors} technical errors, "
            f"{timed_out} timed out, {skipped} skipped/unsafe. "
            f"Mathematical outcomes: {counterexamples} counterexamples, {claim_failures} failed claims, "
            f"{unresolved} diagnostic/inconclusive/not reported."
        )
        findings = result.get("verification_findings") or []
        if findings:
            self.log_message.emit(
                f"Verification findings requiring attention: {len(findings)} provisional high-priority finding(s)."
            )
        inventory_warning = result.get("inventory_warning") or {}
        if inventory_warning.get("has_invalidated_obligations"):
            warning_text = str(inventory_warning.get("message") or "").strip()
            if warning_text:
                self.log_message.emit("Verification inventory warning: " + warning_text)
                self.report_output.emit("Verification inventory warning:\n" + warning_text)
        report_paths = result.get("report_paths") or {}
        if report_paths:
            primary_path = self._primary_output_path(report_paths)
            if primary_path:
                self.log_message.emit(f"Verification report written. Primary output: {primary_path}")
            self.report_output.emit(self._format_path_payload("Verification report outputs", report_paths))
            self.report_paths_updated.emit(dict(report_paths))
            self._emit_current_status()
        else:
            self.report_output.emit("Verification suite finished, but no verification report was produced.")
            self._emit_current_status()
        full_report_paths = result.get("full_report_paths") or {}
        concise_report_paths = result.get("concise_report_paths") or {}
        if full_report_paths:
            self.log_message.emit("Full audit report rebuilt with the latest verification outcomes.")
        if concise_report_paths:
            self.log_message.emit("Concise audit report rebuilt with the latest verification outcomes.")
        for warning in result.get("report_generation_warnings") or []:
            self.log_message.emit("Report rebuild warning: " + str(warning))

    def _handle_chatgpt_context_pack_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.report_output.emit("Unexpected ChatGPT context-pack result.")
            return
        export_folder = str(result.get("export_folder") or "").strip()
        copied = result.get("copied_files") or []
        skipped = result.get("skipped_files") or []
        if export_folder:
            self.log_message.emit(f"ChatGPT context pack exported: {export_folder}")
        lines = [
            "ChatGPT context pack export",
            f"- export_folder: {export_folder}",
            "- starter_prompt: available via Copy Starter Prompt",
            f"- audit_context: {result.get('audit_context', '')}",
            f"- paper_structure: {result.get('paper_structure', '')}",
            f"- sidecar_manifest: {result.get('manifest', '')}",
            f"- copied_files: {len(copied)}",
            f"- skipped_files: {len(skipped)}",
            "- workflow: attach the files in the export folder, then paste the copied starter prompt into ChatGPT.",
        ]
        self.report_output.emit("\n".join(lines))
        self.chatgpt_context_pack_exported.emit(dict(result))

    def _handle_rerun_selected_chunks_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.report_output.emit("Unexpected selected-rerun result.")
            return
        chunk_ids = result.get("chunk_ids") or []
        label = ", ".join(str(chunk_id) for chunk_id in chunk_ids) or "selected chunks"
        archive_root = str(result.get("archive_root") or "").strip()
        self.log_message.emit(f"Selected chunk rerun finished for {label}.")
        if archive_root:
            self.log_message.emit(f"Previous chunk evidence archived under: {archive_root}")
        report_paths = result.get("report_paths") or {}
        if report_paths:
            self.report_output.emit(self._format_path_payload("Selected rerun report outputs", report_paths))
        else:
            self.report_output.emit(f"Selected rerun finished for {label}. Reports were not rebuilt.")
        self.status_updated.emit(self._normalize_status_payload(result))
        self._emit_current_status()

    def _handle_failed_verification_rerun_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.report_output.emit("Unexpected failed-verification rerun result.")
            return
        info = result.get("failed_verification_rerun") or {}
        chunk_ids = info.get("chunk_ids") or result.get("chunk_ids") or []
        label = ", ".join(str(chunk_id) for chunk_id in chunk_ids) or "failed-verification chunks"
        archive_root = str(result.get("archive_root") or "").strip()
        self.log_message.emit(f"Failed-verification chunk rerun finished for {label}.")
        if archive_root:
            self.log_message.emit(f"Previous chunk evidence archived under: {archive_root}")
        report_paths = result.get("report_paths") or {}
        if report_paths:
            self.report_output.emit(self._format_path_payload("Failed-verification rerun report outputs", report_paths))
        else:
            self.report_output.emit(f"Failed-verification rerun finished for {label}. Reports were not rebuilt.")
        self.status_updated.emit(self._normalize_status_payload(result))
        self._emit_current_status()

    def _handle_discussion_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.discussion_output.emit("Unexpected discussion result.")
            return
        mode = str(result.get("mode") or "").strip() or "discussion"
        question = str(result.get("question") or "").strip()
        answer = str(result.get("answer") or "").strip()
        response_id = str(result.get("response_id") or "n/a")
        text = "\n".join(
            [
                f"Mode: {mode}",
                f"Question: {question}",
                f"Response ID: {response_id}",
                "",
                answer or "(empty answer)",
            ]
        ).strip()
        self.log_message.emit(f"{mode.capitalize()} response received.")
        self.discussion_output.emit(text)
        self._load_saved_discussion_threads()
        self.poll_status()

    def _format_path_payload(self, title: str, payload: dict[str, Any]) -> str:
        lines = [title]
        for key in sorted(payload):
            lines.append(f"- {key}: {payload.get(key)}")
        return "\n".join(lines)

    @staticmethod
    def _primary_output_path(payload: dict[str, Any]) -> str:
        for key in ("pdf", "tex", "md", "json"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _format_duration(seconds: float) -> str:
        return _format_duration_for_log(seconds)

    def _require_pdf_file(self) -> bool:
        if not self.pdf_path:
            self.log_message.emit("Choose a PDF first.")
            return False
        path = Path(self.pdf_path)
        if not path.exists():
            self.log_message.emit(f"PDF not found: {self.pdf_path}")
            return False
        return True

    def _require_session(self) -> bool:
        if not self._require_pdf_file():
            return False
        if load_session_from_pdf(self.pdf_path) is None:
            self.log_message.emit("No existing audit session was found for this PDF.")
            return False
        return True

    def _require_api_key_for_live_calls(self) -> bool:
        if self.live_api_key_available():
            return True
        self.log_message.emit("Enter an API key before starting/resuming an audit or using discussion.")
        return False

    def live_api_key_available(self) -> bool:
        return bool(self.api_key or os.environ.get("OPENAI_API_KEY"))

    def _load_status_payload(self) -> dict[str, Any]:
        if not self.pdf_path:
            return self._empty_status_payload("no_pdf", "No PDF selected.")
        try:
            info = get_audit_status(self.pdf_path, include_manifest=True)
        except FileNotFoundError:
            return self._empty_status_payload("no_session", "No existing audit session found for this PDF.")
        except Exception as exc:
            return self._empty_status_payload("error", f"Status read failed: {type(exc).__name__}: {exc}")
        try:
            info["failed_verification"] = get_failed_verification_chunks(self.pdf_path)
        except Exception as exc:
            info["failed_verification"] = {
                "chunk_ids": [],
                "chunks": [],
                "summary": {"failed_chunk_count": 0, "failed_result_count": 0},
                "error": f"{type(exc).__name__}: {exc}",
            }
        try:
            info["verification_suite"] = get_verification_suite_status(self.pdf_path)
        except Exception as exc:
            info["verification_suite"] = {
                "scripts_total": 0,
                "scripts": [],
                "last_run": None,
                "inventory_warning": {"has_invalidated_obligations": False},
                "error": f"{type(exc).__name__}: {exc}",
            }
        if review_summary_polling_enabled():
            try:
                info["review_summary"] = load_post_audit_review_summary(self.pdf_path)
            except Exception as exc:
                info["review_summary"] = {
                    "available": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        else:
            info["review_summary"] = {
                "available": False,
                "message": "Experimental Review tab polling is disabled.",
            }
        return self._normalize_status_payload(info)

    def _empty_status_payload(self, status_name: str, message: str) -> dict[str, Any]:
        return {
            "session": None,
            "status": {
                "status": status_name,
                "progress_pct": 0.0,
                "chunks_completed": 0,
                "chunks_total": 0,
                "estimated_pages_completed": 0,
                "estimated_pages_total": 0,
                "cost_usd": 0.0,
                "current_chunk_id": None,
            },
            "usage": {
                "totals": {
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                    "audit_seconds": 0.0,
                }
            },
            "discussion_usage": {
                "turns": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
            },
            "pause": {"requested": False, "requested_at": None},
            "manifest": None,
            "failed_verification": {
                "chunk_ids": [],
                "chunks": [],
                "summary": {"failed_chunk_count": 0, "failed_result_count": 0},
            },
            "verification_suite": {
                "scripts_total": 0,
                "scripts": [],
                "last_run": None,
                "inventory_warning": {"has_invalidated_obligations": False},
            },
            "report_freshness": {
                "reports": {
                    "full": {"status": "missing"},
                    "concise": {"status": "missing"},
                    "verification": {"status": "missing"},
                }
            },
            "review_summary": {"available": False, "message": message},
            "message": message,
            "session_available": False,
            "pdf_selected": bool(self.pdf_path),
        }

    def _normalize_status_payload(self, info: dict[str, Any]) -> dict[str, Any]:
        payload = dict(info)
        payload["session_available"] = payload.get("session") is not None
        payload["pdf_selected"] = bool(self.pdf_path)
        payload.setdefault("report_freshness", {"reports": {}})
        payload.setdefault("review_summary", {"available": False})
        payload.setdefault("message", "")
        return payload

    @staticmethod
    def _chunk_completion_signature(entry: dict[str, Any]) -> tuple[str, str]:
        return (
            str(entry.get("time") or ""),
            str(entry.get("chunk_id") or ""),
        )

    def _reset_chunk_completion_log_state_for_new_audit(self) -> None:
        self._logged_chunk_completion_signatures = set()
        self._chunk_completion_log_pdf_path = self.pdf_path

    def _prime_chunk_completion_log_state(self) -> None:
        self._logged_chunk_completion_signatures = set()
        self._chunk_completion_log_pdf_path = self.pdf_path
        if not self.pdf_path:
            return
        try:
            session = load_session_from_pdf(self.pdf_path)
            if not session:
                return
            usage = load_usage(session)
        except Exception:
            return
        for entry in usage.get("per_chunk", []) or []:
            if isinstance(entry, dict):
                self._logged_chunk_completion_signatures.add(self._chunk_completion_signature(entry))

    def _log_new_chunk_completions(self, payload: dict[str, Any]) -> set[str]:
        if not payload.get("session_available"):
            return set()
        if self._chunk_completion_log_pdf_path != self.pdf_path:
            self._prime_chunk_completion_log_state()
            return set()
        usage = payload.get("usage") or {}
        entries = usage.get("per_chunk", []) or []
        logged_chunk_ids: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            signature = self._chunk_completion_signature(entry)
            if signature in self._logged_chunk_completion_signatures:
                continue
            self._logged_chunk_completion_signatures.add(signature)
            logged_chunk_ids.add(str(entry.get("chunk_id") or ""))
            self.log_message.emit(format_chunk_completion_log_line(payload, entry))
        return logged_chunk_ids

    @staticmethod
    def _is_redundant_post_completion_running_status(
        status: dict[str, Any],
        pause: dict[str, Any],
        logged_chunk_ids: set[str],
    ) -> bool:
        if status.get("status") != "running":
            return False
        if pause.get("requested"):
            return False
        current_chunk_id = str(status.get("current_chunk_id") or "")
        return bool(current_chunk_id and current_chunk_id in logged_chunk_ids)

    def _log_status_change(self, payload: dict[str, Any]) -> None:
        logged_chunk_ids = self._log_new_chunk_completions(payload)
        status = payload.get("status") or {}
        pause = payload.get("pause") or {}
        auto_retry = status.get("last_file_download_timeout_auto_retry") or {}
        if isinstance(auto_retry, dict) and auto_retry:
            auto_retry_signature = (
                auto_retry.get("time"),
                auto_retry.get("chunk_id"),
                auto_retry.get("action"),
                auto_retry.get("attempt"),
            )
            if auto_retry_signature != self._last_auto_retry_signature:
                self._last_auto_retry_signature = auto_retry_signature
                message = str(auto_retry.get("message") or "").strip()
                if message:
                    self.log_message.emit(message)
        signature = (
            status.get("status"),
            status.get("current_chunk_id"),
            status.get("chunks_completed"),
            status.get("chunks_total"),
            bool(pause.get("requested")),
            pause.get("requested_at"),
        )
        if signature == self._last_status_signature:
            return
        self._last_status_signature = signature

        if self._is_redundant_post_completion_running_status(status, pause, logged_chunk_ids):
            return

        if payload.get("session_available"):
            if status.get("status") == "running" and status.get("current_chunk_id") and not pause.get("requested"):
                self.log_message.emit(format_running_chunk_started_log_line(status))
            else:
                self.log_message.emit(
                    f"Status: {status.get('status', 'unknown')} | "
                    f"Chunk: {status.get('current_chunk_id') or '-'} | "
                    f"Progress: {status.get('chunks_completed', 0)}/{status.get('chunks_total', 0)}"
                )
        elif payload.get("message"):
            self.log_message.emit(payload["message"])
