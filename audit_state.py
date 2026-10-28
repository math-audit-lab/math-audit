from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from audit_models import (
    GPT56_SOL_CACHE_WRITE_LIMITATION,
    CURRENT_MODEL_CAPABILITIES,
    PRICING_VERIFIED_DATE,
    long_context_threshold_for_model,
    LONG_CONTEXT_INPUT_TOKEN_THRESHOLD,
    LONG_CONTEXT_PRICING_USD_PER_1M,
    PRICING_USD_PER_1M,
    pricing_key_for_model,
)

LOW_CACHED_INPUT_RATIO_WARNING_THRESHOLD = 0.10
LOW_CACHED_INPUT_REUSE_WARNING = (
    "Low cached-input reuse for this chunk; cost may be higher, especially after resume/relaunch."
)


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
        "canonical_issue_clusters": root / "state" / "canonical_issue_clusters.json",
        "issue_cluster_events": root / "state" / "issue_cluster_events.jsonl",
        "usage": root / "state" / "usage.json",
        "status": root / "state" / "status.json",
        "manifest": root / "state" / "chunk_manifest.json",
        "chunk_records": root / "state" / "chunks.jsonl",
        "audit_context_db": root / "state" / "audit_context_db.jsonl",
        "verification_state": root / "state" / "verification.json",
        "verification_findings": root / "state" / "verification_findings.json",
        "verification_finding_rechecks": root / "state" / "verification_finding_rechecks.jsonl",
        "verification_technical_repairs": root / "state" / "verification_technical_repairs.jsonl",
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
    if not paths["audit_context_db"].exists():
        paths["audit_context_db"].touch()


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
    pricing_key = pricing_key_for_model(model)
    input_tokens = int(usage_obj.get("input_tokens", 0) or 0)
    if pricing_key in LONG_CONTEXT_PRICING_USD_PER_1M and input_tokens > long_context_threshold_for_model(model):
        return "long"
    return "short"


def pricing_for_model(model: str, usage_obj: Optional[dict[str, Any]] = None) -> dict[str, Optional[float]]:
    pricing_key = pricing_key_for_model(model)
    if pricing_context_for_model_usage(model, usage_obj) == "long":
        return LONG_CONTEXT_PRICING_USD_PER_1M[pricing_key]
    if pricing_key in PRICING_USD_PER_1M:
        return PRICING_USD_PER_1M[pricing_key]
    return {"input": 0.0, "cached_input": 0.0, "output": 0.0}


def compute_usage_cost(
    model: str,
    usage_obj: dict[str, Any],
    *,
    cache_ttl: Optional[str] = None,
    service_tier: str = "standard",
) -> dict[str, Any]:
    """Estimate a new response only; never reprice stored usage or historical totals.

    Output counts already include reasoning (and Pro's aggregated model work).
    Cache reads, writes and ordinary input are mutually exclusive billed buckets.
    Missing write counts/lifetimes are explicitly marked approximate.
    """
    pricing_key = pricing_key_for_model(model)
    pricing_context = pricing_context_for_model_usage(model, usage_obj)
    pricing = dict(pricing_for_model(model, usage_obj))
    pricing_available = pricing_key in PRICING_USD_PER_1M or (
        pricing_context == "long" and pricing_key in LONG_CONTEXT_PRICING_USD_PER_1M
    )
    current = pricing_key in CURRENT_MODEL_CAPABILITIES
    input_tokens = max(0, int(usage_obj.get("input_tokens", 0) or 0))
    output_tokens = max(0, int(usage_obj.get("output_tokens", 0) or 0))
    details = usage_obj.get("input_tokens_details") or {}
    cached_tokens = max(0, int(details.get("cached_tokens", 0) or 0))
    limitations: list[str] = []
    if not pricing_available:
        limitations.append("No verified price is available for this model; the numeric estimate excludes its charges.")
    if current and ("input_tokens" not in usage_obj or "output_tokens" not in usage_obj):
        limitations.append("Input or output usage is missing; unreported token charges are excluded.")
    if current and "cached_tokens" not in details:
        limitations.append("Cached-input usage is missing; unreported reads are estimated as ordinary input.")
    if cached_tokens > input_tokens:
        limitations.append("Cached-input tokens exceed input tokens; inconsistent usage was bounded for this estimate.")
        cached_tokens = input_tokens

    write_tokens = 0
    write_30m = 0
    write_12h = 0
    if current:
        if "cache_write_tokens" not in details:
            limitations.append("Cache-write usage is missing; unreported writes are estimated as ordinary input.")
        else:
            write_tokens = max(0, int(details.get("cache_write_tokens", 0) or 0))
            if write_tokens > input_tokens - cached_tokens:
                limitations.append("Cache-write counts overlap reads or exceed input; inconsistent usage was bounded.")
                write_tokens = input_tokens - cached_tokens
            lifetimes = details.get("cache_write_tokens_details") or {}
            if all(k in lifetimes for k in ("ttl_30m_tokens", "ttl_12h_tokens")):
                write_30m = max(0, int(lifetimes.get("ttl_30m_tokens", 0) or 0))
                write_12h = max(0, int(lifetimes.get("ttl_12h_tokens", 0) or 0))
                if write_30m + write_12h != write_tokens:
                    limitations.append("Cache-write lifetime counts do not sum to total writes; known counts were bounded and the remainder estimated at 30m.")
                    write_12h = min(write_12h, write_tokens)
                    write_30m = write_tokens - write_12h
            elif write_tokens:
                if cache_ttl == "12h":
                    write_12h = write_tokens
                else:
                    write_30m = write_tokens
                limitations.append("Cache-write lifetime breakdown is missing; writes use the requested lifetime, or an assumed 30m lifetime when unspecified.")
    elif pricing_key == "gpt-5.6-sol":
        limitations.append(GPT56_SOL_CACHE_WRITE_LIMITATION)

    # This app exposes Standard only. Accommodate explicit returned tiers without
    # pretending an unknown tier has a known price; do not alter historical rates.
    tier = str(service_tier or "standard")
    tier_multipliers = {"standard": 1.0, "default": 1.0, "fast": 2.0, "priority": 2.0, "flex": 0.5, "batch": 0.5}
    if current:
        multiplier = tier_multipliers.get(tier)
        if multiplier is None:
            limitations.append(f"Unpriced processing tier {tier!r}; estimate uses Standard rates.")
            multiplier = 1.0
        pricing = {k: (v * multiplier if v is not None else None) for k, v in pricing.items()}

    if pricing.get("cached_input") is None:
        billable_uncached = input_tokens
        billable_cached = 0
    else:
        billable_uncached = max(0, input_tokens - cached_tokens - write_tokens)
        billable_cached = cached_tokens
    input_cost = billable_uncached * (pricing.get("input") or 0.0) / 1_000_000
    cached_cost = billable_cached * (pricing.get("cached_input") or 0.0) / 1_000_000
    write_cost = (write_30m * (pricing.get("cache_write_30m") or 0.0)
                  + write_12h * (pricing.get("cache_write_12h") or 0.0)) / 1_000_000
    output_cost = output_tokens * (pricing.get("output") or 0.0) / 1_000_000
    return {
        "input_cost": input_cost,
        "cached_input_cost": cached_cost,
        "cache_write_cost": write_cost,
        "cache_write_tokens": write_tokens,
        "cache_write_30m_tokens": write_30m,
        "cache_write_12h_tokens": write_12h,
        "ordinary_input_tokens": billable_uncached,
        "output_cost": output_cost,
        "total_cost": input_cost + cached_cost + write_cost + output_cost,
        "pricing_context": pricing_context,
        "pricing_model": pricing_key,
        "pricing_available": pricing_available,
        "pricing_rates_usd_per_1m": pricing,
        "pricing_verified_date": PRICING_VERIFIED_DATE if current else None,
        "pricing_input_token_threshold": (
            long_context_threshold_for_model(model) if pricing_key in LONG_CONTEXT_PRICING_USD_PER_1M else None
        ),
        "processing_tier": tier,
        "approximate": bool(limitations),
        "pricing_limitations": limitations,
    }


def usage_cache_diagnostics(usage_obj: dict[str, Any]) -> dict[str, Any]:
    usage_obj = usage_obj or {}
    input_tokens = int(usage_obj.get("input_tokens", 0) or 0)
    cached_tokens = int(((usage_obj.get("input_tokens_details") or {}).get("cached_tokens", 0)) or 0)
    output_tokens = int(usage_obj.get("output_tokens", 0) or 0)
    reasoning_tokens = int(((usage_obj.get("output_tokens_details") or {}).get("reasoning_tokens", 0)) or 0)
    total_tokens = int(usage_obj.get("total_tokens", 0) or 0)
    write_tokens = int(((usage_obj.get("input_tokens_details") or {}).get("cache_write_tokens", 0)) or 0)
    uncached_tokens = max(0, input_tokens - cached_tokens - write_tokens)
    cached_ratio = (cached_tokens / input_tokens) if input_tokens else None
    low_cached_input_reuse = (
        input_tokens > LONG_CONTEXT_INPUT_TOKEN_THRESHOLD
        and cached_ratio is not None
        and cached_ratio < LOW_CACHED_INPUT_RATIO_WARNING_THRESHOLD
    )
    diagnostics = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "uncached_input_tokens": uncached_tokens,
        "cache_write_tokens": write_tokens,
        "cached_input_ratio": cached_ratio,
        "cached_input_percent": round(cached_ratio * 100.0, 1) if cached_ratio is not None else None,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "low_cached_input_reuse": low_cached_input_reuse,
    }
    if low_cached_input_reuse:
        diagnostics["warning"] = LOW_CACHED_INPUT_REUSE_WARNING
    return diagnostics


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


def _ensure_timing_state(
    session: dict[str, Any], default_model: str = "gpt-5.4", *, persist: bool = True,
) -> dict[str, Any]:
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

    if changed_session and persist:
        session["updated_at"] = utc_now()
        save_session(session)
    if changed_usage and persist:
        save_usage(session, usage)
    if changed_status and persist:
        save_status(session, status)
    return session


def update_usage_from_usage_obj(
    session: dict[str, Any],
    chunk_id: str,
    usage_obj: dict[str, Any],
    model: Optional[str] = None,
    elapsed_seconds: float = 0.0,
    activity_kind: str = "audit_chunk",
    activity_id: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    reasoning_mode: Optional[str] = None,
) -> dict[str, Any]:
    usage_state = load_usage(session)
    usage_obj = usage_obj or {}
    actual_model = model or session.get("model") or "unknown"
    cost = compute_usage_cost(
        actual_model, usage_obj,
        cache_ttl=(session.get("prompt_cache_options") or {}).get("ttl"),
        service_tier=session.get("service_tier", "standard"),
    )
    cache_diagnostics = usage_cache_diagnostics(usage_obj)

    totals = usage_state["totals"]
    totals["input_tokens"] += int(usage_obj.get("input_tokens", 0) or 0)
    totals["cached_tokens"] += int(((usage_obj.get("input_tokens_details") or {}).get("cached_tokens", 0)) or 0)
    totals["output_tokens"] += int(usage_obj.get("output_tokens", 0) or 0)
    totals["reasoning_tokens"] += int(((usage_obj.get("output_tokens_details") or {}).get("reasoning_tokens", 0)) or 0)
    totals["total_tokens"] += int(usage_obj.get("total_tokens", 0) or 0)
    totals["cost_usd"] += float(cost["total_cost"])
    totals["cache_write_tokens"] = int(totals.get("cache_write_tokens", 0)) + cost["cache_write_tokens"]
    totals["cost_approximate"] = bool(totals.get("cost_approximate", False) or cost["approximate"])
    totals["audit_seconds"] = float(totals.get("audit_seconds", 0.0) or 0.0) + float(elapsed_seconds or 0.0)

    clean_activity_kind = str(activity_kind or "audit_chunk").strip() or "audit_chunk"
    usage_entry = {
        "time": utc_now(),
        "chunk_id": chunk_id,
        "activity_kind": clean_activity_kind,
        "model": actual_model,
        "reasoning_effort": reasoning_effort if reasoning_effort is not None else session.get("reasoning_effort"),
        "reasoning_mode": reasoning_mode if reasoning_mode is not None else session.get("reasoning_mode", "standard"),
        "activity_id": str(activity_id or "").strip() or None,
        "usage": usage_obj,
        "cost": cost,
        "usage_diagnostics": cache_diagnostics,
        "elapsed_seconds": float(elapsed_seconds or 0.0),
    }
    usage_state["per_chunk"].append(usage_entry)

    activity_totals = usage_state.setdefault("activity_totals", {})
    activity_summary = activity_totals.setdefault(
        clean_activity_kind,
        {
            "calls": 0,
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "elapsed_seconds": 0.0,
        },
    )
    activity_summary["calls"] += 1
    activity_summary["input_tokens"] += int(usage_obj.get("input_tokens", 0) or 0)
    activity_summary["cached_tokens"] += int(
        ((usage_obj.get("input_tokens_details") or {}).get("cached_tokens", 0)) or 0
    )
    activity_summary["output_tokens"] += int(usage_obj.get("output_tokens", 0) or 0)
    activity_summary["reasoning_tokens"] += int(
        ((usage_obj.get("output_tokens_details") or {}).get("reasoning_tokens", 0)) or 0
    )
    activity_summary["total_tokens"] += int(usage_obj.get("total_tokens", 0) or 0)
    activity_summary["cost_usd"] += float(cost["total_cost"])
    activity_summary["cache_write_tokens"] = int(activity_summary.get("cache_write_tokens", 0)) + cost["cache_write_tokens"]
    activity_summary["cost_approximate"] = bool(activity_summary.get("cost_approximate", False) or cost["approximate"])
    activity_summary["elapsed_seconds"] += float(elapsed_seconds or 0.0)
    save_usage(session, usage_state)
    return {"usage": usage_obj, "cost": cost, "usage_diagnostics": cache_diagnostics, "usage_state": usage_state}


__all__ = [
    "LONG_CONTEXT_INPUT_TOKEN_THRESHOLD",
    "LONG_CONTEXT_PRICING_USD_PER_1M",
    "LOW_CACHED_INPUT_REUSE_WARNING",
    "LOW_CACHED_INPUT_RATIO_WARNING_THRESHOLD",
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
    "usage_cache_diagnostics",
    "workdir_from_pdf",
    "_ensure_timing_state",
]
