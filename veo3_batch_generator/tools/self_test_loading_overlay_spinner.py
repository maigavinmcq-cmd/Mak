from __future__ import annotations

import ast
from pathlib import Path


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"missing class {name}")


def main() -> None:
    source = Path("app/gui.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)

    _class_node(tree, "CircularLoadingIndicator")
    overlay = _class_node(tree, "LoadingOverlay")
    overlay_source = ast.get_source_segment(source, overlay) or ""
    spinner = _class_node(tree, "CircularLoadingIndicator")
    spinner_source = ast.get_source_segment(source, spinner) or ""

    assert "CircularLoadingIndicator" in overlay_source
    assert "self.spinner" in overlay_source
    assert "self.progress = QProgressBar" not in overlay_source
    assert "def showEvent" in spinner_source
    assert "def hideEvent" in spinner_source
    assert "PreciseTimer" in spinner_source
    assert "_tick" in spinner_source
    assert "_angle" in spinner_source

    print("loading overlay spinner self-test passed")


if __name__ == "__main__":
    main()
