#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import platform
import struct
import sys
from importlib import metadata
from typing import Any, Callable


WINDOWS_TESTED_PYSIDE6_VERSION = "6.9.3"
WINDOWS_QT_PACKAGES = (
    "PySide6",
    "PySide6_Addons",
    "PySide6_Essentials",
    "shiboken6",
)
PREFLIGHT_OK = 0
PREFLIGHT_DLL_LOAD_FAILED = 10
PREFLIGHT_IMPORT_FAILED = 11
PREFLIGHT_VERSION_MISMATCH = 12


def installed_qt_package_versions(
    version_getter: Callable[[str], str] = metadata.version,
) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package in WINDOWS_QT_PACKAGES:
        try:
            versions[package] = version_getter(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
        except Exception:
            versions[package] = None
    return versions


def windows_qt_repair_command(
    environment_tool: str = "conda",
    environment_name: str = "math-audit",
) -> list[str]:
    return [
        environment_tool,
        "run",
        "-n",
        environment_name,
        "python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--force-reinstall",
        f"PySide6=={WINDOWS_TESTED_PYSIDE6_VERSION}",
    ]


def windows_qt_repair_command_text(
    environment_tool: str = "conda",
    environment_name: str = "math-audit",
) -> str:
    return " ".join(windows_qt_repair_command(environment_tool, environment_name))


def _matching_package_versions(versions: dict[str, str | None]) -> bool:
    present = [versions.get(package) for package in WINDOWS_QT_PACKAGES]
    return all(present) and len(set(present)) == 1


def run_windows_qt_preflight(
    *,
    import_module: Callable[[str], Any] = importlib.import_module,
    version_getter: Callable[[str], str] = metadata.version,
    machine: str | None = None,
    python_bits: int | None = None,
) -> dict[str, Any]:
    versions = installed_qt_package_versions(version_getter)
    imports: dict[str, dict[str, str | bool]] = {}
    qt_version = ""
    first_error = ""
    dll_load_failure = False

    components = (
        ("QtCore", "PySide6.QtCore", "qVersion"),
        ("QtWidgets", "PySide6.QtWidgets", "QApplication"),
        ("QtWebEngineWidgets", "PySide6.QtWebEngineWidgets", "QWebEngineView"),
    )
    for label, module_name, required_attribute in components:
        try:
            module = import_module(module_name)
            attribute = getattr(module, required_attribute)
            if label == "QtCore":
                qt_version = str(attribute())
            imports[label] = {"ok": True, "detail": f"imported {module_name}"}
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            imports[label] = {"ok": False, "detail": detail}
            if not first_error:
                first_error = detail
            if isinstance(exc, ImportError) and "dll load failed" in str(exc).lower():
                dll_load_failure = True

    imports_ok = len(imports) == len(components) and all(
        bool(item.get("ok")) for item in imports.values()
    )
    versions_match = _matching_package_versions(versions)
    if imports_ok and versions_match:
        exit_code = PREFLIGHT_OK
    elif not versions_match:
        exit_code = PREFLIGHT_VERSION_MISMATCH
    elif dll_load_failure:
        exit_code = PREFLIGHT_DLL_LOAD_FAILED
    else:
        exit_code = PREFLIGHT_IMPORT_FAILED

    return {
        "ok": exit_code == PREFLIGHT_OK,
        "exit_code": exit_code,
        "tested_pyside6_version": WINDOWS_TESTED_PYSIDE6_VERSION,
        "installed_versions": versions,
        "versions_match": versions_match,
        "qt_version": qt_version,
        "windows_architecture": machine or platform.machine() or "unknown",
        "python_architecture": f"{python_bits or struct.calcsize('P') * 8}-bit",
        "imports": imports,
        "dll_load_failure": dll_load_failure,
        "error": first_error,
        "repair_recommended": exit_code in {
            PREFLIGHT_DLL_LOAD_FAILED,
            PREFLIGHT_VERSION_MISMATCH,
        },
    }


def format_windows_qt_preflight(result: dict[str, Any]) -> str:
    versions = result.get("installed_versions") or {}
    lines = [
        "Windows Qt preflight",
        f"Windows architecture: {result.get('windows_architecture') or 'unknown'}",
        f"Python architecture: {result.get('python_architecture') or 'unknown'}",
        "Installed Qt-for-Python packages:",
    ]
    for package in WINDOWS_QT_PACKAGES:
        lines.append(f"  {package}: {versions.get(package) or 'not installed'}")
    for label in ("QtCore", "QtWidgets", "QtWebEngineWidgets"):
        item = (result.get("imports") or {}).get(label)
        if not item:
            lines.append(f"[SKIP] {label}: not attempted after an earlier import failure")
        elif item.get("ok"):
            lines.append(f"[OK] {label}: {item.get('detail')}")
        else:
            lines.append(f"[FAIL] {label}: {item.get('detail')}")

    if result.get("ok"):
        pyside_version = versions.get("PySide6") or "unknown"
        lines.append(
            f"Windows Qt preflight passed: PySide6 {pyside_version} / "
            f"Qt {result.get('qt_version') or 'unknown'}."
        )
    else:
        lines.append("Windows Qt preflight failed.")
        if not result.get("versions_match"):
            lines.append(
                "The PySide6, Addons, Essentials, and shiboken6 package versions "
                "are missing or do not match."
            )
        if result.get("dll_load_failure"):
            lines.append(
                "PySide6 could not load its Qt DLLs. This can have several causes; "
                "the tested recovery path is PySide6 6.9.3."
            )
        lines.append("Repair command: " + windows_qt_repair_command_text())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Windows Qt/PySide6 imports.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)
    result = run_windows_qt_preflight()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(format_windows_qt_preflight(result))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
