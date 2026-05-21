from __future__ import annotations

import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from audit_chunking import build_auto_chunks, ensure_chunk_display_labels
from audit_prompts import (
    SHIPPED_AUDIT_SYSTEM_PROMPT,
    effective_audit_system_prompt_with_source,
    prompt_snapshot_metadata,
)
from audit_state import (
    _ensure_timing_state as _audit_state_ensure_timing_state,
    append_jsonl,
    compute_usage_cost,
    ensure_workdir_tree,
    format_duration,
    init_state_files,
    load_issues,
    load_json,
    load_ledger,
    load_manifest,
    load_session_from_pdf,
    load_status,
    load_usage,
    save_issues,
    save_json,
    save_ledger,
    save_manifest,
    save_session,
    save_status,
    session_paths,
    update_usage_from_usage_obj,
    usage_cache_diagnostics,
    utc_now,
    workdir_from_pdf,
)
from audit_verification import (
    _chunk_id_from_script_name,
    _chunk_index_from_chunk_id,
    _collect_verification_scripts,
    _load_verification_results,
    _truncate_text,
    _verification_summary_counts,
    load_verification_state,
    run_verification_suite,
    save_verification_state,
)

DEFAULT_MODEL = "gpt-5.5"
DEFAULT_REASONING_EFFORT = "high"
WORKING_STATUSES = {"queued", "in_progress"}
FAILED_VERIFICATION_STATUSES = {"failed", "timeout"}
MODEL_CHOICES = ("gpt-5.5", "gpt-5.5-pro", "gpt-5.4", "gpt-5.4-mini", "gpt-5.2")
LEGACY_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
MODEL_REASONING_EFFORTS = {
    "gpt-5.5": ("none", "low", "medium", "high", "xhigh"),
    "gpt-5.5-pro": ("high",),
    "gpt-5.4-pro": ("medium", "high", "xhigh"),
}
MODEL_DEFAULT_REASONING_EFFORTS = {
    "gpt-5.5": "xhigh",
    "gpt-5.5-pro": "high",
    "gpt-5.4-pro": "high",
}

_OPENAI_CLIENT = None
_PROMPT_BUILDER_HOOK: Optional[Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]] = None
_FINAL_REPORT_BUILDER_HOOK: Optional[Callable[..., dict[str, str]]] = None
_DISPLAY_AUDIT_HOOK: Optional[Callable[[dict[str, Any]], None]] = None
LEGACY_QA_THREAD_ID = "thread_legacy"
QA_CONTEXT_MODES = ("reduced_audit_context", "full_audit_context")
DEFAULT_QA_CONTEXT_MODE = "full_audit_context"
AUDIT_CONTEXT_MODE_CONTINUOUS = "continuous"
AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL = "fresh_context_experimental"
AUDIT_CONTEXT_MODES = (
    AUDIT_CONTEXT_MODE_CONTINUOUS,
    AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL,
)
DEFAULT_AUDIT_CONTEXT_MODE = AUDIT_CONTEXT_MODE_CONTINUOUS
FRESH_CONTEXT_TEXT_FIRST_NOTE = (
    "This audit request uses explicit extracted chunk text and retrieved context instead of accumulated "
    "PDF conversation context. Do not overclaim visual PDF/reference precision."
)
FRESH_CONTEXT_PRIOR_ISSUE_CAUTION = (
    "Prior audit issues are provisional findings. Recheck them when relevant; do not treat them as established facts. "
    "If current context contradicts a prior issue, flag that."
)
FRESH_CONTEXT_PRIORITY_ISSUE_MIN_SCORE = 2
FRESH_CONTEXT_GENERIC_QUERY_TERMS = {
    "asymptotic",
    "asymptotics",
    "bound",
    "bounds",
    "case",
    "cases",
    "coefficient",
    "coefficients",
    "and",
    "all",
    "also",
    "among",
    "any",
    "because",
    "before",
    "begin",
    "between",
    "are",
    "def",
    "define",
    "definition",
    "definitions",
    "end",
    "eqref",
    "equation",
    "equations",
    "error",
    "errors",
    "estimate",
    "estimates",
    "expression",
    "expressions",
    "formula",
    "formulas",
    "function",
    "functions",
    "for",
    "frac",
    "from",
    "given",
    "gives",
    "approx",
    "approximate",
    "approximately",
    "based",
    "gap",
    "ge",
    "has",
    "have",
    "its",
    "label",
    "lambda",
    "le",
    "left",
    "only",
    "order",
    "orders",
    "other",
    "over",
    "page",
    "pages",
    "parameter",
    "parameters",
    "part",
    "parts",
    "pdf",
    "point",
    "points",
    "proof",
    "range",
    "ref",
    "result",
    "results",
    "right",
    "rho",
    "section",
    "sign",
    "sum",
    "term",
    "terms",
    "tex",
    "text",
    "the",
    "this",
    "where",
    "with",
    "value",
    "values",
    "variable",
    "variables",
}
PDF_TEXT_ONLY_RETRY_NOTE = (
    "PDF attachment disabled for this retry due to repeated API file-download timeout. "
    "Rely on extracted chunk text, reference-map/page metadata, and running audit context. "
    "Do not overclaim visual PDF/reference precision."
)
FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_NONE = "none"
FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_REATTACH = "reattach_pdf"
FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_TEXT_ONLY = "text_only_fresh_conversation"
FILE_DOWNLOAD_TIMEOUT_AUTO_RETRY_MAX = 2
FILE_DOWNLOAD_TIMEOUT_AUTO_RETRY_DELAY_SECONDS = 5.0


def _normalize_audit_context_mode(audit_context_mode: Optional[str] = None) -> str:
    clean = str(audit_context_mode or DEFAULT_AUDIT_CONTEXT_MODE).strip()
    if clean not in AUDIT_CONTEXT_MODES:
        raise ValueError(
            "audit_context_mode must be one of: " + ", ".join(AUDIT_CONTEXT_MODES)
        )
    return clean


def _normalize_qa_context_mode(qa_context_mode: Optional[str] = None) -> str:
    clean = str(qa_context_mode or DEFAULT_QA_CONTEXT_MODE).strip()
    if clean not in QA_CONTEXT_MODES:
        raise ValueError(
            "qa_context_mode must be one of: " + ", ".join(QA_CONTEXT_MODES)
        )
    return clean


def _resolve_audit_system_prompt(
    model: Optional[str],
    audit_system_prompt: Optional[str] = None,
    audit_system_prompt_source: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    if audit_system_prompt is not None:
        prompt = str(audit_system_prompt)
        source = str(audit_system_prompt_source or "explicit_override")
    else:
        prompt, source = effective_audit_system_prompt_with_source(model)
    return prompt, prompt_snapshot_metadata(prompt, source, model)


def _ensure_timing_state(session: dict[str, Any]) -> dict[str, Any]:
    return _audit_state_ensure_timing_state(session, default_model=DEFAULT_MODEL)


def _model_family(model: str) -> str:
    clean = str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    for candidate in sorted(MODEL_REASONING_EFFORTS, key=len, reverse=True):
        if clean == candidate or clean.startswith(candidate + "-"):
            return candidate
    return clean


def model_choices() -> list[str]:
    return list(MODEL_CHOICES)


def supported_reasoning_efforts_for_model(model: str) -> list[str]:
    return list(MODEL_REASONING_EFFORTS.get(_model_family(model), LEGACY_REASONING_EFFORTS))


def default_reasoning_effort_for_model(model: str) -> str:
    return MODEL_DEFAULT_REASONING_EFFORTS.get(_model_family(model), DEFAULT_REASONING_EFFORT)


def normalize_model_and_reasoning_effort(
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> tuple[str, str]:
    clean_model = str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    supported = supported_reasoning_efforts_for_model(clean_model)
    default_effort = default_reasoning_effort_for_model(clean_model)
    clean_effort = str(reasoning_effort or "").strip().lower()
    if clean_effort not in supported:
        clean_effort = default_effort
    return clean_model, clean_effort


def set_openai_client(client) -> None:
    global _OPENAI_CLIENT
    _OPENAI_CLIENT = client


def set_live_audit_hooks(
    prompt_builder: Optional[Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]] = None,
    final_report_builder: Optional[Callable[..., dict[str, str]]] = None,
    display_audit: Optional[Callable[[dict[str, Any]], None]] = None,
) -> None:
    global _PROMPT_BUILDER_HOOK, _FINAL_REPORT_BUILDER_HOOK, _DISPLAY_AUDIT_HOOK
    if prompt_builder is not None:
        _PROMPT_BUILDER_HOOK = prompt_builder
    if final_report_builder is not None:
        _FINAL_REPORT_BUILDER_HOOK = final_report_builder
    if display_audit is not None:
        _DISPLAY_AUDIT_HOOK = display_audit


def set_prompt_builder(
    prompt_builder: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]],
) -> None:
    set_live_audit_hooks(prompt_builder=prompt_builder)


def set_final_report_builder(
    final_report_builder: Callable[..., dict[str, str]],
) -> None:
    set_live_audit_hooks(final_report_builder=final_report_builder)


def set_display_audit_hook(
    display_audit: Callable[[dict[str, Any]], None],
) -> None:
    set_live_audit_hooks(display_audit=display_audit)


def _get_client():
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        from openai import OpenAI

        _OPENAI_CLIENT = OpenAI()
    return _OPENAI_CLIENT


def to_jsonable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return json.loads(json.dumps(obj, default=str))


_UNSAFE_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_JSON_ESCAPE_ARTIFACTS = {
    "\x08": r"\\b",
    "\x0c": r"\\f",
}


def _repair_json_escape_artifacts(text: str) -> str:
    text = "" if text is None else str(text)
    for bad, replacement in _JSON_ESCAPE_ARTIFACTS.items():
        text = text.replace(bad, replacement)
    text = text.replace(r"\u000b", " ").replace(r"\u000B", " ")
    return text


def _strip_unsafe_control_chars(text: str) -> str:
    text = "" if text is None else str(text)
    return _UNSAFE_CONTROL_CHAR_RE.sub("", text)


def normalize_math_delimiters(text: str) -> str:
    text = _strip_unsafe_control_chars(_repair_json_escape_artifacts(text))
    text = re.sub(
        r"\\\[(.*?)\\\]",
        lambda m: "$$\n" + m.group(1).strip() + "\n$$",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\\\((.*?)\\\)",
        lambda m: "$" + m.group(1).strip() + "$",
        text,
        flags=re.DOTALL,
    )
    return _strip_unsafe_control_chars(text)


def sanitize_ascii_punctuation(text: str) -> str:
    repl = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "--",
        "\u2014": "---",
        "\u2212": "-",
        "\u2026": "...",
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


def normalize_report_latex_unicode_math(text: str) -> str:
    text = "" if text is None else str(text)
    text = re.sub(r"√\s*\{([^{}]+)\}", r"\\sqrt{\1}", text)
    text = re.sub(r"√\s*\(([^()]+)\)", r"\\sqrt{\1}", text)
    text = re.sub(r"√\s*([A-Za-z0-9]+)", r"\\sqrt{\1}", text)
    text = text.replace("√", r"\sqrt{}")
    repl = {
        "∇": r"\nabla",
        "ρ": r"\rho",
        "λ": r"\lambda",
        "Λ": r"\Lambda",
        "≤": r" \le ",
        "≥": r" \ge ",
        "≪": r" \ll ",
        "≫": r" \gg ",
        "≍": r" \asymp ",
        "≈": r" \approx ",
        "→": r" \to ",
        "∞": r"\infty",
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


_DANGEROUS_MATH_COMMAND_RE = re.compile(
    r"\\(?:usepackage|documentclass|begin|end|input|include|newcommand|renewcommand|providecommand|def|write18|openout|catcode|usetikzlibrary|ref|eqref|autoref|cref|Cref|cite)\b"
)


def _report_escape_text(s: str) -> str:
    s = sanitize_ascii_punctuation("" if s is None else str(s))
    s = normalize_report_latex_unicode_math(s)
    repl = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(repl.get(ch, ch) for ch in s)


def report_latex_paragraph(text: str) -> str:
    text = normalize_math_delimiters("" if text is None else str(text))
    text = _strip_unsafe_control_chars(_repair_json_escape_artifacts(text))
    parts = re.split(r"(\$\$.*?\$\$|\$.*?\$)", text, flags=re.DOTALL)
    out = []
    for part in parts:
        if not part:
            continue
        if (part.startswith("$$") and part.endswith("$$")) or (part.startswith("$") and part.endswith("$")):
            delim = "$$" if part.startswith("$$") else "$"
            body = part[len(delim) : -len(delim)]
            body = sanitize_ascii_punctuation(body)
            body = normalize_report_latex_unicode_math(body)
            if _DANGEROUS_MATH_COMMAND_RE.search(body):
                out.append(r"\texttt{" + _report_escape_text(part) + "}")
            else:
                out.append(delim + body + delim)
        else:
            out.append(_report_escape_text(part))
    return "".join(out)


def _verbatim_block(text: str) -> str:
    text = _strip_unsafe_control_chars(_repair_json_escape_artifacts("" if text is None else str(text))).rstrip()
    if not text:
        return ""
    text = text.replace(r"\end{Verbatim}", r"\string\end{Verbatim}")
    return "\\begin{Verbatim}[fontsize=\\small]\n" + text + "\n\\end{Verbatim}\n"


def _resolve_session(session_or_pdf: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(session_or_pdf, dict):
        session = session_or_pdf
    else:
        session = load_session_from_pdf(session_or_pdf)
    if session is None:
        raise FileNotFoundError("No audit session found for this PDF.")
    return _ensure_timing_state(session)


def _pause_state_from_session(session: dict[str, Any]) -> dict[str, Any]:
    requested_at = str(session.get("pause_requested_at") or "").strip() or None
    return {
        "requested": bool(requested_at),
        "requested_at": requested_at,
    }


def _read_chunk_records(session: dict[str, Any]) -> list[dict[str, Any]]:
    path = session_paths(session["workdir"])["chunk_records"]
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _write_chunk_records(session: dict[str, Any], records: list[dict[str, Any]]) -> None:
    path = session_paths(session["workdir"])["chunk_records"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _parse_freshness_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _freshness_datetime_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _file_mtime_datetime(path: Path) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _json_file_timestamp(path: Path, keys: tuple[str, ...] = ("updated_at", "generated_at", "created_at")) -> Optional[datetime]:
    if not path.exists():
        return None
    try:
        data = load_json(path)
    except Exception:
        return _file_mtime_datetime(path)
    if isinstance(data, dict):
        for key in keys:
            parsed = _parse_freshness_datetime(data.get(key))
            if parsed is not None:
                return parsed
    return _file_mtime_datetime(path)


def _freshness_source(
    name: str,
    timestamp: Optional[datetime],
    path: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    if timestamp is None:
        return None
    item = {
        "name": name,
        "updated_at": _freshness_datetime_iso(timestamp),
    }
    if path is not None:
        item["path"] = str(path)
    return item


def _report_paths_for_kind(session: dict[str, Any], kind: str) -> dict[str, str]:
    root = Path(session["workdir"])
    stem = Path(session["pdf_path"]).stem
    suffixes = {
        "full": "_audit_report",
        "concise": "_concise_audit_report",
        "verification": "_verification_report",
    }
    suffix = suffixes.get(kind)
    if not suffix:
        raise ValueError(f"Unsupported report kind: {kind}")
    report_stem = stem + suffix
    reports_dir = root / "reports"
    return {
        "markdown": str(reports_dir / f"{report_stem}.md"),
        "tex": str(reports_dir / f"{report_stem}.tex"),
        "json": str(reports_dir / f"{report_stem}.json"),
        "folder": str(reports_dir),
    }


def _report_generated_at(paths: dict[str, str]) -> Optional[datetime]:
    json_path = Path(paths.get("json") or "")
    if not json_path.exists():
        return None
    try:
        data = load_json(json_path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return _parse_freshness_datetime(data.get("generated_at"))


def _report_any_output_exists(paths: dict[str, str]) -> bool:
    return any(Path(path).exists() for key, path in paths.items() if key != "folder")


def _verification_content_timestamp(session: dict[str, Any]) -> Optional[datetime]:
    path = session_paths(session["workdir"])["verification_state"]
    if not path.exists():
        return None
    try:
        state = load_json(path)
    except Exception:
        return _file_mtime_datetime(path)
    if not isinstance(state, dict):
        return _file_mtime_datetime(path)

    candidates: list[datetime] = []
    last_run = state.get("last_run")
    if isinstance(last_run, dict):
        for key in ("finished_at", "started_at"):
            parsed = _parse_freshness_datetime(last_run.get(key))
            if parsed is not None:
                candidates.append(parsed)

    if candidates:
        return max(candidates)

    results_dir = Path(session["workdir"]) / "verification_results"
    if results_dir.exists():
        for result_path in results_dir.glob("*.result.json"):
            timestamp = _file_mtime_datetime(result_path)
            if timestamp is not None:
                candidates.append(timestamp)
    if candidates:
        return max(candidates)

    # Only fall back to verification.json's updated_at when no run/result timestamp
    # exists; report-path bookkeeping updates this file after report generation.
    return _json_file_timestamp(path, keys=("updated_at", "generated_at", "created_at"))


def _jsonl_event_timestamp(path: Path) -> Optional[datetime]:
    if not path.exists():
        return None
    candidates: list[datetime] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    parsed = _parse_freshness_datetime(item.get("time"))
                    if parsed is not None:
                        candidates.append(parsed)
    except OSError:
        return None
    if candidates:
        return max(candidates)
    return _file_mtime_datetime(path)


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    entries.append(item)
    except OSError:
        return []
    return entries


def _session_rerun_timestamp(session: dict[str, Any], key: str) -> Optional[datetime]:
    value = session.get(key)
    if not isinstance(value, dict):
        return None
    for timestamp_key in ("finished_at", "updated_at", "started_at"):
        parsed = _parse_freshness_datetime(value.get(timestamp_key))
        if parsed is not None:
            return parsed
    return None


def _report_freshness_sources(session: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    paths = session_paths(session["workdir"])
    root = Path(session["workdir"])
    source_specs: list[tuple[str, Optional[datetime], Optional[Path]]] = []

    if kind in {"full", "concise"}:
        source_specs.extend(
            [
                ("issues state", _json_file_timestamp(paths["issues"]), paths["issues"]),
                ("status state", _json_file_timestamp(paths["status"]), paths["status"]),
                ("chunk manifest", _json_file_timestamp(paths["manifest"]), paths["manifest"]),
                ("chunk records", _file_mtime_datetime(paths["chunk_records"]), paths["chunk_records"]),
                ("ledger state", _json_file_timestamp(paths["ledger"]), paths["ledger"]),
                ("usage state", _json_file_timestamp(paths["usage"]), paths["usage"]),
                (
                    "verification state",
                    _verification_content_timestamp(session),
                    paths["verification_state"],
                ),
                (
                    "selected chunk rerun log",
                    _jsonl_event_timestamp(root / "logs" / "selected_chunk_reruns.jsonl"),
                    root / "logs" / "selected_chunk_reruns.jsonl",
                ),
                (
                    "failed-verification rerun log",
                    _jsonl_event_timestamp(root / "logs" / "failed_verification_chunk_reruns.jsonl"),
                    root / "logs" / "failed_verification_chunk_reruns.jsonl",
                ),
                (
                    "selected chunk rerun state",
                    _session_rerun_timestamp(session, "last_selected_rerun"),
                    paths["session"],
                ),
                (
                    "failed-verification rerun state",
                    _session_rerun_timestamp(session, "last_failed_verification_rerun"),
                    paths["session"],
                ),
            ]
        )
    elif kind == "verification":
        source_specs.extend(
            [
                (
                    "verification state",
                    _verification_content_timestamp(session),
                    paths["verification_state"],
                ),
                (
                    "selected chunk rerun log",
                    _jsonl_event_timestamp(root / "logs" / "selected_chunk_reruns.jsonl"),
                    root / "logs" / "selected_chunk_reruns.jsonl",
                ),
                (
                    "failed-verification rerun log",
                    _jsonl_event_timestamp(root / "logs" / "failed_verification_chunk_reruns.jsonl"),
                    root / "logs" / "failed_verification_chunk_reruns.jsonl",
                ),
            ]
        )

    sources = []
    for name, timestamp, path in source_specs:
        item = _freshness_source(name, timestamp, path)
        if item is not None:
            sources.append(item)
    return sources


def _report_freshness_for_kind(session: dict[str, Any], kind: str) -> dict[str, Any]:
    paths = _report_paths_for_kind(session, kind)
    sources = _report_freshness_sources(session, kind)
    generated_at = _report_generated_at(paths)
    latest_source = None
    if sources:
        latest_source = max(
            sources,
            key=lambda item: _parse_freshness_datetime(item.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc),
        )
    latest_source_at = _parse_freshness_datetime(latest_source.get("updated_at")) if latest_source else None

    if not _report_any_output_exists(paths):
        status = "missing"
        reason = "No generated report files were found."
    elif generated_at is None:
        status = "unknown"
        reason = "Report exists, but its JSON sidecar is missing generated_at metadata."
    elif latest_source_at is not None and latest_source_at > generated_at:
        status = "stale"
        source_name = str(latest_source.get("name") or "audit state") if latest_source else "audit state"
        reason = f"{source_name} changed after this report was generated."
    else:
        status = "current"
        reason = "Report generated_at is newer than tracked audit state sources."

    return {
        "status": status,
        "reason": reason,
        "generated_at": _freshness_datetime_iso(generated_at),
        "latest_source_at": _freshness_datetime_iso(latest_source_at),
        "latest_source": latest_source,
        "paths": paths,
        "sources": sources,
    }


def get_report_freshness(session_or_pdf: dict[str, Any] | str | Path) -> dict[str, Any]:
    session = _resolve_session(session_or_pdf)
    return {
        "computed_at": utc_now(),
        "reports": {
            "full": _report_freshness_for_kind(session, "full"),
            "concise": _report_freshness_for_kind(session, "concise"),
            "verification": _report_freshness_for_kind(session, "verification"),
        },
    }


def get_audit_status(
    session_or_pdf: dict[str, Any] | str | Path,
    include_manifest: bool = False,
) -> dict[str, Any]:
    session = _resolve_session(session_or_pdf)
    session = _ensure_qa_thread_state(session)
    pause = _pause_state_from_session(session)
    status = dict(load_status(session))
    status["pause_requested"] = pause["requested"]
    status["pause_requested_at"] = pause["requested_at"]
    usage = load_usage(session)
    if not status.get("last_chunk_usage_diagnostics"):
        for entry in reversed(usage.get("per_chunk", []) or []):
            if not isinstance(entry, dict) or not entry.get("usage"):
                continue
            diagnostics = entry.get("usage_diagnostics") or usage_cache_diagnostics(entry.get("usage") or {})
            status["last_chunk_usage_diagnostics"] = {
                "chunk_id": entry.get("chunk_id"),
                "cost_usd": (entry.get("cost") or {}).get("total_cost"),
                **diagnostics,
            }
            break
    active_qa_turns = _load_qa_turns(session, active_thread_only=True)
    payload = {
        "session": session,
        "status": status,
        "usage": usage,
        "discussion_usage": _qa_usage_summary_from_turns(active_qa_turns),
        "discussion_thread": _qa_thread_summary(session),
        "report_freshness": get_report_freshness(session),
        "pause": pause,
    }
    if include_manifest:
        payload["manifest"] = ensure_chunk_display_labels(load_manifest(session))
    return payload


def request_pause(
    session_or_pdf: dict[str, Any] | str | Path,
    include_manifest: bool = False,
) -> dict[str, Any]:
    session = _resolve_session(session_or_pdf)
    if not str(session.get("pause_requested_at") or "").strip():
        session["pause_requested_at"] = utc_now()
        session["updated_at"] = utc_now()
        save_session(session)
    return get_audit_status(session, include_manifest=include_manifest)


def clear_pause_request(
    session_or_pdf: dict[str, Any] | str | Path,
    include_manifest: bool = False,
) -> dict[str, Any]:
    session = _resolve_session(session_or_pdf)
    if "pause_requested_at" in session:
        session.pop("pause_requested_at", None)
        session["updated_at"] = utc_now()
        save_session(session)
    return get_audit_status(session, include_manifest=include_manifest)


def cancel_pending_response_for_retry(
    session_or_pdf: dict[str, Any] | str | Path,
    include_manifest: bool = False,
) -> dict[str, Any]:
    session = _resolve_session(session_or_pdf)
    pending = session.get("pending") or {}
    if not pending:
        raise RuntimeError("This audit session has no pending response to cancel.")
    response_id = str(pending.get("response_id") or "").strip()
    chunk_id = str(pending.get("chunk_id") or "").strip()
    if not response_id or not chunk_id:
        raise RuntimeError(f"Pending response is missing response_id or chunk_id: {pending}")

    manifest = load_manifest(session)
    matches = [chunk for chunk in manifest.get("chunks", []) if chunk.get("chunk_id") == chunk_id]
    if not matches:
        raise RuntimeError(f"Pending chunk {chunk_id!r} was not found in the chunk manifest.")
    chunk = matches[0]
    chunk_index = int(chunk.get("chunk_index") or session.get("next_chunk_index") or 1)

    client = _get_client()
    cancel_response = client.responses.cancel(response_id)
    cancel_json = to_jsonable(cancel_response)

    now = utc_now()
    root = Path(session["workdir"])
    cancel_response_path = root / "responses" / f"{chunk_id}_{response_id}.cancel.json"
    save_json(cancel_response_path, cancel_json)

    event = {
        "time": now,
        "action": "cancel_pending_response_for_retry",
        "chunk_id": chunk_id,
        "chunk_index": chunk_index,
        "response_id": response_id,
        "cancel_status": getattr(cancel_response, "status", None) or cancel_json.get("status"),
        "pending_created_at": pending.get("created_at"),
        "pending_started_at": pending.get("started_at"),
        "request_path": pending.get("request_path"),
        "cancel_response_path": str(cancel_response_path),
        "note": "Cancelled a stale pending background response so Resume Audit can resubmit this chunk.",
    }
    append_jsonl(root / "logs" / "pending_response_cancellations.jsonl", event)

    cancelled_pending = dict(pending)
    cancelled_pending.update({
        "cancelled_at": now,
        "cancel_status": event["cancel_status"],
        "cancel_response_path": str(cancel_response_path),
    })
    if session.get("pause_requested_at"):
        session["last_pause_requested_at"] = session.get("pause_requested_at")
        session.pop("pause_requested_at", None)
    session["last_cancelled_pending"] = cancelled_pending
    session["last_response_id"] = response_id
    session["pending"] = None
    if session.get("pdf_file_id"):
        session["pdf_attached_in_conversation"] = False
    session["next_chunk_index"] = chunk_index
    session["updated_at"] = now
    save_session(session)

    status = load_status(session)
    status.update({
        "status": "paused",
        "pause_reason": "cancelled_pending_response",
        "paused_at": now,
        "current_chunk_id": chunk_id,
        "current_chunk_elapsed_seconds": 0.0,
    })
    save_status(session, status)

    payload = get_audit_status(session, include_manifest=include_manifest)
    payload["cancel_event"] = event
    return payload


def _normalize_rerun_chunk_ids(chunk_ids: Any, manifest: dict[str, Any]) -> list[str]:
    raw_items = chunk_ids if isinstance(chunk_ids, (list, tuple, set)) else [chunk_ids]
    tokens: list[str] = []
    for item in raw_items:
        for token in re.split(r"[\s,]+", str(item or "").strip()):
            if token:
                tokens.append(token)
    if not tokens:
        raise ValueError("Provide at least one chunk id or chunk index to rerun.")

    chunks = manifest.get("chunks", []) or []
    by_id = {str(chunk.get("chunk_id") or ""): chunk for chunk in chunks}
    by_id_lower = {chunk_id.lower(): chunk_id for chunk_id in by_id if chunk_id}
    by_index: dict[int, str] = {}
    for chunk in chunks:
        try:
            by_index[int(chunk.get("chunk_index"))] = str(chunk.get("chunk_id") or "")
        except Exception:
            continue

    selected: list[str] = []
    seen = set()
    invalid: list[str] = []
    for token in tokens:
        clean = token.strip()
        chunk_id = ""
        lower = clean.lower()
        if lower in by_id_lower:
            chunk_id = by_id_lower[lower]
        else:
            match = re.match(r"^chunk_(\d+)$", lower)
            if match:
                chunk_id = by_index.get(int(match.group(1)), "")
            elif clean.isdigit():
                chunk_id = by_index.get(int(clean), "")
        if not chunk_id:
            invalid.append(clean)
            continue
        if chunk_id not in seen:
            seen.add(chunk_id)
            selected.append(chunk_id)

    if invalid:
        raise ValueError(f"Unknown chunk id(s): {', '.join(invalid)}")
    if not selected:
        raise ValueError("No valid chunks were selected for rerun.")
    return selected


def _archive_file_for_rerun(root: Path, archive_root: Path, path: Path, remove: bool = False) -> Optional[str]:
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None
    try:
        rel = path.resolve().relative_to(root.resolve())
    except Exception:
        rel = Path(path.name)
    dest = archive_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    if remove:
        path.unlink()
    return str(dest)


def _archive_state_snapshots_for_rerun(session: dict[str, Any], archive_root: Path) -> list[dict[str, str]]:
    root = Path(session["workdir"])
    paths = session_paths(session["workdir"])
    archived = []
    for key in ["session", "status", "usage", "manifest", "chunk_records", "issues", "ledger", "verification_state"]:
        archived_path = _archive_file_for_rerun(root, archive_root, paths[key], remove=False)
        if archived_path:
            archived.append({"kind": key, "archive_path": archived_path})
    return archived


def _archive_chunk_artifacts_for_rerun(session: dict[str, Any], chunk_id: str, archive_root: Path) -> list[dict[str, str]]:
    root = Path(session["workdir"])
    patterns = [
        ("prompts", f"{chunk_id}_prompt.json"),
        ("requests", f"{chunk_id}_*.request.json"),
        ("responses", f"{chunk_id}.structured.json"),
        ("responses", f"{chunk_id}.md"),
        ("responses", f"{chunk_id}_*"),
        ("latex_patches", f"{chunk_id}_patch_*.tex"),
        ("python_checks", f"{chunk_id}_check_*.py"),
        ("verification_results", f"{chunk_id}_check_*.result.json"),
    ]
    archived: list[dict[str, str]] = []
    seen: set[str] = set()
    for subdir, pattern in patterns:
        folder = root / subdir
        if not folder.exists():
            continue
        for path in sorted(folder.glob(pattern)):
            if not path.is_file():
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            archived_path = _archive_file_for_rerun(root, archive_root / chunk_id, path, remove=True)
            if archived_path:
                archived.append({"source_path": str(path), "archive_path": archived_path})
    return archived


def _ledger_contributions_from_records(records: list[dict[str, Any]]) -> dict[str, set[str]]:
    out = {"assumptions": set(), "notes": set()}
    for rec in records:
        path_text = str(rec.get("structured_response_path") or "").strip()
        if not path_text:
            continue
        try:
            audit = load_json(path_text)
        except Exception:
            continue
        ledger = audit.get("ledger_updates") if isinstance(audit, dict) else None
        if not isinstance(ledger, dict):
            continue
        for key in ["assumptions", "notes"]:
            for item in ledger.get(key, []) or []:
                text = str(item or "").strip()
                if text:
                    out[key].add(text)
    return out


def _cleanup_ledger_for_rerun(
    session: dict[str, Any],
    removed_records: list[dict[str, Any]],
    remaining_records: list[dict[str, Any]],
) -> dict[str, list[str]]:
    removed = _ledger_contributions_from_records(removed_records)
    remaining = _ledger_contributions_from_records(remaining_records)
    ledger = load_ledger(session)
    removed_items: dict[str, list[str]] = {"assumptions": [], "notes": []}
    changed = False
    for key in ["assumptions", "notes"]:
        remove_values = removed[key] - remaining[key]
        if not remove_values:
            continue
        current = list(ledger.get(key, []) or [])
        kept = []
        for item in current:
            if item in remove_values:
                removed_items[key].append(item)
                changed = True
            else:
                kept.append(item)
        ledger[key] = kept
    if changed:
        save_ledger(session, ledger)
    return removed_items


def _verification_result_matches_chunks(item: dict[str, Any], chunk_ids: set[str]) -> bool:
    chunk_id = str(item.get("chunk_id") or "").strip()
    if chunk_id in chunk_ids:
        return True
    script_name = Path(str(item.get("script_name") or item.get("script_path") or "")).name
    return any(script_name.startswith(f"{selected}_check_") for selected in chunk_ids)


def _cleanup_verification_state_for_rerun(session: dict[str, Any], chunk_ids: set[str]) -> dict[str, Any]:
    state = load_verification_state(session)
    original_results = list(state.get("results", []) or [])
    kept = []
    removed = []
    for item in original_results:
        if _verification_result_matches_chunks(item, chunk_ids):
            removed.append(item)
        else:
            kept.append(item)
    if len(kept) != len(original_results):
        state["results"] = kept
        if state.get("last_run"):
            counts = _verification_summary_counts(kept)
            state["last_run"].update(counts)
            state["last_run"]["selected_chunk_results_removed_by_rerun"] = sorted(chunk_ids)
        save_verification_state(session, state)
    return {
        "removed_result_count": len(removed),
        "removed_results": removed,
    }


def _replace_active_state_for_rerun(
    session: dict[str, Any],
    chunk_ids: set[str],
    old_records: list[dict[str, Any]],
    all_records: list[dict[str, Any]],
) -> dict[str, Any]:
    remaining_records = [rec for rec in all_records if str(rec.get("chunk_id") or "") not in chunk_ids]
    _write_chunk_records(session, remaining_records)

    issues_state = load_issues(session)
    old_issues = list(issues_state.get("issues", []) or [])
    kept_issues = [issue for issue in old_issues if str(issue.get("chunk_id") or "") not in chunk_ids]
    removed_issues = [issue for issue in old_issues if str(issue.get("chunk_id") or "") in chunk_ids]
    if len(kept_issues) != len(old_issues):
        issues_state["issues"] = kept_issues
        save_issues(session, issues_state)

    return {
        "removed_chunk_record_count": len(old_records),
        "removed_issue_count": len(removed_issues),
        "removed_issue_ids": [str(issue.get("issue_id") or "") for issue in removed_issues if issue.get("issue_id")],
        "removed_ledger_items": _cleanup_ledger_for_rerun(session, old_records, remaining_records),
        "removed_verification_results": _cleanup_verification_state_for_rerun(session, chunk_ids),
    }


def _sort_active_chunk_records(session: dict[str, Any]) -> None:
    records = _read_chunk_records(session)
    records.sort(key=lambda rec: (int(rec.get("chunk_index") or 10**9), str(rec.get("chunk_id") or "")))
    _write_chunk_records(session, records)


def _compact_failed_verification_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": str(result.get("chunk_id") or "").strip(),
        "chunk_index": result.get("chunk_index"),
        "script_name": str(result.get("script_name") or "").strip(),
        "script_path": str(result.get("script_path") or "").strip(),
        "status": str(result.get("status") or "").strip(),
        "returncode": result.get("returncode"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "conclusion": _truncate_text(str(result.get("conclusion") or ""), limit=500),
        "stdout_excerpt": _truncate_text(str(result.get("stdout") or ""), limit=1000),
        "stderr_excerpt": _truncate_text(str(result.get("stderr") or ""), limit=1000),
        "skip_reason": _truncate_text(str(result.get("skip_reason") or ""), limit=500),
        "result_path": str(result.get("result_path") or "").strip(),
    }


def _verification_result_script_name(item: dict[str, Any]) -> str:
    return Path(str(item.get("script_name") or item.get("script_path") or "")).name


def _verification_inventory_warning(session: dict[str, Any]) -> dict[str, Any]:
    """Detect verification obligations archived by failed chunk reruns.

    This is intentionally conservative: if a failed rerun removed verification
    results and the corresponding script name is not present in the current
    active inventory, the GUI/report should avoid saying the whole historical
    verification inventory is clean.
    """
    root = Path(session["workdir"])
    events = _read_jsonl_dicts(root / "logs" / "selected_chunk_reruns.jsonl")
    if not events:
        return {"has_invalidated_obligations": False}

    active_scripts = _collect_verification_scripts(session)
    active_script_names = {
        str(item.get("script_name") or "").strip()
        for item in active_scripts
        if str(item.get("script_name") or "").strip()
    }
    active_results = _load_verification_results(session, state=load_verification_state(session))
    active_result_script_names = {
        _verification_result_script_name(item)
        for item in active_results
        if _verification_result_script_name(item)
    }
    active_chunk_ids = {
        str(record.get("chunk_id") or "").strip()
        for record in _read_chunk_records(session)
        if str(record.get("chunk_id") or "").strip()
    }

    finished_rerun_ids = {
        str(item.get("rerun_id") or "").strip()
        for item in events
        if item.get("action") == "finished" and str(item.get("rerun_id") or "").strip()
    }
    finished_rerun_chunks = {
        str(chunk_id).strip()
        for item in events
        if item.get("action") == "finished"
        for chunk_id in (item.get("chunk_ids") or [])
        if str(chunk_id).strip()
    }
    invalidated_by_name: dict[str, dict[str, Any]] = {}
    failed_rerun_ids: set[str] = set()
    affected_chunks: set[str] = set()
    removed_result_count = 0

    for event in events:
        if event.get("action") != "failed":
            continue
        rerun_id = str(event.get("rerun_id") or "").strip()
        if rerun_id and rerun_id in finished_rerun_ids:
            continue
        replacement = event.get("replacement_summary") or {}
        removed_info = replacement.get("removed_verification_results") or {}
        removed_results = removed_info.get("removed_results") or []
        if not isinstance(removed_results, list) or not removed_results:
            continue
        if rerun_id:
            failed_rerun_ids.add(rerun_id)
        for item in removed_results:
            if not isinstance(item, dict):
                continue
            removed_result_count += 1
            script_name = _verification_result_script_name(item)
            chunk_id = str(item.get("chunk_id") or "").strip() or _chunk_id_from_script_name(script_name)
            if chunk_id:
                affected_chunks.add(chunk_id)
            if not script_name:
                continue
            active_now = script_name in active_script_names or script_name in active_result_script_names
            if active_now:
                continue
            replacement_state = (
                "chunk_rerun_finished_but_script_missing"
                if chunk_id and chunk_id in finished_rerun_chunks
                else "chunk_still_needs_successful_rerun"
            )
            invalidated_by_name[script_name] = {
                "script_name": script_name,
                "chunk_id": chunk_id,
                "status": str(item.get("status") or "").strip(),
                "result_path": str(item.get("result_path") or "").strip(),
                "rerun_id": rerun_id,
                "chunk_record_active": chunk_id in active_chunk_ids if chunk_id else False,
                "replacement_state": replacement_state,
            }

    invalidated = sorted(
        invalidated_by_name.values(),
        key=lambda item: (_chunk_index_from_chunk_id(str(item.get("chunk_id") or "")), str(item.get("script_name") or "")),
    )
    if not invalidated:
        return {
            "has_invalidated_obligations": False,
            "active_scripts_total": len(active_scripts),
        }

    invalidated_chunks = sorted(
        {str(item.get("chunk_id") or "") for item in invalidated if str(item.get("chunk_id") or "")},
        key=lambda chunk_id: (_chunk_index_from_chunk_id(chunk_id), chunk_id),
    )
    rerun_missing_scripts = [
        item for item in invalidated if item.get("replacement_state") == "chunk_rerun_finished_but_script_missing"
    ]
    needs_rerun_scripts = [
        item for item in invalidated if item.get("replacement_state") == "chunk_still_needs_successful_rerun"
    ]
    rerun_missing_chunks = sorted(
        {str(item.get("chunk_id") or "") for item in rerun_missing_scripts if str(item.get("chunk_id") or "")},
        key=lambda chunk_id: (_chunk_index_from_chunk_id(chunk_id), chunk_id),
    )
    needs_rerun_chunks = sorted(
        {str(item.get("chunk_id") or "") for item in needs_rerun_scripts if str(item.get("chunk_id") or "")},
        key=lambda chunk_id: (_chunk_index_from_chunk_id(chunk_id), chunk_id),
    )
    detail_sentences: list[str] = []
    if needs_rerun_scripts:
        noun = "script still needs" if len(needs_rerun_scripts) == 1 else "scripts still need"
        detail_sentences.append(
            f"{len(needs_rerun_scripts)} {noun} a successful replacement chunk rerun"
        )
    if rerun_missing_scripts:
        noun = "is" if len(rerun_missing_scripts) == 1 else "are"
        detail_sentences.append(
            f"{len(rerun_missing_scripts)} {noun} from chunks that were rerun but did not regenerate equivalent active scripts"
        )
    detail = "; ".join(detail_sentences)
    detail_clause = f"{detail[0].upper()}{detail[1:]}. " if detail else ""
    message = (
        f"{len(invalidated)} archived/invalidated verification script(s) from earlier rerun activity "
        f"are not represented in the currently active verification suite. Affected chunks: {', '.join(invalidated_chunks[:10])}"
        f"{', ...' if len(invalidated_chunks) > 10 else ''}. "
        f"{detail_clause}"
        "Active-suite pass counts describe only currently active verification scripts, not the full historical verification inventory."
    )
    return {
        "has_invalidated_obligations": True,
        "active_scripts_total": len(active_scripts),
        "active_results_total": len(active_results),
        "removed_result_count": removed_result_count,
        "invalidated_script_count": len(invalidated),
        "affected_chunk_count": len(invalidated_chunks),
        "affected_chunks": invalidated_chunks,
        "needs_rerun_script_count": len(needs_rerun_scripts),
        "needs_rerun_chunks": needs_rerun_chunks,
        "rerun_missing_script_count": len(rerun_missing_scripts),
        "rerun_missing_chunks": rerun_missing_chunks,
        "failed_rerun_count": len(failed_rerun_ids),
        "failed_rerun_ids": sorted(failed_rerun_ids),
        "message": message,
        "invalidated_scripts": invalidated[:50],
    }


def get_failed_verification_chunks(session_or_pdf: dict[str, Any] | str | Path) -> dict[str, Any]:
    session = _resolve_session(session_or_pdf)
    state = load_verification_state(session)
    results = _load_verification_results(session, state=state)
    grouped: dict[str, dict[str, Any]] = {}
    for result in results:
        status = str(result.get("status") or "").strip().lower()
        if status not in FAILED_VERIFICATION_STATUSES:
            continue
        chunk_id = str(result.get("chunk_id") or "").strip()
        if not chunk_id:
            continue
        entry = grouped.setdefault(
            chunk_id,
            {
                "chunk_id": chunk_id,
                "chunk_index": result.get("chunk_index"),
                "results": [],
                "result_paths": [],
            },
        )
        compact = _compact_failed_verification_result(result)
        entry["results"].append(compact)
        if compact.get("result_path"):
            entry["result_paths"].append(compact["result_path"])
        if entry.get("chunk_index") in (None, "") and compact.get("chunk_index") not in (None, ""):
            entry["chunk_index"] = compact.get("chunk_index")

    chunks = list(grouped.values())
    chunks.sort(key=lambda item: (int(item.get("chunk_index") or 10**9), str(item.get("chunk_id") or "")))
    return {
        "session": session,
        "chunk_ids": [item["chunk_id"] for item in chunks],
        "chunks": chunks,
        "summary": {
            "failed_chunk_count": len(chunks),
            "failed_result_count": sum(len(item.get("results", []) or []) for item in chunks),
            "statuses": sorted(FAILED_VERIFICATION_STATUSES),
        },
        "verification_state": state,
    }


def _format_failed_verification_context(chunk_summary: dict[str, Any]) -> str:
    lines = [
        "Verification rerun context for this chunk:",
        "These verification results failed or timed out. Treat this as additional evidence, not automatic truth.",
    ]
    for result in chunk_summary.get("results", []) or []:
        lines.extend(
            [
                "",
                f"- Script: {result.get('script_name') or '(unknown script)'}",
                f"  Status: {result.get('status') or 'unknown'}",
                f"  Return code: {result.get('returncode')}",
            ]
        )
        if result.get("conclusion"):
            lines.append(f"  Conclusion: {result.get('conclusion')}")
        if result.get("stdout_excerpt"):
            lines.append(f"  Stdout excerpt: {result.get('stdout_excerpt')}")
        if result.get("stderr_excerpt"):
            lines.append(f"  Stderr excerpt: {result.get('stderr_excerpt')}")
        if result.get("result_path"):
            lines.append(f"  Result artifact: {result.get('result_path')}")
    return "\n".join(lines).strip()


def rerun_selected_chunks(
    session_or_pdf: dict[str, Any] | str | Path,
    chunk_ids: Any,
    extra_rerun_instruction: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    rebuild_reports: bool = True,
    replace_existing: bool = True,
    poll_every: float = 3.0,
    max_wait_seconds: Optional[float] = None,
    display_output: bool = False,
    _per_chunk_extra_rerun_instructions: Optional[dict[str, str]] = None,
    _rerun_kind: str = "selected_chunk",
) -> dict[str, Any]:
    if not replace_existing:
        raise NotImplementedError("replace_existing=False is not supported in the V1 selective rerun flow.")

    session = _resolve_session(session_or_pdf)
    status_before = load_status(session)
    if session.get("pending"):
        raise RuntimeError("Cannot rerun selected chunks while the session has a pending response.")
    if str(status_before.get("status") or "") == "running":
        raise RuntimeError("Cannot rerun selected chunks while the audit status is running.")

    manifest = load_manifest(session)
    chunks = manifest.get("chunks", []) or []
    if not chunks:
        raise RuntimeError("Chunk manifest is empty.")
    selected_chunk_ids = _normalize_rerun_chunk_ids(chunk_ids, manifest)
    selected_set = set(selected_chunk_ids)
    chunks_by_id = {str(chunk.get("chunk_id") or ""): chunk for chunk in chunks}

    if model is not None or reasoning_effort is not None:
        selected_model = model if model is not None else session.get("model")
        selected_effort = reasoning_effort if reasoning_effort is not None else session.get("reasoning_effort")
        selected_model, selected_effort = normalize_model_and_reasoning_effort(selected_model, selected_effort)
        session["model"] = selected_model
        session["reasoning_effort"] = selected_effort
    else:
        selected_model, selected_effort = normalize_model_and_reasoning_effort(
            session.get("model") or DEFAULT_MODEL,
            session.get("reasoning_effort"),
        )
        session["model"] = selected_model
        session["reasoning_effort"] = selected_effort
    session["updated_at"] = utc_now()
    save_session(session)

    started_at = utc_now()
    rerun_id = "rerun_" + _artifact_timestamp_token()
    root = Path(session["workdir"])
    rerun_root = root / "reruns" / rerun_id
    rerun_root.mkdir(parents=True, exist_ok=True)
    log_path = root / "logs" / "selected_chunk_reruns.jsonl"

    all_records = _read_chunk_records(session)
    old_records = [rec for rec in all_records if str(rec.get("chunk_id") or "") in selected_set]
    old_records_by_chunk: dict[str, list[dict[str, Any]]] = {chunk_id: [] for chunk_id in selected_chunk_ids}
    for record in old_records:
        old_records_by_chunk.setdefault(str(record.get("chunk_id") or ""), []).append(record)

    archived_state_paths = _archive_state_snapshots_for_rerun(session, rerun_root)
    replacement_summary = _replace_active_state_for_rerun(session, selected_set, old_records, all_records)
    archived_chunk_paths: dict[str, list[dict[str, str]]] = {}
    for chunk_id in selected_chunk_ids:
        archived_chunk_paths[chunk_id] = _archive_chunk_artifacts_for_rerun(session, chunk_id, rerun_root)

    start_event = {
        "time": started_at,
        "action": "started",
        "rerun_id": rerun_id,
        "chunk_ids": selected_chunk_ids,
        "model": session.get("model"),
        "reasoning_effort": session.get("reasoning_effort"),
        "extra_rerun_instruction": str(extra_rerun_instruction or "").strip(),
        "archive_root": str(rerun_root),
        "archived_state_paths": archived_state_paths,
        "archived_chunk_paths": archived_chunk_paths,
        "replacement_summary": replacement_summary,
        "rebuild_reports": bool(rebuild_reports),
    }
    append_jsonl(log_path, start_event)

    original_next_chunk_index = int(session.get("next_chunk_index") or len(chunks) + 1)
    original_audit_finished_at = session.get("audit_finished_at")
    original_active_chunk_ids = {
        str(record.get("chunk_id") or "").strip()
        for record in all_records
        if str(record.get("chunk_id") or "").strip()
    }
    base_audit_was_complete = (
        str(status_before.get("status") or "").strip().lower() == "completed"
        or bool(original_audit_finished_at)
        or (bool(chunks) and len(original_active_chunk_ids) >= len(chunks))
    )
    results = []
    report_paths: dict[str, str] = {}
    try:
        for chunk_id in selected_chunk_ids:
            session = _resolve_session(session["pdf_path"])
            chunk = dict(chunks_by_id[chunk_id])
            instruction_parts = []
            generic_instruction = str(extra_rerun_instruction or "").strip()
            if generic_instruction:
                instruction_parts.append(generic_instruction)
            per_chunk_instruction = ""
            if isinstance(_per_chunk_extra_rerun_instructions, dict):
                per_chunk_instruction = str(_per_chunk_extra_rerun_instructions.get(chunk_id) or "").strip()
            if per_chunk_instruction:
                instruction_parts.append(per_chunk_instruction)
            if instruction_parts:
                chunk["_extra_rerun_instruction"] = "\n\n".join(instruction_parts)
            chunk["_rerun_id"] = rerun_id
            chunk["_rerun_kind"] = str(_rerun_kind or "selected_chunk")
            chunk["_fresh_rerun_conversation"] = True
            chunk["_rerun_requested_at"] = started_at
            previous_records = old_records_by_chunk.get(chunk_id) or []
            previous_mode = str((previous_records[-1] if previous_records else {}).get("verification_mode") or "local_python_only")
            previous_mode = _normalize_verification_mode(previous_mode)
            if previous_mode == "code_interpreter" and not session.get("use_code_interpreter", False):
                previous_mode = "local_python_only"
            result = process_one_chunk(
                session,
                chunk,
                poll_every=poll_every,
                max_wait_seconds=max_wait_seconds,
                display_output=display_output,
                verification_mode=previous_mode,
            )
            results.append({
                "chunk_id": chunk_id,
                "response_id": result.get("record", {}).get("response_id"),
                "record": result.get("record"),
            })

        session = _resolve_session(session["pdf_path"])
        _sort_active_chunk_records(session)
        usage = load_usage(session)
        session["next_chunk_index"] = len(chunks) + 1 if base_audit_was_complete else original_next_chunk_index
        session["pending"] = None
        if base_audit_was_complete:
            session["audit_finished_at"] = original_audit_finished_at or utc_now()
        else:
            session["audit_finished_at"] = original_audit_finished_at
        session["last_selected_rerun"] = {
            "rerun_id": rerun_id,
            "chunk_ids": selected_chunk_ids,
            "finished_at": utc_now(),
        }
        session["updated_at"] = utc_now()
        save_session(session)

        restored_status = dict(status_before)
        restored_status["cost_usd"] = float(usage["totals"].get("cost_usd", 0.0) or 0.0)
        restored_status["total_audit_seconds"] = float(usage["totals"].get("audit_seconds", 0.0) or 0.0)
        restored_status["current_chunk_id"] = None
        restored_status["current_chunk_elapsed_seconds"] = 0.0
        restored_status["updated_at"] = utc_now()
        if base_audit_was_complete:
            restored_status.update({
                "status": "completed",
                "progress_pct": 100.0,
                "chunks_completed": len(chunks),
                "chunks_total": len(chunks),
                "estimated_pages_completed": manifest.get("pdf_page_count", restored_status.get("estimated_pages_completed", 0)),
                "estimated_pages_total": manifest.get("pdf_page_count", restored_status.get("estimated_pages_total", 0)),
                "audit_finished_at": session.get("audit_finished_at"),
            })
        save_status(session, restored_status)

        if rebuild_reports:
            report_paths = build_final_report(session)

        finish_event = {
            "time": utc_now(),
            "action": "finished",
            "rerun_id": rerun_id,
            "chunk_ids": selected_chunk_ids,
            "model": session.get("model"),
            "reasoning_effort": session.get("reasoning_effort"),
            "extra_rerun_instruction": str(extra_rerun_instruction or "").strip(),
            "archive_root": str(rerun_root),
            "archived_state_paths": archived_state_paths,
            "archived_chunk_paths": archived_chunk_paths,
            "replacement_summary": replacement_summary,
            "report_paths": report_paths,
            "rerun_results": results,
        }
        append_jsonl(log_path, finish_event)
    except Exception as exc:
        append_jsonl(
            log_path,
            {
                "time": utc_now(),
                "action": "failed",
                "rerun_id": rerun_id,
                "chunk_ids": selected_chunk_ids,
                "model": session.get("model"),
                "reasoning_effort": session.get("reasoning_effort"),
                "extra_rerun_instruction": str(extra_rerun_instruction or "").strip(),
                "archive_root": str(rerun_root),
                "archived_state_paths": archived_state_paths,
                "archived_chunk_paths": archived_chunk_paths,
                "replacement_summary": replacement_summary,
                "error": repr(exc),
            },
        )
        raise

    return {
        "session": load_session_from_pdf(session["pdf_path"]) or session,
        "status": load_status(session),
        "usage": load_usage(session),
        "rerun_id": rerun_id,
        "chunk_ids": selected_chunk_ids,
        "archive_root": str(rerun_root),
        "archived_state_paths": archived_state_paths,
        "archived_chunk_paths": archived_chunk_paths,
        "replacement_summary": replacement_summary,
        "rerun_results": results,
        "report_paths": report_paths,
    }


def rerun_failed_verification_chunks(
    session_or_pdf: dict[str, Any] | str | Path,
    chunk_ids: Any = None,
    include_verification_output: bool = True,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    rebuild_reports: bool = True,
) -> dict[str, Any]:
    session = _resolve_session(session_or_pdf)
    manifest = load_manifest(session)
    failed_info = get_failed_verification_chunks(session)
    failed_by_chunk = {str(item.get("chunk_id") or ""): item for item in failed_info.get("chunks", []) or []}
    failed_chunk_ids = [chunk_id for chunk_id in failed_info.get("chunk_ids", []) or [] if chunk_id]
    if not failed_chunk_ids:
        raise RuntimeError("No failed or timed-out verification results were found.")

    if chunk_ids is None or (isinstance(chunk_ids, str) and not chunk_ids.strip()):
        selected_chunk_ids = failed_chunk_ids
    else:
        selected_chunk_ids = _normalize_rerun_chunk_ids(chunk_ids, manifest)
        invalid = [chunk_id for chunk_id in selected_chunk_ids if chunk_id not in failed_by_chunk]
        if invalid:
            raise ValueError(
                "Selected chunk(s) do not currently have failed/timed-out verification results: "
                + ", ".join(invalid)
            )

    context_map = {}
    if include_verification_output:
        context_map = {
            chunk_id: _format_failed_verification_context(failed_by_chunk[chunk_id])
            for chunk_id in selected_chunk_ids
        }

    selected_failures = {
        chunk_id: failed_by_chunk[chunk_id]
        for chunk_id in selected_chunk_ids
    }
    trigger_artifacts = {
        chunk_id: [str(path) for path in (failed_by_chunk[chunk_id].get("result_paths") or []) if path]
        for chunk_id in selected_chunk_ids
    }
    log_path = Path(session["workdir"]) / "logs" / "failed_verification_chunk_reruns.jsonl"
    start_event = {
        "time": utc_now(),
        "action": "started",
        "chunk_ids": selected_chunk_ids,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "include_verification_output": bool(include_verification_output),
        "rebuild_reports": bool(rebuild_reports),
        "triggering_verification_result_paths": trigger_artifacts,
        "failed_verification": selected_failures,
    }
    append_jsonl(log_path, start_event)
    try:
        result = rerun_selected_chunks(
            session,
            selected_chunk_ids,
            model=model,
            reasoning_effort=reasoning_effort,
            rebuild_reports=bool(rebuild_reports),
            _per_chunk_extra_rerun_instructions=context_map,
            _rerun_kind="failed_verification",
        )
    except Exception as exc:
        append_jsonl(
            log_path,
            {
                "time": utc_now(),
                "action": "failed",
                "chunk_ids": selected_chunk_ids,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "include_verification_output": bool(include_verification_output),
                "triggering_verification_result_paths": trigger_artifacts,
                "error": repr(exc),
            },
        )
        raise

    finish_event = {
        "time": utc_now(),
        "action": "finished",
        "chunk_ids": selected_chunk_ids,
        "rerun_id": result.get("rerun_id"),
        "archive_root": result.get("archive_root"),
        "archived_chunk_paths": result.get("archived_chunk_paths"),
        "report_paths": result.get("report_paths"),
        "include_verification_output": bool(include_verification_output),
        "triggering_verification_result_paths": trigger_artifacts,
        "failed_verification": selected_failures,
    }
    append_jsonl(log_path, finish_event)
    result["failed_verification_rerun"] = {
        "chunk_ids": selected_chunk_ids,
        "include_verification_output": bool(include_verification_output),
        "triggering_verification_result_paths": trigger_artifacts,
        "archived_chunk_paths": result.get("archived_chunk_paths"),
        "failed_verification": selected_failures,
        "log_path": str(log_path),
    }
    return result


def build_final_report(
    session_or_pdf: dict[str, Any] | str | Path,
    report_title: Optional[str] = None,
    include_verification_summary_in_final_report: Optional[bool] = None,
    write_separate_verification_report: Optional[bool] = None,
    report_reference_style: Optional[str] = None,
) -> dict[str, str]:
    if _FINAL_REPORT_BUILDER_HOOK is None:
        raise RuntimeError(
            "No final report builder hook is registered. "
            "Call set_live_audit_hooks(final_report_builder=...) before using build_final_report()."
        )
    kwargs: dict[str, Any] = {}
    if report_title is not None:
        kwargs["report_title"] = report_title
    if include_verification_summary_in_final_report is not None:
        kwargs["include_verification_summary_in_final_report"] = include_verification_summary_in_final_report
    if write_separate_verification_report is not None:
        kwargs["write_separate_verification_report"] = write_separate_verification_report
    if report_reference_style is not None:
        kwargs["report_reference_style"] = report_reference_style
    return _FINAL_REPORT_BUILDER_HOOK(session_or_pdf, **kwargs)


def build_concise_report(
    session_or_pdf: dict[str, Any] | str | Path,
    report_title: Optional[str] = None,
    options: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    from audit_policy_hooks import build_concise_report as policy_build_concise_report

    kwargs: dict[str, Any] = {}
    if report_title is not None:
        kwargs["report_title"] = report_title
    if options is not None:
        kwargs["options"] = options
    return policy_build_concise_report(session_or_pdf, **kwargs)


def _display_verification_script_path(session: dict[str, Any], result: dict[str, Any]) -> str:
    script_path_text = str(result.get("script_path") or "").strip()
    if not script_path_text:
        return str(result.get("script_name") or "")
    script_path = Path(script_path_text)
    root = Path(session["workdir"]).resolve()
    try:
        return str(script_path.resolve().relative_to(root))
    except Exception:
        return script_path.name or script_path_text


def _verification_conclusion_for_display(result: dict[str, Any], limit: int = 160) -> str:
    text = _strip_unsafe_control_chars(_repair_json_escape_artifacts(result.get("conclusion") or "No conclusion available."))
    return _truncate_text(text, limit=limit).replace("\\n", " ")


def _verification_report_markdown(
    session: dict[str, Any],
    state: dict[str, Any],
    results: list[dict[str, Any]],
    inventory_warning: Optional[dict[str, Any]] = None,
) -> str:
    counts = _verification_summary_counts(results)
    last_run = state.get("last_run") or {}
    inventory_warning = inventory_warning or {"has_invalidated_obligations": False}
    title = Path(session["pdf_path"]).stem
    lines = [
        f"# Verification report -- {title}",
        "",
        f"- PDF: {session['pdf_path']}",
        f"- Workdir: {session['workdir']}",
        f"- Python interpreter: {last_run.get('python_executable', sys.executable)}",
        f"- Safe only mode: {last_run.get('safe_only', True)}",
        f"- Timeout per script: {last_run.get('timeout_seconds', 0)}s",
        f"- Currently active scripts run: {counts['scripts_total']}",
        f"- Passed: {counts['passed']}",
        f"- Failed: {counts['failed']}",
        f"- Timed out: {counts['timeout']}",
        f"- Skipped: {counts['skipped']}",
        "",
    ]
    if inventory_warning.get("has_invalidated_obligations"):
        lines.extend(
            [
                "## Verification inventory warning",
                "",
                "The counts above refer only to currently active verification scripts.",
                str(inventory_warning.get("message") or "").strip(),
                f"- Active scripts: {int(inventory_warning.get('active_scripts_total', counts['scripts_total']) or 0)}",
                f"- Archived/invalidated scripts: {int(inventory_warning.get('invalidated_script_count', 0) or 0)}",
                f"- Affected chunks: {', '.join(inventory_warning.get('affected_chunks') or [])}",
                "",
            ]
        )
    if not results:
        lines.append("No verification results found.")
        return "\n".join(lines).strip() + "\n"
    for result in results:
        lines.extend(
            [
                f"## {result.get('script_name','script')} [{result.get('status','unknown')}]",
                f"- Chunk: {result.get('chunk_id') or 'unknown'}",
                f"- Script: {_display_verification_script_path(session, result)}",
                f"- Return code: {result.get('returncode')}",
                f"- Elapsed time: {format_duration(result.get('elapsed_seconds', 0.0))}",
                f"- Conclusion: {_verification_conclusion_for_display(result)}",
            ]
        )
        if result.get("skip_reason"):
            lines.append(f"- Skip reason: {result.get('skip_reason')}")
        stdout_excerpt = _truncate_text(result.get("stdout", ""))
        if stdout_excerpt:
            lines.extend(["- Stdout excerpt:", "```text", stdout_excerpt, "```"])
        stderr_excerpt = _truncate_text(result.get("stderr", ""))
        if stderr_excerpt:
            lines.extend(["- Stderr excerpt:", "```text", stderr_excerpt, "```"])
        lines.append("")
    return _strip_unsafe_control_chars("\n".join(lines).strip() + "\n")


def _verification_report_tex(
    session: dict[str, Any],
    state: dict[str, Any],
    results: list[dict[str, Any]],
    inventory_warning: Optional[dict[str, Any]] = None,
) -> str:
    counts = _verification_summary_counts(results)
    last_run = state.get("last_run") or {}
    inventory_warning = inventory_warning or {"has_invalidated_obligations": False}
    title = report_latex_paragraph(f"Verification report -- {Path(session['pdf_path']).stem}")
    parts = [
        r"""\documentclass[11pt]{article}
\usepackage[a4paper,margin=1in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{microtype}
\usepackage{hyperref}
\usepackage{enumitem}
\usepackage{xcolor}
\usepackage{fancyvrb}
\setlist[itemize]{leftmargin=2em}
\setlength{\parskip}{0.5em}
\setlength{\parindent}{0pt}
\begin{document}
"""
    ]
    parts.append(r"\section*{" + title + "}" + "\n")
    parts.append(r"\begin{itemize}" + "\n")
    parts.append(r"\item PDF: " + report_latex_paragraph(session["pdf_path"]) + "\n")
    parts.append(r"\item Workdir: " + report_latex_paragraph(session["workdir"]) + "\n")
    parts.append(r"\item Python interpreter: " + report_latex_paragraph(str(last_run.get("python_executable", sys.executable))) + "\n")
    parts.append(r"\item Safe only mode: " + report_latex_paragraph(str(last_run.get("safe_only", True))) + "\n")
    parts.append(r"\item Timeout per script: " + str(last_run.get("timeout_seconds", 0)) + "s\n")
    parts.append(r"\item Currently active scripts run: " + str(counts["scripts_total"]) + "\n")
    parts.append(r"\item Passed: " + str(counts["passed"]) + "\n")
    parts.append(r"\item Failed: " + str(counts["failed"]) + "\n")
    parts.append(r"\item Timed out: " + str(counts["timeout"]) + "\n")
    parts.append(r"\item Skipped: " + str(counts["skipped"]) + "\n")
    parts.append(r"\end{itemize}" + "\n")
    if inventory_warning.get("has_invalidated_obligations"):
        parts.append(r"\section*{Verification inventory warning}" + "\n")
        parts.append(
            report_latex_paragraph(
                "The counts above refer only to currently active verification scripts. "
                + str(inventory_warning.get("message") or "").strip()
            )
            + "\n"
        )
        parts.append(r"\begin{itemize}" + "\n")
        parts.append(r"\item Active scripts: " + str(int(inventory_warning.get("active_scripts_total", counts["scripts_total"]) or 0)) + "\n")
        parts.append(r"\item Archived/invalidated scripts: " + str(int(inventory_warning.get("invalidated_script_count", 0) or 0)) + "\n")
        parts.append(r"\item Affected chunks: " + report_latex_paragraph(", ".join(inventory_warning.get("affected_chunks") or [])) + "\n")
        parts.append(r"\end{itemize}" + "\n")
    if not results:
        parts.append("No verification results found.\n")
    else:
        for result in results:
            heading = report_latex_paragraph(f"{result.get('script_name','script')} [{result.get('status','unknown')}]")
            parts.append(r"\subsection*{" + heading + "}" + "\n")
            parts.append(r"\begin{itemize}" + "\n")
            parts.append(r"\item Chunk: " + report_latex_paragraph(result.get("chunk_id") or "unknown") + "\n")
            parts.append(r"\item Script: " + report_latex_paragraph(_display_verification_script_path(session, result)) + "\n")
            parts.append(r"\item Return code: " + report_latex_paragraph(str(result.get("returncode"))) + "\n")
            parts.append(r"\item Elapsed time: " + report_latex_paragraph(format_duration(result.get("elapsed_seconds", 0.0))) + "\n")
            parts.append(r"\item Conclusion: " + report_latex_paragraph(_verification_conclusion_for_display(result)) + "\n")
            if result.get("skip_reason"):
                parts.append(r"\item Skip reason: " + report_latex_paragraph(result.get("skip_reason")) + "\n")
            parts.append(r"\end{itemize}" + "\n")
            stdout_excerpt = _truncate_text(result.get("stdout", ""))
            if stdout_excerpt:
                parts.append(r"\paragraph{Stdout excerpt}" + "\n")
                parts.append(_verbatim_block(stdout_excerpt) + "\n")
            stderr_excerpt = _truncate_text(result.get("stderr", ""))
            if stderr_excerpt:
                parts.append(r"\paragraph{Stderr excerpt}" + "\n")
                parts.append(_verbatim_block(stderr_excerpt) + "\n")
    parts.append(r"\end{document}" + "\n")
    return _strip_unsafe_control_chars("".join(parts))


def build_verification_report(session_or_pdf: dict[str, Any] | str | Path) -> dict[str, str]:
    session = _resolve_session(session_or_pdf)
    state = load_verification_state(session)
    results = _load_verification_results(session, state=state)
    inventory_warning = _verification_inventory_warning(session)
    if not state.get("last_run") and not results:
        return {}
    root = Path(session["workdir"])
    report_stem = Path(session["pdf_path"]).stem + "_verification_report"
    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"{report_stem}.md"
    tex_path = reports_dir / f"{report_stem}.tex"
    json_path = reports_dir / f"{report_stem}.json"
    md_path.write_text(_verification_report_markdown(session, state, results, inventory_warning), encoding="utf-8")
    tex_path.write_text(_verification_report_tex(session, state, results, inventory_warning), encoding="utf-8")
    save_json(
        json_path,
        {
            "session": load_session_from_pdf(session["pdf_path"]),
            "verification_state": state,
            "results": results,
            "inventory_warning": inventory_warning,
            "generated_at": utc_now(),
        },
    )
    state["report_paths"] = {
        "markdown": str(md_path),
        "tex": str(tex_path),
        "json": str(json_path),
    }
    save_verification_state(session, state)
    return dict(state["report_paths"])


def run_verification_suite_and_build_report(
    session_or_pdf: dict[str, Any] | str | Path,
    timeout: Optional[int] = None,
    safe_only: bool = True,
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    session = _resolve_session(session_or_pdf)
    effective_timeout = int(timeout if timeout is not None else session.get("verification_timeout_seconds", 120))
    verification_run = run_verification_suite(
        session["pdf_path"],
        timeout=effective_timeout,
        safe_only=bool(safe_only),
        progress_callback=progress_callback,
    )
    report_paths = build_verification_report(session)
    inventory_warning = _verification_inventory_warning(session)
    return {
        "session": load_session_from_pdf(session["pdf_path"]) or session,
        "summary": verification_run.get("summary", {}),
        "report_paths": report_paths,
        "state": verification_run.get("state", {}),
        "inventory_warning": inventory_warning,
    }


def get_verification_suite_status(session_or_pdf: dict[str, Any] | str | Path) -> dict[str, Any]:
    session = _resolve_session(session_or_pdf)
    scripts = _collect_verification_scripts(session)
    state = load_verification_state(session)
    inventory_warning = _verification_inventory_warning(session)
    return {
        "scripts_total": len(scripts),
        "scripts": [
            {
                "chunk_id": item.get("chunk_id"),
                "chunk_index": item.get("chunk_index"),
                "script_name": item.get("script_name"),
                "script_path": item.get("script_path"),
            }
            for item in scripts
        ],
        "last_run": state.get("last_run") if isinstance(state.get("last_run"), dict) else None,
        "inventory_warning": inventory_warning,
    }


def _normalize_chatgpt_context_pack_options(options: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    source = options if isinstance(options, dict) else {}
    preset = str(source.get("preset") or "chatgpt_handoff").strip().lower()
    if preset not in {"chatgpt_handoff", "full_archive"}:
        preset = "chatgpt_handoff"
    context_depth = str(source.get("context_depth") or "full_audit_context").strip()
    if context_depth not in QA_CONTEXT_MODES:
        context_depth = "full_audit_context"
    default_report_formats = ["md", "tex", "pdf", "json"] if preset == "full_archive" else ["md"]
    raw_report_formats = source.get("report_file_formats", default_report_formats)
    if isinstance(raw_report_formats, str):
        raw_formats = [raw_report_formats]
    elif isinstance(raw_report_formats, list):
        raw_formats = raw_report_formats
    else:
        raw_formats = default_report_formats
    report_file_formats: list[str] = []
    for item in raw_formats:
        fmt = str(item or "").strip().lower().lstrip(".")
        if fmt in {"md", "tex", "pdf", "json"} and fmt not in report_file_formats:
            report_file_formats.append(fmt)
    if not report_file_formats:
        report_file_formats = list(default_report_formats)
    return {
        "preset": preset,
        "include_pdf": bool(source.get("include_pdf", True)),
        "include_tex": bool(source.get("include_tex", True)),
        "include_concise_report": bool(source.get("include_concise_report", True)),
        "include_full_report": bool(source.get("include_full_report", preset == "full_archive")),
        "include_verification_report": bool(source.get("include_verification_report", True)),
        "context_depth": context_depth,
        "report_file_formats": report_file_formats,
    }


def _chatgpt_export_report_paths(
    session: dict[str, Any],
    report_kind: str,
    formats: Optional[list[str]] = None,
) -> list[Path]:
    reports_dir = Path(session["workdir"]) / "reports"
    stem = Path(session["pdf_path"]).stem
    suffixes = {
        "full": "_audit_report",
        "concise": "_concise_audit_report",
        "verification": "_verification_report",
    }
    report_suffix = suffixes.get(report_kind)
    if not report_suffix:
        return []
    base = reports_dir / f"{stem}{report_suffix}"
    selected_formats = formats or ["md"]
    return [
        base.with_suffix(f".{fmt}")
        for fmt in selected_formats
        if fmt in {"md", "tex", "pdf", "json"} and base.with_suffix(f".{fmt}").exists()
    ]


def _unique_chatgpt_export_dir(root: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = root / "exports" / f"chatgpt_context_pack_{timestamp}"
    candidate = base
    counter = 2
    while candidate.exists():
        candidate = Path(f"{base}_{counter:02d}")
        counter += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _chatgpt_context_pack_starter_prompt(
    session: dict[str, Any],
    options: dict[str, Any],
    copied_files: Optional[list[dict[str, str]]] = None,
) -> str:
    copied = copied_files or []

    def names_for(label: str) -> list[str]:
        names = []
        for item in copied:
            if item.get("label") != label:
                continue
            destination = str(item.get("destination") or "").strip()
            if destination:
                names.append(Path(destination).name)
        return names

    attached_lines = []
    paper_names = names_for("paper_pdf")
    tex_names = names_for("tex_source")
    report_names = (
        names_for("concise_report")
        + names_for("full_report")
        + names_for("verification_report")
    )
    if paper_names:
        attached_lines.append(f"- the paper PDF (`{paper_names[0]}`) as the primary source of truth,")
    attached_lines.extend(
        [
            "- `audit_context.md` as a structured summary of the audit context,",
            "- `paper_structure.json` as a structured map of notation, assumptions, issues, verification, and rerun context,",
        ]
    )
    if tex_names:
        attached_lines.append(f"- the TeX source (`{tex_names[0]}`) as supporting source material,")
    if report_names:
        attached_lines.append(
            "- selected audit report files as supporting evidence: "
            + ", ".join(f"`{name}`" for name in report_names)
            + "."
        )

    instructions = []
    if paper_names:
        instructions.append(
            "Treat the paper PDF as the main source for mathematical claims, notation, statements, and proofs."
        )
    instructions.extend(
        [
            "Use `audit_context.md` as a compact guide to the audit findings, dependencies, verification results, and rerun outcomes.",
            "Use `paper_structure.json` for structured lookup of chunks, notation/assumptions, issue impact, verification metadata, and rerun summaries.",
        ]
    )
    if tex_names or report_names:
        instructions.append("Treat TeX files and audit reports as supporting evidence, not as automatic truth.")
    instructions.extend(
        [
            "Distinguish clearly between what the paper itself states, what the audit concluded, and what remains uncertain or needs fresh checking.",
            "If you think the audit may be mistaken on a point, say so explicitly and explain what in the paper would resolve it.",
            "When discussing mathematical issues, focus on substantive correctness, dependencies, assumptions, and impact on major claims rather than minor editorial matters.",
            "If useful, begin by briefly stating which attached files you are relying on most for the answer.",
        ]
    )

    lines = [
        "I want to continue work on a completed mathematical paper audit.",
        "",
        "I am attaching:",
        *attached_lines,
        "",
        "Please use the materials as follows:",
    ]
    lines.extend(f"{idx}. {instruction}" for idx, instruction in enumerate(instructions, start=1))
    lines.extend(["", "My question is:", "[Type your question here.]"])
    return "\n".join(lines) + "\n"


def _build_paper_structure_context(session: dict[str, Any]) -> dict[str, Any]:
    """Build an export-only structured map from saved audit artifacts."""

    def clean_text(value: Any, limit: Optional[int] = None) -> str:
        text = _strip_unsafe_control_chars(_repair_json_escape_artifacts("" if value is None else str(value))).strip()
        return _truncate_text(text, limit=limit) if limit else text

    def page_range(source: dict[str, Any]) -> dict[str, Any]:
        return {
            "page_start": source.get("page_start"),
            "page_end": source.get("page_end"),
        }

    def read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        entries = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(raw)
            except Exception:
                continue
            if isinstance(item, dict):
                entries.append(item)
        return entries

    def summarize_rerun_log(path: Path) -> list[dict[str, Any]]:
        summaries = []
        for item in read_jsonl(path):
            chunk_ids = item.get("chunk_ids") or item.get("chunks") or []
            if not isinstance(chunk_ids, list):
                chunk_ids = [chunk_ids]
            summary = {
                "time": item.get("time"),
                "action": item.get("action"),
                "rerun_id": item.get("rerun_id"),
                "chunk_ids": [str(chunk_id) for chunk_id in chunk_ids if str(chunk_id).strip()],
                "model": item.get("model"),
                "reasoning_effort": item.get("reasoning_effort"),
                "extra_rerun_instruction": clean_text(item.get("extra_rerun_instruction"), limit=500),
                "archive_root": item.get("archive_root"),
                "report_paths": item.get("report_paths") or {},
            }
            if item.get("triggering_verification_results"):
                summary["triggering_verification_results"] = item.get("triggering_verification_results")
            summaries.append(summary)
        return summaries

    result_ref_pattern = re.compile(
        r"(?i)\b(theorem|lemma|proposition|corollary|definition|equation|eq\.?|section)\s*"
        r"(?:\(|\[)?([A-Za-z]?\d+(?:\.\d+)*(?:[a-z])?)(?:\)|\])?"
    )
    latex_label_pattern = re.compile(r"\\label\{([^}]+)\}")
    result_refs: list[dict[str, Any]] = []
    seen_refs: set[tuple[str, str, str, str]] = set()

    def add_result_refs(
        text: Any,
        *,
        source_type: str,
        source_id: str,
        chunk_id: str = "",
        source_page_start: Any = None,
        source_page_end: Any = None,
    ) -> None:
        body = clean_text(text)
        if not body:
            return
        for match in result_ref_pattern.finditer(body):
            kind = match.group(1).lower().replace(".", "")
            label = match.group(2)
            key = (kind, label, source_type, source_id)
            if key in seen_refs:
                continue
            seen_refs.add(key)
            start = max(0, match.start() - 90)
            end = min(len(body), match.end() + 90)
            result_refs.append(
                {
                    "reference_type": kind,
                    "reference": label,
                    "source_type": source_type,
                    "source_id": source_id,
                    "chunk_id": chunk_id,
                    "page_start": source_page_start,
                    "page_end": source_page_end,
                    "extraction": "heuristic_text_match",
                    "context_snippet": clean_text(body[start:end], limit=220),
                }
            )
        for match in latex_label_pattern.finditer(body):
            label = match.group(1)
            key = ("latex_label", label, source_type, source_id)
            if key in seen_refs:
                continue
            seen_refs.add(key)
            result_refs.append(
                {
                    "reference_type": "latex_label",
                    "reference": label,
                    "source_type": source_type,
                    "source_id": source_id,
                    "chunk_id": chunk_id,
                    "page_start": source_page_start,
                    "page_end": source_page_end,
                    "extraction": "heuristic_text_match",
                    "context_snippet": clean_text(body[max(0, match.start() - 90) : min(len(body), match.end() + 90)], limit=220),
                }
            )

    status = load_status(session)
    usage = load_usage(session)
    manifest = load_manifest(session)
    ledger = load_ledger(session)
    issues_state = load_issues(session)
    records = _read_chunk_records(session)
    manifest_chunks = manifest.get("chunks") if isinstance(manifest.get("chunks"), list) else []
    records_by_id = {str(rec.get("chunk_id") or ""): rec for rec in records if str(rec.get("chunk_id") or "")}
    issues = [issue for issue in (issues_state.get("issues") or []) if isinstance(issue, dict)]
    issues_by_chunk: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        issues_by_chunk.setdefault(str(issue.get("chunk_id") or ""), []).append(issue)

    chunks = []
    notation_and_assumptions = []
    chunk_sources = records if records else manifest_chunks
    for source in chunk_sources:
        chunk_id = str(source.get("chunk_id") or "").strip()
        if not chunk_id:
            continue
        issue_ids = list(source.get("issue_ids") or [])
        if not issue_ids:
            issue_ids = [str(issue.get("issue_id")) for issue in issues_by_chunk.get(chunk_id, []) if issue.get("issue_id")]
        chunks.append(
            {
                "chunk_id": chunk_id,
                "chunk_index": source.get("chunk_index"),
                "label": clean_text(source.get("label")),
                "boundary": clean_text(source.get("boundary")),
                "page_start": source.get("page_start"),
                "page_end": source.get("page_end"),
                "issue_ids": issue_ids,
                "source": "state/chunks.jsonl" if records else "state/chunk_manifest.json",
            }
        )

        rec = records_by_id.get(chunk_id, source)
        audit = _load_chunk_audit_for_qa(session, rec)
        for idx, item in enumerate(audit.get("assumptions_and_notation") or [], start=1):
            text = clean_text(item)
            if not text:
                continue
            notation_and_assumptions.append(
                {
                    "entry_id": f"{chunk_id}_assumption_{idx:03d}",
                    "text": text,
                    "chunk_id": chunk_id,
                    **page_range(source),
                    "source": "structured_chunk_response.assumptions_and_notation",
                    "provenance": "explicit_structured_field",
                }
            )
            add_result_refs(
                text,
                source_type="assumptions_and_notation",
                source_id=f"{chunk_id}_assumption_{idx:03d}",
                chunk_id=chunk_id,
                source_page_start=source.get("page_start"),
                source_page_end=source.get("page_end"),
            )
        for idx, item in enumerate(audit.get("verified_steps") or [], start=1):
            add_result_refs(
                item,
                source_type="verified_step",
                source_id=f"{chunk_id}_verified_{idx:03d}",
                chunk_id=chunk_id,
                source_page_start=source.get("page_start"),
                source_page_end=source.get("page_end"),
            )

    issue_impact = []
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for issue in sorted(
        issues,
        key=lambda item: (
            severity_rank.get(str(item.get("severity") or "medium").lower(), 2),
            str(item.get("chunk_id") or ""),
            str(item.get("issue_id") or ""),
        ),
    ):
        chunk_id = str(issue.get("chunk_id") or "")
        rec = records_by_id.get(chunk_id, {})
        source_text = "\n".join(
            clean_text(issue.get(key))
            for key in ("title", "location", "description", "evidence", "proposed_fix")
            if clean_text(issue.get(key))
        )
        add_result_refs(
            source_text,
            source_type="issue",
            source_id=str(issue.get("issue_id") or ""),
            chunk_id=chunk_id,
            source_page_start=rec.get("page_start"),
            source_page_end=rec.get("page_end"),
        )
        issue_impact.append(
            {
                "issue_id": str(issue.get("issue_id") or ""),
                "status": str(issue.get("status") or "open"),
                "severity": str(issue.get("severity") or "medium"),
                "chunk_id": chunk_id,
                "page_start": rec.get("page_start"),
                "page_end": rec.get("page_end"),
                "location": clean_text(issue.get("location")),
                "title": clean_text(issue.get("title")),
                "description": clean_text(issue.get("description")),
                "evidence": clean_text(issue.get("evidence")),
                "proposed_fix": clean_text(issue.get("proposed_fix")),
                "tags": list(issue.get("tags") or []),
                "affected_references": [
                    {
                        "reference_type": ref["reference_type"],
                        "reference": ref["reference"],
                        "extraction": ref["extraction"],
                    }
                    for ref in result_refs
                    if ref.get("source_type") == "issue" and ref.get("source_id") == str(issue.get("issue_id") or "")
                ],
                "impact_mapping": "source-derived summary; affected references are heuristic text matches when present",
            }
        )

    ledger_assumptions = [clean_text(item) for item in (ledger.get("assumptions") or []) if clean_text(item)]
    ledger_notes = [clean_text(item) for item in (ledger.get("notes") or []) if clean_text(item)]
    for idx, item in enumerate(ledger_assumptions[:400], start=1):
        add_result_refs(item, source_type="ledger_assumption", source_id=f"ledger_assumption_{idx:03d}")
    for idx, item in enumerate(ledger_notes[:400], start=1):
        add_result_refs(item, source_type="ledger_note", source_id=f"ledger_note_{idx:03d}")

    try:
        verification_state = load_verification_state(session)
        verification_results = _load_verification_results(session, verification_state)
        verification_counts = _verification_summary_counts(verification_results)
    except Exception:
        verification_state = {}
        verification_results = []
        verification_counts = {}
    failed_verification = []
    for item in verification_results:
        status_text = str(item.get("status") or "").lower()
        if status_text not in {"failed", "timeout", "timed_out"}:
            continue
        failed_verification.append(
            {
                "chunk_id": item.get("chunk_id"),
                "chunk_index": item.get("chunk_index"),
                "script_name": item.get("script_name"),
                "status": item.get("status"),
                "returncode": item.get("returncode"),
                "elapsed_seconds": item.get("elapsed_seconds"),
                "conclusion": clean_text(item.get("conclusion"), limit=700),
                "stdout": clean_text(item.get("stdout"), limit=700),
                "stderr": clean_text(item.get("stderr"), limit=700),
                "result_path": item.get("result_path"),
                "script_path": item.get("script_path"),
            }
        )

    logs_dir = Path(session["workdir"]) / "logs"
    reruns = {
        "selected_chunk_reruns": summarize_rerun_log(logs_dir / "selected_chunk_reruns.jsonl"),
        "failed_verification_chunk_reruns": summarize_rerun_log(logs_dir / "failed_verification_chunk_reruns.jsonl"),
    }

    return {
        "metadata": {
            "generated_at": utc_now(),
            "source": "saved_audit_artifacts_only",
            "pdf_name": Path(str(session.get("pdf_path") or "")).name,
            "pdf_path": session.get("pdf_path"),
            "workdir": session.get("workdir"),
            "audit_status": status.get("status"),
            "model": session.get("model"),
            "reasoning_effort": session.get("reasoning_effort"),
            "chunks_completed": status.get("chunks_completed"),
            "chunks_total": status.get("chunks_total"),
            "usage_totals": usage.get("totals") or {},
            "reliability_notes": [
                "Notation and assumptions come from saved structured chunk audit fields.",
                "Ledger entries are global saved audit notes and do not necessarily have precise local provenance.",
                "Result references are heuristic text matches unless an explicit structured field says otherwise.",
                "This file is an export artifact only; it does not modify audit state.",
            ],
        },
        "chunks": chunks,
        "notation_and_assumptions": notation_and_assumptions,
        "ledger": {
            "source": "state/ledger.json",
            "provenance": "global_ledger_no_precise_local_provenance",
            "assumptions": ledger_assumptions,
            "notes": ledger_notes,
        },
        "result_references": result_refs,
        "issue_impact": issue_impact,
        "verification": {
            "source": "state/verification.json",
            "summary": verification_counts or verification_state.get("last_run") or {},
            "failed_or_timed_out_checks": failed_verification,
        },
        "reruns": {
            "source": "logs/*.jsonl",
            "selected_chunk_reruns": reruns["selected_chunk_reruns"],
            "failed_verification_chunk_reruns": reruns["failed_verification_chunk_reruns"],
        },
    }


def export_chatgpt_context_pack(
    session_or_pdf: dict[str, Any] | str | Path,
    options: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    session = _resolve_session(session_or_pdf)
    status = load_status(session)
    if str(status.get("status") or "").strip().lower() == "running":
        raise RuntimeError("Cannot export a ChatGPT context pack while the audit status is running.")
    normalized_options = _normalize_chatgpt_context_pack_options(options)
    root = Path(session["workdir"])
    export_dir = _unique_chatgpt_export_dir(root)

    context_question = "Prepare a ChatGPT handoff context for follow-up questions about this completed audit."
    if normalized_options["context_depth"] == "reduced_audit_context":
        context_text = _build_reduced_audit_qa_context(session, context_question)
    else:
        context_text = _build_full_audit_qa_context(session, context_question)

    audit_context_path = export_dir / "audit_context.md"
    paper_structure_path = export_dir / "paper_structure.json"
    audit_context_path.write_text(context_text.strip() + "\n", encoding="utf-8")
    save_json(paper_structure_path, _build_paper_structure_context(session))

    copied_files: list[dict[str, str]] = []
    skipped_files: list[dict[str, str]] = []

    def copy_optional(path_text: Any, label: str) -> None:
        clean = str(path_text or "").strip()
        path = Path(clean).expanduser()
        if not clean or not path.exists() or not path.is_file():
            skipped_files.append({"label": label, "source": clean, "reason": "missing"})
            return
        dest = export_dir / path.name
        if dest.exists():
            dest = export_dir / f"{path.stem}_{label}{path.suffix}"
        shutil.copy2(path, dest)
        copied_files.append({"label": label, "source": str(path), "destination": str(dest)})

    if normalized_options["include_pdf"]:
        copy_optional(session.get("pdf_path"), "paper_pdf")
    if normalized_options["include_tex"]:
        copy_optional(session.get("tex_path"), "tex_source")

    for option_key, label, report_kind in [
        ("include_concise_report", "concise_report", "concise"),
        ("include_full_report", "full_report", "full"),
        ("include_verification_report", "verification_report", "verification"),
    ]:
        if not normalized_options.get(option_key):
            continue
        paths = _chatgpt_export_report_paths(session, report_kind, normalized_options.get("report_file_formats"))
        if not paths:
            skipped_files.append({"label": label, "source": report_kind, "reason": "missing"})
            continue
        for path in paths:
            copy_optional(str(path), label)

    starter_prompt_text = _chatgpt_context_pack_starter_prompt(session, normalized_options, copied_files)
    manifest_path = export_dir.parent / f"{export_dir.name}_manifest.json"
    manifest = {
        "exported_at": utc_now(),
        "export_folder": str(export_dir),
        "session": load_session_from_pdf(session["pdf_path"]) or session,
        "options": normalized_options,
        "starter_prompt_text": starter_prompt_text,
        "audit_context": str(audit_context_path),
        "paper_structure": str(paper_structure_path),
        "copied_files": copied_files,
        "skipped_files": skipped_files,
    }
    save_json(manifest_path, manifest)

    return {
        "export_folder": str(export_dir),
        "starter_prompt_text": starter_prompt_text,
        "audit_context": str(audit_context_path),
        "paper_structure": str(paper_structure_path),
        "manifest": str(manifest_path),
        "copied_files": copied_files,
        "skipped_files": skipped_files,
        "options": normalized_options,
    }


_QA_TURN_RE = re.compile(r"^qa_(\d+)\.json$")
_QA_STOPWORDS = {
    "a",
    "about",
    "all",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "can",
    "draft",
    "explain",
    "find",
    "findings",
    "for",
    "from",
    "give",
    "how",
    "i",
    "in",
    "into",
    "is",
    "it",
    "its",
    "me",
    "most",
    "of",
    "on",
    "or",
    "paper",
    "please",
    "referee",
    "related",
    "show",
    "so",
    "summarize",
    "summary",
    "tell",
    "that",
    "the",
    "their",
    "them",
    "there",
    "these",
    "this",
    "those",
    "to",
    "what",
    "which",
    "with",
    "would",
    "write",
    "you",
}


def _resolve_qa_session(session_or_pdf_path: dict[str, Any] | str | Path) -> dict[str, Any]:
    return _resolve_session(session_or_pdf_path)


def _assert_qa_ready(session: dict[str, Any]) -> None:
    if session.get("pending"):
        raise RuntimeError(
            "This audit session still has a pending chunk. Recover or finish the audit before using post-audit Q&A."
        )
    status = load_status(session)
    if str(status.get("status") or "").lower() != "completed":
        raise RuntimeError(
            f"Post-audit Q&A is only enabled after the main audit is completed. Current status={status.get('status')!r}."
        )


def _wait_for_response(response_id: str, poll_every: float = 3.0, max_wait_seconds: Optional[float] = None):
    start = time.time()
    client = _get_client()
    while True:
        resp = client.responses.retrieve(response_id)
        if getattr(resp, "status", None) not in WORKING_STATUSES:
            return resp
        if max_wait_seconds is not None and (time.time() - start) > max_wait_seconds:
            raise TimeoutError(f"Polling exceeded {max_wait_seconds} seconds for response {response_id}")
        time.sleep(poll_every)


def _ensure_qa_thread_state(session: dict[str, Any]) -> dict[str, Any]:
    changed = False
    threads = session.get("qa_threads")
    if not isinstance(threads, dict):
        threads = {}
        session["qa_threads"] = threads
        changed = True

    legacy = threads.get(LEGACY_QA_THREAD_ID)
    if not isinstance(legacy, dict):
        legacy = {}
        threads[LEGACY_QA_THREAD_ID] = legacy
        changed = True
    for key, value in {
        "thread_id": LEGACY_QA_THREAD_ID,
        "created_at": session.get("created_at") or utc_now(),
        "conversation_id": session.get("conversation_id"),
        "pdf_attached_in_conversation": bool(session.get("pdf_attached_in_conversation", False)),
    }.items():
        if key not in legacy:
            legacy[key] = value
            changed = True
    if session.get("conversation_id") and not legacy.get("conversation_id"):
        legacy["conversation_id"] = session.get("conversation_id")
        changed = True
    if session.get("pdf_attached_in_conversation") and not legacy.get("pdf_attached_in_conversation"):
        legacy["pdf_attached_in_conversation"] = True
        changed = True

    active_thread_id = str(session.get("active_qa_thread_id") or "").strip()
    if not active_thread_id:
        active_thread_id = LEGACY_QA_THREAD_ID
        session["active_qa_thread_id"] = active_thread_id
        changed = True
    if active_thread_id not in threads or not isinstance(threads.get(active_thread_id), dict):
        threads[active_thread_id] = {
            "thread_id": active_thread_id,
            "created_at": utc_now(),
            "conversation_id": None,
            "pdf_attached_in_conversation": False,
        }
        changed = True

    if changed:
        session["updated_at"] = utc_now()
        save_session(session)
    return session


def _active_qa_thread(session: dict[str, Any]) -> dict[str, Any]:
    session = _ensure_qa_thread_state(session)
    thread_id = str(session.get("active_qa_thread_id") or LEGACY_QA_THREAD_ID)
    return session["qa_threads"][thread_id]


def _qa_thread_summary(session: dict[str, Any]) -> dict[str, Any]:
    session = _ensure_qa_thread_state(session)
    active_thread_id = str(session.get("active_qa_thread_id") or LEGACY_QA_THREAD_ID)
    threads = session.get("qa_threads") or {}
    active = threads.get(active_thread_id) or {}
    return {
        "active_thread_id": active_thread_id,
        "thread_count": len(threads),
        "created_at": active.get("created_at"),
        "conversation_id": active.get("conversation_id"),
    }


def _qa_thread_entries(session: dict[str, Any]) -> list[dict[str, Any]]:
    session = _ensure_qa_thread_state(session)
    active_thread_id = str(session.get("active_qa_thread_id") or LEGACY_QA_THREAD_ID)
    threads = session.get("qa_threads") or {}
    turn_counts: dict[str, int] = {}
    for turn in _load_qa_turns(session):
        thread_id = _qa_turn_thread_id(turn)
        turn_counts[thread_id] = turn_counts.get(thread_id, 0) + 1

    entries = []
    for thread_id, meta in threads.items():
        if not isinstance(meta, dict):
            continue
        created_at = str(meta.get("created_at") or "").strip()
        turn_count = int(turn_counts.get(str(thread_id), 0) or 0)
        if str(thread_id) == LEGACY_QA_THREAD_ID:
            label = f"Legacy thread ({turn_count} {'turn' if turn_count == 1 else 'turns'})"
        else:
            label_base = created_at[:16].replace("T", " ") if created_at else str(thread_id)
            label = f"{label_base} ({turn_count} {'turn' if turn_count == 1 else 'turns'})"
        entries.append(
            {
                "thread_id": str(thread_id),
                "label": label,
                "created_at": created_at,
                "turn_count": turn_count,
                "is_active": str(thread_id) == active_thread_id,
                "has_conversation_id": bool(meta.get("conversation_id")),
                "conversation_id": meta.get("conversation_id"),
            }
        )

    entries.sort(
        key=lambda item: (
            0 if item.get("thread_id") == LEGACY_QA_THREAD_ID else 1,
            str(item.get("created_at") or ""),
            str(item.get("thread_id") or ""),
        )
    )
    return entries


def _prune_empty_qa_threads(session: dict[str, Any]) -> dict[str, Any]:
    session = _ensure_qa_thread_state(session)
    threads = session.get("qa_threads") or {}
    turn_counts: dict[str, int] = {}
    for turn in _load_qa_turns(session):
        thread_id = _qa_turn_thread_id(turn)
        turn_counts[thread_id] = turn_counts.get(thread_id, 0) + 1

    active_thread_id = str(session.get("active_qa_thread_id") or LEGACY_QA_THREAD_ID)
    pruned = []
    for thread_id, meta in list(threads.items()):
        clean_thread_id = str(thread_id)
        if clean_thread_id == LEGACY_QA_THREAD_ID or not isinstance(meta, dict):
            continue
        has_turns = int(turn_counts.get(clean_thread_id, 0) or 0) > 0
        has_conversation = bool(meta.get("conversation_id"))
        has_attached_pdf = bool(meta.get("pdf_attached_in_conversation"))
        if not has_turns and not has_conversation and not has_attached_pdf:
            threads.pop(thread_id, None)
            pruned.append(clean_thread_id)

    if pruned:
        if active_thread_id in pruned:
            session["active_qa_thread_id"] = LEGACY_QA_THREAD_ID
        session["updated_at"] = utc_now()
        save_session(session)
    return session


def list_qa_threads(session_or_pdf_path: dict[str, Any] | str | Path) -> list[dict[str, Any]]:
    session = _resolve_qa_session(session_or_pdf_path)
    session = _prune_empty_qa_threads(session)
    return _qa_thread_entries(session)


def set_active_qa_thread(session_or_pdf_path: dict[str, Any] | str | Path, thread_id: str) -> dict[str, Any]:
    session = _resolve_qa_session(session_or_pdf_path)
    session = _ensure_qa_thread_state(session)
    clean_thread_id = str(thread_id or "").strip()
    threads = session.get("qa_threads") or {}
    if clean_thread_id not in threads:
        raise ValueError(f"Unknown discussion thread: {clean_thread_id or '<empty>'}")

    session["active_qa_thread_id"] = clean_thread_id
    session["updated_at"] = utc_now()
    save_session(session)

    turns = _load_qa_turns(session, active_thread_only=True)
    entries = _qa_thread_entries(session)
    selected = next((entry for entry in entries if entry.get("thread_id") == clean_thread_id), None)
    return {
        "session": session,
        "thread": selected or {"thread_id": clean_thread_id, "label": clean_thread_id},
        "threads": entries,
        "turns": turns,
        "discussion_usage": _qa_usage_summary_from_turns(turns),
    }


def _ensure_qa_conversation(session: dict[str, Any]) -> dict[str, Any]:
    session = _ensure_qa_thread_state(session)
    thread = _active_qa_thread(session)
    changed = False
    client = _get_client()
    if not thread.get("conversation_id"):
        conversation = client.conversations.create()
        thread["conversation_id"] = conversation.id
        changed = True
    if not session.get("pdf_file_id"):
        pdf_path = Path(session["pdf_path"]).expanduser().resolve()
        if pdf_path.exists():
            with pdf_path.open("rb") as f:
                uploaded = client.files.create(file=f, purpose="user_data")
            session["pdf_file_id"] = uploaded.id
            thread["pdf_attached_in_conversation"] = False
            changed = True
    if changed:
        session["updated_at"] = utc_now()
        save_session(session)
    return session


def _ensure_qa_pdf_file(session: dict[str, Any]) -> dict[str, Any]:
    if session.get("pdf_file_id"):
        return session
    client = _get_client()
    pdf_path = Path(session["pdf_path"]).expanduser().resolve()
    if pdf_path.exists():
        with pdf_path.open("rb") as f:
            uploaded = client.files.create(file=f, purpose="user_data")
        session["pdf_file_id"] = uploaded.id
        session["updated_at"] = utc_now()
        save_session(session)
    return session


def start_new_qa_thread(session_or_pdf_path: dict[str, Any] | str | Path) -> dict[str, Any]:
    session = _resolve_qa_session(session_or_pdf_path)
    _assert_qa_ready(session)
    session = _prune_empty_qa_threads(session)
    threads = session["qa_threads"]
    thread_id = "thread_" + _artifact_timestamp_token()
    while thread_id in threads:
        time.sleep(0.001)
        thread_id = "thread_" + _artifact_timestamp_token()
    created_at = utc_now()
    threads[thread_id] = {
        "thread_id": thread_id,
        "created_at": created_at,
        "conversation_id": None,
        "pdf_attached_in_conversation": False,
    }
    session["active_qa_thread_id"] = thread_id
    session["updated_at"] = created_at
    save_session(session)
    return {
        "session": session,
        "thread_id": thread_id,
        "active_thread_id": thread_id,
        "turns": [],
        "discussion_usage": _qa_usage_summary_from_turns([]),
    }


def _qa_dir(session: dict[str, Any]) -> Path:
    path = Path(session["workdir"]) / "qa"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _qa_turn_paths(session: dict[str, Any], idx: int) -> dict[str, Path]:
    root = _qa_dir(session)
    stem = f"qa_{idx:03d}"
    return {
        "json": root / f"{stem}.json",
        "md": root / f"{stem}.md",
    }


def _next_qa_index(session: dict[str, Any]) -> int:
    max_idx = 0
    for path in _qa_dir(session).glob("qa_*.json"):
        m = _QA_TURN_RE.match(path.name)
        if m:
            max_idx = max(max_idx, int(m.group(1)))
    return max_idx + 1


def _qa_turn_thread_id(turn: dict[str, Any]) -> str:
    return str(turn.get("thread_id") or LEGACY_QA_THREAD_ID).strip() or LEGACY_QA_THREAD_ID


def _load_qa_turns(
    session: dict[str, Any],
    thread_id: Optional[str] = None,
    active_thread_only: bool = False,
) -> list[dict[str, Any]]:
    session = _ensure_qa_thread_state(session)
    selected_thread_id = str(thread_id or "").strip()
    if active_thread_only:
        selected_thread_id = str(session.get("active_qa_thread_id") or LEGACY_QA_THREAD_ID)
    turns = []
    for path in sorted(_qa_dir(session).glob("qa_*.json")):
        try:
            data = load_json(path)
        except Exception:
            continue
        if isinstance(data, dict):
            data.setdefault("thread_id", LEGACY_QA_THREAD_ID)
            if selected_thread_id and _qa_turn_thread_id(data) != selected_thread_id:
                continue
            data.setdefault("turn_path", str(path))
            turns.append(data)
    turns.sort(key=lambda item: (str(item.get("time") or ""), str(item.get("turn_id") or "")))
    return turns


def _qa_usage_summary_from_turns(turns: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "turns": 0,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        summary["turns"] += 1
        usage = turn.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        input_details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        total_tokens = int(usage.get("total_tokens", 0) or 0)
        if not total_tokens:
            total_tokens = int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
        summary["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
        summary["cached_tokens"] += int(input_details.get("cached_tokens", 0) or 0)
        summary["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
        summary["reasoning_tokens"] += int(output_details.get("reasoning_tokens", 0) or 0)
        summary["total_tokens"] += total_tokens
        if usage:
            summary["cost_usd"] += float(
                compute_usage_cost(str(turn.get("model") or DEFAULT_MODEL), usage).get("total_cost", 0.0) or 0.0
            )
    return summary


def _save_qa_turn(
    session: dict[str, Any],
    turn: dict[str, Any],
    answer_markdown: str,
    idx: int,
) -> dict[str, str]:
    paths = _qa_turn_paths(session, idx)
    save_json(paths["json"], turn)
    lines = [
        f"# Q&A turn {idx:03d} -- {Path(session['pdf_path']).stem}",
        "",
        f"- Time: {turn.get('time', '')}",
        f"- Mode: {turn.get('mode', '')}",
        f"- Thread ID: {turn.get('thread_id', LEGACY_QA_THREAD_ID)}",
        f"- Context mode: {turn.get('qa_context_mode', DEFAULT_QA_CONTEXT_MODE)}",
        f"- Model: {turn.get('model', '')}",
        f"- Reasoning effort: {turn.get('reasoning_effort', '')}",
        f"- Response ID: {turn.get('response_id', '') or 'n/a'}",
        "",
        "## Question",
        "",
        _strip_unsafe_control_chars(turn.get("question", "")),
        "",
    ]
    grounding_summary = str(turn.get("grounding_summary") or "").strip()
    if grounding_summary:
        lines.extend(
            [
                "## Audit grounding summary",
                "",
                "```text",
                _truncate_text(_strip_unsafe_control_chars(grounding_summary), limit=5000),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Answer",
            "",
            answer_markdown.strip() or "_No answer returned._",
            "",
        ]
    )
    paths["md"].write_text(_strip_unsafe_control_chars("\n".join(lines).strip() + "\n"), encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}


def _extract_qa_answer_text(resp) -> str:
    raw_text = (getattr(resp, "output_text", None) or "").strip()
    if raw_text:
        return _strip_unsafe_control_chars(_repair_json_escape_artifacts(raw_text))

    raw = to_jsonable(resp)
    parts = []
    for item in raw.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if content.get("type") == "output_text":
                txt = (content.get("text") or "").strip()
                if txt:
                    parts.append(txt)
    answer = "\n\n".join(parts).strip()
    if answer:
        return _strip_unsafe_control_chars(_repair_json_escape_artifacts(answer))
    raise ValueError("Could not locate answer text in the Q&A response.")


def _qa_developer_prompt(mode: str) -> str:
    if mode == "audit":
        return (
            "You are answering follow-up questions about a completed mathematical paper audit. "
            "Use the ongoing conversation plus the supplied audit artifacts. "
            "Ground the answer in the saved audit state when possible, distinguish paper facts from audit findings, "
            "cite chunk ids or issue ids when they materially help, and say explicitly when the artifacts are insufficient."
        )
    return (
        "You are answering follow-up questions about a mathematical paper after a completed audit. "
        "Use the ongoing conversation context conservatively, answer clearly, preserve mathematical notation, "
        "and say when a claim cannot be supported from the paper context you have."
    )


def _qa_tokens(text: str) -> list[str]:
    tokens = []
    seen = set()
    for token in re.findall(r"[A-Za-z0-9_.:-]+", str(text).lower()):
        if token in _QA_STOPWORDS:
            continue
        if len(token) < 3 and not re.fullmatch(r"\d+(?:\.\d+)*", token):
            continue
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def _qa_relevance_score(question: str, *parts: Any) -> int:
    haystack = " ".join(str(part or "") for part in parts).lower()
    if not haystack.strip():
        return 0
    score = 0
    for token in _qa_tokens(question):
        if token in haystack:
            score += 1 + min(haystack.count(token), 3)
    return score


def _resolve_qa_artifact_path(session: dict[str, Any], raw_path: Any, subdir: str) -> Optional[Path]:
    if not raw_path:
        return None
    candidate = Path(str(raw_path))
    if candidate.exists():
        return candidate
    fallback = Path(session["workdir"]) / subdir / candidate.name
    if fallback.exists():
        return fallback
    return None


def _coerce_audit_payload(audit: Any) -> dict[str, Any]:
    if not isinstance(audit, dict):
        audit = {}

    def _as_str(x: Any) -> str:
        return _strip_unsafe_control_chars(_repair_json_escape_artifacts("" if x is None else str(x)))

    def _as_list_of_str(x: Any) -> list[str]:
        if isinstance(x, list):
            return [_as_str(v) for v in x if _as_str(v).strip()]
        if x is None:
            return []
        s = _as_str(x).strip()
        return [s] if s else []

    def _as_issue_list(x: Any) -> list[dict[str, Any]]:
        out = []
        for it in (x if isinstance(x, list) else []):
            if not isinstance(it, dict):
                continue
            sev = _as_str(it.get("severity", "medium")).lower().strip() or "medium"
            if sev not in {"low", "medium", "high", "critical"}:
                sev = "medium"
            out.append(
                {
                    "title": _as_str(it.get("title", "Untitled issue")),
                    "severity": sev,
                    "location": _as_str(it.get("location", "")),
                    "description": _as_str(it.get("description", "")),
                    "evidence": _as_str(it.get("evidence", "")),
                    "proposed_fix": _as_str(it.get("proposed_fix", "")),
                    "tags": _as_list_of_str(it.get("tags", [])),
                }
            )
        return out

    ledger = audit.get("ledger_updates") if isinstance(audit.get("ledger_updates"), dict) else {}
    return {
        "assumptions_and_notation": _as_list_of_str(audit.get("assumptions_and_notation", [])),
        "verified_steps": _as_list_of_str(audit.get("verified_steps", [])),
        "issues": _as_issue_list(audit.get("issues", [])),
        "ledger_updates": {
            "assumptions": _as_list_of_str(ledger.get("assumptions", [])),
            "notes": _as_list_of_str(ledger.get("notes", [])),
        },
    }


def _load_chunk_audit_for_qa(session: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    path = _resolve_qa_artifact_path(session, rec.get("structured_response_path"), "responses")
    if path is None:
        return {}
    try:
        return _coerce_audit_payload(load_json(path))
    except Exception:
        return {}


def _select_relevant_strings(question: str, items: list[str], limit: int = 6) -> list[str]:
    prepared = []
    for idx, item in enumerate(items or []):
        text = _strip_unsafe_control_chars(_repair_json_escape_artifacts(str(item or "").strip()))
        if not text:
            continue
        prepared.append((_qa_relevance_score(question, text), idx, text))
    prepared.sort(key=lambda item: (-item[0], item[1]))
    chosen = [text for score, _, text in prepared if score > 0][:limit]
    if chosen:
        return chosen
    return [text for _, _, text in prepared[:limit]]


def _select_relevant_issues(question: str, issues: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    prepared = []
    for idx, issue in enumerate(issues or []):
        score = _qa_relevance_score(
            question,
            issue.get("issue_id"),
            issue.get("chunk_id"),
            issue.get("title"),
            issue.get("location"),
            issue.get("description"),
            issue.get("evidence"),
            issue.get("proposed_fix"),
        )
        prepared.append(
            (
                score,
                severity_rank.get(str(issue.get("severity") or "medium").lower(), 2),
                idx,
                issue,
            )
        )
    prepared.sort(key=lambda item: (-item[0], -item[1], item[2]))
    chosen = [issue for score, _, _, issue in prepared if score > 0][:limit]
    if chosen:
        return chosen
    return [issue for _, _, _, issue in prepared[:limit]]


def _select_relevant_chunk_records(
    question: str,
    records: list[dict[str, Any]],
    issues_by_chunk: dict[str, list[dict[str, Any]]],
    limit: int = 4,
) -> list[dict[str, Any]]:
    prepared = []
    for idx, rec in enumerate(records or []):
        chunk_id = str(rec.get("chunk_id") or "")
        related_issues = issues_by_chunk.get(chunk_id, [])
        score = _qa_relevance_score(
            question,
            chunk_id,
            rec.get("label"),
            rec.get("boundary"),
            rec.get("verification_summary"),
            " ".join(issue.get("title", "") for issue in related_issues),
            " ".join(issue.get("location", "") for issue in related_issues),
        )
        prepared.append((score, idx, rec))
    prepared.sort(key=lambda item: (-item[0], item[1]))
    chosen = [rec for score, _, rec in prepared if score > 0][:limit]
    if chosen:
        return chosen
    return [rec for _, _, rec in prepared[:limit]]


def _verification_summary_for_qa(value: Any) -> str:
    if isinstance(value, dict):
        pieces = []
        mode = "code_interpreter" if value.get("used_code_interpreter") else ""
        if mode:
            pieces.append(mode)
        tool_event_count = value.get("tool_event_count")
        if tool_event_count not in (None, ""):
            pieces.append(f"tool events: {tool_event_count}")
        container_ids = value.get("container_ids")
        if isinstance(container_ids, list) and container_ids:
            pieces.append(f"containers: {', '.join(str(x) for x in container_ids[:3])}")
        file_ids = value.get("file_ids")
        if isinstance(file_ids, list) and file_ids:
            pieces.append(f"files: {', '.join(str(x) for x in file_ids[:3])}")
        return ", ".join(pieces) if pieces else json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, list):
        return ", ".join(str(x) for x in value)
    return str(value or "")


def _build_audit_qa_context(session: dict[str, Any], question: str, max_chars: int = 12000) -> str:
    ledger = load_ledger(session)
    issues_state = load_issues(session)
    status = load_status(session)
    records = _read_chunk_records(session)
    open_issues = [
        issue for issue in (issues_state.get("issues") or []) if str(issue.get("status") or "open").lower() != "resolved"
    ]
    issues_by_chunk: dict[str, list[dict[str, Any]]] = {}
    for issue in open_issues:
        issues_by_chunk.setdefault(str(issue.get("chunk_id") or ""), []).append(issue)

    relevant_assumptions = _select_relevant_strings(question, ledger.get("assumptions") or [], limit=5)
    relevant_notes = _select_relevant_strings(question, ledger.get("notes") or [], limit=5)
    relevant_issues = _select_relevant_issues(question, open_issues, limit=6)
    relevant_records = _select_relevant_chunk_records(question, records, issues_by_chunk, limit=4)

    report_files = []
    reports_dir = Path(session["workdir"]) / "reports"
    if reports_dir.exists():
        for path in sorted(reports_dir.glob("*")):
            if path.is_file():
                report_files.append(path.name)

    lines = [
        "Completed audit context:",
        f"- PDF path: {session.get('pdf_path', '')}",
        f"- Workdir: {session.get('workdir', '')}",
        f"- Audit status: {status.get('status', '')}",
        f"- Chunks completed: {status.get('chunks_completed', 0)}/{status.get('chunks_total', 0)}",
        f"- Open issues tracked: {len(open_issues)}",
        "",
        "Ledger assumptions:",
    ]
    if relevant_assumptions:
        lines.extend(f"- {item}" for item in relevant_assumptions)
    else:
        lines.append("- None recorded.")
    lines.extend(["", "Ledger notes:"])
    if relevant_notes:
        lines.extend(f"- {item}" for item in relevant_notes)
    else:
        lines.append("- None recorded.")

    lines.extend(["", "Open issues most relevant to the question:"])
    if relevant_issues:
        for issue in relevant_issues:
            lines.extend(
                [
                    f"- {issue.get('issue_id', 'issue')} | {issue.get('severity', 'medium')} | {issue.get('title', '')}",
                    f"  chunk: {issue.get('chunk_id', '')}",
                    f"  location: {issue.get('location', '')}",
                    f"  description: {_truncate_text(issue.get('description', ''), limit=350)}",
                    f"  proposed fix: {_truncate_text(issue.get('proposed_fix', ''), limit=220)}",
                ]
            )
    else:
        lines.append("- No open issues were found in the saved audit state.")

    lines.extend(["", "Relevant chunk summaries:"])
    if relevant_records:
        for rec in relevant_records:
            chunk_id = str(rec.get("chunk_id") or "")
            audit = _load_chunk_audit_for_qa(session, rec)
            assumptions = audit.get("assumptions_and_notation") or audit.get("assumptions_notation") or []
            chunk_issues = audit.get("issues") or audit.get("issues_found") or []
            lines.extend(
                [
                    f"- {chunk_id} | {rec.get('label', '') or rec.get('boundary', '')}",
                    f"  boundary: {rec.get('boundary', '')}",
                    f"  verification mode: {rec.get('verification_mode', '') or 'local_python_only'}",
                ]
            )
            if rec.get("issue_ids"):
                lines.append(f"  issue ids: {', '.join(str(x) for x in rec.get('issue_ids') or [])}")
            if audit.get("verified_steps"):
                lines.append(
                    "  verified steps: "
                    + "; ".join(_truncate_text(step, limit=140) for step in (audit.get("verified_steps") or [])[:3])
                )
            if assumptions:
                lines.append(
                    "  assumptions: " + "; ".join(_truncate_text(item, limit=140) for item in assumptions[:2])
                )
            if chunk_issues:
                lines.append(
                    "  chunk issues: "
                    + "; ".join(
                        _truncate_text(f"{issue.get('severity', 'medium')}: {issue.get('title', '')}", limit=140)
                        for issue in chunk_issues[:3]
                    )
                )
            if rec.get("verification_summary"):
                lines.append(
                    f"  verification summary: {_truncate_text(_verification_summary_for_qa(rec.get('verification_summary')), limit=220)}"
                )
    else:
        lines.append("- No chunk summaries were available.")

    lines.extend(["", "Available report files:"])
    if report_files:
        lines.extend(f"- {name}" for name in report_files)
    else:
        lines.append("- No reports directory found yet.")

    context = _strip_unsafe_control_chars(_repair_json_escape_artifacts("\n".join(lines).strip()))
    return _truncate_text(context, limit=max_chars)


def _qa_high_priority_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    severity_rank = {"critical": 0, "high": 1}
    selected = [
        issue
        for issue in issues
        if str(issue.get("status") or "open").lower() != "resolved"
        and str(issue.get("severity") or "").strip().lower() in severity_rank
    ]
    selected.sort(
        key=lambda issue: (
            severity_rank.get(str(issue.get("severity") or "").strip().lower(), 9),
            str(issue.get("chunk_id") or ""),
            str(issue.get("issue_id") or ""),
        )
    )
    return selected


def _qa_rerun_outcome_lines(session: dict[str, Any], limit: int = 6) -> list[str]:
    lines = []
    logs_dir = Path(session["workdir"]) / "logs"
    for name in ("selected_chunk_reruns.jsonl", "failed_verification_chunk_reruns.jsonl"):
        path = logs_dir / name
        if not path.exists():
            continue
        entries = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(raw)
            except Exception:
                continue
            if isinstance(item, dict):
                entries.append(item)
        for item in entries[-limit:]:
            chunk_ids = item.get("chunk_ids") or item.get("chunks") or []
            if not isinstance(chunk_ids, list):
                chunk_ids = [chunk_ids]
            chunks = ", ".join(str(chunk) for chunk in chunk_ids if str(chunk).strip()) or "unknown chunks"
            instruction = _truncate_text(str(item.get("extra_rerun_instruction") or "").strip(), limit=180)
            suffix = f"; instruction: {instruction}" if instruction else ""
            lines.append(f"- {item.get('rerun_id') or 'rerun'} | {chunks}{suffix}")
    return lines[-limit:]


def _build_reduced_audit_qa_context(session: dict[str, Any], question: str, max_chars: int = 36000) -> str:
    ledger = load_ledger(session)
    issues_state = load_issues(session)
    status = load_status(session)
    usage = load_usage(session)
    manifest = load_manifest(session)
    records = _read_chunk_records(session)
    open_issues = [
        issue for issue in (issues_state.get("issues") or []) if str(issue.get("status") or "open").lower() != "resolved"
    ]
    high_priority_issues = _qa_high_priority_issues(open_issues)
    issues_by_chunk: dict[str, list[dict[str, Any]]] = {}
    for issue in open_issues:
        issues_by_chunk.setdefault(str(issue.get("chunk_id") or ""), []).append(issue)

    relevant_assumptions = _select_relevant_strings(question, ledger.get("assumptions") or [], limit=4)
    relevant_notes = _select_relevant_strings(question, ledger.get("notes") or [], limit=4)
    relevant_records = _select_relevant_chunk_records(question, records, issues_by_chunk, limit=6)
    usage_totals = usage.get("totals") or {}

    lines = [
        "Reduced audit context pack:",
        "This is curated from saved audit artifacts. It intentionally excludes raw prior discussion turns.",
        "",
        "Audit run summary:",
        f"- PDF path: {session.get('pdf_path', '')}",
        f"- Workdir: {session.get('workdir', '')}",
        f"- Model: {session.get('model', '')}",
        f"- Reasoning effort: {session.get('reasoning_effort', '')}",
        f"- Audit status: {status.get('status', '')}",
        f"- Chunking mode: {manifest.get('chunking_mode', '')}",
        f"- Chunks completed: {status.get('chunks_completed', 0)}/{status.get('chunks_total', 0)}",
        f"- Estimated pages completed: {status.get('estimated_pages_completed', 0)}/{status.get('estimated_pages_total', 0)}",
        f"- Audit cost USD: {float(usage_totals.get('cost_usd', status.get('cost_usd', 0.0)) or 0.0):.4f}",
        f"- Total audit time: {format_duration(float(usage_totals.get('audit_seconds', 0.0) or status.get('total_audit_seconds', 0.0) or 0.0))}",
        f"- Open issues tracked: {len(open_issues)}",
        f"- Open critical/high issues: {len(high_priority_issues)}",
    ]

    lines.extend(["", "Ledger assumptions most relevant to the question:"])
    lines.extend(f"- {item}" for item in relevant_assumptions) if relevant_assumptions else lines.append("- None recorded.")
    lines.extend(["", "Ledger notes most relevant to the question:"])
    lines.extend(f"- {item}" for item in relevant_notes) if relevant_notes else lines.append("- None recorded.")

    lines.extend(["", "Open critical/high issues:"])
    if high_priority_issues:
        for issue in high_priority_issues:
            lines.extend(
                [
                    f"- {issue.get('issue_id', 'issue')} | {issue.get('severity', 'high')} | {issue.get('title', '')}",
                    f"  chunk: {issue.get('chunk_id', '')}",
                    f"  location: {issue.get('location', '')}",
                    f"  description: {_truncate_text(issue.get('description', ''), limit=320)}",
                    f"  proposed fix: {_truncate_text(issue.get('proposed_fix', ''), limit=180)}",
                ]
            )
    else:
        lines.append("- No open critical/high issues were found.")

    lines.extend(["", "Relevant chunk findings:"])
    if relevant_records:
        for rec in relevant_records:
            chunk_id = str(rec.get("chunk_id") or "")
            audit = _load_chunk_audit_for_qa(session, rec)
            chunk_issues = audit.get("issues") or []
            lines.extend(
                [
                    f"- {chunk_id} | {rec.get('label', '') or rec.get('boundary', '')}",
                    f"  boundary: {rec.get('boundary', '')}",
                    f"  pages: {rec.get('page_start', '')}-{rec.get('page_end', '')}",
                ]
            )
            if chunk_issues:
                lines.append(
                    "  issues: "
                    + "; ".join(
                        _truncate_text(f"{issue.get('severity', 'medium')}: {issue.get('title', '')}", limit=160)
                        for issue in chunk_issues[:5]
                    )
                )
            if audit.get("verified_steps"):
                lines.append(
                    "  verified steps: "
                    + "; ".join(_truncate_text(step, limit=120) for step in (audit.get("verified_steps") or [])[:2])
                )
            if rec.get("verification_summary"):
                lines.append(
                    f"  verification summary: {_truncate_text(_verification_summary_for_qa(rec.get('verification_summary')), limit=180)}"
                )
    else:
        lines.append("- No relevant chunk records were available.")

    try:
        verification_state = load_verification_state(session)
        verification_results = _load_verification_results(session, verification_state)
        verification_counts = _verification_summary_counts(verification_results)
    except Exception:
        verification_results = []
        verification_counts = {}
    lines.extend(["", "Verification summary:"])
    if verification_counts:
        lines.append(
            "- scripts: {scripts_total}, passed: {passed}, failed: {failed}, timed out: {timeout}, skipped: {skipped}".format(
                **{key: verification_counts.get(key, 0) for key in ("scripts_total", "passed", "failed", "timeout", "skipped")}
            )
        )
        bad_results = [
            result
            for result in verification_results
            if str(result.get("status") or "").lower() in {"failed", "timeout"}
        ]
        for result in bad_results[:8]:
            lines.append(
                f"- {result.get('chunk_id', '')} | {result.get('script_name', '')} | {result.get('status', '')}: "
                + _truncate_text(result.get("conclusion", "") or result.get("stderr", "") or result.get("stdout", ""), limit=220)
            )
    else:
        lines.append("- No verification state was available.")

    rerun_lines = _qa_rerun_outcome_lines(session)
    lines.extend(["", "Recent rerun outcomes:"])
    lines.extend(rerun_lines if rerun_lines else ["- No selective rerun logs were found."])

    reports_dir = Path(session["workdir"]) / "reports"
    concise_json = reports_dir / f"{Path(session.get('pdf_path', '')).stem}_concise_audit_report.json"
    if concise_json.exists():
        try:
            concise_data = load_json(concise_json)
            high_count = len(concise_data.get("high_issues") or [])
            typo_count = len(concise_data.get("typographical_errors") or [])
            lines.extend(
                [
                    "",
                    "Concise report summary:",
                    f"- high-priority mathematical/correctness issues: {high_count}",
                    f"- typographical/copyediting issues: {typo_count}",
                ]
            )
        except Exception:
            pass

    context = _strip_unsafe_control_chars(_repair_json_escape_artifacts("\n".join(lines).strip()))
    return _truncate_text(context, limit=max_chars)


def _build_full_audit_qa_context(session: dict[str, Any], question: str, max_chars: int = 90000) -> str:
    ledger = load_ledger(session)
    issues_state = load_issues(session)
    status = load_status(session)
    usage = load_usage(session)
    manifest = load_manifest(session)
    records = _read_chunk_records(session)
    open_issues = [
        issue for issue in (issues_state.get("issues") or []) if str(issue.get("status") or "open").lower() != "resolved"
    ]
    high_priority_issues = _qa_high_priority_issues(open_issues)
    high_ids = {str(issue.get("issue_id") or "") for issue in high_priority_issues}
    other_issues = [issue for issue in open_issues if str(issue.get("issue_id") or "") not in high_ids]
    issues_by_chunk: dict[str, list[dict[str, Any]]] = {}
    for issue in open_issues:
        issues_by_chunk.setdefault(str(issue.get("chunk_id") or ""), []).append(issue)

    relevant_assumptions = _select_relevant_strings(question, ledger.get("assumptions") or [], limit=12)
    relevant_notes = _select_relevant_strings(question, ledger.get("notes") or [], limit=12)
    relevant_records = _select_relevant_chunk_records(question, records, issues_by_chunk, limit=14)
    usage_totals = usage.get("totals") or {}

    lines = [
        "Full audit context pack:",
        "This is reconstructed from saved audit artifacts only. It intentionally excludes prior post-audit Q&A turns from older threads.",
        "",
        "Audit run summary:",
        f"- PDF path: {session.get('pdf_path', '')}",
        f"- Workdir: {session.get('workdir', '')}",
        f"- Model: {session.get('model', '')}",
        f"- Reasoning effort: {session.get('reasoning_effort', '')}",
        f"- Audit status: {status.get('status', '')}",
        f"- Chunking mode: {manifest.get('chunking_mode', '')}",
        f"- Chunks completed: {status.get('chunks_completed', 0)}/{status.get('chunks_total', 0)}",
        f"- Estimated pages completed: {status.get('estimated_pages_completed', 0)}/{status.get('estimated_pages_total', 0)}",
        f"- Audit cost USD: {float(usage_totals.get('cost_usd', status.get('cost_usd', 0.0)) or 0.0):.4f}",
        f"- Total audit tokens: {int(usage_totals.get('total_tokens', 0) or 0)}",
        f"- Total audit time: {format_duration(float(usage_totals.get('audit_seconds', 0.0) or status.get('total_audit_seconds', 0.0) or 0.0))}",
        f"- Open issues tracked: {len(open_issues)}",
        f"- Open critical/high issues: {len(high_priority_issues)}",
    ]

    lines.extend(["", "Ledger assumptions relevant to the question:"])
    lines.extend(f"- {item}" for item in relevant_assumptions) if relevant_assumptions else lines.append("- None recorded.")
    lines.extend(["", "Ledger notes relevant to the question:"])
    lines.extend(f"- {item}" for item in relevant_notes) if relevant_notes else lines.append("- None recorded.")

    lines.extend(["", "Open critical/high issues with details:"])
    if high_priority_issues:
        for issue in high_priority_issues:
            lines.extend(
                [
                    f"- {issue.get('issue_id', 'issue')} | {issue.get('severity', 'high')} | {issue.get('title', '')}",
                    f"  chunk: {issue.get('chunk_id', '')}",
                    f"  location: {issue.get('location', '')}",
                    f"  description: {_truncate_text(issue.get('description', ''), limit=520)}",
                    f"  evidence: {_truncate_text(issue.get('evidence', ''), limit=300)}",
                    f"  proposed fix: {_truncate_text(issue.get('proposed_fix', ''), limit=260)}",
                    f"  tags: {', '.join(str(tag) for tag in (issue.get('tags') or [])[:8])}",
                ]
            )
    else:
        lines.append("- No open critical/high issues were found.")

    lines.extend(["", "Other open issues index:"])
    if other_issues:
        for issue in other_issues:
            lines.append(
                f"- {issue.get('issue_id', 'issue')} | {issue.get('severity', 'medium')} | {issue.get('chunk_id', '')} | "
                f"{_truncate_text(issue.get('title', ''), limit=140)}"
            )
    else:
        lines.append("- No other open issues were found.")

    lines.extend(["", "Relevant chunk findings from structured audit outputs:"])
    if relevant_records:
        for rec in relevant_records:
            chunk_id = str(rec.get("chunk_id") or "")
            audit = _load_chunk_audit_for_qa(session, rec)
            assumptions = audit.get("assumptions_and_notation") or audit.get("assumptions_notation") or []
            chunk_issues = audit.get("issues") or []
            lines.extend(
                [
                    f"- {chunk_id} | {rec.get('label', '') or rec.get('boundary', '')}",
                    f"  boundary: {rec.get('boundary', '')}",
                    f"  pages: {rec.get('page_start', '')}-{rec.get('page_end', '')}",
                    f"  issue ids: {', '.join(str(x) for x in rec.get('issue_ids') or [])}",
                ]
            )
            if assumptions:
                lines.append(
                    "  assumptions/notation: "
                    + "; ".join(_truncate_text(item, limit=170) for item in assumptions[:4])
                )
            if audit.get("verified_steps"):
                lines.append(
                    "  verified steps: "
                    + "; ".join(_truncate_text(step, limit=160) for step in (audit.get("verified_steps") or [])[:4])
                )
            if chunk_issues:
                lines.append(
                    "  chunk issues: "
                    + "; ".join(
                        _truncate_text(
                            f"{issue.get('severity', 'medium')}: {issue.get('title', '')}; {issue.get('description', '')}",
                            limit=260,
                        )
                        for issue in chunk_issues[:6]
                    )
                )
            if rec.get("verification_summary"):
                lines.append(
                    f"  verification summary: {_truncate_text(_verification_summary_for_qa(rec.get('verification_summary')), limit=220)}"
                )
    else:
        lines.append("- No relevant chunk records were available.")

    try:
        verification_state = load_verification_state(session)
        verification_results = _load_verification_results(session, verification_state)
        verification_counts = _verification_summary_counts(verification_results)
    except Exception:
        verification_results = []
        verification_counts = {}
    lines.extend(["", "Verification state/results:"])
    if verification_counts:
        lines.append(
            "- scripts: {scripts_total}, passed: {passed}, failed: {failed}, timed out: {timeout}, skipped: {skipped}".format(
                **{key: verification_counts.get(key, 0) for key in ("scripts_total", "passed", "failed", "timeout", "skipped")}
            )
        )
        bad_results = [
            result
            for result in verification_results
            if str(result.get("status") or "").lower() in {"failed", "timeout"}
        ]
        if bad_results:
            lines.append("Failed/timed-out verification results:")
            for result in bad_results[:16]:
                lines.append(
                    f"- {result.get('chunk_id', '')} | {result.get('script_name', '')} | {result.get('status', '')}: "
                    + _truncate_text(result.get("conclusion", "") or result.get("stderr", "") or result.get("stdout", ""), limit=320)
                )
        else:
            lines.append("- No failed or timed-out verification results were recorded.")
    else:
        lines.append("- No verification state was available.")

    rerun_lines = _qa_rerun_outcome_lines(session, limit=12)
    lines.extend(["", "Selective rerun outcomes:"])
    lines.extend(rerun_lines if rerun_lines else ["- No selective rerun logs were found."])

    reports_dir = Path(session["workdir"]) / "reports"
    report_files = []
    if reports_dir.exists():
        report_files = sorted(path.name for path in reports_dir.glob("*") if path.is_file())
    lines.extend(["", "Report artifacts available:"])
    lines.extend(f"- {name}" for name in report_files[:40]) if report_files else lines.append("- No reports directory found yet.")

    concise_json = reports_dir / f"{Path(session.get('pdf_path', '')).stem}_concise_audit_report.json"
    if concise_json.exists():
        try:
            concise_data = load_json(concise_json)
            lines.extend(
                [
                    "",
                    "Concise report structured summary:",
                    f"- high-priority mathematical/correctness issues: {len(concise_data.get('high_issues') or [])}",
                    f"- typographical/copyediting issues: {len(concise_data.get('typographical_errors') or [])}",
                    f"- selection rules: {json.dumps(concise_data.get('selection_rules') or {}, ensure_ascii=False)}",
                ]
            )
        except Exception:
            pass

    context = _strip_unsafe_control_chars(_repair_json_escape_artifacts("\n".join(lines).strip()))
    return _truncate_text(context, limit=max_chars)


def _qa_request_input(
    session: dict[str, Any],
    question: str,
    mode: str,
    context_text: str = "",
    qa_context_mode: str = DEFAULT_QA_CONTEXT_MODE,
    pdf_attached_in_conversation: Optional[bool] = None,
) -> tuple[list[dict[str, Any]], bool]:
    content = []
    attached_pdf = False
    thread = _active_qa_thread(session)
    pdf_attached = thread.get("pdf_attached_in_conversation", False)
    if pdf_attached_in_conversation is not None:
        pdf_attached = bool(pdf_attached_in_conversation)
    if not pdf_attached and session.get("pdf_file_id"):
        content.append({"type": "input_file", "file_id": session["pdf_file_id"]})
        attached_pdf = True

    clean_question = _strip_unsafe_control_chars(_repair_json_escape_artifacts(str(question or "").strip()))
    if context_text:
        question_heading = "Question about the completed audit:" if mode == "audit" else "Question about the paper after a completed audit:"
        if qa_context_mode == "reduced_audit_context":
            context_heading = "Curated reduced audit context:"
        elif qa_context_mode == "full_audit_context":
            context_heading = "Full audit-only context:"
        else:
            context_heading = "Audit artifact context:"
        prompt_text = "\n\n".join(
            [
                question_heading,
                clean_question,
                context_heading,
                context_text,
                "Use the audit context when it is relevant, but distinguish saved audit findings from direct paper claims.",
            ]
        )
    elif mode == "audit":
        prompt_text = "\n\n".join(
            [
                "Question about the completed audit:",
                clean_question,
                "Audit artifact context:",
                context_text or "(no saved audit context available)",
                "Answer using the audit artifacts above when they are relevant. Distinguish audit findings from direct paper claims.",
            ]
        )
    else:
        prompt_text = clean_question

    content.append({"type": "input_text", "text": prompt_text})
    return [
        {"role": "developer", "content": [{"type": "input_text", "text": _qa_developer_prompt(mode)}]},
        {"role": "user", "content": content},
    ], attached_pdf


def _run_qa_turn(
    session_or_pdf_path: dict[str, Any] | str | Path,
    question: str,
    mode: str,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    qa_context_mode: Optional[str] = None,
    save: bool = True,
) -> dict[str, Any]:
    qa_context_mode = _normalize_qa_context_mode(qa_context_mode)
    session = _resolve_qa_session(session_or_pdf_path)
    _assert_qa_ready(session)
    session = _ensure_qa_thread_state(session)
    session = _ensure_qa_conversation(session)
    thread = _active_qa_thread(session)
    thread_id = str(thread.get("thread_id") or session.get("active_qa_thread_id") or LEGACY_QA_THREAD_ID)
    conversation_id = str(thread.get("conversation_id") or "").strip()
    if not conversation_id:
        raise RuntimeError("Q&A conversation could not be initialized.")

    clean_question = _strip_unsafe_control_chars(_repair_json_escape_artifacts(str(question or "").strip()))
    if not clean_question:
        raise ValueError("question must be a non-empty string")

    qa_model = model or session.get("model") or DEFAULT_MODEL
    qa_effort = reasoning_effort or session.get("reasoning_effort")
    qa_model, qa_effort = normalize_model_and_reasoning_effort(qa_model, qa_effort)
    grounding_summary = ""
    if qa_context_mode == "reduced_audit_context":
        grounding_summary = _build_reduced_audit_qa_context(session, clean_question)
    elif qa_context_mode == "full_audit_context":
        grounding_summary = _build_full_audit_qa_context(session, clean_question)
    pdf_attached = thread.get("pdf_attached_in_conversation", False)
    input_payload, attached_pdf = _qa_request_input(
        session,
        clean_question,
        mode,
        grounding_summary,
        qa_context_mode=qa_context_mode,
        pdf_attached_in_conversation=bool(pdf_attached),
    )

    client = _get_client()
    resp = client.responses.create(
        model=qa_model,
        reasoning={"effort": qa_effort},
        conversation=conversation_id,
        input=input_payload,
        background=False,
        store=bool(session.get("store", True)),
    )
    if getattr(resp, "status", None) in WORKING_STATUSES:
        resp = _wait_for_response(resp.id, poll_every=2.0, max_wait_seconds=None)
    if getattr(resp, "status", None) != "completed":
        raise RuntimeError(f"Q&A response ended with status={getattr(resp, 'status', None)}")

    answer = _extract_qa_answer_text(resp)
    usage_obj = to_jsonable(getattr(resp, "usage", {}) or {})
    if not isinstance(usage_obj, dict):
        usage_obj = {}
    cost = compute_usage_cost(qa_model, usage_obj)
    pricing = {
        "context": cost.get("pricing_context"),
        "rates_usd_per_1m": cost.get("pricing_rates_usd_per_1m"),
        "input_token_threshold": cost.get("pricing_input_token_threshold"),
    }
    turn = {
        "time": utc_now(),
        "turn_id": None,
        "mode": mode,
        "question": clean_question,
        "answer": answer,
        "model": qa_model,
        "reasoning_effort": qa_effort,
        "qa_context_mode": qa_context_mode,
        "response_id": getattr(resp, "id", None),
        "conversation_id": conversation_id,
        "thread_id": thread_id,
        "grounding_summary": grounding_summary,
        "usage": usage_obj,
        "cost": cost,
        "pricing": pricing,
    }

    path_info = {}
    if save:
        idx = _next_qa_index(session)
        turn["turn_id"] = f"qa_{idx:03d}"
        path_info = _save_qa_turn(session, turn, answer, idx)
    if attached_pdf:
        thread["pdf_attached_in_conversation"] = True
    session["updated_at"] = utc_now()
    save_session(session)

    result = dict(turn)
    if path_info:
        result["paths"] = path_info
    return result


def ask_about_paper(
    session_or_pdf_path: dict[str, Any] | str | Path,
    question: str,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    qa_context_mode: Optional[str] = None,
    save: bool = True,
) -> dict[str, Any]:
    return _run_qa_turn(
        session_or_pdf_path,
        question,
        mode="paper",
        model=model,
        reasoning_effort=reasoning_effort,
        qa_context_mode=qa_context_mode,
        save=save,
    )


def ask_about_audit(
    session_or_pdf_path: dict[str, Any] | str | Path,
    question: str,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    qa_context_mode: Optional[str] = None,
    save: bool = True,
) -> dict[str, Any]:
    return _run_qa_turn(
        session_or_pdf_path,
        question,
        mode="audit",
        model=model,
        reasoning_effort=reasoning_effort,
        qa_context_mode=qa_context_mode,
        save=save,
    )


def _qa_report_markdown(session: dict[str, Any], turns: list[dict[str, Any]]) -> str:
    lines = [
        f"# Q&A appendix -- {Path(session['pdf_path']).stem}",
        "",
        f"- PDF: {session['pdf_path']}",
        f"- Workdir: {session['workdir']}",
        f"- Saved turns: {len(turns)}",
        "",
    ]
    if not turns:
        lines.append("No saved Q&A turns found.")
        return _strip_unsafe_control_chars("\n".join(lines).strip() + "\n")

    for turn in turns:
        lines.extend(
            [
                f"## {turn.get('turn_id', 'qa_turn')} [{turn.get('mode', '')}]",
                f"- Time: {turn.get('time', '')}",
                f"- Model: {turn.get('model', '')}",
                f"- Reasoning effort: {turn.get('reasoning_effort', '')}",
                f"- Response ID: {turn.get('response_id', '') or 'n/a'}",
                "",
                "### Question",
                "",
                _strip_unsafe_control_chars(turn.get("question", "")),
                "",
            ]
        )
        grounding_summary = str(turn.get("grounding_summary") or "").strip()
        if grounding_summary:
            lines.extend(
                [
                    "### Audit grounding summary",
                    "",
                    "```text",
                    _truncate_text(_strip_unsafe_control_chars(grounding_summary), limit=5000),
                    "```",
                    "",
                ]
            )
        lines.extend(
            [
                "### Answer",
                "",
                _strip_unsafe_control_chars(turn.get("answer", "")) or "_No answer returned._",
                "",
            ]
        )
    return _strip_unsafe_control_chars("\n".join(lines).strip() + "\n")


def _qa_text_to_tex_blocks(text: str) -> str:
    cleaned = _strip_unsafe_control_chars(_repair_json_escape_artifacts(str(text or "").strip()))
    if not cleaned:
        return report_latex_paragraph("(empty)") + "\n"
    parts = []
    for para in re.split(r"\n\s*\n", cleaned):
        para = para.strip()
        if not para:
            continue
        parts.append(report_latex_paragraph(para) + "\n\n")
    return "".join(parts) or (report_latex_paragraph(cleaned) + "\n")


def _qa_report_tex(session: dict[str, Any], turns: list[dict[str, Any]]) -> str:
    title = report_latex_paragraph(f"Q&A appendix -- {Path(session['pdf_path']).stem}")
    parts = [
        r"""\documentclass[11pt]{article}
\usepackage[a4paper,margin=1in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{microtype}
\usepackage{hyperref}
\usepackage{enumitem}
\usepackage{xcolor}
\usepackage{fancyvrb}
\setlist[itemize]{leftmargin=2em}
\setlength{\parskip}{0.5em}
\setlength{\parindent}{0pt}
\begin{document}
"""
    ]
    parts.append(r"\section*{" + title + "}" + "\n")
    parts.append(r"\begin{itemize}" + "\n")
    parts.append(r"\item PDF: " + report_latex_paragraph(session["pdf_path"]) + "\n")
    parts.append(r"\item Workdir: " + report_latex_paragraph(session["workdir"]) + "\n")
    parts.append(r"\item Saved turns: " + str(len(turns)) + "\n")
    parts.append(r"\end{itemize}" + "\n")
    if not turns:
        parts.append(report_latex_paragraph("No saved Q&A turns found.") + "\n")
    else:
        for turn in turns:
            heading = report_latex_paragraph(f"{turn.get('turn_id', 'qa_turn')} [{turn.get('mode', '')}]")
            parts.append(r"\subsection*{" + heading + "}" + "\n")
            parts.append(r"\begin{itemize}" + "\n")
            parts.append(r"\item Time: " + report_latex_paragraph(str(turn.get("time", ""))) + "\n")
            parts.append(r"\item Model: " + report_latex_paragraph(str(turn.get("model", ""))) + "\n")
            parts.append(r"\item Reasoning effort: " + report_latex_paragraph(str(turn.get("reasoning_effort", ""))) + "\n")
            parts.append(r"\item Response ID: " + report_latex_paragraph(str(turn.get("response_id", "") or "n/a")) + "\n")
            parts.append(r"\end{itemize}" + "\n")
            parts.append(r"\paragraph{Question}" + "\n")
            parts.append(_qa_text_to_tex_blocks(turn.get("question", "")))
            grounding_summary = str(turn.get("grounding_summary") or "").strip()
            if grounding_summary:
                parts.append(r"\paragraph{Audit grounding summary}" + "\n")
                parts.append(_verbatim_block(_truncate_text(grounding_summary, limit=5000)) + "\n")
            parts.append(r"\paragraph{Answer}" + "\n")
            parts.append(_qa_text_to_tex_blocks(turn.get("answer", "")))
    parts.append(r"\end{document}" + "\n")
    return _strip_unsafe_control_chars("".join(parts))


def rebuild_qa_report(session_or_pdf_path: dict[str, Any] | str | Path) -> dict[str, str]:
    session = _resolve_qa_session(session_or_pdf_path)
    turns = _load_qa_turns(session)
    root = Path(session["workdir"])
    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_stem = Path(session["pdf_path"]).stem + "_qa_report"
    md_path = reports_dir / f"{report_stem}.md"
    tex_path = reports_dir / f"{report_stem}.tex"
    json_path = reports_dir / f"{report_stem}.json"
    md_path.write_text(_qa_report_markdown(session, turns), encoding="utf-8")
    tex_path.write_text(_qa_report_tex(session, turns), encoding="utf-8")
    save_json(
        json_path,
        {
            "session": load_session_from_pdf(session["pdf_path"]),
            "turns": turns,
            "generated_at": utc_now(),
        },
    )
    return {
        "markdown": str(md_path),
        "tex": str(tex_path),
        "json": str(json_path),
    }


def load_qa_turns(
    session_or_pdf_path: dict[str, Any] | str | Path,
    thread_id: Optional[str] = None,
    active_thread_only: bool = False,
) -> list[dict[str, Any]]:
    session = _resolve_qa_session(session_or_pdf_path)
    return _load_qa_turns(session, thread_id=thread_id, active_thread_only=active_thread_only)


REFERENCE_MENTION_STYLES = {"auto", "compiled_pdf_numbers", "source_labels"}
VERIFICATION_MODES = {"local_python_only", "code_interpreter", "none"}
CI_FAILURE_FALLBACK_MODES = {"off", "retry_local_python_only_once"}

AUDIT_SYSTEM_PROMPT = SHIPPED_AUDIT_SYSTEM_PROMPT

AUDIT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "boundary": {"type": "string"},
        "chunk_too_large": {"type": "boolean"},
        "chunk_split_suggestions": {"type": "array", "items": {"type": "string"}},
        "assumptions_and_notation": {"type": "array", "items": {"type": "string"}},
        "verified_steps": {"type": "array", "items": {"type": "string"}},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                    "evidence": {"type": "string"},
                    "proposed_fix": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "title",
                    "severity",
                    "location",
                    "description",
                    "evidence",
                    "proposed_fix",
                    "tags",
                ],
                "additionalProperties": False,
            },
        },
        "python_checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "purpose": {"type": "string"},
                    "description": {"type": "string"},
                    "expected_outcome": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["purpose", "description", "expected_outcome", "code"],
                "additionalProperties": False,
            },
        },
        "latex_patch": {"type": "string"},
        "ledger_updates": {
            "type": "object",
            "properties": {
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "notes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["assumptions", "notes"],
            "additionalProperties": False,
        },
        "next_boundary_hint": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": [
        "label",
        "boundary",
        "chunk_too_large",
        "chunk_split_suggestions",
        "assumptions_and_notation",
        "verified_steps",
        "issues",
        "python_checks",
        "latex_patch",
        "ledger_updates",
        "next_boundary_hint",
        "confidence",
    ],
    "additionalProperties": False,
}


def _dedupe_strings(seq: list[str]) -> list[str]:
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _artifact_timestamp_token() -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", utc_now())


def _normalize_verification_mode(mode: Optional[str]) -> str:
    aliases = {
        "local": "local_python_only",
        "python": "local_python_only",
        "local_python": "local_python_only",
        "ci": "code_interpreter",
        "code": "code_interpreter",
        "off": "none",
    }
    normalized = "local_python_only" if mode is None else str(mode).strip().lower()
    normalized = aliases.get(normalized, normalized)
    if normalized not in VERIFICATION_MODES:
        allowed = ", ".join(sorted(VERIFICATION_MODES))
        raise ValueError(f"verification_mode must be one of: {allowed}")
    return normalized


def _normalize_ci_failure_fallback_mode(mode: Optional[str]) -> str:
    aliases = {
        "none": "off",
        "false": "off",
        "disabled": "off",
        "retry": "retry_local_python_only_once",
        "local": "retry_local_python_only_once",
        "retry_local": "retry_local_python_only_once",
        "local_python_only": "retry_local_python_only_once",
    }
    normalized = "off" if mode is None else str(mode).strip().lower()
    normalized = aliases.get(normalized, normalized)
    if normalized not in CI_FAILURE_FALLBACK_MODES:
        allowed = ", ".join(sorted(CI_FAILURE_FALLBACK_MODES))
        raise ValueError(f"ci_failure_fallback_mode must be one of: {allowed}")
    return normalized


def _normalize_reference_mention_style(style: Optional[str]) -> str:
    aliases = {
        "compiled": "compiled_pdf_numbers",
        "numbers": "compiled_pdf_numbers",
        "pdf": "compiled_pdf_numbers",
        "pdf_numbers": "compiled_pdf_numbers",
        "label": "source_labels",
        "labels": "source_labels",
        "source": "source_labels",
    }
    normalized = "auto" if style is None else str(style).strip().lower()
    normalized = aliases.get(normalized, normalized)
    if normalized not in REFERENCE_MENTION_STYLES:
        allowed = ", ".join(sorted(REFERENCE_MENTION_STYLES))
        raise ValueError(f"reference_mention_style must be one of: {allowed}")
    return normalized


def _normalize_report_reference_style(style: Optional[str]) -> str:
    aliases = {
        "match": "match_audit",
        "audit": "match_audit",
        "compiled": "compiled_pdf_numbers",
        "numbers": "compiled_pdf_numbers",
        "pdf": "compiled_pdf_numbers",
        "pdf_numbers": "compiled_pdf_numbers",
        "label": "source_labels",
        "labels": "source_labels",
        "source": "source_labels",
    }
    normalized = "match_audit" if style is None else str(style).strip().lower()
    normalized = aliases.get(normalized, normalized)
    allowed = {"match_audit", "compiled_pdf_numbers", "source_labels"}
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"report_reference_style must be one of: {choices}")
    return normalized


def extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty text")
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    decoder = json.JSONDecoder()
    first = text.find("{")
    while first >= 0:
        try:
            obj, _ = decoder.raw_decode(text[first:])
            return obj
        except Exception:
            first = text.find("{", first + 1)
    raise ValueError("Could not parse JSON object from model output.")


def _clean_lines_to_items(text: str) -> list[str]:
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    if not lines:
        return []
    if len(lines) == 1 and lines[0].lower().startswith("no "):
        return []
    return lines


def _parse_issues_block(text: str) -> list[dict[str, Any]]:
    text = str(text).strip()
    if not text or text.lower().startswith("no issues found"):
        return []
    lines = text.splitlines()
    issues = []
    current = None
    current_field = None

    def flush():
        nonlocal current
        if current is not None:
            if isinstance(current.get("tags"), str):
                current["tags"] = [t.strip() for t in current["tags"].split(",") if t.strip()]
            current.setdefault("severity", "medium")
            current.setdefault("status", "open")
            current.setdefault("location", "")
            current.setdefault("description", "")
            current.setdefault("evidence", "")
            current.setdefault("proposed_fix", "")
            current.setdefault("tags", [])
            issues.append(current)
            current = None

    def next_nonempty(idx: int) -> str:
        for j in range(idx + 1, len(lines)):
            s = lines[j].strip()
            if s:
                return s
        return ""

    field_re = re.compile(r"^(Status|Location|Description|Evidence|Proposed fix|Tags):\s*(.*)$")
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        m = field_re.match(s)
        if m:
            if current is None:
                current = {"title": "Issue", "severity": "medium"}
            key = m.group(1).lower().replace(" ", "_")
            val = m.group(2).strip()
            if key == "tags":
                current["tags"] = val
            else:
                current[key] = val
            current_field = key
            continue
        looks_like_title = (
            ":" not in s
            and (re.search(r"\[[^\]]+\]\s*$", s) is not None or next_nonempty(i).startswith("Status:"))
        )
        if looks_like_title:
            flush()
            m2 = re.match(r"^(.*?)(?:\s*\[([A-Za-z_ -]+)\])?$", s)
            title = m2.group(1).strip()
            sev = (m2.group(2) or "medium").strip().lower()
            current = {"title": title, "severity": sev}
            current_field = None
            continue
        if current is not None and current_field is not None:
            current[current_field] = (current.get(current_field, "") + " " + s).strip()
    flush()
    return issues


def _parse_ledger_block(text: str) -> dict[str, list[str]]:
    text = str(text).strip()
    if not text:
        return {}
    lines = text.splitlines()
    ledger = {}
    current = None
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s in {"Assumptions", "Notes"}:
            current = s.lower()
            ledger.setdefault(current, [])
            continue
        if current is None:
            ledger.setdefault("notes", []).append(s)
        else:
            ledger[current].append(s)
    return ledger


def parse_legacy_markdown_audit(text: str) -> dict[str, Any]:
    lines = str(text).replace("\r\n", "\n").split("\n")
    section_headings = [
        "Assumptions and notation",
        "Verified steps",
        "Issues found",
        "Suggested local Python checks",
        "Minimal LaTeX patch",
        "Ledger updates",
        "Next boundary hint",
    ]
    label = ""
    boundary = ""
    confidence = ""
    buffers = {h: [] for h in section_headings}
    current = None
    first_real_seen = False
    for line in lines:
        s = line.strip()
        if not first_real_seen and s:
            if not s.startswith("Boundary:") and s not in section_headings and not s.startswith("Confidence:"):
                label = s
                first_real_seen = True
                continue
        if s.startswith("Boundary:"):
            boundary = s.split(":", 1)[1].strip()
            current = None
            first_real_seen = True
            continue
        if s.startswith("Confidence:"):
            confidence = s.split(":", 1)[1].strip()
            current = None
            continue
        if s in section_headings:
            current = s
            first_real_seen = True
            continue
        if current is not None:
            buffers[current].append(line)
    sec = {k: "\n".join(v).strip() for k, v in buffers.items()}
    pychecks_text = sec["Suggested local Python checks"].strip()
    pychecks = []
    if pychecks_text and not pychecks_text.lower().startswith("no python checks suggested"):
        pychecks = [{
            "purpose": "Suggested local Python checks",
            "description": "Legacy Python verification script extracted from the chunk audit. Review the code block together with the chunk context for the exact claim being tested.",
            "expected_outcome": "The script should run without exceptions or failed assertions, and its printed output should support the claim under review.",
            "code": pychecks_text,
        }]
    return {
        "label": label or "Audit chunk",
        "boundary": boundary,
        "assumptions_and_notation": _clean_lines_to_items(sec["Assumptions and notation"]),
        "verified_steps": _clean_lines_to_items(sec["Verified steps"]),
        "issues": _parse_issues_block(sec["Issues found"]),
        "python_checks": pychecks,
        "latex_patch": sec["Minimal LaTeX patch"].strip(),
        "ledger_updates": _parse_ledger_block(sec["Ledger updates"]),
        "next_boundary_hint": sec["Next boundary hint"].strip(),
        "confidence": confidence or "",
        "_legacy_raw_markdown": text,
    }


def parse_audit_response(resp) -> dict[str, Any]:
    parsed = getattr(resp, "output_parsed", None)
    if isinstance(parsed, dict):
        return parsed
    raw_text = (getattr(resp, "output_text", None) or "").strip()
    if raw_text:
        try:
            return extract_json_object(raw_text)
        except Exception:
            return parse_legacy_markdown_audit(raw_text)
    raw = to_jsonable(resp)
    for item in raw.get("output", []) or []:
        if item.get("type") == "message":
            for content in item.get("content", []) or []:
                if content.get("type") == "output_text":
                    txt = (content.get("text") or "").strip()
                    if txt:
                        try:
                            return extract_json_object(txt)
                        except Exception:
                            return parse_legacy_markdown_audit(txt)
    raise ValueError("Could not locate a structured audit object in the model response.")


def _coerce_audit_payload(audit: Any) -> dict[str, Any]:
    if not isinstance(audit, dict):
        audit = {}

    def _as_str(x: Any) -> str:
        return _strip_unsafe_control_chars(_repair_json_escape_artifacts("" if x is None else str(x)))

    def _as_list_of_str(x: Any) -> list[str]:
        if isinstance(x, list):
            return [_as_str(v) for v in x if _as_str(v).strip()]
        if x is None:
            return []
        s = _as_str(x).strip()
        return [s] if s else []

    def _as_issue_list(x: Any) -> list[dict[str, Any]]:
        out = []
        for it in (x if isinstance(x, list) else []):
            if not isinstance(it, dict):
                continue
            sev = _as_str(it.get("severity", "medium")).lower().strip() or "medium"
            if sev not in {"low", "medium", "high", "critical"}:
                sev = "medium"
            out.append({
                "title": _as_str(it.get("title", "Untitled issue")),
                "severity": sev,
                "location": _as_str(it.get("location", "")),
                "description": _as_str(it.get("description", "")),
                "evidence": _as_str(it.get("evidence", "")),
                "proposed_fix": _as_str(it.get("proposed_fix", "")),
                "tags": _as_list_of_str(it.get("tags", [])),
            })
        return out

    def _as_python_checks(x: Any) -> list[dict[str, str]]:
        out = []
        for it in (x if isinstance(x, list) else []):
            if not isinstance(it, dict):
                continue
            purpose = _as_str(it.get("purpose", "")).strip() or "Python check"
            description = _as_str(it.get("description", "")).strip() or purpose
            expected_outcome = _as_str(it.get("expected_outcome", "")).strip()
            code = _as_str(it.get("code", ""))
            if purpose or description or expected_outcome or code:
                out.append({
                    "purpose": purpose,
                    "description": description,
                    "expected_outcome": expected_outcome,
                    "code": code,
                })
        return out

    ledger = audit.get("ledger_updates") if isinstance(audit.get("ledger_updates"), dict) else {}
    return {
        "label": _as_str(audit.get("label", "")),
        "boundary": _as_str(audit.get("boundary", "")),
        "chunk_too_large": bool(audit.get("chunk_too_large", False)),
        "chunk_split_suggestions": _as_list_of_str(audit.get("chunk_split_suggestions", [])),
        "assumptions_and_notation": _as_list_of_str(audit.get("assumptions_and_notation", [])),
        "verified_steps": _as_list_of_str(audit.get("verified_steps", [])),
        "issues": _as_issue_list(audit.get("issues", [])),
        "python_checks": _as_python_checks(audit.get("python_checks", [])),
        "latex_patch": _as_str(audit.get("latex_patch", "")),
        "ledger_updates": {
            "assumptions": _as_list_of_str(ledger.get("assumptions", [])),
            "notes": _as_list_of_str(ledger.get("notes", [])),
        },
        "next_boundary_hint": _as_str(audit.get("next_boundary_hint", "")),
        "confidence": _as_str(audit.get("confidence", "medium")) or "medium",
    }


def format_list_for_markdown(items: list[str]) -> str:
    if not items:
        return "- None."
    return "\n".join(f"- {normalize_math_delimiters(str(x))}" for x in items)


def _normalize_python_check_entry(
    chk: dict[str, Any],
    chunk_label: str = "",
    chunk_boundary: str = "",
) -> dict[str, str]:
    purpose = str(chk.get("purpose", "") or "").strip() or "Python check"
    description = str(chk.get("description", "") or "").strip()
    if not description:
        lead = "This local verification script tests the following claim"
        if chunk_label:
            lead += f" from {chunk_label}"
        if chunk_boundary:
            lead += f" (boundary: {chunk_boundary})"
        description = (
            f"{lead}: {purpose}. "
            "It is intended as a runnable numerical or symbolic check of the argument discussed in this chunk."
        )
    expected_outcome = str(chk.get("expected_outcome", "") or "").strip()
    if not expected_outcome:
        expected_outcome = (
            "The script should finish without exceptions or failed assertions, and any printed output "
            "should be consistent with the claim described above."
        )
    code = str(chk.get("code", "") or "")
    return {
        "purpose": purpose,
        "description": description,
        "expected_outcome": expected_outcome,
        "code": code,
    }


def render_audit_markdown(audit: dict[str, Any]) -> str:
    issues_md = []
    issues = audit.get("issues", []) or []
    if not issues:
        issues_md.append("- No issues found.")
    else:
        for issue in issues:
            issues_md.append(
                "\n".join([
                    f"### {normalize_math_delimiters(issue.get('title', 'Untitled issue'))} [{issue.get('severity', 'low')}]",
                    f"- Location: {normalize_math_delimiters(issue.get('location', ''))}",
                    f"- Description: {normalize_math_delimiters(issue.get('description', ''))}",
                    f"- Evidence: {normalize_math_delimiters(issue.get('evidence', ''))}",
                    f"- Proposed fix: {normalize_math_delimiters(issue.get('proposed_fix', ''))}",
                    f"- Tags: {', '.join(issue.get('tags', [])) if issue.get('tags') else 'none'}",
                ])
            )
    py_checks = audit.get("python_checks", []) or []
    if py_checks:
        py_md = []
        for chk in py_checks:
            entry = _normalize_python_check_entry(
                chk,
                chunk_label=str(audit.get("label", "") or ""),
                chunk_boundary=str(audit.get("boundary", "") or ""),
            )
            py_md.append(
                "\n".join([
                    f"#### {normalize_math_delimiters(entry['purpose'])}",
                    normalize_math_delimiters(entry["description"]),
                    f"- Expected outcome: {normalize_math_delimiters(entry['expected_outcome'])}",
                    "```python",
                    entry["code"].strip(),
                    "```",
                ])
            )
        python_md = "\n\n".join(py_md)
    else:
        python_md = "- No Python checks suggested."
    latex_patch = (audit.get("latex_patch") or "").strip()
    latex_patch_md = "- No LaTeX patch suggested."
    if latex_patch:
        latex_patch_md = "\n".join(["```latex", latex_patch, "```"])
    split_md = ""
    if audit.get("chunk_too_large"):
        split_md = "\n".join([
            "## Chunk size",
            "- Chunk judged too large.",
            format_list_for_markdown(audit.get("chunk_split_suggestions", [])),
            "",
        ])
    md = "\n\n".join([
        f"# {normalize_math_delimiters(audit.get('label', 'Audit chunk'))}",
        f"**Boundary:** {normalize_math_delimiters(audit.get('boundary', ''))}",
        split_md,
        "## Assumptions and notation",
        format_list_for_markdown(audit.get("assumptions_and_notation", [])),
        "## Verified steps",
        format_list_for_markdown(audit.get("verified_steps", [])),
        "## Issues found",
        "\n\n".join(issues_md),
        "## Suggested local Python checks",
        python_md,
        "## Minimal LaTeX patch",
        latex_patch_md,
        "## Ledger updates",
        "### Assumptions\n" + format_list_for_markdown((audit.get("ledger_updates") or {}).get("assumptions", [])),
        "### Notes\n" + format_list_for_markdown((audit.get("ledger_updates") or {}).get("notes", [])),
        "## Next boundary hint",
        normalize_math_delimiters(audit.get("next_boundary_hint", "None.")),
        f"**Confidence:** {audit.get('confidence', 'medium')}",
    ])
    return _strip_unsafe_control_chars(md)


def _emit_audit_display(audit: dict[str, Any]) -> None:
    if _DISPLAY_AUDIT_HOOK is not None:
        _DISPLAY_AUDIT_HOOK(audit)


def save_patch_and_code_files(session: dict[str, Any], chunk_id: str, audit: dict[str, Any]) -> dict[str, list[str]]:
    root = Path(session["workdir"])
    latex_paths: list[str] = []
    python_paths: list[str] = []
    latex_patch = (audit.get("latex_patch") or "").strip()
    if latex_patch:
        p = root / "latex_patches" / f"{chunk_id}_patch_01.tex"
        p.write_text(latex_patch + "\n", encoding="utf-8")
        latex_paths.append(str(p))
    for idx, chk in enumerate(audit.get("python_checks", []) or [], start=1):
        code = (chk.get("code") or "").strip()
        if code:
            p = root / "python_checks" / f"{chunk_id}_check_{idx:02d}.py"
            p.write_text(code + "\n", encoding="utf-8")
            python_paths.append(str(p))
    return {"latex_paths": latex_paths, "python_paths": python_paths}


def update_ledger_from_audit(session: dict[str, Any], audit: dict[str, Any]) -> None:
    ledger = load_ledger(session)
    updates = audit.get("ledger_updates") or {}
    assumptions = ledger.get("assumptions", [])
    notes = ledger.get("notes", [])
    for item in updates.get("assumptions", []) or []:
        if item not in assumptions:
            assumptions.append(item)
    for item in updates.get("notes", []) or []:
        if item not in notes:
            notes.append(item)
    ledger["assumptions"] = assumptions
    ledger["notes"] = notes
    save_ledger(session, ledger)


def add_issues_from_audit(session: dict[str, Any], chunk_id: str, issues_payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues_state = load_issues(session)
    created = []
    for payload in issues_payload or []:
        issue = {
            "issue_id": f"I{issues_state['next_issue_id']:03d}",
            "chunk_id": chunk_id,
            "status": "open",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "resolved_at": None,
            "title": payload.get("title", "Untitled issue"),
            "severity": payload.get("severity", "low"),
            "location": payload.get("location", ""),
            "description": payload.get("description", ""),
            "evidence": payload.get("evidence", ""),
            "proposed_fix": payload.get("proposed_fix", ""),
            "tags": payload.get("tags", []),
        }
        issues_state["issues"].append(issue)
        created.append(issue)
        issues_state["next_issue_id"] += 1
    save_issues(session, issues_state)
    return created


def _audit_context_db_path(session: dict[str, Any]) -> Path:
    return session_paths(session["workdir"])["audit_context_db"]


def _compact_context_text(text: Any, limit: int = 900) -> str:
    clean = re.sub(r"\s+", " ", _strip_unsafe_control_chars(str(text or ""))).strip()
    return _truncate_text(clean, limit=limit)


def _classify_context_entry_kind(text: str, default: str = "assumption") -> str:
    lower = str(text or "").lower()
    if any(word in lower for word in ("ambiguous", "ambiguity", "unclear", "not clear", "unresolved")):
        return "ambiguity"
    if any(word in lower for word in ("definition", "defined", "denote", "denotes", "write ", "notation", "symbol")):
        return "definition" if "definition" in lower or "defined" in lower else "notation"
    if any(word in lower for word in ("regime", "asymptotic", "parameter range", "for all", "uniformly", "assume")):
        return "regime" if "regime" in lower or "asymptotic" in lower or "uniformly" in lower else "assumption"
    if any(word in lower for word in ("depends on", "dependency", "uses theorem", "uses lemma", "relies on", "requires")):
        return "dependency"
    return default


def _context_base_entry(
    chunk: dict[str, Any],
    kind: str,
    text: str,
    ordinal: int,
    confidence: str,
    **extra: Any,
) -> dict[str, Any]:
    chunk_id = str(chunk.get("chunk_id") or "chunk")
    entry = {
        "entry_id": f"{chunk_id}:{kind}:{ordinal:03d}:{_artifact_timestamp_token()}",
        "time": utc_now(),
        "kind": kind,
        "text": _compact_context_text(text),
        "source_chunk_id": chunk_id,
        "source_chunk_index": chunk.get("chunk_index"),
        "page_start": chunk.get("page_start"),
        "page_end": chunk.get("page_end"),
        "label": chunk.get("label"),
        "boundary": chunk.get("boundary"),
        "confidence": confidence,
    }
    entry.update({key: value for key, value in extra.items() if value is not None})
    return entry


def _append_audit_context_db_entries(
    session: dict[str, Any],
    chunk: dict[str, Any],
    audit: dict[str, Any],
    created_issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    ordinal = 1

    def add(kind: str, text: Any, confidence: str = "source-derived", **extra: Any) -> None:
        nonlocal ordinal
        clean = _compact_context_text(text)
        if not clean:
            return
        entries.append(_context_base_entry(chunk, kind, clean, ordinal, confidence, **extra))
        ordinal += 1

    issue_count = len(audit.get("issues") or [])
    verified_count = len(audit.get("verified_steps") or [])
    notation_count = len(audit.get("assumptions_and_notation") or [])
    summary = (
        f"{chunk.get('chunk_id')}: {chunk.get('label') or chunk.get('boundary') or 'chunk'}; "
        f"pages {chunk.get('page_start')}-{chunk.get('page_end')}; "
        f"{notation_count} notation/assumption items, {verified_count} verified/contextual steps, "
        f"{issue_count} issues; confidence {audit.get('confidence') or 'unknown'}."
    )
    add("chunk_summary", summary, confidence="source-derived")

    for item in audit.get("assumptions_and_notation") or []:
        clean = _compact_context_text(item)
        add(_classify_context_entry_kind(clean, default="notation"), clean, confidence="source-derived")

    for item in audit.get("verified_steps") or []:
        add("verified_step", item, confidence="source-derived")

    ledger = audit.get("ledger_updates") if isinstance(audit.get("ledger_updates"), dict) else {}
    for item in ledger.get("assumptions", []) or []:
        clean = _compact_context_text(item)
        add(_classify_context_entry_kind(clean, default="assumption"), clean, confidence="source-derived")
    for item in ledger.get("notes", []) or []:
        clean = _compact_context_text(item)
        add(_classify_context_entry_kind(clean, default="dependency"), clean, confidence="source-derived")

    for issue in created_issues or []:
        text = " | ".join(
            _compact_context_text(issue.get(key), limit=320)
            for key in ("title", "location", "description")
            if _compact_context_text(issue.get(key), limit=320)
        )
        add(
            "issue",
            text,
            confidence="source-derived",
            issue_id=issue.get("issue_id"),
            severity=issue.get("severity"),
            status=issue.get("status"),
        )

    hint = _compact_context_text(audit.get("next_boundary_hint"))
    if hint:
        add("next_boundary_hint", hint, confidence="source-derived")

    path = _audit_context_db_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        append_jsonl(path, entry)
    return entries


def _read_audit_context_db(session: dict[str, Any]) -> list[dict[str, Any]]:
    path = _audit_context_db_path(session)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries


def _context_query_terms(chunk: dict[str, Any], *, include_generic: bool = False) -> set[str]:
    text = " ".join(
        str(chunk.get(key) or "")
        for key in ("chunk_text", "label", "boundary")
    )
    terms = {item.lower() for item in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text)}
    terms.update(item.lower() for item in re.findall(r"\\[A-Za-z]+", text))
    if not include_generic:
        terms = {
            term
            for term in terms
            if term.lstrip("\\").lower() not in FRESH_CONTEXT_GENERIC_QUERY_TERMS
        }
    return terms


def _context_entry_score(entry: dict[str, Any], query_terms: set[str]) -> int:
    haystack = " ".join(
        str(entry.get(key) or "")
        for key in ("kind", "text", "label", "boundary", "issue_id", "severity")
    ).lower()
    score = sum(1 for term in query_terms if term and term in haystack)
    kind = str(entry.get("kind") or "")
    if kind in {"definition", "notation", "assumption", "regime", "dependency", "ambiguity"}:
        score += 2
    return score


def build_fresh_audit_context_for_chunk(
    session: dict[str, Any],
    chunk: dict[str, Any],
    max_chars: int = 12000,
    recent_summary_limit: int = 4,
) -> dict[str, Any]:
    current_index = _chunk_index_from_chunk_id(chunk.get("chunk_id")) or chunk.get("chunk_index")
    try:
        current_index = int(current_index)
    except Exception:
        current_index = None
    prior_entries = []
    for entry in _read_audit_context_db(session):
        try:
            source_index = int(entry.get("source_chunk_index") or 0)
        except Exception:
            source_index = 0
        if current_index is not None and source_index >= current_index:
            continue
        prior_entries.append(entry)

    def sort_key(entry: dict[str, Any]) -> tuple[int, str]:
        try:
            idx = int(entry.get("source_chunk_index") or 0)
        except Exception:
            idx = 0
        return (idx, str(entry.get("entry_id") or ""))

    summaries = [entry for entry in prior_entries if entry.get("kind") == "chunk_summary"]
    summaries.sort(key=sort_key)
    selected: list[dict[str, Any]] = summaries[-recent_summary_limit:]
    query_terms = _context_query_terms(chunk)

    priority_issue_entries = [
        entry
        for entry in prior_entries
        if entry.get("kind") == "issue"
        and str(entry.get("status") or "open").lower() != "resolved"
        and str(entry.get("severity") or "").lower() in {"critical", "high"}
    ]
    if current_index is not None:
        recent_issue_window = max(int(recent_summary_limit), 4)
        filtered_priority_issues: list[dict[str, Any]] = []
        for entry in priority_issue_entries:
            try:
                source_index = int(entry.get("source_chunk_index") or 0)
            except Exception:
                source_index = 0
            is_recent = source_index > 0 and 0 < current_index - source_index <= recent_issue_window
            if is_recent or (
                query_terms
                and _context_entry_score(entry, query_terms) >= FRESH_CONTEXT_PRIORITY_ISSUE_MIN_SCORE
            ):
                filtered_priority_issues.append(entry)
        priority_issue_entries = filtered_priority_issues
    priority_issue_entries.sort(
        key=lambda entry: (
            0 if str(entry.get("severity") or "").lower() == "critical" else 1,
            sort_key(entry),
        )
    )
    selected.extend(priority_issue_entries)

    selected_ids = {str(entry.get("entry_id") or "") for entry in selected}
    scored = []
    for entry in prior_entries:
        entry_id = str(entry.get("entry_id") or "")
        if entry_id in selected_ids:
            continue
        score = _context_entry_score(entry, query_terms)
        min_score = (
            FRESH_CONTEXT_PRIORITY_ISSUE_MIN_SCORE
            if str(entry.get("kind") or "") == "issue"
            else 1
        )
        if score >= min_score:
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], sort_key(item[1])))
    selected.extend(entry for _score, entry in scored)

    lines = [
        "Retrieved fresh-context audit database context:",
        "Use this compact saved-state context conservatively; the current chunk text below is authoritative.",
        FRESH_CONTEXT_TEXT_FIRST_NOTE,
        "Context provenance:",
        "- Paper-derived context entries summarize saved prior chunk notation, definitions, assumptions, dependencies, verified steps, and boundary hints.",
    ]
    if any(entry.get("kind") == "issue" for entry in selected):
        lines.extend(
            [
                "- Prior audit issue entries are audit-derived warnings, not paper claims.",
                FRESH_CONTEXT_PRIOR_ISSUE_CAUTION,
            ]
        )
    included: list[dict[str, Any]] = []
    for entry in selected:
        kind = str(entry.get("kind") or "context")
        display_kind = "prior audit issue (provisional)" if kind == "issue" else f"paper-derived {kind}"
        label = " | ".join(
            part
            for part in [
                display_kind,
                str(entry.get("source_chunk_id") or ""),
                f"pages {entry.get('page_start')}-{entry.get('page_end')}",
                str(entry.get("issue_id") or ""),
                str(entry.get("severity") or ""),
            ]
            if part.strip()
        )
        line = f"- {label}: {_compact_context_text(entry.get('text'), limit=420)}"
        candidate = "\n".join(lines + [line, "End retrieved fresh-context audit database context."])
        if len(candidate) > int(max_chars):
            break
        lines.append(line)
        included.append(entry)

    if not included:
        lines.append("- No prior context database entries are available yet.")
    lines.append("End retrieved fresh-context audit database context.")
    block = _strip_unsafe_control_chars("\n".join(lines).strip())
    return {
        "block": block,
        "entries": included,
        "entry_count": len(included),
        "chars": len(block),
        "max_chars": int(max_chars),
    }


def _parse_iso_utc(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _seconds_since(ts: str | None) -> float:
    dt = _parse_iso_utc(ts)
    if dt is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


def wait_for_response(response_id: str, poll_every: float = 3.0, max_wait_seconds: Optional[float] = None):
    start = time.time()
    client = _get_client()
    while True:
        resp = client.responses.retrieve(response_id)
        if getattr(resp, "status", None) not in WORKING_STATUSES:
            return resp
        if max_wait_seconds is not None and (time.time() - start) > max_wait_seconds:
            raise TimeoutError(f"Polling exceeded {max_wait_seconds} seconds for response {response_id}")
        time.sleep(poll_every)


def _wait_for_response(response_id: str, poll_every: float = 3.0, max_wait_seconds: Optional[float] = None):
    return wait_for_response(response_id, poll_every=poll_every, max_wait_seconds=max_wait_seconds)


def _save_request_metadata(
    session: dict[str, Any],
    chunk: dict[str, Any],
    request_kwargs: dict[str, Any],
    verification_mode: str,
    used_code_interpreter_tool: bool,
    code_interpreter_file_ids: Optional[list[str]] = None,
    attempt_label: Optional[str] = None,
) -> str:
    root = Path(session["workdir"])
    suffix = f"_{attempt_label}" if attempt_label else ""
    request_path = root / "requests" / f"{chunk['chunk_id']}_{_artifact_timestamp_token()}{suffix}.request.json"
    payload = {
        "time": utc_now(),
        "chunk_id": chunk.get("chunk_id"),
        "chunk_index": chunk.get("chunk_index"),
        "label": chunk.get("label"),
        "boundary": chunk.get("boundary"),
        "audit_context_mode": _normalize_audit_context_mode(session.get("audit_context_mode")),
        "verification_mode": verification_mode,
        "used_code_interpreter_tool": bool(used_code_interpreter_tool),
        "code_interpreter_file_ids": list(code_interpreter_file_ids or []),
        "audit_system_prompt_metadata": session.get("audit_system_prompt_metadata"),
        "request_size_diagnostics": _audit_request_size_diagnostics(session, chunk, request_kwargs),
        "request": to_jsonable(request_kwargs),
    }
    if chunk.get("_pdf_text_only_retry"):
        payload["pdf_attachment"] = {
            "disabled": True,
            "reason": "repeated_file_download_timeout",
            "note": chunk.get("_pdf_attachment_disabled_note") or PDF_TEXT_ONLY_RETRY_NOTE,
        }
    elif chunk.get("_fresh_context_conversation"):
        payload["pdf_attachment"] = {
            "disabled": bool(chunk.get("_suppress_pdf_attachment")),
            "reason": "fresh_context_experimental_text_first",
            "note": chunk.get("_pdf_attachment_disabled_note") or FRESH_CONTEXT_TEXT_FIRST_NOTE,
        }
        payload["fresh_context"] = {
            "fresh_context_conversation": True,
            "main_conversation_id": chunk.get("_main_conversation_id"),
            "fresh_context_conversation_id": chunk.get("_fresh_context_conversation_id"),
            "retrieved_context_entry_count": int(chunk.get("_retrieved_context_entry_count") or 0),
            "retrieved_context_chars": int(chunk.get("_retrieved_context_chars") or 0),
        }
    if chunk.get("_rerun_id") or chunk.get("_extra_rerun_instruction"):
        payload["rerun"] = {
            "rerun_id": chunk.get("_rerun_id"),
            "rerun_kind": chunk.get("_rerun_kind"),
            "rerun_requested_at": chunk.get("_rerun_requested_at"),
            "extra_rerun_instruction": chunk.get("_extra_rerun_instruction"),
            "fresh_rerun_conversation": bool(chunk.get("_fresh_rerun_conversation")),
            "main_conversation_id": chunk.get("_main_conversation_id"),
            "rerun_conversation_id": chunk.get("_fresh_rerun_conversation_id"),
        }
    save_json(request_path, payload)
    return str(request_path)


def _extract_code_interpreter_summary(resp_json: dict[str, Any]) -> dict[str, Any]:
    stack = [resp_json]
    tool_event_count = 0
    file_ids: list[str] = []
    container_ids: list[str] = []
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            typ = cur.get("type")
            if isinstance(typ, str) and "code_interpreter" in typ:
                tool_event_count += 1
            fid = cur.get("file_id")
            if isinstance(fid, str) and fid.startswith("file-"):
                file_ids.append(fid)
            fids = cur.get("file_ids")
            if isinstance(fids, list):
                for item in fids:
                    if isinstance(item, str) and item.startswith("file-"):
                        file_ids.append(item)
            cid = cur.get("container_id")
            if isinstance(cid, str) and cid:
                container_ids.append(cid)
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for item in cur:
                if isinstance(item, (dict, list)):
                    stack.append(item)
    return {
        "used_code_interpreter": tool_event_count > 0,
        "tool_event_count": tool_event_count,
        "file_ids": _dedupe_strings(file_ids)[:20],
        "container_ids": _dedupe_strings(container_ids)[:5],
    }


def _response_failure_error_text(failure_summary: dict[str, Any]) -> str:
    error = failure_summary.get("error")
    parts: list[str] = []
    if isinstance(error, dict):
        parts.extend(str(error.get(key) or "") for key in ("code", "message"))
    elif error:
        parts.append(str(error))
    parts.extend(
        str(failure_summary.get(key) or "")
        for key in ("incomplete_details", "last_error", "status")
    )
    return " ".join(part for part in parts if part).strip()


def _is_file_download_timeout_failure(failure_summary: dict[str, Any]) -> bool:
    text = _response_failure_error_text(failure_summary).lower()
    return "timeout while downloading" in text and "fileserviceuploads" in text


def _retryable_response_failure_reason(failure_summary: dict[str, Any]) -> Optional[str]:
    if _is_file_download_timeout_failure(failure_summary):
        return "file_download_timeout"
    return None


def _failed_chunk_events(session: dict[str, Any], chunk_id: str) -> list[dict[str, Any]]:
    log_path = Path(session["workdir"]) / "logs" / "failed_chunks.jsonl"
    if not log_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if isinstance(item, dict) and str(item.get("chunk_id") or "") == chunk_id:
            events.append(item)
    return events


def _file_download_timeout_retry_mode(session: dict[str, Any], chunk: dict[str, Any]) -> str:
    chunk_id = str(chunk.get("chunk_id") or "")
    if not chunk_id or not session.get("pdf_file_id"):
        return FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_NONE
    failures = _failed_chunk_events(session, chunk_id)
    if not failures:
        return FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_NONE
    latest_failure = failures[-1]
    if _retryable_response_failure_reason(latest_failure) != "file_download_timeout":
        return FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_NONE
    timeout_count = _file_download_timeout_failure_count(session, chunk_id, failures=failures)
    if timeout_count >= 2:
        return FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_TEXT_ONLY
    return FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_REATTACH


def _file_download_timeout_failure_count(
    session: dict[str, Any],
    chunk_id: str,
    failures: Optional[list[dict[str, Any]]] = None,
) -> int:
    events = failures if failures is not None else _failed_chunk_events(session, chunk_id)
    return sum(
        1
        for failure in events
        if _retryable_response_failure_reason(failure) == "file_download_timeout"
    )


def _should_reattach_pdf_for_chunk_retry(session: dict[str, Any], chunk: dict[str, Any]) -> bool:
    return _file_download_timeout_retry_mode(session, chunk) == FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_REATTACH


def _file_download_timeout_auto_retry_decision(
    session: dict[str, Any],
    chunk: dict[str, Any],
    attempts_used: int,
    max_retries: int = FILE_DOWNLOAD_TIMEOUT_AUTO_RETRY_MAX,
    delay_seconds: float = FILE_DOWNLOAD_TIMEOUT_AUTO_RETRY_DELAY_SECONDS,
) -> dict[str, Any]:
    chunk_id = str(chunk.get("chunk_id") or "")
    if session.get("pause_requested_at"):
        return {"auto_retry": False, "reason": "pause_requested"}
    failures = _failed_chunk_events(session, chunk_id) if chunk_id else []
    latest_failure = failures[-1] if failures else None
    if not chunk_id or not isinstance(latest_failure, dict):
        return {"auto_retry": False, "reason": "no_failed_chunk_event"}
    if _retryable_response_failure_reason(latest_failure) != "file_download_timeout":
        return {"auto_retry": False, "reason": "not_file_download_timeout"}
    if int(attempts_used or 0) >= int(max_retries):
        return {
            "auto_retry": False,
            "reason": "max_auto_retries_exhausted",
            "attempts_used": int(attempts_used or 0),
            "max_retries": int(max_retries),
            "latest_failure_path": latest_failure.get("failure_summary_path"),
        }
    retry_mode = _file_download_timeout_retry_mode(session, chunk)
    if retry_mode == FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_REATTACH:
        strategy = "pdf_reattachment_retry"
        strategy_label = "PDF reattachment retry"
    elif retry_mode == FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_TEXT_ONLY:
        strategy = "fresh_conversation_text_only_retry"
        strategy_label = "fresh-conversation text-only retry"
    else:
        return {
            "auto_retry": False,
            "reason": "no_retry_mode",
            "latest_failure_path": latest_failure.get("failure_summary_path"),
        }
    attempt = int(attempts_used or 0) + 1
    return {
        "auto_retry": True,
        "reason": "file_download_timeout",
        "attempt": attempt,
        "max_retries": int(max_retries),
        "delay_seconds": float(delay_seconds),
        "retry_mode": retry_mode,
        "strategy": strategy,
        "strategy_label": strategy_label,
        "file_download_timeout_count": _file_download_timeout_failure_count(
            session,
            chunk_id,
            failures=failures,
        ),
        "latest_failure_path": latest_failure.get("failure_summary_path"),
    }


def _record_file_download_timeout_auto_retry_event(
    session: dict[str, Any],
    chunk: dict[str, Any],
    decision: dict[str, Any],
    action: str,
) -> dict[str, Any]:
    root = Path(session["workdir"])
    chunk_id = str(chunk.get("chunk_id") or "")
    attempt = int(decision.get("attempt") or decision.get("attempts_used") or 0)
    max_retries = int(decision.get("max_retries") or FILE_DOWNLOAD_TIMEOUT_AUTO_RETRY_MAX)
    strategy_label = str(decision.get("strategy_label") or decision.get("strategy") or "automatic retry")
    if action == "scheduled":
        message = (
            f"{chunk_id} hit a retryable API file-download timeout. "
            f"Automatic retry {attempt}/{max_retries} scheduled after "
            f"{float(decision.get('delay_seconds') or 0.0):.0f}s: {strategy_label}."
        )
    else:
        message = (
            f"{chunk_id} hit retryable API file-download timeouts, but automatic retries are exhausted "
            f"({max_retries}/{max_retries}). Audit paused; manual Resume Audit or inspection is required."
        )
    event = {
        "time": utc_now(),
        "action": action,
        "chunk_id": chunk_id,
        "chunk_index": chunk.get("chunk_index"),
        "attempt": attempt,
        "max_retries": max_retries,
        "retry_mode": decision.get("retry_mode"),
        "strategy": decision.get("strategy"),
        "delay_seconds": decision.get("delay_seconds"),
        "file_download_timeout_count": decision.get("file_download_timeout_count"),
        "latest_failure_path": decision.get("latest_failure_path"),
        "message": message,
    }
    append_jsonl(root / "logs" / "file_download_timeout_auto_retries.jsonl", event)
    session["last_file_download_timeout_auto_retry"] = event
    session["updated_at"] = utc_now()
    save_session(session)
    status = load_status(session)
    status["last_file_download_timeout_auto_retry"] = event
    status["updated_at"] = event["time"]
    save_status(session, status)
    return event


def _pause_audit_after_chunk_failure(
    session: dict[str, Any],
    chunk: dict[str, Any],
    resp,
    verification_mode: str,
    used_code_interpreter_tool: bool = False,
    request_path: Optional[str] = None,
    note: Optional[str] = None,
    discovered_during_recovery: bool = False,
) -> dict[str, Any]:
    root = Path(session["workdir"])
    chunk_id = chunk["chunk_id"]
    response_id = getattr(resp, "id", None) or f"failed_{_artifact_timestamp_token()}"
    resp_json = to_jsonable(resp)
    raw_json_path = root / "responses" / f"{chunk_id}_{response_id}.json"
    save_json(raw_json_path, resp_json)
    raw_text = (getattr(resp, "output_text", None) or "").strip()
    raw_text_path = root / "responses" / f"{chunk_id}_{response_id}.raw.txt"
    if raw_text:
        raw_text_path.write_text(raw_text, encoding="utf-8")
    tool_summary = {
        "used_code_interpreter": False,
        "tool_event_count": 0,
        "file_ids": [],
        "container_ids": [],
    }
    if verification_mode == "code_interpreter" or used_code_interpreter_tool:
        tool_summary = _extract_code_interpreter_summary(resp_json)
    failure_summary = {
        "time": utc_now(),
        "chunk_id": chunk_id,
        "chunk_index": chunk.get("chunk_index"),
        "label": chunk.get("label"),
        "boundary": chunk.get("boundary"),
        "response_id": response_id,
        "status": getattr(resp, "status", None),
        "error": resp_json.get("error"),
        "incomplete_details": resp_json.get("incomplete_details"),
        "last_error": resp_json.get("last_error"),
        "verification_mode": verification_mode,
        "used_code_interpreter_tool": bool(used_code_interpreter_tool),
        "tool_summary": tool_summary,
        "request_path": request_path,
        "raw_response_path": str(raw_json_path),
        "raw_text_path": str(raw_text_path) if raw_text else None,
        "discovered_during_recovery": bool(discovered_during_recovery),
        "note": note,
    }
    if chunk.get("_pdf_text_only_retry"):
        failure_summary["pdf_attachment"] = {
            "disabled": True,
            "reason": "repeated_file_download_timeout",
            "note": chunk.get("_pdf_attachment_disabled_note") or PDF_TEXT_ONLY_RETRY_NOTE,
        }
    elif chunk.get("_fresh_context_conversation"):
        failure_summary["pdf_attachment"] = {
            "disabled": bool(chunk.get("_suppress_pdf_attachment")),
            "reason": "fresh_context_experimental_text_first",
            "note": chunk.get("_pdf_attachment_disabled_note") or FRESH_CONTEXT_TEXT_FIRST_NOTE,
        }
        failure_summary["fresh_context"] = {
            "fresh_context_conversation": True,
            "main_conversation_id": chunk.get("_main_conversation_id"),
            "fresh_context_conversation_id": chunk.get("_fresh_context_conversation_id"),
            "retrieved_context_entry_count": int(chunk.get("_retrieved_context_entry_count") or 0),
            "retrieved_context_chars": int(chunk.get("_retrieved_context_chars") or 0),
        }
    if chunk.get("_fresh_rerun_conversation"):
        failure_summary["rerun_conversation"] = {
            "fresh_rerun_conversation": True,
            "rerun_kind": chunk.get("_rerun_kind"),
            "main_conversation_id": chunk.get("_main_conversation_id"),
            "rerun_conversation_id": chunk.get("_fresh_rerun_conversation_id"),
        }
    retryable_reason = _retryable_response_failure_reason(failure_summary)
    failure_summary["retryable"] = bool(retryable_reason)
    failure_summary["retryable_reason"] = retryable_reason
    if retryable_reason == "file_download_timeout":
        failure_summary["same_chunk_file_download_timeout_count"] = (
            _file_download_timeout_failure_count(session, chunk_id) + 1
        )
    failure_path = root / "responses" / f"{chunk_id}_{response_id}.failure.json"
    save_json(failure_path, failure_summary)
    failure_summary["failure_summary_path"] = str(failure_path)
    append_jsonl(root / "logs" / "failed_chunks.jsonl", failure_summary)
    session["last_response_id"] = response_id
    session["pending"] = None
    session["next_chunk_index"] = int(chunk.get("chunk_index") or session.get("next_chunk_index") or 1)
    if retryable_reason == "file_download_timeout":
        if not chunk.get("_fresh_rerun_conversation"):
            session["pdf_attached_in_conversation"] = False
        session["last_retryable_failure_reason"] = retryable_reason
    session["updated_at"] = utc_now()
    save_session(session)
    manifest = load_manifest(session)
    paused_at = utc_now()
    status = load_status(session)
    status.update({
        "status": "paused",
        "pause_reason": "retryable_response_failure" if retryable_reason else "chunk_failed",
        "paused_at": paused_at,
        "current_chunk_id": chunk_id,
        "chunks_total": len(manifest.get("chunks", [])),
        "estimated_pages_total": manifest.get("pdf_page_count", 0),
        "current_chunk_elapsed_seconds": 0.0,
        "audit_started_at": session.get("audit_started_at", session.get("created_at")),
        "audit_finished_at": None,
        "updated_at": paused_at,
    })
    save_status(session, status)
    return failure_summary


def _is_ci_invalid_prompt_failure(failure_summary: Optional[dict[str, Any]]) -> bool:
    if not isinstance(failure_summary, dict):
        return False
    err = failure_summary.get("error")
    if not isinstance(err, dict):
        return False
    return str(err.get("code") or "").strip().lower() == "invalid_prompt"


def _record_ci_invalid_prompt_local_fallback(
    session: dict[str, Any],
    chunk: dict[str, Any],
    failure_summary: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    root = Path(session["workdir"])
    event = {
        "time": utc_now(),
        "source": source,
        "reason": "code_interpreter_invalid_prompt",
        "chunk_id": chunk.get("chunk_id"),
        "chunk_index": chunk.get("chunk_index"),
        "label": chunk.get("label"),
        "original_response_id": failure_summary.get("response_id"),
        "original_status": failure_summary.get("status"),
        "error": failure_summary.get("error"),
        "failure_summary_path": failure_summary.get("failure_summary_path"),
        "request_path": failure_summary.get("request_path"),
        "retry_verification_mode": "local_python_only",
    }
    fallback_log_path = root / "logs" / "ci_invalid_prompt_fallbacks.jsonl"
    append_jsonl(fallback_log_path, event)
    failure_summary["auto_local_fallback_triggered"] = True
    failure_summary["auto_local_fallback_reason"] = "code_interpreter_invalid_prompt"
    failure_summary["auto_local_fallback_log_path"] = str(fallback_log_path)
    failure_path = failure_summary.get("failure_summary_path")
    if isinstance(failure_path, str) and failure_path:
        try:
            save_json(Path(failure_path), failure_summary)
        except Exception:
            pass
    session["last_ci_invalid_prompt_fallback"] = event
    session["updated_at"] = utc_now()
    save_session(session)
    return event


def _build_code_interpreter_tools(
    session: dict[str, Any],
    extra_file_ids: Optional[list[str]] = None,
    include_memory_limit: bool = True,
) -> list[dict[str, Any]]:
    container = {"type": "auto"}
    file_ids = []
    for fid in [session.get("pdf_file_id")] + list(extra_file_ids or []):
        if isinstance(fid, str) and fid and fid.startswith("file-"):
            file_ids.append(fid)
    file_ids = _dedupe_strings(file_ids)
    if file_ids:
        container["file_ids"] = file_ids
    memory_limit = session.get("code_interpreter_memory_limit")
    if include_memory_limit and memory_limit:
        container["memory_limit"] = str(memory_limit)
    return [{"type": "code_interpreter", "container": container}]


def _request_input_text_parts(request_kwargs: dict[str, Any], role: Optional[str] = None) -> list[str]:
    input_payload = request_kwargs.get("input")
    if not isinstance(input_payload, list):
        return []
    out: list[str] = []
    for message in input_payload:
        if not isinstance(message, dict):
            continue
        if role is not None and message.get("role") != role:
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "input_text":
                out.append(str(item.get("text") or ""))
    return out


def _request_includes_pdf_attachment(request_kwargs: dict[str, Any]) -> bool:
    input_payload = request_kwargs.get("input")
    if not isinstance(input_payload, list):
        return False
    for message in input_payload:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "input_file":
                return True
    return False


def _prompt_section_length(text: str, start_marker: str, end_markers: list[str]) -> int:
    start = text.find(start_marker)
    if start < 0:
        return 0
    end_candidates = [
        idx
        for marker in end_markers
        if (idx := text.find(marker, start + len(start_marker))) >= 0
    ]
    end = min(end_candidates) if end_candidates else len(text)
    return len(text[start:end].strip())


def _audit_request_size_diagnostics(
    session: dict[str, Any],
    chunk: dict[str, Any],
    request_kwargs: dict[str, Any],
) -> dict[str, Any]:
    developer_texts = _request_input_text_parts(request_kwargs, role="developer")
    user_texts = _request_input_text_parts(request_kwargs, role="user")
    user_prompt_text = "\n".join(user_texts)
    system_prompt = str(session.get("audit_system_prompt") or AUDIT_SYSTEM_PROMPT)
    audit_context_mode = _normalize_audit_context_mode(session.get("audit_context_mode"))
    fresh_context = bool(chunk.get("_fresh_context_conversation")) or audit_context_mode == AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL
    text_only_retry = bool(chunk.get("_pdf_text_only_retry"))
    pdf_suppressed = bool(chunk.get("_suppress_pdf_attachment"))
    fresh_rerun = bool(chunk.get("_fresh_rerun_conversation"))
    developer_prompt_included = bool(developer_texts)
    conversation_state = "reused_seeded_conversation"
    if fresh_context:
        conversation_state = "fresh_context_conversation"
    elif text_only_retry and fresh_rerun:
        conversation_state = "fresh_rerun_text_only_retry_conversation"
    elif text_only_retry:
        conversation_state = "fresh_text_only_retry_conversation"
    elif fresh_rerun:
        conversation_state = "fresh_rerun_conversation"
    elif developer_prompt_included:
        conversation_state = "unseeded_or_new_conversation"

    previous_conversation_id = None
    last_text_only = session.get("last_text_only_file_timeout_retry")
    if text_only_retry and isinstance(last_text_only, dict) and last_text_only.get("chunk_id") == chunk.get("chunk_id"):
        previous_conversation_id = last_text_only.get("previous_conversation_id")

    return {
        "audit_context_mode": audit_context_mode,
        "audit_system_prompt_length": len(system_prompt),
        "developer_prompt_included": developer_prompt_included,
        "developer_prompt_payload_length": sum(len(text) for text in developer_texts),
        "user_prompt_length": sum(len(text) for text in user_texts),
        "total_input_text_length": sum(len(text) for text in developer_texts + user_texts),
        "chunk_text_length": len(str(chunk.get("chunk_text") or "")),
        "running_audit_context_length": _prompt_section_length(
            user_prompt_text,
            "Running audit context from earlier chunks:",
            ["Paper macro glossary for this chunk:", "Chunk text:"],
        ),
        "retrieved_fresh_context_length": _prompt_section_length(
            user_prompt_text,
            "Retrieved fresh-context audit database context:",
            ["Paper macro glossary for this chunk:", "Chunk text:"],
        ),
        "tex_macro_glossary_length": _prompt_section_length(
            user_prompt_text,
            "Paper macro glossary for this chunk:",
            ["Chunk text:"],
        ),
        "pdf_attachment_included": _request_includes_pdf_attachment(request_kwargs),
        "pdf_attachment_suppressed": pdf_suppressed,
        "pdf_attached_in_conversation_before_request": bool(session.get("pdf_attached_in_conversation", False)),
        "text_only_fallback_active": text_only_retry,
        "conversation_id": request_kwargs.get("conversation"),
        "conversation_state": conversation_state,
        "fresh_conversation_for_text_only_retry": text_only_retry,
        "fresh_context_conversation": fresh_context,
        "retrieved_context_entry_count": int(chunk.get("_retrieved_context_entry_count") or 0),
        "retrieved_context_chars": int(chunk.get("_retrieved_context_chars") or 0),
        "fresh_rerun_conversation": fresh_rerun,
        "rerun_kind": chunk.get("_rerun_kind"),
        "original_conversation_id": chunk.get("_main_conversation_id"),
        "rerun_conversation_id": chunk.get("_fresh_rerun_conversation_id"),
        "previous_conversation_id": previous_conversation_id,
    }


def update_usage_from_response(session: dict[str, Any], chunk_id: str, resp, elapsed_seconds: float = 0.0) -> dict[str, Any]:
    usage_obj = to_jsonable(getattr(resp, "usage", {}) or {})
    return update_usage_from_usage_obj(
        session,
        chunk_id,
        usage_obj,
        model=session.get("model") or DEFAULT_MODEL,
        elapsed_seconds=elapsed_seconds,
    )


def _format_cache_diagnostics_for_log(diagnostics: dict[str, Any]) -> str:
    input_tokens = int(diagnostics.get("input_tokens", 0) or 0)
    if input_tokens <= 0:
        return "Cache: n/a"
    cached_tokens = int(diagnostics.get("cached_input_tokens", 0) or 0)
    percent = diagnostics.get("cached_input_percent")
    if percent is None:
        ratio = diagnostics.get("cached_input_ratio")
        percent = float(ratio or 0.0) * 100.0
    return f"Cache: {float(percent):.1f}% ({cached_tokens:,}/{input_tokens:,} input)"


def _require_prompt_builder() -> Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]:
    if _PROMPT_BUILDER_HOOK is None:
        raise RuntimeError(
            "No prompt builder hook is registered. "
            "Call set_live_audit_hooks(prompt_builder=...) before starting or resuming an audit."
        )
    return _PROMPT_BUILDER_HOOK


def finalize_chunk(
    session: dict[str, Any],
    chunk: dict[str, Any],
    resp,
    display_output: bool = True,
    verification_mode: str = "local_python_only",
    used_code_interpreter_tool: bool = False,
) -> dict[str, Any]:
    root = Path(session["workdir"])
    chunk_id = chunk["chunk_id"]
    verification_mode = _normalize_verification_mode(verification_mode)
    raw_json_path = root / "responses" / f"{chunk_id}_{resp.id}.json"
    resp_json = to_jsonable(resp)
    save_json(raw_json_path, resp_json)
    raw_text = (resp.output_text or "").strip()
    raw_text_path = root / "responses" / f"{chunk_id}_{resp.id}.raw.txt"
    if raw_text:
        raw_text_path.write_text(raw_text, encoding="utf-8")
    try:
        audit = _coerce_audit_payload(parse_audit_response(resp))
    except Exception as e:
        log_path = root / "logs" / "parse_failures.jsonl"
        append_jsonl(log_path, {
            "time": utc_now(),
            "chunk_id": chunk_id,
            "response_id": getattr(resp, "id", None),
            "error": repr(e),
            "raw_text_path": str(raw_text_path) if raw_text else None,
        })
        raise RuntimeError(
            f"Could not parse structured output for {chunk_id}. "
            f"See {raw_text_path.name if raw_text else raw_json_path.name} and logs/parse_failures.jsonl"
        ) from e
    audit.setdefault("label", chunk["label"])
    audit.setdefault("boundary", chunk["boundary"])
    audit.setdefault("chunk_too_large", False)
    audit.setdefault("chunk_split_suggestions", [])
    audit.setdefault("assumptions_and_notation", [])
    audit.setdefault("verified_steps", [])
    audit.setdefault("issues", [])
    audit.setdefault("python_checks", [])
    audit.setdefault("latex_patch", "")
    audit.setdefault("ledger_updates", {"assumptions": [], "notes": []})
    audit.setdefault("next_boundary_hint", "")
    audit.setdefault("confidence", "medium")
    structured_path = root / "responses" / f"{chunk_id}.structured.json"
    save_json(structured_path, audit)
    md_text = render_audit_markdown(audit)
    md_path = root / "responses" / f"{chunk_id}.md"
    md_path.write_text(md_text, encoding="utf-8")
    verification_summary = {
        "used_code_interpreter": False,
        "tool_event_count": 0,
        "file_ids": [],
        "container_ids": [],
    }
    verification_path = None
    if verification_mode == "code_interpreter" or used_code_interpreter_tool:
        verification_summary = _extract_code_interpreter_summary(resp_json)
        verification_payload = {
            "time": utc_now(),
            "chunk_id": chunk_id,
            "response_id": getattr(resp, "id", None),
            "verification_mode": verification_mode,
            "used_code_interpreter_tool": bool(used_code_interpreter_tool),
            "summary": verification_summary,
        }
        verification_path = root / "responses" / f"{chunk_id}_{resp.id}.verification.json"
        save_json(verification_path, verification_payload)
        if session.get("reuse_code_interpreter_container") and verification_summary.get("container_ids"):
            session["code_interpreter_container_id"] = verification_summary["container_ids"][0]
    extracted = save_patch_and_code_files(session, chunk_id, audit)
    created_issues = add_issues_from_audit(session, chunk_id, audit.get("issues", []))
    update_ledger_from_audit(session, audit)
    context_entries = _append_audit_context_db_entries(session, chunk, audit, created_issues)
    pending = session.get("pending") or {}
    elapsed_seconds = _seconds_since(pending.get("started_at") or pending.get("created_at"))
    usage_update = update_usage_from_response(session, chunk_id, resp, elapsed_seconds=elapsed_seconds)
    usage_diagnostics = usage_update.get("usage_diagnostics") or usage_cache_diagnostics(usage_update["usage"])
    record = {
        "time": utc_now(),
        "chunk_id": chunk_id,
        "chunk_index": chunk["chunk_index"],
        "label": chunk["label"],
        "boundary": chunk["boundary"],
        "source_kind": chunk["source_kind"],
        "page_start": chunk["page_start"],
        "page_end": chunk["page_end"],
        "paper_progress_end": chunk["paper_progress_end"],
        "response_id": resp.id,
        "request_path": pending.get("request_path"),
        "raw_response_path": str(raw_json_path),
        "structured_response_path": str(structured_path),
        "markdown_path": str(md_path),
        "latex_paths": extracted["latex_paths"],
        "python_paths": extracted["python_paths"],
        "issue_ids": [x["issue_id"] for x in created_issues],
        "cost_usd": usage_update["cost"]["total_cost"],
        "usage": usage_update["usage"],
        "usage_diagnostics": usage_diagnostics,
        "elapsed_seconds": float(elapsed_seconds or 0.0),
        "verification_mode": verification_mode,
        "verification_summary": verification_summary,
        "verification_path": str(verification_path) if verification_path else None,
        "audit_context_db_entries": len(context_entries),
    }
    if chunk.get("_pdf_text_only_retry"):
        record["pdf_attachment"] = {
            "disabled": True,
            "reason": "repeated_file_download_timeout",
            "note": chunk.get("_pdf_attachment_disabled_note") or PDF_TEXT_ONLY_RETRY_NOTE,
        }
    if chunk.get("_fresh_rerun_conversation"):
        record["rerun_conversation"] = {
            "fresh_rerun_conversation": True,
            "rerun_kind": chunk.get("_rerun_kind"),
            "main_conversation_id": chunk.get("_main_conversation_id"),
            "rerun_conversation_id": chunk.get("_fresh_rerun_conversation_id"),
        }
    if chunk.get("_fresh_context_conversation"):
        record["fresh_context"] = {
            "fresh_context_conversation": True,
            "main_conversation_id": chunk.get("_main_conversation_id"),
            "fresh_context_conversation_id": chunk.get("_fresh_context_conversation_id"),
            "retrieved_context_entry_count": int(chunk.get("_retrieved_context_entry_count") or 0),
            "retrieved_context_chars": int(chunk.get("_retrieved_context_chars") or 0),
            "pdf_attachment_suppressed": bool(chunk.get("_suppress_pdf_attachment")),
        }
    append_jsonl(session_paths(session["workdir"])["chunk_records"], record)
    latest_session = _resolve_session(session["pdf_path"]) if session.get("pdf_path") else session
    if session.get("code_interpreter_container_id"):
        latest_session["code_interpreter_container_id"] = session.get("code_interpreter_container_id")
    latest_session["last_response_id"] = resp.id
    latest_session["next_chunk_index"] = chunk["chunk_index"] + 1
    latest_session["pending"] = None
    latest_session["updated_at"] = utc_now()
    if not chunk.get("_fresh_rerun_conversation") and not chunk.get("_fresh_context_conversation"):
        latest_session["pdf_attached_in_conversation"] = not bool(chunk.get("_pdf_text_only_retry"))
        latest_session["developer_prompt_seeded"] = True
    if chunk["chunk_index"] >= len(load_manifest(latest_session)["chunks"]):
        latest_session["audit_finished_at"] = utc_now()
    save_session(latest_session)
    session = latest_session
    manifest = load_manifest(session)
    usage_state = usage_update["usage_state"]
    status = load_status(session)
    status.update({
        "status": "running" if chunk["chunk_index"] < len(manifest["chunks"]) else "completed",
        "progress_pct": round(100.0 * chunk["paper_progress_end"], 1),
        "current_chunk_id": chunk_id,
        "chunks_completed": chunk["chunk_index"],
        "chunks_total": len(manifest["chunks"]),
        "estimated_pages_completed": chunk["page_end"],
        "estimated_pages_total": manifest["pdf_page_count"],
        "cost_usd": usage_state["totals"]["cost_usd"],
        "last_chunk_usage_diagnostics": {
            "chunk_id": chunk_id,
            "cost_usd": record["cost_usd"],
            **usage_diagnostics,
        },
        "total_audit_seconds": float(usage_state["totals"].get("audit_seconds", 0.0) or 0.0),
        "current_chunk_elapsed_seconds": 0.0,
        "audit_started_at": session.get("audit_started_at", session.get("created_at")),
        "audit_finished_at": session.get("audit_finished_at"),
    })
    if chunk["chunk_index"] >= len(manifest["chunks"]):
        status["status"] = "completed"
        status["progress_pct"] = 100.0
        status["estimated_pages_completed"] = manifest["pdf_page_count"]
    save_status(session, status)
    if display_output:
        print(
            f"[{chunk_id}] Progress: {status['progress_pct']:.1f}% | "
            f"Pages: {status['estimated_pages_completed']}/{status['estimated_pages_total']} | "
            f"Chunk cost: ${record['cost_usd']:.4f} | "
            f"Chunk time: {format_duration(record['elapsed_seconds'])} | "
            f"{_format_cache_diagnostics_for_log(usage_diagnostics)} | "
            f"Cumulative cost: ${usage_state['totals']['cost_usd']:.4f} | "
            f"Total audit time: {format_duration(usage_state['totals'].get('audit_seconds', 0.0))} | "
            f"Total tokens: {usage_state['totals']['total_tokens']}"
        )
        if usage_diagnostics.get("warning"):
            print(f"[{chunk_id}] Warning: {usage_diagnostics['warning']}")
        if verification_mode != "local_python_only" or verification_summary.get("used_code_interpreter"):
            print(
                f"[{chunk_id}] Verification mode: {verification_mode} | "
                f"CI tool events: {verification_summary.get('tool_event_count', 0)}"
            )
        _emit_audit_display(audit)
    return {"audit": audit, "record": record}


def process_one_chunk(
    session: dict[str, Any],
    chunk: dict[str, Any],
    poll_every: float = 3.0,
    max_wait_seconds: Optional[float] = None,
    display_output: bool = True,
    verification_mode: str = "local_python_only",
    code_interpreter_file_ids: Optional[list[str]] = None,
    allow_ci_failure_fallback: bool = True,
) -> dict[str, Any]:
    verification_mode = _normalize_verification_mode(verification_mode)
    if verification_mode == "code_interpreter" and not session.get("use_code_interpreter", False):
        raise RuntimeError(
            "verification_mode='code_interpreter' requested, but this session has use_code_interpreter=False. "
            "Enable use_code_interpreter when creating the session or in audit_the_paper(...)."
        )
    audit_context_mode = _normalize_audit_context_mode(session.get("audit_context_mode"))
    file_timeout_retry_mode = _file_download_timeout_retry_mode(session, chunk)
    fresh_rerun_conversation = bool(chunk.get("_fresh_rerun_conversation"))
    fresh_context_conversation = (
        audit_context_mode == AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL
        and not fresh_rerun_conversation
    )
    main_conversation_id = str(session.get("conversation_id") or "")
    request_session = session
    if fresh_context_conversation:
        conversation = _get_client().conversations.create()
        chunk = dict(chunk)
        chunk["_fresh_context_conversation"] = True
        chunk["_fresh_context_conversation_id"] = conversation.id
        chunk["_main_conversation_id"] = main_conversation_id
        chunk["_suppress_pdf_attachment"] = True
        chunk["_pdf_attachment_disabled_note"] = FRESH_CONTEXT_TEXT_FIRST_NOTE
        request_session = dict(session)
        request_session["conversation_id"] = conversation.id
        request_session["pdf_attached_in_conversation"] = False
        request_session["developer_prompt_seeded"] = False
        request_session["audit_context_mode"] = audit_context_mode
    elif fresh_rerun_conversation:
        conversation = _get_client().conversations.create()
        chunk = dict(chunk)
        chunk["_fresh_rerun_conversation"] = True
        chunk["_fresh_rerun_conversation_id"] = conversation.id
        chunk["_main_conversation_id"] = main_conversation_id
        request_session = dict(session)
        request_session["conversation_id"] = conversation.id
        request_session["pdf_attached_in_conversation"] = False
        request_session["developer_prompt_seeded"] = False
        if file_timeout_retry_mode == FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_TEXT_ONLY:
            chunk["_pdf_text_only_retry"] = True
            chunk["_suppress_pdf_attachment"] = True
            chunk["_pdf_attachment_disabled_note"] = PDF_TEXT_ONLY_RETRY_NOTE
            request_session["last_text_only_file_timeout_retry"] = {
                "chunk_id": chunk.get("chunk_id"),
                "started_at": utc_now(),
                "previous_conversation_id": main_conversation_id,
                "conversation_id": conversation.id,
                "reason": "repeated_file_download_timeout",
                "note": PDF_TEXT_ONLY_RETRY_NOTE,
            }
    elif file_timeout_retry_mode == FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_TEXT_ONLY:
        previous_conversation_id = str(session.get("conversation_id") or "")
        conversation = _get_client().conversations.create()
        session["conversation_id"] = conversation.id
        session["pdf_attached_in_conversation"] = False
        session["developer_prompt_seeded"] = False
        session["last_text_only_file_timeout_retry"] = {
            "chunk_id": chunk.get("chunk_id"),
            "started_at": utc_now(),
            "previous_conversation_id": previous_conversation_id,
            "conversation_id": conversation.id,
            "reason": "repeated_file_download_timeout",
            "note": PDF_TEXT_ONLY_RETRY_NOTE,
        }
        session["updated_at"] = utc_now()
        save_session(session)
        chunk = dict(chunk)
        chunk["_pdf_text_only_retry"] = True
        chunk["_suppress_pdf_attachment"] = True
        chunk["_pdf_attachment_disabled_note"] = PDF_TEXT_ONLY_RETRY_NOTE
    elif file_timeout_retry_mode == FILE_DOWNLOAD_TIMEOUT_RETRY_MODE_REATTACH:
        session["pdf_attached_in_conversation"] = False
        session["updated_at"] = utc_now()
        save_session(session)
    user_input = _require_prompt_builder()(request_session, chunk)
    if not request_session.get("developer_prompt_seeded", False):
        audit_system_prompt = str(request_session.get("audit_system_prompt") or AUDIT_SYSTEM_PROMPT)
        input_payload = [{"role": "developer", "content": [{"type": "input_text", "text": audit_system_prompt}]}] + user_input
    else:
        input_payload = user_input
    verification_instruction = ""
    if verification_mode == "code_interpreter":
        verification_instruction = (
            "Verification mode for this chunk: code_interpreter. "
            "Use the code_interpreter tool when numeric/symbolic verification is needed. "
            "Keep python_checks concise local fallbacks when useful."
        )
    elif verification_mode == "none":
        verification_instruction = (
            "Verification mode for this chunk: none. "
            "Skip optional numeric/symbolic code verification and keep python_checks empty unless strictly necessary."
        )
    if verification_instruction and input_payload:
        try:
            content = input_payload[-1].get("content")
            if isinstance(content, list):
                content.append({"type": "input_text", "text": verification_instruction})
        except Exception:
            pass
    prompt_path = Path(session["workdir"]) / "prompts" / f"{chunk['chunk_id']}_prompt.json"
    save_json(prompt_path, input_payload)
    chunk_started_at = utc_now()
    request_kwargs = {
        "model": request_session["model"],
        "reasoning": {"effort": request_session["reasoning_effort"]},
        "conversation": request_session["conversation_id"],
        "input": input_payload,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "math_audit",
                "strict": True,
                "schema": AUDIT_RESPONSE_SCHEMA,
            }
        },
        "background": request_session["background"],
        "store": request_session["store"],
    }
    used_code_interpreter_tool = False
    if verification_mode == "code_interpreter":
        request_kwargs["tools"] = _build_code_interpreter_tools(
            request_session,
            extra_file_ids=code_interpreter_file_ids,
            include_memory_limit=True,
        )
        used_code_interpreter_tool = True
    request_path = _save_request_metadata(
        request_session,
        chunk,
        request_kwargs,
        verification_mode=verification_mode,
        used_code_interpreter_tool=used_code_interpreter_tool,
        code_interpreter_file_ids=code_interpreter_file_ids,
        attempt_label="initial",
    )
    client = _get_client()
    try:
        resp = client.responses.create(**request_kwargs)
    except Exception as e:
        if used_code_interpreter_tool and "memory" in str(e).lower():
            request_kwargs["tools"] = _build_code_interpreter_tools(
                request_session,
                extra_file_ids=code_interpreter_file_ids,
                include_memory_limit=False,
            )
            request_path = _save_request_metadata(
                request_session,
                chunk,
                request_kwargs,
                verification_mode=verification_mode,
                used_code_interpreter_tool=used_code_interpreter_tool,
                code_interpreter_file_ids=code_interpreter_file_ids,
                attempt_label="memory_retry",
            )
            resp = client.responses.create(**request_kwargs)
        else:
            raise
    session["pending"] = {
        "chunk_id": chunk["chunk_id"],
        "response_id": resp.id,
        "created_at": chunk_started_at,
        "started_at": chunk_started_at,
        "verification_mode": verification_mode,
        "used_code_interpreter_tool": used_code_interpreter_tool,
        "request_path": request_path,
    }
    if fresh_rerun_conversation:
        session["pending"]["fresh_rerun_conversation"] = True
        session["pending"]["rerun_kind"] = chunk.get("_rerun_kind")
        session["pending"]["main_conversation_id"] = main_conversation_id
        session["pending"]["rerun_conversation_id"] = chunk.get("_fresh_rerun_conversation_id")
    if fresh_context_conversation:
        session["pending"]["fresh_context_conversation"] = True
        session["pending"]["main_conversation_id"] = main_conversation_id
        session["pending"]["fresh_context_conversation_id"] = chunk.get("_fresh_context_conversation_id")
        session["pending"]["pdf_attachment_disabled_note"] = FRESH_CONTEXT_TEXT_FIRST_NOTE
    if chunk.get("_pdf_text_only_retry"):
        session["pending"]["pdf_text_only_retry"] = True
        session["pending"]["pdf_attachment_disabled_note"] = PDF_TEXT_ONLY_RETRY_NOTE
    session["updated_at"] = utc_now()
    save_session(session)
    manifest = load_manifest(session)
    status = load_status(session)
    status.update({
        "status": "running",
        "current_chunk_id": chunk["chunk_id"],
        "chunks_total": len(manifest["chunks"]),
        "estimated_pages_total": manifest["pdf_page_count"],
        "current_chunk_elapsed_seconds": 0.0,
        "total_audit_seconds": float(load_usage(session)["totals"].get("audit_seconds", 0.0) or 0.0),
        "audit_started_at": session.get("audit_started_at", session.get("created_at")),
    })
    save_status(session, status)
    if getattr(resp, "status", None) in WORKING_STATUSES:
        resp = wait_for_response(resp.id, poll_every=poll_every, max_wait_seconds=max_wait_seconds)
    if getattr(resp, "status", None) != "completed":
        failure_summary = _pause_audit_after_chunk_failure(
            session,
            chunk,
            resp,
            verification_mode=verification_mode,
            used_code_interpreter_tool=used_code_interpreter_tool,
            request_path=request_path,
            note="Responses API returned a terminal non-completed status during process_one_chunk.",
        )
        if (
            allow_ci_failure_fallback
            and verification_mode == "code_interpreter"
            and used_code_interpreter_tool
            and _is_ci_invalid_prompt_failure(failure_summary)
        ):
            _record_ci_invalid_prompt_local_fallback(session, chunk, failure_summary, source="process_one_chunk")
            if display_output:
                print(
                    f"[{chunk['chunk_id']}] Code Interpreter chunk failed with error.code=invalid_prompt. "
                    "Retrying once with verification_mode='local_python_only'."
                )
            return process_one_chunk(
                session,
                chunk,
                poll_every=poll_every,
                max_wait_seconds=max_wait_seconds,
                display_output=display_output,
                verification_mode="local_python_only",
                code_interpreter_file_ids=code_interpreter_file_ids,
                allow_ci_failure_fallback=False,
            )
        fallback_mode = _normalize_ci_failure_fallback_mode(session.get("ci_failure_fallback_mode", "off"))
        if (
            allow_ci_failure_fallback
            and verification_mode == "code_interpreter"
            and used_code_interpreter_tool
            and fallback_mode == "retry_local_python_only_once"
        ):
            if display_output:
                print(
                    f"[{chunk['chunk_id']}] Code Interpreter chunk failed with status={getattr(resp, 'status', None)}. "
                    "Retrying once with verification_mode='local_python_only'."
                )
            return process_one_chunk(
                session,
                chunk,
                poll_every=poll_every,
                max_wait_seconds=max_wait_seconds,
                display_output=display_output,
                verification_mode="local_python_only",
                code_interpreter_file_ids=code_interpreter_file_ids,
                allow_ci_failure_fallback=False,
            )
        if failure_summary.get("retryable_reason") == "file_download_timeout":
            timeout_count = int(failure_summary.get("same_chunk_file_download_timeout_count") or 0)
            next_retry = (
                "Resume Audit will retry this chunk in a fresh text-only conversation with degraded PDF visual/reference context."
                if timeout_count >= 2
                else "Resume Audit will retry this chunk and reattach the PDF."
            )
            raise RuntimeError(
                f"Chunk {chunk['chunk_id']} hit a retryable API file-download timeout. "
                f"{next_retry} "
                f"See {failure_summary.get('failure_summary_path')} and logs/failed_chunks.jsonl"
            )
        raise RuntimeError(
            f"Chunk {chunk['chunk_id']} ended with status={getattr(resp, 'status', None)}. "
            f"See {failure_summary.get('failure_summary_path')} and logs/failed_chunks.jsonl"
        )
    return finalize_chunk(
        session,
        chunk,
        resp,
        display_output=display_output,
        verification_mode=verification_mode,
        used_code_interpreter_tool=used_code_interpreter_tool,
    )


def recover_pending_chunk(
    session: dict[str, Any],
    poll_every: float = 3.0,
    max_wait_seconds: Optional[float] = None,
    display_output: bool = True,
    allow_ci_failure_fallback: bool = True,
) -> Optional[dict[str, Any]]:
    pending = session.get("pending")
    if not pending:
        return None
    response_id = pending.get("response_id")
    chunk_id = pending.get("chunk_id")
    if not response_id or not chunk_id:
        return None
    if "started_at" not in pending:
        pending["started_at"] = pending.get("created_at", utc_now())
        session["pending"] = pending
        save_session(session)
    manifest = load_manifest(session)
    chunks = manifest.get("chunks", [])
    matches = [c for c in chunks if c["chunk_id"] == chunk_id]
    if not matches:
        return None
    chunk = matches[0]
    if pending.get("fresh_context_conversation"):
        chunk = dict(chunk)
        chunk["_fresh_context_conversation"] = True
        chunk["_fresh_context_conversation_id"] = pending.get("fresh_context_conversation_id")
        chunk["_main_conversation_id"] = pending.get("main_conversation_id")
        chunk["_suppress_pdf_attachment"] = True
        chunk["_pdf_attachment_disabled_note"] = pending.get("pdf_attachment_disabled_note") or FRESH_CONTEXT_TEXT_FIRST_NOTE
    if pending.get("pdf_text_only_retry"):
        chunk = dict(chunk)
        chunk["_pdf_text_only_retry"] = True
        chunk["_suppress_pdf_attachment"] = True
        chunk["_pdf_attachment_disabled_note"] = pending.get("pdf_attachment_disabled_note") or PDF_TEXT_ONLY_RETRY_NOTE
    structured_path = Path(session["workdir"]) / "responses" / f"{chunk_id}.structured.json"
    if structured_path.exists():
        session["pending"] = None
        if session.get("next_chunk_index", 1) <= chunk["chunk_index"]:
            session["next_chunk_index"] = chunk["chunk_index"] + 1
        session["updated_at"] = utc_now()
        save_session(session)
        return {"recovered": False, "skipped": True, "chunk_id": chunk_id}
    status = load_status(session)
    status["current_chunk_id"] = chunk_id
    status["current_chunk_elapsed_seconds"] = _seconds_since(pending.get("started_at"))
    save_status(session, status)
    client = _get_client()
    try:
        resp = client.responses.retrieve(response_id)
    except Exception as e:
        session["pending"] = None
        session["updated_at"] = utc_now()
        save_session(session)
        status = load_status(session)
        status["status"] = "paused"
        status["current_chunk_id"] = None
        status["current_chunk_elapsed_seconds"] = 0.0
        save_status(session, status)
        return {"recovered": False, "skipped": True, "chunk_id": chunk_id, "error": repr(e)}
    if getattr(resp, "status", None) in WORKING_STATUSES:
        resp = wait_for_response(response_id, poll_every=poll_every, max_wait_seconds=max_wait_seconds)
    if getattr(resp, "status", None) != "completed":
        verification_mode = _normalize_verification_mode((pending or {}).get("verification_mode", "local_python_only"))
        used_code_interpreter_tool = bool((pending or {}).get("used_code_interpreter_tool", False))
        failure_summary = _pause_audit_after_chunk_failure(
            session,
            chunk,
            resp,
            verification_mode=verification_mode,
            used_code_interpreter_tool=used_code_interpreter_tool,
            request_path=(pending or {}).get("request_path"),
            note="Responses API returned a terminal non-completed status during recovery of an earlier submitted request.",
            discovered_during_recovery=True,
        )
        if (
            allow_ci_failure_fallback
            and verification_mode == "code_interpreter"
            and used_code_interpreter_tool
            and _is_ci_invalid_prompt_failure(failure_summary)
        ):
            _record_ci_invalid_prompt_local_fallback(session, chunk, failure_summary, source="recover_pending_chunk")
            if display_output:
                print(
                    f"[{chunk_id}] Recovered Code Interpreter chunk failed with error.code=invalid_prompt. "
                    "Retrying once with verification_mode='local_python_only'."
                )
            return process_one_chunk(
                session,
                chunk,
                poll_every=poll_every,
                max_wait_seconds=max_wait_seconds,
                display_output=display_output,
                verification_mode="local_python_only",
                allow_ci_failure_fallback=False,
            )
        fallback_mode = _normalize_ci_failure_fallback_mode(session.get("ci_failure_fallback_mode", "off"))
        if (
            allow_ci_failure_fallback
            and verification_mode == "code_interpreter"
            and used_code_interpreter_tool
            and fallback_mode == "retry_local_python_only_once"
        ):
            if display_output:
                print(
                    f"[{chunk_id}] Recovered Code Interpreter chunk failed with status={getattr(resp, 'status', None)}. "
                    "Retrying once with verification_mode='local_python_only'."
                )
            return process_one_chunk(
                session,
                chunk,
                poll_every=poll_every,
                max_wait_seconds=max_wait_seconds,
                display_output=display_output,
                verification_mode="local_python_only",
                allow_ci_failure_fallback=False,
            )
        if display_output:
            print(
                f"[{chunk_id}] A previously submitted request ended with status={getattr(resp, 'status', None)} during recovery. "
                f"Saved failure details to {failure_summary.get('failure_summary_path')}."
            )
        return {
            "recovered": False,
            "paused": True,
            "chunk_id": chunk_id,
            "status": getattr(resp, "status", None),
            "failure_summary_path": failure_summary.get("failure_summary_path"),
            "failure_summary": failure_summary,
        }
    verification_mode = _normalize_verification_mode((pending or {}).get("verification_mode", "local_python_only"))
    used_code_interpreter_tool = bool((pending or {}).get("used_code_interpreter_tool", False))
    return finalize_chunk(
        session,
        chunk,
        resp,
        display_output=display_output,
        verification_mode=verification_mode,
        used_code_interpreter_tool=used_code_interpreter_tool,
    )


def archive_existing_audit_workdir(pdf_path: str | Path) -> Path | None:
    pdf_path = Path(pdf_path).expanduser().resolve()
    workdir = workdir_from_pdf(pdf_path)
    if not workdir.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archived = workdir.with_name(workdir.name + "_archived_" + timestamp)
    counter = 1
    while archived.exists():
        archived = workdir.with_name(workdir.name + f"_archived_{timestamp}_{counter}")
        counter += 1
    shutil.move(str(workdir), str(archived))
    print(f"Archived existing workdir to: {archived}")
    return archived


def create_new_session(
    pdf_path: str | Path,
    model: str = DEFAULT_MODEL,
    reasoning_effort: Optional[str] = None,
    tex_max_chars: int = 4500,
    pdf_max_chars: int = 3500,
    store: bool = True,
    background: bool = True,
    archive_existing_workdir: bool = False,
    use_code_interpreter: bool = False,
    code_interpreter_memory_limit: Optional[str] = "4g",
    reuse_code_interpreter_container: bool = False,
    ci_failure_fallback_mode: str = "off",
    reference_mention_style: str = "auto",
    report_reference_style: str = "match_audit",
    audit_context_mode: str = DEFAULT_AUDIT_CONTEXT_MODE,
    audit_system_prompt: Optional[str] = None,
    audit_system_prompt_source: Optional[str] = None,
) -> dict[str, Any]:
    model, reasoning_effort = normalize_model_and_reasoning_effort(model, reasoning_effort)
    audit_context_mode = _normalize_audit_context_mode(audit_context_mode)
    resolved_prompt, prompt_metadata = _resolve_audit_system_prompt(
        model,
        audit_system_prompt=audit_system_prompt,
        audit_system_prompt_source=audit_system_prompt_source,
    )
    pdf_path = Path(pdf_path).expanduser().resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)
    tex_path = pdf_path.with_suffix(".tex")
    workdir = workdir_from_pdf(pdf_path)
    if archive_existing_workdir and workdir.exists():
        archive_existing_audit_workdir(pdf_path)
    ensure_workdir_tree(workdir)
    init_state_files(workdir, model=model, reasoning_effort=reasoning_effort)
    client = _get_client()
    conversation = client.conversations.create()
    with pdf_path.open("rb") as f:
        uploaded = client.files.create(file=f, purpose="user_data")
    session = {
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "pdf_path": str(pdf_path),
        "tex_path": str(tex_path) if tex_path.exists() else None,
        "workdir": str(workdir),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "store": store,
        "background": background,
        "conversation_id": conversation.id,
        "pdf_file_id": uploaded.id,
        "pdf_attached_in_conversation": False,
        "developer_prompt_seeded": False,
        "audit_system_prompt": resolved_prompt,
        "audit_system_prompt_metadata": prompt_metadata,
        "next_chunk_index": 1,
        "last_response_id": None,
        "pending": None,
        "use_code_interpreter": bool(use_code_interpreter),
        "code_interpreter_memory_limit": str(code_interpreter_memory_limit) if code_interpreter_memory_limit else None,
        "reuse_code_interpreter_container": bool(reuse_code_interpreter_container),
        "code_interpreter_container_id": None,
        "ci_failure_fallback_mode": _normalize_ci_failure_fallback_mode(ci_failure_fallback_mode),
        "reference_mention_style": _normalize_reference_mention_style(reference_mention_style),
        "report_reference_style": _normalize_report_reference_style(report_reference_style),
        "audit_context_mode": audit_context_mode,
    }
    manifest = build_auto_chunks(
        pdf_path=pdf_path,
        tex_path=tex_path if tex_path.exists() else None,
        tex_max_chars=tex_max_chars,
        pdf_max_chars=pdf_max_chars,
    )
    save_session(session)
    save_manifest(session, manifest)
    status = load_status(session)
    status.update({
        "status": "initialized",
        "progress_pct": 0.0,
        "current_chunk_id": None,
        "chunks_completed": 0,
        "chunks_total": len(manifest["chunks"]),
        "estimated_pages_completed": 0,
        "estimated_pages_total": manifest.get("pdf_page_count", 0),
        "cost_usd": 0.0,
    })
    save_status(session, status)
    return _ensure_timing_state(session)


def _is_obviously_noncomputational_chunk(chunk: dict[str, Any], short_text_limit: int = 350) -> bool:
    if (chunk.get("source_kind") or "").strip().lower() != "tex-gap":
        return False
    text = (chunk.get("chunk_text") or "").strip()
    if not text or len(text) > int(short_text_limit):
        return False
    if re.search(r"\\(?:label|eqref|ref|cref|Cref|autoref)\{", text):
        return False
    if re.search(r"\\begin\{(?:align\*?|equation\*?|gather\*?|multline\*?|split)\}", text):
        return False
    if "\\[" in text or "$$" in text:
        return False
    if re.search(
        r"\\(?:begin|end)\{(?:theorem|lemma|proposition|corollary|claim|fact|definition|remark|example|conjecture|algorithm|thm|lem|prop|cor|defn|proof)\}",
        text,
        flags=re.IGNORECASE,
    ):
        return False
    return True


def _pause_audit_if_requested(session: dict[str, Any], verbose: bool = True) -> Optional[dict[str, Any]]:
    pause_requested_at = str(session.get("pause_requested_at") or "").strip()
    if not pause_requested_at:
        return None
    session["last_pause_requested_at"] = pause_requested_at
    session.pop("pause_requested_at", None)
    session["updated_at"] = utc_now()
    save_session(session)
    usage = load_usage(session)
    status = load_status(session)
    status["status"] = "paused"
    status["current_chunk_id"] = None
    status["current_chunk_elapsed_seconds"] = 0.0
    status["total_audit_seconds"] = float(usage["totals"].get("audit_seconds", 0.0) or 0.0)
    status["paused_at"] = utc_now()
    status["pause_reason"] = "requested"
    save_status(session, status)
    if verbose:
        print(
            f"Audit paused on request after completing the current chunk. "
            f"Resume from chunk index {session.get('next_chunk_index', 1)} when ready."
        )
    return {
        "paused": True,
        "reason": "requested",
        "pause_requested_at": pause_requested_at,
        "next_chunk_index": int(session.get("next_chunk_index", 1) or 1),
    }


def audit_the_paper(
    pdf_path: str | Path,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    tex_max_chars: int = 4500,
    pdf_max_chars: int = 3500,
    continue_existing: bool = True,
    archive_existing_workdir: bool = False,
    poll_every: float = 3.0,
    max_wait_seconds: Optional[float] = None,
    stop_after_chunks: Optional[int] = None,
    run_generated_verification: bool = False,
    verification_timeout_seconds: int = 120,
    include_verification_summary_in_final_report: bool = True,
    write_separate_verification_report: bool = True,
    use_code_interpreter: Optional[bool] = None,
    code_interpreter_memory_limit: Optional[str] = None,
    reuse_code_interpreter_container: Optional[bool] = None,
    ci_failure_fallback_mode: Optional[str] = None,
    reference_mention_style: Optional[str] = None,
    report_reference_style: Optional[str] = None,
    audit_context_mode: Optional[str] = None,
    verification_mode: str = "local_python_only",
    verification_chunk_indices: Optional[list[int]] = None,
    audit_system_prompt: Optional[str] = None,
    audit_system_prompt_source: Optional[str] = None,
    verbose: bool = True,
) -> dict[str, Any]:
    pdf_path = Path(pdf_path).expanduser().resolve()
    explicit_model_override = model is not None
    explicit_reasoning_override = reasoning_effort is not None
    verification_mode = _normalize_verification_mode(verification_mode)
    explicit_verification_chunk_selection = verification_chunk_indices is not None
    selected_chunk_indices = set()
    for raw_idx in verification_chunk_indices or []:
        try:
            selected_chunk_indices.add(int(raw_idx))
        except Exception:
            continue
    session = _resolve_session(pdf_path) if continue_existing else None
    if session is None:
        model, reasoning_effort = normalize_model_and_reasoning_effort(model, reasoning_effort)
        session = create_new_session(
            pdf_path,
            model=model,
            reasoning_effort=reasoning_effort,
            tex_max_chars=tex_max_chars,
            pdf_max_chars=pdf_max_chars,
            archive_existing_workdir=archive_existing_workdir,
            use_code_interpreter=bool(use_code_interpreter) if use_code_interpreter is not None else False,
            code_interpreter_memory_limit=code_interpreter_memory_limit if code_interpreter_memory_limit is not None else "4g",
            reuse_code_interpreter_container=bool(reuse_code_interpreter_container) if reuse_code_interpreter_container is not None else False,
            ci_failure_fallback_mode=_normalize_ci_failure_fallback_mode(ci_failure_fallback_mode) if ci_failure_fallback_mode is not None else "off",
            reference_mention_style=_normalize_reference_mention_style(reference_mention_style) if reference_mention_style is not None else "auto",
            report_reference_style=_normalize_report_reference_style(report_reference_style) if report_reference_style is not None else "match_audit",
            audit_context_mode=_normalize_audit_context_mode(audit_context_mode),
            audit_system_prompt=audit_system_prompt,
            audit_system_prompt_source=audit_system_prompt_source,
        )
        if verbose:
            print(f"Created workdir: {session['workdir']}")
            print(f"PDF: {session['pdf_path']}")
            print(f"TeX: {session['tex_path'] or 'not found'}")
    else:
        if explicit_model_override or explicit_reasoning_override:
            selected_model = model if explicit_model_override else session.get("model")
            selected_effort = reasoning_effort if explicit_reasoning_override else None
            selected_model, selected_effort = normalize_model_and_reasoning_effort(selected_model, selected_effort)
            session["model"] = selected_model
            session["reasoning_effort"] = selected_effort
        else:
            selected_model, selected_effort = normalize_model_and_reasoning_effort(
                session.get("model") or DEFAULT_MODEL,
                session.get("reasoning_effort"),
            )
            session["model"] = selected_model
            session["reasoning_effort"] = selected_effort
        if use_code_interpreter is not None:
            session["use_code_interpreter"] = bool(use_code_interpreter)
        if code_interpreter_memory_limit is not None:
            session["code_interpreter_memory_limit"] = str(code_interpreter_memory_limit) if code_interpreter_memory_limit else None
        if reuse_code_interpreter_container is not None:
            session["reuse_code_interpreter_container"] = bool(reuse_code_interpreter_container)
        if ci_failure_fallback_mode is not None:
            session["ci_failure_fallback_mode"] = _normalize_ci_failure_fallback_mode(ci_failure_fallback_mode)
        if reference_mention_style is not None:
            session["reference_mention_style"] = _normalize_reference_mention_style(reference_mention_style)
        if report_reference_style is not None:
            session["report_reference_style"] = _normalize_report_reference_style(report_reference_style)
        if audit_context_mode is not None:
            session["audit_context_mode"] = _normalize_audit_context_mode(audit_context_mode)
        else:
            session["audit_context_mode"] = _normalize_audit_context_mode(session.get("audit_context_mode"))
        if audit_system_prompt is not None:
            resolved_prompt, prompt_metadata = _resolve_audit_system_prompt(
                session.get("model") or DEFAULT_MODEL,
                audit_system_prompt=audit_system_prompt,
                audit_system_prompt_source=audit_system_prompt_source,
            )
            session["audit_system_prompt"] = resolved_prompt
            session["audit_system_prompt_metadata"] = prompt_metadata
        session["updated_at"] = utc_now()
        save_session(session)
        if verbose:
            print(f"Loaded existing session from {session['workdir']}")
            print(f"Using model: {session['model']}")
            print(f"Using reasoning effort: {session['reasoning_effort']}")
    session["verification_timeout_seconds"] = int(verification_timeout_seconds)
    session["include_verification_summary_in_final_report"] = bool(include_verification_summary_in_final_report)
    session["write_separate_verification_report"] = bool(write_separate_verification_report)
    session["audit_context_mode"] = _normalize_audit_context_mode(session.get("audit_context_mode"))
    session["updated_at"] = utc_now()
    save_session(session)
    manifest = load_manifest(session)
    chunks = manifest["chunks"]
    if not chunks:
        raise RuntimeError("Chunk manifest is empty.")
    file_download_auto_retry_counts: dict[str, int] = {}
    recovery_result = None
    if session.get("pending"):
        if verbose:
            print(f"Recovering pending chunk {session['pending'].get('chunk_id')} ...")
        recovery_result = recover_pending_chunk(
            session,
            poll_every=poll_every,
            max_wait_seconds=max_wait_seconds,
            display_output=True,
        )
        paused_status = load_status(session)
        if paused_status.get("status") == "paused":
            retry_after_recovery = False
            if isinstance(recovery_result, dict):
                failure_summary = recovery_result.get("failure_summary") or {}
                recovery_chunk_id = str(
                    recovery_result.get("chunk_id")
                    or failure_summary.get("chunk_id")
                    or ""
                )
                matches = [chunk for chunk in chunks if str(chunk.get("chunk_id") or "") == recovery_chunk_id]
                if matches and _retryable_response_failure_reason(failure_summary) == "file_download_timeout":
                    decision = _file_download_timeout_auto_retry_decision(
                        session,
                        matches[0],
                        file_download_auto_retry_counts.get(recovery_chunk_id, 0),
                    )
                    if decision.get("auto_retry"):
                        file_download_auto_retry_counts[recovery_chunk_id] = int(decision.get("attempt") or 0)
                        event = _record_file_download_timeout_auto_retry_event(
                            session,
                            matches[0],
                            decision,
                            action="scheduled",
                        )
                        if verbose:
                            print(event["message"])
                        delay = float(decision.get("delay_seconds") or 0.0)
                        if delay > 0:
                            time.sleep(delay)
                        session = _resolve_session(pdf_path)
                        pause_result = _pause_audit_if_requested(session, verbose=verbose)
                        if pause_result is not None:
                            return {
                                "session": session,
                                "status": load_status(session),
                                "usage": load_usage(session),
                                "report_paths": None,
                                "pause_result": pause_result,
                                "recovery_result": recovery_result,
                            }
                        retry_after_recovery = True
            if not retry_after_recovery:
                if verbose:
                    failure_path = None
                    if isinstance(recovery_result, dict):
                        failure_path = recovery_result.get("failure_summary_path")
                    if failure_path:
                        print(f"Audit paused after recovery detected a previously submitted request failed remotely. See: {failure_path}")
                    else:
                        print("Audit paused during recovery of a previously submitted request. Inspect logs/ and responses/ for details.")
                return {
                    "session": session,
                    "status": paused_status,
                    "usage": load_usage(session),
                    "report_paths": None,
                    "recovery_result": recovery_result,
                }
    session = _resolve_session(pdf_path)
    pause_result = _pause_audit_if_requested(session, verbose=verbose)
    if pause_result is not None:
        return {
            "session": session,
            "status": load_status(session),
            "usage": load_usage(session),
            "report_paths": None,
            "pause_result": pause_result,
        }
    start_idx = session["next_chunk_index"]
    processed = 0
    for idx in range(start_idx, len(chunks) + 1):
        chunk = chunks[idx - 1]
        chunk_verification_mode = verification_mode if (not selected_chunk_indices or idx in selected_chunk_indices) else "local_python_only"
        if (
            session.get("use_code_interpreter", False)
            and verification_mode == "code_interpreter"
            and not explicit_verification_chunk_selection
            and chunk_verification_mode == "code_interpreter"
            and _is_obviously_noncomputational_chunk(chunk)
        ):
            chunk_verification_mode = "local_python_only"
        while True:
            try:
                process_one_chunk(
                    session,
                    chunk,
                    poll_every=poll_every,
                    max_wait_seconds=max_wait_seconds,
                    display_output=True,
                    verification_mode=chunk_verification_mode,
                )
            except RuntimeError as exc:
                session = _resolve_session(pdf_path)
                chunk_id = str(chunk.get("chunk_id") or "")
                decision = _file_download_timeout_auto_retry_decision(
                    session,
                    chunk,
                    file_download_auto_retry_counts.get(chunk_id, 0),
                )
                if decision.get("auto_retry"):
                    file_download_auto_retry_counts[chunk_id] = int(decision.get("attempt") or 0)
                    event = _record_file_download_timeout_auto_retry_event(
                        session,
                        chunk,
                        decision,
                        action="scheduled",
                    )
                    if verbose:
                        print(event["message"])
                    delay = float(decision.get("delay_seconds") or 0.0)
                    if delay > 0:
                        time.sleep(delay)
                    session = _resolve_session(pdf_path)
                    pause_result = _pause_audit_if_requested(session, verbose=verbose)
                    if pause_result is not None:
                        return {
                            "session": session,
                            "status": load_status(session),
                            "usage": load_usage(session),
                            "report_paths": None,
                            "pause_result": pause_result,
                        }
                    continue
                if decision.get("reason") == "max_auto_retries_exhausted":
                    event = _record_file_download_timeout_auto_retry_event(
                        session,
                        chunk,
                        decision,
                        action="giving_up",
                    )
                    if verbose:
                        print(event["message"])
                    raise RuntimeError(event["message"]) from exc
                raise
            break
        processed += 1
        session = _resolve_session(pdf_path)
        pause_result = _pause_audit_if_requested(session, verbose=verbose)
        if pause_result is not None:
            return {
                "session": session,
                "status": load_status(session),
                "usage": load_usage(session),
                "report_paths": None,
                "pause_result": pause_result,
            }
        if stop_after_chunks is not None and processed >= stop_after_chunks:
            break
    if run_generated_verification:
        verification_run = run_verification_suite(
            pdf_path,
            timeout=int(verification_timeout_seconds),
            safe_only=True,
        )
        if verbose:
            summary = verification_run.get("summary", {})
            print(
                "Verification suite:"
                f"{summary.get('passed', 0)} passed, "
                f"{summary.get('failed', 0)} failed, "
                f"{summary.get('timeout', 0)} timed out, "
                f"{summary.get('skipped', 0)} skipped"
            )
    session = _resolve_session(pdf_path)
    paths = build_final_report(
        session,
        include_verification_summary_in_final_report=include_verification_summary_in_final_report,
        write_separate_verification_report=write_separate_verification_report,
    )
    if verbose:
        print("Final report:", paths.get("markdown"))
        print("JSON report:", paths.get("json"))
        print("TeX report:", paths.get("tex"))
    return {
        "session": session,
        "status": load_status(session),
        "usage": load_usage(session),
        "report_paths": paths,
    }


def start_fresh_audit(
    pdf_path: str | Path,
    model: str = DEFAULT_MODEL,
    reasoning_effort: Optional[str] = None,
    tex_max_chars: int = 4500,
    pdf_max_chars: int = 3500,
    poll_every: float = 3.0,
    max_wait_seconds: Optional[float] = None,
    stop_after_chunks: Optional[int] = None,
    run_generated_verification: bool = False,
    verification_timeout_seconds: int = 120,
    include_verification_summary_in_final_report: bool = True,
    write_separate_verification_report: bool = True,
    use_code_interpreter: bool = False,
    code_interpreter_memory_limit: Optional[str] = "4g",
    reuse_code_interpreter_container: bool = False,
    ci_failure_fallback_mode: str = "off",
    reference_mention_style: str = "auto",
    report_reference_style: str = "match_audit",
    audit_context_mode: str = DEFAULT_AUDIT_CONTEXT_MODE,
    verification_mode: str = "local_python_only",
    verification_chunk_indices: Optional[list[int]] = None,
    audit_system_prompt: Optional[str] = None,
    audit_system_prompt_source: Optional[str] = None,
    verbose: bool = True,
) -> dict[str, Any]:
    return audit_the_paper(
        pdf_path=pdf_path,
        model=model,
        reasoning_effort=reasoning_effort,
        audit_system_prompt=audit_system_prompt,
        audit_system_prompt_source=audit_system_prompt_source,
        tex_max_chars=tex_max_chars,
        pdf_max_chars=pdf_max_chars,
        continue_existing=False,
        archive_existing_workdir=True,
        poll_every=poll_every,
        max_wait_seconds=max_wait_seconds,
        stop_after_chunks=stop_after_chunks,
        run_generated_verification=run_generated_verification,
        verification_timeout_seconds=verification_timeout_seconds,
        include_verification_summary_in_final_report=include_verification_summary_in_final_report,
        write_separate_verification_report=write_separate_verification_report,
        use_code_interpreter=use_code_interpreter,
        code_interpreter_memory_limit=code_interpreter_memory_limit,
        reuse_code_interpreter_container=reuse_code_interpreter_container,
        ci_failure_fallback_mode=ci_failure_fallback_mode,
        reference_mention_style=reference_mention_style,
        report_reference_style=report_reference_style,
        audit_context_mode=audit_context_mode,
        verification_mode=verification_mode,
        verification_chunk_indices=verification_chunk_indices,
        verbose=verbose,
    )


def resume_audit(
    pdf_path: str | Path,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    tex_max_chars: int = 4500,
    pdf_max_chars: int = 3500,
    poll_every: float = 3.0,
    max_wait_seconds: Optional[float] = None,
    stop_after_chunks: Optional[int] = None,
    run_generated_verification: bool = False,
    verification_timeout_seconds: int = 120,
    include_verification_summary_in_final_report: bool = True,
    write_separate_verification_report: bool = True,
    use_code_interpreter: Optional[bool] = None,
    code_interpreter_memory_limit: Optional[str] = None,
    reuse_code_interpreter_container: Optional[bool] = None,
    ci_failure_fallback_mode: Optional[str] = None,
    reference_mention_style: Optional[str] = None,
    report_reference_style: Optional[str] = None,
    audit_context_mode: Optional[str] = None,
    verification_mode: str = "local_python_only",
    verification_chunk_indices: Optional[list[int]] = None,
    audit_system_prompt: Optional[str] = None,
    audit_system_prompt_source: Optional[str] = None,
    verbose: bool = True,
) -> dict[str, Any]:
    return audit_the_paper(
        pdf_path=pdf_path,
        model=model,
        reasoning_effort=reasoning_effort,
        audit_system_prompt=audit_system_prompt,
        audit_system_prompt_source=audit_system_prompt_source,
        tex_max_chars=tex_max_chars,
        pdf_max_chars=pdf_max_chars,
        continue_existing=True,
        archive_existing_workdir=False,
        poll_every=poll_every,
        max_wait_seconds=max_wait_seconds,
        stop_after_chunks=stop_after_chunks,
        run_generated_verification=run_generated_verification,
        verification_timeout_seconds=verification_timeout_seconds,
        include_verification_summary_in_final_report=include_verification_summary_in_final_report,
        write_separate_verification_report=write_separate_verification_report,
        use_code_interpreter=use_code_interpreter,
        code_interpreter_memory_limit=code_interpreter_memory_limit,
        reuse_code_interpreter_container=reuse_code_interpreter_container,
        ci_failure_fallback_mode=ci_failure_fallback_mode,
        reference_mention_style=reference_mention_style,
        report_reference_style=report_reference_style,
        audit_context_mode=audit_context_mode,
        verification_mode=verification_mode,
        verification_chunk_indices=verification_chunk_indices,
        verbose=verbose,
    )


__all__ = [
    "ask_about_audit",
    "ask_about_paper",
    "AUDIT_CONTEXT_MODES",
    "audit_the_paper",
    "build_fresh_audit_context_for_chunk",
    "build_concise_report",
    "build_final_report",
    "build_verification_report",
    "cancel_pending_response_for_retry",
    "clear_pause_request",
    "create_new_session",
    "default_reasoning_effort_for_model",
    "export_chatgpt_context_pack",
    "finalize_chunk",
    "get_failed_verification_chunks",
    "get_audit_status",
    "get_report_freshness",
    "get_verification_suite_status",
    "list_qa_threads",
    "load_qa_turns",
    "model_choices",
    "normalize_model_and_reasoning_effort",
    "process_one_chunk",
    "QA_CONTEXT_MODES",
    "DEFAULT_AUDIT_CONTEXT_MODE",
    "DEFAULT_QA_CONTEXT_MODE",
    "rebuild_qa_report",
    "recover_pending_chunk",
    "request_pause",
    "rerun_failed_verification_chunks",
    "rerun_selected_chunks",
    "resume_audit",
    "run_verification_suite_and_build_report",
    "set_display_audit_hook",
    "set_final_report_builder",
    "set_active_qa_thread",
    "set_live_audit_hooks",
    "set_openai_client",
    "set_prompt_builder",
    "start_new_qa_thread",
    "start_fresh_audit",
    "supported_reasoning_efforts_for_model",
    "wait_for_response",
]
