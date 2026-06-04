from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.task import TaskItem
from app.task_logs import append_task_log, export_task_logs, task_logs_to_text


def make_task() -> TaskItem:
    return TaskItem(
        row_index=2,
        task_name="VEO_TEST_TASK",
        pid="1730000000000000000",
        owner="tester",
        netdisk_path=r"\\server\share\pid",
        image_prompt="image prompt",
        video_prompt="video prompt",
        batch_id="2026-05-18_120000",
        batch_name="test_batch",
    )


def main() -> None:
    task = make_task()
    append_task_log(task, "INFO", "TASK", "task created")
    append_task_log(task, "ERROR", "VIDEO", "video submit failed", detail="HTTP 400", node_id="video_stage_1")

    text = task_logs_to_text(task)
    assert "task created" in text
    assert "video_stage_1" in text
    assert "HTTP 400" in text

    restored = TaskItem.model_validate(json.loads(task.model_dump_json()))
    assert len(restored.task_logs) == 2
    assert restored.task_logs[1].detail == "HTTP 400"

    with tempfile.TemporaryDirectory() as tmp:
        paths = export_task_logs(restored, Path(tmp))
        assert paths["txt"].exists()
        assert paths["json"].exists()
        assert "video submit failed" in paths["txt"].read_text(encoding="utf-8")
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        assert data["task"]["task_uid"]
        assert data["logs"][1]["node_id"] == "video_stage_1"

    print("self_test_task_logs passed")


if __name__ == "__main__":
    main()
