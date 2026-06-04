from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    worker = (ROOT / "app" / "worker.py").read_text(encoding="utf-8")
    assert "state_save_error" in worker, "BatchWorker should preserve live emits when task_state save fails"
    assert "live UI update was still emitted" in worker, "save failure should be logged as deferred, not block UI"
    assert "self.task_updated.emit(task)" in worker, "task_updated must still emit after save attempts"
    assert worker.index("except Exception as exc:", worker.index("def _save_emit")) < worker.index(
        "self.task_updated.emit(task)", worker.index("def _save_emit")
    ), "save exceptions must be caught before task_updated emit"
    assert "task_state final flush deferred" in worker, "final flush should not crash worker on flaky network shares"
    print("worker emit resilience static checks passed")


if __name__ == "__main__":
    main()
