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
    worker_source = Path("app/worker.py").read_text(encoding="utf-8-sig")
    worker_tree = ast.parse(worker_source)
    worker = _class_node(worker_tree, "BatchWorker")

    retry_reset = _method_source(worker_source, worker, "_reset_failed_workflow_targets")
    assert "manual_reset_node" in retry_reset
    assert "NODE_STATUS_FAILED" in retry_reset
    assert "self.failed_only" in retry_reset
    assert "auto_retry_count = 0" in retry_reset

    run_workflow = _method_source(worker_source, worker, "_run_workflow_engine_run")
    assert "_reset_failed_workflow_targets" in run_workflow
    assert run_workflow.index("_select_workflow_targets()") < run_workflow.index("_reset_failed_workflow_targets")
    assert run_workflow.index("_reset_failed_workflow_targets") < run_workflow.index("_run_workflow_pipeline")

    pipeline = _method_source(worker_source, worker, "_run_workflow_pipeline")
    assert "_auto_retry_failed_workflow_targets" in pipeline
    assert pipeline.index("_auto_retry_failed_workflow_targets") < pipeline.index("executor.has_actionable_nodes")

    auto_retry = _method_source(worker_source, worker, "_auto_retry_failed_workflow_targets")
    assert "auto_retry_failed_workflow_enabled" in auto_retry
    assert "reset_failed_nodes_for_auto_retry" in auto_retry

    poll_one = _method_source(worker_source, worker, "_poll_one_workflow_node")
    assert "_auto_retry_failed_workflow_targets" in poll_one
    assert "_drive_task_once" in poll_one or "_schedule_workflow_drive" in poll_one

    print("workflow retry-failed static self-test passed")


if __name__ == "__main__":
    main()
