from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# Standard-tier prices in USD per 1M tokens.
# Update these if OpenAI pricing changes.
PRICING_USD_PER_1M = {
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.5-pro": {"input": 30.00, "cached_input": None, "output": 180.00},
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-pro": {"input": 30.00, "cached_input": None, "output": 180.00},
    "gpt-5": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00},
    "gpt-5-nano": {"input": 0.05, "cached_input": 0.005, "output": 0.40},
}

LONG_CONTEXT_INPUT_TOKEN_THRESHOLD = 270_000

LONG_CONTEXT_PRICING_USD_PER_1M = {
    "gpt-5.5": {"input": 10.00, "cached_input": 1.00, "output": 45.00},
    "gpt-5.5-pro": {"input": 60.00, "cached_input": None, "output": 270.00},
    "gpt-5.4": {"input": 5.00, "cached_input": 0.50, "output": 22.50},
    "gpt-5.4-pro": {"input": 60.00, "cached_input": None, "output": 270.00},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_json(path: str | Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def append_jsonl(path: str | Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def workdir_from_pdf(pdf_path: str | Path) -> Path:
    pdf_path = Path(pdf_path).expanduser().resolve()
    return pdf_path.with_name(pdf_path.stem + "_audit")


def session_paths(workdir: str | Path) -> dict[str, Path]:
    root = Path(workdir)
    return {
        "session": root / "state" / "session.json",
        "ledger": root / "state" / "ledger.json",
        "issues": root / "state" / "issues.json",
        "usage": root / "state" / "usage.json",
        "status": root / "state" / "status.json",
        "manifest": root / "state" / "chunk_manifest.json",
        "chunk_records": root / "state" / "chunks.jsonl",
        "verification_state": root / "state" / "verification.json",
    }


def ensure_workdir_tree(workdir: str | Path) -> None:
    root = Path(workdir)
    for sub in [
        "state",
        "requests",
        "responses",
        "prompts",
        "latex_patches",
        "python_checks",
        "reports",
        "logs",
        "verification_results",
    ]:
        (root / sub).mkdir(parents=True, exist_ok=True)


def init_state_files(workdir: str | Path, model: str, reasoning_effort: str) -> None:
    del reasoning_effort
    ensure_workdir_tree(workdir)
    paths = session_paths(workdir)
    if not paths["ledger"].exists():
        save_json(paths["ledger"], {"assumptions": [], "notes": [], "updated_at": utc_now()})
    if not paths["issues"].exists():
        save_json(paths["issues"], {"next_issue_id": 1, "issues": [], "updated_at": utc_now()})
    if not paths["usage"].exists():
        save_json(
            paths["usage"],
            {
                "model": model,
                "pricing_basis": "standard",
                "totals": {
                    "input_tokens": 0,
                    "cached_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                },
                "per_chunk": [],
                "updated_at": utc_now(),
            },
        )
    if not paths["status"].exists():
        save_json(
            paths["status"],
            {
                "status": "initialized",
                "progress_pct": 0.0,
                "current_chunk_id": None,
                "chunks_completed": 0,
                "chunks_total": 0,
                "estimated_pages_completed": 0,
                "estimated_pages_total": 0,
                "cost_usd": 0.0,
                "updated_at": utc_now(),
            },
        )


def load_session_from_pdf(pdf_path: str | Path) -> Optional[dict[str, Any]]:
    workdir = workdir_from_pdf(pdf_path)
    session_path = session_paths(workdir)["session"]
    if session_path.exists():
        return load_json(session_path)
    return None


def save_session(session: dict[str, Any]) -> None:
    save_json(session_paths(session["workdir"])["session"], session)


def load_ledger(session: dict[str, Any]) -> dict[str, Any]:
    return load_json(session_paths(session["workdir"])["ledger"])


def save_ledger(session: dict[str, Any], ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = utc_now()
    save_json(session_paths(session["workdir"])["ledger"], ledger)


def load_issues(session: dict[str, Any]) -> dict[str, Any]:
    return load_json(session_paths(session["workdir"])["issues"])


def save_issues(session: dict[str, Any], issues: dict[str, Any]) -> None:
    issues["updated_at"] = utc_now()
    save_json(session_paths(session["workdir"])["issues"], issues)


def load_usage(session: dict[str, Any]) -> dict[str, Any]:
    return load_json(session_paths(session["workdir"])["usage"])


def save_usage(session: dict[str, Any], usage: dict[str, Any]) -> None:
    usage["updated_at"] = utc_now()
    save_json(session_paths(session["workdir"])["usage"], usage)


def load_status(session: dict[str, Any]) -> dict[str, Any]:
    return load_json(session_paths(session["workdir"])["status"])


def save_status(session: dict[str, Any], status: dict[str, Any]) -> None:
    status["updated_at"] = utc_now()
    save_json(session_paths(session["workdir"])["status"], status)


def load_manifest(session: dict[str, Any]) -> dict[str, Any]:
    return load_json(session_paths(session["workdir"])["manifest"])


def save_manifest(session: dict[str, Any], manifest: dict[str, Any]) -> None:
    save_json(session_paths(session["workdir"])["manifest"], manifest)


def pricing_context_for_model_usage(model: str, usage_obj: Optional[dict[str, Any]] = None) -> str:
    usage_obj = usage_obj or {}
    input_tokens = int(usage_obj.get("input_tokens", 0) or 0)
    if model in LONG_CONTEXT_PRICING_USD_PER_1M and input_tokens > LONG_CONTEXT_INPUT_TOKEN_THRESHOLD:
        return "long"
    return "short"


def pricing_for_model(model: str, usage_obj: Optional[dict[str, Any]] = None) -> dict[str, Optional[float]]:
    if pricing_context_for_model_usage(model, usage_obj) == "long":
        return LONG_CONTEXT_PRICING_USD_PER_1M[model]
    if model in PRICING_USD_PER_1M:
        return PRICING_USD_PER_1M[model]
    return {"input": 0.0, "cached_input": 0.0, "output": 0.0}


def compute_usage_cost(model: str, usage_obj: dict[str, Any]) -> dict[str, Any]:
    pricing_context = pricing_context_for_model_usage(model, usage_obj)
    pricing = pricing_for_model(model, usage_obj)
    input_tokens = int(usage_obj.get("input_tokens", 0) or 0)
    output_tokens = int(usage_obj.get("output_tokens", 0) or 0)
    cached_tokens = int(((usage_obj.get("input_tokens_details") or {}).get("cached_tokens", 0)) or 0)

    if pricing.get("cached_input") is None:
        billable_uncached = input_tokens
        billable_cached = 0
    else:
        billable_uncached = max(0, input_tokens - cached_tokens)
        billable_cached = cached_tokens
    input_cost = billable_uncached * (pricing.get("input") or 0.0) / 1_000_000
    cached_cost = billable_cached * (pricing.get("cached_input") or 0.0) / 1_000_000
    output_cost = output_tokens * (pricing.get("output") or 0.0) / 1_000_000

    return {
        "input_cost": input_cost,
        "cached_input_cost": cached_cost,
        "output_cost": output_cost,
        "total_cost": input_cost + cached_cost + output_cost,
        "pricing_context": pricing_context,
        "pricing_rates_usd_per_1m": dict(pricing),
        "pricing_input_token_threshold": (
            LONG_CONTEXT_INPUT_TOKEN_THRESHOLD if model in LONG_CONTEXT_PRICING_USD_PER_1M else None
        ),
    }


def format_duration(seconds: float) -> str:
    try:
        seconds = float(seconds)
    except Exception:
        seconds = 0.0
    if seconds < 0:
        seconds = 0.0
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{seconds:.1f}s" if seconds < 10 else f"{s}s"


def _ensure_timing_state(session: dict[str, Any], default_model: str = "gpt-5.4") -> dict[str, Any]:
    changed_session = False
    if "audit_started_at" not in session:
        session["audit_started_at"] = session.get("created_at", utc_now())
        changed_session = True
    if "audit_finished_at" not in session:
        session["audit_finished_at"] = None
        changed_session = True

    for key, value in {
        "use_code_interpreter": False,
        "code_interpreter_memory_limit": "4g",
        "reuse_code_interpreter_container": False,
        "code_interpreter_container_id": None,
        "verification_timeout_seconds": 120,
        "include_verification_summary_in_final_report": True,
        "write_separate_verification_report": True,
        "reference_mention_style": "auto",
        "report_reference_style": "match_audit",
        "ci_failure_fallback_mode": "off",
    }.items():
        if key not in session:
            session[key] = value
            changed_session = True

    usage = load_usage(session)
    changed_usage = False
    usage.setdefault("model", session.get("model", default_model))
    usage.setdefault("pricing_basis", "standard")
    totals = usage.setdefault("totals", {})
    for k, v in {
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "audit_seconds": 0.0,
    }.items():
        if k not in totals:
            totals[k] = v
            changed_usage = True

    per_chunk = usage.setdefault("per_chunk", [])
    for entry in per_chunk:
        if "elapsed_seconds" not in entry:
            entry["elapsed_seconds"] = 0.0
            changed_usage = True
    if "updated_at" not in usage:
        usage["updated_at"] = utc_now()
        changed_usage = True

    status = load_status(session)
    changed_status = False
    for k, v in {
        "status": "initialized",
        "progress_pct": 0.0,
        "current_chunk_id": None,
        "chunks_completed": 0,
        "chunks_total": 0,
        "estimated_pages_completed": 0,
        "estimated_pages_total": 0,
        "cost_usd": 0.0,
        "total_audit_seconds": float(totals.get("audit_seconds", 0.0) or 0.0),
        "current_chunk_elapsed_seconds": 0.0,
        "audit_started_at": session["audit_started_at"],
        "audit_finished_at": session.get("audit_finished_at"),
    }.items():
        if k not in status:
            status[k] = v
            changed_status = True

    if changed_session:
        session["updated_at"] = utc_now()
        save_session(session)
    if changed_usage:
        save_usage(session, usage)
    if changed_status:
        save_status(session, status)
    return session


def update_usage_from_usage_obj(
    session: dict[str, Any],
    chunk_id: str,
    usage_obj: dict[str, Any],
    model: Optional[str] = None,
    elapsed_seconds: float = 0.0,
) -> dict[str, Any]:
    usage_state = load_usage(session)
    usage_obj = usage_obj or {}
    cost = compute_usage_cost(model or session.get("model") or "gpt-5.4", usage_obj)

    totals = usage_state["totals"]
    totals["input_tokens"] += int(usage_obj.get("input_tokens", 0) or 0)
    totals["cached_tokens"] += int(((usage_obj.get("input_tokens_details") or {}).get("cached_tokens", 0)) or 0)
    totals["output_tokens"] += int(usage_obj.get("output_tokens", 0) or 0)
    totals["reasoning_tokens"] += int(((usage_obj.get("output_tokens_details") or {}).get("reasoning_tokens", 0)) or 0)
    totals["total_tokens"] += int(usage_obj.get("total_tokens", 0) or 0)
    totals["cost_usd"] += float(cost["total_cost"])
    totals["audit_seconds"] = float(totals.get("audit_seconds", 0.0) or 0.0) + float(elapsed_seconds or 0.0)

    usage_state["per_chunk"].append(
        {
            "time": utc_now(),
            "chunk_id": chunk_id,
            "usage": usage_obj,
            "cost": cost,
            "elapsed_seconds": float(elapsed_seconds or 0.0),
        }
    )
    save_usage(session, usage_state)
    return {"usage": usage_obj, "cost": cost, "usage_state": usage_state}


__all__ = [
    "LONG_CONTEXT_INPUT_TOKEN_THRESHOLD",
    "LONG_CONTEXT_PRICING_USD_PER_1M",
    "PRICING_USD_PER_1M",
    "append_jsonl",
    "compute_usage_cost",
    "ensure_workdir_tree",
    "format_duration",
    "init_state_files",
    "load_issues",
    "load_json",
    "load_ledger",
    "load_manifest",
    "load_session_from_pdf",
    "load_status",
    "load_usage",
    "pricing_for_model",
    "pricing_context_for_model_usage",
    "save_issues",
    "save_json",
    "save_ledger",
    "save_manifest",
    "save_session",
    "save_status",
    "save_usage",
    "session_paths",
    "update_usage_from_usage_obj",
    "workdir_from_pdf",
    "_ensure_timing_state",
]
