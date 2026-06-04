"""Static checks that stale RUNNING batch cards stay UI-thread safe.

Run as:
    python tools/self_test_stale_running_batch_status_static.py
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "app" / "gui.py"


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
    source = GUI.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    main_window = _class_node(tree, "MainWindow")
    init_state = _method_source(source, main_window, "_load_initial_state")
    refresh_combo = _method_source(source, main_window, "refresh_batch_combo")
    refresh_table = _method_source(source, main_window, "_refresh_batch_table_impl")
    reconcile = _method_source(source, main_window, "reconcile_stale_runtime_state")
    status_summary = _method_source(source, main_window, "_update_status_summary_impl")

    assert "def reconcile_stale_runtime_state(" in source
    assert "def stale_inactive_batch_status(" in source
    assert "def display_batch_for_runtime_state(" in source
    assert "reconcile_stale_runtime_state(force=True)" in init_state
    assert "reconcile_stale_runtime_state(" not in refresh_combo
    assert "reconcile_stale_runtime_state(" not in refresh_table
    assert "TaskManager(" not in reconcile
    assert "load_state(" not in reconcile
    assert "update_batch(" not in reconcile
    assert "save_batch(" not in reconcile
    assert "upsert_index(" not in reconcile
    assert "display_batch_for_runtime_state(" in refresh_combo
    assert "display_batch_for_runtime_state(" in refresh_table
    assert "self.is_background_running()" in status_summary
    assert "is_running = batch.batch_id == self.active_running_batch_id and self.is_background_running()" in refresh_table
    assert "status_override=status" in source
    print("stale running batch status static self-test passed")


if __name__ == "__main__":
    main()
