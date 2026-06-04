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
    status_src = _method_source(source, main_window, "_update_status_summary_impl")

    assert "self.batch_card_batches" in init_src, "batch_card_batches must exist before _build_ui builds the status bar"
    assert init_src.index("self.batch_card_batches") < init_src.index("self._build_ui()"), "batch_card_batches must be initialized before _build_ui()"
    assert "getattr(self, \"batch_card_batches\", {})" in status_src, "status summary must tolerate early startup before batch cards exist"


if __name__ == "__main__":
    main()
