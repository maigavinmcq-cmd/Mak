"""Offline checks for workflow completion semantics and auto-retry behavior.

Run as:
    python tools/self_test_workflow_completion_retry.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import AppConfig
from app.models.task import TaskItem, TaskStatus
from app.task_manager import TaskManager
from app.worker import BatchWorker
from app.workflow import (
    NODE_STATUS_COMPLETED,
    NODE_STATUS_FAILED,
    NODE_STATUS_PENDING,
    NODE_STATUS_READY,
    NODE_STATUS_SUBMITTED,
    ensure_task_node_states,
    default_workflow_definition,
)
from app.workflow_executor import WorkflowExecutor


def _make_task(**kwargs) -> TaskItem:
    defaults = dict(
        row_index=2,
        pid="1730000000000000000",
        netdisk_path=r"\\server\share\pid",
        image_prompt="legacy image prompt",
        video_prompt="legacy video prompt",
        product_image_path=r"C:\tmp\source.png",
    )
    defaults.update(kwargs)
    return TaskItem(**defaults)


def _executor() -> WorkflowExecutor:
    config = AppConfig()
    config.auto_retry_failed_workflow_enabled = True
    config.auto_retry_image_nodes = True
    config.auto_retry_video_nodes = True
    config.auto_retry_video_download = True
    config.retry_count = 3
    return WorkflowExecutor(config, default_workflow_definition())


def test_video_url_without_local_file_is_not_task_done() -> None:
    executor = _executor()
    workflow = default_workflow_definition()
    task = _make_task(prompt_stage_1="make image", prompt_stage_2="make video")
    ensure_task_node_states(task, workflow)

    task.node_states["image_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["image_stage_1"]["output_image_path"] = r"C:\tmp\image1.png"
    task.node_states["video_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["video_stage_1"]["output_video_url"] = "https://cdn.example/video.mp4"

    status = executor.aggregate_task_status(task)
    assert status == TaskStatus.VIDEO_DOWNLOAD_PENDING
    assert not TaskStatus.is_done(status)


def test_two_stage_task_done_only_after_video_1_downloaded() -> None:
    executor = _executor()
    workflow = default_workflow_definition()
    task = _make_task(prompt_stage_1="make image", prompt_stage_2="make video")
    ensure_task_node_states(task, workflow)

    task.node_states["image_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["image_stage_1"]["output_image_path"] = r"C:\tmp\image1.png"
    task.node_states["video_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["video_stage_1"]["output_video_url"] = "https://cdn.example/video.mp4"
    task.node_states["video_stage_1"]["output_video_local_path"] = r"C:\tmp\video1.mp4"

    status = executor.aggregate_task_status(task)
    assert status == TaskStatus.VIDEO_DOWNLOADED
    assert TaskStatus.is_done(status)


def test_four_stage_task_waits_for_final_video_2_download() -> None:
    executor = _executor()
    workflow = default_workflow_definition()
    task = _make_task(
        prompt_stage_1="make image 1",
        prompt_stage_2="make video 1",
        prompt_stage_3="make image 2",
        prompt_stage_4="make video 2",
    )
    ensure_task_node_states(task, workflow)

    task.node_states["image_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["image_stage_1"]["output_image_path"] = r"C:\tmp\image1.png"
    task.node_states["video_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["video_stage_1"]["output_video_url"] = "https://cdn.example/video1.mp4"
    task.node_states["video_stage_1"]["output_video_local_path"] = r"C:\tmp\video1.mp4"
    task.node_states["image_stage_2"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["image_stage_2"]["output_image_path"] = r"C:\tmp\image2.png"
    task.node_states["video_stage_2"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["video_stage_2"]["output_video_url"] = "https://cdn.example/video2.mp4"

    status = executor.aggregate_task_status(task)
    assert status == TaskStatus.VIDEO_DOWNLOAD_PENDING
    assert not TaskStatus.is_done(status)

    task.node_states["video_stage_2"]["output_video_local_path"] = r"C:\tmp\video2.mp4"
    status = executor.aggregate_task_status(task)
    assert status == TaskStatus.VIDEO_DOWNLOADED
    assert TaskStatus.is_done(status)


def test_failed_nodes_reset_for_default_auto_retry() -> None:
    executor = _executor()
    workflow = default_workflow_definition()
    task = _make_task(prompt_stage_1="make image", prompt_stage_2="make video")
    ensure_task_node_states(task, workflow)

    state = task.node_states["image_stage_1"]
    state["status"] = NODE_STATUS_FAILED
    state["error_message"] = "temporary image provider failure"

    reset = executor.reset_failed_nodes_for_auto_retry(task)
    assert reset == 1
    assert task.node_states["image_stage_1"]["status"] in {NODE_STATUS_PENDING, NODE_STATUS_READY}
    assert task.node_states["image_stage_1"]["auto_retry_count"] == 1
    assert task.status == TaskStatus.AUTO_RETRYING


def test_failed_nodes_stop_retrying_after_retry_budget() -> None:
    executor = _executor()
    workflow = default_workflow_definition()
    task = _make_task(prompt_stage_1="make image", prompt_stage_2="make video")
    ensure_task_node_states(task, workflow)

    state = task.node_states["video_stage_1"]
    state["status"] = NODE_STATUS_FAILED
    state["error_message"] = "permanent video provider failure"
    state["auto_retry_count"] = executor.config.retry_count

    reset = executor.reset_failed_nodes_for_auto_retry(task)
    assert reset == 0
    assert task.node_states["video_stage_1"]["status"] == NODE_STATUS_FAILED
    assert executor.aggregate_task_status(task) == TaskStatus.FAILED_RETRY_EXHAUSTED


def test_manual_retry_preserves_completed_image_when_image_node_has_output() -> None:
    config = AppConfig()
    workflow = default_workflow_definition()
    task = _make_task(prompt_stage_1="make image", prompt_stage_2="make video", status=TaskStatus.FAILED_RETRY_EXHAUSTED)
    ensure_task_node_states(task, workflow)

    image_state = task.node_states["image_stage_1"]
    image_state["status"] = NODE_STATUS_FAILED
    image_state["output_image_path"] = r"C:\tmp\already-generated.png"
    image_state["output_image_url"] = "https://cdn.example/already-generated.png"
    image_state["error_message"] = "stale failure after image was saved"
    task.generated_image_path = r"C:\tmp\already-generated.png"
    task.generated_image_url = "https://cdn.example/already-generated.png"

    video_state = task.node_states["video_stage_1"]
    video_state["status"] = NODE_STATUS_FAILED
    video_state["error_message"] = "video submit failed"

    with tempfile.TemporaryDirectory() as tmp:
        manager = TaskManager(Path(tmp) / "task_state.json")
        manager.tasks = [task]
        worker = BatchWorker(manager, config, logger=None, failed_only=True, workflow_definition=workflow)
        executor = WorkflowExecutor(config, workflow)

        reset = worker._reset_failed_workflow_targets(executor, [task])

    assert reset == 1
    assert task.node_states["image_stage_1"]["status"] == NODE_STATUS_COMPLETED
    assert task.node_states["image_stage_1"]["output_image_path"] == r"C:\tmp\already-generated.png"
    assert task.node_states["image_stage_1"]["output_image_url"] == "https://cdn.example/already-generated.png"
    assert task.node_states["video_stage_1"]["status"] == NODE_STATUS_READY
    assert "image_stage_1" not in [node["node_id"] for node in executor.find_ready_image_nodes(task)]
    assert [node["node_id"] for node in executor.find_ready_video_nodes(task)] == ["video_stage_1"]


def test_stale_input_repair_preserves_completed_image_output() -> None:
    executor = _executor()
    workflow = default_workflow_definition()
    task = _make_task(prompt_stage_1="make image", prompt_stage_2="make video", product_image_path=r"C:\tmp\new-source.png")
    ensure_task_node_states(task, workflow)

    image_state = task.node_states["image_stage_1"]
    image_state["status"] = NODE_STATUS_COMPLETED
    image_state["input_images"] = [r"C:\tmp\old-source.png"]
    image_state["output_image_path"] = r"C:\tmp\already-generated.png"
    image_state["output_image_url"] = "https://cdn.example/already-generated.png"
    task.generated_image_path = r"C:\tmp\already-generated.png"
    task.generated_image_url = "https://cdn.example/already-generated.png"

    repaired = executor.repair_stale_node_inputs(task)

    assert repaired == 0
    assert task.node_states["image_stage_1"]["status"] == NODE_STATUS_COMPLETED
    assert task.node_states["image_stage_1"]["output_image_path"] == r"C:\tmp\already-generated.png"
    assert task.node_states["image_stage_1"]["output_image_url"] == "https://cdn.example/already-generated.png"


def test_download_failure_retries_before_refreshing_expired_url() -> None:
    executor = _executor()
    workflow = default_workflow_definition()
    task = _make_task(prompt_stage_1="make image", prompt_stage_2="make video")
    ensure_task_node_states(task, workflow)
    node = workflow["nodes"][1]
    state = task.node_states["video_stage_1"]
    state["status"] = NODE_STATUS_COMPLETED
    state["task_id"] = "video-task-1"
    state["output_video_url"] = "https://cdn.example/expiring-video.mp4"
    state["download_attempt_count"] = 1

    outcome = executor._handle_video_download_failure(task, "video_stage_1", state, RuntimeError("primary"), RuntimeError("fallback"))
    assert not outcome.success
    assert state["download_status"] == "RETRYING"
    assert state["output_video_url"] == "https://cdn.example/expiring-video.mp4"
    assert [n["node_id"] for n in executor.find_downloadable_nodes(task)] == [node["node_id"]]

    state["download_attempt_count"] = executor.config.retry_count
    outcome = executor._handle_video_download_failure(task, "video_stage_1", state, RuntimeError("primary"), RuntimeError("fallback"))
    assert not outcome.success
    assert outcome.submitted
    assert state["download_status"] == "WAITING_NEW_URL"
    assert state["output_video_url"] is None
    assert state["status"] == NODE_STATUS_SUBMITTED


def test_completed_status_is_not_done_anymore() -> None:
    assert not TaskStatus.is_done(TaskStatus.COMPLETED)
    assert TaskStatus.is_done(TaskStatus.VIDEO_DOWNLOADED)


def main() -> None:
    tests = [
        test_video_url_without_local_file_is_not_task_done,
        test_two_stage_task_done_only_after_video_1_downloaded,
        test_four_stage_task_waits_for_final_video_2_download,
        test_failed_nodes_reset_for_default_auto_retry,
        test_failed_nodes_stop_retrying_after_retry_budget,
        test_manual_retry_preserves_completed_image_when_image_node_has_output,
        test_stale_input_repair_preserves_completed_image_output,
        test_download_failure_retries_before_refreshing_expired_url,
        test_completed_status_is_not_done_anymore,
    ]
    for test in tests:
        test()
    print(f"workflow completion/retry self-test passed: {len(tests)} checks")


if __name__ == "__main__":
    main()
