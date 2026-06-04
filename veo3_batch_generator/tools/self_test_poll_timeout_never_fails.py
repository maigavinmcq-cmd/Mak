from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import AppConfig
from app.models.task import TaskItem, TaskStatus
from app.workflow import NODE_STATUS_POLLING, default_workflow_definition, ensure_task_node_states, get_node_state
import app.workflow_executor as workflow_executor_module
from app.workflow_executor import WorkflowExecutor


class _PendingVideoProvider:
    def poll_video_task(self, task_id: str, api_key: str, extra_params: dict | None = None):
        return SimpleNamespace(
            success=True,
            finished=False,
            failed=False,
            video_url=None,
            status="in_progress",
            raw_response={"status": "in_progress"},
            error_message=None,
        )


def _make_task() -> TaskItem:
    return TaskItem(
        row_index=1,
        pid="pid-1",
        netdisk_path="",
        image_prompt="image",
        video_prompt="video",
        video_provider="dummy_video",
        video_task_id="task_still_running",
        status=TaskStatus.VIDEO_POLLING,
    )


def main() -> None:
    workflow = default_workflow_definition()
    node = next(item for item in workflow["nodes"] if item["node_id"] == "video_stage_1")
    task = _make_task()
    ensure_task_node_states(task, workflow)
    state = get_node_state(task, "video_stage_1")
    state.update(
        {
            "node_id": "video_stage_1",
            "node_type": "video_generation",
            "status": NODE_STATUS_POLLING,
            "task_id": "task_still_running",
            "poll_count": 1,
        }
    )
    saves: list[tuple[bool, str, str]] = []
    executor = WorkflowExecutor(
        AppConfig(max_poll_count=1, video_provider="dummy_video", video_api_key="sk-test"),
        workflow,
        save_emit_callback=lambda t, force=False: saves.append((force, t.status, state["status"])),
    )

    original_get_video_provider = workflow_executor_module.get_video_provider
    workflow_executor_module.get_video_provider = lambda _key: _PendingVideoProvider()
    try:
        outcome = executor.poll_video_node(task, node)
    finally:
        workflow_executor_module.get_video_provider = original_get_video_provider

    assert outcome.status == NODE_STATUS_POLLING
    assert state["status"] == NODE_STATUS_POLLING
    assert task.status == TaskStatus.VIDEO_POLLING
    assert "error_message" not in state or not state.get("error_message")
    assert saves, "polling state should still be emitted/saved"
    print("polling max count no longer marks video nodes failed")


if __name__ == "__main__":
    main()
