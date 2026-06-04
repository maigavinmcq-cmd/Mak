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


def _method_node(class_node: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"method {name} not found")


def _source(method: ast.FunctionDef, source: str) -> str:
    segment = ast.get_source_segment(source, method)
    assert segment, f"source segment missing for {method.name}"
    return segment


def main() -> None:
    worker_path = PROJECT_ROOT / "app" / "worker.py"
    source = worker_path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    worker = _class_node(tree, "BatchWorker")

    for name in [
        "_schedule_actionable_workflow_tasks",
        "_schedule_workflow_drive",
        "_workflow_has_active_work",
    ]:
        _method_node(worker, name)

    pipeline_source = _source(_method_node(worker, "_run_workflow_pipeline"), source)
    assert "max_drive_passes" not in pipeline_source, "V2 pipeline should not wait on bounded whole-batch pass loops"
    assert "_schedule_workflow_drive" in pipeline_source
    assert "prep_pool.map" not in pipeline_source, "Task preparation must not wait for a whole-batch map barrier"
    assert "as_completed(futures)" in pipeline_source, "Prepared tasks should be scheduled as soon as each one is ready"

    poll_image_source = _source(_method_node(worker, "_poll_one_workflow_image_node"), source)
    assert "_schedule_workflow_drive" in poll_image_source
    assert "_drive_task_once(executor, task)" not in poll_image_source

    poll_video_source = _source(_method_node(worker, "_poll_one_workflow_node"), source)
    assert "_schedule_workflow_drive" in poll_video_source

    driver_source = _source(_method_node(worker, "_drive_task_once"), source)
    assert "if outcome.submitted:" in driver_source
    assert "self._poll_now_event.set()" in driver_source

    print("self_test_workflow_async_scheduler_static: PASS")


if __name__ == "__main__":
    main()
