from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.gui as gui_module
from app.gui import COL_INDEX, MainWindow


def _no_dialog(*_args, **_kwargs):
    return QMessageBox.No


def _ok_dialog(*_args, **_kwargs):
    return QMessageBox.Ok


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    QMessageBox.question = _no_dialog
    QMessageBox.information = _ok_dialog
    QMessageBox.warning = _ok_dialog
    gui_module.save_config = lambda *_args, **_kwargs: None

    window = MainWindow()
    try:
        window.resize(1500, 900)
        window._apply_responsive_layout(force=True)
        assert window._layout_mode in {"compact", "extra_compact"}

        target = "流程进度"
        assert not window.table.isColumnHidden(COL_INDEX[target])
        window.hide_column_by_name(target)

        assert target not in window.current_visible_columns()
        assert window.table.isColumnHidden(COL_INDEX[target])

        active_key = window._active_column_config_key()
        assert target not in window.config.task_table_columns[active_key]

        original_exec = QDialog.exec
        QDialog.exec = lambda self: QDialog.Accepted
        try:
            window.open_column_visibility_dialog()
        finally:
            QDialog.exec = original_exec
        assert window.config.task_table_columns[active_key] == window.current_visible_columns()

        window.resize(1920, 1080)
        window._apply_responsive_layout(force=True)
        assert window._layout_mode == "large"
        window._set_visible_columns_for_current_mode(["任务名称", "视频链接", "错误信息"])
        window.apply_column_visibility()
        assert window.current_visible_columns() == ["任务名称", "视频链接", "错误信息"]
        assert not window.table.isColumnHidden(COL_INDEX["视频链接"])
        assert window.table.isColumnHidden(COL_INDEX["流程进度"])
    finally:
        window.close()
        app.quit()

    print("self_test_field_visibility_ui: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
