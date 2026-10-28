"""Verified Responses request policy (official documentation checked 2026-10-28).

Keep request validation separate from permissive historical readers. SDKs predating
prompt_cache_options send that documented wire parameter through extra_body.
"""
from __future__ import annotations

from typing import Any

from audit_models import validate_new_audit_settings, request_reasoning


def session_request_settings(
    session: dict[str, Any], model: str | None = None, reasoning_effort: str | None = None,
) -> tuple[str, str, str]:
    """Validate saved provenance first; an override cannot migrate a legacy session."""
    if not session.get("model") or not session.get("reasoning_effort"):
        raise ValueError("Saved model/reasoning settings are incomplete. Inspect this audit or start a fresh audit; it cannot be silently migrated.")
    saved = validate_new_audit_settings(session["model"], session["reasoning_effort"], session.get("reasoning_mode", "standard"))
    return validate_new_audit_settings(
        model if model is not None else saved[0],
        reasoning_effort if reasoning_effort is not None else saved[1],
        saved[2],
    )


def response_request_options(model: str, effort: str, mode: str = "standard") -> dict[str, Any]:
    model, effort, mode = validate_new_audit_settings(model, effort, mode)
    reasoning = request_reasoning(model, effort, mode)
    # Persisted reasoning and dynamic effort need a separate controlled evaluation.
    reasoning["context"] = "current_turn"
    return {
        "model": model,
        "reasoning": reasoning,
        "service_tier": "default",
        "extra_body": {"prompt_cache_options": {"ttl": "30m"}},
    }


def validate_response_storage(session: dict[str, Any]) -> None:
    if session.get("background", True) and not session.get("store", True):
        raise ValueError("Background Responses require store=True; choose foreground execution for store=False.")
