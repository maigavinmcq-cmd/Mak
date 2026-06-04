from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI_PATH = ROOT / "app" / "gui.py"
WORKER_PATH = ROOT / "app" / "worker.py"


def _method_body(source: str, name: str) -> str:
    pattern = re.compile(rf"^    def {re.escape(name)}\(", re.MULTILINE)
    match = pattern.search(source)
    assert match, f"{name} not found"
    next_match = re.search(r"^    def ", source[match.end() :], re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(source)
    return source[match.start() : end]


def main() -> None:
    gui_source = GUI_PATH.read_text(encoding="utf-8")
    worker_source = WORKER_PATH.read_text(encoding="utf-8")
    start_worker = _method_body(gui_source, "start_worker")
    run_with_feedback = _method_body(gui_source, "run_with_feedback")

    assert "BACKGROUND_STARTED" in run_with_feedback, "run_with_feedback must close the launch overlay once the background worker is started"
    assert "return \"BACKGROUND_STARTED\"" in start_worker, "start_worker should report that the long job moved to background"
    assert "setup_logger(" not in start_worker, "start_worker must not open network log files on the Qt UI thread"

    after_thread_start = start_worker.split("self.worker.start()", 1)[-1]
    assert "self.batch_manager.update_batch(" not in after_thread_start, "start_worker must not write batch_info/index on the Qt UI thread after worker.start()"
    assert "self.refresh_batch_combo()" not in after_thread_start, "start_worker must not rebuild the batch card list synchronously after worker.start()"
    assert "self.queue_batch_update(" in after_thread_start, "start_worker should defer batch index/stat writes through the UI queue"

    assert "log_dir" in worker_source and "log_path" in worker_source, "BatchWorker should support deferred worker-thread logger setup"
    assert "_ensure_file_logger" in worker_source, "BatchWorker should initialize the file logger inside the worker thread"


if __name__ == "__main__":
    main()
