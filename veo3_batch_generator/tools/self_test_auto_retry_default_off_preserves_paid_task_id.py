from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import AppConfig
from app.models.task import TaskItem
from app.workflow import NODE_STATUS_FAILED, default_workflow_definition, ensure_task_node_states
from app.workflow_executor import WorkflowExecutor


def main() -> None:
    workflow = default_workflow_definition()
    task = TaskItem(
        row_index=7,
        pid="pid_paid_video_task",
        netdisk_path="",
        product_image_path="C:/tmp/source.png",
        image_prompt="image prompt",
        video_prompt="video prompt",
    )
    ensure_task_node_states(task, workflow)
    state = task.node_states["video_stage_1"]
    state["status"] = NODE_STATUS_FAILED
    state["task_id"] = "v_paid_once"
    state["auto_retry_count"] = 0
    state["error_message"] = "JimmyAI Veo poll failed: request too frequent, please try later"

    executor = WorkflowExecutor(AppConfig(retry_count=5), workflow)
    reset_count = executor.reset_failed_nodes_for_auto_retry(task)

    assert reset_count == 0
    assert task.node_states["video_stage_1"]["status"] == NODE_STATUS_FAILED
    assert task.node_states["video_stage_1"]["task_id"] == "v_paid_once"
    print("auto retry defaults off and preserves paid video task_id")


if __name__ == "__main__":
    main()
