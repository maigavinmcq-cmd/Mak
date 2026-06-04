"""Static checks for the GUI batch run queue.

Run as:
    python tools/self_test_batch_run_queue_static.py
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "app" / "gui.py"


def test_run_queue_symbols_exist() -> None:
    source = GUI.read_text(encoding="utf-8")
    assert "BatchRunRequest" in source
    assert "self.batch_run_queue" in source
    assert "enqueue_batch_run(" in source
    assert "start_next_queued_batch(" in source


def test_running_batch_enqueue_does_not_stop_current_batch() -> None:
    source = GUI.read_text(encoding="utf-8")
    start_worker = source[source.index("    def start_worker(") : source.index("    def custom_poll(")]
    running_branch = start_worker[start_worker.index("if self.is_background_running():") : start_worker.index("batch = self.current_batch")]
    assert "enqueue_batch_run(" in running_branch
    assert "stop_worker()" not in running_branch
    assert "pending_start_request" not in running_branch


def test_finished_worker_advances_queue_after_normal_finish() -> None:
    source = GUI.read_text(encoding="utf-8")
    worker_finished = source[source.index("    def worker_finished(") : source.index("    def update_running_stats(")]
    assert "start_next_queued_batch(" in worker_finished
    assert "pending_start_request" not in worker_finished


def main() -> None:
    test_run_queue_symbols_exist()
    test_running_batch_enqueue_does_not_stop_current_batch()
    test_finished_worker_advances_queue_after_normal_finish()
    print("batch run queue static self-test passed")


if __name__ == "__main__":
    main()
