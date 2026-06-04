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
    snapshot = _method_source(source, main_window, "refresh_running_batch_snapshot")

    assert "return" in snapshot, "refresh_running_batch_snapshot should return early when no batch is running"
    assert "if not self.active_running_batch_id" in snapshot, "idle timer must not refresh batch cards when no batch is running"
    assert "queue_batch_update" in snapshot, "running batches should update the active card through queued stats"
    assert "self._batch_refresh_pending = True" not in snapshot, "running snapshots must not rebuild the full batch card list"
    idle_guard = snapshot.split("if not self.active_running_batch_id", 1)[-1].split("return", 1)[0]
    assert "_batch_refresh_pending = True" not in idle_guard, "idle path must not mark batch cards for refresh"

    queue_update = _method_source(source, main_window, "queue_batch_update")
    assert "_pending_batch_updates" in queue_update
    assert "_batch_refresh_pending = True" not in queue_update, "queued running stats must not trigger a full batch list reload"


if __name__ == "__main__":
    main()
