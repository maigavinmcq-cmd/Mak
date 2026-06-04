from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from light_image_tool.config import PROJECT_ROOT
from light_image_tool.window import LightImageToolWindow


def launch() -> None:
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    icon_path = PROJECT_ROOT / "assets" / "app_icon.ico"
    if Path(icon_path).exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = LightImageToolWindow()
    window.show()
    if owns_app:
        sys.exit(app.exec())
