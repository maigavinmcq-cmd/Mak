from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.gui import MainWindow


def _no_dialog(*_args, **_kwargs):
    return QMessageBox.No


def _ok_dialog(*_args, **_kwargs):
    return QMessageBox.Ok


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    QMessageBox.question = _no_dialog
    QMessageBox.information = _ok_dialog
    QMessageBox.warning = _ok_dialog
    window = MainWindow()
    window.close()
    app.quit()
    print("mainwindow startup smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
