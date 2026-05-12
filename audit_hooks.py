from __future__ import annotations

from typing import Any, Callable, MutableMapping, Optional

from audit_runtime import (
    audit_the_paper,
    build_concise_report,
    create_new_session,
    finalize_chunk,
    get_failed_verification_chunks,
    get_report_freshness,
    process_one_chunk,
    recover_pending_chunk,
    rerun_failed_verification_chunks,
    rerun_selected_chunks,
    resume_audit,
    run_verification_suite_and_build_report,
    set_live_audit_hooks,
    set_openai_client,
    start_fresh_audit,
)

LIVE_AUDIT_RUNTIME_EXPORTS = {
    "create_new_session": create_new_session,
    "finalize_chunk": finalize_chunk,
    "get_failed_verification_chunks": get_failed_verification_chunks,
    "get_report_freshness": get_report_freshness,
    "process_one_chunk": process_one_chunk,
    "recover_pending_chunk": recover_pending_chunk,
    "rerun_failed_verification_chunks": rerun_failed_verification_chunks,
    "rerun_selected_chunks": rerun_selected_chunks,
    "build_concise_report": build_concise_report,
    "audit_the_paper": audit_the_paper,
    "start_fresh_audit": start_fresh_audit,
    "resume_audit": resume_audit,
    "run_verification_suite_and_build_report": run_verification_suite_and_build_report,
}


def noop_display_audit(audit: dict[str, Any]) -> None:
    del audit


def configure_runtime(
    *,
    client: Any = None,
    prompt_builder: Optional[Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]] = None,
    final_report_builder: Optional[Callable[..., dict[str, str]]] = None,
    display_audit: Optional[Callable[[dict[str, Any]], None]] = None,
    default_noop_display: bool = True,
) -> None:
    if client is not None:
        set_openai_client(client)

    hook_kwargs: dict[str, Any] = {}
    if prompt_builder is not None:
        hook_kwargs["prompt_builder"] = prompt_builder
    if final_report_builder is not None:
        hook_kwargs["final_report_builder"] = final_report_builder
    if display_audit is not None:
        hook_kwargs["display_audit"] = display_audit
    elif default_noop_display and (prompt_builder is not None or final_report_builder is not None):
        hook_kwargs["display_audit"] = noop_display_audit

    if hook_kwargs:
        set_live_audit_hooks(**hook_kwargs)


def install_runtime_frontend(
    namespace: MutableMapping[str, Any],
    *,
    client: Any = None,
    prompt_builder: Optional[Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]] = None,
    final_report_builder: Optional[Callable[..., dict[str, str]]] = None,
    display_audit: Optional[Callable[[dict[str, Any]], None]] = None,
    default_noop_display: bool = False,
) -> dict[str, Any]:
    configure_runtime(
        client=client,
        prompt_builder=prompt_builder,
        final_report_builder=final_report_builder,
        display_audit=display_audit,
        default_noop_display=default_noop_display,
    )
    namespace.update(LIVE_AUDIT_RUNTIME_EXPORTS)
    return dict(LIVE_AUDIT_RUNTIME_EXPORTS)


__all__ = [
    "LIVE_AUDIT_RUNTIME_EXPORTS",
    "configure_runtime",
    "get_report_freshness",
    "install_runtime_frontend",
    "noop_display_audit",
]
