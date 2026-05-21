#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_policy_hooks import build_user_message_for_chunk
from audit_runtime import (
    AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL,
    AUDIT_RESPONSE_SCHEMA,
    AUDIT_SYSTEM_PROMPT,
    FRESH_CONTEXT_TEXT_FIRST_NOTE,
    _audit_request_size_diagnostics,
    _coerce_audit_payload,
    _get_client,
    build_fresh_audit_context_for_chunk,
    parse_audit_response,
    render_audit_markdown,
    to_jsonable,
    wait_for_response,
)
from audit_state import compute_usage_cost, session_paths, usage_cache_diagnostics, utc_now
from scripts.prepare_context_mode_comparison import (
    _backfill_context_db,
    _copy_state_for_staging,
    _context_summary,
    _issue_reason_counts,
    _latest_by_chunk,
    _latest_request_path,
    _load_json,
    _parse_chunks,
    _priority_issue_details,
    _read_jsonl,
    _request_text,
    _snapshot_paths,
    _summarize_baseline_request,
    _write_json,
)


SUPPORTED_MODE = AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _guard_output_dir(audit_workdir: Path, output_dir: Path) -> None:
    audit_workdir = audit_workdir.resolve()
    output_dir = output_dir.resolve()
    if output_dir == audit_workdir or _is_relative_to(output_dir, audit_workdir):
        raise RuntimeError(
            "Refusing to write A/B test artifacts inside the source audit workdir. "
            f"Choose an output directory outside {audit_workdir}."
        )


def _load_source_state(audit_workdir: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    paths = session_paths(audit_workdir)
    session = _load_json(paths["session"], default={})
    manifest = _load_json(paths["manifest"], default={})
    issues_payload = _load_json(paths["issues"], default=[])
    if not isinstance(session, dict):
        raise RuntimeError(f"Invalid session JSON: {paths['session']}")
    if not isinstance(manifest, dict):
        raise RuntimeError(f"Invalid chunk manifest JSON: {paths['manifest']}")
    if isinstance(issues_payload, dict) and isinstance(issues_payload.get("issues"), list):
        issues = issues_payload["issues"]
    elif isinstance(issues_payload, list):
        issues = issues_payload
    else:
        issues = []
    return session, manifest, issues


def _prepare_staging_session(
    source_session: dict[str, Any],
    staging_workdir: Path,
    model: str,
    reasoning_effort: str,
) -> dict[str, Any]:
    session = dict(source_session)
    session["workdir"] = str(staging_workdir)
    session["audit_context_mode"] = SUPPORTED_MODE
    session["model"] = model
    session["reasoning_effort"] = reasoning_effort
    session["pdf_attached_in_conversation"] = False
    session["developer_prompt_seeded"] = False
    session["pdf_file_id"] = None
    return session


def _required_baseline_paths(audit_workdir: Path, chunk_id: str) -> tuple[Path, Path]:
    structured_path = audit_workdir / "responses" / f"{chunk_id}.structured.json"
    if not structured_path.exists():
        raise RuntimeError(f"Missing baseline structured output for {chunk_id}: {structured_path}")
    request_path = _latest_request_path(audit_workdir, chunk_id)
    if request_path is None or not request_path.exists():
        raise RuntimeError(f"Missing baseline request metadata for {chunk_id} in {audit_workdir / 'requests'}")
    return structured_path, request_path


def _developer_input(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "developer",
        "content": [
            {
                "type": "input_text",
                "text": str(session.get("audit_system_prompt") or AUDIT_SYSTEM_PROMPT),
            }
        ],
    }


def _build_request(
    session: dict[str, Any],
    chunk: dict[str, Any],
    conversation_id: str,
) -> tuple[list[dict[str, Any]], str, dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    chunk["_fresh_context_conversation"] = True
    chunk["_fresh_context_conversation_id"] = conversation_id
    chunk["_main_conversation_id"] = session.get("conversation_id")
    chunk["_suppress_pdf_attachment"] = True
    chunk["_pdf_attachment_disabled_note"] = FRESH_CONTEXT_TEXT_FIRST_NOTE

    retrieved = build_fresh_audit_context_for_chunk(session, chunk)
    user_input = build_user_message_for_chunk(session, chunk)
    input_payload = [_developer_input(session)] + user_input
    prompt_text = _request_text(user_input)
    request_kwargs = {
        "model": session["model"],
        "reasoning": {"effort": session["reasoning_effort"]},
        "conversation": conversation_id,
        "input": input_payload,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "math_audit",
                "strict": True,
                "schema": AUDIT_RESPONSE_SCHEMA,
            }
        },
        "background": bool(session.get("background", True)),
        "store": bool(session.get("store", True)),
    }
    diagnostics = _audit_request_size_diagnostics(session, chunk, request_kwargs)
    return user_input, prompt_text, request_kwargs, list(retrieved.get("entries") or []), diagnostics


def _write_comparison_notes(path: Path, chunk_id: str, live: bool) -> None:
    lines = [
        f"# Fresh-Context A/B Notes: {chunk_id}",
        "",
        "## Review Checklist",
        "- Compare issue severity and specificity against the baseline continuous output.",
        "- Check whether prior audit issues were treated as provisional warnings.",
        "- Check whether lack of PDF attachment caused reference or visual-context overclaiming.",
        "- Check whether custom macros and notation were handled correctly.",
        "- Check whether generated Python checks are useful and non-duplicative.",
        "",
        "## Manual Notes",
        "- ",
    ]
    if not live:
        lines.insert(2, "Dry-run only: no model output was generated.")
        lines.insert(3, "")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _live_response(
    request_kwargs: dict[str, Any],
    poll_every: float,
    max_wait_seconds: float | None,
):
    client = _get_client()
    resp = client.responses.create(**request_kwargs)
    if getattr(resp, "status", None) not in {None, "completed"}:
        resp = wait_for_response(resp.id, poll_every=poll_every, max_wait_seconds=max_wait_seconds)
    return resp


def _response_text(resp: Any) -> str:
    text = getattr(resp, "output_text", None)
    if text:
        return str(text).strip()
    raw = to_jsonable(resp)
    parts: list[str] = []
    for item in raw.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(str(content.get("text")))
    return "\n".join(parts).strip()


def _save_live_outputs(
    chunk_dir: Path,
    chunk_id: str,
    model: str,
    resp: Any,
) -> dict[str, Any]:
    raw_json = to_jsonable(resp)
    _write_json(chunk_dir / "fresh_context_raw_response.json", raw_json)
    raw_text = _response_text(resp)
    if raw_text:
        (chunk_dir / "fresh_context_raw_response.txt").write_text(raw_text, encoding="utf-8")

    status = str(getattr(resp, "status", None) or raw_json.get("status") or "unknown")
    result: dict[str, Any] = {
        "status": "live_completed" if status == "completed" else "live_non_completed",
        "api_response_id": getattr(resp, "id", None) or raw_json.get("id"),
        "api_status": status,
    }
    if status != "completed":
        return result

    parsed = _coerce_audit_payload(parse_audit_response(resp))
    _write_json(chunk_dir / "fresh_context_structured.json", parsed)
    (chunk_dir / "fresh_context.md").write_text(render_audit_markdown(parsed), encoding="utf-8")

    usage_obj = raw_json.get("usage") if isinstance(raw_json, dict) else {}
    usage_obj = usage_obj if isinstance(usage_obj, dict) else {}
    cost = compute_usage_cost(model, usage_obj)
    usage_payload = {
        "chunk_id": chunk_id,
        "usage": usage_obj,
        "cost": cost,
        "usage_diagnostics": usage_cache_diagnostics(usage_obj),
    }
    _write_json(chunk_dir / "usage_cost.json", usage_payload)
    result["usage_cost"] = usage_payload
    result["issue_count"] = len(parsed.get("issues") or [])
    result["python_check_count"] = len(parsed.get("python_checks") or [])
    result["confidence"] = parsed.get("confidence")
    return result


def run_context_mode_ab_test(
    audit_workdir: Path,
    chunk_ids: list[str],
    output_dir: Path,
    mode: str = SUPPORTED_MODE,
    model: str | None = None,
    reasoning_effort: str | None = None,
    live: bool = False,
    poll_every: float = 3.0,
    max_wait_seconds: float | None = None,
) -> dict[str, Any]:
    audit_workdir = audit_workdir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if mode != SUPPORTED_MODE:
        raise RuntimeError(f"Unsupported mode for V1 A/B script: {mode!r}")
    _guard_output_dir(audit_workdir, output_dir)
    if live and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for --live mode.")

    source_paths = session_paths(audit_workdir)
    source_session, manifest, issues = _load_source_state(audit_workdir)
    selected_model = str(model or source_session.get("model") or "gpt-5.5")
    selected_effort = str(reasoning_effort or source_session.get("reasoning_effort") or "xhigh")
    chunks = manifest.get("chunks") or []
    if not isinstance(chunks, list):
        raise RuntimeError(f"Manifest has no chunk list: {source_paths['manifest']}")
    chunks_by_id = {str(chunk.get("chunk_id") or ""): chunk for chunk in chunks if isinstance(chunk, dict)}
    missing_chunks = [chunk_id for chunk_id in chunk_ids if chunk_id not in chunks_by_id]
    if missing_chunks:
        raise RuntimeError(f"Selected chunks not found in manifest: {', '.join(missing_chunks)}")

    baseline_pairs = {chunk_id: _required_baseline_paths(audit_workdir, chunk_id) for chunk_id in chunk_ids}
    source_files_to_watch = [
        path
        for path in [
            source_paths.get("session"),
            source_paths.get("manifest"),
            source_paths.get("chunk_records"),
            source_paths.get("issues"),
            source_paths.get("ledger"),
            source_paths.get("verification_state"),
            source_paths.get("audit_context_db"),
        ]
        if path
    ]
    for structured_path, request_path in baseline_pairs.values():
        source_files_to_watch.extend([structured_path, request_path])

    output_dir.mkdir(parents=True, exist_ok=True)
    per_chunk: list[dict[str, Any]] = []
    backfill_warnings: list[str] = []
    total_entries = 0

    with tempfile.TemporaryDirectory(prefix="math_audit_ab_context_") as tmp:
        staging_workdir = Path(tmp) / "staged_audit"
        _copy_state_for_staging(audit_workdir, staging_workdir)
        staging_session = _prepare_staging_session(source_session, staging_workdir, selected_model, selected_effort)
        _write_json(session_paths(staging_workdir)["session"], staging_session)

        chunk_records = _latest_by_chunk(_read_jsonl(source_paths["chunk_records"]))
        total_entries, backfill_warnings, structured_paths = _backfill_context_db(
            audit_workdir,
            staging_session,
            chunks,
            chunk_records,
            issues,
        )
        source_files_to_watch.extend(structured_paths)
        before = _snapshot_paths(source_files_to_watch)

        for chunk_id in chunk_ids:
            chunk = dict(chunks_by_id[chunk_id])
            conversation_id = f"dry-run-fresh-context-{chunk_id}"
            live_conversation_id = None
            if live:
                conversation = _get_client().conversations.create()
                conversation_id = conversation.id
                live_conversation_id = conversation.id

            _user_input, prompt_text, request_kwargs, entries, diagnostics = _build_request(
                staging_session,
                chunk,
                conversation_id,
            )
            priority_issues = _priority_issue_details(chunk, entries)
            reason_counts = _issue_reason_counts(priority_issues)
            structured_path, request_path = baseline_pairs[chunk_id]
            baseline = _summarize_baseline_request(request_path)

            chunk_dir = output_dir / chunk_id
            chunk_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(structured_path, chunk_dir / "baseline_continuous_structured.json")
            shutil.copy2(request_path, chunk_dir / "baseline_continuous_request_metadata.json")
            (chunk_dir / "fresh_context_prompt.txt").write_text(prompt_text, encoding="utf-8")
            _write_json(chunk_dir / "fresh_context_entries.json", entries)
            _write_json(
                chunk_dir / "fresh_context_request_metadata.json",
                {
                    "chunk_id": chunk_id,
                    "generated_at": utc_now(),
                    "dry_run": not live,
                    "would_call_api": bool(live),
                    "audit_context_mode": SUPPORTED_MODE,
                    "model": selected_model,
                    "reasoning_effort": selected_effort,
                    "fresh_context_conversation": True,
                    "main_conversation_id": source_session.get("conversation_id"),
                    "conversation_id": conversation_id,
                    "live_conversation_id": live_conversation_id,
                    "pdf_attachment_suppressed": True,
                    "pdf_attachment_note": FRESH_CONTEXT_TEXT_FIRST_NOTE,
                    "request_size_diagnostics": diagnostics,
                    "priority_issues": priority_issues,
                    "priority_issue_reason_counts": reason_counts,
                    "request": to_jsonable(request_kwargs),
                },
            )
            (chunk_dir / "context_summary.md").write_text(
                _context_summary(chunk, entries, diagnostics, baseline),
                encoding="utf-8",
            )
            _write_comparison_notes(chunk_dir / "comparison_notes.md", chunk_id, live=live)

            chunk_result = {
                "chunk_id": chunk_id,
                "status": "dry_run_prepared",
                "prompt_chars": diagnostics.get("user_prompt_length", 0),
                "retrieved_context_chars": diagnostics.get("retrieved_context_chars", 0),
                "retrieved_context_entry_count": diagnostics.get("retrieved_context_entry_count", 0),
                "pdf_attachment_suppressed": True,
                "baseline_structured_path": str(structured_path),
                "baseline_request_path": str(request_path),
                "priority_issue_reason_counts": reason_counts,
            }
            if live:
                try:
                    resp = _live_response(request_kwargs, poll_every=poll_every, max_wait_seconds=max_wait_seconds)
                    chunk_result.update(_save_live_outputs(chunk_dir, chunk_id, selected_model, resp))
                except Exception as exc:
                    chunk_result.update({"status": "live_failed", "error": f"{type(exc).__name__}: {exc}"})
                    _write_json(
                        chunk_dir / "fresh_context_live_failure.json",
                        {
                            "time": utc_now(),
                            "chunk_id": chunk_id,
                            "error": chunk_result["error"],
                        },
                    )
            per_chunk.append(chunk_result)

    after = _snapshot_paths(source_files_to_watch)
    manifest_payload = {
        "generated_at": utc_now(),
        "source_audit_workdir": str(audit_workdir),
        "selected_chunks": chunk_ids,
        "mode": mode,
        "dry_run": not live,
        "live": bool(live),
        "model": selected_model,
        "reasoning_effort": selected_effort,
        "output_dir": str(output_dir),
        "source_mutation_guard": {
            "source_unmodified_by_script": before == after,
            "output_dir_inside_source_audit": _is_relative_to(output_dir, audit_workdir),
        },
        "total_backfilled_context_entries": total_entries,
        "warnings": backfill_warnings,
        "chunks": per_chunk,
    }
    _write_json(output_dir / "ab_test_manifest.json", manifest_payload)
    return manifest_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Developer-only dry-run/live A/B runner for experimental fresh-context chunk audits."
    )
    parser.add_argument("--audit-workdir", required=True, type=Path)
    parser.add_argument("--chunks", required=True, type=_parse_chunks)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mode", default=SUPPORTED_MODE)
    parser.add_argument("--model", default=None)
    parser.add_argument("--reasoning-effort", default=None)
    parser.add_argument("--live", action="store_true", help="Actually call the OpenAI API. Omit for dry-run.")
    parser.add_argument("--poll-every", type=float, default=3.0)
    parser.add_argument("--max-wait-seconds", type=float, default=None)
    args = parser.parse_args(argv)

    try:
        manifest = run_context_mode_ab_test(
            audit_workdir=args.audit_workdir,
            chunk_ids=args.chunks,
            output_dir=args.output_dir,
            mode=args.mode,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            live=args.live,
            poll_every=args.poll_every,
            max_wait_seconds=args.max_wait_seconds,
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    mode_label = "LIVE" if manifest["live"] else "DRY-RUN"
    print(f"Fresh-context A/B comparison prepared ({mode_label}).")
    print(f"  Source audit: {manifest['source_audit_workdir']}")
    print(f"  Output dir: {manifest['output_dir']}")
    print(f"  Source unmodified by script: {manifest['source_mutation_guard']['source_unmodified_by_script']}")
    print(f"  Backfilled context entries: {manifest['total_backfilled_context_entries']}")
    for item in manifest.get("chunks", []):
        response_id = item.get("api_response_id")
        suffix = f", response {response_id}" if response_id else ""
        print(
            "  "
            f"{item['chunk_id']}: {item['status']}, "
            f"{item.get('retrieved_context_entry_count', 0)} entries, "
            f"{item.get('retrieved_context_chars', 0)} context chars, "
            f"{item.get('prompt_chars', 0)} prompt chars"
            f"{suffix}"
        )
    warnings = manifest.get("warnings") or []
    if warnings:
        print("Warnings:")
        for warning in warnings[:20]:
            print(f"  - {warning}")
        if len(warnings) > 20:
            print(f"  - ... {len(warnings) - 20} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
