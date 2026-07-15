#!/usr/bin/env python3
from __future__ import annotations

import importlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.check_windows_qt import (  # noqa: E402
    WINDOWS_TESTED_PYSIDE6_VERSION,
    format_windows_qt_preflight,
    run_windows_qt_preflight,
    windows_qt_repair_command_text,
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    required: bool = True


def _ok(name: str, detail: str, required: bool = True) -> CheckResult:
    return CheckResult(name=name, status="OK", detail=detail, required=required)


def _warn(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, status="WARN", detail=detail, required=False)


def _fail(name: str, detail: str, required: bool = True) -> CheckResult:
    return CheckResult(name=name, status="FAIL", detail=detail, required=required)


def _check_python_version() -> CheckResult:
    version = sys.version_info
    detail = f"{version.major}.{version.minor}.{version.micro}"
    if version < (3, 9):
        return _fail("Python version", f"{detail}; expected Python 3.9 or newer")
    if version < (3, 11):
        return _warn("Python version", f"{detail}; environment.yml recommends Python 3.11")
    return _ok("Python version", f"{detail}; OK")


def _check_import(name: str, module: str, required: bool = True) -> CheckResult:
    try:
        imported = importlib.import_module(module)
    except Exception as exc:
        detail = f"could not import {module}: {type(exc).__name__}: {exc}"
        return _fail(name, detail, required=required) if required else _warn(name, detail)

    version = getattr(imported, "__version__", None)
    detail = f"imported {module}"
    if version:
        detail += f" ({version})"
    return _ok(name, detail, required=required)


def _check_windows_qt() -> CheckResult:
    result = run_windows_qt_preflight()
    if result["ok"]:
        versions = result["installed_versions"]
        return _ok(
            "Windows Qt preflight",
            f"PySide6 {versions.get('PySide6')} / Qt {result.get('qt_version')}; "
            f"Windows {result.get('windows_architecture')}, Python {result.get('python_architecture')}",
        )

    detail = format_windows_qt_preflight(result)
    detail += (
        "\nPySide6 could not provide a compatible Qt installation on Windows. "
        f"This app currently tests PySide6 {WINDOWS_TESTED_PYSIDE6_VERSION} on Windows.\n"
        "The Microsoft Visual C++ Redistributable x64 may be required. If it is already "
        "installed and the error persists, reinstall the tested PySide6 version. All "
        "PySide6, Addons, Essentials, and shiboken6 packages must have matching versions.\n"
        f"Try: {windows_qt_repair_command_text()}"
    )
    return _fail("Windows Qt preflight", detail)


def _check_mathjax_assets() -> CheckResult:
    required_assets = [
        PROJECT_ROOT / "gui_assets" / "mathjax" / "es5" / "tex-mml-svg.js",
        PROJECT_ROOT
        / "gui_assets"
        / "mathjax-fonts"
        / "mathjax-newcm-font"
        / "svg"
        / "dynamic"
        / "script.js",
    ]
    missing = [path.relative_to(PROJECT_ROOT) for path in required_assets if not path.exists()]
    if missing:
        return _warn(
            "Local MathJax assets",
            "missing assets for rendered discussion math: " + ", ".join(str(path) for path in missing),
        )
    return _ok("Local MathJax assets", "found bundled MathJax SVG bundle and font loader", required=False)


def _check_openai_key() -> CheckResult:
    if os.environ.get("OPENAI_API_KEY"):
        return _ok("OPENAI_API_KEY", "set in environment", required=False)
    return _warn("OPENAI_API_KEY", "not set; paste an API key into the GUI before live audit/discussion calls")


def _check_pdflatex() -> CheckResult:
    path = shutil.which("pdflatex")
    if path:
        return _ok("pdflatex", f"found at {path}", required=False)
    return _warn("pdflatex", "not found on PATH; TeX reports can be generated but not compiled locally")


def _check_project_writable() -> CheckResult:
    if os.access(PROJECT_ROOT, os.W_OK):
        return _ok("Project directory writable", str(PROJECT_ROOT), required=False)
    return _warn(
        "Project directory writable",
        f"{PROJECT_ROOT} is not writable; prompt profiles or local GUI settings may not save",
    )


def run_checks() -> list[CheckResult]:
    results: list[CheckResult] = [_check_python_version()]

    third_party = [
        ("OpenAI SDK", "openai"),
        ("PyMuPDF", "fitz"),
        ("pypdf", "pypdf"),
        ("Markdown", "markdown"),
        ("NumPy", "numpy"),
        ("SymPy", "sympy"),
    ]
    results.extend(_check_import(name, module) for name, module in third_party)
    if sys.platform == "win32":
        results.append(_check_windows_qt())
    else:
        results.extend(
            _check_import(name, module)
            for name, module in [
                ("PySide6 QtCore", "PySide6.QtCore"),
                ("PySide6 QtWidgets", "PySide6.QtWidgets"),
                ("PySide6 QtWebEngineWidgets", "PySide6.QtWebEngineWidgets"),
            ]
        )

    core_modules = [
        "audit_gui",
        "audit_models",
        "audit_state",
        "audit_prompts",
        "audit_chunking",
        "audit_verification",
        "audit_runtime",
        "audit_hooks",
        "audit_policy_hooks",
        "gui_controller",
        "gui_main_window",
    ]
    results.extend(_check_import(f"Project module {module}", module) for module in core_modules)

    results.extend(
        [
            _check_mathjax_assets(),
            _check_openai_key(),
            _check_pdflatex(),
            _check_project_writable(),
        ]
    )
    return results


def print_results(results: list[CheckResult]) -> None:
    print("Math Audit setup smoke check")
    print(f"Project root: {PROJECT_ROOT}")
    print()
    for result in results:
        requirement = "required" if result.required else "optional"
        print(f"[{result.status:4}] {result.name} ({requirement})")
        print(f"       {result.detail}")

    fatal_failures = [result for result in results if result.required and result.status == "FAIL"]
    warnings = [result for result in results if result.status == "WARN"]
    print()
    if fatal_failures:
        print(f"Result: FAILED ({len(fatal_failures)} required check(s) failed, {len(warnings)} warning(s)).")
        print("Fix the required failures before launching the GUI.")
    else:
        print(f"Result: OK ({len(warnings)} warning(s)).")
        print("Required setup checks passed. Optional warnings may limit some features.")


def main() -> int:
    results = run_checks()
    print_results(results)
    return 1 if any(result.required and result.status == "FAIL" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
