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
    source = Path("app/workflow_executor.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    executor = _class_node(tree, "WorkflowExecutor")

    run_image_node = _method_source(source, executor, "run_image_node")
    assert '"on_task_id"' in run_image_node
    assert "_record_image_node_task_id" in run_image_node
    assert "submit_image_task" in run_image_node
    assert "poll_image_task_once" in run_image_node
    assert "poll_image_task" in run_image_node
    assert "resume image polling" in run_image_node
    assert "result_task_id" in run_image_node

    record_task_id = _method_source(source, executor, "_record_image_node_task_id")
    assert "NODE_STATUS_POLLING" in record_task_id
    assert "self._save_emit(task, force=True)" in record_task_id
    assert "task.image_task_id" not in record_task_id
    assert "self._mirror_image_node_to_legacy_fields" in record_task_id

    find_ready_image_nodes = _method_source(source, executor, "find_ready_image_nodes")
    assert "NODE_STATUS_SUBMITTED" in find_ready_image_nodes
    assert "NODE_STATUS_POLLING" in find_ready_image_nodes
    assert 'out.append(node)' not in find_ready_image_nodes.split("if current in {NODE_STATUS_SUBMITTED, NODE_STATUS_POLLING}:", 1)[1].split("if current in {NODE_STATUS_RUNNING}", 1)[0]

    find_polling_image_nodes = _method_source(source, executor, "find_polling_image_nodes")
    assert 'state.get("task_id")' in find_polling_image_nodes
    assert "NODE_STATUS_SUBMITTED" in find_polling_image_nodes
    assert "NODE_STATUS_POLLING" in find_polling_image_nodes

    aggregate_task_status = _method_source(source, executor, "aggregate_task_status")
    assert "waiting_video" in aggregate_task_status
    assert "waiting_image" in aggregate_task_status
    assert "TaskStatus.GENERATING_IMAGE" in aggregate_task_status
    assert aggregate_task_status.index("if waiting_video:") < aggregate_task_status.index("if waiting_image:")

    print("workflow image task_id persistence static self-test passed")


if __name__ == "__main__":
    main()
