from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI_PATH = ROOT / "app" / "gui.py"
UI_LAYOUT_PATH = ROOT / "app" / "ui_layout.py"


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"missing class {name}")


def _method_source(source: str, class_node: ast.ClassDef, name: str) -> str:
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"missing method {class_node.name}.{name}")


def main() -> None:
    gui_source = GUI_PATH.read_text(encoding="utf-8-sig")
    tree = ast.parse(gui_source)
    log_dialog = _class_node(tree, "LogWorkbenchDialog")
    main_window = _class_node(tree, "MainWindow")

    dialog_init = _method_source(gui_source, log_dialog, "__init__")
    filter_panel = _method_source(gui_source, log_dialog, "_build_filter_panel")
    apply_filters = _method_source(gui_source, log_dialog, "apply_filters")
    build_log_box = _method_source(gui_source, main_window, "_build_log_box")
    apply_log_state = _method_source(gui_source, main_window, "_apply_log_panel_state")
    build_preview_panel = _method_source(gui_source, main_window, "_build_preview_panel")
    on_cell_clicked = _method_source(gui_source, main_window, "on_cell_clicked")
    update_detail = _method_source(gui_source, main_window, "update_detail_panel_for_task")

    for text in ["日志工作台", "错误聚合", "PID", "task_id", "关键词"]:
        assert text in dialog_init or text in filter_panel or text in apply_filters, f"log workbench missing {text}"
    assert "hide_diagnostics_check" in filter_panel
    assert "PERF" in apply_filters and "诊断" in filter_panel
    assert "open_log_workbench" in gui_source
    assert "copy_error_logs" in gui_source
    assert "log_workbench_btn" in build_log_box
    assert "log_compact_only_widgets" in build_log_box
    assert "log_expanded_only_widgets" in build_log_box
    assert "setVisible(collapsed)" in apply_log_state
    assert "setVisible(not collapsed)" in apply_log_state
    assert "_set_ui_log_entries_from_text" in gui_source
    assert "QTabWidget" in gui_source
    assert "task_log_preview_text" in build_preview_panel
    assert "任务日志" in build_preview_panel
    assert "task_logs_to_text(task)" in update_detail
    assert "log_text.append" not in on_cell_clicked, "clicking a task must not pollute the global runtime log"

    layout_source = UI_LAYOUT_PATH.read_text(encoding="utf-8-sig")
    assert '"log_panel_default_collapsed": True' in layout_source

    print("log workbench UI static self-test passed")


if __name__ == "__main__":
    main()
