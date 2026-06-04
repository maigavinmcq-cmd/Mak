from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _install_task_stub() -> None:
    """BatchManager imports task.py, which needs pydantic in the full app.

    This test only exercises batch index repair/deletion, so a tiny TaskStatus
    stub keeps the test dependency-free while still using the real BatchManager
    implementation.
    """
    module = types.ModuleType("app.models.task")

    class TaskStatus:
        COMPLETED = "COMPLETED"
        VIDEO_DOWNLOADED = "VIDEO_DOWNLOADED"
        VIDEO_FAILED = "VIDEO_FAILED"
        VIDEO_TIMEOUT = "VIDEO_TIMEOUT"
        VIDEO_POLLING = "VIDEO_POLLING"
        PENDING = "PENDING"

        @classmethod
        def is_running(cls, status: str) -> bool:
            return status in set()

    module.TaskItem = object
    module.TaskStatus = TaskStatus
    sys.modules["app.models.task"] = module


def main() -> None:
    _install_task_stub()

    from app.batch_manager import BatchManager
    from app.models.batch import TaskBatch

    with tempfile.TemporaryDirectory() as tmp:
        manager = BatchManager(Path(tmp), "batches")
        batch = TaskBatch(
            batch_id="2026-05-14_123700",
            batch_name="will be deleted",
            source_excel_path="same.xlsx",
            imported_at="2026-05-14 12:37:00",
            task_count=422,
        )
        manager.save_batch(batch)
        manager.upsert_index(batch)

        manager.remove_batch_record(batch.batch_id, remove_batch_files=False)

        # The metadata directory is intentionally left behind by the "delete
        # record only" flow. It must not be resurrected as an empty batch card.
        assert manager.batch_dir(batch.batch_id).exists()
        loaded_ids = [item.batch_id for item in manager.load_index()]
        assert batch.batch_id not in loaded_ids, loaded_ids

        ghost = TaskBatch(
            batch_id="2026-05-14_ghost",
            batch_name="ghost",
            source_excel_path="",
            imported_at="2026-05-14 12:38:00",
            task_count=0,
        )
        manager.batch_dir(ghost.batch_id).mkdir(parents=True, exist_ok=True)
        manager.save_index([ghost])
        loaded_ids = [item.batch_id for item in manager.load_index()]
        assert ghost.batch_id not in loaded_ids, loaded_ids

    print("batch delete no ghost self-test passed")


if __name__ == "__main__":
    main()
