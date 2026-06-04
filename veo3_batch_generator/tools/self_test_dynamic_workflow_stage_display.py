from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.gui import (
    stage_nodes_from_workflow_definition,
    task_flow_summary,
    workflow_stage_cells,
)
from app.models.task import TaskItem, TaskStatus


def main() -> None:
    workflow = {
        "workflow_version": "video_task_recovery_v1",
        "nodes": [
            {
                "node_id": "video_stage_1",
                "node_name": "视频任务恢复下载",
                "node_type": "video_generation",
                "input_refs": [],
                "enabled": True,
            }
        ],
    }
    task = TaskItem(
        row_index=1,
        task_name="recovery row",
        pid="pid-1",
        owner="",
        netdisk_path="",
        image_prompt="",
        video_prompt="",
        status=TaskStatus.VIDEO_DOWNLOADED,
        node_states={
            "video_stage_1": {
                "node_id": "video_stage_1",
                "node_name": "视频任务恢复下载",
                "node_type": "video_generation",
                "status": "COMPLETED",
                "output_video_url": "https://example.test/video.mp4",
                "output_video_local_path": r"\\192.168.1.6\share\video.mp4",
                "task_id": "v_recovered",
            }
        },
    )

    stage_nodes = stage_nodes_from_workflow_definition(workflow)
    assert [node_id for node_id, *_ in stage_nodes] == ["video_stage_1"]

    summary = task_flow_summary(task, stage_nodes)
    assert "视频任务恢复下载:视频已归档" in summary
    assert "视频1:轮询中" not in summary
    assert "图2:" not in summary

    cells = workflow_stage_cells(task, stage_nodes, max_columns=4)
    assert "视频已归档" in cells[0][0]
    assert cells[0][1] == "COMPLETED"
    assert cells[1][0] == ""
    assert cells[2][0] == ""
    assert cells[3][0] == ""

    print("dynamic workflow stage display self-test passed")


if __name__ == "__main__":
    main()
