from __future__ import annotations

import os
import signal
import shutil
import sys
from pathlib import Path


def _print_startup_import_error(exc: BaseException) -> None:
    print(
        "Math Audit GUI could not start because a required GUI dependency is missing.\n"
        f"{type(exc).__name__}: {exc}\n\n"
        "Try recreating or updating the environment with:\n"
        "  conda env update -f environment.yml --prune\n\n"
        "If the error mentions QtWebEngine, make sure the PySide6 WebEngine package is installed.",
        file=sys.stderr,
    )


def _startup_check_messages(project_root: Path) -> list[str]:
    messages: list[str] = []

    if os.environ.get("OPENAI_API_KEY"):
        messages.append(
            "Startup check: OPENAI_API_KEY is set. The GUI still keeps its API key field explicit; "
            "paste the key there before live audit/discussion calls if needed."
        )
    else:
        messages.append(
            "Startup check: OPENAI_API_KEY is not set. Paste an API key into the GUI before live audit/discussion calls."
        )

    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except Exception as exc:
        messages.append(
            "Startup warning: PySide6 Qt WebEngine is unavailable; rendered discussion output may not work. "
            f"{type(exc).__name__}: {exc}"
        )

    mathjax_path = project_root / "gui_assets" / "mathjax" / "es5" / "tex-mml-svg.js"
    mathjax_font_script = (
        project_root
        / "gui_assets"
        / "mathjax-fonts"
        / "mathjax-newcm-font"
        / "svg"
        / "dynamic"
        / "script.js"
    )
    missing_mathjax = [path for path in (mathjax_path, mathjax_font_script) if not path.exists()]
    if missing_mathjax:
        missing = ", ".join(str(path.relative_to(project_root)) for path in missing_mathjax)
        messages.append(
            "Startup warning: local MathJax assets are incomplete; Rendered discussion math may be unavailable. "
            f"Missing: {missing}"
        )

    if shutil.which("pdflatex") is None:
        messages.append(
            "Startup note: pdflatex was not found on PATH. The app can still generate reports, "
            "but compiling TeX reports requires a local TeX installation."
        )

    if not os.access(project_root, os.W_OK):
        messages.append(
            f"Startup warning: project folder is not writable: {project_root}. "
            "Prompt profiles and local GUI settings may not save correctly."
        )

    return messages


def main() -> int:
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        from gui_controller import GuiController
        from gui_main_window import MainWindow
    except Exception as exc:
        _print_startup_import_error(exc)
        return 1

    project_root = Path(__file__).resolve().parent
    app = QApplication(sys.argv)
    controller = GuiController()
    window = MainWindow(controller)
    window.show()

    def request_interrupt_shutdown(_signum, _frame) -> None:
        controller.log_message.emit("Keyboard interrupt received; requesting GUI shutdown.")
        QTimer.singleShot(0, window.close)

    signal.signal(signal.SIGINT, request_interrupt_shutdown)

    def emit_startup_checks() -> None:
        for message in _startup_check_messages(project_root):
            controller.log_message.emit(message)

    QTimer.singleShot(0, emit_startup_checks)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
