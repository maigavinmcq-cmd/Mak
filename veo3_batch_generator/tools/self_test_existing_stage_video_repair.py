from __future__ import annotations

import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.file_utils import (
    build_existing_video_archive_index,
    find_existing_stage_video_path,
    safe_pid,
    task_batch_part,
    task_date,
    video_identifier,
)
from app.models.task import TaskItem


def _write_minimal_valid_mp4(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
        + b"\x00" * 2048
        + b"mdat"
        + b"\x00" * 2048
        + b"moov"
        + b"\x00" * 2048
    )


def _task() -> TaskItem:
    return TaskItem(
        row_index=7,
        task_name="VEO_20260514_010051_",
        pid="1729760163146663641",
        owner="丁孟岚",
        netdisk_path=r"\\192.168.1.6\004.短视频运营中心\01.产品信息\SG\1729760163146663641",
        image_prompt="image",
        video_prompt="video",
        batch_date="2026-05-15",
        task_added_date="2026-05-15",
        batch_id="2026-05-15_073003",
    )


def test_finds_new_stage_video_layout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        task = _task()
        video_url = "https://videos-us3.ss2.life/generated-1/b1c69ea4c9.mp4"
        ident = video_identifier(video_url, "task_abc")
        path = (
            root
            / task.owner
            / task_date(task)
            / task_batch_part(task)
            / safe_pid(task.pid)
            / task.task_name
            / f"video_stage_1_output_{ident}.mp4"
        )
        _write_minimal_valid_mp4(path)

        found = find_existing_stage_video_path(
            root,
            task,
            "video_stage_1",
            True,
            video_url=video_url,
            task_id="task_abc",
        )

        assert found == path


def test_finds_legacy_row_video_layout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        task = _task()
        video_url = "https://videos-us3.ss2.life/generated-1/legacy.mp4"
        ident = video_identifier(video_url, "task_legacy")
        path = (
            root
            / task.owner
            / task_date(task)
            / task_batch_part(task)
            / safe_pid(task.pid)
            / f"{safe_pid(task.pid)}_row{task.row_index}_{ident}.mp4"
        )
        _write_minimal_valid_mp4(path)

        found = find_existing_stage_video_path(
            root,
            task,
            "video_stage_1",
            True,
            video_url=video_url,
            task_id="task_legacy",
        )

        assert found == path


def test_batch_video_archive_index_finds_stage_video_without_per_task_scan() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        task = _task()
        video_url = "https://videos-us3.ss2.life/generated-1/indexed.mp4"
        ident = video_identifier(video_url, "task_indexed")
        path = (
            root
            / task.owner
            / task_date(task)
            / task_batch_part(task)
            / safe_pid(task.pid)
            / task.task_name
            / f"video_stage_1_output_{ident}.mp4"
        )
        _write_minimal_valid_mp4(path)
        index = build_existing_video_archive_index(root, [task], True)

        found = find_existing_stage_video_path(
            root,
            task,
            "video_stage_1",
            True,
            video_url=video_url,
            task_id="task_indexed",
            archive_index=index,
        )

        assert found == path


if __name__ == "__main__":
    test_finds_new_stage_video_layout()
    test_finds_legacy_row_video_layout()
    test_batch_video_archive_index_finds_stage_video_without_per_task_scan()
    print("PASS: existing stage video repair lookup finds new and legacy archived videos")
