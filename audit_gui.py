from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from gui_controller import GuiController
from gui_main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    controller = GuiController()
    window = MainWindow(controller)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
