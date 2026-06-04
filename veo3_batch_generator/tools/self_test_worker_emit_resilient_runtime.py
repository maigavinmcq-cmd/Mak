from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import AppConfig
from app.models.task import TaskItem
from app.worker import BatchWorker


class FailingSaveManager:
    def __init__(self) -> None:
        self.tasks: list[TaskItem] = []

    def save_state(self, force_backup: bool = False) -> None:
        raise PermissionError("simulated network share lock")

    def stats(self) -> dict:
        return {"total": len(self.tasks)}


def main() -> None:
    manager = FailingSaveManager()
    task = TaskItem(
        pid="test_pid",
        row_index=1,
        batch_id="test_batch",
        netdisk_path="",
        image_prompt="",
        video_prompt="",
    )
    task.task_uid = "test_batch::test_pid::row_1"
    manager.tasks = [task]
    worker = BatchWorker(manager, AppConfig(), None)
    seen: list[tuple[str, object]] = []
    worker.task_updated.connect(lambda emitted: seen.append(("task", emitted.task_uid)))
    worker.stats_updated.connect(lambda stats: seen.append(("stats", stats)))

    worker._save_emit(task, force=True)

    assert ("task", task.task_uid) in seen
    assert any(kind == "stats" for kind, _payload in seen)
    print("worker emit resilience runtime check passed")


if __name__ == "__main__":
    main()
