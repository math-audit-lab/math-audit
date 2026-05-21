#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_policy_hooks import build_user_message_for_chunk
from audit_runtime import (
    AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL,
    FRESH_CONTEXT_PRIOR_ISSUE_CAUTION,
    FRESH_CONTEXT_TEXT_FIRST_NOTE,
    _append_audit_context_db_entries,
    _audit_request_size_diagnostics,
    _context_entry_score,
    _context_query_terms,
    build_fresh_audit_context_for_chunk,
)
from audit_state import session_paths


STATE_FILES_TO_STAGE = (
    "session",
    "manifest",
    "issues",
    "ledger",
    "status",
    "usage",
    "chunk_records",
    "verification_state",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _path_stat(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _snapshot_paths(paths: list[Path]) -> dict[str, tuple[int, int] | None]:
    return {str(path): _path_stat(path) for path in paths}


def _copy_state_for_staging(source_workdir: Path, staging_workdir: Path) -> dict[str, Any]:
    source_paths = session_paths(source_workdir)
    staging_paths = session_paths(staging_workdir)
    staging_workdir.mkdir(parents=True, exist_ok=True)
    (staging_workdir / "state").mkdir(parents=True, exist_ok=True)

    staged: dict[str, Any] = {}
    for key in STATE_FILES_TO_STAGE:
        source_path = source_paths.get(key)
        staging_path = staging_paths.get(key)
        if source_path and staging_path and source_path.exists():
            staging_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, staging_path)
            staged[key] = str(staging_path)
    source_ref = source_workdir / "state" / "reference_map.json"
    if source_ref.exists():
        staging_ref = staging_workdir / "state" / "reference_map.json"
        shutil.copy2(source_ref, staging_ref)
        staged["reference_map"] = str(staging_ref)
    return staged


def _latest_by_chunk(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        chunk_id = str(record.get("chunk_id") or "")
        if not chunk_id:
            continue
        latest[chunk_id] = record
    return latest


def _issues_by_chunk(issues: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for issue in issues:
        chunk_id = str(issue.get("chunk_id") or issue.get("source_chunk_id") or "")
        if chunk_id:
            grouped[chunk_id].append(issue)
    return grouped


def _structured_response_path(record: dict[str, Any], source_workdir: Path) -> Path | None:
    candidates = [
        record.get("structured_response_path"),
        record.get("response_path"),
        record.get("audit_path"),
    ]
    chunk_id = str(record.get("chunk_id") or "")
    if chunk_id:
        candidates.extend(
            str(path)
            for path in sorted((source_workdir / "responses").glob(f"*{chunk_id}*structured*.json"))
        )
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate))
        if not path.is_absolute():
            path = source_workdir / path
        if path.exists():
            return path
    return None


def _request_text(message: list[dict[str, Any]]) -> str:
    texts: list[str] = []
    for item in message:
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "input_text":
                texts.append(str(part.get("text") or ""))
    return "\n".join(texts)


def _latest_request_path(source_workdir: Path, chunk_id: str) -> Path | None:
    request_dir = source_workdir / "requests"
    if not request_dir.exists():
        return None
    paths = sorted(
        request_dir.glob(f"*{chunk_id}*.request.json"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
    return paths[-1] if paths else None


def _summarize_baseline_request(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"available": False}
    payload = _load_json(path, default={})
    diagnostics = payload.get("request_size_diagnostics") if isinstance(payload, dict) else None
    return {
        "available": True,
        "path": str(path),
        "metadata": payload if isinstance(payload, dict) else {},
        "request_size_diagnostics": diagnostics if isinstance(diagnostics, dict) else {},
    }


def _chunk_index(chunk: dict[str, Any]) -> int | None:
    try:
        return int(chunk.get("chunk_index"))
    except Exception:
        pass
    chunk_id = str(chunk.get("chunk_id") or "")
    tail = chunk_id.rsplit("_", 1)[-1]
    try:
        return int(tail)
    except Exception:
        return None


def _short_text(text: Any, limit: int = 180) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)].rstrip() + "..."


def _priority_issue_details(
    chunk: dict[str, Any],
    entries: list[dict[str, Any]],
    recent_summary_limit: int = 4,
) -> list[dict[str, Any]]:
    current_index = _chunk_index(chunk)
    recent_window = max(int(recent_summary_limit), 4)
    query_terms = _context_query_terms(chunk)
    details: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("kind") != "issue":
            continue
        severity = str(entry.get("severity") or "").lower()
        status = str(entry.get("status") or "open").lower()
        if severity not in {"critical", "high"}:
            continue
        try:
            source_index = int(entry.get("source_chunk_index") or 0)
        except Exception:
            source_index = 0
        is_recent = bool(
            current_index is not None
            and source_index > 0
            and 0 < current_index - source_index <= recent_window
        )
        lexical_score = _context_entry_score(entry, query_terms) if query_terms else 0
        priority_rule_match = status != "resolved" and severity in {"critical", "high"}
        broad_priority_only = priority_rule_match and not is_recent and lexical_score <= 0
        reasons: list[str] = []
        if is_recent:
            reasons.append("recent")
        if lexical_score > 0:
            reasons.append(f"lexically relevant (score {lexical_score})")
        if broad_priority_only:
            reasons.append("broad priority rule")
        elif priority_rule_match:
            reasons.append("priority issue")
        details.append(
            {
                "issue_id": str(entry.get("issue_id") or ""),
                "severity": severity or "unknown",
                "status": status or "unknown",
                "source_chunk_id": str(entry.get("source_chunk_id") or ""),
                "source_chunk_index": source_index or None,
                "summary": _short_text(entry.get("text")),
                "recent": is_recent,
                "lexically_relevant": lexical_score > 0,
                "lexical_score": lexical_score,
                "priority_rule_match": priority_rule_match,
                "broad_priority_only": broad_priority_only,
                "reasons": reasons or ["included by retrieved context selection"],
            }
        )
    return details


def _issue_reason_counts(details: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter()
    for detail in details:
        if detail.get("recent"):
            counts["recent"] += 1
        if detail.get("lexically_relevant"):
            counts["lexically_relevant"] += 1
        if detail.get("priority_rule_match"):
            counts["priority_rule_match"] += 1
        if detail.get("broad_priority_only"):
            counts["broad_priority_only"] += 1
    return dict(counts)


def _context_summary(
    chunk: dict[str, Any],
    entries: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    baseline: dict[str, Any],
) -> str:
    chunk_id = str(chunk.get("chunk_id") or "chunk")
    kind_counts = Counter(str(entry.get("kind") or "unknown") for entry in entries)
    priority_issues = _priority_issue_details(chunk, entries)
    issue_reason_counts = _issue_reason_counts(priority_issues)
    recent_summaries = [
        str(entry.get("source_chunk_id") or "")
        for entry in entries
        if entry.get("kind") == "chunk_summary"
    ]
    baseline_diag = baseline.get("request_size_diagnostics") or {}
    lines = [
        f"# Context Mode Comparison: {chunk_id}",
        "",
        "## Fresh-Context Dry Run",
        f"- Retrieved entries: {diagnostics.get('retrieved_context_entry_count', 0)}",
        f"- Retrieved context chars: {diagnostics.get('retrieved_context_chars', 0)}",
        f"- User prompt chars: {diagnostics.get('user_prompt_length', 0)}",
        f"- Chunk text chars: {diagnostics.get('chunk_text_length', 0)}",
        f"- Macro glossary chars: {diagnostics.get('tex_macro_glossary_length', 0)}",
        f"- PDF attachment included: {diagnostics.get('pdf_attachment_included', False)}",
        f"- PDF attachment suppressed: {diagnostics.get('pdf_attachment_suppressed', False)}",
        "",
        "## Context Entry Kinds",
    ]
    if kind_counts:
        for kind, count in sorted(kind_counts.items()):
            lines.append(f"- {kind}: {count}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Included Priority Issues",
            f"- Note: {FRESH_CONTEXT_PRIOR_ISSUE_CAUTION}",
        ]
    )
    if priority_issues:
        lines.append("")
        lines.append("### Reason Counts")
        for key in ("recent", "lexically_relevant", "priority_rule_match", "broad_priority_only"):
            lines.append(f"- {key.replace('_', ' ').title()}: {issue_reason_counts.get(key, 0)}")
        lines.append("")
        lines.append("### Issue Details")
        for issue in priority_issues:
            lines.append(
                "- "
                f"{issue['issue_id'] or 'unknown issue'} | "
                f"{issue['severity']} | "
                f"status: {issue['status']} | "
                f"source: {issue['source_chunk_id'] or 'unknown'} | "
                f"reasons: {', '.join(issue['reasons'])} | "
                f"{issue['summary'] or 'no short description available'}"
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Included Recent Chunk Summaries",
            f"- {', '.join(item for item in recent_summaries if item) or 'none'}",
            "",
            "## Baseline Request",
            f"- Available: {baseline.get('available', False)}",
            f"- Path: {baseline.get('path', 'n/a')}",
            f"- Baseline user prompt chars: {baseline_diag.get('user_prompt_length', 'n/a')}",
            f"- Baseline chunk text chars: {baseline_diag.get('chunk_text_length', 'n/a')}",
            f"- Baseline running context chars: {baseline_diag.get('running_audit_context_length', 'n/a')}",
            f"- Baseline retrieved fresh-context chars: {baseline_diag.get('retrieved_fresh_context_length', 'n/a')}",
            f"- Baseline PDF attachment included: {baseline_diag.get('pdf_attachment_included', 'n/a')}",
        ]
    )
    return "\n".join(lines) + "\n"


def _prepare_staging_session(source_session: dict[str, Any], staging_workdir: Path) -> dict[str, Any]:
    session = dict(source_session)
    session["workdir"] = str(staging_workdir)
    session["audit_context_mode"] = AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL
    session["pdf_attached_in_conversation"] = False
    session["developer_prompt_seeded"] = False
    return session


def _backfill_context_db(
    source_workdir: Path,
    staging_session: dict[str, Any],
    chunks: list[dict[str, Any]],
    chunk_records: dict[str, dict[str, Any]],
    issues: list[dict[str, Any]],
) -> tuple[int, list[str], list[Path]]:
    warnings: list[str] = []
    source_paths: list[Path] = []
    by_chunk = _issues_by_chunk(issues)
    total_entries = 0
    for chunk in chunks:
        chunk_id = str(chunk.get("chunk_id") or "")
        record = chunk_records.get(chunk_id)
        if not record:
            continue
        structured_path = _structured_response_path(record, source_workdir)
        if structured_path is None:
            warnings.append(f"{chunk_id}: no saved structured response found")
            continue
        source_paths.append(structured_path)
        audit = _load_json(structured_path, default={})
        if not isinstance(audit, dict):
            warnings.append(f"{chunk_id}: structured response is not a JSON object")
            continue
        entries = _append_audit_context_db_entries(
            staging_session,
            chunk,
            audit,
            by_chunk.get(chunk_id, []),
        )
        total_entries += len(entries)
    return total_entries, warnings, source_paths


def prepare_context_mode_comparison(
    audit_workdir: Path,
    chunk_ids: list[str],
    output_dir: Path,
) -> dict[str, Any]:
    audit_workdir = audit_workdir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    source_paths = session_paths(audit_workdir)
    session = _load_json(source_paths["session"], default={})
    manifest = _load_json(source_paths["manifest"], default={})
    issues_payload = _load_json(source_paths["issues"], default=[])
    if not isinstance(session, dict):
        raise RuntimeError(f"Invalid session JSON: {source_paths['session']}")
    if not isinstance(manifest, dict):
        raise RuntimeError(f"Invalid chunk manifest JSON: {source_paths['manifest']}")
    if isinstance(issues_payload, dict) and isinstance(issues_payload.get("issues"), list):
        issues = issues_payload["issues"]
    elif isinstance(issues_payload, list):
        issues = issues_payload
    else:
        issues = []

    chunks = manifest.get("chunks") or []
    if not isinstance(chunks, list):
        raise RuntimeError(f"Manifest has no chunk list: {source_paths['manifest']}")
    chunks_by_id = {str(chunk.get("chunk_id") or ""): chunk for chunk in chunks if isinstance(chunk, dict)}
    missing_chunks = [chunk_id for chunk_id in chunk_ids if chunk_id not in chunks_by_id]
    if missing_chunks:
        raise RuntimeError(f"Selected chunks not found in manifest: {', '.join(missing_chunks)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    source_files_to_watch = [
        path
        for path in [
            source_paths.get("session"),
            source_paths.get("manifest"),
            source_paths.get("chunk_records"),
            source_paths.get("issues"),
            source_paths.get("ledger"),
            source_paths.get("audit_context_db"),
        ]
        if path
    ]

    with tempfile.TemporaryDirectory(prefix="math_audit_context_compare_") as tmp:
        staging_workdir = Path(tmp) / "staged_audit"
        staged_files = _copy_state_for_staging(audit_workdir, staging_workdir)
        staging_session = _prepare_staging_session(session, staging_workdir)
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

        baseline_paths = [_latest_request_path(audit_workdir, chunk_id) for chunk_id in chunk_ids]
        source_files_to_watch.extend(path for path in baseline_paths if path is not None)
        before = _snapshot_paths(source_files_to_watch)

        per_chunk: list[dict[str, Any]] = []
        for chunk_id in chunk_ids:
            chunk = dict(chunks_by_id[chunk_id])
            chunk["_fresh_context_conversation"] = True
            chunk["_fresh_context_conversation_id"] = "dry-run-fresh-context"
            chunk["_main_conversation_id"] = session.get("conversation_id")
            chunk["_suppress_pdf_attachment"] = True
            chunk["_pdf_attachment_disabled_note"] = FRESH_CONTEXT_TEXT_FIRST_NOTE

            retrieved = build_fresh_audit_context_for_chunk(staging_session, chunk)
            message = build_user_message_for_chunk(staging_session, chunk)
            prompt_text = _request_text(message)
            request_kwargs = {
                "conversation": "dry-run-fresh-context",
                "input": message,
            }
            diagnostics = _audit_request_size_diagnostics(staging_session, chunk, request_kwargs)
            baseline = _summarize_baseline_request(_latest_request_path(audit_workdir, chunk_id))
            entries = retrieved.get("entries") or []
            priority_issue_details = _priority_issue_details(chunk, entries)
            issue_reason_counts = _issue_reason_counts(priority_issue_details)

            chunk_dir = output_dir / chunk_id
            chunk_dir.mkdir(parents=True, exist_ok=True)
            _write_json(chunk_dir / "baseline_request_metadata.json", baseline)
            (chunk_dir / "fresh_context_prompt.txt").write_text(prompt_text, encoding="utf-8")
            _write_json(chunk_dir / "fresh_context_entries.json", entries)
            _write_json(
                chunk_dir / "fresh_context_request_metadata.json",
                {
                    "chunk_id": chunk_id,
                    "generated_at": _utc_now(),
                    "dry_run": True,
                    "would_call_api": False,
                    "audit_context_mode": AUDIT_CONTEXT_MODE_FRESH_EXPERIMENTAL,
                    "fresh_context_conversation": True,
                    "main_conversation_id": session.get("conversation_id"),
                    "dry_run_conversation": "dry-run-fresh-context",
                    "pdf_attachment_suppressed": True,
                    "pdf_attachment_note": FRESH_CONTEXT_TEXT_FIRST_NOTE,
                    "request_size_diagnostics": diagnostics,
                    "priority_issues": priority_issue_details,
                    "priority_issue_reason_counts": issue_reason_counts,
                },
            )
            (chunk_dir / "context_summary.md").write_text(
                _context_summary(chunk, entries, diagnostics, baseline),
                encoding="utf-8",
            )

            per_chunk.append(
                {
                    "chunk_id": chunk_id,
                    "entry_count": int(retrieved.get("entry_count") or 0),
                    "context_chars": int(retrieved.get("chars") or 0),
                    "context_kinds": dict(Counter(str(entry.get("kind") or "unknown") for entry in entries)),
                    "has_high_or_critical_issues": bool(priority_issue_details),
                    "priority_issues": priority_issue_details,
                    "priority_issue_reason_counts": issue_reason_counts,
                    "has_recent_chunk_summaries": any(entry.get("kind") == "chunk_summary" for entry in entries),
                    "chunk_text_chars": diagnostics.get("chunk_text_length", 0),
                    "macro_glossary_chars": diagnostics.get("tex_macro_glossary_length", 0),
                    "user_prompt_chars": diagnostics.get("user_prompt_length", 0),
                    "baseline_request_available": baseline.get("available", False),
                }
            )

        after = _snapshot_paths(source_files_to_watch)

    manifest_payload = {
        "generated_at": _utc_now(),
        "source_audit_workdir": str(audit_workdir),
        "output_dir": str(output_dir),
        "source_mutation_policy": "read-only; source audit folder is never written",
        "source_unmodified_by_script": before == after,
        "selected_chunks": chunk_ids,
        "total_backfilled_context_entries": total_entries,
        "source_context_db_exists": bool(source_paths.get("audit_context_db") and source_paths["audit_context_db"].exists()),
        "staged_state_keys": sorted(staged_files),
        "warnings": backfill_warnings,
        "chunks": per_chunk,
    }
    _write_json(output_dir / "comparison_manifest.json", manifest_payload)
    return manifest_payload


def _parse_chunks(raw: str) -> list[str]:
    chunks = [item.strip() for item in raw.split(",") if item.strip()]
    if not chunks:
        raise argparse.ArgumentTypeError("at least one chunk id is required")
    return chunks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare dry-run artifacts comparing continuous mode with experimental fresh-context mode."
    )
    parser.add_argument("--audit-workdir", required=True, type=Path, help="Existing audit workdir to inspect read-only.")
    parser.add_argument("--chunks", required=True, type=_parse_chunks, help="Comma-separated chunk ids to compare.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for generated comparison artifacts.")
    args = parser.parse_args(argv)

    try:
        manifest = prepare_context_mode_comparison(args.audit_workdir, args.chunks, args.output_dir)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("Context mode comparison prepared.")
    print(f"  Source audit: {manifest['source_audit_workdir']}")
    print(f"  Output dir: {manifest['output_dir']}")
    print(f"  Backfilled context entries: {manifest['total_backfilled_context_entries']}")
    print(f"  Source unmodified by script: {manifest['source_unmodified_by_script']}")
    for item in manifest.get("chunks", []):
        print(
            "  "
            f"{item['chunk_id']}: {item['entry_count']} entries, "
            f"{item['context_chars']} context chars, "
            f"{item['user_prompt_chars']} prompt chars"
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
