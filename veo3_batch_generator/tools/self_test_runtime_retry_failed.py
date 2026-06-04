from __future__ import annotations

import ast
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import AppConfig
from app.models.task import TaskItem, TaskStatus
from app.task_manager import TaskManager
from app.worker import BatchWorker
from app.workflow import NODE_STATUS_COMPLETED, NODE_STATUS_FAILED, NODE_STATUS_SUBMITTED, default_workflow_definition, ensure_task_node_states
from app.workflow_executor import NodeRunOutcome


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


def test_retry_button_stays_enabled_while_running() -> None:
    gui_source = Path("app/gui.py").read_text(encoding="utf-8-sig")
    gui_tree = ast.parse(gui_source)
    main_window = _class_node(gui_tree, "MainWindow")
    set_buttons = _method_source(gui_source, main_window, "_set_running_buttons")
    assert "self.retry_failed_btn.setEnabled(True)" in set_buttons

    start_worker = _method_source(gui_source, main_window, "start_worker")
    assert "failed_only" in start_worker
    assert "retry_failed" in start_worker


def test_running_worker_can_reset_failed_nodes_without_failed_only_mode() -> None:
    workflow = default_workflow_definition()
    task = TaskItem(
        row_index=1,
        task_name="runtime retry",
        pid="pid-runtime-retry",
        netdisk_path="",
        image_prompt="image",
        video_prompt="video",
        generated_image_path="C:/tmp/generated.png",
        status=TaskStatus.FAILED_VIDEO_API,
        error_message="submit failed before task_id",
    )
    ensure_task_node_states(task, workflow)
    task.node_states["image_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["image_stage_1"]["output_image_path"] = task.generated_image_path
    task.node_states["video_stage_1"]["status"] = NODE_STATUS_FAILED
    task.node_states["video_stage_1"]["error_message"] = task.error_message

    with tempfile.TemporaryDirectory() as tmp:
        manager = TaskManager(Path(tmp) / "task_state.json")
        manager.tasks = [task]
        worker = BatchWorker(
            manager,
            AppConfig(),
            failed_only=False,
            poll_only=False,
            execution_mode="image_only",
            logger=logging.getLogger("self_test_runtime_retry_failed"),
            workflow_definition=workflow,
            log_dir=tmp,
        )
        worker._workflow_executor = worker._make_workflow_executor()
        worker._workflow_runtime_target_keys = set()
        submitted_nodes: list[str] = []

        def fake_submit_video_node(task_arg: TaskItem, node: dict) -> NodeRunOutcome:
            submitted_nodes.append(str(node.get("node_id") or ""))
            state = task_arg.node_states[str(node.get("node_id") or "")]
            state["status"] = NODE_STATUS_SUBMITTED
            state["task_id"] = "task_runtime_retry"
            return NodeRunOutcome(True, NODE_STATUS_SUBMITTED, submitted=True)

        worker._workflow_executor.submit_video_node = fake_submit_video_node  # type: ignore[method-assign]

        reset_count = worker.retry_failed_workflow_targets_now()

    assert reset_count == 1
    assert submitted_nodes == ["video_stage_1"]
    assert task.node_states["video_stage_1"]["status"] == NODE_STATUS_SUBMITTED
    assert worker._task_runtime_key(task) in worker._workflow_runtime_target_keys


def main() -> None:
    test_retry_button_stays_enabled_while_running()
    test_running_worker_can_reset_failed_nodes_without_failed_only_mode()
    print("runtime retry failed self-test passed")


if __name__ == "__main__":
    main()
