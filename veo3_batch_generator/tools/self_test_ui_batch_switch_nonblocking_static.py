from __future__ import annotations

import ast
from pathlib import Path


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
    source = Path("app/gui.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    main_window = _class_node(tree, "MainWindow")

    init_src = _method_source(source, main_window, "__init__")
    assert "_batch_switch_in_progress" in init_src
    assert "_suspend_live_refresh_until" in init_src
    assert "_last_full_table_refresh" in init_src
    assert "_last_batch_snapshot_write" in init_src

    load_async = _method_source(source, main_window, "load_batch_async")
    assert "_batch_switch_in_progress = True" in load_async
    assert "_suspend_live_refresh_until" in load_async
    assert "_table_refresh_pending = False" in load_async

    apply_payload = _method_source(source, main_window, "apply_loaded_batch_payload")
    assert "_table_refresh_pending = False" in apply_payload
    assert "_batch_switch_in_progress = False" in apply_payload
    assert "_last_full_table_refresh" in apply_payload

    flush = _method_source(source, main_window, "flush_deferred_ui_updates")
    assert "_batch_switch_in_progress" in flush
    assert "_suspend_live_refresh_until" in flush
    assert "_last_full_table_refresh" in flush
    assert "_last_batch_snapshot_write" in flush

    snapshot = _method_source(source, main_window, "refresh_running_batch_snapshot")
    assert "batch_manager.update_batch" not in snapshot
    assert "queue_batch_update" in snapshot

    print("ui batch switch nonblocking static self-test passed")


if __name__ == "__main__":
    main()
