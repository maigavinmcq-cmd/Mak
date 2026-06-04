from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import AppConfig
from app.ui_layout import batch_status_text, ensure_ui_config, layout_mode_for_width, task_status_text


def main() -> None:
    config = AppConfig()
    config.ui_layout = {"compact_breakpoint": 1600}
    config.ui_theme = {"primary": "#123456"}
    config.task_table_columns = {"compact_visible": ["任务名称", "任务状态"]}
    config.log_panel = {"visible_levels": ["ERROR"]}
    ensure_ui_config(config)

    assert config.ui_layout["layout_version"] == "ui_v2_responsive_console"
    assert config.ui_layout["compact_breakpoint"] == 1600
    assert config.ui_theme["primary"] == "#123456"
    assert "large_visible" in config.task_table_columns
    assert config.log_panel["visible_levels"] == ["ERROR"]

    assert batch_status_text("RUNNING") == "运行中"
    assert batch_status_text("COMPLETED") == "已完成"
    assert task_status_text("VIDEO_DOWNLOAD_PENDING") == "视频已生成，等待下载"
    assert task_status_text("VIDEO_DOWNLOADED") == "已归档完成"
    assert layout_mode_for_width(1920, config.ui_layout) == "large"
    assert layout_mode_for_width(1500, config.ui_layout) == "compact"
    assert layout_mode_for_width(1300, config.ui_layout) == "extra_compact"


if __name__ == "__main__":
    main()
