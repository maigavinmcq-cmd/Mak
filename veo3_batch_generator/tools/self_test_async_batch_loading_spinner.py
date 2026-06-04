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

    _class_node(tree, "BatchLoadWorker")
    main_window = _class_node(tree, "MainWindow")

    view_source = _method_source(source, main_window, "view_batch_by_id")
    assert "load_batch_async" in view_source
    assert "run_with_feedback" not in view_source

    async_source = _method_source(source, main_window, "load_batch_async")
    assert "BatchLoadWorker" in async_source
    assert ".start()" in async_source

    print("async batch loading spinner self-test passed")


if __name__ == "__main__":
    main()
