from __future__ import annotations

import ast
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _class_node(tree: ast.AST, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found")


def _method_source(class_node: ast.ClassDef, source: str, name: str) -> str:
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment, f"source segment missing for {name}"
            return segment
    raise AssertionError(f"method {name} not found")


def main() -> None:
    gui_path = PROJECT_ROOT / "app" / "gui.py"
    worker_path = PROJECT_ROOT / "app" / "worker.py"
    source = gui_path.read_text(encoding="utf-8-sig")
    worker_source = worker_path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    worker_tree = ast.parse(worker_source)
    main_window = _class_node(tree, "MainWindow")
    manual_worker = _class_node(worker_tree, "ManualPollWorker")

    manual_poll = _method_source(main_window, source, "manual_poll_batch")
    assert "self.manual_poll_loading_state = None" in manual_poll
    assert "self.manual_poll_worker.start()" in manual_poll
    assert "self.end_loading(loading_state" in manual_poll, "manual poll overlay must be released immediately after startup"

    event_poll = _method_source(main_window, source, "poll_process_worker_events")
    assert "max_events=80" in event_poll
    assert "max_bytes=1_000_000" in event_poll
    assert "self.handle_process_worker_events(events)" in event_poll

    batch_handler = _method_source(main_window, source, "handle_process_worker_events")
    assert "latest_task_updates" in batch_handler
    assert "latest_stats" in batch_handler
    assert "finish_process_worker" in batch_handler

    flush = _method_source(main_window, source, "_flush_deferred_ui_updates_impl")
    assert "self._pending_log_lines[:200]" in flush, "log flush should be chunked to avoid UI stalls"

    manual_init = _method_source(manual_worker, worker_source, "__init__")
    assert "_state_save_interval_seconds" in manual_init
    assert "_emit_min_interval_seconds" in manual_init
    manual_save = _method_source(manual_worker, worker_source, "_save_emit")
    assert "now - self._last_state_save_time >= self._state_save_interval_seconds" in manual_save
    assert "now - last < self._emit_min_interval_seconds" in manual_save

    print("self_test_ui_background_feedback_static: PASS")


if __name__ == "__main__":
    main()
