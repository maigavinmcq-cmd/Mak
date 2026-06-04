from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.task import TaskItem, TaskStatus
from app.runtime.worker_events import append_event, read_events
from app.runtime.worker_process import BufferedEventWriter, compact_task_event
from app.task_manager import TaskManager


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def make_large_task() -> TaskItem:
    task = TaskItem(
        row_index=2,
        task_name="VEO_TEST",
        pid="pid123",
        netdisk_path=r"\\192.168.1.6\004\SG\pid123",
        image_prompt="image prompt " * 5000,
        video_prompt="video prompt " * 5000,
        batch_id="batch_test",
    )
    task.task_uid = TaskManager.task_uid_for(task)
    task.status = TaskStatus.GENERATING_IMAGE
    task.node_states = {
        "image_stage_1": {
            "node_id": "image_stage_1",
            "node_name": "图1",
            "node_type": "image_generation",
            "status": "POLLING",
            "task_id": "task_img_1",
            "raw_response": {
                "id": "task_img_1",
                "data": [{"b64_json": "a" * 500_000}],
                "message": "ok",
            },
            "input_images": ["data:image/png;base64," + "b" * 200_000],
        }
    }
    task.image_raw_response = {"data": [{"b64_json": "c" * 500_000}], "id": "task_img_1"}
    return task


def main() -> None:
    task = make_large_task()
    manager = TaskManager(Path(tempfile.gettempdir()) / "task_state_runtime_event_test.json")

    state_dump = manager._compact_task_dump(task)
    state_text = json.dumps(state_dump, ensure_ascii=False)
    assert_true("a" * 1000 not in state_text, "state dump must omit large node raw_response base64")
    assert_true("data:image/png;base64," + "b" * 1000 not in state_text, "state dump must omit large node image input")

    event_patch = compact_task_event(task)
    event_text = json.dumps(event_patch, ensure_ascii=False)
    assert_true(len(event_text) < 20_000, f"event patch too large: {len(event_text)} bytes")
    assert_true("image prompt" not in event_text, "UI event patch must not include long prompts")
    assert_true("raw_response" not in event_text, "UI event patch must not include raw responses")
    assert_true(event_patch["node_states"]["image_stage_1"]["task_id"] == "task_img_1", "node task_id must be preserved")

    with tempfile.TemporaryDirectory() as tmp_dir:
        events_path = Path(tmp_dir) / "events.jsonl"
        for index in range(5):
            append_event(events_path, "task_updated", {"index": index, "task": event_patch})
        events, offset = read_events(events_path, 0, max_events=2)
        assert_true(len(events) == 2, "read_events max_events should limit one UI tick")
        more, _ = read_events(events_path, offset, max_events=10)
        assert_true(len(more) == 3, "read_events offset should continue from prior tick")

    with tempfile.TemporaryDirectory() as tmp_dir:
        events_path = Path(tmp_dir) / "events.jsonl"
        events_path.write_text(("old event payload\n" * 100), encoding="utf-8")
        writer = BufferedEventWriter(events_path, flush_interval=0.05, max_batch=10, max_file_bytes=512)
        writer.start()
        for index in range(20):
            writer.emit("log", {"index": index, "message": "new event"})
        time.sleep(0.2)
        writer.close()
        assert_true(events_path.stat().st_size < 4096, "active worker_events file must stay bounded after rotation")
        rotated_path = events_path.with_name(events_path.name + ".1")
        assert_true(rotated_path.exists(), "event rotation should preserve one previous file for diagnostics")
        reset_events, reset_offset = read_events(events_path, 10_000_000, max_events=10)
        assert_true(reset_events, "read_events must recover when caller offset points past a rotated file")
        assert_true(reset_offset <= events_path.stat().st_size, "offset after reset must stay inside the active file")

    print("runtime event compaction self-test passed")


if __name__ == "__main__":
    main()
