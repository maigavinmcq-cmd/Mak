from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.batch import TaskBatch
from app.models.task import TaskItem, TaskStatus
from app.runtime_provider_switch import apply_runtime_provider_switch
from app.workflow import (
    NODE_STATUS_COMPLETED,
    NODE_STATUS_FAILED,
    NODE_STATUS_PENDING,
    NODE_STATUS_POLLING,
    NODE_STATUS_READY,
    NODE_STATUS_RUNNING,
    NODE_STATUS_SUBMITTED,
    default_workflow_definition,
    ensure_task_node_states,
)


def _task(row: int) -> TaskItem:
    task = TaskItem(
        row_index=row,
        pid=f"173000000000000000{row}",
        netdisk_path=r"\\server\share\pid",
        image_prompt="legacy image prompt",
        video_prompt="legacy video prompt",
        prompt_stage_1="stage 1 prompt",
        prompt_stage_2="stage 2 prompt",
        product_image_path=r"C:\tmp\source.png",
        image_provider="xibapi_gpt_image2",
        image_model_logical_key="gpt_image_2",
        image_model_display="GPT Image 2",
        video_provider="xibapi_veo",
        video_model_logical_key="veo_3_1_fast",
        video_model_display="Veo 3.1 Fast",
        batch_id="batch-runtime-switch",
    )
    ensure_task_node_states(task, default_workflow_definition())
    return task


def test_switch_image_provider_resets_failed_image_without_touching_submitted_video() -> None:
    batch = TaskBatch(
        batch_id="batch-runtime-switch",
        batch_name="runtime switch",
        source_excel_path="",
        imported_at="2026-05-15 00:00:00",
        task_count=1,
        batch_config={
            "image_provider": "xibapi_gpt_image2",
            "image_model_logical_key": "gpt_image_2",
            "video_provider": "xibapi_veo",
            "video_model_logical_key": "veo_3_1_fast",
        },
        workflow_definition=default_workflow_definition(),
    )
    task = _task(2)
    task.node_states["image_stage_1"]["status"] = NODE_STATUS_FAILED
    task.node_states["image_stage_1"]["error_message"] = "xibapi unavailable"
    task.node_states["video_stage_1"]["status"] = NODE_STATUS_SUBMITTED
    task.node_states["video_stage_1"]["task_id"] = "old-video-task-id"
    task.video_task_id = "old-video-task-id"
    task.status = TaskStatus.AUTO_RETRYING

    summary = apply_runtime_provider_switch(
        batch,
        [task],
        {
            "apply_image": True,
            "image_provider": "hellobabygo_image",
            "image_model_logical_key": "gpt_image_2",
            "image_api_key": "new-image-key",
            "image_api_base_url": "https://api.hellobabygo.com",
            "apply_video": False,
            "force_restart_submitted": False,
        },
        default_workflow_definition(),
    )

    assert summary["image_nodes_reset"] == 2
    assert summary["video_nodes_reset"] == 0
    assert task.image_provider == "hellobabygo_image"
    assert task.image_model_logical_key == "gpt_image_2"
    assert task.node_states["image_stage_1"]["status"] in {NODE_STATUS_PENDING, NODE_STATUS_READY}
    assert task.node_states["image_stage_1"]["error_message"] is None
    assert task.video_provider == "xibapi_veo"
    assert task.node_states["video_stage_1"]["task_id"] == "old-video-task-id"
    assert batch.batch_config["image_provider"] == "hellobabygo_image"
    assert batch.batch_config["image_api_key"] == "new-image-key"


def test_switch_image_provider_resets_failed_image_even_when_old_task_id_exists() -> None:
    batch = TaskBatch(
        batch_id="batch-runtime-switch",
        batch_name="runtime switch",
        source_excel_path="",
        imported_at="2026-05-15 00:00:00",
        task_count=1,
        batch_config={
            "image_provider": "xibapi_gpt_image2",
            "image_model_logical_key": "gpt_image_2",
        },
        workflow_definition=default_workflow_definition(),
    )
    task = _task(5)
    task.node_states["image_stage_1"]["status"] = NODE_STATUS_FAILED
    task.node_states["image_stage_1"]["task_id"] = "old-failed-image-task-id"
    task.node_states["image_stage_1"]["error_message"] = "old provider failed"
    task.image_task_id = "old-failed-image-task-id"
    task.status = TaskStatus.FAILED_IMAGE_API

    summary = apply_runtime_provider_switch(
        batch,
        [task],
        {
            "apply_image": True,
            "image_provider": "hellobabygo_image",
            "image_model_logical_key": "gpt_image_2",
            "apply_video": False,
            "force_restart_submitted": False,
        },
        default_workflow_definition(),
    )

    image_state = task.node_states["image_stage_1"]
    video_state = task.node_states["video_stage_1"]
    assert summary["image_nodes_reset"] >= 1
    assert summary["skipped_task_id_nodes"] == 0
    assert task.node_states["image_stage_1"]["status"] == NODE_STATUS_PENDING
    assert task.node_states["image_stage_1"]["task_id"] is None
    assert task.image_task_id is None
    assert task.node_states["image_stage_1"]["provider"] == "hellobabygo_image"
    assert task.image_provider == "hellobabygo_image"
    assert task.status == TaskStatus.PENDING


def test_switch_image_provider_resets_active_image_nodes_without_force_restart() -> None:
    batch = TaskBatch(
        batch_id="batch-runtime-switch",
        batch_name="runtime switch",
        source_excel_path="",
        imported_at="2026-05-15 00:00:00",
        task_count=1,
        batch_config={
            "image_provider": "xibapi_gpt_image2",
            "image_model_logical_key": "gpt_image_2",
            "video_provider": "xibapi_veo",
            "video_model_logical_key": "veo_3_1_fast",
        },
        workflow_definition=default_workflow_definition(),
    )
    task = _task(6)
    image_state = task.node_states["image_stage_1"]
    image_state["status"] = NODE_STATUS_RUNNING
    image_state["provider"] = "xibapi_gpt_image2"
    image_state["model_logical_key"] = "gpt_image_2"
    image_state["task_id"] = "old-running-image-task-id"
    task.image_task_id = "old-running-image-task-id"

    video_state = task.node_states["video_stage_1"]
    video_state["status"] = NODE_STATUS_POLLING
    video_state["provider"] = "xibapi_veo"
    video_state["task_id"] = "keep-video-task-id"
    task.video_task_id = "keep-video-task-id"

    summary = apply_runtime_provider_switch(
        batch,
        [task],
        {
            "apply_image": True,
            "image_provider": "hellobabygo_image",
            "image_model_logical_key": "gpt_image_2",
            "image_api_key": "new-image-key",
            "image_api_base_url": "https://api.hellobabygo.com",
            "apply_video": False,
            "force_restart_submitted": False,
        },
        default_workflow_definition(),
    )

    image_state = task.node_states["image_stage_1"]
    video_state = task.node_states["video_stage_1"]
    assert summary["image_nodes_reset"] >= 1
    assert summary["skipped_running_nodes"] == 0
    assert image_state["status"] == NODE_STATUS_PENDING
    assert image_state["provider"] == "hellobabygo_image"
    assert image_state["task_id"] is None
    assert task.image_provider == "hellobabygo_image"
    assert task.image_task_id is None
    assert video_state["status"] == NODE_STATUS_POLLING
    assert video_state["provider"] == "xibapi_veo"
    assert video_state["task_id"] == "keep-video-task-id"


def test_switch_video_provider_skips_completed_and_preserves_existing_task_id_by_default() -> None:
    batch = TaskBatch(
        batch_id="batch-runtime-switch",
        batch_name="runtime switch",
        source_excel_path="",
        imported_at="2026-05-15 00:00:00",
        task_count=2,
        batch_config={},
        workflow_definition=default_workflow_definition(),
    )
    completed = _task(3)
    completed.node_states["video_stage_1"]["status"] = NODE_STATUS_COMPLETED
    completed.node_states["video_stage_1"]["output_video_url"] = "https://cdn/video.mp4"
    completed.node_states["video_stage_1"]["output_video_local_path"] = r"C:\tmp\video.mp4"

    submitted = _task(4)
    submitted.node_states["video_stage_1"]["status"] = NODE_STATUS_SUBMITTED
    submitted.node_states["video_stage_1"]["task_id"] = "keep-polling-task-id"
    submitted.video_task_id = "keep-polling-task-id"

    summary = apply_runtime_provider_switch(
        batch,
        [completed, submitted],
        {
            "apply_image": False,
            "apply_video": True,
            "video_provider": "hellobabygo_veo",
            "video_model_logical_key": "veo_3_1_fast_portrait_fl_hd",
            "video_api_key": "new-video-key",
            "video_api_base_url": "https://api.hellobabygo.com",
            "force_restart_submitted": False,
        },
        default_workflow_definition(),
    )

    assert summary["video_nodes_reset"] == 2
    assert summary["skipped_completed_nodes"] == 1
    assert summary["skipped_task_id_nodes"] == 1
    assert completed.node_states["video_stage_1"]["provider"] == "xibapi_veo"
    assert submitted.node_states["video_stage_1"]["provider"] == "xibapi_veo"
    assert completed.node_states["video_stage_2"]["provider"] == "hellobabygo_veo"
    assert submitted.node_states["video_stage_2"]["provider"] == "hellobabygo_veo"
    assert submitted.node_states["video_stage_1"]["task_id"] == "keep-polling-task-id"
    assert batch.batch_config["video_provider"] == "hellobabygo_veo"


def main() -> None:
    test_switch_image_provider_resets_failed_image_without_touching_submitted_video()
    test_switch_image_provider_resets_failed_image_even_when_old_task_id_exists()
    test_switch_image_provider_resets_active_image_nodes_without_force_restart()
    test_switch_video_provider_skips_completed_and_preserves_existing_task_id_by_default()
    print("runtime provider switch self-test passed")


if __name__ == "__main__":
    main()
