from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.task_manager import TaskManager


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "batches" / "2026-05-15_002015" / "task_state.json"
        manager = TaskManager(state_path)

        with patch("pathlib.Path.replace", side_effect=PermissionError("locked by another process")):
            saved = manager.save_state()

        assert saved is False, "save_state should report failure without raising on PermissionError"
        assert "PermissionError" in manager.last_save_error or "locked by another process" in manager.last_save_error
        assert manager.last_save_fallback_path, "save_state should keep a local fallback copy when network replace fails"
        fallback_path = Path(manager.last_save_fallback_path)
        assert fallback_path.exists(), "fallback state file should exist"
        assert "task_state" in fallback_path.name

    print("self_test_task_manager_save_permission_deferred: PASS")


if __name__ == "__main__":
    main()
