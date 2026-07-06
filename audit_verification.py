from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from audit_state import load_json, load_session_from_pdf, save_json, session_paths, utc_now


_VERIFICATION_SCRIPT_RE = re.compile(r"^(chunk_\d+)_check_\d+\.py$")
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
        for raw_path in rec.get("python_paths", []) or []:
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


def _infer_verification_conclusion(result: dict[str, Any]) -> str:
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


def _verification_summary_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"scripts_total": len(results), "passed": 0, "failed": 0, "timeout": 0, "skipped": 0}
    for result in results:
        status = str(result.get("status") or "skipped")
        if status not in counts:
            counts[status] = 0
        counts[status] += 1
    return counts


def _load_verification_results(session: dict[str, Any], state: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    state = load_verification_state(session) if state is None else state
    results = []
    seen = set()
    for item in state.get("results", []) or []:
        result_path = item.get("result_path")
        if not result_path:
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
    results.sort(key=lambda item: (int(item.get("chunk_index") or _chunk_index_from_chunk_id(item.get("chunk_id", ""))), item.get("script_name", "")))
    return results


def _run_verification_scripts(
    session: dict[str, Any],
    script_entries: list[dict[str, Any]],
    timeout: int = 120,
    safe_only: bool = True,
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
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
            "skip_reason": "",
            "conclusion": "",
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
                    result["status"] = "passed" if completed.returncode == 0 else "failed"
                except subprocess.TimeoutExpired as e:
                    result["stdout"] = e.stdout or ""
                    result["stderr"] = e.stderr or ""
                    result["status"] = "timeout"
                except Exception as e:
                    result["stderr"] = repr(e)
                    result["status"] = "failed"
        result["elapsed_seconds"] = max(0.0, time.time() - start)
        if result["skip_reason"]:
            result["status"] = "skipped"
        result["conclusion"] = _infer_verification_conclusion(result)
        result_path = _verification_result_path(session, script_name or f"script_{len(results)+1:03d}.py")
        result["result_path"] = str(result_path)
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
            conclusion=result.get("conclusion"),
        )
    summary = _verification_summary_counts(results)
    state = load_verification_state(session)
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
            "returncode": result.get("returncode"),
            "elapsed_seconds": result.get("elapsed_seconds"),
            "conclusion": result.get("conclusion"),
        }
        for result in results
    ]
    save_verification_state(session, state)
    return {"session": session, "results": results, "summary": summary, "state": state}


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
        if item.get("status") in {"failed", "timeout"}
    }
    failed_names.discard("")
    if not failed_names:
        results = _load_verification_results(session, state=state)
        return {"session": session, "results": results, "summary": _verification_summary_counts(results), "state": state}
    scripts = _collect_verification_scripts(session, only_script_names=failed_names)
    return _run_verification_scripts(session, scripts, timeout=int(timeout), safe_only=bool(safe_only))


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
    print(f"Passed: {last_run.get('passed', 0)}")
    print(f"Failed: {last_run.get('failed', 0)}")
    print(f"Timed out: {last_run.get('timeout', 0)}")
    print(f"Skipped: {last_run.get('skipped', 0)}")
    print(f"Timeout per script: {last_run.get('timeout_seconds', 0)}s")
    if last_run.get("finished_at"):
        print(f"Last run finished: {last_run.get('finished_at')}")
    failing = [item for item in results if item.get("status") in {"failed", "timeout"}]
    if failing:
        print("Failing scripts:")
        for item in failing:
            print(f"- {item.get('script_name')}: {item.get('conclusion')}")
    return {"session": session, "state": state, "results": results}


__all__ = [
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
    "_verification_summary_counts",
    "_load_verification_results",
    "_run_verification_scripts",
    "run_verification_suite",
    "rerun_failed_verification_scripts",
    "show_verification_status",
]
