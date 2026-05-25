#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


SEVERITY_RANK = {
    "unknown": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

RECHECK_RISK_TERMS = {
    "algebra",
    "asymptotic",
    "contradiction",
    "curvature",
    "dependency",
    "downstream",
    "equation",
    "propagation",
    "reference",
    "sign",
    "uniform",
    "variance",
}

DOWNSTREAM_TERMS = {
    "assuming",
    "conditional",
    "depend",
    "downstream",
    "earlier issue",
    "inherits",
    "propagat",
    "relies on",
    "unresolved",
}

STOPWORDS = {
    "about",
    "above",
    "after",
    "against",
    "also",
    "because",
    "before",
    "being",
    "between",
    "cannot",
    "could",
    "audit",
    "does",
    "during",
    "each",
    "from",
    "have",
    "into",
    "later",
    "more",
    "must",
    "paper",
    "proof",
    "should",
    "shows",
    "such",
    "than",
    "that",
    "their",
    "then",
    "there",
    "these",
    "this",
    "through",
    "under",
    "using",
    "which",
    "while",
    "with",
    "would",
}

GENERIC_MATH_TERMS = {
    "asymptotic",
    "bound",
    "coefficient",
    "constant",
    "definition",
    "displayed",
    "equation",
    "error",
    "estimate",
    "expression",
    "formula",
    "function",
    "identity",
    "lambda",
    "lemma",
    "proof",
    "proposition",
    "result",
    "section",
    "series",
    "term",
    "theorem",
}

STANDARD_LATEX_COMMANDS = {
    "alpha",
    "beta",
    "cdot",
    "cos",
    "exp",
    "frac",
    "ge",
    "in",
    "int",
    "lambda",
    "le",
    "left",
    "log",
    "max",
    "min",
    "pi",
    "rho",
    "right",
    "sin",
    "sqrt",
    "sum",
    "theta",
    "times",
}


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


def _short_text(text: Any, limit: int = 240) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)].rstrip() + "..."


def _issue_text(issue: dict[str, Any]) -> str:
    fields = [
        issue.get("issue_id"),
        issue.get("title"),
        issue.get("severity"),
        issue.get("status"),
        issue.get("location"),
        issue.get("description"),
        issue.get("evidence"),
        issue.get("proposed_fix"),
        " ".join(str(tag) for tag in issue.get("tags") or []),
    ]
    return "\n".join(str(field) for field in fields if field)


def _normalize_label(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def _extract_equation_refs(text: str) -> list[str]:
    refs: set[str] = set()
    for match in re.finditer(r"\\(?:eqref|ref|label)\{([^}]{1,80})\}", text):
        refs.add(_normalize_label(match.group(1)))
    for match in re.finditer(
        r"(?i)\b(?:equation|eq\.?|formula|identity)\s*(?:\(|\[)?([A-Za-z]?\d+(?:\.\d+)*[a-z]?|[A-Za-z][A-Za-z0-9_:.:-]{2,})(?:\)|\])?",
        text,
    ):
        value = _normalize_label(match.group(1))
        if any(ch.isdigit() for ch in value) or ":" in value:
            refs.add(value)
    return sorted(refs)


def _extract_theorem_refs(text: str) -> list[str]:
    refs: set[str] = set()
    for match in re.finditer(
        r"\b(Theorem|Lemma|Proposition|Corollary|Definition|Remark)\s+((?:[A-Z]\.)?\d+(?:\.\d+)*|[A-Z])",
        text,
    ):
        refs.add(f"{match.group(1).title()} {match.group(2)}")
    return sorted(refs)


def _extract_symbols(text: str) -> list[str]:
    symbols: set[str] = set()
    for match in re.finditer(r"(?<!\\)\$(?!\$)(.{1,80}?)(?<!\\)\$", text, flags=re.DOTALL):
        snippet = _normalize_label(match.group(1))
        if len(snippet) <= 2:
            continue
        if (
            "\\" in snippet
            or "_" in snippet
            or "*" in snippet
            or re.search(r"\b[A-Z]\s*\(", snippet)
        ):
            symbols.add(snippet)
    for match in re.finditer(r"\b[A-Z]\s*\([A-Za-z0-9_*+\-/,\s]{1,24}\)", text):
        symbols.add(_normalize_label(match.group(0)))
    for match in re.finditer(r"\\([A-Za-z]{2,})", text):
        command = match.group(1)
        if command not in STANDARD_LATEX_COMMANDS:
            symbols.add("\\" + command)
    return sorted(symbols)[:30]


def _keyword_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text.lower()):
        token = raw.strip("_-")
        if len(token) < 4:
            continue
        if token in STOPWORDS or token in GENERIC_MATH_TERMS:
            continue
        tokens.add(token)
    return tokens


def _extract_risk_terms(issue: dict[str, Any]) -> list[str]:
    text = _issue_text(issue).lower()
    tags = {str(tag).lower() for tag in issue.get("tags") or []}
    hits: set[str] = set()
    for term in RECHECK_RISK_TERMS:
        if term in tags or term in text:
            hits.add(term)
    return sorted(hits)


def _has_downstream_language(issue: dict[str, Any]) -> bool:
    text = _issue_text(issue).lower()
    tags = {str(tag).lower() for tag in issue.get("tags") or []}
    if tags & {"dependency", "downstream", "propagation", "inherited"}:
        return True
    return any(term in text for term in DOWNSTREAM_TERMS)


def _chunk_index_from_id(chunk_id: str) -> int | None:
    match = re.match(r"^chunk_(\d+)$", str(chunk_id or ""))
    if not match:
        return None
    return int(match.group(1))


def _chunk_index_from_issue(issue: dict[str, Any], chunks_by_id: dict[str, dict[str, Any]]) -> int | None:
    chunk_id = str(issue.get("chunk_id") or "")
    chunk = chunks_by_id.get(chunk_id) or {}
    try:
        return int(chunk.get("chunk_index"))
    except Exception:
        return _chunk_index_from_id(chunk_id)


def _load_issues(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path, default={})
    if isinstance(payload, dict) and isinstance(payload.get("issues"), list):
        return [item for item in payload["issues"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _load_chunks(workdir: Path) -> dict[str, dict[str, Any]]:
    manifest = _load_json(workdir / "state" / "chunk_manifest.json", default={})
    chunks: dict[str, dict[str, Any]] = {}
    if isinstance(manifest, dict):
        for item in manifest.get("chunks") or []:
            if isinstance(item, dict) and item.get("chunk_id"):
                chunks[str(item["chunk_id"])] = dict(item)
    for record in _read_jsonl(workdir / "state" / "chunks.jsonl"):
        chunk_id = str(record.get("chunk_id") or "")
        if not chunk_id:
            continue
        merged = dict(chunks.get(chunk_id) or {})
        merged.update(record)
        chunks[chunk_id] = merged
    return chunks


def _structured_path_for_chunk(workdir: Path, chunk: dict[str, Any]) -> Path | None:
    candidates = [chunk.get("structured_response_path"), chunk.get("response_path")]
    chunk_id = str(chunk.get("chunk_id") or "")
    if chunk_id:
        candidates.extend(str(path) for path in sorted((workdir / "responses").glob(f"*{chunk_id}*structured*.json")))
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate))
        if not path.is_absolute():
            path = workdir / path
        if path.exists():
            return path
    return None


def _structured_summary_text(payload: dict[str, Any], max_chars: int = 1600) -> str:
    parts: list[str] = []
    for key in ("assumptions_and_notation", "verified_steps"):
        value = payload.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value[:8])
    ledger = payload.get("ledger_updates")
    if isinstance(ledger, dict):
        for key in ("assumptions", "notes"):
            value = ledger.get(key)
            if isinstance(value, list):
                parts.extend(str(item) for item in value[:8])
    if payload.get("next_boundary_hint"):
        parts.append(str(payload.get("next_boundary_hint")))
    return _short_text("\n".join(parts), max_chars)


def _flatten_ledger_items(ledger: Any) -> list[str]:
    if not isinstance(ledger, dict):
        return []
    items: list[str] = []
    for key in ("assumptions", "notes"):
        value = ledger.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                items.append(_short_text(json.dumps(item, ensure_ascii=False), 600))
            else:
                items.append(str(item))
    return items


def _candidate_reasons(
    issue: dict[str, Any],
    min_severity: str,
    include_medium: bool,
) -> list[str]:
    status = str(issue.get("status") or "open").lower()
    if status in {"resolved", "closed"}:
        return []
    severity = str(issue.get("severity") or "unknown").lower()
    rank = SEVERITY_RANK.get(severity, 0)
    min_rank = SEVERITY_RANK.get(min_severity, SEVERITY_RANK["high"])
    reasons: list[str] = []
    if rank >= min_rank:
        reasons.append(f"open {severity} issue")
    elif include_medium and severity == "medium":
        reasons.append("open medium issue included by --include-medium")
    risk_terms = _extract_risk_terms(issue)
    if risk_terms and reasons:
        reasons.append("recheck-risk wording/tags: " + ", ".join(risk_terms))
    return reasons


def _verification_index(workdir: Path) -> tuple[dict[str, list[dict[str, str]]], list[Path]]:
    by_chunk: dict[str, list[dict[str, str]]] = defaultdict(list)
    source_paths: list[Path] = []
    checks_dir = workdir / "python_checks"
    if checks_dir.exists():
        for path in sorted(checks_dir.glob("*.py")):
            source_paths.append(path)
            chunk_match = re.match(r"(chunk_\d+)", path.name)
            chunk_id = chunk_match.group(1) if chunk_match else ""
            by_chunk[chunk_id].append({"kind": "script", "path": str(path), "name": path.name})
    results_dir = workdir / "verification_results"
    if results_dir.exists():
        for path in sorted(results_dir.glob("*.result.json")):
            source_paths.append(path)
            payload = _load_json(path, default={})
            if isinstance(payload, dict):
                chunk_id = str(payload.get("chunk_id") or "")
                if not chunk_id:
                    match = re.match(r"(chunk_\d+)", path.name)
                    chunk_id = match.group(1) if match else ""
                by_chunk[chunk_id].append(
                    {
                        "kind": "result",
                        "path": str(path),
                        "name": path.name,
                        "status": str(payload.get("status") or payload.get("outcome") or "unknown"),
                    }
                )
    return dict(by_chunk), source_paths


def _candidate_from_issue(
    issue: dict[str, Any],
    chunks_by_id: dict[str, dict[str, Any]],
    verification_by_chunk: dict[str, list[dict[str, str]]],
    selection_reasons: list[str],
) -> dict[str, Any]:
    text = _issue_text(issue)
    chunk_id = str(issue.get("chunk_id") or "")
    chunk = chunks_by_id.get(chunk_id) or {}
    verification_refs = verification_by_chunk.get(chunk_id, [])
    return {
        "issue_id": str(issue.get("issue_id") or ""),
        "severity": str(issue.get("severity") or "unknown").lower(),
        "status": str(issue.get("status") or "open").lower(),
        "chunk_id": chunk_id,
        "chunk_index": _chunk_index_from_issue(issue, chunks_by_id),
        "title": str(issue.get("title") or ""),
        "location": str(issue.get("location") or ""),
        "tags": [str(tag) for tag in issue.get("tags") or []],
        "short_description": _short_text(issue.get("description"), 360),
        "proposed_fix": _short_text(issue.get("proposed_fix"), 360),
        "selection_reasons": selection_reasons,
        "risk_terms": _extract_risk_terms(issue),
        "downstream_language": _has_downstream_language(issue),
        "features": {
            "equation_refs": _extract_equation_refs(text),
            "theorem_refs": _extract_theorem_refs(text),
            "symbols": _extract_symbols(text),
            "keywords": sorted(_keyword_tokens(text))[:24],
        },
        "source_chunk": {
            "label": chunk.get("display_label") or chunk.get("label") or "",
            "boundary": chunk.get("boundary") or "",
            "page_start": chunk.get("page_start"),
            "page_end": chunk.get("page_end"),
        },
        "has_verification": bool(verification_refs),
        "verification": {
            "script_count": sum(1 for item in verification_refs if item.get("kind") == "script"),
            "result_count": sum(1 for item in verification_refs if item.get("kind") == "result"),
            "refs": verification_refs[:12],
        },
        "group_ids": [],
        "group_role": "unclassified",
    }


def _feature_sets(candidate: dict[str, Any]) -> dict[str, set[str]]:
    features = candidate.get("features") or {}
    return {
        "equation_refs": {str(item).lower() for item in features.get("equation_refs") or []},
        "theorem_refs": {str(item).lower() for item in features.get("theorem_refs") or []},
        "symbols": {str(item).lower() for item in features.get("symbols") or []},
        "keywords": {str(item).lower() for item in features.get("keywords") or []},
    }


def _distinctive_symbols(symbols: set[str]) -> set[str]:
    generic = {
        "k",
        "n",
        "x",
        "r",
        "z",
        "lambda",
        "\\lambda",
        "rho",
        "\\rho",
        "\\asymp",
        "\\infty",
        "\\to",
        "\\varepsilon",
        "o(1)",
    }
    return {
        symbol
        for symbol in symbols
        if symbol not in generic
        and (
            "\\" in symbol
            or "_" in symbol
            or "*" in symbol
            or re.search(r"[a-z]\s*\(", symbol)
            or len(symbol) >= 5
        )
    }


def _shared_features(left: dict[str, Any], right: dict[str, Any]) -> dict[str, list[str]]:
    left_sets = _feature_sets(left)
    right_sets = _feature_sets(right)
    shared = {
        "equation_refs": sorted(left_sets["equation_refs"] & right_sets["equation_refs"]),
        "theorem_refs": sorted(left_sets["theorem_refs"] & right_sets["theorem_refs"]),
        "symbols": sorted(_distinctive_symbols(left_sets["symbols"]) & _distinctive_symbols(right_sets["symbols"])),
        "keywords": sorted((left_sets["keywords"] & right_sets["keywords"]) - GENERIC_MATH_TERMS),
    }
    return {key: value for key, value in shared.items() if value}


def _has_strong_shared_symbol(symbols: list[str]) -> bool:
    for symbol in symbols:
        # A single-letter function such as W(n) is often too broad in long
        # asymptotic papers; labels or richer symbols should carry the link.
        if re.fullmatch(r"[a-z]\s*\([a-z0-9_*+\-/,\s]{1,16}\)", symbol):
            continue
        return True
    return False


def _link_candidates(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any] | None:
    shared = _shared_features(left, right)
    if not shared:
        return None
    left_index = left.get("chunk_index") or 0
    right_index = right.get("chunk_index") or 0
    later = right if right_index >= left_index else left
    reasons: list[str] = []
    strong = bool(
        shared.get("equation_refs")
        or shared.get("theorem_refs")
        or _has_strong_shared_symbol(shared.get("symbols") or [])
    )
    if shared.get("equation_refs"):
        reasons.append("shared equation/reference labels")
    if shared.get("theorem_refs"):
        reasons.append("shared theorem-like references")
    if shared.get("symbols") and _has_strong_shared_symbol(shared.get("symbols") or []):
        reasons.append("shared distinctive symbols")
    if len(shared.get("keywords") or []) >= 3:
        reasons.append("shared distinctive keywords")
    keyword_count = len(shared.get("keywords") or [])
    if later.get("downstream_language") and (strong or keyword_count >= 3):
        reasons.append("later issue has dependency/propagation wording")
    if strong:
        return {
            "left_issue_id": left["issue_id"],
            "right_issue_id": right["issue_id"],
            "shared_features": shared,
            "reasons": reasons or ["related by shared features"],
        }
    return None


class _UnionFind:
    def __init__(self, values: list[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _build_groups(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ids = [candidate["issue_id"] for candidate in candidates if candidate.get("issue_id")]
    by_id = {candidate["issue_id"]: candidate for candidate in candidates}
    uf = _UnionFind(ids)
    links: list[dict[str, Any]] = []
    for i, left in enumerate(candidates):
        for right in candidates[i + 1 :]:
            link = _link_candidates(left, right)
            if link is None:
                continue
            links.append(link)
            uf.union(link["left_issue_id"], link["right_issue_id"])

    grouped_ids: dict[str, list[str]] = defaultdict(list)
    for issue_id in ids:
        grouped_ids[uf.find(issue_id)].append(issue_id)

    groups: list[dict[str, Any]] = []
    for raw_group_id, issue_ids in sorted(grouped_ids.items()):
        if len(issue_ids) < 2:
            continue
        members = [by_id[issue_id] for issue_id in issue_ids]
        members.sort(key=lambda item: (item.get("chunk_index") or 0, item.get("issue_id") or ""))
        upstream = next((item for item in members if not item.get("downstream_language")), members[0])
        group_id = f"G{len(groups) + 1:03d}"
        group_links = [
            link
            for link in links
            if link["left_issue_id"] in issue_ids and link["right_issue_id"] in issue_ids
        ]
        shared_counter: Counter[str] = Counter()
        for link in group_links:
            for values in (link.get("shared_features") or {}).values():
                for value in values:
                    shared_counter[str(value)] += 1
        member_payload: list[dict[str, Any]] = []
        for member in members:
            if member["issue_id"] == upstream["issue_id"]:
                role = "candidate_upstream"
            elif member.get("downstream_language"):
                role = "possible_downstream"
            else:
                role = "related_same_topic"
            member["group_ids"].append(group_id)
            member["group_role"] = role
            member_payload.append(
                {
                    "issue_id": member["issue_id"],
                    "chunk_id": member["chunk_id"],
                    "severity": member["severity"],
                    "role": role,
                    "title": member["title"],
                }
            )
        groups.append(
            {
                "group_id": group_id,
                "upstream_issue_id": upstream["issue_id"],
                "classification": "tentative_dependency_or_same-topic_group",
                "members": member_payload,
                "link_reasons": sorted({reason for link in group_links for reason in link.get("reasons", [])}),
                "shared_features": [item for item, _count in shared_counter.most_common(20)],
                "links": group_links,
            }
        )
    return groups, links


def _overlap_terms(candidate: dict[str, Any]) -> set[str]:
    features = candidate.get("features") or {}
    terms = set(str(item).lower() for item in features.get("equation_refs") or [])
    terms.update(str(item).lower() for item in features.get("theorem_refs") or [])
    terms.update(str(item).lower() for item in features.get("symbols") or [])
    terms.update(str(item).lower() for item in features.get("keywords") or [])
    return {term for term in terms if len(term) >= 3}


def _matches_terms(text: str, terms: set[str]) -> bool:
    lower = text.lower()
    return any(term and term in lower for term in terms)


def _build_evidence(
    candidate: dict[str, Any],
    all_issues: list[dict[str, Any]],
    chunks_by_id: dict[str, dict[str, Any]],
    structured_by_chunk: dict[str, str],
    ledger_items: list[str],
    max_context_chars: int,
) -> dict[str, Any]:
    terms = _overlap_terms(candidate)
    chunk_index = candidate.get("chunk_index") or 0
    budget = max(500, int(max_context_chars))

    ledger_snippets = [_short_text(item, 360) for item in ledger_items if _matches_terms(item, terms)]
    later_issue_snippets: list[dict[str, str]] = []
    for issue in all_issues:
        issue_id = str(issue.get("issue_id") or "")
        if issue_id == candidate.get("issue_id"):
            continue
        other_index = _chunk_index_from_issue(issue, chunks_by_id) or 0
        if other_index < chunk_index:
            continue
        text = _issue_text(issue)
        if _matches_terms(text, terms):
            later_issue_snippets.append(
                {
                    "issue_id": issue_id,
                    "chunk_id": str(issue.get("chunk_id") or ""),
                    "severity": str(issue.get("severity") or "unknown"),
                    "title": _short_text(issue.get("title"), 160),
                }
            )

    later_chunk_snippets: list[dict[str, str]] = []
    for chunk_id, summary in structured_by_chunk.items():
        chunk = chunks_by_id.get(chunk_id) or {}
        other_index = chunk.get("chunk_index") or _chunk_index_from_id(chunk_id) or 0
        if other_index <= chunk_index:
            continue
        if summary and _matches_terms(summary, terms):
            later_chunk_snippets.append(
                {
                    "chunk_id": chunk_id,
                    "label": str(chunk.get("display_label") or chunk.get("label") or ""),
                    "snippet": _short_text(summary, 360),
                }
            )

    issue_text = _short_text(
        "\n".join(
            str(candidate.get(key) or "")
            for key in ("title", "location", "short_description", "proposed_fix")
        ),
        min(800, budget),
    )
    return {
        "note": "Evidence is deterministic context for human/LLM recheck; it is not a truth judgment.",
        "issue_text": issue_text,
        "source_chunk": candidate.get("source_chunk") or {},
        "ledger_snippets": ledger_snippets[:6],
        "later_issue_snippets": later_issue_snippets[:8],
        "later_chunk_snippets": later_chunk_snippets[:6],
        "verification_refs": (candidate.get("verification") or {}).get("refs", [])[:8],
        "context_cap_chars": budget,
    }


def _collect_structured_summaries(
    workdir: Path,
    chunks_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], list[Path], list[str]]:
    summaries: dict[str, str] = {}
    source_paths: list[Path] = []
    warnings: list[str] = []
    for chunk_id, chunk in sorted(chunks_by_id.items()):
        path = _structured_path_for_chunk(workdir, chunk)
        if path is None:
            continue
        source_paths.append(path)
        payload = _load_json(path, default={})
        if isinstance(payload, dict):
            summaries[chunk_id] = _structured_summary_text(payload)
        else:
            warnings.append(f"{chunk_id}: structured response is not a JSON object")
    return summaries, source_paths, warnings


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(Counter(str(item.get(key) or "unknown") for item in items))


def _markdown_report(manifest: dict[str, Any]) -> str:
    lines = [
        "# Issue Recheck Candidates",
        "",
        "This deterministic preparation pass does not decide mathematical truth, close issues, or modify audit state.",
        "Use it to prioritize high/critical rechecks and inspect possible dependency-propagation groups.",
        "",
        "## Summary",
        f"- Source audit: {manifest['source_audit_workdir']}",
        f"- Candidates: {manifest['candidate_count']}",
        f"- Tentative groups: {manifest['group_count']}",
        f"- Source unmodified by script: {manifest['source_unmodified_by_script']}",
        "",
        "## Candidates By Severity",
    ]
    for severity in ("critical", "high", "medium", "low", "unknown"):
        count = manifest.get("candidate_counts_by_severity", {}).get(severity, 0)
        if count:
            lines.append(f"- {severity}: {count}")
    if not any(manifest.get("candidate_counts_by_severity", {}).values()):
        lines.append("- none")

    lines.extend(["", "## Tentative Dependency / Same-Topic Groups"])
    groups = manifest.get("groups") or []
    if not groups:
        lines.append("- none")
    for group in groups:
        lines.append("")
        lines.append(f"### {group['group_id']} upstream candidate: {group['upstream_issue_id']}")
        lines.append(f"- Link reasons: {', '.join(group.get('link_reasons') or []) or 'shared features'}")
        lines.append(f"- Shared features: {', '.join(group.get('shared_features') or []) or 'n/a'}")
        for member in group.get("members") or []:
            lines.append(
                "- "
                f"{member['issue_id']} | {member['severity']} | {member['role']} | "
                f"{member['chunk_id']} | {member['title']}"
            )

    lines.extend(["", "## Candidate Details"])
    for candidate in manifest.get("candidates") or []:
        features = candidate.get("features") or {}
        lines.append("")
        lines.append(f"### {candidate['issue_id']} [{candidate['severity']}] {candidate['title']}")
        lines.append(f"- Status: {candidate['status']}")
        lines.append(f"- Chunk: {candidate['chunk_id']}")
        lines.append(f"- Group role: {candidate.get('group_role', 'unclassified')}")
        lines.append(f"- Selection reasons: {', '.join(candidate.get('selection_reasons') or [])}")
        lines.append(f"- Risk terms: {', '.join(candidate.get('risk_terms') or []) or 'none'}")
        lines.append(f"- Location: {candidate.get('location') or 'n/a'}")
        lines.append(f"- Tags: {', '.join(candidate.get('tags') or []) or 'none'}")
        lines.append(f"- Equation refs: {', '.join(features.get('equation_refs') or []) or 'none'}")
        lines.append(f"- Theorem refs: {', '.join(features.get('theorem_refs') or []) or 'none'}")
        lines.append(f"- Symbols: {', '.join(features.get('symbols') or []) or 'none'}")
        lines.append(f"- Verification refs: {(candidate.get('verification') or {}).get('script_count', 0)} scripts, {(candidate.get('verification') or {}).get('result_count', 0)} results")
        if candidate.get("short_description"):
            lines.append(f"- Description: {candidate['short_description']}")
        evidence = candidate.get("evidence") or {}
        ledger = evidence.get("ledger_snippets") or []
        later_issues = evidence.get("later_issue_snippets") or []
        later_chunks = evidence.get("later_chunk_snippets") or []
        if ledger:
            lines.append("- Ledger overlap:")
            for item in ledger[:3]:
                lines.append(f"  - {item}")
        if later_issues:
            lines.append("- Later issue overlap:")
            for item in later_issues[:4]:
                lines.append(f"  - {item['issue_id']} ({item['severity']}, {item['chunk_id']}): {item['title']}")
        if later_chunks:
            lines.append("- Later chunk/context overlap:")
            for item in later_chunks[:3]:
                lines.append(f"  - {item['chunk_id']}: {item['snippet']}")
    lines.append("")
    return "\n".join(lines)


def prepare_issue_recheck_candidates(
    audit_workdir: Path,
    output_dir: Path,
    *,
    min_severity: str = "high",
    include_medium: bool = False,
    max_context_chars: int = 2200,
) -> dict[str, Any]:
    audit_workdir = audit_workdir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not audit_workdir.exists():
        raise RuntimeError(f"Audit workdir does not exist: {audit_workdir}")
    if output_dir == audit_workdir or audit_workdir in output_dir.parents:
        raise RuntimeError("Output directory must not be inside the source audit workdir.")
    if min_severity not in SEVERITY_RANK:
        raise RuntimeError(f"Unsupported min severity: {min_severity}")

    issues_path = audit_workdir / "state" / "issues.json"
    chunks_path = audit_workdir / "state" / "chunks.jsonl"
    manifest_path = audit_workdir / "state" / "chunk_manifest.json"
    ledger_path = audit_workdir / "state" / "ledger.json"
    verification_path = audit_workdir / "state" / "verification.json"

    issues = _load_issues(issues_path)
    chunks_by_id = _load_chunks(audit_workdir)
    ledger = _load_json(ledger_path, default={})
    ledger_items = _flatten_ledger_items(ledger)
    verification_by_chunk, verification_paths = _verification_index(audit_workdir)
    structured_summaries, structured_paths, structured_warnings = _collect_structured_summaries(audit_workdir, chunks_by_id)

    source_paths = [
        issues_path,
        chunks_path,
        manifest_path,
        ledger_path,
        verification_path,
        *verification_paths,
        *structured_paths,
    ]
    before = _snapshot_paths(source_paths)

    candidates: list[dict[str, Any]] = []
    for issue in issues:
        reasons = _candidate_reasons(issue, min_severity=min_severity, include_medium=include_medium)
        if not reasons:
            continue
        candidate = _candidate_from_issue(issue, chunks_by_id, verification_by_chunk, reasons)
        candidates.append(candidate)

    candidates.sort(key=lambda item: (item.get("chunk_index") or 0, item.get("issue_id") or ""))
    groups, links = _build_groups(candidates)
    for candidate in candidates:
        candidate["evidence"] = _build_evidence(
            candidate,
            issues,
            chunks_by_id,
            structured_summaries,
            ledger_items,
            max_context_chars=max_context_chars,
        )

    after = _snapshot_paths(source_paths)

    manifest = {
        "generated_at": _utc_now(),
        "source_audit_workdir": str(audit_workdir),
        "output_dir": str(output_dir),
        "source_mutation_policy": "read-only; source audit folder is never written",
        "source_unmodified_by_script": before == after,
        "selection": {
            "min_severity": min_severity,
            "include_medium": include_medium,
            "max_context_chars": max_context_chars,
            "note": "Candidates are selected for recheck preparation only; no mathematical truth is decided.",
        },
        "issue_count_total": len(issues),
        "candidate_count": len(candidates),
        "group_count": len(groups),
        "candidate_counts_by_severity": _count_by(candidates, "severity"),
        "candidate_counts_by_role": _count_by(candidates, "group_role"),
        "warnings": structured_warnings,
        "links": links,
        "groups": groups,
        "candidates": candidates,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "issue_recheck_candidates.json", manifest)
    (output_dir / "issue_recheck_candidates.md").write_text(_markdown_report(manifest), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare deterministic high/critical issue recheck candidates without mutating an audit folder."
    )
    parser.add_argument("--audit-workdir", required=True, type=Path, help="Existing audit workdir to inspect read-only.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for generated JSON/Markdown summaries.")
    parser.add_argument(
        "--min-severity",
        default="high",
        choices=["critical", "high", "medium", "low"],
        help="Minimum open issue severity to include. Default: high.",
    )
    parser.add_argument(
        "--include-medium",
        action="store_true",
        help="Include open medium issues even when --min-severity is high/critical.",
    )
    parser.add_argument(
        "--max-context-chars",
        type=int,
        default=2200,
        help="Approximate per-issue evidence snippet cap. Default: 2200.",
    )
    args = parser.parse_args(argv)

    try:
        manifest = prepare_issue_recheck_candidates(
            args.audit_workdir,
            args.output_dir,
            min_severity=args.min_severity,
            include_medium=args.include_medium,
            max_context_chars=args.max_context_chars,
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("Issue recheck candidates prepared.")
    print(f"  Source audit: {manifest['source_audit_workdir']}")
    print(f"  Output dir: {manifest['output_dir']}")
    print(f"  Total issues inspected: {manifest['issue_count_total']}")
    print(f"  Candidates: {manifest['candidate_count']}")
    print(f"  Tentative groups: {manifest['group_count']}")
    print(f"  Source unmodified by script: {manifest['source_unmodified_by_script']}")
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
