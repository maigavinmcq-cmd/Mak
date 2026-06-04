from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import AppConfig
from app.models.task import TaskItem, TaskStatus
from app.workflow import (
    NODE_STATUS_BLOCKED,
    NODE_STATUS_COMPLETED,
    NODE_STATUS_POLLING,
    NODE_STATUS_WAITING_INPUT,
    default_workflow_definition,
    ensure_task_node_states,
)
from app.workflow_executor import WorkflowExecutor


def main() -> None:
    workflow = default_workflow_definition()
    task = TaskItem(
        row_index=9,
        pid="pid_retry_marker",
        netdisk_path="",
        product_image_path="C:/tmp/source.png",
        image_prompt="image prompt",
        video_prompt="video prompt",
        status=TaskStatus.AUTO_RETRYING,
        last_auto_retry_stage="video_stage_1",
        last_auto_retry_reason="old video timeout",
    )
    ensure_task_node_states(task, workflow)

    image_state = task.node_states["image_stage_1"]
    image_state["status"] = NODE_STATUS_COMPLETED
    image_state["output_image_path"] = "C:/tmp/generated.png"
    image_state["auto_retrying"] = False
    image_state["last_auto_retry_reason"] = "old image submit failure"

    video_state = task.node_states["video_stage_1"]
    video_state["status"] = NODE_STATUS_POLLING
    video_state["task_id"] = "task_video_123"
    video_state["auto_retrying"] = False
    video_state["last_auto_retry_reason"] = "old video timeout"

    task.node_states["image_stage_2"]["status"] = NODE_STATUS_WAITING_INPUT
    task.node_states["video_stage_2"]["status"] = NODE_STATUS_BLOCKED

    executor = WorkflowExecutor(AppConfig(), workflow)
    executor.cleanup_inactive_retry_markers(task)

    assert task.last_auto_retry_reason is None
    assert task.last_auto_retry_stage is None
    assert image_state.get("last_auto_retry_reason") is None
    assert video_state.get("last_auto_retry_reason") is None
    assert executor.aggregate_task_status(task) == TaskStatus.VIDEO_POLLING

    print("retry marker cleanup self-test passed")


if __name__ == "__main__":
    main()
