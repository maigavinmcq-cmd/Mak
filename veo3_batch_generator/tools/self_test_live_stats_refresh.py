from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.models.batch import TaskBatch
from app.stats_utils import apply_stats_to_batch_snapshot, stabilize_live_completion_stats


def main() -> None:
    previous = {
        "total": 614,
        "completed": 156,
        "failed": 435,
        "timeout": 2,
        "skipped": 1,
    }
    incoming = {
        "total": 614,
        "completed": 155,
        "failed": 42,
        "timeout": 0,
        "skipped": 0,
    }
    stable = stabilize_live_completion_stats(previous, incoming, same_batch=True, active_running=True)
    assert stable["completed"] == 156, "completed should still be protected from stale snapshots"
    assert stable["failed"] == 42, "failed must refresh live when retry resets failed tasks"
    assert stable["timeout"] == 0
    assert stable["skipped"] == 0

    batch = TaskBatch(
        batch_id="batch_live_stats",
        batch_name="batch_live_stats",
        source_excel_path="",
        imported_at="2026-05-29 17:00:00",
        task_count=614,
        completed_count=151,
        failed_count=431,
        status="RUNNING",
    )
    apply_stats_to_batch_snapshot(
        batch,
        {
            "total": 614,
            "completed": 156,
            "failed": 435,
            "timeout": 0,
            "skipped": 0,
            "pending": 13,
            "polling": 389,
            "running": 1,
        },
        active_running=True,
    )
    assert batch.task_count == 614
    assert batch.completed_count == 156
    assert batch.failed_count == 435
    assert batch.pending_count == 13
    assert batch.polling_count == 389
    assert batch.status == "RUNNING"

    print("live stats refresh self-test passed")


if __name__ == "__main__":
    main()
