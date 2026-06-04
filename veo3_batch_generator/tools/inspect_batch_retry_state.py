from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: inspect_batch_retry_state.py TASK_STATE_JSON")
    path = Path(sys.argv[1])
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    tasks = data.get("tasks", [])
    print("tasks", len(tasks))
    summary: dict[tuple[str, str], int] = {}
    interesting: list[tuple] = []
    for task in tasks:
        states = task.get("node_states") or {}
        for node_id, state in states.items():
            if isinstance(state, dict):
                key = (node_id, str(state.get("status") or ""))
                summary[key] = summary.get(key, 0) + 1
        img = states.get("image_stage_1") or {}
        vid = states.get("video_stage_1") or {}
        has_img = bool(
            img.get("output_image_path")
            or img.get("output_image_url")
            or task.get("generated_image_path")
            or task.get("generated_image_url")
        )
        failed = (
            str(task.get("status") or "").startswith("FAILED")
            or img.get("status") == "FAILED"
            or vid.get("status") == "FAILED"
        )
        if failed and has_img:
            error = str(vid.get("error_message") or task.get("error_message") or "")[:120]
            interesting.append(
                (
                    task.get("row_index"),
                    task.get("status"),
                    img.get("status"),
                    bool(img.get("output_image_path") or task.get("generated_image_path")),
                    bool(img.get("output_image_url") or task.get("generated_image_url")),
                    vid.get("status"),
                    bool(vid.get("task_id") or task.get("video_task_id")),
                    error,
                )
            )
    print("node summary")
    for key, count in sorted(summary.items()):
        print(f"{key[0]}|{key[1]}={count}")
    print("failed_with_image", len(interesting))
    for row in interesting[:20]:
        print(row)


if __name__ == "__main__":
    main()
