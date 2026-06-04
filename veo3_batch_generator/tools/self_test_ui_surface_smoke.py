from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.gui as gui_module
from app.gui import MainWindow, TABLE_HEADERS


def _no_dialog(*_args, **_kwargs):
    return QMessageBox.No


def _ok_dialog(*_args, **_kwargs):
    return QMessageBox.Ok


def _close_dialogs(app: QApplication, window: MainWindow) -> None:
    for widget in app.topLevelWidgets():
        if widget is not window and isinstance(widget, QDialog):
            widget.close()


def _assert_visible_columns_are_valid(window: MainWindow) -> None:
    visible = window.current_visible_columns()
    assert visible, "task table should keep at least one visible column"
    assert set(visible).issubset(set(TABLE_HEADERS)), visible
    hidden_mismatch = [
        header
        for index, header in enumerate(TABLE_HEADERS)
        if window.table.isColumnHidden(index) == (header in visible)
    ]
    assert not hidden_mismatch, hidden_mismatch


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    QMessageBox.question = _no_dialog
    QMessageBox.information = _ok_dialog
    QMessageBox.warning = _ok_dialog
    gui_module.save_config = lambda *_args, **_kwargs: None

    window = MainWindow()
    try:
        for attr in [
            "main_splitter",
            "batch_panel",
            "table",
            "preview_panel",
            "log_panel",
            "field_visibility_btn",
            "filter_popup_btn",
            "group_popup_btn",
            "sort_popup_btn",
            "current_batch_status_label",
        ]:
            assert hasattr(window, attr), attr

        window.resize(1920, 1080)
        window._apply_responsive_layout(force=True)
        assert window._layout_mode == "large"
        window.apply_column_visibility()
        _assert_visible_columns_are_valid(window)

        window.resize(1500, 900)
        window._apply_responsive_layout(force=True)
        assert window._layout_mode in {"compact", "extra_compact"}
        window.apply_column_visibility()
        _assert_visible_columns_are_valid(window)

        window._set_visible_columns_for_current_mode(["任务名称", "任务状态", "错误信息"])
        window.apply_column_visibility()
        assert window.current_visible_columns() == ["任务名称", "任务状态", "错误信息"]
        _assert_visible_columns_are_valid(window)

        window.show_filter_popup(window.filter_popup_btn)
        app.processEvents()
        _close_dialogs(app, window)
        window.show_group_popup(window.group_popup_btn)
        app.processEvents()
        _close_dialogs(app, window)
        window.show_sort_popup(window.sort_popup_btn)
        app.processEvents()
        _close_dialogs(app, window)
    finally:
        _close_dialogs(app, window)
        window.close()
        app.quit()

    print("self_test_ui_surface_smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
