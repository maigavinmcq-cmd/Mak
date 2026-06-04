from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK_MANAGER_PATH = ROOT / "app" / "task_manager.py"


def main() -> None:
    source = TASK_MANAGER_PATH.read_text(encoding="utf-8")
    assert "def _unique_tmp_state_path" in source, "TaskManager should use a per-process temp state path"
    assert "os.getpid()" in source, "temp state path should include process id to avoid UI/worker collisions"
    assert "threading.get_ident()" in source, "temp state path should include thread id"
    assert 'with_suffix(self.state_path.suffix + ".tmp")' not in source, "fixed task_state.json.tmp races across processes"
    assert "except FileNotFoundError" in source, "save_state should retry when a network tmp file disappears"


if __name__ == "__main__":
    main()
