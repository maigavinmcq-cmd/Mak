from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import AppConfig
from app.file_utils import safe_pid, task_batch_part, task_date, video_identifier
from app.models.task import TaskItem, TaskStatus
from app.workflow import NODE_STATUS_COMPLETED, default_workflow_definition, ensure_task_node_states
from app.workflow_executor import WorkflowExecutor


def _write_minimal_valid_mp4(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
        + b"\x00" * 2048
        + b"mdat"
        + b"\x00" * 2048
        + b"moov"
        + b"\x00" * 2048
    )


def test_repair_archived_video_path_updates_node_and_task_status() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        task = TaskItem(
            row_index=7,
            task_name="VEO_20260514_010051_",
            pid="1729760163146663641",
            owner="丁孟岚",
            netdisk_path=r"\\192.168.1.6\004.短视频运营中心\01.产品信息\SG\1729760163146663641",
            image_prompt="image",
            video_prompt="video",
            batch_date="2026-05-15",
            task_added_date="2026-05-15",
            batch_id="2026-05-15_073003",
            status=TaskStatus.VIDEO_DOWNLOAD_PENDING,
        )
        workflow = default_workflow_definition()
        ensure_task_node_states(task, workflow)
        state = task.node_states["video_stage_1"]
        video_url = "https://videos-us3.ss2.life/generated-1/b1c69ea4c9.mp4"
        state["status"] = NODE_STATUS_COMPLETED
        state["output_video_url"] = video_url
        state["task_id"] = "task_abc"

        ident = video_identifier(video_url, "task_abc")
        archived_path = (
            root
            / task.owner
            / task_date(task)
            / task_batch_part(task)
            / safe_pid(task.pid)
            / task.task_name
            / f"video_stage_1_output_{ident}.mp4"
        )
        _write_minimal_valid_mp4(archived_path)

        config = AppConfig(video_download_root=root)
        executor = WorkflowExecutor(config, workflow)
        repaired = executor.repair_archived_video_paths(task)

        assert repaired == 1
        assert task.node_states["video_stage_1"]["output_video_local_path"] == str(archived_path)
        assert task.video_file_path == str(archived_path)
        assert task.status == TaskStatus.VIDEO_DOWNLOADED


if __name__ == "__main__":
    test_repair_archived_video_path_updates_node_and_task_status()
    print("PASS: workflow repair restores archived video paths and terminal status")
