from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.task import TaskItem, TaskStatus
from app.task_patch import merge_task_patch
from app.workflow import NODE_STATUS_COMPLETED


def _task() -> TaskItem:
    return TaskItem(
        row_index=2,
        task_name="VEO_20260514_011246_",
        pid="1732698690774861288",
        owner="owner-a",
        netdisk_path=r"\\server\share\pid",
        image_prompt="image prompt",
        video_prompt="video prompt",
        prompt_stage_1="stage 1",
        prompt_stage_2="stage 2",
        product_image_path=r"\\server\share\pid\01.产品白底图\source.png",
        product_image_url="https://media.example/source.png",
        generated_image_path=r"\\server\assets\image_stage_1_output.png",
        generated_image_url="https://media.example/image_stage_1_output.png",
        image_task_id="img-task-1",
        image_provider="old_image_provider",
        image_model_logical_key="old_image_model",
        image_model_display="Old Image Model",
        video_url="https://cdn.example/video.mp4",
        video_file_path=r"\\server\videos\video_stage_1_output.mp4",
        video_task_id="video-task-1",
        video_provider="old_video_provider",
        video_model_logical_key="old_video_model",
        video_model_display="Old Video Model",
        status=TaskStatus.VIDEO_DOWNLOADED,
        batch_id="batch-1",
        batch_name="batch name",
        source_excel_path=r"C:\tmp\tasks.xlsx",
    )


def test_empty_process_patch_does_not_clear_durable_task_fields() -> None:
    task = _task()
    merge_task_patch(
        task,
        {
            "task_uid": "",
            "task_name": "",
            "owner": "",
            "product_image_path": None,
            "product_image_url": "",
            "generated_image_path": None,
            "generated_image_url": "",
            "image_task_id": None,
            "image_provider": "",
            "image_model_logical_key": "",
            "image_model_display": "",
            "video_url": None,
            "video_file_path": "",
            "video_task_id": None,
            "video_provider": "",
            "video_model_logical_key": "",
            "video_model_display": "",
            "source_excel_path": "",
            "status": TaskStatus.VIDEO_DOWNLOADING,
        },
    )

    assert task.task_name == "VEO_20260514_011246_"
    assert task.owner == "owner-a"
    assert task.product_image_path == r"\\server\share\pid\01.产品白底图\source.png"
    assert task.product_image_url == "https://media.example/source.png"
    assert task.generated_image_path == r"\\server\assets\image_stage_1_output.png"
    assert task.generated_image_url == "https://media.example/image_stage_1_output.png"
    assert task.image_task_id == "img-task-1"
    assert task.image_provider == "old_image_provider"
    assert task.video_url == "https://cdn.example/video.mp4"
    assert task.video_file_path == r"\\server\videos\video_stage_1_output.mp4"
    assert task.video_task_id == "video-task-1"
    assert task.video_provider == "old_video_provider"
    assert task.source_excel_path == r"C:\tmp\tasks.xlsx"
    assert task.status == TaskStatus.VIDEO_DOWNLOADED


def test_empty_process_patch_does_not_clear_durable_node_fields() -> None:
    task = _task()
    task.node_states = {
        "video_stage_1": {
            "status": NODE_STATUS_COMPLETED,
            "task_id": "video-task-1",
            "output_video_url": "https://cdn.example/video.mp4",
            "output_video_local_path": r"\\server\videos\video_stage_1_output.mp4",
            "input_images": [r"\\server\assets\image_stage_1_output.png"],
        }
    }

    merge_task_patch(
        task,
        {
            "node_states": {
                "video_stage_1": {
                    "task_id": None,
                    "output_video_url": "",
                    "output_video_local_path": None,
                    "input_images": [],
                    "status": NODE_STATUS_COMPLETED,
                    "download_status": "DOWNLOADED",
                }
            }
        },
    )

    state = task.node_states["video_stage_1"]
    assert state["task_id"] == "video-task-1"
    assert state["output_video_url"] == "https://cdn.example/video.mp4"
    assert state["output_video_local_path"] == r"\\server\videos\video_stage_1_output.mp4"
    assert state["input_images"] == [r"\\server\assets\image_stage_1_output.png"]
    assert state["download_status"] == "DOWNLOADED"


def test_stale_process_patch_does_not_regress_downloaded_task_status() -> None:
    task = _task()
    task.node_states = {
        "video_stage_1": {
            "status": NODE_STATUS_COMPLETED,
            "task_id": "video-task-1",
            "output_video_url": "https://cdn.example/video.mp4",
            "output_video_local_path": r"\\server\videos\video_stage_1_output.mp4",
            "download_status": "DOWNLOADED",
        }
    }

    merge_task_patch(
        task,
        {
            "status": TaskStatus.PENDING,
            "video_file_path": "",
            "node_states": {
                "video_stage_1": {
                    "status": NODE_STATUS_COMPLETED,
                    "output_video_local_path": "",
                    "download_status": "QUEUED",
                }
            },
        },
    )

    assert task.status == TaskStatus.VIDEO_DOWNLOADED
    assert task.video_file_path == r"\\server\videos\video_stage_1_output.mp4"
    assert task.node_states["video_stage_1"]["output_video_local_path"] == r"\\server\videos\video_stage_1_output.mp4"


def main() -> None:
    test_empty_process_patch_does_not_clear_durable_task_fields()
    test_empty_process_patch_does_not_clear_durable_node_fields()
    test_stale_process_patch_does_not_regress_downloaded_task_status()
    print("task patch merge self-test passed")


if __name__ == "__main__":
    main()
