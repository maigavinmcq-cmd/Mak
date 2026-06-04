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
    executor_source = Path("app/workflow_executor.py").read_text(encoding="utf-8-sig")
    executor_tree = ast.parse(executor_source)
    executor = _class_node(executor_tree, "WorkflowExecutor")
    find_polling_image_nodes = _method_source(executor_source, executor, "find_polling_image_nodes")
    assert "NODE_TYPE_IMAGE" in find_polling_image_nodes
    assert 'state.get("task_id")' in find_polling_image_nodes
    assert 'state.get("output_image_path")' in find_polling_image_nodes
    assert 'state.get("output_image_url")' in find_polling_image_nodes

    worker_source = Path("app/worker.py").read_text(encoding="utf-8-sig")
    worker_tree = ast.parse(worker_source)
    worker = _class_node(worker_tree, "BatchWorker")
    run_workflow = _method_source(worker_source, worker, "_run_workflow_engine_run")
    assert "_poll_cycle_workflow_images()" in run_workflow
    assert run_workflow.index("_poll_cycle_workflow_images()") < run_workflow.index("_poll_cycle_workflow()")

    poll_images = _method_source(worker_source, worker, "_poll_cycle_workflow_images")
    assert "find_polling_image_nodes" in poll_images
    assert "_poll_one_workflow_image_node" in poll_images
    assert "poll_concurrency" in poll_images
    assert "No image nodes need polling" not in poll_images

    poll_one_image = _method_source(worker_source, worker, "_poll_one_workflow_image_node")
    assert "run_image_node" in poll_one_image
    assert "_drive_task_once" in poll_one_image
    assert "poll_only" in poll_one_image

    print("workflow image poll-only static self-test passed")


if __name__ == "__main__":
    main()
