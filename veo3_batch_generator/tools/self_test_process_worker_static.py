from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI_PATH = ROOT / "app" / "gui.py"
RUNTIME_EVENTS = ROOT / "app" / "runtime" / "worker_events.py"
RUNTIME_PROCESS = ROOT / "app" / "runtime" / "worker_process.py"
DISCOVER_TASK_IDS = ROOT / "tools" / "discover_video_task_ids.py"


def _method_body(source: str, name: str) -> str:
    pattern = re.compile(rf"^    def {re.escape(name)}\(", re.MULTILINE)
    match = pattern.search(source)
    assert match, f"{name} not found"
    next_match = re.search(r"^    def ", source[match.end() :], re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(source)
    return source[match.start() : end]


def main() -> None:
    assert RUNTIME_EVENTS.exists(), "worker_events.py should define the JSONL event bridge"
    assert RUNTIME_PROCESS.exists(), "worker_process.py should define the isolated background runner"
    assert DISCOVER_TASK_IDS.exists(), "video task-id recovery helper should exist"

    gui_source = GUI_PATH.read_text(encoding="utf-8")
    start_worker = _method_body(gui_source, "start_worker")

    assert "subprocess.Popen" in gui_source, "GUI should launch background execution with a separate process"
    assert "runtime_events_dir(self.config" in gui_source, "worker event bridge should use a local runtime IPC directory"
    assert 'project_outputs_dir(self.config, "runtime_events"' not in gui_source, (
        "worker event bridge must not live under project_outputs because that may be a slow network share"
    )
    assert 'PROJECT_ROOT / "outputs" / "runtime_events"' not in gui_source, "worker event bridge must not write huge runtime logs into local outputs"
    assert "_start_process_worker(" in start_worker, "start_worker should delegate long execution to the process supervisor"
    assert "self.worker = BatchWorker(" not in start_worker, "start_worker must not create the heavy BatchWorker in the UI process"
    assert "poll_process_worker_events" in gui_source, "GUI should poll worker JSONL events without blocking"
    assert "send_process_worker_command" in gui_source, "pause/resume/stop/poll should communicate with the worker process"

    process_source = RUNTIME_PROCESS.read_text(encoding="utf-8")
    assert "BatchWorker(" in process_source, "worker process should reuse existing BatchWorker execution logic"
    assert "worker.run()" in process_source, "worker process should run the execution engine outside the UI process"
    assert "watch_commands" in process_source, "worker process should watch pause/resume/stop commands"
    assert "--parent-pid" in gui_source and "os.getpid()" in gui_source, "GUI must pass its PID to background workers"
    assert "--parent-pid" in process_source and "watch_parent_process" in process_source, (
        "worker process must monitor the GUI parent and stop itself when orphaned"
    )
    assert "os._exit" in process_source, "orphaned workers need a final hard-exit fallback after graceful stop"
    assert "max_file_bytes" in process_source and "_rotate_if_needed" in process_source, (
        "worker_events.jsonl must be bounded so long runs cannot create huge network files"
    )
    assert '"download_concurrency"' in process_source, "worker process must honor per-batch download_concurrency"
    assert '"image_concurrency"' in process_source, "worker process must honor per-batch image_concurrency"
    assert '"video_submit_concurrency"' in process_source, "worker process must honor per-batch video_submit_concurrency"
    assert '"poll_concurrency"' in process_source, "worker process must honor per-batch poll_concurrency"

    discover_source = DISCOVER_TASK_IDS.read_text(encoding="utf-8")
    assert "tempfile.gettempdir()" in discover_source, "task-id recovery should scan the local runtime event directory"
    assert "runtime_event_roots" in discover_source, "task-id recovery should include both current and legacy event locations"


if __name__ == "__main__":
    main()
