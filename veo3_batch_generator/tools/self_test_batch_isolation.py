from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.batch_manager import BatchManager
from app.file_utils import image_assets_dir, video_download_dir
from app.models.task import TaskItem, TaskStatus
from app.task_manager import TaskManager


def _make_tasks() -> list[TaskItem]:
    return [
        TaskItem(
            row_index=2,
            pid="1735039169102645078",
            owner="tester",
            netdisk_path=r"\\192.168.1.6\004.短视频运营中心\01.产品信息\SG\1735039169102645078",
            image_prompt="same image prompt",
            video_prompt="same video prompt",
        )
    ]


def _attach_batch(tasks: list[TaskItem], batch) -> None:
    for task in tasks:
        task.batch_id = batch.batch_id
        task.batch_name = batch.batch_name
        task.batch_date = batch.batch_id[:10]
        task.task_added_date = batch.batch_id[:10]
        task.imported_at = batch.imported_at
        task.source_excel_path = batch.source_excel_path
        task.task_uid = TaskManager.task_uid_for(task)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="veo3_batch_isolation_") as temp_dir:
        root = Path(temp_dir)
        manager = BatchManager(root / "software_logs")
        excel_path = root / "same_tasks.xlsx"
        excel_path.write_text("placeholder", encoding="utf-8")

        first_tasks = _make_tasks()
        first_batch = manager.create_batch(first_tasks, excel_path)
        _attach_batch(first_tasks, first_batch)
        first_state = TaskManager(manager.task_state_path(first_batch.batch_id))
        first_state.set_tasks(first_tasks)
        first_state_path = manager.task_state_path(first_batch.batch_id)
        first_state_before = first_state_path.read_text(encoding="utf-8")

        second_tasks = _make_tasks()
        second_batch = manager.create_batch(second_tasks, excel_path)
        _attach_batch(second_tasks, second_batch)
        second_state = TaskManager(manager.task_state_path(second_batch.batch_id))
        second_state.set_tasks(second_tasks)

        assert first_batch.batch_id != second_batch.batch_id
        assert manager.batch_dir(first_batch.batch_id).exists()
        assert manager.batch_dir(second_batch.batch_id).exists()
        assert first_state_path.read_text(encoding="utf-8") == first_state_before
        assert manager.task_state_path(second_batch.batch_id).exists()

        index_data = json.loads(manager.index_path.read_text(encoding="utf-8"))
        ids = [item["batch_id"] for item in index_data.get("batches", [])]
        assert first_batch.batch_id in ids
        assert second_batch.batch_id in ids

        image_root = root / "image_assets"
        video_root = root / "video_downloads"
        assert first_batch.batch_id in str(image_assets_dir(image_root, first_tasks[0]))
        assert second_batch.batch_id in str(image_assets_dir(image_root, second_tasks[0]))
        assert image_assets_dir(image_root, first_tasks[0]) != image_assets_dir(image_root, second_tasks[0])
        assert first_batch.batch_id in str(video_download_dir(video_root, first_tasks[0], True))
        assert second_batch.batch_id in str(video_download_dir(video_root, second_tasks[0], True))
        assert video_download_dir(video_root, first_tasks[0], True) != video_download_dir(video_root, second_tasks[0], True)

        legacy_state_path = manager.batch_dir(first_batch.batch_id) / "legacy_missing_batch_state.json"
        legacy_state_path.write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "row_index": 2,
                            "pid": first_tasks[0].pid,
                            "owner": first_tasks[0].owner,
                            "netdisk_path": first_tasks[0].netdisk_path,
                            "image_prompt": first_tasks[0].image_prompt,
                            "video_prompt": first_tasks[0].video_prompt,
                            "status": TaskStatus.COMPLETED,
                            "video_task_id": "task_saved_before_crash",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        legacy_manager = TaskManager(legacy_state_path)
        legacy_manager.configure_batch_context(
            first_batch.batch_id,
            first_batch.batch_name,
            first_batch.imported_at,
            first_batch.source_excel_path,
        )
        legacy_tasks = legacy_manager.load_state(allow_legacy=False, save_after_load=False)
        assert legacy_tasks[0].batch_id == first_batch.batch_id
        assert legacy_tasks[0].task_uid == f"{first_batch.batch_id}::{first_tasks[0].pid}::row_2"
        assert legacy_tasks[0].status == TaskStatus.COMPLETED
        assert legacy_tasks[0].video_task_id == "task_saved_before_crash"

        guarded_manager = TaskManager(manager.task_state_path(first_batch.batch_id))
        wrong_task = _make_tasks()[0]
        wrong_task.batch_id = second_batch.batch_id
        wrong_task.task_uid = TaskManager.task_uid_for(wrong_task)
        guarded_manager.tasks = [wrong_task]
        try:
            guarded_manager.save_state()
        except RuntimeError:
            pass
        else:
            raise AssertionError("saving a task into the wrong batch directory should be rejected")

        print("batch isolation self-test passed")
        print(f"first_batch={first_batch.batch_id}")
        print(f"second_batch={second_batch.batch_id}")


if __name__ == "__main__":
    main()
