from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    worker_process = (ROOT / "app" / "runtime" / "worker_process.py").read_text(encoding="utf-8")
    worker_events = (ROOT / "app" / "runtime" / "worker_events.py").read_text(encoding="utf-8")

    assert "DirectConnection" in worker_process, (
        "worker_process runs without a Qt event loop; worker signals emitted from ThreadPoolExecutor "
        "threads must use Qt.DirectConnection so task_updated events reach the UI"
    )
    assert "task_updated.connect" in worker_process and "DirectConnection" in worker_process
    assert "_APPEND_EVENT_LOCK" in worker_events, "JSONL event writes must be serialized across worker threads"
    assert "with _APPEND_EVENT_LOCK:" in worker_events, "append_event must hold the lock while writing one JSONL line"
    print("worker process direct signal static check passed")


if __name__ == "__main__":
    main()
