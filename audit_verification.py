from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from audit_state import (
    append_jsonl,
    load_issues,
    load_json,
    load_ledger,
    load_manifest,
    load_session_from_pdf,
    save_json,
    session_paths,
    utc_now,
)


_VERIFICATION_SCRIPT_RE = re.compile(r"^(chunk_\d+)_check_\d+\.py$")
VERIFICATION_RESULT_SENTINEL = "MATH_AUDIT_VERIFICATION_RESULT_JSON="
VERIFICATION_RESULT_SCHEMA_VERSION = 1
MAX_VERIFICATION_REPLACEMENT_CODE_CHARS = 200000
VERIFICATION_EXECUTION_STATUSES = {
    "completed",
    "runtime_error",
    "timeout",
    "parse_error",
    "unsafe",
    "skipped",
    "not_run",
}
VERIFICATION_MATHEMATICAL_OUTCOMES = {
    "counterexample_found",
    "claim_failed",
    "no_counterexample_found",
    "check_satisfied",
    "diagnostic_only",
    "inconclusive",
    "not_reported",
}
NEGATIVE_VERIFICATION_OUTCOMES = {"counterexample_found", "claim_failed"}
TECHNICAL_VERIFICATION_FAILURE_STATUSES = {"runtime_error", "timeout", "parse_error"}
VERIFICATION_FINDING_RECHECK_OUTCOMES = {
    "counterexample_confirmed",
    "claim_failure_confirmed",
    "script_error",
    "scope_or_hypothesis_mismatch",
    "notation_or_interpretation_mismatch",
    "inconclusive",
}
CONCLUSIVE_VERIFICATION_FINDING_RECHECK_OUTCOMES = (
    VERIFICATION_FINDING_RECHECK_OUTCOMES - {"inconclusive"}
)
VERIFICATION_FINDING_RECHECK_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "integer", "enum": [1]},
        "finding_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        },
        "chunk_id": {"type": "string"},
        "recheck_outcome": {
            "type": "string",
            "enum": sorted(VERIFICATION_FINDING_RECHECK_OUTCOMES),
        },
        "script_assessment": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "implements_intended_quantity": {"type": ["boolean", "null"]},
                "script_correct": {"type": ["boolean", "null"]},
                "error_type": {"type": "string"},
                "error_explanation": {"type": "string"},
                "affected_lines": {"type": "array", "items": {"type": "string"}},
                "limitations": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "implements_intended_quantity",
                "script_correct",
                "error_type",
                "error_explanation",
                "affected_lines",
                "limitations",
            ],
        },
        "mathematical_assessment": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "candidate_satisfies_hypotheses": {"type": ["boolean", "null"]},
                "counterexample_valid": {"type": ["boolean", "null"]},
                "claim_contradicted": {"type": ["boolean", "null"]},
                "affected_claim": {"type": "string"},
                "explanation": {"type": "string"},
                "exact_computation": {"type": "string"},
            },
            "required": [
                "candidate_satisfies_hypotheses",
                "counterexample_valid",
                "claim_contradicted",
                "affected_claim",
                "explanation",
                "exact_computation",
            ],
        },
        "recommended_issue_action": {
            "type": "string",
            "enum": [
                "strengthen_existing",
                "link_existing",
                "create_new",
                "no_issue_change",
                "human_review",
            ],
        },
        "linked_issue_ids": {"type": "array", "items": {"type": "string"}},
        "recommended_severity": {
            "type": "string",
            "enum": ["none", "low", "medium", "high", "critical"],
        },
        "proposed_issue": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "location": {"type": "string"},
                "description": {"type": "string"},
                "evidence": {"type": "string"},
                "proposed_fix": {"type": "string"},
            },
            "required": ["title", "location", "description", "evidence", "proposed_fix"],
        },
        "downstream_dependencies": {"type": "array", "items": {"type": "string"}},
        "replacement_recommended": {"type": "boolean"},
        "replacement_unavailable_explanation": {"type": "string"},
        "replacement_checks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "check_id": {"type": "string"},
                    "relationship_to_original": {
                        "type": "string",
                        "enum": ["corrected_implementation", "independent_implementation"],
                    },
                    "purpose": {"type": "string"},
                    "correction_explanation": {"type": "string"},
                    "independence_note": {"type": "string"},
                    "python_code": {"type": "string"},
                    "expected_check_kind": {"type": "string"},
                    "tested_scope": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "description": {"type": "string"},
                            "parameters": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "name": {"type": "string"},
                                        "value": {"type": "string"},
                                    },
                                    "required": ["name", "value"],
                                },
                            },
                        },
                        "required": ["description", "parameters"],
                    },
                },
                "required": [
                    "check_id",
                    "relationship_to_original",
                    "purpose",
                    "correction_explanation",
                    "independence_note",
                    "python_code",
                    "expected_check_kind",
                    "tested_scope",
                ],
            },
        },
        "independent_check_recommended": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "needs_human_review": {"type": "boolean"},
        "summary": {"type": "string"},
    },
    "required": [
        "schema_version",
        "finding_ids",
        "chunk_id",
        "recheck_outcome",
        "script_assessment",
        "mathematical_assessment",
        "recommended_issue_action",
        "linked_issue_ids",
        "recommended_severity",
        "proposed_issue",
        "downstream_dependencies",
        "replacement_recommended",
        "replacement_unavailable_explanation",
        "replacement_checks",
        "independent_check_recommended",
        "confidence",
        "needs_human_review",
        "summary",
    ],
}
_LEGACY_FAILURE_LIST_RE = re.compile(
    r"^\s*(?P<label>[^:\n]{0,160}?\b(?:counterexamples|(?:finite\s+)?failures|failed\s+cases))\s*:\s*(?P<value>\[.*\])\s*$",
    flags=re.IGNORECASE,
)
_TARGET_REFERENCE_RE = re.compile(
    r"\b(?P<kind>Theorem|Lemma|Proposition|Corollary|Equation|Identity|Claim)\s+(?P<label>[A-Za-z0-9.()_-]+)",
    flags=re.IGNORECASE,
)
_TESTED_RANGE_RE = re.compile(
    r"(?P<minimum>-?\d+)\s*(?:<=|≤|\\leq?|\\le)\s*(?P<variable>[A-Za-z][A-Za-z0-9_]*)\s*(?:<=|≤|\\leq?|\\le)\s*(?P<maximum>-?\d+)"
)
_DANGEROUS_VERIFICATION_IMPORTS = {"subprocess", "socket", "requests", "urllib", "http", "ftplib", "telnetlib"}
_DANGEROUS_VERIFICATION_SIMPLE_CALLS = {"eval", "exec", "compile", "__import__"}
_DANGEROUS_VERIFICATION_FULL_CALLS = {
    "os.system", "os.remove", "os.unlink", "os.rmdir", "os.removedirs",
    "shutil.rmtree", "shutil.move", "shutil.copy", "shutil.copy2", "shutil.copytree",
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
}
_DANGEROUS_VERIFICATION_ATTR_NAMES = {
    "write_text", "write_bytes", "unlink", "rmdir", "rename", "replace",
    "touch", "mkdir", "chmod", "symlink_to", "hardlink_to",
}


def load_verification_state(session: dict[str, Any]) -> dict[str, Any]:
    path = session_paths(session["workdir"])["verification_state"]
    if path.exists():
        state = load_json(path)
        if isinstance(state, dict):
            return state
    return {
        "updated_at": None,
        "last_run": None,
        "results": [],
        "report_paths": {},
    }


def save_verification_state(session: dict[str, Any], state: dict[str, Any]) -> None:
    path = session_paths(session["workdir"])["verification_state"]
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now()
    save_json(path, state)


def _verification_results_path(session: dict[str, Any]) -> Path:
    return Path(session["workdir"]) / "verification_results"


def _ensure_verification_results_dir(session: dict[str, Any]) -> Path:
    path = _verification_results_path(session)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _verification_result_path(session: dict[str, Any], script_name: str) -> Path:
    return _ensure_verification_results_dir(session) / f"{Path(script_name).stem}.result.json"


def _chunk_id_from_script_name(name: str) -> str:
    m = _VERIFICATION_SCRIPT_RE.match(name or "")
    return m.group(1) if m else ""


def _chunk_index_from_chunk_id(chunk_id: str) -> int:
    m = re.match(r"^chunk_(\d+)$", chunk_id or "")
    return int(m.group(1)) if m else 10**9


def _resolve_verification_script_path(session: dict[str, Any], raw_path: str | Path | None) -> Optional[Path]:
    if not raw_path:
        return None
    root = Path(session["workdir"])
    raw = Path(str(raw_path))
    candidates = []
    if raw.name:
        candidates.append(root / "python_checks" / raw.name)
    if not raw.is_absolute():
        candidates.append(root / raw)
    candidates.append(raw)
    seen = set()
    for cand in candidates:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        if cand.exists():
            return cand.resolve()
    return None


def _read_chunk_records_for_verification(session: dict[str, Any]) -> list[dict[str, Any]]:
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


def _collect_verification_scripts(session: dict[str, Any], only_script_names: Optional[set[str]] = None) -> list[dict[str, Any]]:
    root = Path(session["workdir"])
    scripts: dict[str, dict[str, Any]] = {}
    for rec in _read_chunk_records_for_verification(session):
        chunk_id = str(rec.get("chunk_id") or "")
        chunk_index = int(rec.get("chunk_index") or _chunk_index_from_chunk_id(chunk_id))
        checks: list[dict[str, Any]] = []
        structured_path = rec.get("structured_response_path")
        if structured_path:
            try:
                structured = load_json(structured_path)
                audit_payload = structured.get("audit") if isinstance(structured, dict) else None
                if not isinstance(audit_payload, dict) and isinstance(structured, dict):
                    audit_payload = structured
                raw_checks = audit_payload.get("python_checks", []) if isinstance(audit_payload, dict) else []
                checks = [item for item in raw_checks if isinstance(item, dict)]
            except Exception:
                checks = []
        for check_index, raw_path in enumerate(rec.get("python_paths", []) or []):
            script_name = Path(str(raw_path)).name
            if only_script_names and script_name not in only_script_names:
                continue
            entry = scripts.get(script_name, {
                "chunk_id": chunk_id,
                "chunk_index": chunk_index,
                "script_name": script_name,
                "script_path": None,
            })
            resolved = _resolve_verification_script_path(session, raw_path)
            if resolved is not None:
                entry["script_path"] = str(resolved)
            if check_index < len(checks):
                check = checks[check_index]
                entry["purpose"] = str(check.get("purpose") or "").strip()
                entry["description"] = str(check.get("description") or "").strip()
                entry["expected_outcome"] = str(check.get("expected_outcome") or "").strip()
            scripts[script_name] = entry
    current_dir = root / "python_checks"
    if current_dir.exists():
        for path in sorted(current_dir.glob("*.py")):
            if only_script_names and path.name not in only_script_names:
                continue
            inferred_chunk_id = _chunk_id_from_script_name(path.name)
            entry = scripts.get(path.name, {
                "chunk_id": inferred_chunk_id,
                "chunk_index": _chunk_index_from_chunk_id(inferred_chunk_id),
                "script_name": path.name,
                "script_path": None,
            })
            entry["script_path"] = str(path.resolve())
            scripts[path.name] = entry
    out = list(scripts.values())
    out.sort(key=lambda item: (int(item.get("chunk_index", 10**9)), item.get("script_name", "")))
    return out


def _emit_verification_progress(
    progress_callback: Optional[Callable[[dict[str, Any]], None]],
    event: str,
    **payload: Any,
) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback({"event": event, **payload})
    except Exception:
        # Progress reporting must never alter verification behavior.
        return


def _ast_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _ast_call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _open_call_looks_dangerous(node: ast.Call) -> bool:
    mode_node = None
    if len(node.args) >= 2:
        mode_node = node.args[1]
    else:
        for kw in node.keywords:
            if kw.arg == "mode":
                mode_node = kw.value
                break
    if mode_node is None:
        return False
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return any(ch in mode_node.value for ch in ("w", "a", "x", "+"))
    return True


def _is_string_literal_node(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_int_literal_node(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool)


class _VerificationSafetyVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.reason = ""
        self._string_vars: list[set[str]] = [set()]

    def fail(self, reason: str) -> None:
        if not self.reason:
            self.reason = reason

    def visit(self, node: ast.AST) -> Any:
        if self.reason:
            return None
        return super().visit(node)

    @property
    def known_string_vars(self) -> set[str]:
        return self._string_vars[-1]

    def _target_names(self, target: ast.AST) -> list[str]:
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.Tuple, ast.List)):
            names: list[str] = []
            for item in target.elts:
                names.extend(self._target_names(item))
            return names
        return []

    def _mark_assignment_targets(self, targets: list[ast.AST], is_known_string: bool) -> None:
        for target in targets:
            for name in self._target_names(target):
                if is_known_string:
                    self.known_string_vars.add(name)
                else:
                    self.known_string_vars.discard(name)

    def _is_safe_string_receiver(self, node: ast.AST) -> bool:
        if _is_string_literal_node(node):
            return True
        if isinstance(node, ast.Name):
            return node.id in self.known_string_vars
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # Common cosmetic formatter pattern: " + ".join(parts).replace("+ -", "- ").
            return (
                node.func.attr == "join"
                and _is_string_literal_node(node.func.value)
                and len(node.args) == 1
                and not node.keywords
            )
        return False

    def _is_safe_string_replace_call(self, node: ast.Call) -> bool:
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "replace":
            return False
        if node.keywords:
            return False
        if len(node.args) not in {2, 3}:
            return False
        if not self._is_safe_string_receiver(node.func.value):
            return False
        if not all(_is_string_literal_node(arg) for arg in node.args[:2]):
            return False
        if len(node.args) == 3 and not _is_int_literal_node(node.args[2]):
            return False
        return True

    def _is_known_string_expression(self, node: ast.AST) -> bool:
        return self._is_safe_string_receiver(node) or (
            isinstance(node, ast.Call) and self._is_safe_string_replace_call(node)
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root_name = (alias.name or "").split(".", 1)[0]
            if root_name in _DANGEROUS_VERIFICATION_IMPORTS:
                self.fail(f"import of {root_name} is not allowed in safe_only mode")
                return

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root_name = ((node.module or "").split(".", 1)[0])
        if root_name in _DANGEROUS_VERIFICATION_IMPORTS:
            self.fail(f"import of {root_name} is not allowed in safe_only mode")

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        if self.reason:
            return
        self._mark_assignment_targets(list(node.targets), self._is_known_string_expression(node.value))

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            if self.reason:
                return
        self._mark_assignment_targets([node.target], bool(node.value is not None and self._is_known_string_expression(node.value)))

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._mark_assignment_targets([node.target], False)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._mark_assignment_targets([node.target], False)
        for item in node.body:
            self.visit(item)
        for item in node.orelse:
            self.visit(item)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._string_vars.append(set())
        try:
            for item in node.decorator_list:
                self.visit(item)
            for item in list(node.args.defaults) + [kw for kw in node.args.kw_defaults if kw is not None]:
                self.visit(item)
            if node.returns is not None:
                self.visit(node.returns)
            for item in node.body:
                self.visit(item)
        finally:
            self._string_vars.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._string_vars.append(set())
        try:
            for item in list(node.args.defaults) + [kw for kw in node.args.kw_defaults if kw is not None]:
                self.visit(item)
            self.visit(node.body)
        finally:
            self._string_vars.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for item in node.decorator_list:
            self.visit(item)
        for item in node.bases:
            self.visit(item)
        for item in node.keywords:
            self.visit(item)
        self._string_vars.append(set())
        try:
            for item in node.body:
                self.visit(item)
        finally:
            self._string_vars.pop()

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _ast_call_name(node.func)
        attr_name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if call_name in {"open", "builtins.open"} and _open_call_looks_dangerous(node):
            self.fail("write-capable open() call is not allowed in safe_only mode")
            return
        if call_name in _DANGEROUS_VERIFICATION_SIMPLE_CALLS:
            self.fail(f"call to {call_name} is not allowed in safe_only mode")
            return
        if call_name in _DANGEROUS_VERIFICATION_FULL_CALLS:
            self.fail(f"call to {call_name} is not allowed in safe_only mode")
            return
        if attr_name in _DANGEROUS_VERIFICATION_ATTR_NAMES:
            if not (attr_name == "replace" and self._is_safe_string_replace_call(node)):
                self.fail(f"call to {attr_name}() is not allowed in safe_only mode")
                return
        if call_name.startswith("socket.") or call_name.startswith("subprocess."):
            self.fail(f"call to {call_name} is not allowed in safe_only mode")
            return
        self.generic_visit(node)


def _check_verification_script_safety(script_path: Path) -> tuple[bool, str]:
    try:
        source = script_path.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"could not read script: {e}"
    try:
        tree = ast.parse(source, filename=str(script_path))
    except Exception as e:
        return False, f"could not parse script: {e}"
    visitor = _VerificationSafetyVisitor()
    visitor.visit(tree)
    if visitor.reason:
        return False, visitor.reason
    return True, ""


def _first_nonempty_line(text: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s
    return ""


def _truncate_text(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n..."


def _target_from_text(*values: Any) -> dict[str, str]:
    for value in values:
        match = _TARGET_REFERENCE_RE.search(str(value or ""))
        if match:
            kind = match.group("kind").title()
            label = match.group("label")
            return {"kind": kind.lower(), "label": f"{kind} {label}"}
    return {}


def _tested_range_from_text(*values: Any) -> Optional[dict[str, Any]]:
    for value in values:
        match = _TESTED_RANGE_RE.search(str(value or ""))
        if match:
            return {
                "variable": match.group("variable"),
                "minimum": int(match.group("minimum")),
                "maximum": int(match.group("maximum")),
                "source": "text_inference",
            }
    return None


def _verification_metadata_from_entry(entry: Optional[dict[str, Any]]) -> dict[str, Any]:
    entry = entry if isinstance(entry, dict) else {}
    target = _target_from_text(entry.get("purpose"), entry.get("description"), entry.get("expected_outcome"))
    return {
        "purpose": str(entry.get("purpose") or "").strip(),
        "description": str(entry.get("description") or "").strip(),
        "expected_outcome": str(entry.get("expected_outcome") or "").strip(),
        "target": target,
    }


def _validate_structured_verification_payload(payload: Any) -> tuple[Optional[dict[str, Any]], str]:
    if not isinstance(payload, dict):
        return None, "verification result sentinel must contain a JSON object"
    required = {"schema_version", "check_kind", "outcome", "summary"}
    missing = sorted(required - set(payload))
    if missing:
        return None, "verification result is missing required field(s): " + ", ".join(missing)
    if payload.get("schema_version") != VERIFICATION_RESULT_SCHEMA_VERSION:
        return None, f"unsupported verification result schema_version: {payload.get('schema_version')!r}"
    check_kind = str(payload.get("check_kind") or "").strip()
    if not check_kind:
        return None, "verification result check_kind must be nonempty"
    outcome = str(payload.get("outcome") or "").strip().lower()
    if outcome not in VERIFICATION_MATHEMATICAL_OUTCOMES:
        return None, f"unsupported mathematical outcome: {outcome or '(empty)'}"
    if not isinstance(payload.get("summary"), str):
        return None, "verification result summary must be a string"
    for list_key in ("counterexamples", "failed_cases", "linked_issue_ids"):
        if list_key in payload and not isinstance(payload.get(list_key), list):
            return None, f"verification result {list_key} must be a list"
    if outcome in NEGATIVE_VERIFICATION_OUTCOMES:
        cases = list(payload.get("counterexamples") or []) + list(payload.get("failed_cases") or [])
        if not cases:
            return None, f"{outcome} requires a nonempty counterexamples or failed_cases list"
    normalized = dict(payload)
    normalized["check_kind"] = check_kind
    normalized["outcome"] = outcome
    normalized["summary"] = payload.get("summary", "").strip()
    normalized.setdefault("counterexamples", [])
    normalized.setdefault("failed_cases", [])
    normalized.setdefault("linked_issue_ids", [])
    normalized.setdefault("tested_range", payload.get("tested_scope"))
    normalized.setdefault("target", {})
    return normalized, ""


def _parse_verification_result_sentinel(stdout: str) -> tuple[Optional[dict[str, Any]], str, int]:
    sentinel_values = [
        line.split(VERIFICATION_RESULT_SENTINEL, 1)[1].strip()
        for line in (stdout or "").splitlines()
        if line.startswith(VERIFICATION_RESULT_SENTINEL)
    ]
    if not sentinel_values:
        return None, "", 0
    if len(sentinel_values) != 1:
        return None, f"expected exactly one verification result sentinel, found {len(sentinel_values)}", len(sentinel_values)
    try:
        payload = json.loads(sentinel_values[0])
    except Exception as exc:
        return None, f"could not parse verification result sentinel JSON: {type(exc).__name__}: {exc}", 1
    payload, error = _validate_structured_verification_payload(payload)
    return payload, error, 1


def _json_safe_legacy_case(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_safe_legacy_case(item) for item in value]
    if isinstance(value, list):
        return [_json_safe_legacy_case(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe_legacy_case(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _normalize_legacy_counterexample(value: Any, label: str) -> Any:
    safe = _json_safe_legacy_case(value)
    if (
        "theorem 3" in label.lower()
        and isinstance(safe, list)
        and len(safe) >= 2
        and isinstance(safe[0], int)
        and isinstance(safe[1], int)
    ):
        item: dict[str, Any] = {
            "a": safe[0],
            "omega_sigma": safe[1],
            "omega_input": safe[0],
            "legacy_values": safe,
        }
        if len(safe) >= 3 and isinstance(safe[2], dict):
            item["factorization"] = safe[2]
        return item
    return {"legacy_values": safe}


def _parse_legacy_verification_stdout(stdout: str) -> Optional[dict[str, Any]]:
    parsed_records: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        match = _LEGACY_FAILURE_LIST_RE.match(line)
        if not match:
            continue
        try:
            value = ast.literal_eval(match.group("value"))
        except (SyntaxError, ValueError):
            continue
        if not isinstance(value, (list, tuple)):
            continue
        label = match.group("label").strip()
        parsed_records.append({"label": label, "cases": list(value)})
    if not parsed_records:
        return None
    nonempty = [record for record in parsed_records if record["cases"]]
    if nonempty:
        counterexamples = [
            _normalize_legacy_counterexample(case, record["label"])
            for record in nonempty
            for case in record["cases"]
        ]
        labels = "; ".join(record["label"] for record in nonempty)
        target = _target_from_text(*(record["label"] for record in nonempty))
        return {
            "schema_version": 0,
            "check_kind": "legacy_failure_list",
            "outcome": "counterexample_found",
            "summary": f"Legacy verification output reported nonempty failure data: {labels}.",
            "counterexamples": counterexamples,
            "failed_cases": [],
            "target": target,
            "tested_range": None,
            "linked_issue_ids": [],
            "legacy_records": [
                {"label": record["label"], "cases": _json_safe_legacy_case(record["cases"])}
                for record in parsed_records
            ],
        }
    labels = "; ".join(record["label"] for record in parsed_records)
    return {
        "schema_version": 0,
        "check_kind": "legacy_failure_list",
        "outcome": "no_counterexample_found",
        "summary": f"Legacy verification output reported empty failure lists: {labels}. This is limited to the tested scope.",
        "counterexamples": [],
        "failed_cases": [],
        "target": _target_from_text(*(record["label"] for record in parsed_records)),
        "tested_range": None,
        "linked_issue_ids": [],
        "legacy_records": [
            {"label": record["label"], "cases": []}
            for record in parsed_records
        ],
    }


def _legacy_execution_status(status: Any, skip_reason: str = "") -> str:
    status = str(status or "").strip().lower()
    if status in VERIFICATION_EXECUTION_STATUSES:
        return status
    if status == "passed":
        return "completed"
    if status == "failed":
        return "runtime_error"
    if status == "timeout":
        return "timeout"
    if status == "skipped":
        return "unsafe" if skip_reason else "skipped"
    return "not_run"


def _normalize_verification_result(
    raw_result: dict[str, Any],
    script_entry: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    result = dict(raw_result or {})
    metadata = _verification_metadata_from_entry(script_entry)
    for key, value in metadata.items():
        if value and not result.get(key):
            result[key] = value
    execution_status = str(result.get("execution_status") or "").strip().lower()
    if execution_status not in VERIFICATION_EXECUTION_STATUSES:
        execution_status = _legacy_execution_status(result.get("status"), str(result.get("skip_reason") or ""))
        if execution_status == "not_run" and result.get("returncode") is not None:
            try:
                execution_status = "completed" if int(result.get("returncode")) == 0 else "runtime_error"
            except (TypeError, ValueError):
                execution_status = "not_run"
    result["execution_status"] = execution_status

    existing_outcome = str(result.get("mathematical_outcome") or "").strip().lower()
    if existing_outcome in VERIFICATION_MATHEMATICAL_OUTCOMES:
        result.setdefault("outcome_source", "structured_result" if result.get("structured_result") else "saved_result")
        return result

    if execution_status == "completed":
        payload, parse_error, sentinel_count = _parse_verification_result_sentinel(str(result.get("stdout") or ""))
        result["sentinel_count"] = sentinel_count
        if parse_error:
            result["execution_status"] = "parse_error"
            result["mathematical_outcome"] = "not_reported"
            result["outcome_source"] = "malformed_structured_result"
            result["outcome_parse_error"] = parse_error
        elif payload is not None:
            result["structured_result"] = payload
            result["mathematical_outcome"] = payload["outcome"]
            result["outcome_source"] = "structured_sentinel"
            result["counterexamples"] = list(payload.get("counterexamples") or [])
            result["failed_cases"] = list(payload.get("failed_cases") or [])
            result["tested_range"] = payload.get("tested_range")
            result["target"] = payload.get("target") or result.get("target") or {}
            result["linked_issue_ids"] = list(payload.get("linked_issue_ids") or [])
            if payload.get("summary"):
                result["conclusion"] = payload["summary"]
        else:
            legacy = _parse_legacy_verification_stdout(str(result.get("stdout") or ""))
            if legacy is not None:
                result["structured_result"] = legacy
                result["mathematical_outcome"] = legacy["outcome"]
                result["outcome_source"] = "legacy_stdout_inference"
                result["counterexamples"] = list(legacy.get("counterexamples") or [])
                result["failed_cases"] = list(legacy.get("failed_cases") or [])
                result["target"] = legacy.get("target") or result.get("target") or {}
                result["linked_issue_ids"] = list(legacy.get("linked_issue_ids") or [])
                if legacy.get("summary"):
                    result["conclusion"] = legacy["summary"]
            else:
                result["mathematical_outcome"] = "not_reported"
                result["outcome_source"] = "no_structured_result"
    elif execution_status in {"runtime_error", "timeout", "parse_error"}:
        result["mathematical_outcome"] = "inconclusive"
        result["outcome_source"] = "execution_incomplete"
    else:
        result["mathematical_outcome"] = "not_reported"
        result["outcome_source"] = "not_executed"

    result.setdefault("counterexamples", [])
    result.setdefault("failed_cases", [])
    result.setdefault("linked_issue_ids", [])
    result.setdefault("target", metadata.get("target") or {})
    if not result.get("tested_range"):
        result["tested_range"] = _tested_range_from_text(
            result.get("stdout"),
            result.get("description"),
            result.get("expected_outcome"),
        )
    return result


def _legacy_status_for_execution(execution_status: str) -> str:
    if execution_status == "completed":
        return "passed"
    if execution_status == "timeout":
        return "timeout"
    if execution_status in {"runtime_error", "parse_error"}:
        return "failed"
    return "skipped"


def _infer_verification_conclusion(result: dict[str, Any]) -> str:
    result = _normalize_verification_result(result)
    outcome = result.get("mathematical_outcome")
    structured = result.get("structured_result") if isinstance(result.get("structured_result"), dict) else {}
    if structured.get("summary"):
        return str(structured["summary"])
    if result.get("outcome_parse_error"):
        return str(result.get("outcome_parse_error"))
    status = result.get("status")
    stdout_line = _first_nonempty_line(result.get("stdout", ""))
    stderr_line = _first_nonempty_line(result.get("stderr", ""))
    if status == "passed":
        return stdout_line or "Script completed successfully."
    if status == "failed":
        return stderr_line or stdout_line or f"Script exited with code {result.get('returncode')}"
    if status == "timeout":
        return f"Script exceeded timeout of {result.get('timeout_seconds')} seconds."
    if status == "skipped":
        return result.get("skip_reason") or "Script was skipped."
    return stdout_line or stderr_line or "No conclusion available."


def _verification_summary_counts(results: list[dict[str, Any]]) -> dict[str, Any]:
    execution = {key: 0 for key in sorted(VERIFICATION_EXECUTION_STATUSES)}
    outcomes = {key: 0 for key in sorted(VERIFICATION_MATHEMATICAL_OUTCOMES)}
    for raw_result in results:
        result = _normalize_verification_result(raw_result)
        execution[result["execution_status"]] += 1
        outcomes[result["mathematical_outcome"]] += 1
    counts: dict[str, Any] = {
        "scripts_total": len(results),
        "execution_summary": execution,
        "mathematical_outcome_summary": outcomes,
        "negative_mathematical_findings": sum(outcomes[key] for key in NEGATIVE_VERIFICATION_OUTCOMES),
        "mathematically_unresolved": outcomes["diagnostic_only"] + outcomes["inconclusive"] + outcomes["not_reported"],
        "all_mathematical_checks_satisfied": bool(results)
        and not sum(outcomes[key] for key in NEGATIVE_VERIFICATION_OUTCOMES)
        and not (outcomes["diagnostic_only"] + outcomes["inconclusive"] + outcomes["not_reported"]),
        # Backward-compatible execution aliases. User-facing output must not call these mathematical pass/fail counts.
        "passed": execution["completed"],
        "failed": execution["runtime_error"] + execution["parse_error"],
        "timeout": execution["timeout"],
        "skipped": execution["skipped"] + execution["unsafe"] + execution["not_run"],
    }
    for key, value in execution.items():
        counts[f"execution_{key}"] = value
    for key, value in outcomes.items():
        counts[f"outcome_{key}"] = value
    return counts


def _verification_findings_path(session: dict[str, Any]) -> Path:
    return session_paths(session["workdir"])["verification_findings"]


def _verification_target_label(result: dict[str, Any]) -> str:
    target = result.get("target")
    if isinstance(target, dict):
        return str(target.get("label") or target.get("name") or "").strip()
    return str(target or "").strip()


def _matching_issue_ids_for_verification(session: dict[str, Any], result: dict[str, Any]) -> list[str]:
    explicit = [str(item).strip() for item in (result.get("linked_issue_ids") or []) if str(item).strip()]
    try:
        issues = [item for item in (load_issues(session).get("issues") or []) if isinstance(item, dict)]
    except Exception:
        return explicit
    valid_ids = {str(item.get("issue_id") or "").strip() for item in issues}
    explicit = [item for item in explicit if item in valid_ids]
    if explicit:
        return explicit

    target = _verification_target_label(result).lower()
    purpose = str(result.get("purpose") or "").lower()
    chunk_id = str(result.get("chunk_id") or "").strip()
    scored: list[tuple[int, str]] = []
    for issue in issues:
        issue_id = str(issue.get("issue_id") or "").strip()
        if not issue_id:
            continue
        title = str(issue.get("title") or "").lower()
        location = str(issue.get("location") or "").lower()
        description = str(issue.get("description") or "").lower()
        score = 0
        if target:
            score += 4 if target in title else 0
            score += 3 if target in location else 0
            score += 1 if target in description else 0
        if chunk_id and str(issue.get("chunk_id") or "").strip() == chunk_id:
            score += 1
        for word in ("finite", "counterexample", "computation", "check"):
            if word in purpose and word in title:
                score += 2
        if score >= 5:
            scored.append((score, issue_id))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return sorted(issue_id for score, issue_id in scored if score == best)


def _verification_finding_from_result(session: dict[str, Any], raw_result: dict[str, Any]) -> Optional[dict[str, Any]]:
    result = _normalize_verification_result(raw_result)
    outcome = str(result.get("mathematical_outcome") or "")
    if outcome not in NEGATIVE_VERIFICATION_OUTCOMES:
        return None
    script_name = str(result.get("script_name") or "verification script").strip()
    chunk_id = str(result.get("chunk_id") or "").strip()
    target_label = _verification_target_label(result)
    counterexamples = list(result.get("counterexamples") or [])
    failed_cases = list(result.get("failed_cases") or [])
    identity_material = json.dumps(
        {
            "script_name": script_name,
            "chunk_id": chunk_id,
            "outcome": outcome,
            "target": target_label,
            "counterexamples": counterexamples,
            "failed_cases": failed_cases,
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    finding_id = "VF-" + hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:12].upper()
    title_target = target_label or "a checked claim"
    title = (
        f"Counterexample found for {title_target}"
        if outcome == "counterexample_found"
        else f"Verification check reports failure of {title_target}"
    )
    summary = str(result.get("conclusion") or "").strip()
    evidence_parts = [
        f"{script_name} executed with status {result.get('execution_status')} and reported outcome {outcome}.",
    ]
    if summary:
        evidence_parts.append(summary)
    if counterexamples:
        preview = counterexamples[:5]
        suffix = f" (showing 5 of {len(counterexamples)})" if len(counterexamples) > 5 else ""
        evidence_parts.append(
            "Structured counterexamples"
            + suffix
            + ": "
            + json.dumps(preview, ensure_ascii=False, sort_keys=True)
        )
    if failed_cases:
        preview = failed_cases[:5]
        suffix = f" (showing 5 of {len(failed_cases)})" if len(failed_cases) > 5 else ""
        evidence_parts.append(
            "Structured failed cases"
            + suffix
            + ": "
            + json.dumps(preview, ensure_ascii=False, sort_keys=True)
        )
    matched_issue_ids = _matching_issue_ids_for_verification(session, result)
    return {
        "finding_id": finding_id,
        "source": "verification",
        "verification_derived": True,
        "provisional": True,
        "active": True,
        "severity": "high",
        "title": title,
        "description": (
            f"A local verification check produced a negative mathematical outcome for {title_target}. "
            "The result requires human mathematical review and must not be treated as a mere execution failure."
        ),
        "evidence": " ".join(evidence_parts),
        "recommended_action": "Check the script and mathematical claim independently; if confirmed, correct the claim and all dependent arguments.",
        "script_name": script_name,
        "script_path": str(result.get("script_path") or ""),
        "result_path": str(result.get("result_path") or ""),
        "chunk_id": chunk_id,
        "target": result.get("target") or {},
        "location": target_label or chunk_id or "verification output",
        "execution_status": result.get("execution_status"),
        "mathematical_outcome": outcome,
        "outcome_source": result.get("outcome_source"),
        "check_kind": (result.get("structured_result") or {}).get("check_kind") if isinstance(result.get("structured_result"), dict) else "",
        "summary": summary,
        "counterexamples": counterexamples,
        "failed_cases": failed_cases,
        "tested_range": result.get("tested_range"),
        "linked_issue_ids": list(result.get("linked_issue_ids") or []),
        "matched_issue_ids": matched_issue_ids,
    }


def _verification_finding_rechecks_path(session: dict[str, Any]) -> Path:
    return session_paths(session["workdir"])["verification_finding_rechecks"]


def append_verification_finding_recheck_event(
    session: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(event or {})
    payload.setdefault("schema_version", 1)
    payload.setdefault("time", utc_now())
    append_jsonl(_verification_finding_rechecks_path(session), payload)
    return payload


def verification_finding_rechecks_for_session(
    session: dict[str, Any],
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    path = _verification_finding_rechecks_path(session)
    if not path.exists():
        return []
    collapsed: dict[str, dict[str, Any]] = {}
    order: dict[str, int] = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if not isinstance(event, dict):
                    continue
                recheck_id = str(event.get("recheck_id") or "").strip()
                if not recheck_id:
                    continue
                merged = dict(collapsed.get(recheck_id) or {})
                merged.update(event)
                collapsed[recheck_id] = merged
                order[recheck_id] = index
    except OSError:
        return []

    records = sorted(
        collapsed.values(),
        key=lambda item: (str(item.get("time") or ""), order.get(str(item.get("recheck_id") or ""), 0)),
    )
    if include_superseded:
        return records

    latest_by_finding: dict[str, dict[str, Any]] = {}
    for record in records:
        if str(record.get("status") or "").strip().lower() != "completed":
            continue
        if record.get("canonical") is False:
            continue
        result = record.get("structured_result")
        if not isinstance(result, dict):
            continue
        try:
            result = validate_verification_finding_recheck_response(
                _backfill_verification_finding_recheck_result(result)
            )
        except (TypeError, ValueError):
            continue
        record = dict(record)
        record["structured_result"] = result
        for finding_id in record.get("finding_ids") or result.get("finding_ids") or []:
            clean = str(finding_id or "").strip()
            if clean:
                latest_by_finding[clean] = record
    unique: dict[str, dict[str, Any]] = {}
    for record in latest_by_finding.values():
        unique[str(record.get("recheck_id") or "")] = record
    return sorted(unique.values(), key=lambda item: str(item.get("time") or ""))


def _backfill_verification_finding_recheck_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    script_assessment = result.get("script_assessment")
    if isinstance(script_assessment, dict):
        script_assessment = dict(script_assessment)
        script_assessment.setdefault("error_type", "")
        script_assessment.setdefault("error_explanation", "")
        script_assessment.setdefault("affected_lines", [])
        if str(result.get("recheck_outcome") or "") == "script_error":
            script_assessment["error_explanation"] = str(
                script_assessment.get("error_explanation")
                or result.get("summary")
                or "Legacy script-error recheck explanation unavailable."
            )
        result["script_assessment"] = script_assessment
    result.setdefault("replacement_recommended", False)
    result.setdefault("replacement_checks", [])
    result.setdefault("replacement_unavailable_explanation", "")
    if str(result.get("recheck_outcome") or "") == "script_error" and not result.get("replacement_recommended"):
        result["replacement_unavailable_explanation"] = str(
            result.get("replacement_unavailable_explanation")
            or "Legacy recheck predates replacement-script support."
        )
    return result


def verification_finding_recheck_map(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for record in verification_finding_rechecks_for_session(session):
        result = record.get("structured_result") if isinstance(record.get("structured_result"), dict) else {}
        for finding_id in record.get("finding_ids") or result.get("finding_ids") or []:
            clean = str(finding_id or "").strip()
            if clean:
                mapping[clean] = record
    return mapping


def _verification_finding_recheck_display_state(record: Optional[dict[str, Any]]) -> str:
    if not isinstance(record, dict):
        return "not_rechecked"
    result = record.get("structured_result") if isinstance(record.get("structured_result"), dict) else {}
    outcome = str(result.get("recheck_outcome") or "").strip().lower()
    if outcome in {"counterexample_confirmed", "claim_failure_confirmed"}:
        return "confirmed"
    if outcome in {"script_error", "scope_or_hypothesis_mismatch", "notation_or_interpretation_mismatch"}:
        return "challenged"
    if outcome == "inconclusive":
        return "inconclusive"
    return "not_rechecked"


def apply_verification_finding_rechecks(
    session: dict[str, Any],
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    recheck_map = verification_finding_recheck_map(session)
    replacement_groups = {
        str(item.get("recheck_id") or ""): item
        for item in verification_replacement_check_inventory(session).get("groups") or []
        if isinstance(item, dict)
    }
    applied = []
    for finding in findings:
        item = dict(finding)
        finding_id = str(item.get("finding_id") or "").strip()
        latest = recheck_map.get(finding_id)
        state = _verification_finding_recheck_display_state(latest)
        item["recheck_state"] = state
        item["recheck_confirmed"] = state == "confirmed"
        item["recheck_challenged"] = state == "challenged"
        item["recheck_inconclusive"] = state == "inconclusive"
        if latest is not None:
            item["latest_recheck"] = latest
            replacement_state = replacement_groups.get(str(latest.get("recheck_id") or ""))
            if replacement_state is not None:
                item["replacement_check_state"] = replacement_state
        applied.append(item)
    return applied


def verification_finding_recheck_summary(
    session: dict[str, Any],
    findings: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    current = findings if findings is not None else verification_findings_for_session(session)
    states = {"confirmed": 0, "challenged": 0, "inconclusive": 0, "not_rechecked": 0}
    for finding in current:
        state = str(finding.get("recheck_state") or "not_rechecked")
        states[state if state in states else "not_rechecked"] += 1
    return {
        "active_finding_count": len(current),
        "rechecked_finding_count": len(current) - states["not_rechecked"],
        "confirmed_count": states["confirmed"],
        "challenged_count": states["challenged"],
        "inconclusive_count": states["inconclusive"],
        "not_rechecked_count": states["not_rechecked"],
        "canonical_recheck_count": len(verification_finding_rechecks_for_session(session)),
    }


def verification_finding_recheck_schema_errors(schema: Optional[dict[str, Any]] = None) -> list[str]:
    root = VERIFICATION_FINDING_RECHECK_RESPONSE_SCHEMA if schema is None else schema
    errors: list[str] = []

    def visit(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            required = node.get("required")
            if not isinstance(required, list):
                errors.append(f"{path}: object schema is missing required")
            else:
                missing = sorted(set(properties) - set(required))
                extra = sorted(set(required) - set(properties))
                if missing:
                    errors.append(f"{path}: properties missing from required: {', '.join(missing)}")
                if extra:
                    errors.append(f"{path}: required keys missing from properties: {', '.join(extra)}")
            for key, child in properties.items():
                visit(child, f"{path}.{key}")
        items = node.get("items")
        if isinstance(items, dict):
            visit(items, f"{path}[]")

    visit(root, "$")
    return errors


def validate_verification_finding_recheck_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Verification-finding recheck response must be a JSON object.")
    required = set(VERIFICATION_FINDING_RECHECK_RESPONSE_SCHEMA["required"])
    missing = sorted(required - set(payload))
    extra = sorted(set(payload) - set(VERIFICATION_FINDING_RECHECK_RESPONSE_SCHEMA["properties"]))
    if missing:
        raise ValueError("Missing verification-finding recheck fields: " + ", ".join(missing))
    if extra:
        raise ValueError("Unexpected verification-finding recheck fields: " + ", ".join(extra))
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError("Unsupported verification-finding recheck schema_version.")
    outcome = str(payload.get("recheck_outcome") or "").strip()
    if outcome not in VERIFICATION_FINDING_RECHECK_OUTCOMES:
        raise ValueError(f"Unsupported verification-finding recheck outcome: {outcome!r}")
    finding_ids = [str(item).strip() for item in payload.get("finding_ids") or [] if str(item).strip()]
    if not finding_ids:
        raise ValueError("Verification-finding recheck response must contain finding_ids.")
    if not str(payload.get("chunk_id") or "").strip():
        raise ValueError("Verification-finding recheck response must contain chunk_id.")
    for key in ("script_assessment", "mathematical_assessment", "proposed_issue"):
        if not isinstance(payload.get(key), dict):
            raise ValueError(f"Verification-finding recheck field {key!r} must be an object.")
        nested_schema = VERIFICATION_FINDING_RECHECK_RESPONSE_SCHEMA["properties"][key]
        expected = set(nested_schema["required"])
        missing_nested = sorted(expected - set(payload[key]))
        extra_nested = sorted(set(payload[key]) - set(nested_schema["properties"]))
        if missing_nested:
            raise ValueError(f"Verification-finding recheck field {key!r} is missing: {', '.join(missing_nested)}")
        if extra_nested:
            raise ValueError(f"Verification-finding recheck field {key!r} has unexpected keys: {', '.join(extra_nested)}")
    for key in ("finding_ids", "linked_issue_ids", "downstream_dependencies"):
        if not isinstance(payload.get(key), list) or any(not isinstance(item, str) for item in payload[key]):
            raise ValueError(f"Verification-finding recheck field {key!r} must be an array of strings.")
    script_assessment = payload["script_assessment"]
    if not isinstance(script_assessment.get("affected_lines"), list) or any(
        not isinstance(item, str) for item in script_assessment["affected_lines"]
    ):
        raise ValueError("Verification-finding recheck script_assessment.affected_lines must be an array of strings.")
    if not isinstance(script_assessment.get("limitations"), list) or any(
        not isinstance(item, str) for item in script_assessment["limitations"]
    ):
        raise ValueError("Verification-finding recheck script_assessment.limitations must be an array of strings.")
    for key in ("independent_check_recommended", "needs_human_review", "replacement_recommended"):
        if not isinstance(payload.get(key), bool):
            raise ValueError(f"Verification-finding recheck field {key!r} must be boolean.")
    replacement_checks = payload.get("replacement_checks")
    if not isinstance(replacement_checks, list):
        raise ValueError("Verification-finding recheck replacement_checks must be an array.")
    replacement_schema = VERIFICATION_FINDING_RECHECK_RESPONSE_SCHEMA["properties"]["replacement_checks"]["items"]
    replacement_required = set(replacement_schema["required"])
    replacement_properties = set(replacement_schema["properties"])
    seen_check_ids: set[str] = set()
    for index, replacement in enumerate(replacement_checks):
        if not isinstance(replacement, dict):
            raise ValueError(f"Verification-finding replacement check {index} must be an object.")
        missing_replacement = sorted(replacement_required - set(replacement))
        extra_replacement = sorted(set(replacement) - replacement_properties)
        if missing_replacement or extra_replacement:
            raise ValueError(
                f"Verification-finding replacement check {index} has invalid keys; "
                f"missing={missing_replacement}, extra={extra_replacement}."
            )
        check_id = str(replacement.get("check_id") or "").strip()
        if not check_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", check_id):
            raise ValueError(f"Verification-finding replacement check {index} has an invalid check_id.")
        if check_id in seen_check_ids:
            raise ValueError(f"Verification-finding replacement check_id is duplicated: {check_id}")
        seen_check_ids.add(check_id)
        if replacement.get("relationship_to_original") not in {
            "corrected_implementation",
            "independent_implementation",
        }:
            raise ValueError(f"Verification-finding replacement check {check_id} has an invalid relationship.")
        for text_key in (
            "purpose",
            "correction_explanation",
            "independence_note",
            "python_code",
            "expected_check_kind",
        ):
            if not isinstance(replacement.get(text_key), str):
                raise ValueError(f"Verification-finding replacement check {check_id} field {text_key} must be a string.")
            if not str(replacement.get(text_key) or "").strip():
                raise ValueError(f"Verification-finding replacement check {check_id} field {text_key} must be nonempty.")
        tested_scope = replacement.get("tested_scope")
        if not isinstance(tested_scope, dict) or set(tested_scope) != {"description", "parameters"}:
            raise ValueError(f"Verification-finding replacement check {check_id} has invalid tested_scope.")
        if not isinstance(tested_scope.get("description"), str) or not isinstance(tested_scope.get("parameters"), list):
            raise ValueError(f"Verification-finding replacement check {check_id} has invalid tested_scope values.")
        for parameter in tested_scope["parameters"]:
            if (
                not isinstance(parameter, dict)
                or set(parameter) != {"name", "value"}
                or not all(isinstance(parameter.get(field), str) for field in ("name", "value"))
            ):
                raise ValueError(f"Verification-finding replacement check {check_id} has invalid scope parameters.")
    if outcome == "script_error":
        if script_assessment.get("script_correct") is not False:
            raise ValueError("script_error requires script_assessment.script_correct=false.")
        if not str(script_assessment.get("error_explanation") or "").strip():
            raise ValueError("script_error requires a precise script error explanation.")
        if payload.get("replacement_recommended") and not replacement_checks:
            raise ValueError("script_error with replacement_recommended=true requires replacement_checks.")
        if not payload.get("replacement_recommended") and replacement_checks:
            raise ValueError("script_error with replacement_recommended=false cannot include replacement_checks.")
        if not payload.get("replacement_recommended") and not str(
            payload.get("replacement_unavailable_explanation") or ""
        ).strip():
            raise ValueError(
                "script_error without a replacement requires replacement_unavailable_explanation."
            )
    elif replacement_checks or payload.get("replacement_recommended"):
        raise ValueError("Replacement checks are only supported when recheck_outcome=script_error.")
    for key, allowed in (
        ("recommended_issue_action", {"strengthen_existing", "link_existing", "create_new", "no_issue_change", "human_review"}),
        ("recommended_severity", {"none", "low", "medium", "high", "critical"}),
        ("confidence", {"low", "medium", "high"}),
    ):
        if payload.get(key) not in allowed:
            raise ValueError(f"Verification-finding recheck field {key!r} has unsupported value {payload.get(key)!r}.")
    return dict(payload)


def validate_verification_replacement_code(code: Any) -> dict[str, Any]:
    source = str(code or "")
    code_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if not source.strip():
        return {"status": "parse_error", "reason": "replacement script is empty", "code_sha256": code_hash}
    if len(source) > MAX_VERIFICATION_REPLACEMENT_CODE_CHARS:
        return {
            "status": "invalid_contract",
            "reason": (
                f"replacement script exceeds {MAX_VERIFICATION_REPLACEMENT_CODE_CHARS} characters"
            ),
            "code_sha256": code_hash,
        }
    try:
        tree = ast.parse(source, filename="<verification replacement>")
    except Exception as exc:
        return {
            "status": "parse_error",
            "reason": f"could not parse replacement script: {type(exc).__name__}: {exc}",
            "code_sha256": code_hash,
        }
    visitor = _VerificationSafetyVisitor()
    visitor.visit(tree)
    if visitor.reason:
        return {"status": "unsafe", "reason": visitor.reason, "code_sha256": code_hash}
    sentinel_mentions = source.count(VERIFICATION_RESULT_SENTINEL)
    if sentinel_mentions != 1:
        return {
            "status": "invalid_contract",
            "reason": (
                "replacement script must contain exactly one structured-result sentinel marker; "
                f"found {sentinel_mentions}"
            ),
            "code_sha256": code_hash,
        }
    return {"status": "ready", "reason": "", "code_sha256": code_hash}


def _safe_replacement_check_id(value: Any) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "").strip()).strip("_")
    return clean[:80] or "replacement"


def persist_verification_replacement_proposals(
    session: dict[str, Any],
    *,
    recheck_id: str,
    artifact_root: str | Path,
    structured_result: dict[str, Any],
    evidence: dict[str, Any],
    model: str,
    reasoning_effort: str,
) -> dict[str, Any]:
    if str(structured_result.get("recheck_outcome") or "") != "script_error":
        return {}
    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    workdir = Path(session["workdir"]).resolve()
    originating_scripts = []
    for index, item in enumerate(evidence.get("verification_evidence") or []):
        if not isinstance(item, dict):
            continue
        source = str(item.get("complete_python_script") or "")
        original_name = "original_script.py" if index == 0 else f"original_script_{index + 1:02d}.py"
        original_path = root / original_name
        original_path.write_text(source, encoding="utf-8")
        originating_scripts.append(
            {
                "script_name": str(item.get("script_name") or ""),
                "source_script_path": str(item.get("script_path") or ""),
                "preserved_copy_path": str(original_path.resolve().relative_to(workdir)),
                "code_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            }
        )

    replacement_entries = []
    for proposal in structured_result.get("replacement_checks") or []:
        check_id = _safe_replacement_check_id(proposal.get("check_id"))
        code = str(proposal.get("python_code") or "")
        script_path = root / f"replacement_{check_id}.py"
        script_path.write_text(code, encoding="utf-8")
        validation = validate_verification_replacement_code(code)
        replacement_entries.append(
            {
                "check_id": check_id,
                "relationship_to_original": proposal.get("relationship_to_original"),
                "purpose": proposal.get("purpose"),
                "correction_explanation": proposal.get("correction_explanation"),
                "independence_note": proposal.get("independence_note"),
                "expected_check_kind": proposal.get("expected_check_kind"),
                "tested_scope": proposal.get("tested_scope") or {},
                "script_path": str(script_path.resolve().relative_to(workdir)),
                "code_sha256": validation["code_sha256"],
                "validation": validation,
            }
        )

    manifest_path = root / "replacement_manifest.json"
    event_path = root / "replacement_execution_events.jsonl"
    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "recheck_id": str(recheck_id),
        "finding_ids": list(structured_result.get("finding_ids") or []),
        "chunk_id": str(structured_result.get("chunk_id") or ""),
        "model": str(model or ""),
        "reasoning_effort": str(reasoning_effort or ""),
        "script_error_explanation": str(
            (structured_result.get("script_assessment") or {}).get("error_explanation") or ""
        ),
        "replacement_recommended": bool(structured_result.get("replacement_recommended")),
        "replacement_unavailable_explanation": str(
            structured_result.get("replacement_unavailable_explanation") or ""
        ),
        "originating_scripts": originating_scripts,
        "replacement_checks": replacement_entries,
        "execution_events_path": str(event_path.resolve().relative_to(workdir)),
    }
    save_json(manifest_path, manifest)
    return {
        "manifest": manifest,
        "manifest_path": str(manifest_path.resolve().relative_to(workdir)),
        "ready_count": sum(
            1 for item in replacement_entries if (item.get("validation") or {}).get("status") == "ready"
        ),
        "proposed_count": len(replacement_entries),
    }


def _relative_audit_path(session: dict[str, Any], path_value: Any) -> str:
    raw = str(path_value or "").strip()
    if not raw:
        return ""
    root = Path(session["workdir"]).resolve()
    path = Path(raw)
    try:
        return str(path.resolve().relative_to(root))
    except Exception:
        return raw


def _verification_counterexample_summary(counterexample: dict[str, Any], script_text: str = "") -> str:
    if not isinstance(counterexample, dict):
        return str(counterexample)
    a_value = counterexample.get("a")
    factorization = counterexample.get("factorization")
    factor_text = ""
    if isinstance(factorization, dict):
        factors = []
        for prime, exponent in sorted(
            factorization.items(),
            key=lambda item: (0, int(item[0])) if str(item[0]).isdigit() else (1, str(item[0])),
        ):
            try:
                clean_exponent = int(exponent)
            except Exception:
                clean_exponent = exponent
            factors.append(str(prime) if clean_exponent == 1 else f"{prime}^{clean_exponent}")
        factor_text = " * ".join(factors)
    base_match = re.search(r"sigma\((\d+)\^a\)", script_text, flags=re.IGNORECASE)
    base = base_match.group(1) if base_match else "n"
    parts = []
    if a_value is not None:
        parts.append(f"a = {a_value}")
    if factor_text:
        if a_value is not None and base != "n":
            parts.append(f"sigma({base}^{a_value}) = {factor_text}")
        else:
            parts.append(f"factorization = {factor_text}")
    if counterexample.get("omega_sigma") is not None:
        target = f"sigma({base}^{a_value})" if a_value is not None and base != "n" else "sigma(input)"
        parts.append(f"Omega({target}) = {counterexample.get('omega_sigma')}")
    if counterexample.get("omega_input") is not None:
        target = f"{base}^{a_value}" if a_value is not None and base != "n" else "input"
        parts.append(f"Omega({target}) = {counterexample.get('omega_input')}")
    return "; ".join(parts) or json.dumps(counterexample, ensure_ascii=False, sort_keys=True)


def _bounded_verification_output(text: Any, limit: int = 30000) -> dict[str, Any]:
    raw = str(text or "")
    if len(raw) <= limit:
        return {"text": raw, "truncated": False, "original_chars": len(raw)}
    keep = max(1000, limit // 3)
    result_lines = [
        line
        for line in raw.splitlines()
        if any(token in line.lower() for token in ("counterexample", "failure", "failed", "claim", "math_audit_verification_result_json="))
    ]
    middle = "\n".join(result_lines)
    bounded = raw[:keep] + "\n... [unrelated output omitted] ...\n"
    if middle:
        bounded += middle + "\n... [end preserved mathematical-result lines] ...\n"
    bounded += raw[-keep:]
    return {"text": bounded, "truncated": True, "original_chars": len(raw)}


def verification_finding_recheck_candidates(
    session: dict[str, Any],
    findings: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    current_findings = verification_findings_for_session(session) if findings is None else list(findings)
    manifest = load_manifest(session)
    chunk_ids = {
        str(item.get("chunk_id") or "").strip()
        for item in manifest.get("chunks") or []
        if isinstance(item, dict)
    }
    latest_map = verification_finding_recheck_map(session)
    grouped: dict[str, list[dict[str, Any]]] = {}
    excluded: list[dict[str, Any]] = []
    for finding in current_findings:
        finding_id = str(finding.get("finding_id") or "").strip()
        chunk_id = str(finding.get("chunk_id") or "").strip()
        outcome = str(finding.get("mathematical_outcome") or "").strip()
        latest = latest_map.get(finding_id)
        latest_result = latest.get("structured_result") if isinstance(latest, dict) else {}
        latest_outcome = str((latest_result or {}).get("recheck_outcome") or "").strip()
        reason = ""
        if not finding.get("active", True) or finding.get("superseded"):
            reason = "finding is inactive or superseded"
        elif outcome not in NEGATIVE_VERIFICATION_OUTCOMES:
            reason = f"mathematical outcome {outcome or 'unknown'} is not a counterexample/claim failure"
        elif latest_outcome in CONCLUSIVE_VERIFICATION_FINDING_RECHECK_OUTCOMES:
            reason = f"latest canonical recheck is conclusive ({latest_outcome})"
        if reason:
            excluded.append({"finding_id": finding_id, "chunk_id": chunk_id, "reason": reason})
            continue
        grouped.setdefault(chunk_id, []).append(finding)

    candidates = []
    for chunk_id, chunk_findings in sorted(grouped.items(), key=lambda item: _chunk_index_from_chunk_id(item[0])):
        scripts = []
        linked_issue_ids: set[str] = set()
        targets = []
        counterexample_count = 0
        unavailable_reasons = []
        for finding in chunk_findings:
            script_name = str(finding.get("script_name") or "").strip()
            script_path = _resolve_verification_script_path(session, finding.get("script_path") or script_name)
            if script_path is None:
                unavailable_reasons.append(f"missing script {script_name or '(unnamed)'}")
            scripts.append(
                {
                    "script_name": script_name,
                    "script_path": _relative_audit_path(session, script_path or finding.get("script_path")),
                    "available": script_path is not None,
                }
            )
            linked_issue_ids.update(str(item) for item in (finding.get("linked_issue_ids") or []) if str(item).strip())
            linked_issue_ids.update(str(item) for item in (finding.get("matched_issue_ids") or []) if str(item).strip())
            if finding.get("target"):
                targets.append(finding.get("target"))
            counterexample_count += len(finding.get("counterexamples") or []) + len(finding.get("failed_cases") or [])
        if chunk_id not in chunk_ids:
            unavailable_reasons.append(f"missing canonical chunk {chunk_id or '(unknown)'}")
        finding_ids = [str(item.get("finding_id") or "") for item in chunk_findings]
        candidates.append(
            {
                "candidate_id": f"verification_finding_recheck:{chunk_id or 'unknown'}",
                "workflow": "verification_finding_recheck",
                "chunk_id": chunk_id,
                "finding_ids": finding_ids,
                "finding_count": len(finding_ids),
                "scripts": scripts,
                "linked_issue_ids": sorted(linked_issue_ids),
                "targets": targets,
                "mathematical_outcomes": sorted({str(item.get("mathematical_outcome") or "") for item in chunk_findings}),
                "counterexample_count": counterexample_count,
                "latest_recheck_status": "inconclusive" if any(
                    str(((latest_map.get(finding_id) or {}).get("structured_result") or {}).get("recheck_outcome") or "") == "inconclusive"
                    for finding_id in finding_ids
                ) else "not_rechecked",
                "eligible": not unavailable_reasons,
                "availability_issues": unavailable_reasons,
                "eligibility_reason": (
                    "active counterexample/claim-failure finding requires an evidence-rich issue-level recheck"
                    if not unavailable_reasons
                    else "candidate is visible but unavailable: " + "; ".join(unavailable_reasons)
                ),
            }
        )
    eligible = [item for item in candidates if item.get("eligible")]
    return {
        "workflow": "verification_finding_recheck",
        "candidates": candidates,
        "excluded_findings": excluded,
        "summary": {
            "candidate_chunk_count": len(candidates),
            "eligible_chunk_count": len(eligible),
            "eligible_finding_count": sum(int(item.get("finding_count") or 0) for item in eligible),
            "unavailable_chunk_count": len(candidates) - len(eligible),
        },
    }


def build_verification_finding_recheck_evidence(
    session: dict[str, Any],
    candidate: dict[str, Any],
    max_context_chars: int = 12000,
) -> dict[str, Any]:
    chunk_id = str(candidate.get("chunk_id") or "").strip()
    finding_ids = {str(item).strip() for item in candidate.get("finding_ids") or [] if str(item).strip()}
    findings = [
        item
        for item in verification_findings_for_session(session)
        if str(item.get("finding_id") or "") in finding_ids
    ]
    manifest = load_manifest(session)
    chunks = [item for item in manifest.get("chunks") or [] if isinstance(item, dict)]
    chunk_position = next((index for index, item in enumerate(chunks) if str(item.get("chunk_id") or "") == chunk_id), -1)
    if chunk_position < 0:
        raise ValueError(f"Canonical chunk {chunk_id!r} is not available.")
    chunk = dict(chunks[chunk_position])
    chunk_text = str(chunk.get("chunk_text") or "")
    records = [item for item in _read_chunk_records_for_verification(session) if str(item.get("chunk_id") or "") == chunk_id]
    chunk_record = dict(records[-1]) if records else {}

    structured_response = None
    structured_path_value = chunk_record.get("structured_response_path")
    structured_candidates = []
    if structured_path_value:
        structured_candidates.append(Path(str(structured_path_value)))
        structured_candidates.append(Path(session["workdir"]) / str(structured_path_value))
    structured_candidates.append(Path(session["workdir"]) / "responses" / f"{chunk_id}.structured.json")
    for path in structured_candidates:
        if path.exists():
            try:
                structured_response = load_json(path)
                break
            except Exception:
                continue

    results = {
        str(item.get("script_name") or ""): item
        for item in _load_verification_results(session)
        if str(item.get("chunk_id") or "") == chunk_id
    }
    verification_evidence = []
    evidence_hashes = {"chunk_text_sha256": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(), "scripts": {}, "results": {}}
    for finding in findings:
        script_name = str(finding.get("script_name") or "").strip()
        script_path = _resolve_verification_script_path(session, finding.get("script_path") or script_name)
        if script_path is None:
            raise FileNotFoundError(f"Verification script is unavailable: {script_name}")
        script_text = script_path.read_text(encoding="utf-8")
        result = dict(results.get(script_name) or {})
        if not result:
            result_path_value = str(finding.get("result_path") or "").strip()
            result_path = Path(result_path_value) if result_path_value else None
            if result_path is not None and result_path.is_file():
                result = load_json(result_path)
        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        structured_result = result.get("structured_result") if isinstance(result.get("structured_result"), dict) else {}
        counterexamples = list(result.get("counterexamples") or finding.get("counterexamples") or [])
        failed_cases = list(result.get("failed_cases") or finding.get("failed_cases") or [])
        sentinel_lines = [
            line for line in str(result.get("stdout") or "").splitlines()
            if line.strip().startswith(VERIFICATION_RESULT_SENTINEL)
        ]
        verification_evidence.append(
            {
                "finding_id": finding.get("finding_id"),
                "script_name": script_name,
                "script_path": _relative_audit_path(session, script_path),
                "complete_python_script": script_text,
                "purpose": result.get("purpose"),
                "description": result.get("description"),
                "expected_outcome": result.get("expected_outcome"),
                "execution_status": result.get("execution_status"),
                "mathematical_outcome": result.get("mathematical_outcome"),
                "outcome_source": result.get("outcome_source"),
                "returncode": result.get("returncode"),
                "elapsed_seconds": result.get("elapsed_seconds"),
                "python_executable": result.get("python_executable"),
                "safe_only": result.get("safe_only"),
                "tested_range": result.get("tested_range"),
                "target": result.get("target") or finding.get("target") or {},
                "linked_issue_ids": sorted(set(result.get("linked_issue_ids") or []) | set(finding.get("matched_issue_ids") or [])),
                "structured_result_sentinel_lines": sentinel_lines,
                "complete_structured_result": structured_result,
                "complete_counterexamples": counterexamples,
                "complete_failed_cases": failed_cases,
                "counterexample_summaries": [
                    _verification_counterexample_summary(item, script_text)
                    for item in counterexamples
                    if isinstance(item, dict)
                ],
                "stdout": stdout,
                "stdout_truncated": False,
                "stdout_original_chars": len(stdout),
                "stderr": stderr,
                "stderr_truncated": False,
                "stderr_original_chars": len(stderr),
                "result_path": _relative_audit_path(session, result.get("result_path") or finding.get("result_path")),
            }
        )
        evidence_hashes["scripts"][script_name] = hashlib.sha256(script_text.encode("utf-8")).hexdigest()
        evidence_hashes["results"][script_name] = hashlib.sha256(
            json.dumps(result, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    issues = [item for item in (load_issues(session).get("issues") or []) if isinstance(item, dict)]
    linked_ids = {
        str(issue_id)
        for finding in findings
        for issue_id in (finding.get("linked_issue_ids") or []) + (finding.get("matched_issue_ids") or [])
        if str(issue_id).strip()
    }
    current_issues = [
        item for item in issues
        if str(item.get("chunk_id") or "") == chunk_id or str(item.get("issue_id") or "") in linked_ids
    ]

    reference_labels = []
    reference_path = Path(session["workdir"]) / "state" / "reference_map.json"
    if reference_path.exists():
        try:
            reference_state = load_json(reference_path)
        except Exception:
            reference_state = {}
        label_map = reference_state.get("label_map") if isinstance(reference_state, dict) else {}
        if isinstance(label_map, dict):
            targets_text = " ".join(
                str((finding.get("target") or {}).get("label") or (finding.get("target") or {}).get("name") or "")
                for finding in findings
                if isinstance(finding.get("target"), dict)
            )
            for source_label, info in label_map.items():
                info_dict = dict(info) if isinstance(info, dict) else {"printed_label": info}
                printed = str(info_dict.get("printed_label") or info_dict.get("number") or "")
                if str(source_label) in chunk_text or str(source_label) in targets_text or (printed and printed in targets_text):
                    reference_labels.append({"source_label": source_label, **info_dict})

    neighbors = []
    for index in (chunk_position - 1, chunk_position + 1):
        if 0 <= index < len(chunks):
            neighbor = chunks[index]
            neighbor_text = str(neighbor.get("chunk_text") or "")
            excerpt = _bounded_verification_output(neighbor_text, limit=max(1500, int(max_context_chars) // 4))
            neighbors.append(
                {
                    "chunk_id": neighbor.get("chunk_id"),
                    "label": neighbor.get("display_label") or neighbor.get("label"),
                    "boundary": neighbor.get("boundary"),
                    "page_start": neighbor.get("page_start"),
                    "page_end": neighbor.get("page_end"),
                    "text_excerpt": excerpt["text"],
                    "excerpt_truncated": excerpt["truncated"],
                }
            )
    ledger = load_ledger(session)
    ledger_text = json.dumps(ledger, ensure_ascii=False, sort_keys=True, default=str)
    ledger_excerpt = _bounded_verification_output(ledger_text, limit=max(2000, int(max_context_chars) // 2))

    return {
        "schema_version": 1,
        "workflow": "verification_finding_recheck",
        "candidate": candidate,
        "manuscript": {
            "chunk_id": chunk_id,
            "chunk_index": chunk.get("chunk_index"),
            "title": chunk.get("display_label") or chunk.get("label"),
            "label": chunk.get("label"),
            "boundary": chunk.get("boundary"),
            "page_start": chunk.get("page_start"),
            "page_end": chunk.get("page_end"),
            "source_kind": chunk.get("source_kind"),
            "source_label": chunk.get("source_label"),
            "printed_label": chunk.get("printed_label"),
            "complete_canonical_chunk_text": chunk_text,
            "chunk_record": chunk_record,
            "original_structured_chunk_audit_response": structured_response,
            "current_canonical_issues": current_issues,
            "relevant_reference_labels": reference_labels,
            "neighboring_context": neighbors,
            "global_ledger_context": ledger_excerpt["text"],
            "global_ledger_context_truncated": ledger_excerpt["truncated"],
        },
        "verification_findings": findings,
        "verification_evidence": verification_evidence,
        "input_evidence_hashes": evidence_hashes,
        "evidence_policy": {
            "complete_chunk_included": True,
            "complete_scripts_included": True,
            "complete_structured_counterexamples_included": True,
            "complete_stdout_stderr_included": True,
            "large_output_policy": "execution stdout/stderr are complete; only ancillary neighbor and ledger context is bounded",
        },
    }


def build_verification_finding_recheck_prompt(evidence: dict[str, Any]) -> str:
    parts = [
        "Verification-finding recheck task",
        (
            "Review the supplied manuscript chunk and verification evidence as a focused reconciliation task. "
            "Do not redo the whole audit and do not replace the original chunk findings. The original verification "
            "finding is deterministic evidence that must remain visible unless later verification supersedes it."
        ),
        (
            "Determine whether each negative result is a genuine counterexample/claim failure, a generated-script "
            "error, a theorem-scope or hypothesis mismatch, a notation/interpretation mismatch, or inconclusive. "
            "Do not discard a counterexample merely because the Python process returned exit code zero."
        ),
        "Answer all of these questions in the structured response:",
        "1. Does each script compute the intended mathematical quantity?",
        "2. Does it implement the theorem or claim as stated?",
        "3. Does each candidate input satisfy every hypothesis?",
        "4. Are the exact arithmetic, factorization, and derived quantities correct?",
        "5. Does the observed result truly contradict the claim?",
        "6. Is there a scope, indexing, normalization, or notation mismatch?",
        "7. Is the problem in the theorem, proof, script, or interpretation?",
        "8. Which current issue should be strengthened or linked?",
        "9. Is a new verification-derived issue needed?",
        "10. Which downstream results depend on the contradicted claim?",
        "11. Would an independently implemented second check be useful?",
        (
            "If and only if recheck_outcome is script_error, identify the exact defect and affected lines. "
            "Normally provide a complete corrected standalone Python script in replacement_checks. If practical, "
            "also provide a materially independent second implementation. If no safe or meaningful replacement can "
            "be produced, set replacement_recommended=false and explain why in replacement_unavailable_explanation."
        ),
        (
            "Each replacement must test the original claim as stated without weakening its hypotheses, range, "
            "normalization, indexing, or arithmetic definitions. Prefer exact arithmetic. Each script must emit exactly "
            "one final MATH_AUDIT_VERIFICATION_RESULT_JSON= record using the existing structured result contract. "
            "Distinguish execution success from mathematical outcome, report counterexamples completely, and describe "
            "finite negative searches as 'no counterexample found in tested range', never as proof of the theorem."
        ),
        (
            "For non-script-error outcomes, set replacement_recommended=false, replacement_checks=[], and leave "
            "replacement_unavailable_explanation empty."
        ),
    ]
    for item in evidence.get("verification_evidence") or []:
        if not isinstance(item, dict):
            continue
        script_name = str(item.get("script_name") or "verification script")
        parts.extend(
            [
                f"Complete Python script: {script_name}",
                "```python\n" + str(item.get("complete_python_script") or "") + "\n```",
                f"Exact stdout for {script_name}",
                "```text\n" + str(item.get("stdout") or "") + "\n```",
                f"Exact stderr for {script_name}",
                "```text\n" + str(item.get("stderr") or "") + "\n```",
                f"Complete structured counterexample/result data for {script_name}",
                json.dumps(
                    {
                        "structured_result": item.get("complete_structured_result") or {},
                        "counterexamples": item.get("complete_counterexamples") or [],
                        "failed_cases": item.get("complete_failed_cases") or [],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    default=str,
                ),
            ]
        )
    parts.extend(
        [
            "Complete evidence package (JSON):",
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        ]
    )
    return "\n\n".join(parts).strip() + "\n"


def verification_findings_for_session(
    session: dict[str, Any],
    results: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    active_results = _load_verification_results(session) if results is None else results
    findings = [
        finding
        for result in active_results
        for finding in [_verification_finding_from_result(session, result)]
        if finding is not None
    ]
    findings.sort(key=lambda item: (_chunk_index_from_chunk_id(str(item.get("chunk_id") or "")), str(item.get("script_name") or "")))
    return apply_verification_finding_rechecks(session, findings)


def _persist_verification_findings(
    session: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    path = _verification_findings_path(session)
    previous: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = load_json(path)
            previous = loaded if isinstance(loaded, dict) else {}
        except Exception:
            previous = {}
    previous_findings = {
        str(item.get("finding_id") or ""): item
        for item in (previous.get("findings") or [])
        if isinstance(item, dict) and item.get("finding_id")
    }
    findings = verification_findings_for_session(session, results=results)
    current_findings = {str(item.get("finding_id")): item for item in findings}
    history_path = Path(session["workdir"]) / "logs" / "verification_finding_events.jsonl"
    for finding_id in sorted(current_findings.keys() - previous_findings.keys()):
        append_jsonl(
            history_path,
            {
                "time": utc_now(),
                "action": "verification_finding_activated",
                "finding_id": finding_id,
                "script_name": current_findings[finding_id].get("script_name"),
                "chunk_id": current_findings[finding_id].get("chunk_id"),
                "mathematical_outcome": current_findings[finding_id].get("mathematical_outcome"),
            },
        )
    for finding_id in sorted(previous_findings.keys() - current_findings.keys()):
        append_jsonl(
            history_path,
            {
                "time": utc_now(),
                "action": "verification_finding_superseded",
                "finding_id": finding_id,
                "script_name": previous_findings[finding_id].get("script_name"),
                "chunk_id": previous_findings[finding_id].get("chunk_id"),
                "reason": "latest canonical verification results no longer contain this negative outcome",
            },
        )
    sidecar = {
        "schema_version": 1,
        "updated_at": utc_now(),
        "findings": findings,
        "active_finding_count": len(findings),
    }
    save_json(path, sidecar)
    return sidecar


def supersede_verification_findings_for_chunks(session: dict[str, Any], chunk_ids: set[str]) -> dict[str, Any]:
    path = _verification_findings_path(session)
    if not path.exists():
        return {"superseded_finding_count": 0, "superseded_finding_ids": []}
    try:
        sidecar = load_json(path)
    except Exception:
        return {"superseded_finding_count": 0, "superseded_finding_ids": []}
    if not isinstance(sidecar, dict):
        return {"superseded_finding_count": 0, "superseded_finding_ids": []}
    findings = [item for item in (sidecar.get("findings") or []) if isinstance(item, dict)]
    removed = [item for item in findings if str(item.get("chunk_id") or "") in chunk_ids]
    if not removed:
        return {"superseded_finding_count": 0, "superseded_finding_ids": []}
    kept = [item for item in findings if str(item.get("chunk_id") or "") not in chunk_ids]
    sidecar["findings"] = kept
    sidecar["active_finding_count"] = len(kept)
    sidecar["updated_at"] = utc_now()
    save_json(path, sidecar)
    history_path = Path(session["workdir"]) / "logs" / "verification_finding_events.jsonl"
    for finding in removed:
        append_jsonl(
            history_path,
            {
                "time": utc_now(),
                "action": "verification_finding_superseded",
                "finding_id": finding.get("finding_id"),
                "script_name": finding.get("script_name"),
                "chunk_id": finding.get("chunk_id"),
                "reason": "source chunk selected for rerun",
            },
        )
    return {
        "superseded_finding_count": len(removed),
        "superseded_finding_ids": [str(item.get("finding_id") or "") for item in removed],
    }


def verification_result_needs_technical_retry(result: dict[str, Any]) -> bool:
    normalized = _normalize_verification_result(result)
    return str(normalized.get("execution_status") or "") in TECHNICAL_VERIFICATION_FAILURE_STATUSES


def _load_verification_results(session: dict[str, Any], state: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    state = load_verification_state(session) if state is None else state
    results = []
    seen = set()
    for item in state.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        result_path = item.get("result_path")
        if not result_path:
            results.append(dict(item))
            continue
        path = Path(result_path)
        if not path.exists():
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        data = load_json(path)
        if isinstance(data, dict):
            results.append(data)
    results_dir = _verification_results_path(session)
    if results_dir.exists():
        for path in sorted(results_dir.glob("*.result.json")):
            key = str(path.resolve())
            if key in seen:
                continue
            data = load_json(path)
            if isinstance(data, dict):
                results.append(data)
    script_entries = {
        str(item.get("script_name") or ""): item
        for item in _collect_verification_scripts(session)
        if str(item.get("script_name") or "")
    }
    normalized_results = [
        _normalize_verification_result(item, script_entries.get(str(item.get("script_name") or "")))
        for item in results
    ]
    canonical_by_key: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(normalized_results):
        script_name = str(item.get("script_name") or "").strip()
        chunk_id = str(item.get("chunk_id") or "").strip()
        key = f"{chunk_id}:{script_name}" if script_name else str(item.get("result_path") or f"inline:{index}")
        canonical_by_key[key] = item
    canonical_results = list(canonical_by_key.values())
    canonical_results.sort(key=lambda item: (int(item.get("chunk_index") or _chunk_index_from_chunk_id(item.get("chunk_id", ""))), item.get("script_name", "")))
    return canonical_results


def _run_verification_scripts(
    session: dict[str, Any],
    script_entries: list[dict[str, Any]],
    timeout: int = 120,
    safe_only: bool = True,
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    replace_all_results: bool = True,
) -> dict[str, Any]:
    root = Path(session["workdir"])
    started_at = utc_now()
    results = []
    total = len(script_entries)
    _emit_verification_progress(progress_callback, "suite_started", total=total, scripts_total=total)
    for index, entry in enumerate(script_entries, start=1):
        script_name = str(entry.get("script_name") or "")
        chunk_id = str(entry.get("chunk_id") or _chunk_id_from_script_name(script_name))
        chunk_index = int(entry.get("chunk_index") or _chunk_index_from_chunk_id(chunk_id))
        script_path_text = entry.get("script_path")
        script_path = Path(script_path_text) if script_path_text else None
        _emit_verification_progress(
            progress_callback,
            "script_started",
            index=index,
            total=total,
            script_name=script_name,
            chunk_id=chunk_id,
        )
        result = {
            "time": utc_now(),
            "chunk_id": chunk_id,
            "chunk_index": chunk_index,
            "script_name": script_name,
            "script_path": str(script_path) if script_path else "",
            "python_executable": sys.executable,
            "timeout_seconds": int(timeout),
            "safe_only": bool(safe_only),
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "elapsed_seconds": 0.0,
            "status": "skipped",
            "execution_status": "not_run",
            "mathematical_outcome": "",
            "outcome_source": "",
            "skip_reason": "",
            "conclusion": "",
            "purpose": str(entry.get("purpose") or "").strip(),
            "description": str(entry.get("description") or "").strip(),
            "expected_outcome": str(entry.get("expected_outcome") or "").strip(),
            "target": _verification_metadata_from_entry(entry).get("target") or {},
        }
        start = time.time()
        if script_path is None or not script_path.exists():
            result["skip_reason"] = "Script not found in the current audit folder."
        else:
            if safe_only:
                allowed, reason = _check_verification_script_safety(script_path)
                if not allowed:
                    result["skip_reason"] = reason
            if not result["skip_reason"]:
                try:
                    completed = subprocess.run(
                        [sys.executable, str(script_path)],
                        cwd=str(root),
                        capture_output=True,
                        text=True,
                        timeout=int(timeout),
                    )
                    result["returncode"] = int(completed.returncode)
                    result["stdout"] = completed.stdout or ""
                    result["stderr"] = completed.stderr or ""
                    result["execution_status"] = "completed" if completed.returncode == 0 else "runtime_error"
                except subprocess.TimeoutExpired as e:
                    result["stdout"] = e.stdout or ""
                    result["stderr"] = e.stderr or ""
                    result["execution_status"] = "timeout"
                except Exception as e:
                    result["stderr"] = repr(e)
                    result["execution_status"] = "runtime_error"
        result["elapsed_seconds"] = max(0.0, time.time() - start)
        if result["skip_reason"]:
            result["execution_status"] = "unsafe" if safe_only and script_path is not None else "skipped"
        result = _normalize_verification_result(result, entry)
        result["status"] = _legacy_status_for_execution(str(result.get("execution_status") or ""))
        result["status_semantics"] = "deprecated_legacy_execution_alias"
        result["conclusion"] = _infer_verification_conclusion(result)
        result_path = _verification_result_path(session, script_name or f"script_{len(results)+1:03d}.py")
        result["result_path"] = str(result_path)
        if result_path.exists():
            try:
                previous_result = _normalize_verification_result(load_json(result_path), entry)
            except Exception:
                previous_result = None
            if isinstance(previous_result, dict):
                append_jsonl(
                    Path(session["workdir"]) / "logs" / "verification_result_events.jsonl",
                    {
                        "time": utc_now(),
                        "action": "verification_result_superseded",
                        "script_name": previous_result.get("script_name"),
                        "chunk_id": previous_result.get("chunk_id"),
                        "execution_status": previous_result.get("execution_status"),
                        "mathematical_outcome": previous_result.get("mathematical_outcome"),
                        "outcome_source": previous_result.get("outcome_source"),
                        "result_path": str(result_path),
                    },
                )
        save_json(result_path, result)
        results.append(result)
        _emit_verification_progress(
            progress_callback,
            "script_finished",
            index=index,
            total=total,
            script_name=script_name,
            chunk_id=chunk_id,
            status=result.get("status"),
            execution_status=result.get("execution_status"),
            mathematical_outcome=result.get("mathematical_outcome"),
            conclusion=result.get("conclusion"),
        )
    state = load_verification_state(session)
    canonical_results = list(results)
    if not replace_all_results:
        existing_results = _load_verification_results(session, state=state)
        replacements = {str(item.get("script_name") or ""): item for item in results}
        canonical_by_name = {
            str(item.get("script_name") or ""): item
            for item in existing_results
            if str(item.get("script_name") or "") not in replacements
        }
        canonical_by_name.update(replacements)
        canonical_results = sorted(
            canonical_by_name.values(),
            key=lambda item: (
                int(item.get("chunk_index") or _chunk_index_from_chunk_id(str(item.get("chunk_id") or ""))),
                str(item.get("script_name") or ""),
            ),
        )
    summary = _verification_summary_counts(canonical_results)
    state["last_run"] = {
        "started_at": started_at,
        "finished_at": utc_now(),
        "timeout_seconds": int(timeout),
        "safe_only": bool(safe_only),
        "python_executable": sys.executable,
        **summary,
    }
    state["results"] = [
        {
            "chunk_id": result.get("chunk_id"),
            "chunk_index": result.get("chunk_index"),
            "script_name": result.get("script_name"),
            "script_path": result.get("script_path"),
            "result_path": result.get("result_path"),
            "status": result.get("status"),
            "status_semantics": result.get("status_semantics"),
            "execution_status": result.get("execution_status"),
            "mathematical_outcome": result.get("mathematical_outcome"),
            "outcome_source": result.get("outcome_source"),
            "counterexamples": result.get("counterexamples") or [],
            "failed_cases": result.get("failed_cases") or [],
            "tested_range": result.get("tested_range"),
            "target": result.get("target") or {},
            "linked_issue_ids": result.get("linked_issue_ids") or [],
            "returncode": result.get("returncode"),
            "elapsed_seconds": result.get("elapsed_seconds"),
            "conclusion": result.get("conclusion"),
        }
        for result in canonical_results
    ]
    save_verification_state(session, state)
    findings_state = _persist_verification_findings(session, canonical_results)
    return {
        "session": session,
        "results": canonical_results,
        "executed_results": results,
        "summary": summary,
        "state": state,
        "verification_findings": findings_state.get("findings", []),
    }


def run_verification_suite(
    pdf_path: str | Path,
    timeout: int = 120,
    safe_only: bool = True,
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    session = load_session_from_pdf(pdf_path)
    if session is None:
        raise FileNotFoundError("No audit session found for this PDF.")
    scripts = _collect_verification_scripts(session)
    return _run_verification_scripts(
        session,
        scripts,
        timeout=int(timeout),
        safe_only=bool(safe_only),
        progress_callback=progress_callback,
    )


def rerun_failed_verification_scripts(pdf_path: str | Path, timeout: int = 120, safe_only: bool = True) -> dict[str, Any]:
    session = load_session_from_pdf(pdf_path)
    if session is None:
        raise FileNotFoundError("No audit session found for this PDF.")
    state = load_verification_state(session)
    failed_names = {
        Path(str(item.get("script_name") or item.get("script_path") or "")).name
        for item in state.get("results", []) or []
        if verification_result_needs_technical_retry(item)
    }
    failed_names.discard("")
    if not failed_names:
        results = _load_verification_results(session, state=state)
        return {"session": session, "results": results, "summary": _verification_summary_counts(results), "state": state}
    scripts = _collect_verification_scripts(session, only_script_names=failed_names)
    return _run_verification_scripts(
        session,
        scripts,
        timeout=int(timeout),
        safe_only=bool(safe_only),
        replace_all_results=False,
    )


def _replacement_manifest_for_record(
    session: dict[str, Any],
    record: dict[str, Any],
) -> tuple[dict[str, Any], Optional[Path]]:
    relative = str(record.get("replacement_manifest_path") or "").strip()
    if not relative:
        return {}, None
    workdir = Path(session["workdir"]).resolve()
    path = (workdir / relative).resolve()
    try:
        path.relative_to(workdir)
    except ValueError:
        return {}, None
    if not path.is_file():
        return {}, path
    try:
        manifest = load_json(path)
    except Exception:
        return {}, path
    return (manifest if isinstance(manifest, dict) else {}), path


def _replacement_execution_events(
    session: dict[str, Any],
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    relative = str(manifest.get("execution_events_path") or "").strip()
    if not relative:
        return []
    workdir = Path(session["workdir"]).resolve()
    path = (workdir / relative).resolve()
    try:
        path.relative_to(workdir)
    except ValueError:
        return []
    if not path.is_file():
        return []
    events = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                events.append(item)
    except Exception:
        return []
    return events


def _canonical_replacement_executions(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    canonical: dict[str, dict[str, Any]] = {}
    for event in events:
        if str(event.get("event") or "") != "execution_completed":
            continue
        check_id = str(event.get("replacement_check_id") or "").strip()
        if check_id:
            canonical[check_id] = event
    return canonical


def _replacement_reconciliation(checks: list[dict[str, Any]]) -> dict[str, Any]:
    executed = [item for item in checks if isinstance(item.get("latest_execution"), dict)]
    outcomes = {
        str((item.get("latest_execution") or {}).get("mathematical_outcome") or "")
        for item in executed
    }
    outcomes.discard("")
    execution_statuses = {
        str((item.get("latest_execution") or {}).get("execution_status") or "")
        for item in executed
    }
    negative = outcomes & NEGATIVE_VERIFICATION_OUTCOMES
    favorable = outcomes & {"no_counterexample_found", "check_satisfied"}
    technical_failure = bool(
        execution_statuses & {"runtime_error", "timeout", "parse_error", "unsafe", "skipped", "not_run"}
    )
    if negative and favorable:
        status = "conflicting_replacement_results"
        explanation = "Replacement checks disagree; the mathematical finding remains unresolved."
    elif negative:
        status = "counterexample_supported_by_replacement"
        explanation = "A corrected or independent replacement check reports a counterexample or claim failure."
    elif technical_failure:
        status = "unresolved_replacement_failure"
        explanation = "At least one replacement check failed or emitted unusable output; the finding remains unresolved."
    elif "check_satisfied" in outcomes:
        status = "provisionally_resolved_exact_check"
        explanation = "A replacement exact check was satisfied; human review must confirm that its scope matches the claim."
    elif "no_counterexample_found" in outcomes:
        status = "provisionally_challenged_finite_check"
        explanation = "No counterexample was found in the replacement check's stated scope; this is not a proof beyond that scope."
    elif executed and execution_statuses <= {"completed"}:
        status = "unresolved_no_structured_outcome"
        explanation = "Replacement execution completed without a usable mathematical outcome."
    elif executed:
        status = "unresolved_replacement_failure"
        explanation = "A replacement check failed, timed out, was unsafe, or emitted malformed output."
    elif checks:
        status = "awaiting_replacement_execution"
        explanation = "Replacement scripts are proposed but have not been executed."
    else:
        status = "replacement_unavailable"
        explanation = "No replacement script is available."
    return {
        "status": status,
        "explanation": explanation,
        "executed_check_count": len(executed),
        "mathematical_outcomes": sorted(outcomes),
        "execution_statuses": sorted(execution_statuses),
        "human_review_required": True,
    }


def verification_replacement_check_inventory(session: dict[str, Any]) -> dict[str, Any]:
    groups = []
    for record in verification_finding_rechecks_for_session(session):
        result = record.get("structured_result") if isinstance(record.get("structured_result"), dict) else {}
        if str(result.get("recheck_outcome") or "") != "script_error":
            continue
        manifest, manifest_path = _replacement_manifest_for_record(session, record)
        events = _replacement_execution_events(session, manifest)
        canonical = _canonical_replacement_executions(events)
        checks = []
        for check in manifest.get("replacement_checks") or []:
            if not isinstance(check, dict):
                continue
            item = dict(check)
            latest_execution = canonical.get(str(item.get("check_id") or ""))
            if isinstance(latest_execution, dict):
                latest_execution = dict(latest_execution)
                result_relative = str(latest_execution.get("result_path") or "").strip()
                if result_relative:
                    result_path = (Path(session["workdir"]).resolve() / result_relative).resolve()
                    try:
                        result_path.relative_to(Path(session["workdir"]).resolve())
                        loaded_result = load_json(result_path) if result_path.is_file() else {}
                    except Exception:
                        loaded_result = {}
                    if isinstance(loaded_result, dict):
                        latest_execution["result"] = loaded_result
            item["latest_execution"] = latest_execution
            checks.append(item)
        reconciliation = _replacement_reconciliation(checks)
        groups.append(
            {
                "recheck_id": str(record.get("recheck_id") or ""),
                "finding_ids": list(record.get("finding_ids") or result.get("finding_ids") or []),
                "chunk_id": str(record.get("chunk_id") or result.get("chunk_id") or ""),
                "script_error_explanation": str(
                    manifest.get("script_error_explanation")
                    or (result.get("script_assessment") or {}).get("error_explanation")
                    or result.get("summary")
                    or ""
                ),
                "manifest_path": str(record.get("replacement_manifest_path") or ""),
                "manifest_available": bool(manifest_path and manifest),
                "replacement_unavailable_explanation": str(
                    manifest.get("replacement_unavailable_explanation")
                    or result.get("replacement_unavailable_explanation")
                    or ""
                ),
                "checks": checks,
                "execution_history_count": len(events),
                "execution_history": events,
                "reconciliation": reconciliation,
            }
        )
    return {
        "schema_version": 1,
        "groups": groups,
        "summary": {
            "script_error_recheck_count": len(groups),
            "replacement_check_count": sum(len(item.get("checks") or []) for item in groups),
            "ready_check_count": sum(
                1
                for group in groups
                for check in group.get("checks") or []
                if (check.get("validation") or {}).get("status") == "ready"
            ),
            "executed_check_count": sum(
                1
                for group in groups
                for check in group.get("checks") or []
                if isinstance(check.get("latest_execution"), dict)
            ),
        },
    }


def run_verification_replacement_checks(
    session: dict[str, Any],
    recheck_id: str,
    check_ids: Optional[list[str]] = None,
    timeout: int = 120,
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    record = next(
        (
            item
            for item in verification_finding_rechecks_for_session(session)
            if str(item.get("recheck_id") or "") == str(recheck_id or "")
        ),
        None,
    )
    if record is None:
        raise ValueError(f"Canonical verification-finding recheck not found: {recheck_id}")
    result = record.get("structured_result") if isinstance(record.get("structured_result"), dict) else {}
    if str(result.get("recheck_outcome") or "") != "script_error":
        raise ValueError("Replacement execution is only available for canonical script_error rechecks.")
    manifest, manifest_path = _replacement_manifest_for_record(session, record)
    if not manifest or manifest_path is None:
        raise ValueError("This script-error recheck has no replacement manifest.")
    requested = {str(item).strip() for item in (check_ids or []) if str(item).strip()}
    checks = [item for item in manifest.get("replacement_checks") or [] if isinstance(item, dict)]
    if requested:
        checks = [item for item in checks if str(item.get("check_id") or "") in requested]
        missing = requested - {str(item.get("check_id") or "") for item in checks}
        if missing:
            raise ValueError("Unknown replacement check ID(s): " + ", ".join(sorted(missing)))
    if not checks:
        raise ValueError("No replacement checks were selected.")
    not_ready = [
        str(item.get("check_id") or "")
        for item in checks
        if (item.get("validation") or {}).get("status") != "ready"
    ]
    if not_ready:
        raise ValueError("Replacement checks are not safe/ready for execution: " + ", ".join(not_ready))

    workdir = Path(session["workdir"]).resolve()
    artifact_root = manifest_path.parent
    workspace = artifact_root / "replacement_execution_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    temp_session = dict(session)
    temp_session["workdir"] = str(workspace)
    event_path = (workdir / str(manifest.get("execution_events_path") or "")).resolve()
    event_path.parent.mkdir(parents=True, exist_ok=True)
    prior_events = _replacement_execution_events(session, manifest)
    canonical_prior = _canonical_replacement_executions(prior_events)
    attempts = []
    for check in checks:
        check_id = str(check.get("check_id") or "")
        script_path = (workdir / str(check.get("script_path") or "")).resolve()
        current_code = script_path.read_text(encoding="utf-8")
        current_validation = validate_verification_replacement_code(current_code)
        if current_validation.get("status") != "ready" or current_validation.get("code_sha256") != check.get("code_sha256"):
            raise ValueError(f"Replacement check {check_id} changed or no longer passes safe validation.")
        entry = {
            "chunk_id": str(manifest.get("chunk_id") or ""),
            "chunk_index": _chunk_index_from_chunk_id(str(manifest.get("chunk_id") or "")),
            "script_name": script_path.name,
            "script_path": str(script_path),
            "purpose": str(check.get("purpose") or ""),
            "description": str(check.get("correction_explanation") or ""),
            "expected_outcome": str(check.get("expected_check_kind") or ""),
        }
        bundle = _run_verification_scripts(
            temp_session,
            [entry],
            timeout=int(timeout),
            safe_only=True,
            progress_callback=progress_callback,
            replace_all_results=True,
        )
        executed = dict((bundle.get("executed_results") or [{}])[0])
        structured_execution = (
            executed.get("structured_result")
            if isinstance(executed.get("structured_result"), dict)
            else {}
        )
        emitted_check_kind = str(structured_execution.get("check_kind") or "").strip()
        expected_check_kind = str(check.get("expected_check_kind") or "").strip()
        if (
            executed.get("execution_status") == "completed"
            and emitted_check_kind
            and emitted_check_kind != expected_check_kind
        ):
            executed["execution_status"] = "parse_error"
            executed["mathematical_outcome"] = "inconclusive"
            executed["outcome_source"] = "replacement_contract_validation"
            executed["structured_result_error"] = (
                f"replacement emitted check_kind={emitted_check_kind!r}; expected {expected_check_kind!r}"
            )
            executed["status"] = _legacy_status_for_execution("parse_error")
            executed["conclusion"] = _infer_verification_conclusion(executed)
        attempt_id = f"replacement_attempt_{int(time.time() * 1000000)}_{check_id}"
        result_path = artifact_root / "replacement_results" / f"{attempt_id}.result.json"
        executed.update(
            {
                "origin": "verification_finding_recheck",
                "recheck_id": str(recheck_id),
                "replacement_attempt_id": attempt_id,
                "replacement_check_id": check_id,
                "relationship_to_original": check.get("relationship_to_original"),
                "replaces_script_semantically": [
                    str(item.get("script_name") or "") for item in manifest.get("originating_scripts") or []
                ],
                "tested_scope": check.get("tested_scope") or {},
                "result_path": str(result_path.resolve().relative_to(workdir)),
            }
        )
        save_json(result_path, executed)
        previous = canonical_prior.get(check_id)
        event = {
            "schema_version": 1,
            "time": utc_now(),
            "event": "execution_completed",
            "recheck_id": str(recheck_id),
            "replacement_attempt_id": attempt_id,
            "replacement_check_id": check_id,
            "supersedes_attempt_id": str((previous or {}).get("replacement_attempt_id") or "") or None,
            "result_path": str(result_path.resolve().relative_to(workdir)),
            "execution_status": executed.get("execution_status"),
            "mathematical_outcome": executed.get("mathematical_outcome"),
            "tested_scope": check.get("tested_scope") or {},
            "elapsed_seconds": executed.get("elapsed_seconds"),
            "local_execution": True,
            "api_cost_usd": 0.0,
        }
        append_jsonl(event_path, event)
        canonical_prior[check_id] = event
        attempts.append({"event": event, "result": executed})
    return {
        "workflow": "verification_replacement_execution",
        "recheck_id": str(recheck_id),
        "attempts": attempts,
        "replacement_check_runtime": sum(
            float((item.get("result") or {}).get("elapsed_seconds", 0.0) or 0.0) for item in attempts
        ),
        "replacement_check_execution_status": [
            str((item.get("result") or {}).get("execution_status") or "") for item in attempts
        ],
        "inventory": verification_replacement_check_inventory(session),
    }


def show_verification_status(pdf_path: str | Path) -> dict[str, Any]:
    session = load_session_from_pdf(pdf_path)
    if session is None:
        raise FileNotFoundError("No audit session found for this PDF.")
    state = load_verification_state(session)
    results = _load_verification_results(session, state=state)
    last_run = state.get("last_run") or {}
    if not last_run:
        print("No verification run found.")
        return {"session": session, "state": state, "results": results}
    print(f"Verification scripts: {last_run.get('scripts_total', len(results))}")
    summary = _verification_summary_counts(results)
    execution = summary.get("execution_summary") or {}
    outcomes = summary.get("mathematical_outcome_summary") or {}
    print(f"Execution completed: {execution.get('completed', 0)}")
    print(f"Runtime errors: {execution.get('runtime_error', 0)}")
    print(f"Timed out: {execution.get('timeout', 0)}")
    print(f"Skipped/unsafe: {execution.get('skipped', 0) + execution.get('unsafe', 0)}")
    print(f"Counterexamples found: {outcomes.get('counterexample_found', 0)}")
    print(f"Claims failed: {outcomes.get('claim_failed', 0)}")
    print(f"No counterexample found in tested scope: {outcomes.get('no_counterexample_found', 0)}")
    print(f"Checks satisfied: {outcomes.get('check_satisfied', 0)}")
    print(f"Inconclusive/not reported: {outcomes.get('inconclusive', 0) + outcomes.get('not_reported', 0)}")
    print(f"Timeout per script: {last_run.get('timeout_seconds', 0)}s")
    if last_run.get("finished_at"):
        print(f"Last run finished: {last_run.get('finished_at')}")
    failing = [item for item in results if verification_result_needs_technical_retry(item)]
    if failing:
        print("Failing scripts:")
        for item in failing:
            print(f"- {item.get('script_name')}: {item.get('conclusion')}")
    return {"session": session, "state": state, "results": results}


__all__ = [
    "VERIFICATION_RESULT_SENTINEL",
    "VERIFICATION_RESULT_SCHEMA_VERSION",
    "VERIFICATION_EXECUTION_STATUSES",
    "VERIFICATION_MATHEMATICAL_OUTCOMES",
    "NEGATIVE_VERIFICATION_OUTCOMES",
    "TECHNICAL_VERIFICATION_FAILURE_STATUSES",
    "VERIFICATION_FINDING_RECHECK_OUTCOMES",
    "CONCLUSIVE_VERIFICATION_FINDING_RECHECK_OUTCOMES",
    "VERIFICATION_FINDING_RECHECK_RESPONSE_SCHEMA",
    "load_verification_state",
    "save_verification_state",
    "_verification_results_path",
    "_ensure_verification_results_dir",
    "_verification_result_path",
    "_chunk_id_from_script_name",
    "_chunk_index_from_chunk_id",
    "_resolve_verification_script_path",
    "_collect_verification_scripts",
    "_ast_call_name",
    "_open_call_looks_dangerous",
    "_check_verification_script_safety",
    "_first_nonempty_line",
    "_truncate_text",
    "_infer_verification_conclusion",
    "_parse_verification_result_sentinel",
    "_parse_legacy_verification_stdout",
    "_normalize_verification_result",
    "_verification_summary_counts",
    "verification_findings_for_session",
    "append_verification_finding_recheck_event",
    "verification_finding_rechecks_for_session",
    "verification_finding_recheck_map",
    "apply_verification_finding_rechecks",
    "verification_finding_recheck_summary",
    "verification_finding_recheck_schema_errors",
    "validate_verification_finding_recheck_response",
    "validate_verification_replacement_code",
    "persist_verification_replacement_proposals",
    "verification_finding_recheck_candidates",
    "build_verification_finding_recheck_evidence",
    "build_verification_finding_recheck_prompt",
    "supersede_verification_findings_for_chunks",
    "verification_result_needs_technical_retry",
    "_load_verification_results",
    "_run_verification_scripts",
    "run_verification_suite",
    "rerun_failed_verification_scripts",
    "verification_replacement_check_inventory",
    "run_verification_replacement_checks",
    "show_verification_status",
]
