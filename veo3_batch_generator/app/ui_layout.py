from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DEFAULT_UI_LAYOUT: dict[str, Any] = {
    "layout_version": "ui_v2_responsive_console",
    "compact_breakpoint": 1700,
    "extra_compact_breakpoint": 1450,
    "nav_width_large": 72,
    "nav_width_compact": 64,
    "batch_list_width_large": 300,
    "batch_list_width_compact": 260,
    "detail_panel_width": 380,
    "detail_panel_collapsed_in_compact": True,
    "log_panel_height": 160,
    "log_panel_default_collapsed": True,
    "log_panel_collapsed_in_compact": True,
    "task_table_row_height": 36,
    "log_max_visible_lines": 100,
}

DEFAULT_UI_THEME: dict[str, str] = {
    "main_bg": "#0F1218",
    "panel_bg": "#1B1F27",
    "card_bg": "#232832",
    "border": "#343A46",
    "primary": "#168BFF",
    "success": "#20C997",
    "warning": "#FFB020",
    "danger": "#FF5C5C",
    "text_primary": "#F2F5FA",
    "text_secondary": "#9AA4B2",
}

DEFAULT_TASK_TABLE_COLUMNS: dict[str, list[str]] = {
    "compact_visible": [
        "任务名称",
        "任务状态",
        "流程进度",
        "阶段1",
        "阶段2",
        "错误信息",
    ],
    "large_visible": [
        "任务名称",
        "任务状态",
        "流程进度",
        "负责人",
        "阶段1",
        "阶段2",
        "阶段3",
        "阶段4",
        "生成图片路径",
        "视频链接",
        "视频本地路径",
        "错误信息",
    ],
    "hidden_by_default": [
        "PID",
        "网盘路径",
        "Http路径",
        "产品白底图路径",
        "产品白底图URL",
        "图片提示词",
        "视频提示词",
        "图生图平台",
        "图生图模型",
        "图生视频平台",
        "图生视频模型",
        "video_task_id",
        "批次ID",
    ],
}

DEFAULT_LOG_PANEL: dict[str, Any] = {
    "auto_scroll": True,
    "visible_levels": ["INFO", "WARN", "WARNING", "ERROR"],
    "max_visible_lines": 100,
}

BATCH_STATUS_TEXT_MAP: dict[str, str] = {
    "RUNNING": "运行中",
    "STOPPED": "已停止",
    "COMPLETED": "已完成",
    "FAILED": "失败",
    "PARTIAL_FAILED": "部分完成",
    "PARTIAL_COMPLETED": "部分完成",
    "CREATED": "等待中",
    "PENDING": "等待中",
    "POLLING": "轮询中",
    "PAUSED": "暂停中",
    "PAUSED_BY_ERROR": "异常暂停",
}

TASK_STATUS_TEXT_MAP: dict[str, str] = {
    "PENDING": "等待提交",
    "CHECKING_PRODUCT_IMAGE": "检查产品图中",
    "GENERATING_IMAGE": "图片生成中",
    "IMAGE_DONE": "图片已生成",
    "VIDEO_SUBMITTING": "视频任务提交中",
    "VIDEO_SUBMITTED": "视频已提交",
    "VIDEO_POLLING": "等待视频结果",
    "GENERATING_VIDEO": "视频生成中",
    "VIDEO_DONE": "视频已生成",
    "VIDEO_DOWNLOAD_PENDING": "视频已生成，等待下载",
    "VIDEO_DOWNLOADING": "视频下载中",
    "VIDEO_DOWNLOADED": "已归档完成",
    "COMPLETED": "已完成",
    "WORKFLOW_RUNNING": "流程执行中",
    "WORKFLOW_WAITING_INPUT": "等待补充输入",
    "AUTO_RETRYING": "自动重试中",
    "FAILED_RETRY_EXHAUSTED": "重试后仍失败",
    "FAILED_IMAGE_API": "图生图失败",
    "FAILED_VIDEO_API": "图生视频失败",
    "FAILED_UNKNOWN": "失败",
    "VIDEO_FAILED": "视频生成失败",
    "VIDEO_TIMEOUT": "视频轮询超时",
}


@dataclass
class UILayoutState:
    layout_version: str = "ui_v2_responsive_console"
    is_compact_mode: bool = False
    nav_width: int = 72
    batch_list_width: int = 300
    detail_panel_width: int = 380
    detail_panel_collapsed: bool = False
    log_panel_height: int = 160
    log_panel_collapsed: bool = False
    selected_main_nav: str = "console"
    selected_batch_id: str | None = None
    last_window_width: int | None = None
    last_window_height: int | None = None


@dataclass
class BatchCardViewModel:
    batch_id: str
    batch_name: str
    status_text: str
    status_color_key: str
    completed_text: str
    failed_text: str
    success_rate_text: str
    progress_percent: float
    imported_time_text: str
    is_selected: bool = False
    is_running: bool = False


@dataclass
class TaskTableColumnConfig:
    column_key: str
    title: str
    visible_in_large: bool = True
    visible_in_compact: bool = True
    width: int | None = None
    min_width: int | None = None
    stretch: bool = False
    order: int = 0


@dataclass
class LogPanelState:
    collapsed: bool = False
    auto_scroll: bool = True
    visible_levels: list[str] = field(default_factory=lambda: ["INFO", "WARN", "WARNING", "ERROR"])
    max_visible_lines: int = 100


def merge_defaults(value: Any, defaults: dict[str, Any]) -> dict[str, Any]:
    result = dict(defaults)
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(result.get(key), dict) and isinstance(item, dict):
                result[key] = merge_defaults(item, result[key])
            else:
                result[key] = item
    return result


def ensure_ui_config(config: Any) -> None:
    config.ui_layout = merge_defaults(getattr(config, "ui_layout", None), DEFAULT_UI_LAYOUT)
    config.ui_theme = merge_defaults(getattr(config, "ui_theme", None), DEFAULT_UI_THEME)
    config.task_table_columns = merge_defaults(getattr(config, "task_table_columns", None), DEFAULT_TASK_TABLE_COLUMNS)
    config.log_panel = merge_defaults(getattr(config, "log_panel", None), DEFAULT_LOG_PANEL)


def batch_status_text(status: str) -> str:
    text = str(status or "").strip()
    return BATCH_STATUS_TEXT_MAP.get(text, text or "等待中")


def task_status_text(status: str) -> str:
    text = str(status or "").strip()
    return TASK_STATUS_TEXT_MAP.get(text, text or "等待提交")


def layout_mode_for_width(width: int, ui_layout: dict[str, Any] | None = None) -> str:
    layout = merge_defaults(ui_layout or {}, DEFAULT_UI_LAYOUT)
    if int(width or 0) < int(layout.get("extra_compact_breakpoint", 1450)):
        return "extra_compact"
    if int(width or 0) < int(layout.get("compact_breakpoint", 1700)):
        return "compact"
    return "large"
