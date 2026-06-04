from __future__ import annotations

import ast
import sys
import threading
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import AppConfig
from app.task_manager import TaskManager
from app.worker import BatchWorker


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
    source = Path("app/worker.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    worker = _class_node(tree, "BatchWorker")

    request_poll_cycle = _method_source(source, worker, "request_poll_cycle")
    assert "_manual_poll_now_requested" in request_poll_cycle, "manual poll must be tracked separately from internal wakeups"

    wait_helper = _method_source(source, worker, "_wait_for_next_poll_interval")
    assert "_consume_manual_poll_now_request" in wait_helper
    assert "_poll_now_event.wait" in wait_helper

    for name in ("_poll_loop", "_poll_loop_workflow"):
        method = _method_source(source, worker, name)
        assert "self._wait_for_next_poll_interval(interval)" in method, f"{name} must keep automatic polling on the configured interval"
        assert "self._poll_now_event.wait(timeout=interval)" not in method, f"{name} must not let internal wakeups create partial polling sweeps"

    poll_cycle = _method_source(source, worker, "_poll_cycle_workflow")
    assert "for task in self.manager.tasks" in poll_cycle
    assert "executor.find_polling_nodes(task)" in poll_cycle
    assert "pool.submit(self._poll_one_workflow_node, task, node)" in poll_cycle

    runtime_worker = BatchWorker(
        TaskManager(Path("outputs") / "_self_test_auto_poll_state.json"),
        AppConfig(poll_interval_seconds=20),
        None,
    )
    start = time.monotonic()
    threading.Timer(0.05, runtime_worker._poll_now_event.set).start()
    runtime_worker._wait_for_next_poll_interval(0.25)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.20, "internal wakeups from submit/driver completion must not create tiny auto-poll sweeps"

    start = time.monotonic()
    threading.Timer(0.05, runtime_worker.request_poll_cycle).start()
    runtime_worker._wait_for_next_poll_interval(0.25)
    elapsed = time.monotonic() - start
    assert elapsed < 0.20, "manual poll requests must still trigger an immediate sweep"

    print("auto poll full-sweep static self-test passed")


if __name__ == "__main__":
    main()
