from __future__ import annotations

import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.batch_manager import BatchManager
from app.models.batch import TaskBatch
from app.models.task import TaskItem, TaskStatus
from app.task_manager import TaskManager
from app.workflow import NODE_STATUS_POLLING, NODE_STATUS_RUNNING, NODE_STATUS_SUBMITTED, NODE_TYPE_IMAGE, NODE_TYPE_VIDEO


def make_task(row: int, status: str) -> TaskItem:
    return TaskItem(
        row_index=row,
        pid=f"pid_{row}",
        netdisk_path="",
        image_prompt="image prompt",
        video_prompt="video prompt",
        status=status,
    )


def main() -> None:
    tasks = [
        make_task(1, TaskStatus.VIDEO_DOWNLOADED),
        make_task(2, TaskStatus.COMPLETED),
        make_task(3, TaskStatus.VIDEO_DONE),
        make_task(4, TaskStatus.FAILED_VIDEO_API),
        make_task(5, TaskStatus.VIDEO_TIMEOUT),
        make_task(6, TaskStatus.SKIPPED_EMPTY_PROMPT),
        make_task(7, TaskStatus.PENDING),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        manager = TaskManager(Path(tmp) / "task_state.json")
        manager.tasks = tasks
        stats = manager.stats()
        assert stats["total"] == 7
        assert stats["completed"] == 1, "Only VIDEO_DOWNLOADED should count as a full workflow completion"

        batch_manager = BatchManager(Path(tmp) / "logs")
        batch = TaskBatch(batch_id="batch_1", batch_name="batch_1", source_excel_path="", imported_at="2026-05-15 00:00:00")
        batch_manager.update_stats(batch, tasks, save=False)
        assert batch.completed_count == 1
        assert batch.progress_percent == round(1 / 7 * 100, 2)
        assert batch.success_rate == round(1 / 7 * 100, 2), "Success rate should be completed / total, not completed / ended"

        node_task = make_task(8, TaskStatus.PENDING)
        node_task.node_states = {
            "image_stage_1": {"node_type": NODE_TYPE_IMAGE, "status": NODE_STATUS_RUNNING},
            "video_stage_1": {"node_type": NODE_TYPE_VIDEO, "status": NODE_STATUS_SUBMITTED},
            "video_stage_2": {"node_type": NODE_TYPE_VIDEO, "status": NODE_STATUS_POLLING},
        }
        manager.tasks = [node_task]
        node_stats = manager.stats()
        assert node_stats["image_running"] == 1, "Node RUNNING image stage must update dashboard image-running stats"
        assert node_stats["submitted"] == 1, "Node SUBMITTED video stage must update dashboard submitted stats"
        assert node_stats["polling"] == 1, "Node POLLING video stage must update dashboard polling stats"
        assert node_stats["pending"] == 0, "Active node work should not still be counted as pending"
        assert node_stats["running"] == 1, "Active node work should count as running even if aggregate status is stale"

    gui_source = (PROJECT_ROOT / "app" / "gui.py").read_text(encoding="utf-8")
    assert "remaining = max(total - completed, 0)" in gui_source, "Estimated remaining should only subtract full completions"
    assert "success_rate = (completed / total * 100)" in gui_source, "Top success rate should be completed / total"
    print("self_test_stats_full_workflow_completion: PASS")


if __name__ == "__main__":
    main()
