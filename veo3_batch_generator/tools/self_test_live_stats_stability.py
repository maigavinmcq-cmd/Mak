from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.stats_utils import stabilize_live_completion_stats


def main() -> None:
    previous = {"total": 100, "completed": 25, "failed": 5, "skipped": 1, "timeout": 0}

    stale = stabilize_live_completion_stats(
        previous,
        {"total": 100, "completed": 22, "failed": 3, "skipped": 0, "timeout": 0},
        same_batch=True,
        active_running=True,
    )
    assert stale["completed"] == 25, "running dashboard must not regress on stale stats snapshots"
    assert stale["failed"] == 5, "running batch cards must not show stale lower failed counts while polling"
    assert stale["skipped"] == 1, "running batch cards must not show stale lower skipped counts while polling"

    newer = stabilize_live_completion_stats(
        stale,
        {"total": 100, "completed": 26, "failed": 6, "skipped": 1, "timeout": 0},
        same_batch=True,
        active_running=True,
    )
    assert newer["completed"] == 26
    assert newer["failed"] == 6

    other_batch = stabilize_live_completion_stats(
        newer,
        {"total": 12, "completed": 1, "failed": 0},
        same_batch=False,
        active_running=True,
    )
    assert other_batch["completed"] == 1, "switching batches must reset the stability floor"

    idle_refresh = stabilize_live_completion_stats(
        newer,
        {"total": 100, "completed": 20, "failed": 0},
        same_batch=True,
        active_running=False,
    )
    assert idle_refresh["completed"] == 20, "manual reloads should show persisted data exactly"

    gui_source = (PROJECT_ROOT / "app" / "gui.py").read_text(encoding="utf-8")
    apply_start = gui_source.index("    def apply_batch_card_update")
    apply_end = gui_source.index("    def queue_table_refresh", apply_start)
    apply_source = gui_source[apply_start:apply_end]
    assert "_stable_batch_card_stats" in apply_source, "batch card updates must use stable live stats"

    refresh_start = gui_source.index("    def _refresh_batch_table_impl")
    refresh_end = gui_source.index("    def set_batch_quick_filter", refresh_start)
    refresh_source = gui_source[refresh_start:refresh_end]
    assert "_stable_batch_card_stats" in refresh_source, "batch list rebuilds must preserve stable running-card stats"

    print("live stats stability self-test passed")


if __name__ == "__main__":
    main()
