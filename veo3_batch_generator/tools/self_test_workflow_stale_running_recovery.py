from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import AppConfig
from app.models.task import TaskItem, TaskStatus
from app.workflow import (
    NODE_STATUS_COMPLETED,
    NODE_STATUS_READY,
    NODE_STATUS_RUNNING,
    NODE_STATUS_SUBMITTED,
    default_workflow_definition,
    ensure_task_node_states,
)
from app.workflow_executor import WorkflowExecutor


def _make_task() -> TaskItem:
    return TaskItem(
        row_index=2,
        pid="1730000000000000000",
        netdisk_path=r"\\server\share\pid",
        image_prompt="legacy image prompt",
        video_prompt="legacy video prompt",
        prompt_stage_1="stage 1 prompt",
        prompt_stage_2="stage 2 prompt",
        prompt_stage_3="stage 3 prompt",
        prompt_stage_4="stage 4 prompt",
        product_image_path=r"C:\tmp\source.png",
    )


def test_running_image_without_task_id_reenters_queue() -> None:
    workflow = default_workflow_definition()
    executor = WorkflowExecutor(AppConfig(), workflow)
    task = _make_task()
    ensure_task_node_states(task, workflow)
    state = task.node_states["image_stage_1"]
    state["status"] = NODE_STATUS_RUNNING
    state["started_at"] = "2026-05-15 01:57:00"
    task.status = TaskStatus.GENERATING_IMAGE

    recovered = executor.recover_interrupted_running_nodes(task)

    assert recovered == 1
    assert task.node_states["image_stage_1"]["status"] == NODE_STATUS_READY
    assert [n["node_id"] for n in executor.find_ready_image_nodes(task)] == ["image_stage_1"]


def test_running_video_without_task_id_reenters_queue() -> None:
    workflow = default_workflow_definition()
    executor = WorkflowExecutor(AppConfig(), workflow)
    task = _make_task()
    ensure_task_node_states(task, workflow)
    task.node_states["image_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["image_stage_1"]["output_image_path"] = r"C:\tmp\image1.png"
    state = task.node_states["video_stage_1"]
    state["status"] = NODE_STATUS_RUNNING
    state["started_at"] = "2026-05-15 01:57:00"
    task.status = TaskStatus.VIDEO_SUBMITTING

    recovered = executor.recover_interrupted_running_nodes(task)

    assert recovered == 1
    assert task.node_states["video_stage_1"]["status"] == NODE_STATUS_READY
    assert [n["node_id"] for n in executor.find_ready_video_nodes(task)] == ["video_stage_1"]


def test_running_video_with_task_id_resumes_polling() -> None:
    workflow = default_workflow_definition()
    executor = WorkflowExecutor(AppConfig(), workflow)
    task = _make_task()
    ensure_task_node_states(task, workflow)
    task.node_states["image_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["image_stage_1"]["output_image_path"] = r"C:\tmp\image1.png"
    state = task.node_states["video_stage_1"]
    state["status"] = NODE_STATUS_RUNNING
    state["task_id"] = "task_video_123"
    state["started_at"] = "2026-05-15 01:57:00"
    task.status = TaskStatus.VIDEO_SUBMITTING

    recovered = executor.recover_interrupted_running_nodes(task)

    assert recovered == 1
    assert task.node_states["video_stage_1"]["status"] == NODE_STATUS_SUBMITTED
    assert [n["node_id"] for n in executor.find_polling_nodes(task)] == ["video_stage_1"]


def main() -> None:
    test_running_image_without_task_id_reenters_queue()
    test_running_video_without_task_id_reenters_queue()
    test_running_video_with_task_id_resumes_polling()
    print("workflow stale-running recovery self-test passed")


if __name__ == "__main__":
    main()
