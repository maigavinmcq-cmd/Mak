from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
    gui_source = (ROOT / "app" / "gui.py").read_text(encoding="utf-8-sig")
    gui_tree = ast.parse(gui_source)
    main_window = _class_node(gui_tree, "MainWindow")

    stop_worker = _method_source(gui_source, main_window, "stop_worker")
    force_shutdown = _method_source(gui_source, main_window, "_schedule_process_worker_force_shutdown")

    process_branch = stop_worker[
        stop_worker.index("if self.is_process_worker_running():") : stop_worker.index("if self.worker:")
    ]
    assert 'send_process_worker_command("stop")' in process_branch
    assert "_schedule_process_worker_force_shutdown(" in process_branch, (
        "user stop must schedule a hard process shutdown fallback; a worker stuck in "
        "requests/download/network-IO cannot always observe the JSONL stop command promptly"
    )
    assert "process.terminate()" in force_shutdown, "first fallback should terminate the background worker process"
    assert "process.kill()" in force_shutdown, "second fallback should kill the process if terminate is ignored"
    assert "QTimer.singleShot" in force_shutdown, "fallback must be delayed so normal cooperative stop can save state first"
    assert "force_shutdown_reason" in gui_source, "shutdown callbacks must be scoped to the current stop request"

    print("user stop force process shutdown static check passed")


if __name__ == "__main__":
    main()
