from __future__ import annotations

import sys
import tempfile
from pathlib import Path
import logging


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api.video_api import VideoPollResult
from app.config import AppConfig
from app.models.task import TaskItem
from app.models.task import TaskStatus
from app.task_manager import TaskManager
from app.workflow import (
    NODE_STATUS_FAILED,
    NODE_STATUS_POLLING,
    default_workflow_definition,
    ensure_task_node_states,
)
import app.workflow_executor as workflow_executor
from app.workflow_executor import WorkflowExecutor
import app.worker as worker_module
from app.worker import ManualPollWorker


class _DummyPollProvider:
    def __init__(self, result: VideoPollResult) -> None:
        self.result = result

    def poll_video_task(self, **kwargs):
        return self.result


def _task_with_video_task_id() -> TaskItem:
    workflow = default_workflow_definition()
    task = TaskItem(
        row_index=1,
        pid="pid_paid_poll",
        netdisk_path="",
        product_image_path="C:/tmp/source.png",
        image_prompt="image prompt",
        video_prompt="video prompt",
        generated_image_path="C:/tmp/generated.png",
        generated_image_url="https://example.test/generated.png",
        video_task_id="task_paid_once",
    )
    ensure_task_node_states(task, workflow)
    state = task.node_states["video_stage_1"]
    state["status"] = NODE_STATUS_POLLING
    state["task_id"] = "task_paid_once"
    state["provider"] = "xibapi_veo"
    return task


def test_paid_task_id_poll_http_error_keeps_polling() -> None:
    workflow = default_workflow_definition()
    executor = WorkflowExecutor(AppConfig(retry_count=5), workflow)
    task = _task_with_video_task_id()
    node = next(node for node in workflow["nodes"] if node["node_id"] == "video_stage_1")
    original = workflow_executor.get_video_provider
    workflow_executor.get_video_provider = lambda key: _DummyPollProvider(
        VideoPollResult(
            False,
            failed=True,
            status="HTTP_400",
            raw_response={"error_code": "bad_request", "message": "temporary upstream response"},
            error_message="video poll failed: HTTP 400 temporary upstream response",
        )
    )
    try:
        outcome = executor.poll_video_node(task, node)
    finally:
        workflow_executor.get_video_provider = original

    state = task.node_states["video_stage_1"]
    assert outcome.status == NODE_STATUS_POLLING
    assert state["status"] == NODE_STATUS_POLLING
    assert state["task_id"] == "task_paid_once"
    assert task.video_task_id == "task_paid_once"


def test_paid_task_id_explicit_failed_status_can_fail_terminally() -> None:
    workflow = default_workflow_definition()
    executor = WorkflowExecutor(AppConfig(retry_count=5), workflow)
    task = _task_with_video_task_id()
    node = next(node for node in workflow["nodes"] if node["node_id"] == "video_stage_1")
    original = workflow_executor.get_video_provider
    workflow_executor.get_video_provider = lambda key: _DummyPollProvider(
        VideoPollResult(
            False,
            failed=True,
            status="failed",
            raw_response={"id": "task_paid_once", "status": "failed", "error": {"message": "upstream terminal failed"}},
            error_message="video task failed: upstream terminal failed",
        )
    )
    try:
        outcome = executor.poll_video_node(task, node)
    finally:
        workflow_executor.get_video_provider = original

    state = task.node_states["video_stage_1"]
    assert outcome.status == NODE_STATUS_FAILED
    assert state["status"] == NODE_STATUS_FAILED
    assert state["task_id"] == "task_paid_once"


def test_no_task_id_video_submit_failure_auto_retries_by_default() -> None:
    workflow = default_workflow_definition()
    executor = WorkflowExecutor(AppConfig(retry_count=5), workflow)
    task = TaskItem(
        row_index=2,
        pid="pid_no_task_id",
        netdisk_path="",
        product_image_path="C:/tmp/source.png",
        image_prompt="image prompt",
        video_prompt="video prompt",
        generated_image_path="C:/tmp/generated.png",
        generated_image_url="https://example.test/generated.png",
    )
    ensure_task_node_states(task, workflow)
    state = task.node_states["video_stage_1"]
    state["status"] = NODE_STATUS_FAILED
    state["task_id"] = ""
    state["auto_retry_count"] = 0
    state["error_message"] = "xibapi submit failed before task_id: 默认分组暂无可用源站"

    reset_count = executor.reset_failed_nodes_for_auto_retry(task)

    assert reset_count == 1
    assert task.node_states["video_stage_1"]["task_id"] in {"", None}
    assert task.node_states["video_stage_1"]["status"] != NODE_STATUS_FAILED


def test_manual_poll_retryable_task_not_exist_keeps_existing_task_id() -> None:
    task = TaskItem(
        row_index=20,
        pid="pid_manual_poll",
        netdisk_path="",
        product_image_path="C:/tmp/source.png",
        image_prompt="image prompt",
        video_prompt="video prompt",
        video_provider="hellobabygo_veo",
        video_task_id="task_missing_temporarily",
        status=TaskStatus.VIDEO_POLLING,
        video_status=TaskStatus.VIDEO_POLLING,
    )
    with tempfile.TemporaryDirectory() as tmp:
        manager = TaskManager(Path(tmp) / "task_state.json")
        manager.tasks = [task]
        poll_worker = ManualPollWorker(
            manager,
            AppConfig(video_api_key="sk-test", video_provider="hellobabygo_veo"),
            logging.getLogger("self-test-manual-poll"),
            "batch_self_test",
        )
        original_get_video_provider = worker_module.get_video_provider
        worker_module.get_video_provider = lambda _key: _DummyPollProvider(
            VideoPollResult(
                False,
                failed=True,
                retryable_failure=True,
                status="HTTP_400",
                raw_response={"code": "task_not_exist", "message": "task_not_exist"},
                error_message="HelloBabyGo video poll failed: HTTP 400 {'code': 'task_not_exist'}",
            )
        )
        try:
            result = poll_worker._poll_one_manual(task)
        finally:
            worker_module.get_video_provider = original_get_video_provider

    assert result == "ERROR"
    assert task.video_task_id == "task_missing_temporarily"
    assert task.status == TaskStatus.VIDEO_POLLING
    assert task.video_status == TaskStatus.VIDEO_POLLING


def main() -> None:
    test_paid_task_id_poll_http_error_keeps_polling()
    test_paid_task_id_explicit_failed_status_can_fail_terminally()
    test_no_task_id_video_submit_failure_auto_retries_by_default()
    test_manual_poll_retryable_task_not_exist_keeps_existing_task_id()
    print("video retry policy self-test passed")


if __name__ == "__main__":
    main()
