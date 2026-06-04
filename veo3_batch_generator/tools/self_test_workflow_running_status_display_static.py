from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI_PATH = ROOT / "app" / "gui.py"


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"missing class {name}")


def _function_source(source: str, tree: ast.Module, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"missing function {name}")


def _method_source(source: str, class_node: ast.ClassDef, name: str) -> str:
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"missing method {class_node.name}.{name}")


def main() -> None:
    source = GUI_PATH.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    display_function = _function_source(source, tree, "task_overall_status_display")
    main_window = _class_node(tree, "MainWindow")
    fill_row = _method_source(source, main_window, "_fill_row")
    field_value = _method_source(source, main_window, "task_field_value")

    assert "TaskStatus.WORKFLOW_RUNNING" in display_function
    assert "等待视频结果" in display_function
    assert "等待视频提交" in display_function
    assert "等待图片结果" in display_function
    assert "等待补充输入" in display_function
    assert "task_overall_status_display(task, stage_nodes)" in fill_row
    assert "task_overall_status_display(task, stage_nodes)" in field_value

    print("workflow running status display static self-test passed")


if __name__ == "__main__":
    main()
