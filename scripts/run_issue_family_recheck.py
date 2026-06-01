#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_runtime import _get_client, to_jsonable, wait_for_response  # noqa: E402
from audit_state import compute_usage_cost, session_paths, usage_cache_diagnostics  # noqa: E402


SCHEMA_VERSION = "1.0"
RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "family_id",
        "verdict",
        "upstream_issue_ids",
        "downstream_issue_ids",
        "false_positive_issue_ids",
        "recommended_severity_by_issue",
        "recommended_status_by_issue",
        "grouping_recommendations",
        "final_report_treatment",
        "evidence_for",
        "evidence_against",
        "confidence",
        "needs_human_review",
        "summary",
    ],
    "properties": {
        "family_id": {"type": "string"},
        "verdict": {"type": "string"},
        "upstream_issue_ids": {"type": "array", "items": {"type": "string"}},
        "downstream_issue_ids": {"type": "array", "items": {"type": "string"}},
        "false_positive_issue_ids": {"type": "array", "items": {"type": "string"}},
        "recommended_severity_by_issue": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["issue_id", "severity", "rationale"],
                "properties": {
                    "issue_id": {"type": "string"},
                    "severity": {"type": "string"},
                    "rationale": {"type": "string"},
                },
            },
        },
        "recommended_status_by_issue": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["issue_id", "status", "rationale"],
                "properties": {
                    "issue_id": {"type": "string"},
                    "status": {"type": "string"},
                    "rationale": {"type": "string"},
                },
            },
        },
        "grouping_recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["upstream_issue_id", "downstream_issue_ids", "rationale"],
                "properties": {
                    "upstream_issue_id": {"type": "string"},
                    "downstream_issue_ids": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                },
            },
        },
        "final_report_treatment": {"type": "string"},
        "evidence_for": {"type": "array", "items": {"type": "string"}},
        "evidence_against": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string"},
        "needs_human_review": {"type": "boolean"},
        "summary": {"type": "string"},
    },
}


def validate_result_schema(schema: dict[str, Any]) -> None:
    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            properties = node.get("properties")
            if not isinstance(properties, dict):
                raise ValueError(f"{path}: object schema must define properties")
            required = node.get("required")
            if not isinstance(required, list):
                raise ValueError(f"{path}: object schema must define required")
            property_keys = set(properties)
            required_keys = set(str(item) for item in required)
            missing_required = sorted(property_keys - required_keys)
            extra_required = sorted(required_keys - property_keys)
            if missing_required or extra_required:
                raise ValueError(
                    f"{path}: required/properties mismatch; missing_required={missing_required}, extra_required={extra_required}"
                )
            if node.get("additionalProperties") is not False:
                raise ValueError(f"{path}: object schema must set additionalProperties=false")
            for key, child in properties.items():
                walk(child, f"{path}.properties.{key}")
        items = node.get("items")
        if items is not None:
            walk(items, f"{path}.items")

    walk(schema, "RESULT_SCHEMA")


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
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _path_stat(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _snapshot_paths(paths: list[Path]) -> dict[str, tuple[int, int] | None]:
    return {str(path): _path_stat(path) for path in paths}


def _short_text(text: Any, limit: int = 900) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)].rstrip() + "..."


def _guard_output_dir(audit_workdir: Path, output_dir: Path) -> None:
    audit_workdir = audit_workdir.resolve()
    output_dir = output_dir.resolve()
    if output_dir == audit_workdir or audit_workdir in output_dir.parents:
        raise RuntimeError("Output directory must not be inside the source audit workdir.")


def _load_session(workdir: Path) -> dict[str, Any]:
    payload = _load_json(session_paths(workdir)["session"], default={})
    return payload if isinstance(payload, dict) else {}


def _load_issues(workdir: Path) -> dict[str, dict[str, Any]]:
    payload = _load_json(session_paths(workdir)["issues"], default={})
    issues = payload.get("issues") if isinstance(payload, dict) else payload
    if not isinstance(issues, list):
        return {}
    return {str(item.get("issue_id")): item for item in issues if isinstance(item, dict) and item.get("issue_id")}


def _load_chunks(workdir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    manifest = _load_json(session_paths(workdir)["manifest"], default={})
    chunks_by_id: dict[str, dict[str, Any]] = {}
    if isinstance(manifest, dict):
        for chunk in manifest.get("chunks") or []:
            if isinstance(chunk, dict) and chunk.get("chunk_id"):
                chunks_by_id[str(chunk["chunk_id"])] = dict(chunk)
    latest_records: dict[str, dict[str, Any]] = {}
    for record in _read_jsonl(session_paths(workdir)["chunk_records"]):
        chunk_id = str(record.get("chunk_id") or "")
        if chunk_id:
            latest_records[chunk_id] = record
    return chunks_by_id, latest_records


def _select_family(families_payload: dict[str, Any], family_id: str) -> dict[str, Any]:
    for family in families_payload.get("families") or []:
        if isinstance(family, dict) and str(family.get("family_id")) == family_id:
            return family
    raise RuntimeError(f"Family id not found: {family_id}")


def _issue_payload(issue_id: str, issues_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    issue = dict(issues_by_id.get(issue_id) or {"issue_id": issue_id})
    return {
        "issue_id": issue_id,
        "severity": issue.get("severity"),
        "status": issue.get("status"),
        "chunk_id": issue.get("chunk_id"),
        "title": issue.get("title"),
        "location": issue.get("location"),
        "description": issue.get("description"),
        "evidence": issue.get("evidence"),
        "proposed_fix": issue.get("proposed_fix"),
        "tags": issue.get("tags") or [],
    }


def _chunk_payload(chunk_id: str, chunks_by_id: dict[str, dict[str, Any]], records_by_chunk: dict[str, dict[str, Any]]) -> dict[str, Any]:
    chunk = dict(chunks_by_id.get(chunk_id) or {"chunk_id": chunk_id})
    record = records_by_chunk.get(chunk_id) or {}
    return {
        "chunk_id": chunk_id,
        "chunk_index": chunk.get("chunk_index") or record.get("chunk_index"),
        "label": chunk.get("display_label") or chunk.get("label") or "",
        "boundary": chunk.get("boundary") or "",
        "page_start": chunk.get("page_start"),
        "page_end": chunk.get("page_end"),
        "record": {
            "time": record.get("time"),
            "response_id": record.get("response_id"),
            "structured_response_path": record.get("structured_response_path"),
            "status": record.get("status"),
        },
    }


def _structured_path(workdir: Path, chunk_id: str, records_by_chunk: dict[str, dict[str, Any]]) -> Path | None:
    record = records_by_chunk.get(chunk_id) or {}
    candidates = [record.get("structured_response_path"), workdir / "responses" / f"{chunk_id}.structured.json"]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate))
        if not path.is_absolute():
            path = workdir / path
        if path.exists():
            return path
    return None


def _structured_summary(payload: dict[str, Any], limit: int = 1800) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("assumptions_and_notation", "verified_steps"):
        value = payload.get(key)
        if isinstance(value, list):
            out[key] = [_short_text(item, 360) for item in value[:8]]
    ledger = payload.get("ledger_updates")
    if isinstance(ledger, dict):
        out["ledger_updates"] = {
            key: [_short_text(item, 360) for item in value[:8]]
            for key, value in ledger.items()
            if key in {"assumptions", "notes"} and isinstance(value, list)
        }
    if payload.get("next_boundary_hint"):
        out["next_boundary_hint"] = _short_text(payload.get("next_boundary_hint"), 360)
    if payload.get("issues"):
        out["issue_count"] = len(payload.get("issues") or [])
    encoded = json.dumps(out, ensure_ascii=False)
    if len(encoded) <= limit:
        return out
    return {"summary_truncated": _short_text(encoded, limit)}


def _load_structured_outputs(workdir: Path, chunk_ids: list[str], records_by_chunk: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[Path]]:
    outputs = []
    paths = []
    for chunk_id in chunk_ids:
        path = _structured_path(workdir, chunk_id, records_by_chunk)
        if path is None:
            continue
        payload = _load_json(path, default={})
        if not isinstance(payload, dict):
            continue
        paths.append(path)
        outputs.append({"chunk_id": chunk_id, "path": str(path), "summary": _structured_summary(payload)})
    return outputs, paths


def _nearby_chunk_summaries(
    workdir: Path,
    chunk_ids: list[str],
    chunks_by_id: dict[str, dict[str, Any]],
    records_by_chunk: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[Path]]:
    target_indexes = set()
    for chunk_id in chunk_ids:
        chunk = chunks_by_id.get(chunk_id) or {}
        try:
            target_indexes.add(int(chunk.get("chunk_index")))
        except Exception:
            match = re.match(r"chunk_(\d+)", chunk_id)
            if match:
                target_indexes.add(int(match.group(1)))
    wanted = {idx + offset for idx in target_indexes for offset in (-1, 1)}
    nearby_ids = [
        chunk_id
        for chunk_id, chunk in chunks_by_id.items()
        if chunk_id not in chunk_ids and int(chunk.get("chunk_index") or -9999) in wanted
    ]
    return _load_structured_outputs(workdir, sorted(nearby_ids), records_by_chunk)


def _ledger_notes(workdir: Path, terms: set[str], limit: int = 16) -> list[str]:
    ledger = _load_json(session_paths(workdir)["ledger"], default={})
    if not isinstance(ledger, dict):
        return []
    notes = []
    for key in ("assumptions", "notes"):
        for item in ledger.get(key) or []:
            text = json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item)
            lower = text.lower()
            if any(term and term in lower for term in terms):
                notes.append(_short_text(text, 700))
    return notes[:limit]


def _verification_refs(workdir: Path, chunk_ids: list[str]) -> tuple[list[dict[str, Any]], list[Path]]:
    refs = []
    paths = []
    checks_dir = workdir / "python_checks"
    results_dir = workdir / "verification_results"
    for chunk_id in chunk_ids:
        check_paths = sorted(checks_dir.glob(f"{chunk_id}*.py")) if checks_dir.exists() else []
        for path in check_paths:
            paths.append(path)
            refs.append({"chunk_id": chunk_id, "kind": "script", "path": str(path), "name": path.name, "excerpt": _short_text(path.read_text(encoding="utf-8"), 900)})
        result_paths = sorted(results_dir.glob(f"{chunk_id}*.result.json")) if results_dir.exists() else []
        for path in result_paths:
            paths.append(path)
            payload = _load_json(path, default={})
            if isinstance(payload, dict):
                refs.append(
                    {
                        "chunk_id": chunk_id,
                        "kind": "result",
                        "path": str(path),
                        "status": payload.get("status"),
                        "stdout": _short_text(payload.get("stdout"), 500),
                        "stderr": _short_text(payload.get("stderr"), 500),
                        "conclusion": _short_text(payload.get("conclusion"), 500),
                    }
                )
    return refs, paths


def _parse_aux_labels(aux_path: Path) -> dict[str, dict[str, str]]:
    labels: dict[str, dict[str, str]] = {}
    if not aux_path.exists():
        return labels
    pattern = re.compile(r"\\newlabel\{([^}]+)\}\{\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}")
    for match in pattern.finditer(aux_path.read_text(encoding="utf-8", errors="replace")):
        labels[match.group(1)] = {
            "number": match.group(2),
            "page": match.group(3),
            "caption": match.group(4),
            "kind": match.group(5),
        }
    return labels


def _tex_source_paths(session: dict[str, Any], audit_workdir: Path) -> tuple[list[Path], Path | None]:
    paths = []
    tex_path = session.get("tex_path")
    if tex_path:
        path = Path(str(tex_path)).expanduser()
        if path.exists():
            paths.append(path.resolve())
    pdf_path = session.get("pdf_path")
    if pdf_path:
        pdf_parent = Path(str(pdf_path)).expanduser().resolve().parent
        paths.extend(path.resolve() for path in sorted(pdf_parent.glob("*.tex")))
    seen = []
    seen_keys = set()
    for path in paths:
        if str(path) not in seen_keys:
            seen.append(path)
            seen_keys.add(str(path))
    aux_path = None
    if pdf_path:
        candidate = Path(str(pdf_path)).expanduser().resolve().with_suffix(".aux")
        if candidate.exists():
            aux_path = candidate
    if aux_path is None and seen:
        candidate = seen[0].with_suffix(".aux")
        if candidate.exists():
            aux_path = candidate
    return seen, aux_path


def _line_excerpt(lines: list[str], index: int, radius: int = 18) -> str:
    start = max(0, index - radius)
    end = min(len(lines), index + radius + 1)
    return "\n".join(f"{line_no + 1}: {lines[line_no].rstrip()}" for line_no in range(start, end))


def _tex_queries(family: dict[str, Any], issues: list[dict[str, Any]], aux_labels: dict[str, dict[str, str]]) -> set[str]:
    queries: set[str] = set()
    refs = [str(item).lower() for item in family.get("main_references") or []]
    for label, info in aux_labels.items():
        number = str(info.get("number") or "").lower()
        caption = str(info.get("caption") or "").lower()
        kind = str(info.get("kind") or "").lower()
        for ref in refs:
            if ref == number or ref in {caption, kind} or (ref.startswith("theorem ") and number in ref):
                queries.add(label)
    all_issue_text = " ".join(json.dumps(issue, ensure_ascii=False).lower() for issue in issues)
    if "leibniz" in all_issue_text:
        queries.add("E:Leibniz")
    if "factorization" in all_issue_text or "factorisation" in all_issue_text:
        queries.add("E:fg")
    if "finite-difference" in all_issue_text or "finite difference" in all_issue_text:
        queries.add("E:finite-diff-k")
    for phrase in ("Proposition 4.1", "Theorem 4.1"):
        if phrase.lower() in all_issue_text or phrase.lower() in " ".join(refs):
            queries.add(phrase)
    return queries


def _tex_excerpts(session: dict[str, Any], audit_workdir: Path, family: dict[str, Any], issues: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[Path]]:
    tex_paths, aux_path = _tex_source_paths(session, audit_workdir)
    aux_labels = _parse_aux_labels(aux_path) if aux_path else {}
    queries = _tex_queries(family, issues, aux_labels)
    excerpts = []
    source_paths = list(tex_paths)
    if aux_path:
        source_paths.append(aux_path)
    for tex_path in tex_paths[:4]:
        lines = tex_path.read_text(encoding="utf-8", errors="replace").splitlines()
        seen_indexes = set()
        for query in sorted(queries):
            for index, line in enumerate(lines):
                if query in line and index not in seen_indexes:
                    seen_indexes.add(index)
                    excerpts.append(
                        {
                            "path": str(tex_path),
                            "query": query,
                            "line": index + 1,
                            "excerpt": _line_excerpt(lines, index),
                        }
                    )
                    break
        if len(excerpts) >= 12:
            break
    return excerpts, source_paths


def _terms_for_overlap(family: dict[str, Any]) -> set[str]:
    terms = {str(item).lower() for item in family.get("main_references") or []}
    terms.update(str(item).lower() for item in family.get("main_symbols") or [])
    terms.update(issue_id.lower() for issue_id in family.get("all_issue_ids") or [])
    for ref in list(terms):
        if re.fullmatch(r"\d+", ref):
            terms.add(f"equation ({ref})")
            terms.add(f"equation {ref}")
    return {term for term in terms if term}


def _build_evidence(audit_workdir: Path, families_payload: dict[str, Any], family: dict[str, Any], max_context_chars: int) -> tuple[dict[str, Any], list[Path]]:
    session = _load_session(audit_workdir)
    issues_by_id = _load_issues(audit_workdir)
    chunks_by_id, records_by_chunk = _load_chunks(audit_workdir)
    issue_ids = [str(item) for item in family.get("all_issue_ids") or []]
    upstream_ids = [str(item) for item in family.get("primary_upstream_issue_ids") or []]
    downstream_ids = [str(item) for item in family.get("downstream_issue_ids") or []]
    related_ids = [str(item) for item in family.get("related_issue_ids") or []]
    issues = [_issue_payload(issue_id, issues_by_id) for issue_id in issue_ids]
    chunk_ids = sorted({str(issue.get("chunk_id")) for issue in issues if issue.get("chunk_id")})
    chunk_records = [_chunk_payload(chunk_id, chunks_by_id, records_by_chunk) for chunk_id in chunk_ids]
    structured_outputs, structured_paths = _load_structured_outputs(audit_workdir, chunk_ids, records_by_chunk)
    nearby_outputs, nearby_paths = _nearby_chunk_summaries(audit_workdir, chunk_ids, chunks_by_id, records_by_chunk)
    terms = _terms_for_overlap(family)
    ledger_notes = _ledger_notes(audit_workdir, terms)
    verification, verification_paths = _verification_refs(audit_workdir, chunk_ids)
    tex_excerpts, tex_paths = _tex_excerpts(session, audit_workdir, family, issues)
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "family": family,
        "audit_session": {
            "model": session.get("model"),
            "reasoning_effort": session.get("reasoning_effort"),
            "audit_context_mode": session.get("audit_context_mode"),
            "pdf_path": session.get("pdf_path"),
            "tex_path": session.get("tex_path"),
        },
        "issues": {
            "upstream": [_issue_payload(issue_id, issues_by_id) for issue_id in upstream_ids],
            "downstream": [_issue_payload(issue_id, issues_by_id) for issue_id in downstream_ids],
            "related": [_issue_payload(issue_id, issues_by_id) for issue_id in related_ids],
            "all": issues,
        },
        "chunks": chunk_records,
        "structured_chunk_outputs": structured_outputs,
        "nearby_chunk_summaries": nearby_outputs[:12],
        "ledger_notes": ledger_notes,
        "verification_refs": verification,
        "tex_excerpts": tex_excerpts,
        "context_cap_chars": max_context_chars,
    }
    encoded = json.dumps(evidence, ensure_ascii=False, indent=2)
    if len(encoded) > max_context_chars:
        evidence["prompt_context_truncation_note"] = (
            f"Prompt context is capped at {max_context_chars} characters; see family_recheck_evidence.json for full dry-run evidence bundle."
        )
    source_paths = [
        session_paths(audit_workdir)["session"],
        session_paths(audit_workdir)["issues"],
        session_paths(audit_workdir)["manifest"],
        session_paths(audit_workdir)["chunk_records"],
        session_paths(audit_workdir)["ledger"],
        *structured_paths,
        *nearby_paths,
        *verification_paths,
        *tex_paths,
    ]
    return evidence, source_paths


def _prompt_context(evidence: dict[str, Any], max_context_chars: int) -> str:
    text = json.dumps(evidence, ensure_ascii=False, indent=2)
    if len(text) <= max_context_chars:
        return text
    return text[: max_context_chars - 500].rstrip() + "\n\n[TRUNCATED: see family_recheck_evidence.json for full evidence bundle]\n"


def _build_prompt(evidence: dict[str, Any], max_context_chars: int) -> str:
    family = evidence.get("family") or {}
    return f"""You are rechecking a mathematical audit issue family. This is not a full chunk audit.

Family: {family.get('family_id')} - {family.get('title')}

Important instructions:
- Treat all prior audit issues as provisional findings, not established facts.
- Recheck the paper evidence directly from the included TeX/chunk excerpts when available.
- Decide whether issues are independent, downstream consequences, over-escalated, false positives, or human-review items.
- Do not invent changes to audit state. Your output is advisory only.
- Prefer calibrated final-report treatment over maximizing issue count.

Answer these questions:
1. Which issue is the upstream issue?
2. Which issues are downstream consequences?
3. Are any issues false positives?
4. Are any severities over-escalated?
5. Which issues should appear in the concise/referee report?
6. Which issues should be grouped under the upstream issue?
7. What should the final report wording be?
8. What remains uncertain or needs human review?

Return JSON matching this schema:
{json.dumps(RESULT_SCHEMA, indent=2)}

Evidence bundle:
{_prompt_context(evidence, max_context_chars)}
"""


def _notes_md(family: dict[str, Any], live: bool) -> str:
    lines = [
        f"# Issue Family Recheck Notes: {family.get('family_id')}",
        "",
        f"Family: {family.get('title')}",
        "",
        "Dry-run artifact: no model output was generated." if not live else "Live run artifact: review model output before changing audit state.",
        "",
        "## Manual Review Checklist",
        "- Confirm the upstream issue from the paper text.",
        "- Decide which downstream issues should be grouped rather than listed independently.",
        "- Check whether any issue is over-escalated or false positive.",
        "- Decide concise-report wording.",
        "- Record any remaining human-review uncertainties.",
        "",
    ]
    return "\n".join(lines)


def _response_text(resp: Any) -> str:
    text = getattr(resp, "output_text", None)
    if text:
        return str(text).strip()
    raw = to_jsonable(resp)
    parts = []
    for item in raw.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts).strip()


def _save_live_outputs(output_dir: Path, prompt: str, model: str, reasoning_effort: str, poll_every: float, max_wait_seconds: float | None) -> dict[str, Any]:
    client = _get_client()
    request_kwargs = {
        "model": model,
        "reasoning": {"effort": reasoning_effort},
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "issue_family_recheck",
                "strict": True,
                "schema": RESULT_SCHEMA,
            }
        },
        "background": True,
        "store": True,
    }
    resp = client.responses.create(**request_kwargs)
    if getattr(resp, "status", None) not in {None, "completed"}:
        resp = wait_for_response(resp.id, poll_every=poll_every, max_wait_seconds=max_wait_seconds)
    raw = to_jsonable(resp)
    _write_json(output_dir / "family_recheck_raw_response.json", raw)
    text = _response_text(resp)
    if text:
        (output_dir / "family_recheck_raw_response.txt").write_text(text, encoding="utf-8")
    try:
        result = json.loads(text)
    except Exception:
        result = {"parse_error": "response output was not valid JSON", "raw_text": text}
    _write_json(output_dir / "family_recheck_result.json", result)
    (output_dir / "family_recheck_result.md").write_text("# Issue Family Recheck Result\n\n```json\n" + json.dumps(result, indent=2, ensure_ascii=False) + "\n```\n", encoding="utf-8")
    usage_obj = raw.get("usage") if isinstance(raw, dict) else {}
    usage_obj = usage_obj if isinstance(usage_obj, dict) else {}
    usage_payload = {
        "usage": usage_obj,
        "cost": compute_usage_cost(model, usage_obj),
        "usage_diagnostics": usage_cache_diagnostics(usage_obj),
    }
    _write_json(output_dir / "usage_cost.json", usage_payload)
    return {"response_id": raw.get("id"), "status": raw.get("status"), "usage_cost": usage_payload}


def run_issue_family_recheck(
    audit_workdir: Path,
    families_json: Path,
    family_id: str,
    output_dir: Path,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    live: bool = False,
    max_context_chars: int = 30000,
    poll_every: float = 3.0,
    max_wait_seconds: float | None = None,
    allow_output_inside_audit: bool = False,
) -> dict[str, Any]:
    audit_workdir = audit_workdir.expanduser().resolve()
    families_json = families_json.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not audit_workdir.exists():
        raise RuntimeError(f"Audit workdir does not exist: {audit_workdir}")
    if not families_json.exists():
        raise RuntimeError(f"Families JSON does not exist: {families_json}")
    if not allow_output_inside_audit:
        _guard_output_dir(audit_workdir, output_dir)
    if live and not os.environ.get("OPENAI_API_KEY"):
        try:
            _get_client()
        except Exception as exc:
            raise RuntimeError("OPENAI_API_KEY is required for --live mode.") from exc
    validate_result_schema(RESULT_SCHEMA)

    session = _load_session(audit_workdir)
    selected_model = str(model or session.get("model") or "gpt-5.5")
    selected_effort = str(reasoning_effort or session.get("reasoning_effort") or "xhigh")
    families_payload = _load_json(families_json, default={})
    if not isinstance(families_payload, dict):
        raise RuntimeError(f"Families JSON is not an object: {families_json}")
    family = _select_family(families_payload, family_id)
    evidence, source_paths = _build_evidence(audit_workdir, families_payload, family, max_context_chars=max_context_chars)
    source_paths.append(families_json)
    before = _snapshot_paths(source_paths)
    prompt = _build_prompt(evidence, max_context_chars=max_context_chars)

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "family_recheck_evidence.json", evidence)
    (output_dir / "family_recheck_prompt.txt").write_text(prompt, encoding="utf-8")
    (output_dir / "family_recheck_notes.md").write_text(_notes_md(family, live), encoding="utf-8")

    live_result: dict[str, Any] | None = None
    if live:
        live_result = _save_live_outputs(output_dir, prompt, selected_model, selected_effort, poll_every, max_wait_seconds)

    after = _snapshot_paths(source_paths)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "audit_workdir": str(audit_workdir),
        "families_json": str(families_json),
        "family_id": family_id,
        "output_dir": str(output_dir),
        "live": bool(live),
        "dry_run": not live,
        "would_call_api": bool(live),
        "live_result": live_result,
        "model": selected_model,
        "reasoning_effort": selected_effort,
        "max_context_chars": max_context_chars,
        "artifacts": {
            "manifest": "family_recheck_manifest.json",
            "prompt": "family_recheck_prompt.txt",
            "evidence": "family_recheck_evidence.json",
            "notes": "family_recheck_notes.md",
        },
        "live_artifacts": [
            "family_recheck_raw_response.json",
            "family_recheck_raw_response.txt",
            "family_recheck_result.json",
            "family_recheck_result.md",
            "usage_cost.json",
        ]
        if live
        else [],
        "evidence_summary": {
            "issue_count": len(evidence.get("issues", {}).get("all", [])),
            "chunk_count": len(evidence.get("chunks", [])),
            "structured_output_count": len(evidence.get("structured_chunk_outputs", [])),
            "tex_excerpt_count": len(evidence.get("tex_excerpts", [])),
            "verification_ref_count": len(evidence.get("verification_refs", [])),
        },
        "source_mutation_policy": (
            "read-only; canonical audit state, issue state, and family JSON are never modified"
            if allow_output_inside_audit
            else "read-only; source audit folder, issue state, and family JSON are never modified"
        ),
        "source_unmodified_by_script": before == after,
    }
    _write_json(output_dir / "family_recheck_manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or run a focused issue-family recheck request.")
    parser.add_argument("--audit-workdir", required=True, type=Path)
    parser.add_argument("--families-json", required=True, type=Path)
    parser.add_argument("--family-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-context-chars", type=int, default=30000)
    parser.add_argument("--poll-every", type=float, default=3.0)
    parser.add_argument("--max-wait-seconds", type=float)
    args = parser.parse_args(argv)
    manifest = run_issue_family_recheck(
        args.audit_workdir,
        args.families_json,
        args.family_id,
        args.output_dir,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        live=bool(args.live),
        max_context_chars=int(args.max_context_chars),
        poll_every=float(args.poll_every),
        max_wait_seconds=args.max_wait_seconds,
    )
    print("Issue family recheck prepared.")
    print(f"  Family: {manifest['family_id']}")
    print(f"  Output dir: {manifest['output_dir']}")
    print(f"  Dry run: {manifest['dry_run']}")
    print(f"  Source unmodified by script: {manifest['source_unmodified_by_script']}")
    print(f"  Evidence summary: {manifest['evidence_summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
