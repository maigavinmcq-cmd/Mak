from __future__ import annotations

import ast
from pathlib import Path


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
    gui_source = Path("app/gui.py").read_text(encoding="utf-8-sig")
    gui_tree = ast.parse(gui_source)
    main_window = _class_node(gui_tree, "MainWindow")

    fill_row = _method_source(gui_source, main_window, "_fill_row")
    assert 'getattr(task, "task_name"' in fill_row
    assert fill_row.index('getattr(task, "task_name"') < fill_row.index("task.pid")
    assert "product_image_filename" in fill_row
    assert fill_row.index("product_image_filename") < fill_row.index("self._summary(task.image_prompt)")

    flow_summary = _function_source(gui_source, gui_tree, "task_flow_summary")
    flow_stage_label = _function_source(gui_source, gui_tree, "_flow_stage_label")
    assert "_flow_stage_label" in flow_summary
    assert "FLOW_STAGE_LABELS" in flow_stage_label
    assert "workflow_stage_status_label" in flow_summary
    assert "stage_status_icon" not in flow_summary
    assert "circled" not in flow_summary

    executor_source = Path("app/workflow_executor.py").read_text(encoding="utf-8-sig")
    for marker in [
        "self._log(\"INFO\", f\"[{node_id}] PID={task.pid} row={task.row_index} start image generation\"",
        "self._log(\"INFO\", f\"[{node_id}] PID={task.pid} row={task.row_index} submit video task\"",
    ]:
        before = executor_source[: executor_source.index(marker)]
        window = before[-450:]
        assert "state[\"status\"] = NODE_STATUS_RUNNING" in window
        assert "self._save_emit(task, force=False)" in window

    print("task table status and flow text self-test passed")


if __name__ == "__main__":
    main()
