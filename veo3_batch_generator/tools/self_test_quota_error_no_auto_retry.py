from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import AppConfig
from app.workflow import default_workflow_definition
from app.workflow_executor import WorkflowExecutor


def main() -> None:
    workflow = default_workflow_definition()
    executor = WorkflowExecutor(
        AppConfig(
            retry_count=5,
            auto_retry_failed_workflow_enabled=True,
            auto_retry_image_nodes=True,
            auto_retry_video_nodes=True,
        ),
        workflow,
    )
    video_node = next(node for node in workflow["nodes"] if node["node_id"] == "video_stage_1")

    quota_state = {
        "status": "FAILED",
        "auto_retry_count": 0,
        "error_message": (
            "xibapi 视频提交参数或鉴权错误：HTTP 403 "
            "{'code': 'insufficient_user_quota', 'message': '预扣费额度失败, 用户剩余额度: ¥0.072000'}"
        ),
    }
    assert not executor._can_auto_retry_node(video_node, quota_state)

    transient_state = {
        "status": "FAILED",
        "auto_retry_count": 0,
        "error_message": "xibapi 视频提交 HTTP 502: Bad gateway",
    }
    assert executor._can_auto_retry_node(video_node, transient_state)

    print("quota/auth errors are not auto-retried")


if __name__ == "__main__":
    main()
