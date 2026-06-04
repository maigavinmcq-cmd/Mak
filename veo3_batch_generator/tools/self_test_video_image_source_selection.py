from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import AppConfig
from app.workflow_executor import WorkflowExecutor


class _MultipartVideoProvider:
    requires_remote_image_url = False


def main() -> None:
    executor = WorkflowExecutor(AppConfig())
    bad_netdisk_url = "https://media.pennitech.top:48443/share/image_stage_1_output.png"
    unc_image_path = r"\\192.168.1.6\004.短视频运营中心\麦超群\01.Veo3任务资料\batch\pid\row_1\image_stage_1_output.png"

    selected = executor._preferred_video_image_source(
        [bad_netdisk_url, unc_image_path],
        _MultipartVideoProvider(),
    )

    assert selected == unc_image_path, selected
    print("video image source selection self-test passed")


if __name__ == "__main__":
    main()
