from __future__ import annotations

import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api.video_api import VideoPollResult
from app.config import AppConfig
from app.models.task import TaskItem
from app.workflow import NODE_STATUS_SUBMITTED, default_workflow_definition, ensure_task_node_states
import app.workflow_executor as workflow_executor_module
from app.workflow_executor import WorkflowExecutor


class RateLimitedProvider:
    def __init__(self) -> None:
        self.calls = 0

    def poll_video_task(self, **kwargs):
        self.calls += 1
        return VideoPollResult(
            True,
            finished=False,
            failed=False,
            retryable_failure=True,
            status="rate_limited",
            error_message="JimmyAI Veo 轮询临时失败，继续使用原 task_id 轮询: 请求过于频繁，请稍后再试",
        )


def main() -> None:
    workflow = default_workflow_definition()
    task = TaskItem(
        row_index=1,
        pid="pid_rate_limit_backoff",
        netdisk_path="",
        image_prompt="",
        video_prompt="",
    )
    ensure_task_node_states(task, workflow)
    state = task.node_states["video_stage_1"]
    state["status"] = NODE_STATUS_SUBMITTED
    state["task_id"] = "v_rate_limited"
    state["provider"] = "jimmy_veo"

    provider = RateLimitedProvider()
    original_get_video_provider = workflow_executor_module.get_video_provider
    workflow_executor_module.get_video_provider = lambda key: provider
    try:
        executor = WorkflowExecutor(AppConfig(poll_interval_seconds=20), workflow)
        node = next(item for item in workflow["nodes"] if item["node_id"] == "video_stage_1")
        before = time.time()

        outcome = executor.poll_video_node(task, node)
        assert outcome.success is True
        assert outcome.status == "POLLING"
        assert state["task_id"] == "v_rate_limited"
        assert state["poll_rate_limit_backoff_seconds"] == 5
        assert state["next_poll_after_ts"] >= before + 24
        assert executor.find_polling_nodes(task) == []

        state["next_poll_after_ts"] = time.time() - 1
        assert executor.find_polling_nodes(task) == [node]
        executor.poll_video_node(task, node)
        assert state["poll_rate_limit_backoff_seconds"] == 10
        assert provider.calls == 2
    finally:
        workflow_executor_module.get_video_provider = original_get_video_provider

    print("rate-limit polling uses adaptive per-task delay")


if __name__ == "__main__":
    main()
