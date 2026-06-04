from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import AppConfig
from app.models.task import TaskItem, TaskStatus
from app.workflow import NODE_STATUS_COMPLETED, default_workflow_definition, ensure_task_node_states
from app.workflow_executor import WorkflowExecutor


def main() -> None:
    workflow = default_workflow_definition()
    task = TaskItem(
        row_index=2,
        pid="pid_retry_success",
        netdisk_path="",
        product_image_path="C:/tmp/source.png",
        image_prompt="image prompt",
        video_prompt="video prompt",
        status=TaskStatus.AUTO_RETRYING,
    )
    ensure_task_node_states(task, workflow)

    image_state = task.node_states["image_stage_1"]
    image_state["status"] = NODE_STATUS_COMPLETED
    image_state["output_image_path"] = "C:/tmp/generated.png"
    image_state["auto_retrying"] = True

    video_state = task.node_states["video_stage_1"]
    video_state["status"] = NODE_STATUS_COMPLETED
    video_state["output_video_url"] = "https://example.test/video.mp4"
    video_state["output_video_local_path"] = "C:/tmp/video.mp4"
    video_state["download_status"] = "DOWNLOADED"
    video_state["auto_retrying"] = True

    executor = WorkflowExecutor(AppConfig(), workflow)
    assert executor.aggregate_task_status(task) == TaskStatus.VIDEO_DOWNLOADED

    print("auto retry completed status self-test passed")


if __name__ == "__main__":
    main()
