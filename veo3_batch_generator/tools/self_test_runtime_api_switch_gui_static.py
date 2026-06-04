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

    secondary = _method_source(source, main_window, "_build_secondary_actions")
    assert "switch_api_btn" in secondary
    assert "switch_batch_api_by_id" in secondary

    context_menu = _method_source(source, main_window, "show_batch_context_menu")
    assert "切换该批次 API" in context_menu
    assert "switch_batch_api_by_id" in context_menu

    switch_method = _method_source(source, main_window, "switch_batch_api_by_id")
    assert "pending_api_switch_request" in switch_method
    assert "stop_worker" in switch_method
    assert "_schedule_fast_process_worker_shutdown_for_api_switch" in switch_method
    assert "apply_runtime_api_switch" in switch_method

    shutdown_method = _method_source(source, main_window, "_schedule_fast_process_worker_shutdown_for_api_switch")
    assert "pending_api_switch_request" in shutdown_method
    assert "QTimer.singleShot" in shutdown_method
    assert ".terminate()" in shutdown_method
    assert ".kill()" in shutdown_method

    finish_method = _method_source(source, main_window, "worker_finished")
    assert "pending_api_switch_request" in finish_method
    assert finish_method.index("pending_api_switch_request") < finish_method.index("pending_start_request")

    apply_method = _method_source(source, main_window, "apply_runtime_api_switch")
    assert "apply_runtime_provider_switch" in apply_method
    assert "manager.save_state" in apply_method
    assert "restart" in apply_method

    print("runtime API switch GUI static self-test passed")


if __name__ == "__main__":
    main()
