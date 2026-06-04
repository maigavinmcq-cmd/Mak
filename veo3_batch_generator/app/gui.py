from __future__ import annotations

import os
import re
import sys
import html
import json
import copy
import shutil
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QBrush, QDesktopServices, QFont, QIcon, QPainter, QPen, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget

    HAS_MEDIA_PLAYER = True
except Exception:
    QAudioOutput = None
    QMediaPlayer = None
    QVideoWidget = None
    HAS_MEDIA_PLAYER = False

from app.api.provider_registry import (
    IMAGE_PROVIDERS,
    VIDEO_PROVIDERS,
    get_image_provider,
    get_video_provider,
    model_display_items,
    model_display_name,
    provider_display_items,
)
from app.batch_manager import BatchManager
from app.config import (
    AppConfig,
    PROJECT_ROOT,
    apply_api_profile_to_config,
    ensure_runtime_dirs,
    exports_dir,
    get_api_profile,
    load_config,
    mask_api_key,
    new_batch_id,
    normalize_netdisk_http_prefix,
    reconcile_api_key_with_provider_profile,
    refresh_runtime_paths,
    runtime_events_dir,
    save_config,
    today_text,
    update_api_profile,
)
from app.excel_loader import load_tasks_from_excel
from app.file_utils import (
    download_video_to_path,
    convert_netdisk_path_to_http,
    find_product_image_for_task,
    open_path,
    path_to_http_url,
    save_image_task_assets,
    safe_pid,
    selected_image_assets_dir,
    selected_video_output_path,
    sync_task_urls_from_paths,
    task_metadata,
    validate_downloaded_file,
    video_download_output_path,
)
from app.logger import setup_logger
from app.models.task import TaskItem, TaskStatus, now_text
from app.models.batch import TaskBatch
from app.runtime.worker_events import append_event, read_events
from app.runtime_provider_switch import apply_runtime_provider_switch
from app.stats_utils import apply_stats_to_batch_snapshot, stabilize_live_completion_stats
from app.task_patch import merge_task_patch
from app.task_manager import TaskManager
from app.task_logs import append_task_log, export_task_logs, task_error_log_summary, task_logs_to_text
from app.ui_combo import bounded_popup_width
from app.ui_diagnostics import UIDiagnostics
from app.ui_layout import batch_status_text, layout_mode_for_width, task_status_text
from app.worker import BatchWorker, ManualPollWorker
from app.workflow import WORKFLOW_VERSION_V2_DEFAULT, workflow_nodes
from app.workflow_executor import WorkflowExecutor


@dataclass
class BatchRunRequest:
    batch_id: str
    failed_only: bool = False
    poll_only: bool = False
    execution_mode: str = "full"
    selected_task_keys: tuple[str, ...] = ()
    requested_at: str = ""
    source: str = ""


STATUS_COLORS = {
    "COMPLETED": QColor("#1f7a3f"),
    "VIDEO_DOWNLOADED": QColor("#238a4d"),
    "PENDING": QColor("#2c2f38"),
    "GENERATING_IMAGE": QColor("#8a6500"),
    "GENERATING_VIDEO": QColor("#8a6500"),
    "VIDEO_SUBMITTING": QColor("#8a6500"),
    "VIDEO_SUBMITTED": QColor("#1d4ed8"),
    "VIDEO_POLLING": QColor("#007aff"),
    "VIDEO_DOWNLOAD_PENDING": QColor("#8a6500"),
    "VIDEO_DOWNLOADING": QColor("#8a6500"),
    "VIDEO_TIMEOUT": QColor("#8b2f2f"),
    "VIDEO_FAILED": QColor("#8b1e2d"),
    "WORKFLOW_RUNNING": QColor("#245aa8"),
    "WORKFLOW_WAITING_INPUT": QColor("#6b6f7a"),
    "AUTO_RETRYING": QColor("#8a6500"),
    "FAILED_RETRY_EXHAUSTED": QColor("#8b1e2d"),
    "CHECKING_PRODUCT_IMAGE": QColor("#245aa8"),
    "IMAGE_DONE": QColor("#007aff"),
    "FAILED_IMAGE_API": QColor("#8b1e2d"),
    "FAILED_VIDEO_API": QColor("#8b1e2d"),
    "FAILED_UNKNOWN": QColor("#8b1e2d"),
    "SKIPPED_NO_PRODUCT_IMAGE_FOLDER": QColor("#4a4a4d"),
    "SKIPPED_NO_PRODUCT_IMAGE": QColor("#4a4a4d"),
    "SKIPPED_NO_PRODUCT_IMAGE_URL": QColor("#4a4a4d"),
    "SKIPPED_EMPTY_PROMPT": QColor("#4a4a4d"),
}

# --- v2 workflow UI helpers ---------------------------------------------------
# Ordered list of (node_id, display_label, prompt_attr) per the default 4-stage
# workflow. Drives the "阶段X" columns, the mini flow progress text and the
# right-click submenu.
STAGE_NODES: list[tuple[str, str, str, str]] = [
    ("image_stage_1", "阶段1", "产品图生图1", "prompt_stage_1"),
    ("video_stage_1", "阶段2", "图1生视频1", "prompt_stage_2"),
    ("image_stage_2", "阶段3", "图1+产品图生图2", "prompt_stage_3"),
    ("video_stage_2", "阶段4", "图2生视频2", "prompt_stage_4"),
]
DEFAULT_STAGE_NODE_BY_ID = {node_id: (short_label, node_name, prompt_attr) for node_id, short_label, node_name, prompt_attr in STAGE_NODES}

# Per node-status icon + colour for the 阶段 columns.
NODE_STATUS_ICON: dict[str, str] = {
    "COMPLETED": "✅",
    "RUNNING": "🔄",
    "SUBMITTED": "📤",
    "POLLING": "⏳",
    "FAILED": "⚠",
    "SKIPPED": "—",
    "BLOCKED": "⛔",
    "WAITING_INPUT": "⏸",
    "READY": "▶",
    "PENDING": "●",
    "": "●",
}

NODE_STATUS_COLOR: dict[str, QColor] = {
    "COMPLETED": QColor("#1f7a3f"),
    "RUNNING": QColor("#8a6500"),
    "SUBMITTED": QColor("#1d4ed8"),
    "POLLING": QColor("#007aff"),
    "FAILED": QColor("#8b1e2d"),
    "SKIPPED": QColor("#4a4a4d"),
    "BLOCKED": QColor("#8b2f2f"),
    "WAITING_INPUT": QColor("#6b6f7a"),
    "READY": QColor("#245aa8"),
    "PENDING": QColor("#2c2f38"),
}

FLOW_STAGE_LABELS: dict[str, str] = {
    "image_stage_1": "图1",
    "video_stage_1": "视频1",
    "image_stage_2": "图2",
    "video_stage_2": "视频2",
}

FLOW_STATUS_LABELS: dict[str, str] = {
    "COMPLETED": "已生成",
    "RUNNING": "生成中",
    "SUBMITTED": "已提交",
    "POLLING": "轮询中",
    "FAILED": "失败",
    "SKIPPED": "跳过",
    "BLOCKED": "阻塞",
    "WAITING_INPUT": "缺输入",
    "READY": "可执行",
    "PENDING": "待执行",
    "": "待执行",
}

# Human-friendly Chinese rendering of the cryptic SKIPPED_X / FAILED_X codes.
FRIENDLY_STATUS_TEXT: dict[str, str] = {
    "PENDING": "等待执行",
    "CHECKING_PRODUCT_IMAGE": "检查产品图中",
    "GENERATING_IMAGE": "图生图进行中",
    "IMAGE_DONE": "图生图已完成",
    "VIDEO_SUBMITTING": "视频任务提交中",
    "VIDEO_SUBMITTED": "视频任务已提交",
    "VIDEO_POLLING": "视频生成轮询中",
    "GENERATING_VIDEO": "视频生成中",
    "VIDEO_DONE": "视频已完成",
    "VIDEO_FAILED": "视频生成失败",
    "VIDEO_TIMEOUT": "视频轮询超时",
    "VIDEO_DOWNLOAD_PENDING": "视频已生成，等待下载",
    "VIDEO_DOWNLOADING": "视频下载中",
    "COMPLETED": "阶段已完成",
    "VIDEO_DOWNLOADED": "已归档完成",
    "WORKFLOW_RUNNING": "流程执行中",
    "WORKFLOW_WAITING_INPUT": "等待补充输入",
    "AUTO_RETRYING": "自动重试中",
    "FAILED_RETRY_EXHAUSTED": "重试后仍失败",
    "FAILED_IMAGE_API": "图生图接口失败",
    "FAILED_VIDEO_API": "图生视频接口失败",
    "FAILED_UNKNOWN": "发生未预期的错误",
    "SKIPPED_NO_PRODUCT_IMAGE_FOLDER": "网盘路径不可用或为空",
    "SKIPPED_NO_PRODUCT_IMAGE": "01.产品白底图 目录下没有图片",
    "SKIPPED_NO_PRODUCT_IMAGE_URL": "缺少产品白底图来源",
    "SKIPPED_EMPTY_PROMPT": "提示词为空",
}

# Per-status next-step suggestion shown in tooltips + the detail panel.
FRIENDLY_STATUS_HINT: dict[str, str] = {
    "FAILED_IMAGE_API": "可以稍后重试该任务，或检查图生图 API Key / Base URL",
    "FAILED_VIDEO_API": "可以稍后重试，或先确认视频提示词与首帧图是否合规",
    "FAILED_UNKNOWN": "可以查看日志了解详细原因，然后重试",
    "VIDEO_TIMEOUT": "可以点击「手动轮询」继续追踪，或重试该任务",
    "VIDEO_DOWNLOAD_PENDING": "视频已经生成，系统会继续下载并归档到本地/网盘",
    "VIDEO_DOWNLOADING": "正在下载视频文件，请等待归档完成",
    "WORKFLOW_WAITING_INPUT": "请检查产品图、提示词或前置阶段输出是否齐全",
    "AUTO_RETRYING": "系统正在自动清理失败状态并重新入队",
    "FAILED_RETRY_EXHAUSTED": "已达到自动重试次数，可检查提示词/API返回后手动重试",
    "SKIPPED_NO_PRODUCT_IMAGE_FOLDER": "请检查 Excel 中的网盘路径是否存在并可访问",
    "SKIPPED_NO_PRODUCT_IMAGE": "请在网盘 01.产品白底图 目录下放入产品白底图",
    "SKIPPED_NO_PRODUCT_IMAGE_URL": "可在 Excel 表格中补充「产品白底图URL」字段",
    "SKIPPED_EMPTY_PROMPT": "请补全 Excel 中对应任务的提示词字段",
}


def stage_status_icon(status: str) -> str:
    return NODE_STATUS_ICON.get(str(status or ""), "●")


def stage_status_color(status: str) -> QColor:
    return NODE_STATUS_COLOR.get(str(status or ""), QColor("#2c2f38"))


def task_node_state(task, node_id: str) -> dict:
    states = getattr(task, "node_states", None)
    if not isinstance(states, dict):
        return {}
    state = states.get(node_id)
    return state if isinstance(state, dict) else {}


def workflow_stage_status_label(task, node_id: str) -> str:
    state = task_node_state(task, node_id)
    status = str(state.get("status") or "PENDING")
    is_video = node_id.startswith("video_")
    if status == "COMPLETED":
        if is_video:
            if state.get("output_video_local_path"):
                return "视频已归档"
            if state.get("output_video_url"):
                return "视频待下载"
            return "视频已生成"
        return "图片已生成"
    if status == "FAILED" and state.get("auto_retry_count"):
        return "失败待重试"
    return FLOW_STATUS_LABELS.get(status, friendly_status_text(status) or status or "待执行")


def stage_nodes_from_workflow_definition(workflow_definition) -> list[tuple[str, str, str, str]]:
    """Return UI stage columns for the active batch workflow.

    Older UI code assumed the default 4-stage workflow. Recovery batches and
    other custom workflows can have a different node graph, so the table must
    render the batch snapshot instead of hard-coded node ids.
    """
    if not isinstance(workflow_definition, dict) or not workflow_definition.get("nodes"):
        return list(STAGE_NODES)
    is_default_workflow = str(workflow_definition.get("workflow_version") or "") == WORKFLOW_VERSION_V2_DEFAULT
    stage_nodes: list[tuple[str, str, str, str]] = []
    for idx, node in enumerate(workflow_nodes(workflow_definition)):
        if not bool(node.get("enabled", True)):
            continue
        node_id = str(node.get("node_id") or "").strip()
        if not node_id:
            continue
        default = DEFAULT_STAGE_NODE_BY_ID.get(node_id)
        short_label = f"阶段{len(stage_nodes) + 1}"
        if default and is_default_workflow:
            node_name = default[1]
        else:
            node_name = str(node.get("node_name") or (default[1] if default else node_id))
        prompt_attr = default[2] if default else f"prompt_stage_{idx + 1}"
        stage_nodes.append((node_id, short_label, node_name, prompt_attr))
    return stage_nodes or list(STAGE_NODES)


def _uses_default_stage_labels(stage_nodes: list[tuple[str, str, str, str]] | None) -> bool:
    if not stage_nodes:
        return True
    return [node_id for node_id, *_ in stage_nodes] == [node_id for node_id, *_ in STAGE_NODES]


def _flow_stage_label(node_id: str, idx: int, node_name: str, stage_nodes: list[tuple[str, str, str, str]] | None) -> str:
    if _uses_default_stage_labels(stage_nodes):
        return FLOW_STAGE_LABELS.get(node_id, f"阶段{idx + 1}")
    return node_name or FLOW_STAGE_LABELS.get(node_id, f"阶段{idx + 1}")


def workflow_stage_cells(
    task,
    stage_nodes: list[tuple[str, str, str, str]] | None = None,
    max_columns: int = 4,
) -> list[tuple[str, str, str, str, str, str]]:
    """Build padded table cells for the 阶段 columns.

    Each tuple is ``(display_text, status, node_id, short_label, node_name,
    prompt_attr)``. Empty tuples are returned for unused stage columns so custom
    one-node recovery workflows do not show stale default stages.
    """
    nodes = list(stage_nodes or STAGE_NODES)
    cells: list[tuple[str, str, str, str, str, str]] = []
    for node_id, short_label, node_name, prompt_attr in nodes[:max_columns]:
        state = task_node_state(task, node_id)
        status = str(state.get("status") or "")
        icon = stage_status_icon(status)
        label = workflow_stage_status_label(task, node_id) if status else "待执行"
        display = f"{icon} {label}" if label else icon
        cells.append((display, status, node_id, short_label, node_name, prompt_attr))
    while len(cells) < max_columns:
        cells.append(("", "", "", "", "", ""))
    return cells


def task_flow_summary(task, stage_nodes: list[tuple[str, str, str, str]] | None = None) -> str:
    """Readable mini flow progress for the 流程进度 column."""
    parts: list[str] = []
    nodes = list(stage_nodes or STAGE_NODES)
    for idx, (node_id, _short, node_name, _attr) in enumerate(nodes):
        stage_label = _flow_stage_label(node_id, idx, node_name, nodes)
        status_label = workflow_stage_status_label(task, node_id)
        parts.append(f"{stage_label}:{status_label}")
    return " | ".join(parts)


def friendly_status_text(status: str) -> str:
    text = FRIENDLY_STATUS_TEXT.get(str(status or ""), "")
    return text or str(status or "")


def friendly_status_hint(status: str) -> str:
    return FRIENDLY_STATUS_HINT.get(str(status or ""), "")


class LogWorkbenchDialog(QDialog):
    """Large log inspector for batch-level runtime logs."""

    def __init__(self, entries: list[tuple[str, str]], parent=None, open_log_callback=None):
        super().__init__(parent)
        self.entries = [(str(level or "INFO").upper(), str(message or "")) for level, message in entries]
        self.open_log_callback = open_log_callback
        self.filtered_indexes: list[int] = []
        self.setWindowTitle("日志工作台")
        self.resize(1280, 760)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("日志工作台")
        title.setObjectName("sectionTitle")
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("mutedLabel")
        header.addWidget(title)
        header.addWidget(self.summary_label, 1)
        root.addLayout(header)

        aggregate_box = QFrame()
        aggregate_layout = QHBoxLayout(aggregate_box)
        aggregate_layout.setContentsMargins(0, 0, 0, 0)
        aggregate_layout.setSpacing(8)
        aggregate_layout.addWidget(QLabel("错误聚合"))
        for signature, count in self._top_error_signatures():
            button = QPushButton(f"{signature} × {count}")
            button.setObjectName("dangerButton")
            button.clicked.connect(lambda _checked=False, text=signature: self._apply_signature_filter(text))
            aggregate_layout.addWidget(button)
        aggregate_layout.addStretch(1)
        root.addWidget(aggregate_box)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_filter_panel())
        splitter.addWidget(self._build_log_table())
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 2)
        splitter.setSizes([260, 720, 360])
        root.addWidget(splitter, 1)

        footer = QHBoxLayout()
        self.copy_filtered_btn = QPushButton("复制筛选结果")
        self.copy_detail_btn = QPushButton("复制详情")
        self.open_log_btn = QPushButton("打开日志文件")
        close_btn = QPushButton("关闭")
        self.copy_filtered_btn.clicked.connect(self.copy_filtered_logs)
        self.copy_detail_btn.clicked.connect(self.copy_selected_detail)
        self.open_log_btn.clicked.connect(self._open_log_file)
        close_btn.clicked.connect(self.accept)
        footer.addStretch(1)
        for button in [self.copy_filtered_btn, self.copy_detail_btn, self.open_log_btn, close_btn]:
            footer.addWidget(button)
        root.addLayout(footer)
        self.apply_filters()

    def _build_filter_panel(self) -> QWidget:
        panel = QFrame()
        panel.setMinimumWidth(240)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        layout.addWidget(QLabel("级别"))
        self.level_combo = QComboBox()
        self.level_combo.addItems(["全部", "ERROR", "WARN", "INFO"])
        layout.addWidget(self.level_combo)

        layout.addWidget(QLabel("分类"))
        self.category_combo = QComboBox()
        self.category_combo.addItem("全部")
        for category in sorted({self._category_for_message(message) for _level, message in self.entries}):
            self.category_combo.addItem(category)
        layout.addWidget(self.category_combo)

        layout.addWidget(QLabel("PID"))
        self.pid_edit = QLineEdit()
        self.pid_edit.setPlaceholderText("输入 PID")
        layout.addWidget(self.pid_edit)

        layout.addWidget(QLabel("task_id"))
        self.task_id_edit = QLineEdit()
        self.task_id_edit.setPlaceholderText("输入 task_id")
        layout.addWidget(self.task_id_edit)

        layout.addWidget(QLabel("关键词"))
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("错误、阶段、请求 ID")
        layout.addWidget(self.keyword_edit)

        self.hide_diagnostics_check = QCheckBox("隐藏诊断/PERF")
        self.hide_diagnostics_check.setChecked(True)
        layout.addWidget(self.hide_diagnostics_check)

        apply_btn = QPushButton("应用筛选")
        reset_btn = QPushButton("重置")
        apply_btn.clicked.connect(self.apply_filters)
        reset_btn.clicked.connect(self.reset_filters)
        layout.addWidget(apply_btn)
        layout.addWidget(reset_btn)
        layout.addStretch(1)

        for widget in [
            self.level_combo,
            self.category_combo,
            self.pid_edit,
            self.task_id_edit,
            self.keyword_edit,
            self.hide_diagnostics_check,
        ]:
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(lambda *_: self.apply_filters())
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(lambda *_: self.apply_filters())
            elif isinstance(widget, QCheckBox):
                widget.stateChanged.connect(lambda *_: self.apply_filters())
        return panel

    def _build_log_table(self) -> QTableWidget:
        self.log_table = QTableWidget(0, 5)
        self.log_table.setHorizontalHeaderLabels(["序号", "级别", "分类", "PID/行", "摘要"])
        self.log_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.log_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.log_table.verticalHeader().setVisible(False)
        self.log_table.horizontalHeader().setStretchLastSection(True)
        self.log_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.log_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.log_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.log_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.log_table.itemSelectionChanged.connect(self.update_selected_detail)
        return self.log_table

    def _build_detail_panel(self) -> QWidget:
        panel = QFrame()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        title = QLabel("日志详情")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setFont(QFont("Consolas", 10))
        layout.addWidget(self.detail_text, 1)
        return panel

    def _top_error_signatures(self) -> list[tuple[str, int]]:
        counter: Counter[str] = Counter()
        for level, message in self.entries:
            if level in {"ERROR", "WARN", "WARNING"}:
                counter[self._error_signature(message)] += 1
        return counter.most_common(5)

    def _apply_signature_filter(self, signature: str) -> None:
        self.level_combo.setCurrentText("全部")
        self.keyword_edit.setText(signature)
        self.apply_filters()

    def reset_filters(self) -> None:
        self.level_combo.setCurrentText("全部")
        self.category_combo.setCurrentText("全部")
        self.pid_edit.clear()
        self.task_id_edit.clear()
        self.keyword_edit.clear()
        self.hide_diagnostics_check.setChecked(True)
        self.apply_filters()

    def apply_filters(self) -> None:
        wanted_level = self.level_combo.currentText() if hasattr(self, "level_combo") else "全部"
        wanted_category = self.category_combo.currentText() if hasattr(self, "category_combo") else "全部"
        pid = self.pid_edit.text().strip() if hasattr(self, "pid_edit") else ""
        task_id = self.task_id_edit.text().strip() if hasattr(self, "task_id_edit") else ""
        keyword = self.keyword_edit.text().strip().lower() if hasattr(self, "keyword_edit") else ""
        hide_diagnostics = bool(self.hide_diagnostics_check.isChecked()) if hasattr(self, "hide_diagnostics_check") else True

        self.filtered_indexes = []
        for index, (level, message) in enumerate(self.entries):
            category = self._category_for_message(message)
            normalized_level = "WARN" if level == "WARNING" else level
            if wanted_level != "全部" and normalized_level != wanted_level:
                continue
            if wanted_category != "全部" and category != wanted_category:
                continue
            if hide_diagnostics and (category in {"PERF", "DIAGNOSTIC", "STATE_SAVE"} or "[PERF]" in message or "STATE_SAVE" in message):
                continue
            if pid and pid not in message:
                continue
            if task_id and task_id not in message:
                continue
            if keyword and keyword not in f"{level} {message}".lower():
                continue
            self.filtered_indexes.append(index)
        self._render_table()

    def _render_table(self) -> None:
        self.log_table.setRowCount(0)
        for row, entry_index in enumerate(self.filtered_indexes[-2000:]):
            level, message = self.entries[entry_index]
            category = self._category_for_message(message)
            pid_row = self._pid_row_for_message(message)
            summary = self._summary(message, 180)
            self.log_table.insertRow(row)
            values = [str(entry_index + 1), level, category, pid_row, summary]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, entry_index)
                item.setForeground(QBrush(self._level_color(level)))
                self.log_table.setItem(row, col, item)
        self.summary_label.setText(f"显示 {len(self.filtered_indexes)}/{len(self.entries)} 条日志")
        if self.log_table.rowCount():
            self.log_table.selectRow(self.log_table.rowCount() - 1)
        else:
            self.detail_text.setPlainText("没有符合筛选条件的日志。")

    def update_selected_detail(self) -> None:
        row = self.log_table.currentRow()
        item = self.log_table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        entry_index = int(item.data(Qt.UserRole))
        level, message = self.entries[entry_index]
        category = self._category_for_message(message)
        pid_row = self._pid_row_for_message(message)
        task_id = self._task_id_for_message(message)
        lines = [
            f"序号: {entry_index + 1}",
            f"级别: {level}",
            f"分类: {category}",
            f"PID/行: {pid_row or '-'}",
            f"task_id: {task_id or '-'}",
            "",
            message,
        ]
        self.detail_text.setPlainText("\n".join(lines))

    def copy_selected_detail(self) -> None:
        QApplication.clipboard().setText(self.detail_text.toPlainText())

    def copy_filtered_logs(self) -> None:
        lines = [f"[{self.entries[index][0]}] {self.entries[index][1]}" for index in self.filtered_indexes]
        QApplication.clipboard().setText("\n".join(lines))

    def _open_log_file(self) -> None:
        if callable(self.open_log_callback):
            self.open_log_callback()

    @staticmethod
    def _category_for_message(message: str) -> str:
        text = str(message or "")
        bracket = re.match(r"\[([A-Z0-9_\-]+)\]", text)
        if bracket:
            value = bracket.group(1).upper()
            if value in {"PERF", "DOWNLOAD", "IMAGE", "VIDEO", "TASK", "RETRY", "AUTO_RETRY", "MANUAL_POLL"}:
                return value
        node = re.search(r"\[(image_stage_\d+|video_stage_\d+)\]", text)
        if node:
            return node.group(1)
        if "STATE_SAVE" in text:
            return "STATE_SAVE"
        if "PERF" in text:
            return "PERF"
        if "poll" in text.lower() or "轮询" in text:
            return "POLL"
        if "download" in text.lower() or "下载" in text:
            return "DOWNLOAD"
        if "video" in text.lower() or "视频" in text:
            return "VIDEO"
        if "image" in text.lower() or "图片" in text:
            return "IMAGE"
        return "TASK"

    @staticmethod
    def _pid_row_for_message(message: str) -> str:
        pid = re.search(r"PID=([^\s,]+)", message)
        row = re.search(r"row=([0-9]+)", message)
        if pid and row:
            return f"{pid.group(1)} / row {row.group(1)}"
        if pid:
            return pid.group(1)
        if row:
            return f"row {row.group(1)}"
        return ""

    @staticmethod
    def _task_id_for_message(message: str) -> str:
        match = re.search(r"task_[A-Za-z0-9_\-]+", message)
        return match.group(0) if match else ""

    @staticmethod
    def _error_signature(message: str) -> str:
        text = str(message or "")
        known = [
            "Invalid token",
            "task_not_exist",
            "图片下载失败",
            "默认分组暂无可用源站",
            "insufficient_user_quota",
            "fail_to_fetch_task",
            "Failed to establish a new connection",
        ]
        for item in known:
            if item in text:
                return item
        text = re.sub(r"request id[:： ]+[A-Za-z0-9_\-]+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"PID=[^\s,]+", "PID=*", text)
        text = re.sub(r"row=\d+", "row=*", text)
        text = re.sub(r"task_[A-Za-z0-9_\-]+", "task_*", text)
        text = re.sub(r"https?://\S+", "URL", text)
        return LogWorkbenchDialog._summary(text, 64)

    @staticmethod
    def _summary(value: str, limit: int) -> str:
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"

    @staticmethod
    def _level_color(level: str) -> QColor:
        level = str(level or "").upper()
        if level == "ERROR":
            return QColor("#ff9b9b")
        if level in {"WARN", "WARNING"}:
            return QColor("#ffd27a")
        return QColor("#dfe7f7")


def task_overall_status_display(
    task,
    stage_nodes: list[tuple[str, str, str, str]] | None = None,
) -> str:
    """Render the overall task state without hiding useful workflow detail.

    The persisted task.status may stay WORKFLOW_RUNNING while node-level state
    has already moved to "waiting for video result", "ready to submit", or
    "waiting input". Showing that derived state prevents the table from making
    hundreds of async tasks look like they are stuck in the same phase.
    """
    raw_status = str(getattr(task, "status", "") or "")
    fallback = task_status_text(raw_status) or friendly_status_text(raw_status) or raw_status
    if raw_status != TaskStatus.WORKFLOW_RUNNING:
        return fallback

    nodes = list(stage_nodes or STAGE_NODES)
    states: list[tuple[str, str]] = []
    for node_id, _short, _name, _attr in nodes:
        state = task_node_state(task, node_id)
        node_status = str(state.get("status") or "")
        if node_status:
            states.append((node_id, node_status))

    if any(node_id.startswith("video_") and status in {"RUNNING", "SUBMITTED", "POLLING"} for node_id, status in states):
        return "等待视频结果"
    if any(node_id.startswith("image_") and status in {"RUNNING", "SUBMITTED", "POLLING"} for node_id, status in states):
        return "等待图片结果"
    if any(node_id.startswith("video_") and status == "READY" for node_id, status in states):
        return "等待视频提交"
    if any(node_id.startswith("image_") and status == "READY" for node_id, status in states):
        return "等待图片提交"
    if any(status == "WAITING_INPUT" for _node_id, status in states):
        return "等待补充输入"
    if any(status == "BLOCKED" for _node_id, status in states):
        return "等待前置阶段"
    if any(status == "PENDING" for _node_id, status in states):
        return "等待执行"
    if states and all(status == "COMPLETED" for _node_id, status in states):
        return "等待归档"
    return fallback or "流程执行中"


class Toast(QFrame):
    """Lightweight self-painted notification balloon.

    Stacks in the bottom-right of the parent main window. Auto-dismisses after
    ``duration_ms``. Closeable via the × button.
    """

    KIND_INFO = "info"
    KIND_SUCCESS = "success"
    KIND_WARNING = "warning"
    KIND_ERROR = "error"

    _PALETTE = {
        KIND_INFO: ("#1d4ed8", "#0f3aa1", "ⓘ"),
        KIND_SUCCESS: ("#1f7a3f", "#155f2e", "✓"),
        KIND_WARNING: ("#a86a00", "#7a4d00", "⚠"),
        KIND_ERROR: ("#8b1e2d", "#6b1620", "✕"),
    }

    def __init__(self, parent: QWidget, message: str, kind: str = KIND_INFO, duration_ms: int = 3500) -> None:
        super().__init__(parent)
        self.setObjectName("toastFrame")
        bg, border, icon = self._PALETTE.get(kind, self._PALETTE[self.KIND_INFO])
        self.setStyleSheet(
            f"""
            QFrame#toastFrame {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
            QFrame#toastFrame QLabel {{
                color: white;
                background: transparent;
            }}
            QFrame#toastFrame QPushButton {{
                color: rgba(255, 255, 255, 200);
                background: transparent;
                border: none;
                padding: 0;
                font-size: 16px;
            }}
            QFrame#toastFrame QPushButton:hover {{
                color: white;
            }}
            """
        )
        self.setMinimumWidth(280)
        self.setMaximumWidth(440)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 10, 10)
        layout.setSpacing(10)
        icon_label = QLabel(icon)
        icon_label.setStyleSheet("color: white; font-size: 16px; font-weight: bold;")
        icon_label.setFixedWidth(20)
        icon_label.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        layout.addWidget(icon_label, 0)
        text_label = QLabel(message)
        text_label.setWordWrap(True)
        text_label.setStyleSheet("color: white; font-size: 13px;")
        text_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(text_label, 1)
        close_btn = QPushButton("×")
        close_btn.setFixedSize(18, 18)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self._dismiss)
        layout.addWidget(close_btn, 0, Qt.AlignTop)
        self.adjustSize()
        self._duration_ms = max(800, int(duration_ms))
        self._dismissed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._dismiss)
        self._timer.start(self._duration_ms)

    def enterEvent(self, event) -> None:
        # Pause auto-dismiss while the user is hovering.
        if self._timer.isActive():
            self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._dismissed:
            self._timer.start(self._duration_ms)
        super().leaveEvent(event)

    def _dismiss(self) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        self.hide()
        parent = self.parent()
        if isinstance(parent, MainWindow):
            parent._toast_dismissed(self)
        self.deleteLater()


class CircularLoadingIndicator(QWidget):
    """Small indeterminate circular spinner used by the loading overlay."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(48, 48)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def stop(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._angle = (self._angle + 12) % 360
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(5, 5, -5, -5)

        base_pen = QPen(QColor(255, 255, 255, 42), 4)
        base_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(base_pen)
        painter.drawArc(rect, 0, 360 * 16)

        arc_pen = QPen(QColor("#007aff"), 4)
        arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arc_pen)
        painter.drawArc(rect, -self._angle * 16, -96 * 16)


class LoadingOverlay(QFrame):
    """Soft blocking overlay for long UI operations in the main workspace."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("loadingOverlay")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.WaitCursor)
        self.setStyleSheet(
            """
            QFrame#loadingOverlay {
                background-color: rgba(9, 12, 18, 170);
                border-radius: 18px;
            }
            QFrame#loadingCard {
                background-color: rgba(31, 36, 48, 238);
                border: 1px solid rgba(255, 255, 255, 0.16);
                border-radius: 16px;
            }
            QLabel#loadingTitle {
                color: #f6f8ff;
                font-size: 17px;
                font-weight: 700;
            }
            QLabel#loadingDetail {
                color: #b8c4d8;
                font-size: 12px;
            }
            """
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)

        card = QFrame(self)
        card.setObjectName("loadingCard")
        card.setMinimumWidth(360)
        card.setMaximumWidth(560)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 20, 24, 20)
        card_layout.setSpacing(10)

        self.title_label = QLabel("正在处理...")
        self.title_label.setObjectName("loadingTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.detail_label = QLabel("数据较多时可能需要几秒，请稍等。")
        self.detail_label.setObjectName("loadingDetail")
        self.detail_label.setWordWrap(True)
        self.detail_label.setAlignment(Qt.AlignCenter)
        self.spinner = CircularLoadingIndicator()

        card_layout.addWidget(self.title_label)
        card_layout.addWidget(self.spinner, 0, Qt.AlignCenter)
        card_layout.addWidget(self.detail_label)
        outer.addWidget(card, 0, Qt.AlignCenter)
        outer.addStretch(1)
        self.hide()

    def show_operation(self, title: str, detail: str) -> None:
        self.title_label.setText(title)
        self.detail_label.setText(detail)
        self.setGeometry(self.parentWidget().rect())
        self.spinner.start()
        self.spinner.repaint()
        self.raise_()
        self.show()

    def hide(self) -> None:
        self.spinner.stop()
        super().hide()


TABLE_HEADERS = [
    "任务名称",
    "PID",
    "负责人",
    "流程进度",
    "阶段1",
    "阶段2",
    "阶段3",
    "阶段4",
    "网盘路径",
    "Http路径",
    "产品白底图路径",
    "产品白底图URL",
    "指定白底图文件名",
    "图片提示词",
    "视频提示词",
    "图生图平台",
    "图生图模型",
    "图生图状态",
    "生成图片路径",
    "生成图片URL",
    "图生视频平台",
    "图生视频模型",
    "video_task_id",
    "视频轮询次数",
    "手动轮询次数",
    "最后手动轮询时间",
    "最后手动轮询结果",
    "视频状态",
    "视频链接",
    "视频本地路径",
    "视频下载状态",
    "视频下载尝试次数",
    "最后视频下载错误",
    "任务状态",
    "错误信息",
    "任务添加日期",
    "批次ID",
    "开始时间",
    "结束时间",
    "耗时秒数",
]

COL_INDEX = {name: index for index, name in enumerate(TABLE_HEADERS)}
FILTER_FIELDS = [
    "任务名称",
    "PID",
    "负责人",
    "任务状态",
    "图生图状态",
    "视频状态",
    "图生图平台",
    "图生图模型",
    "图生视频平台",
    "图生视频模型",
    "批次ID",
    "任务添加日期",
    "是否有视频链接",
    "是否有错误信息",
]
GROUP_FIELDS = ["PID", "负责人", "任务状态", "图生图平台", "图生图模型", "图生视频平台", "图生视频模型", "批次ID", "任务添加日期"]
DEFAULT_VISIBLE_COLUMNS = [
    "任务名称",
    "PID",
    "负责人",
    "流程进度",
    "阶段1",
    "阶段2",
    "阶段3",
    "阶段4",
    "网盘路径",
    "产品白底图URL",
    "指定白底图文件名",
    "图片提示词",
    "视频提示词",
    "生成图片路径",
    "video_task_id",
    "视频轮询次数",
    "视频链接",
    "视频本地路径",
    "任务状态",
    "错误信息",
]

BATCH_HEADERS = [
    "批次名称",
    "批次ID",
    "导入时间",
    "任务总数",
    "已完成",
    "失败",
    "跳过",
    "轮询中",
    "完成百分比",
    "成功率",
    "批次状态",
    "图生图",
    "图生视频",
    "来源Excel",
    "备注",
]

BATCH_COL_INDEX = {name: index for index, name in enumerate(BATCH_HEADERS)}
STALE_RUNTIME_BATCH_STATUSES = {"RUNNING", "PAUSED", "POLLING", "PAUSED_BY_ERROR"}


class BatchCardWidget(QFrame):
    STATUS_COLORS = {
        "RUNNING": "#20c997",
        "PAUSED": "#f5c542",
        "FAILED": "#ff5c7a",
        "PARTIAL_FAILED": "#ff5c7a",
        "COMPLETED": "#33d17a",
        "CREATED": "#8a93a5",
        "STOPPED": "#ff9f43",
    }

    def __init__(self, batch: TaskBatch, selected: bool = False, running: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("batchCard")
        self.setMinimumHeight(78)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QGridLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(4)

        self.dot_label = QLabel("●")
        self.dot_label.setFixedWidth(16)
        self.title_label = QLabel()
        self.title_label.setObjectName("batchCardTitle")
        self.title_label.setMinimumWidth(0)
        self.status_label = QLabel()
        self.status_label.setObjectName("batchStatusTag")
        self.time_label = QLabel()
        self.time_label.setObjectName("mutedLabel")
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("batchCardSummary")
        self.summary_label.setWordWrap(False)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(7)
        self.rate_label = QLabel()
        self.rate_label.setObjectName("mutedLabel")
        self.rate_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.detail_label = QLabel()
        self.detail_label.setObjectName("mutedLabel")
        self.detail_label.setWordWrap(False)

        layout.addWidget(self.dot_label, 0, 0, 2, 1)
        layout.addWidget(self.title_label, 0, 1)
        layout.addWidget(self.status_label, 0, 2)
        layout.addWidget(self.time_label, 0, 3)
        layout.addWidget(self.summary_label, 1, 1, 1, 3)
        layout.addWidget(self.progress, 2, 1, 1, 2)
        layout.addWidget(self.rate_label, 2, 3)
        layout.addWidget(self.detail_label, 3, 1, 1, 3)
        layout.setColumnStretch(1, 1)
        self.update_batch(batch, selected, running)

    @staticmethod
    def _short_time(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return text[5:16] if len(text) >= 16 else text

    def update_batch(self, batch: TaskBatch, selected: bool = False, running: bool = False) -> None:
        status = batch.status or "CREATED"
        color = self.STATUS_COLORS.get(status, "#8a93a5")
        self.dot_label.setStyleSheet(f"color: {color}; font-size: 16px;")
        self.title_label.setText(batch.batch_name or batch.batch_id)
        self.title_label.setToolTip(f"{batch.batch_name or batch.batch_id}\n{batch.batch_id}")
        self.status_label.setText("执行中" if running else batch_status_text(status))
        self.time_label.setText(self._short_time(batch.imported_at))
        self.summary_label.setText(
            f"完成 {batch.completed_count}/{batch.task_count}｜失败 {batch.failed_count}｜成功率 {batch.success_rate:.1f}%"
        )
        self.progress.setValue(max(0, min(100, int(round(batch.progress_percent or 0)))))
        self.rate_label.setText(self._short_time(batch.imported_at))
        model_text = " / ".join(part for part in [batch.video_model_display_name, batch.remark or ""] if part)
        self.detail_label.setText(model_text or f"进度 {batch.progress_percent:.1f}%")
        self.detail_label.setToolTip(model_text or batch.source_excel_path or batch.batch_id)
        border = "#007aff" if selected else "rgba(255,255,255,0.08)"
        bg = "rgba(0,122,255,0.22)" if selected else ("rgba(32,201,151,0.14)" if running else "rgba(255,255,255,0.045)")
        self.setStyleSheet(
            f"""
            QFrame#batchCard {{
                background: {bg};
                border: 1px solid {border};
                border-left: 4px solid {color if selected or running else 'rgba(255,255,255,0.08)'};
                border-radius: 12px;
            }}
            QLabel#batchCardTitle {{
                color: #f6f8ff;
                font-weight: 700;
            }}
            QLabel#batchCardSummary {{
                color: #c9d4e7;
            }}
            QLabel#batchStatusTag {{
                color: #ffffff;
                background: {color};
                border-radius: 8px;
                padding: 2px 7px;
                font-size: 11px;
                font-weight: 700;
            }}
            QProgressBar {{
                background: rgba(255,255,255,0.10);
                border: none;
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background: #007aff;
                border-radius: 3px;
            }}
            """
        )


class ExcelImportWorker(QThread):
    loaded = Signal(object, object)
    failed = Signal(str, object)

    def __init__(self, source_excel_path: Path, dialog_result: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.source_excel_path = Path(source_excel_path)
        self.dialog_result = dict(dialog_result)

    def run(self) -> None:
        try:
            tasks = load_tasks_from_excel(self.source_excel_path)
            self.loaded.emit(tasks, self.dialog_result)
        except Exception as exc:
            self.failed.emit(str(exc), self.dialog_result)


class BatchLoadWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        batch_manager: BatchManager,
        batch_id: str,
        config: AppConfig,
        active_running_batch_id: str = "",
        running_manager: TaskManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.batch_manager = batch_manager
        self.batch_id = str(batch_id or "")
        self.config_snapshot = copy.deepcopy(config)
        self.active_running_batch_id = str(active_running_batch_id or "")
        self.running_manager = running_manager

    def run(self) -> None:
        try:
            batch = self.batch_manager.load_batch(self.batch_id)
            if not batch:
                self.loaded.emit({"batch": None, "manager": None, "log_text": "", "warnings": []})
                return
            manager = (
                self.running_manager
                if self.active_running_batch_id == batch.batch_id and self.running_manager is not None
                else self._load_manager_for_batch(batch)
            )
            log_text = self._read_batch_log(batch.batch_id)
            self.loaded.emit({"batch": batch, "manager": manager, "log_text": log_text, "warnings": getattr(self, "_warnings", [])})
        except Exception as exc:
            self.failed.emit(str(exc))

    def _load_manager_for_batch(self, batch: TaskBatch) -> TaskManager:
        warnings: list[str] = []
        self._warnings = warnings
        manager = TaskManager(self.batch_manager.task_state_path(batch.batch_id))
        manager.configure_batch_context(batch.batch_id, batch.batch_name, batch.imported_at, batch.source_excel_path)
        manager.configure_defaults(
            self.config_snapshot.image_provider,
            self.config_snapshot.image_model_logical_key,
            self.config_snapshot.video_provider,
            self.config_snapshot.video_model_logical_key,
        )
        tasks = manager.load_state(allow_legacy=False, save_after_load=False)
        if tasks:
            mismatched = [task for task in tasks if task.batch_id != batch.batch_id]
            if mismatched:
                backup_tasks = []
                try:
                    backup_tasks = manager.load_tasks_from_state_path(manager.backup_state_path)
                except Exception:
                    backup_tasks = []
                backup_matches = backup_tasks and not any(task.batch_id and task.batch_id != batch.batch_id for task in backup_tasks)
                if backup_matches:
                    manager.tasks = backup_tasks
                    tasks = backup_tasks
                    warnings.append(f"批次 {batch.batch_id} 主状态批次字段异常，已从备份状态恢复 {len(tasks)} 条")
                else:
                    for task in mismatched:
                        task.batch_id = batch.batch_id
                        task.batch_name = batch.batch_name
                        task.imported_at = task.imported_at or batch.imported_at
                        task.source_excel_path = task.source_excel_path or batch.source_excel_path
                        task.task_uid = TaskManager.task_uid_for(task)
                    warnings.append(f"批次 {batch.batch_id} 的任务状态字段已按批次目录修复，保留原执行记录 {len(tasks)} 条")
                manager.save_state(force_backup=True)
        if not tasks and batch.task_count and batch.source_excel_path:
            source = Path(batch.source_excel_path)
            if source.exists():
                try:
                    recovered = load_tasks_from_excel(source)
                    batch_config = dict(batch.batch_config or {})
                    image_provider = get_image_provider(batch_config.get("image_provider") or self.config_snapshot.image_provider)
                    video_provider = get_video_provider(batch_config.get("video_provider") or self.config_snapshot.video_provider)
                    for task in recovered:
                        task.batch_id = batch.batch_id
                        task.batch_name = batch.batch_name
                        task.task_uid = TaskManager.task_uid_for(task)
                        task.imported_at = batch.imported_at
                        task.source_excel_path = batch.source_excel_path
                        task.task_added_date = task.task_added_date or (batch.batch_id[:10] if len(batch.batch_id) >= 10 else today_text())
                        task.batch_date = task.batch_date or task.task_added_date
                        task.image_provider = batch_config.get("image_provider") or self.config_snapshot.image_provider
                        task.image_model_logical_key = batch_config.get("image_model_logical_key") or self.config_snapshot.image_model_logical_key
                        task.image_model_display = model_display_name(image_provider, task.image_model_logical_key)
                        task.video_provider = batch_config.get("video_provider") or self.config_snapshot.video_provider
                        task.video_model_logical_key = batch_config.get("video_model_logical_key") or self.config_snapshot.video_model_logical_key
                        task.video_model_display = model_display_name(video_provider, task.video_model_logical_key)
                        task.netdisk_original_path = task.netdisk_original_path or task.netdisk_path
                        task.netdisk_http_path = convert_netdisk_path_to_http(task.netdisk_original_path, self.config_snapshot)
                    manager.set_tasks(recovered)
                    self.batch_manager.update_batch(batch.batch_id, manager.tasks)
                except Exception as exc:
                    warnings.append(f"批次状态缺失，尝试从 Excel 恢复失败: {exc}")
        return manager

    def _read_batch_log(self, batch_id: str) -> str:
        path = self.batch_manager.log_path(batch_id)
        try:
            if path.exists() and path.stat().st_size > 0:
                return "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[-500:])
        except OSError:
            return ""
        return ""


class ArchiveVideoRepairWorker(QThread):
    repaired = Signal(dict)
    failed = Signal(str, str)

    def __init__(
        self,
        batch_manager: BatchManager,
        batch_id: str,
        config: AppConfig,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.batch_manager = batch_manager
        self.batch_id = batch_id
        self.config_snapshot = copy.deepcopy(config)

    def run(self) -> None:
        try:
            batch = self.batch_manager.load_batch(self.batch_id)
            if not batch:
                self.repaired.emit({"batch_id": self.batch_id, "repaired": 0, "tasks": []})
                return

            manager = TaskManager(self.batch_manager.task_state_path(batch.batch_id))
            manager.configure_batch_context(batch.batch_id, batch.batch_name, batch.imported_at, batch.source_excel_path)
            manager.configure_defaults(
                self.config_snapshot.image_provider,
                self.config_snapshot.image_model_logical_key,
                self.config_snapshot.video_provider,
                self.config_snapshot.video_model_logical_key,
            )
            manager.load_state(allow_legacy=False, save_after_load=False)
            if not manager.tasks:
                self.repaired.emit({"batch_id": batch.batch_id, "repaired": 0, "tasks": []})
                return

            executor = WorkflowExecutor(self.config_snapshot, batch.workflow_definition)
            repaired_tasks: list[dict] = []
            repaired = 0
            for task in manager.tasks:
                try:
                    executor.migrate_legacy_task(task)
                    executor.hydrate_legacy_video_url_for_download(task)
                    count = int(executor.repair_archived_video_paths(task) or 0)
                    if count:
                        repaired += count
                        repaired_tasks.append(task.model_dump(mode="json"))
                except Exception:
                    continue

            if repaired:
                manager.save_state(force_backup=True)
                self.batch_manager.update_batch(batch.batch_id, manager.tasks)
            self.repaired.emit({"batch_id": batch.batch_id, "repaired": repaired, "tasks": repaired_tasks})
        except Exception as exc:
            self.failed.emit(self.batch_id, str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Veo3 视频批量生成工具")
        self._set_app_icon()

        self.config = load_config()
        ensure_runtime_dirs(self.config)
        self.batch_manager = BatchManager(self.config.software_log_root, self.config.batch_root_dir_name)
        self.logger, self.log_path = setup_logger(self.config.output_dir)
        self.ui_diag = UIDiagnostics(PROJECT_ROOT, runtime_events_dir(self.config, "ui_diagnostics"))
        self.manager = TaskManager(self.config.state_path)
        self.manager.configure_defaults(
            self.config.image_provider,
            self.config.image_model_logical_key,
            self.config.video_provider,
            self.config.video_model_logical_key,
        )
        self.worker: BatchWorker | None = None
        self.worker_process: subprocess.Popen | None = None
        self.worker_events_path: Path | None = None
        self.worker_commands_path: Path | None = None
        self.worker_event_offset = 0
        self.worker_process_finished_seen = False
        self.manual_poll_worker: ManualPollWorker | None = None
        self.batch_load_worker: BatchLoadWorker | None = None
        self.excel_import_worker: ExcelImportWorker | None = None
        self.import_loading_state: tuple[QPushButton | None, str, bool, str] | None = None
        self.archive_repair_worker: ArchiveVideoRepairWorker | None = None
        self.manual_poll_manager: TaskManager | None = None
        self.manual_poll_batch_id = ""
        self.manual_poll_loading_state: tuple[QPushButton | None, str, bool, str] | None = None
        self.polling_task_ids_in_progress: set[str] = set()
        self.polling_task_ids_lock = threading.Lock()
        self.running_manager: TaskManager | None = None
        self.running_config: AppConfig | None = None
        self.active_running_batch_id = self.config.active_running_batch_id or ""
        self.active_running_status_override: str | None = None
        self.current_batch_id = ""
        self.current_view_batch_id = self.config.last_view_batch_id or self.config.last_selected_batch_id or ""
        self.selected_batch_list_id = self.current_view_batch_id
        self.current_batch: TaskBatch | None = None
        self.batch_run_queue: list[BatchRunRequest] = []
        self.run_queue_paused_by_user_stop = False
        self.pending_api_switch_request: tuple[str, dict, bool] | None = None
        self.force_shutdown_reason = ""
        self._loading_batch_combo = False
        self._loading_batch_table = False
        self._restoring_column_widths = False
        self._table_refresh_pending = False
        self._batch_refresh_pending = False
        self._stats_refresh_pending = False
        self._pending_stats: dict | None = None
        self._pending_task: TaskItem | None = None
        self._pending_batch_updates: dict[str, tuple[TaskManager, str | None]] = {}
        self._batch_card_live_stats_cache: dict[str, dict] = {}
        self._batch_card_stats_snapshots: dict[str, dict] = {}
        self._pending_log_lines: list[str] = []
        self._background_log_counter = 0
        self._batch_switch_in_progress = False
        self._suspend_live_refresh_until = 0.0
        self._last_full_table_refresh = 0.0
        self._last_batch_snapshot_write = 0.0
        self._last_stale_runtime_reconcile_at = 0.0
        self._table_refresh_interval_seconds = 2.5
        self._batch_snapshot_write_interval_seconds = 5.0
        self._loading_depth = 0
        self._loading_overlay: LoadingOverlay | None = None
        self.main_workspace: QWidget | None = None
        self.active_filters: dict[str, set[str]] = {
            str(field): {str(value) for value in values}
            for field, values in dict(self.config.active_filters or {}).items()
            if isinstance(values, list)
        }
        self.group_by_fields: list[str] = list(self.config.group_by_fields or [])
        self.group_collapsed: set[str] = set()
        self.sort_rules: list[tuple[str, bool]] = []
        self.display_rows: list[dict] = []
        self.filtered_task_keys: set[tuple[int, str]] = set()
        self.batch_card_batches: dict[str, TaskBatch] = {}
        self._dashboard_stats_batch_id = ""
        self._dashboard_stats_snapshot: dict | None = None
        self._layout_mode = ""
        self._log_panel_collapsed = bool((getattr(self.config, "ui_layout", {}) or {}).get("log_panel_default_collapsed", True))
        self._log_panel_user_toggled = False
        self._detail_panel_collapsed = False
        self._ui_log_entries: list[tuple[str, str]] = []
        self._ui_log_cleared = False
        # Toast notifications stack in the bottom-right of the window.
        self._toast_stack: list[Toast] = []
        self.light_image_tool_window: QWidget | None = None

        self._build_ui()
        self.setAcceptDrops(bool(getattr(self.config, "enable_excel_drag_drop_import", True)))
        self._apply_adaptive_window_size()
        self.refresh_batch_combo()
        self._start_batch_refresh_timer()
        self._load_initial_state()

    def _set_app_icon(self) -> None:
        candidates = [
            PROJECT_ROOT / "assets" / "app_icon.ico",
            Path(getattr(sys, "_MEIPASS", "")) / "assets" / "app_icon.ico",
        ]
        for path in candidates:
            if path.exists():
                self.setWindowIcon(QIcon(str(path)))
                return

    def _apply_adaptive_window_size(self) -> None:
        screen = QApplication.primaryScreen()
        if not screen:
            self.setMinimumSize(720, 480)
            self.resize(1200, 760)
            return
        available = screen.availableGeometry()
        min_w = max(480, min(620, int(available.width() * 0.55)))
        min_h = max(360, min(420, int(available.height() * 0.55)))
        target_w = max(min_w, min(1500, int(available.width() * 0.92)))
        target_h = max(min_h, min(900, int(available.height() * 0.90)))
        self.setMinimumSize(min_w, min_h)
        self.resize(target_w, target_h)

    def _build_ui(self) -> None:
        self.setFont(QFont("Microsoft YaHei UI", 10))
        self._install_style()
        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.sidebar = self._build_sidebar()
        self.batch_panel = self._build_batch_list_panel()
        self.main_splitter.addWidget(self.sidebar)
        self.main_splitter.addWidget(self.batch_panel)
        main = QWidget()
        self.main_workspace = main
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(14)
        main_layout.addWidget(self._build_top_config_bar())

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_console_page())
        self.pages.addWidget(self._build_settings_page())
        main_layout.addWidget(self.pages, 1)

        self.main_splitter.addWidget(main)
        self._loading_overlay = LoadingOverlay(main)
        self._loading_overlay.hide()
        main.installEventFilter(self)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 0)
        self.main_splitter.setStretchFactor(2, 1)
        ui_layout = getattr(self.config, "ui_layout", {}) or {}
        self.main_splitter.setSizes([
            int(ui_layout.get("nav_width_large", 72)),
            int(ui_layout.get("batch_list_width_large", 300)),
            1280,
        ])
        layout.addWidget(self.main_splitter, 1)
        self._build_status_bar()
        self._install_button_feedback()
        self._switch_page(0)
        self.update_stats(self.manager.stats())
        self._apply_responsive_layout(force=True)

    def _build_status_bar(self) -> None:
        self.operation_progress = QProgressBar()
        self.operation_progress.setRange(0, 0)
        self.operation_progress.setFixedWidth(120)
        self.operation_progress.hide()
        self.view_status_label = QLabel("当前查看：未选择批次")
        self.running_status_label = QLabel("当前未执行任务")
        self.run_queue_status_label = QLabel("运行队列：0")
        self.statusBar().addPermanentWidget(self.view_status_label, 1)
        self.statusBar().addPermanentWidget(self.running_status_label, 1)
        self.statusBar().addPermanentWidget(self.run_queue_status_label)
        self.statusBar().addPermanentWidget(self.operation_progress)
        self.update_status_summary()

    def show_status(self, message: str, timeout_ms: int = 4000, log: bool = False) -> None:
        if hasattr(self, "statusBar"):
            self.statusBar().showMessage(message, timeout_ms)
        if log and hasattr(self, "log_text"):
            self.append_log("INFO", message)

    # --- v2 Toast feedback ---------------------------------------------------
    def show_toast(self, message: str, kind: str = Toast.KIND_INFO, duration_ms: int = 3500, also_status: bool = True) -> None:
        """Show a self-painted floating notification.

        Respects ``config.enable_toast_feedback``; when disabled, falls back to
        the existing statusBar message so the feedback isn't silently lost.
        """
        if not message:
            return
        toast_enabled = bool(getattr(self.config, "enable_toast_feedback", True))
        statusbar_enabled = bool(getattr(self.config, "enable_statusbar_feedback", True))
        if not toast_enabled:
            if statusbar_enabled:
                self.show_status(message, timeout_ms=duration_ms)
            return
        toast = Toast(self, message, kind=kind, duration_ms=duration_ms)
        toast.show()
        self._toast_stack.append(toast)
        self._restack_toasts()
        if also_status and statusbar_enabled:
            self.show_status(message, timeout_ms=duration_ms)

    def show_success_toast(self, message: str, duration_ms: int = 3000) -> None:
        self.show_toast(message, kind=Toast.KIND_SUCCESS, duration_ms=duration_ms)

    def show_warning_toast(self, message: str, duration_ms: int = 5000) -> None:
        self.show_toast(message, kind=Toast.KIND_WARNING, duration_ms=duration_ms)

    def show_error_toast(self, message: str, duration_ms: int = 6000) -> None:
        self.show_toast(message, kind=Toast.KIND_ERROR, duration_ms=duration_ms)

    def _toast_dismissed(self, toast: Toast) -> None:
        if toast in self._toast_stack:
            self._toast_stack.remove(toast)
        self._restack_toasts()

    def _restack_toasts(self) -> None:
        margin = 18
        bottom_offset = 32  # leave room for the QStatusBar
        spacing = 8
        parent_w = self.width()
        parent_h = self.height()
        # Newest toast sits at the bottom; older ones stack upward.
        y_cursor = parent_h - bottom_offset
        for toast in reversed(self._toast_stack):
            toast.adjustSize()
            x = max(margin, parent_w - toast.width() - margin)
            y_cursor -= toast.height() + spacing
            toast.move(x, max(margin, y_cursor))
            toast.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt naming
        super().resizeEvent(event)
        self._restack_toasts()
        self._position_loading_overlay()
        self._apply_responsive_layout()

    def _position_loading_overlay(self) -> None:
        overlay = getattr(self, "_loading_overlay", None)
        if overlay is not None and self.main_workspace is not None:
            overlay.setGeometry(self.main_workspace.rect())
            if overlay.isVisible():
                overlay.raise_()

    def _apply_responsive_layout(self, force: bool = False) -> None:
        ui_layout = getattr(self.config, "ui_layout", {}) or {}
        mode = layout_mode_for_width(self.width(), ui_layout)
        if not force and mode == getattr(self, "_layout_mode", ""):
            return
        old_mode = getattr(self, "_layout_mode", "")
        self._layout_mode = mode
        compact = mode != "large"
        extra_compact = mode == "extra_compact"

        nav_width = int(ui_layout.get("nav_width_compact" if compact else "nav_width_large", 64 if compact else 72))
        batch_width = int(ui_layout.get("batch_list_width_compact" if compact else "batch_list_width_large", 260 if compact else 300))
        if hasattr(self, "sidebar"):
            self.sidebar.setMinimumWidth(nav_width)
            self.sidebar.setMaximumWidth(max(nav_width, 104))
        if hasattr(self, "batch_panel"):
            self.batch_panel.setMinimumWidth(max(220, batch_width - 20))
            self.batch_panel.setMaximumWidth(batch_width + (30 if compact else 80))
        if hasattr(self, "main_splitter"):
            self.main_splitter.setSizes([nav_width, batch_width, max(600, self.width() - nav_width - batch_width - 60)])

        self._layout_metric_cards(compact)
        if hasattr(self, "preview_panel"):
            collapse_detail = compact and bool(ui_layout.get("detail_panel_collapsed_in_compact", True))
            self._detail_panel_collapsed = collapse_detail
            self.preview_panel.setVisible(not collapse_detail)
        if hasattr(self, "log_panel"):
            if compact:
                self._log_panel_collapsed = bool(ui_layout.get("log_panel_collapsed_in_compact", True))
            elif not bool(getattr(self, "_log_panel_user_toggled", False)):
                self._log_panel_collapsed = bool(ui_layout.get("log_panel_default_collapsed", True))
            self._apply_log_panel_state()
        if hasattr(self, "stats_strip"):
            self.stats_strip.setVisible(not extra_compact)
        if hasattr(self, "table"):
            self.apply_column_visibility()
            row_height = int(ui_layout.get("task_table_row_height", 36))
            self.table.verticalHeader().setDefaultSectionSize(max(30, row_height - (4 if extra_compact else 0)))
        if old_mode and old_mode != mode:
            self.ui_diag.write_event(
                "RESPONSIVE_LAYOUT_CHANGED",
                window_width=self.width(),
                window_height=self.height(),
                old_layout_mode=old_mode,
                new_layout_mode=mode,
                detail_panel_collapsed=bool(getattr(self, "_detail_panel_collapsed", False)),
                log_panel_collapsed=bool(getattr(self, "_log_panel_collapsed", False)),
                visible_columns=self.current_visible_columns() if hasattr(self, "table") else [],
            )

    def _layout_metric_cards(self, compact: bool) -> None:
        layout = getattr(self, "metric_board_layout", None)
        cards = getattr(self, "metric_cards", {})
        if layout is None or not cards:
            return
        for key, card in cards.items():
            layout.removeWidget(card)
            card.show()
        order = ["progress", "total", "pending", "failed", "concurrency"]
        if compact:
            positions = {
                "progress": (0, 0),
                "pending": (0, 1),
                "failed": (1, 0),
                "concurrency": (1, 1),
                "total": (1, 2),
            }
            for key in order:
                if key in cards:
                    row, col = positions[key]
                    layout.addWidget(cards[key], row, col)
        else:
            for col, key in enumerate(order):
                if key in cards:
                    layout.addWidget(cards[key], 0, col)

    def _apply_log_panel_state(self) -> None:
        if not hasattr(self, "log_panel"):
            return
        collapsed = bool(getattr(self, "_log_panel_collapsed", False))
        if hasattr(self, "log_text"):
            self.log_text.setVisible(not collapsed)
        always_visible = (getattr(self, "log_workbench_btn", None), getattr(self, "log_open_btn", None))
        for widget in getattr(self, "log_compact_only_widgets", []) or []:
            if any(widget is item for item in always_visible):
                widget.setVisible(True)
            else:
                widget.setVisible(collapsed)
        for widget in getattr(self, "log_expanded_only_widgets", []) or []:
            widget.setVisible(not collapsed)
        if hasattr(self, "log_toggle_btn"):
            self.log_toggle_btn.setText("展开" if collapsed else "折叠")
        height = int((getattr(self.config, "ui_layout", {}) or {}).get("log_panel_height", 160))
        self.log_panel.setMaximumHeight(48 if collapsed else max(120, height + 70))
        self.log_panel.setMinimumHeight(42 if collapsed else 110)

    def toggle_detail_panel(self) -> None:
        if not hasattr(self, "preview_panel"):
            return
        self._detail_panel_collapsed = self.preview_panel.isVisible()
        self.preview_panel.setVisible(not self._detail_panel_collapsed)
        self.show_status("已隐藏任务详情面板" if self._detail_panel_collapsed else "已展开任务详情面板", log=True)

    @staticmethod
    def _loading_detail_for(message: str) -> str:
        text = str(message or "")
        if any(keyword in text for keyword in ["导入", "加载", "刷新", "检查", "筛选", "分组", "字段"]):
            return "数据较多时可能需要几秒，请稍等。"
        if any(keyword in text for keyword in ["下载", "保存素材"]):
            return "正在保存生成结果，请不要关闭程序。"
        if "导出" in text:
            return "正在整理结果文件，完成后会提示你。"
        if any(keyword in text for keyword in ["启动", "执行", "轮询"]):
            return "正在启动后台任务，启动后你可以继续查看其他批次。"
        if "保存设置" in text:
            return "正在写入本地配置。"
        return "已收到你的操作，正在为你处理。"

    def _show_loading_overlay(self, message: str) -> None:
        if not getattr(self.config, "ui_loading_indicator_enabled", True):
            return
        overlay = getattr(self, "_loading_overlay", None)
        if overlay is None:
            return
        overlay.show_operation(f"正在{message}...", self._loading_detail_for(message))

    def _hide_loading_overlay(self) -> None:
        overlay = getattr(self, "_loading_overlay", None)
        if overlay is not None:
            overlay.hide()

    def _toast_for_loading_result(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            text = "已完成，结果已刷新"
        if any(keyword in text for keyword in ["失败", "错误", "异常"]):
            self.show_error_toast(f"{text}。这次没有成功，已保留当前记录，可稍后重试")
        elif "取消" in text:
            self.show_warning_toast(text)
        else:
            self.show_success_toast(text)

    def begin_loading(self, message: str, button: QPushButton | None = None) -> tuple[QPushButton | None, str, bool, str]:
        self.ui_diag.mark_action(message)
        self.show_status(f"正在{message}...", log=True)
        if hasattr(self, "operation_progress") and self.config.ui_loading_indicator_enabled:
            self.operation_progress.show()
        self._loading_depth += 1
        self._show_loading_overlay(message)
        old_text = button.text() if button else ""
        old_enabled = button.isEnabled() if button else True
        if button and self.config.ui_button_feedback_enabled:
            button.setText("处理中...")
            button.setEnabled(False)
        QApplication.processEvents()
        return button, old_text, old_enabled, message

    def end_loading(self, state: tuple[QPushButton | None, str, bool, str], message: str = "操作完成") -> None:
        button = state[0] if state else None
        old_text = state[1] if state and len(state) > 1 else ""
        old_enabled = state[2] if state and len(state) > 2 else True
        if button:
            button.setText(old_text)
            button.setEnabled(old_enabled)
        self._loading_depth = max(0, self._loading_depth - 1)
        if self._loading_depth == 0 and hasattr(self, "operation_progress"):
            self.operation_progress.hide()
            self._hide_loading_overlay()
        self.show_status(message, log=True)
        self._toast_for_loading_result(message)
        QApplication.processEvents()

    def run_with_feedback(self, action: str, func, button: QPushButton | None = None):
        state = self.begin_loading(action, button)
        try:
            result = func()
            if result == "BACKGROUND_STARTED":
                self.end_loading(state, "后台执行已启动，可以继续查看其他批次")
                if self.is_background_running():
                    self._set_running_buttons(True)
                return result
            if result == "QUEUED_FOR_RUN":
                self.end_loading(state, "已加入运行队列，当前批次完成后会自动执行")
                if self.is_background_running():
                    self._set_running_buttons(True)
                return result
            if result == "RETRY_REQUESTED":
                self.end_loading(state, "已发送运行中重试请求")
                if self.is_background_running():
                    self._set_running_buttons(True)
                return result
            self.end_loading(state, f"{action}完成")
            if self.is_background_running():
                self._set_running_buttons(True)
            return result
        except Exception:
            self.end_loading(state, f"{action}失败")
            if self.is_background_running():
                self._set_running_buttons(True)
            raise

    def _install_button_feedback(self) -> None:
        if not self.config.ui_button_feedback_enabled:
            return
        for button in self.findChildren(QPushButton):
            button.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if obj is getattr(self, "main_workspace", None) and event.type() in {QEvent.Resize, QEvent.Move}:
            self._position_loading_overlay()
        if hasattr(self, "_task_empty_overlay") and hasattr(self, "table") and obj is self.table.viewport() and event.type() == QEvent.Resize:
            self._position_table_empty_overlay()
        if isinstance(obj, QComboBox) and event.type() in {QEvent.MouseButtonPress, QEvent.FocusIn, QEvent.Show}:
            self._refresh_combo_box_display(obj)
        if self.config.ui_button_feedback_enabled and isinstance(obj, QPushButton) and event.type() == QEvent.MouseButtonPress:
            self.show_status(f"已点击：{obj.text()}", timeout_ms=1800)
        return super().eventFilter(obj, event)

    def _setup_combo_box(self, combo: QComboBox, min_popup_width: int = 220, compact_chars: int = 12) -> QComboBox:
        combo.setMinimumWidth(0)
        combo.setMinimumContentsLength(max(8, int(compact_chars)))
        try:
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        except AttributeError:
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        combo.setProperty("min_popup_width", int(min_popup_width))
        combo.installEventFilter(self)
        combo.currentIndexChanged.connect(lambda *_args, c=combo: self._refresh_combo_box_display(c))
        self._refresh_combo_box_display(combo)
        return combo

    def _refresh_combo_box_display(self, combo: QComboBox) -> None:
        if combo is None:
            return
        texts = [combo.itemText(index) for index in range(combo.count())]
        for index, text in enumerate(texts):
            combo.setItemData(index, text, Qt.ToolTipRole)
        combo.setToolTip(combo.currentText())
        view = combo.view()
        if view is None:
            return
        screen = combo.screen() or QApplication.primaryScreen()
        screen_width = screen.availableGeometry().width() if screen else self.width()
        metrics = view.fontMetrics()
        text_pixels = [metrics.horizontalAdvance(text) for text in texts]
        min_popup_width = int(combo.property("min_popup_width") or 220)
        width = bounded_popup_width(
            texts,
            combo.width(),
            screen_width,
            char_px=max(6, metrics.averageCharWidth()),
            text_pixel_widths=text_pixels,
            min_width=min_popup_width,
        )
        view.setMinimumWidth(width)

    def _start_batch_refresh_timer(self) -> None:
        self.batch_refresh_timer = QTimer(self)
        self.batch_refresh_timer.timeout.connect(self.refresh_running_batch_snapshot)
        interval = max(1, int(self.config.batch_list_refresh_interval_seconds or 2)) * 1000
        self.batch_refresh_timer.start(interval)
        self.ui_refresh_timer = QTimer(self)
        self.ui_refresh_timer.timeout.connect(self.flush_deferred_ui_updates)
        self.ui_refresh_timer.start(1000)
        self.process_worker_timer = QTimer(self)
        self.process_worker_timer.timeout.connect(self.poll_process_worker_events)
        self.process_worker_timer.start(500)
        self.ui_heartbeat_timer = QTimer(self)
        self.ui_heartbeat_timer.timeout.connect(self.check_ui_heartbeat)
        self.ui_heartbeat_timer.start(self.ui_diag.heartbeat_interval_ms)

    def queue_batch_update(self, batch_id: str, manager: TaskManager | None, status_override: str | None = None) -> None:
        if batch_id and manager is not None:
            self._pending_batch_updates[batch_id] = (manager, status_override)

    def apply_batch_card_update(self, batch_id: str, manager: TaskManager | None, status_override: str | None = None) -> None:
        if not batch_id or manager is None:
            return
        batch = self.batch_card_batches.get(batch_id)
        if batch is None and self.current_batch and self.current_batch.batch_id == batch_id:
            batch = self.current_batch
        if batch is None:
            return
        batch = self.batch_manager.update_stats(batch, manager.tasks, status_override=status_override, save=False)
        live_stats = self._batch_card_live_stats_cache.get(batch_id)
        if live_stats and batch_id == self.active_running_batch_id and self.is_background_running():
            batch = apply_stats_to_batch_snapshot(
                batch,
                live_stats,
                status_override=status_override,
                active_running=True,
            )
        batch = self._stable_batch_card_stats(batch_id, batch)
        self._render_batch_card_update(batch_id, batch)

    def apply_batch_card_stats_update(self, batch_id: str, stats: dict, status_override: str | None = None) -> None:
        if not batch_id:
            return
        batch = self.batch_card_batches.get(batch_id)
        if batch is None and self.current_batch and self.current_batch.batch_id == batch_id:
            batch = self.current_batch
        if batch is None:
            return
        batch = apply_stats_to_batch_snapshot(
            batch,
            stats,
            status_override=status_override,
            active_running=batch_id == self.active_running_batch_id and self.is_background_running(),
        )
        batch = self._stable_batch_card_stats(batch_id, batch)
        self._render_batch_card_update(batch_id, batch)

    def _render_batch_card_update(self, batch_id: str, batch: TaskBatch) -> None:
        self.batch_card_batches[batch_id] = batch
        if self.current_batch and self.current_batch.batch_id == batch_id:
            self.current_batch = batch
        if hasattr(self, "batch_list"):
            for row in range(self.batch_list.count()):
                item = self.batch_list.item(row)
                if str(item.data(Qt.UserRole) or "") != batch_id:
                    continue
                widget = self.batch_list.itemWidget(item)
                if isinstance(widget, BatchCardWidget):
                    widget.update_batch(
                        batch,
                        selected=batch_id == self.selected_batch_list_id or batch_id == self.current_batch_id,
                        running=batch_id == self.active_running_batch_id and self.is_background_running(),
                    )
                break

    def queue_table_refresh(self, task: TaskItem | None = None, stats: dict | None = None) -> None:
        if task is not None:
            self._pending_task = task
        if stats is not None:
            self._pending_stats = stats
            self._stats_refresh_pending = True
        self._table_refresh_pending = True

    def queue_log_line(self, level: str, message: str) -> None:
        self._pending_log_lines.append(f"[{level}] {message}")
        if len(self._pending_log_lines) > 1000:
            del self._pending_log_lines[:-1000]

    def check_ui_heartbeat(self) -> None:
        event = self.ui_diag.record_heartbeat(
            current_batch_id=self.current_batch_id,
            active_running_batch_id=self.active_running_batch_id,
            task_count=len(getattr(self.manager, "tasks", []) or []),
            display_rows=len(getattr(self, "display_rows", []) or []),
            pending_logs=len(getattr(self, "_pending_log_lines", []) or []),
            pending_batch_updates=len(getattr(self, "_pending_batch_updates", {}) or {}),
            table_refresh_pending=bool(getattr(self, "_table_refresh_pending", False)),
            events_count=0,
            active_threads=threading.active_count(),
        )
        if event and time.monotonic() - self.ui_diag._last_lag_status_time > 8:
            self.ui_diag._last_lag_status_time = time.monotonic()
            delay = int(event.get("delay_ms", 0) or 0)
            self.show_status(f"检测到界面卡顿 {delay}ms，诊断已写入 {self.ui_diag.log_dir}", timeout_ms=3500)

    def is_process_worker_running(self) -> bool:
        return self.worker_process is not None and self.worker_process.poll() is None

    def is_background_running(self) -> bool:
        return bool((self.worker and self.worker.isRunning()) or self.is_process_worker_running())

    def stale_inactive_batch_status(self, batch: TaskBatch) -> str:
        total = max(0, int(batch.task_count or 0))
        completed = max(0, int(batch.completed_count or 0))
        failed = max(0, int(batch.failed_count or 0))
        skipped = max(0, int(batch.skipped_count or 0))
        timeout = max(0, int(batch.timeout_count or 0))
        ended = completed + failed + skipped + timeout
        if total <= 0:
            return "CREATED"
        if completed >= total and not (failed or skipped or timeout):
            return "COMPLETED"
        if ended >= total:
            if completed <= 0 and failed + timeout >= total:
                return "FAILED"
            return "PARTIAL_FAILED" if (failed or skipped or timeout) else "COMPLETED"
        if failed or skipped or timeout:
            return "PARTIAL_FAILED"
        return "STOPPED"

    def display_batch_for_runtime_state(self, batch: TaskBatch) -> TaskBatch:
        if batch.batch_id == self.active_running_batch_id and self.is_background_running():
            return batch
        if str(batch.status or "") not in STALE_RUNTIME_BATCH_STATUSES:
            return batch
        display_batch = copy.copy(batch)
        display_batch.status = self.stale_inactive_batch_status(display_batch)
        display_batch.polling_count = 0
        return display_batch

    def reconcile_stale_runtime_state(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_stale_runtime_reconcile_at < 60.0:
            return
        self._last_stale_runtime_reconcile_at = now
        background_running = self.is_background_running()
        if self.active_running_batch_id and not background_running:
            stale_id = self.active_running_batch_id
            self.active_running_batch_id = ""
            self.active_running_status_override = None
            self.config.active_running_batch_id = ""
            save_config(self.config)
            self.append_log("WARNING", f"[BATCH_STATUS] 已清理失效的运行中标记：{stale_id}")

    def run_queue_count(self) -> int:
        return len(getattr(self, "batch_run_queue", []) or [])

    def run_queue_summary_text(self) -> str:
        count = self.run_queue_count()
        if count <= 0:
            return "运行队列：0"
        next_batch_id = self.batch_run_queue[0].batch_id
        batch = self.batch_manager.load_batch(next_batch_id)
        next_name = batch.batch_name if batch else next_batch_id
        return f"运行队列：{count}｜下一个：{next_name}"

    def update_run_queue_status(self) -> None:
        text = self.run_queue_summary_text()
        if hasattr(self, "run_queue_status_label"):
            self.run_queue_status_label.setText(text)
            self.run_queue_status_label.setToolTip(self._run_queue_tooltip())
        if hasattr(self, "run_queue_strip_label"):
            self.run_queue_strip_label.setText(text)
            self.run_queue_strip_label.setToolTip(self._run_queue_tooltip())
        if hasattr(self, "current_batch_status_label"):
            self.current_batch_status_label.setToolTip(self._run_queue_tooltip())

    def _run_queue_tooltip(self) -> str:
        queue = getattr(self, "batch_run_queue", []) or []
        if not queue:
            return "当前没有等待执行的批次"
        lines = ["等待执行的批次："]
        for index, request in enumerate(queue[:20], start=1):
            batch = self.batch_manager.load_batch(request.batch_id)
            name = batch.batch_name if batch else request.batch_id
            mode = self._run_request_mode_text(request)
            lines.append(f"{index}. {name} / {request.batch_id} / {mode}")
        if len(queue) > 20:
            lines.append(f"... 还有 {len(queue) - 20} 个批次")
        return "\n".join(lines)

    @staticmethod
    def _run_request_mode_text(request: BatchRunRequest) -> str:
        if request.poll_only:
            return "继续轮询"
        if request.failed_only:
            return "重试失败"
        if request.execution_mode == "image_only":
            return "仅生成图片"
        if request.execution_mode == "video_only":
            return "图生视频"
        return "完整流程"

    def enqueue_batch_run(
        self,
        batch_id: str,
        failed_only: bool = False,
        poll_only: bool = False,
        execution_mode: str = "full",
        selected_task_keys: set[str] | tuple[str, ...] | list[str] | None = None,
        source: str = "user",
    ) -> str:
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            return "QUEUE_SKIPPED"
        keys = tuple(str(key) for key in (selected_task_keys or []) if str(key))
        request = BatchRunRequest(
            batch_id=batch_id,
            failed_only=bool(failed_only),
            poll_only=bool(poll_only),
            execution_mode=str(execution_mode or "full"),
            selected_task_keys=keys,
            requested_at=now_text(),
            source=source,
        )
        self.batch_run_queue.append(request)
        self.update_run_queue_status()
        batch = self.batch_manager.load_batch(batch_id)
        name = batch.batch_name if batch else batch_id
        message = f"[RUN_QUEUE] 已加入运行队列：{name} / {batch_id}，当前位置 {len(self.batch_run_queue)}"
        self.append_log("INFO", message)
        self.show_status(message)
        return "QUEUED_FOR_RUN"

    def start_next_queued_batch(self) -> bool:
        if self.is_background_running():
            return False
        if self.run_queue_paused_by_user_stop:
            if self.run_queue_count():
                self.show_status("运行队列已暂停：用户刚刚停止了当前批次，后续批次不会自动启动", log=True)
            self.update_run_queue_status()
            return False
        while self.batch_run_queue:
            request = self.batch_run_queue.pop(0)
            self.update_run_queue_status()
            batch = self.batch_manager.load_batch(request.batch_id)
            if not batch:
                self.append_log("WARNING", f"[RUN_QUEUE] 跳过不存在的批次：{request.batch_id}")
                continue
            self.show_status(f"[RUN_QUEUE] 正在启动队列中的下一个批次：{batch.batch_name} / {batch.batch_id}", log=True)
            QTimer.singleShot(
                0,
                lambda req=request: self.start_worker(
                    failed_only=req.failed_only,
                    poll_only=req.poll_only,
                    batch_id=req.batch_id,
                    execution_mode=req.execution_mode,
                    selected_task_keys=set(req.selected_task_keys) if req.selected_task_keys else None,
                    queued_start=True,
                ),
            )
            return True
        return False

    def process_runtime_paths(self, batch_id: str) -> tuple[Path, Path]:
        runtime_dir = runtime_events_dir(self.config, safe_pid(batch_id))
        return runtime_dir / "worker_events.jsonl", runtime_dir / "worker_commands.jsonl"

    def send_process_worker_command(self, command: str, payload: dict | None = None) -> None:
        if not self.worker_commands_path:
            return
        data = dict(payload or {})
        data["command"] = command
        append_event(self.worker_commands_path, "command", data)

    def _schedule_fast_process_worker_shutdown_for_api_switch(self, batch_id: str) -> None:
        """Do not let old in-flight provider calls delay a runtime API switch for minutes."""
        if not self.is_process_worker_running():
            return

        def pending_switch_matches() -> bool:
            pending = self.pending_api_switch_request
            return bool(pending and str(pending[0]) == str(batch_id))

        def kill_if_needed() -> None:
            process = self.worker_process
            if not process or process.poll() is not None or not pending_switch_matches():
                return
            self.append_log("WARNING", "旧后台进程仍未退出，正在强制结束后继续切换 API")
            try:
                process.kill()
            except Exception as exc:
                self.append_log("WARNING", f"强制结束旧后台进程失败：{exc}")

        def terminate_if_needed() -> None:
            process = self.worker_process
            if not process or process.poll() is not None or not pending_switch_matches():
                return
            self.append_log("WARNING", "旧后台请求未及时结束，正在快速结束旧执行进程，随后切换 API")
            try:
                process.terminate()
            except Exception as exc:
                self.append_log("WARNING", f"结束旧后台进程失败：{exc}")
            QTimer.singleShot(2500, kill_if_needed)

        QTimer.singleShot(2500, terminate_if_needed)

    def _schedule_process_worker_force_shutdown(
        self,
        reason: str,
        *,
        terminate_after_ms: int = 8000,
        kill_after_ms: int = 5000,
    ) -> None:
        """Escalate a cooperative stop when the child process cannot observe it."""
        process = self.worker_process
        if not process or process.poll() is not None:
            return
        batch_id = self.active_running_batch_id
        token = f"{batch_id}:{id(process)}:{time.monotonic():.6f}:{reason}"
        self.force_shutdown_reason = token

        def still_current_process() -> bool:
            return bool(
                self.worker_process is process
                and process.poll() is None
                and self.force_shutdown_reason == token
            )

        def kill_if_needed() -> None:
            if not still_current_process():
                return
            self.append_log("WARNING", f"{reason} 后后台进程仍未退出，正在强制结束进程")
            try:
                process.kill()
            except Exception as exc:
                self.append_log("WARNING", f"强制结束后台进程失败：{exc}")

        def terminate_if_needed() -> None:
            if not still_current_process():
                return
            self.append_log("WARNING", f"{reason} 后后台进程未及时退出，正在结束后台执行进程")
            try:
                process.terminate()
            except Exception as exc:
                self.append_log("WARNING", f"结束后台进程失败：{exc}")
            QTimer.singleShot(max(500, int(kill_after_ms)), kill_if_needed)

        QTimer.singleShot(max(1000, int(terminate_after_ms)), terminate_if_needed)

    def poll_process_worker_events(self) -> None:
        with self.ui_diag.measure("poll_process_worker_events",
            events_count=0,
            active_threads=threading.active_count(),
            current_batch_id=self.current_batch_id,
            active_running_batch_id=self.active_running_batch_id,
        ):
            if not self.worker_events_path:
                return
            events, self.worker_event_offset = read_events(
                self.worker_events_path,
                self.worker_event_offset,
                max_events=80,
                max_bytes=1_000_000,
            )
            if len(events) >= 200:
                self.ui_diag.write_event(
                    "WORKER_EVENT_BURST",
                    events_count=len(events),
                    active_threads=threading.active_count(),
                    current_batch_id=self.current_batch_id,
                    active_running_batch_id=self.active_running_batch_id,
                )
            self.handle_process_worker_events(events)
            if self.worker_process and self.worker_process.poll() is not None and not self.worker_process_finished_seen:
                self.finish_process_worker("后台执行进程已结束")

    def handle_process_worker_events(self, events: list[dict]) -> None:
        latest_task_updates: dict[str, dict] = {}
        unkeyed_task_updates: list[dict] = []
        latest_stats: dict | None = None
        finished_messages: list[str] = []

        for event in events:
            event_type = str(event.get("type") or "")
            if event_type == "log":
                self.append_worker_log(str(event.get("level") or "INFO"), str(event.get("message") or ""))
            elif event_type == "stats":
                stats = event.get("stats") if isinstance(event.get("stats"), dict) else {}
                latest_stats = stats
            elif event_type == "task_updated":
                task_data = event.get("task") if isinstance(event.get("task"), dict) else {}
                key = str(task_data.get("task_uid") or "").strip()
                if key:
                    latest_task_updates[key] = task_data
                elif task_data:
                    unkeyed_task_updates.append(task_data)
            elif event_type == "finished":
                finished_messages.append(str(event.get("message") or "后台执行完成"))
            elif event_type == "error":
                self.append_worker_log("ERROR", str(event.get("message") or "后台进程发生错误"))
            elif event_type == "started":
                self.show_status("后台执行进程已启动", timeout_ms=1800)

        for task_data in list(latest_task_updates.values()) + unkeyed_task_updates[-20:]:
            self.apply_process_task_update(task_data)
        if latest_stats is not None:
            self.update_running_stats(latest_stats)
        for message in finished_messages:
            self.finish_process_worker(message)

    def handle_process_worker_event(self, event: dict) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "log":
            self.append_worker_log(str(event.get("level") or "INFO"), str(event.get("message") or ""))
        elif event_type == "stats":
            stats = event.get("stats") if isinstance(event.get("stats"), dict) else {}
            self.update_running_stats(stats)
        elif event_type == "task_updated":
            self.apply_process_task_update(event.get("task") if isinstance(event.get("task"), dict) else {})
        elif event_type == "finished":
            self.finish_process_worker(str(event.get("message") or "后台执行完成"))
        elif event_type == "error":
            self.append_worker_log("ERROR", str(event.get("message") or "后台进程发生错误"))
        elif event_type == "started":
            self.show_status("后台执行进程已启动", timeout_ms=1800)

    def apply_process_task_update(self, task_data: dict) -> None:
        if not task_data:
            return
        manager = self.running_manager
        if manager is None:
            return
        key = str(task_data.get("task_uid") or "").strip()
        if not key:
            try:
                key = TaskManager.task_uid_for(TaskItem.model_validate(task_data))
            except Exception:
                return
        replaced = False
        task: TaskItem | None = None
        for index, old in enumerate(manager.tasks):
            old_key = old.task_uid or TaskManager.task_uid_for(old)
            if old_key == key:
                self._merge_process_task_patch(old, task_data)
                task = old
                replaced = True
                break
        if not replaced:
            try:
                task = TaskItem.model_validate(task_data)
            except Exception:
                return
            manager.tasks.append(task)
        if self.active_running_batch_id == self.current_batch_id:
            self.manager = manager
        if task is not None:
            self.update_task_row(task)

    def _merge_process_task_patch(self, task: TaskItem, patch: dict) -> None:
        merge_task_patch(task, patch)

    def finish_process_worker(self, message: str) -> None:
        if self.worker_process_finished_seen:
            return
        self.worker_process_finished_seen = True
        if self.active_running_batch_id:
            try:
                self.running_manager = self.load_manager_for_batch(self.active_running_batch_id)
            except Exception as exc:
                self.append_worker_log("WARNING", f"后台状态重载失败，继续使用内存快照：{exc}")
        self.worker_process = None
        self.worker_events_path = None
        self.worker_commands_path = None
        self.force_shutdown_reason = ""
        self.worker_event_offset = 0
        self.worker_finished(message)

    def flush_deferred_ui_updates(self) -> None:
        with self.ui_diag.measure("flush_deferred_ui_updates",
            pending_logs=len(getattr(self, "_pending_log_lines", []) or []),
            pending_batch_updates=len(getattr(self, "_pending_batch_updates", {}) or {}),
            table_refresh_pending=bool(getattr(self, "_table_refresh_pending", False)),
            stats_refresh_pending=bool(getattr(self, "_stats_refresh_pending", False)),
            display_rows=len(getattr(self, "display_rows", []) or []),
            task_count=len(getattr(self.manager, "tasks", []) or []),
            batch_switch_in_progress=bool(self._batch_switch_in_progress),
            suspend_live_refresh_until=self._suspend_live_refresh_until,
            last_full_table_refresh=self._last_full_table_refresh,
            last_batch_snapshot_write=self._last_batch_snapshot_write,
            active_threads=threading.active_count(),
        ):
            self._flush_deferred_ui_updates_impl()

    def _flush_deferred_ui_updates_impl(self) -> None:
        now = time.monotonic()
        live_refresh_suspended = self._batch_switch_in_progress or now < self._suspend_live_refresh_until
        if self._pending_log_lines and hasattr(self, "log_text"):
            lines = self._pending_log_lines[:200]
            del self._pending_log_lines[: len(lines)]
            for line in lines:
                level, message = self._parse_log_line(line)
                self._add_ui_log_line(level, message)

        if self._pending_batch_updates:
            if live_refresh_suspended or now - self._last_batch_snapshot_write < self._batch_snapshot_write_interval_seconds:
                for batch_id, (manager, status_override) in self._pending_batch_updates.items():
                    self.apply_batch_card_update(batch_id, manager, status_override)
            else:
                updates = dict(self._pending_batch_updates)
                self._pending_batch_updates.clear()
                for batch_id, (manager, status_override) in updates.items():
                    self.apply_batch_card_update(batch_id, manager, status_override)
                self._last_batch_snapshot_write = now

        if self._table_refresh_pending:
            if live_refresh_suspended or now - self._last_full_table_refresh < self._effective_table_refresh_interval_seconds():
                if self._pending_stats is not None:
                    self.update_stats(self._pending_stats, update_batch=False)
                elif self.active_running_batch_id == self.current_batch_id:
                    self.update_stats(self.manager.stats(), update_batch=False)
            else:
                self._table_refresh_pending = False
                self.refresh_table()
                self._last_full_table_refresh = now
                if self._pending_task is not None and hasattr(self, "stat_labels"):
                    self.stat_labels["pid"].setText(f"当前 PID: {self._pending_task.pid}")
                    self.stat_labels["row"].setText(f"当前行号: {self._pending_task.row_index}")
                stats = self._pending_stats or self.manager.stats()
                self._pending_stats = None
                self._pending_task = None
                self._stats_refresh_pending = False
                self.update_stats(stats, update_batch=False)
        elif self._stats_refresh_pending and self._pending_stats is not None:
            stats = self._pending_stats
            self._pending_stats = None
            self._stats_refresh_pending = False
            self.update_stats(stats, update_batch=False)

        if self._batch_refresh_pending:
            if live_refresh_suspended:
                return
            self._batch_refresh_pending = False
            self.refresh_batch_table()
            self.update_status_summary()

    def _effective_table_refresh_interval_seconds(self) -> float:
        task_count = len(getattr(self.manager, "tasks", []) or [])
        if task_count >= 1000:
            return max(self._table_refresh_interval_seconds, 8.0)
        if task_count >= 500:
            return max(self._table_refresh_interval_seconds, 5.0)
        return self._table_refresh_interval_seconds

    def update_status_summary(self) -> None:
        with self.ui_diag.measure("update_status_summary",
            current_batch_id=self.current_batch_id,
            active_running_batch_id=self.active_running_batch_id,
            active_threads=threading.active_count(),
        ):
            self._update_status_summary_impl()

    def _update_status_summary_impl(self) -> None:
        view_text = "当前查看：未选择批次"
        if self.current_batch:
            view_text = f"当前查看：{self.current_batch.batch_name} / {self.current_batch.batch_id}"
        running_text = "当前未执行任务"
        if self.active_running_batch_id and self.is_background_running():
            batch = getattr(self, "batch_card_batches", {}).get(self.active_running_batch_id)
            if batch is None and self.current_batch and self.current_batch.batch_id == self.active_running_batch_id:
                batch = self.current_batch
            status = batch.status if batch else (self.active_running_status_override or "RUNNING")
            running_text = f"当前执行：{(batch.batch_name if batch else self.active_running_batch_id)} / {self.active_running_batch_id} / {batch_status_text(status)}"
        if hasattr(self, "view_status_label"):
            self.view_status_label.setText(view_text)
        if hasattr(self, "running_status_label"):
            self.running_status_label.setText(running_text)
        if hasattr(self, "top_view_batch_label"):
            self.top_view_batch_label.setText(view_text)
        if hasattr(self, "top_running_batch_label"):
            self.top_running_batch_label.setText(running_text)
        self.update_run_queue_status()

    def current_batch_status_text(self, total: int = 0, completed: int = 0, failed: int = 0, timeout: int = 0, skipped: int = 0) -> str:
        if not self.current_batch:
            return "当前未选择批次"
        status = batch_status_text(self.current_batch.status)
        running = "当前未执行任务"
        if self.active_running_batch_id and self.is_background_running():
            running = "当前执行：" + (
                self.current_batch.batch_name if self.active_running_batch_id == self.current_batch.batch_id else self.active_running_batch_id
            )
        error_count = int(failed or 0) + int(timeout or 0)
        return (
            f"当前批次：{self.current_batch.batch_name}｜{status}｜"
            f"任务 {completed}/{total}｜失败 {error_count}｜跳过 {skipped}｜{running}"
        )

    def _install_style(self) -> None:
        self.setStyleSheet(
            """
            #appRoot {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #121212, stop:0.55 #171a22, stop:1 #1e1e1e);
            }
            QLabel {
                color: #eef2ff;
                font-size: 13px;
            }
            QLabel#appTitle {
                font-size: 23px;
                font-weight: 700;
                letter-spacing: 0px;
            }
            QLabel#sectionTitle {
                font-size: 20px;
                font-weight: 700;
            }
            QLabel#mutedLabel {
                color: #8f98aa;
                font-size: 12px;
            }
            QLabel#metricValue {
                font-size: 28px;
                font-weight: 800;
                color: #ffffff;
            }
            QLabel#metricCaption {
                color: #8f98aa;
                font-size: 12px;
            }
            QLabel#chipLabel {
                color: #cdd6e8;
                padding: 7px 10px;
                border-radius: 8px;
                background: rgba(255, 255, 255, 0.06);
            }
            QFrame#sidebar {
                background: rgba(255, 255, 255, 0.075);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 22px;
            }
            QFrame#card, QFrame#topBar, QFrame#controlCard, QFrame#batchPanel {
                background: rgba(255, 255, 255, 0.075);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 18px;
            }
            QFrame#controlCard {
                background: rgba(20, 24, 34, 0.88);
                border: 1px solid rgba(0, 122, 255, 0.34);
            }
            QLineEdit, QComboBox, QSpinBox {
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 10px;
                color: #f5f7fb;
                padding: 8px 10px;
                selection-background-color: #007aff;
            }
            QSpinBox::up-button, QSpinBox::down-button {
                width: 18px;
                border: none;
                background: transparent;
            }
            QPushButton {
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.13);
                border-radius: 12px;
                color: #f5f7fb;
                padding: 9px 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: rgba(255, 255, 255, 0.13);
            }
            QPushButton:disabled {
                color: #626b7d;
                background: rgba(255, 255, 255, 0.04);
                border-color: rgba(255, 255, 255, 0.07);
            }
            QPushButton#primaryButton {
                background: #007aff;
                border-color: #2491ff;
                color: #ffffff;
            }
            QPushButton#primaryButton:hover {
                background: #1688ff;
            }
            QPushButton#dangerButton {
                background: rgba(255, 69, 58, 0.16);
                border-color: rgba(255, 69, 58, 0.36);
                color: #ffb4ae;
            }
            QPushButton#navButton {
                text-align: left;
                padding: 12px 14px;
                border-radius: 12px;
                background: transparent;
                border: 1px solid transparent;
                color: #aeb7c8;
            }
            QPushButton#navButton[selected="true"] {
                color: #ffffff;
                background: rgba(0, 122, 255, 0.22);
                border: 1px solid rgba(0, 122, 255, 0.42);
            }
            QCheckBox {
                color: #e9eefc;
                spacing: 8px;
            }
            QScrollArea {
                background: transparent;
                border: none;
            }
            QScrollArea > QWidget > QWidget {
                background: transparent;
            }
            QTextEdit {
                background: rgba(8, 10, 14, 0.78);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 14px;
                color: #dfe7f7;
                padding: 10px;
                selection-background-color: #007aff;
            }
            QTableWidget {
                background: rgba(9, 12, 18, 0.72);
                alternate-background-color: rgba(255, 255, 255, 0.035);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 14px;
                color: #f3f6ff;
                gridline-color: rgba(255, 255, 255, 0.07);
                selection-background-color: rgba(0, 122, 255, 0.32);
                selection-color: #ffffff;
            }
            QHeaderView::section {
                background: rgba(255, 255, 255, 0.09);
                color: #c7d2e8;
                border: none;
                border-right: 1px solid rgba(255, 255, 255, 0.08);
                padding: 9px 8px;
                font-weight: 700;
            }
            QListWidget#batchCardList {
                background: transparent;
                border: none;
                padding: 2px;
                outline: none;
            }
            QListWidget#batchCardList::item {
                border: none;
                margin: 0;
                padding: 0;
            }
            QDialog#popupPanel {
                background: #25272d;
                border: 1px solid rgba(255, 255, 255, 0.16);
                border-radius: 12px;
            }
            """
        )

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        nav_width = int((getattr(self.config, "ui_layout", {}) or {}).get("nav_width_large", 72))
        sidebar.setMinimumWidth(nav_width)
        sidebar.setMaximumWidth(max(nav_width, 104))
        sidebar.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 16, 12, 14)
        layout.setSpacing(9)

        title = QLabel("Veo3\nStudio")
        title.setObjectName("appTitle")
        subtitle = QLabel("Batch Generator")
        subtitle.setObjectName("mutedLabel")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(12)

        self.nav_buttons: list[QPushButton] = []
        for index, text in enumerate(["控制台", "参数配置", "日志中心"]):
            btn = QPushButton(text)
            btn.setObjectName("navButton")
            btn.setProperty("selected", False)
            if index == 2:
                btn.clicked.connect(self.focus_log_panel)
            else:
                btn.clicked.connect(lambda checked=False, i=index: self._switch_page(i))
            self.nav_buttons.append(btn)
            layout.addWidget(btn)
        self.light_image_nav_btn = QPushButton("轻量图生图")
        self.light_image_nav_btn.setObjectName("navButton")
        self.light_image_nav_btn.setToolTip("打开独立的白底图 / 指定图生图批量生成工具")
        self.light_image_nav_btn.clicked.connect(self.open_light_image_tool)
        layout.addWidget(self.light_image_nav_btn)

        layout.addStretch(1)
        hint = QLabel("深色 Acrylic 风格\n状态实时写入本地")
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return sidebar

    def _switch_page(self, index: int) -> None:
        if hasattr(self, "pages"):
            self.pages.setCurrentIndex(index)
        for i, btn in enumerate(getattr(self, "nav_buttons", [])):
            btn.setProperty("selected", i == index)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def open_light_image_tool(self) -> None:
        try:
            from light_image_tool.window import LightImageToolWindow

            if self.light_image_tool_window is None:
                self.light_image_tool_window = LightImageToolWindow(self)
            self.light_image_tool_window.show()
            self.light_image_tool_window.raise_()
            self.light_image_tool_window.activateWindow()
            self.show_status("已打开轻量图生图工具", log=True)
        except Exception as exc:
            QMessageBox.warning(self, "轻量图生图工具", f"打开轻量图生图工具失败：{exc}")

    def _card(self, object_name: str = "card") -> QFrame:
        frame = QFrame()
        frame.setObjectName(object_name)
        return frame

    def _title_block(self, title: str, subtitle: str) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        title_label = QLabel(title)
        title_label.setObjectName("sectionTitle")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("mutedLabel")
        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)
        return box

    def _build_batch_list_panel(self) -> QFrame:
        panel = self._card("batchPanel")
        width = int((getattr(self.config, "ui_layout", {}) or {}).get("batch_list_width_large", 300))
        panel.setMinimumWidth(max(240, width - 40))
        panel.setMaximumWidth(width + 80)
        panel.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._title_block("任务批次", "双击卡片在右侧查看详情"))

        self.batch_search_edit = QLineEdit()
        self.batch_search_edit.setPlaceholderText("搜索批次名称 / ID")
        self.batch_search_edit.textChanged.connect(lambda *_: self.refresh_batch_table())

        quick_layout = QGridLayout()
        quick_layout.setHorizontalSpacing(5)
        quick_layout.setVerticalSpacing(5)
        self.batch_quick_filter = "全部"
        self.batch_filter_buttons: dict[str, QPushButton] = {}
        for index, name in enumerate(["全部", "运行中", "已完成", "失败", "已停止"]):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setChecked(name == "全部")
            btn.setMinimumWidth(0)
            btn.clicked.connect(lambda checked=False, n=name: self.set_batch_quick_filter(n))
            self.batch_filter_buttons[name] = btn
            quick_layout.addWidget(btn, index // 3, index % 3)

        self.batch_list = QListWidget()
        self.batch_list.setObjectName("batchCardList")
        self.batch_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.batch_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.batch_list.setSpacing(8)
        self.batch_list.setUniformItemSizes(False)
        self.batch_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.batch_list.itemClicked.connect(self.on_batch_card_clicked)
        self.batch_list.itemDoubleClicked.connect(lambda item: self.view_batch_by_id(str(item.data(Qt.UserRole) or "")))
        self.batch_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.batch_list.customContextMenuRequested.connect(self.show_batch_context_menu)

        bottom = QHBoxLayout()
        self.import_batch_list_btn = QPushButton("导入新批次")
        self.import_batch_list_btn.setObjectName("primaryButton")
        self.refresh_batch_list_btn = QPushButton("刷新")
        self.import_batch_list_btn.clicked.connect(lambda: self.load_tasks(self.import_batch_list_btn))
        self.refresh_batch_list_btn.clicked.connect(lambda: self.run_with_feedback("刷新批次列表", self.refresh_batch_table, self.refresh_batch_list_btn))
        bottom.addWidget(self.import_batch_list_btn, 1)
        bottom.addWidget(self.refresh_batch_list_btn)

        layout.addWidget(self.batch_search_edit)
        layout.addLayout(quick_layout)
        layout.addWidget(self.batch_list, 1)
        layout.addLayout(bottom)
        return panel

    def _build_top_config_bar(self) -> QFrame:
        box = self._card("topBar")
        grid = QGridLayout(box)
        grid.setContentsMargins(18, 14, 18, 14)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        self.excel_edit = QLineEdit(str(self.config.excel_path))
        self.output_edit = QLineEdit(str(self.config.software_log_root))
        for edit in [self.excel_edit, self.output_edit]:
            edit.setMinimumWidth(0)
            edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            edit.setToolTip(edit.text())
            edit.textChanged.connect(lambda text, e=edit: e.setToolTip(text))
        self.choose_excel_btn = QPushButton("选择 Excel")
        self.choose_excel_btn.setObjectName("primaryButton")
        self.choose_excel_btn.clicked.connect(self.choose_excel)
        self.choose_output_btn = QPushButton("日志目录")
        self.choose_output_btn.clicked.connect(self.choose_output)
        self.load_btn = QPushButton("导入新批次")
        self.load_btn.clicked.connect(lambda: self.load_tasks(self.load_btn))
        self.check_btn = QPushButton("检查任务")
        self.check_btn.clicked.connect(self.check_tasks)
        self.light_image_tool_btn = QPushButton("轻量图生图工具")
        self.light_image_tool_btn.setToolTip("打开独立轻量工具，不影响当前 Veo3 批次流程")
        self.light_image_tool_btn.clicked.connect(self.open_light_image_tool)
        self.top_view_batch_label = QLabel("当前查看：未选择批次")
        self.top_view_batch_label.setObjectName("chipLabel")
        self.top_running_batch_label = QLabel("当前未执行任务")
        self.top_running_batch_label.setObjectName("chipLabel")

        grid.addWidget(QLabel("任务 Excel"), 0, 0)
        grid.addWidget(self.excel_edit, 0, 1)
        grid.addWidget(self.choose_excel_btn, 0, 2)
        grid.addWidget(self.load_btn, 0, 3)
        grid.addWidget(QLabel("软件日志目录"), 1, 0)
        grid.addWidget(self.output_edit, 1, 1)
        grid.addWidget(self.choose_output_btn, 1, 2)
        grid.addWidget(self.check_btn, 1, 3)
        grid.addWidget(self.light_image_tool_btn, 0, 4, 2, 1)
        grid.addWidget(self.top_view_batch_label, 2, 0, 1, 2)
        grid.addWidget(self.top_running_batch_label, 2, 2, 1, 2)
        grid.setColumnStretch(1, 1)
        return box

    def _build_console_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._title_block("执行控制中枢", "控制、统计、任务队列、预览和日志在同一工作区"))
        layout.addWidget(self._build_current_batch_status_strip())
        layout.addWidget(self._build_metric_board())

        control_row = QWidget()
        control_layout = QHBoxLayout(control_row)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(10)
        control_row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        control_row.setMinimumHeight(144)
        control_layout.addWidget(self._build_primary_controls(), 1)
        control_layout.addWidget(self._build_secondary_actions(), 1)
        layout.addWidget(control_row)
        layout.addWidget(self._build_stats_strip())

        workspace_splitter = QSplitter(Qt.Vertical)
        self.workspace_splitter = workspace_splitter
        workspace_splitter.setChildrenCollapsible(False)

        queue_area = QWidget()
        queue_layout = QVBoxLayout(queue_area)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.setSpacing(8)
        queue_layout.addWidget(self._title_block("任务队列", "字段较多时可横向滚动查看"))
        queue_layout.addWidget(self._build_task_toolbar())
        table_splitter = QSplitter(Qt.Horizontal)
        self.table_splitter = table_splitter
        table_splitter.setChildrenCollapsible(False)
        table_splitter.addWidget(self._build_table())
        table_splitter.addWidget(self._build_preview_panel())
        table_splitter.setStretchFactor(0, 7)
        table_splitter.setStretchFactor(1, 3)
        queue_layout.addWidget(table_splitter, 1)

        workspace_splitter.addWidget(queue_area)
        workspace_splitter.addWidget(self._build_log_box())
        workspace_splitter.setStretchFactor(0, 5)
        workspace_splitter.setStretchFactor(1, 1)
        workspace_splitter.setSizes([760, 130])
        layout.addWidget(workspace_splitter, 1)
        return page

    def _build_current_batch_status_strip(self) -> QFrame:
        strip = self._card()
        strip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(12, 8, 12, 8)
        self.current_batch_status_label = QLabel("当前未执行任务")
        self.current_batch_status_label.setObjectName("chipLabel")
        self.current_batch_status_label.setWordWrap(False)
        self.current_batch_status_label.setMinimumWidth(0)
        self.current_batch_status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.current_batch_status_label, 1)
        self.run_queue_strip_label = QLabel("运行队列：0")
        self.run_queue_strip_label.setObjectName("chipLabel")
        self.run_queue_strip_label.setWordWrap(False)
        self.run_queue_strip_label.setMinimumWidth(120)
        layout.addWidget(self.run_queue_strip_label, 0)
        return strip

    def _build_metric_board(self) -> QWidget:
        board = QWidget()
        layout = QGridLayout(board)
        self.metric_board_layout = layout
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(12)
        self.metric_labels: dict[str, QLabel] = {}
        self.metric_cards: dict[str, QFrame] = {}
        metrics = [
            ("progress", "完成进度", "0%"),
            ("total", "任务总量", "0"),
            ("pending", "待处理", "0"),
            ("failed", "失败", "0"),
            ("concurrency", "实际并发", f"0/{self.config.concurrency}"),
        ]
        self.metric_order = [key for key, _, _ in metrics]
        for col, (key, caption, value) in enumerate(metrics):
            card = self._build_metric_card(key, caption, value)
            self.metric_cards[key] = card
            layout.addWidget(card, 0, col)
        return board

    def _build_metric_card(self, key: str, caption: str, value: str) -> QFrame:
        card = self._card()
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(5)
        value_label = QLabel(value)
        value_label.setObjectName("metricValue")
        caption_label = QLabel(caption)
        caption_label.setObjectName("metricCaption")
        layout.addWidget(value_label)
        layout.addWidget(caption_label)
        self.metric_labels[key] = value_label
        return card

    def _build_primary_controls(self) -> QFrame:
        box = self._card("controlCard")
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        box.setMinimumHeight(132)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)
        layout.addWidget(self._title_block("批次执行控制", "执行当前查看批次，后台运行不锁定查看"))

        buttons = QGridLayout()
        buttons.setHorizontalSpacing(8)
        buttons.setVerticalSpacing(7)
        self.start_btn = QPushButton("完整流程")
        self.start_btn.setToolTip("开始当前批次完整流程")
        self.start_btn.setObjectName("primaryButton")
        self.image_only_btn = QPushButton("仅生成图片")
        self.image_only_btn.setToolTip("开始当前批次仅生成图片")
        self.video_only_btn = QPushButton("图生视频")
        self.video_only_btn.setToolTip("开始当前批次图生视频")
        self.selected_video_btn = QPushButton("所选视频")
        self.selected_video_btn.setToolTip("开始所选任务图生视频")
        self.custom_poll_btn = QPushButton("继续轮询")
        self.custom_poll_btn.setToolTip("继续轮询当前批次")
        self.custom_poll_btn.setObjectName("primaryButton")
        self.manual_poll_btn = QPushButton("手动轮询")
        self.manual_poll_btn.setToolTip("手动轮询当前批次：立即检查所有已有 video_task_id 的未完成任务，不受最大轮询次数限制")
        self.manual_poll_btn.setObjectName("primaryButton")
        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setToolTip("暂停执行批次")
        self.resume_btn = QPushButton("继续")
        self.resume_btn.setToolTip("继续执行批次")
        control_buttons = [
            self.start_btn,
            self.image_only_btn,
            self.video_only_btn,
            self.selected_video_btn,
            self.custom_poll_btn,
            self.manual_poll_btn,
            self.pause_btn,
            self.resume_btn,
        ]
        for index, btn in enumerate(control_buttons):
            btn.setMinimumHeight(30)
            btn.setMaximumHeight(34)
            btn.setMinimumWidth(0)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            buttons.addWidget(btn, index // 4, index % 4)
        layout.addLayout(buttons)

        self.start_btn.clicked.connect(lambda: self.start_batch_by_id(self.current_batch_id, False, False, self.start_btn))
        self.image_only_btn.clicked.connect(self.start_image_only_current_batch)
        self.video_only_btn.clicked.connect(self.start_video_only_current_batch)
        self.selected_video_btn.clicked.connect(self.start_selected_video_tasks)
        self.custom_poll_btn.clicked.connect(lambda: self.custom_poll(self.custom_poll_btn))
        self.manual_poll_btn.clicked.connect(lambda: self.manual_poll_batch(self.current_batch_id, self.manual_poll_btn))
        self.pause_btn.clicked.connect(self.pause_worker)
        self.resume_btn.clicked.connect(self.resume_worker)
        return box

    def _build_secondary_actions(self) -> QFrame:
        box = self._card()
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        box.setMinimumHeight(132)
        layout = QGridLayout(box)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(7)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setToolTip("停止执行批次")
        self.stop_btn.setObjectName("dangerButton")
        self.retry_failed_btn = QPushButton("重试失败")
        self.switch_api_btn = QPushButton("切换API")
        self.switch_api_btn.setToolTip("切换当前批次后续节点使用的图生图/图生视频 API")
        self.export_btn = QPushButton("导出结果")
        self.export_btn.setToolTip("导出当前批次结果")
        self.download_video_btn = QPushButton("下载视频")
        self.download_video_btn.setToolTip("下载选中任务视频")
        self.download_images_btn = QPushButton("下载图片")
        self.download_images_btn.setToolTip("下载选中任务图片资料")
        self.batch_download_video_btn = QPushButton("下载全部")
        self.batch_download_video_btn.setToolTip("下载全部视频")
        self.open_output_btn = QPushButton("打开目录")
        self.open_output_btn.setToolTip("打开当前批次目录")
        self.stop_btn.setMinimumHeight(30)
        self.stop_btn.setMaximumHeight(34)
        self.stop_btn.setMinimumWidth(0)
        self.stop_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.stop_btn, 0, 0, 1, 4)
        buttons = [
            self.retry_failed_btn,
            self.switch_api_btn,
            self.export_btn,
            self.download_video_btn,
            self.download_images_btn,
            self.batch_download_video_btn,
            self.open_output_btn,
        ]
        for index, btn in enumerate(buttons):
            btn.setMinimumHeight(30)
            btn.setMaximumHeight(34)
            btn.setMinimumWidth(0)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            layout.addWidget(btn, 1 + index // 4, index % 4)
        self.retry_failed_btn.clicked.connect(lambda: self.retry_failed_current_batch(self.retry_failed_btn))
        self.switch_api_btn.clicked.connect(lambda: self.switch_batch_api_by_id(self.current_batch_id, self.switch_api_btn))
        self.stop_btn.clicked.connect(self.confirm_stop_worker)
        self.export_btn.clicked.connect(self.export_results)
        self.download_video_btn.clicked.connect(self.download_selected_video)
        self.download_images_btn.clicked.connect(self.download_selected_images)
        self.batch_download_video_btn.clicked.connect(self.batch_download_videos)
        self.open_output_btn.clicked.connect(self.open_current_batch_dir)
        self._set_running_buttons(False)
        return box

    def _build_task_toolbar(self) -> QFrame:
        box = self._card()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(8)
        self.group_enabled_check = QCheckBox("启用分组")
        self.group_enabled_check.setChecked(self.config.enable_group_view)
        self.group_enabled_check.hide()
        self.group_enabled_check.stateChanged.connect(lambda *_: self.refresh_table())

        self.add_task_btn = QPushButton("+ 添加任务")
        self.refresh_task_btn = QPushButton("刷新")
        self.field_visibility_btn = QPushButton("字段配置")
        self.filter_popup_btn = QPushButton("筛选")
        self.group_popup_btn = QPushButton("分组")
        self.sort_popup_btn = QPushButton("排序")
        self.row_height_btn = QPushButton("行高")
        self.detail_panel_btn = QPushButton("详情")
        self.toolbar_export_btn = QPushButton("导出")
        self.more_task_btn = QPushButton("更多")
        self.filter_result_label = QLabel("显示: 0/0")
        self.filter_result_label.setObjectName("chipLabel")
        self.active_filter_label = QLabel("筛选: 无")
        self.active_filter_label.setObjectName("chipLabel")
        self.group_label = QLabel("分组: 无")
        self.group_label.setObjectName("chipLabel")

        self.add_task_btn.clicked.connect(lambda: self.load_tasks(self.add_task_btn))
        self.refresh_task_btn.clicked.connect(lambda: self.run_with_feedback("刷新任务队列", self.refresh_table, self.refresh_task_btn))
        self.field_visibility_btn.clicked.connect(self.open_column_visibility_dialog)
        self.filter_popup_btn.clicked.connect(lambda: self.show_filter_popup(self.filter_popup_btn))
        self.group_popup_btn.clicked.connect(lambda: self.show_group_popup(self.group_popup_btn))
        self.sort_popup_btn.clicked.connect(lambda: self.show_sort_popup(self.sort_popup_btn))
        self.row_height_btn.clicked.connect(lambda: self.show_row_height_popup(self.row_height_btn))
        self.detail_panel_btn.clicked.connect(self.toggle_detail_panel)
        self.toolbar_export_btn.clicked.connect(self.export_results)
        self.more_task_btn.clicked.connect(lambda: self.show_status("更多操作请使用任务行右键菜单", log=True))

        for button in [
            self.add_task_btn,
            self.refresh_task_btn,
            self.field_visibility_btn,
            self.filter_popup_btn,
            self.group_popup_btn,
            self.sort_popup_btn,
            self.row_height_btn,
            self.detail_panel_btn,
            self.toolbar_export_btn,
            self.more_task_btn,
        ]:
            button.setMinimumWidth(0)
            layout.addWidget(button)
        layout.addWidget(self.filter_result_label)
        layout.addWidget(self.active_filter_label)
        layout.addWidget(self.group_label)
        layout.addWidget(self.group_enabled_check)
        layout.addStretch(1)
        return box

    def _build_queue_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._title_block("任务队列", "按 PID、状态和链接追踪生成进度"))
        layout.addWidget(self._build_stats_strip())
        layout.addWidget(self._build_filter_box())
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_table())
        splitter.addWidget(self._build_preview_panel())
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)
        return page

    def _build_stats_strip(self) -> QFrame:
        box = self._card()
        self.stats_strip = box
        layout = QGridLayout(box)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        self.stat_labels = {}
        items = [
            ("batch_name", "当前批次"),
            ("batch_id", "批次ID"),
            ("imported_at", "导入时间"),
            ("total", "总任务数"),
            ("pending", "待提交"),
            ("image_running", "图片生成中"),
            ("submitted", "视频已提交"),
            ("polling", "视频轮询中"),
            ("completed", "已完成"),
            ("failed", "失败"),
            ("timeout", "超时"),
            ("skipped", "已跳过"),
            ("progress", "完成百分比"),
            ("success_rate", "成功率"),
            ("pid", "当前 PID"),
            ("row", "当前行号"),
        ]
        for index, (key, text) in enumerate(items):
            label = QLabel(f"{text}: 0")
            label.setObjectName("chipLabel")
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.stat_labels[key] = label
            layout.addWidget(label, index // 4, index % 4)
        return box

    def _build_filter_box(self) -> QFrame:
        box = self._card()
        layout = QGridLayout(box)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setHorizontalSpacing(9)
        layout.setVerticalSpacing(8)
        self.filter_column_combo = self._setup_combo_box(QComboBox(), 280, 14)
        self.filter_column_combo.addItem("全部列", -1)
        for index, header in enumerate(TABLE_HEADERS):
            self.filter_column_combo.addItem(header, index)
        self.filter_text_edit = QLineEdit()
        self.filter_text_edit.setPlaceholderText("输入筛选内容，支持部分匹配")

        self.multi_filter_field_combo = self._setup_combo_box(QComboBox(), 260, 14)
        self.multi_filter_field_combo.addItems(FILTER_FIELDS)
        self.multi_filter_value_combo = self._setup_combo_box(QComboBox(), 320, 14)
        self.multi_filter_value_combo.setEditable(True)
        self.multi_filter_field_combo.currentTextChanged.connect(self.refresh_filter_value_options)
        self.add_filter_btn = QPushButton("添加条件")
        self.add_filter_btn.clicked.connect(self.add_active_filter)
        self.save_filter_btn = QPushButton("保存筛选方案")
        self.load_filter_btn = QPushButton("加载筛选方案")
        self.save_filter_btn.clicked.connect(self.save_filter_scheme)
        self.load_filter_btn.clicked.connect(self.load_filter_scheme)

        self.group_field_combo = self._setup_combo_box(QComboBox(), 260, 14)
        self.group_field_combo.addItems(GROUP_FIELDS)
        self._refresh_combo_box_display(self.filter_column_combo)
        self._refresh_combo_box_display(self.multi_filter_field_combo)
        self._refresh_combo_box_display(self.group_field_combo)
        self.add_group_btn = QPushButton("添加分组")
        self.remove_group_btn = QPushButton("移除分组")
        self.clear_group_btn = QPushButton("清空分组")
        self.expand_all_groups_btn = QPushButton("全部展开")
        self.collapse_all_groups_btn = QPushButton("全部折叠")
        self.group_enabled_check = QCheckBox("启用分组")
        self.group_enabled_check.setChecked(self.config.enable_group_view)
        self.group_label = QLabel("分组: 无")
        self.group_label.setObjectName("chipLabel")
        self.add_group_btn.clicked.connect(self.add_group_field)
        self.remove_group_btn.clicked.connect(self.remove_group_field)
        self.clear_group_btn.clicked.connect(self.clear_group_fields)
        self.expand_all_groups_btn.clicked.connect(self.expand_all_groups)
        self.collapse_all_groups_btn.clicked.connect(self.collapse_all_groups)
        self.group_enabled_check.stateChanged.connect(lambda *_: self.refresh_table())

        self.field_visibility_btn = QPushButton("字段显示设置")
        self.field_visibility_btn.clicked.connect(self.open_column_visibility_dialog)
        self.apply_filter_btn = QPushButton("筛选")
        self.apply_filter_btn.setObjectName("primaryButton")
        self.clear_filter_btn = QPushButton("清除")
        self.filter_result_label = QLabel("显示: 0/0")
        self.filter_result_label.setObjectName("chipLabel")
        self.apply_filter_btn.clicked.connect(self.apply_table_filter)
        self.clear_filter_btn.clicked.connect(self.clear_table_filter)
        self.filter_text_edit.returnPressed.connect(self.apply_table_filter)
        self.active_filter_label = QLabel("筛选: 无")
        self.active_filter_label.setObjectName("chipLabel")

        layout.addWidget(QLabel("列"), 0, 0)
        layout.addWidget(self.filter_column_combo, 0, 1)
        layout.addWidget(QLabel("内容"), 0, 2)
        layout.addWidget(self.filter_text_edit, 0, 3)
        layout.addWidget(self.apply_filter_btn, 0, 4)
        layout.addWidget(self.clear_filter_btn, 0, 5)
        layout.addWidget(self.filter_result_label, 0, 6)
        layout.addWidget(QLabel("筛选字段"), 1, 0)
        layout.addWidget(self.multi_filter_field_combo, 1, 1)
        layout.addWidget(QLabel("筛选选项"), 1, 2)
        layout.addWidget(self.multi_filter_value_combo, 1, 3)
        layout.addWidget(self.add_filter_btn, 1, 4)
        layout.addWidget(self.save_filter_btn, 1, 5)
        layout.addWidget(self.load_filter_btn, 1, 6)
        layout.addWidget(self.active_filter_label, 2, 0, 1, 4)
        layout.addWidget(self.group_enabled_check, 2, 4)
        layout.addWidget(self.field_visibility_btn, 2, 5)
        layout.addWidget(QLabel("分组字段"), 3, 0)
        layout.addWidget(self.group_field_combo, 3, 1)
        layout.addWidget(self.add_group_btn, 3, 2)
        layout.addWidget(self.remove_group_btn, 3, 3)
        layout.addWidget(self.clear_group_btn, 3, 4)
        layout.addWidget(self.expand_all_groups_btn, 3, 5)
        layout.addWidget(self.collapse_all_groups_btn, 3, 6)
        layout.addWidget(self.group_label, 4, 0, 1, 7)
        layout.setColumnStretch(3, 1)
        self.refresh_filter_value_options()
        self.update_filter_group_labels()
        return box

    def _build_log_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._title_block("运行日志", "轮询、提交、下载和错误信息实时输出"))
        layout.addWidget(self._build_log_box(), 1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._title_block("参数配置", "平台、模型、路径、并发、轮询和下载规则均可保存到本地配置"))

        box = self._card()
        form = QFormLayout(box)
        form.setContentsMargins(18, 18, 18, 18)
        form.setSpacing(12)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.default_excel_edit = QLineEdit(str(self.config.excel_path))
        self.image_doc_edit = QLineEdit(str(self.config.image_doc_path))
        self.video_doc_edit = QLineEdit(str(self.config.video_doc_path))
        self.video_download_root_edit = QLineEdit(str(self.config.video_download_root))
        self.image_assets_root_edit = QLineEdit(str(self.config.image_assets_root))
        self.software_log_root_edit = QLineEdit(str(self.config.software_log_root))
        self.netdisk_local_prefix_edit = QLineEdit(str(self.config.netdisk_local_prefix))
        self.netdisk_http_prefix_edit = QLineEdit(str(self.config.netdisk_http_prefix))
        self.image_api_key_edit = QLineEdit(self.config.image_api_key)
        self.video_api_key_edit = QLineEdit(self.config.video_api_key)
        self.image_api_key_edit.setEchoMode(QLineEdit.Password)
        self.video_api_key_edit.setEchoMode(QLineEdit.Password)
        self.image_base_url_edit = QLineEdit(self.config.image_api_base_url)
        self.video_base_url_edit = QLineEdit(self.config.video_api_base_url)
        self.image_provider_combo = self._setup_combo_box(QComboBox(), 280, 16)
        self.image_model_combo = self._setup_combo_box(QComboBox(), 260, 14)
        self.video_provider_combo = self._setup_combo_box(QComboBox(), 280, 16)
        self.video_model_combo = self._setup_combo_box(QComboBox(), 300, 14)
        for edit in [
            self.default_excel_edit,
            self.image_doc_edit,
            self.video_doc_edit,
            self.video_download_root_edit,
            self.image_assets_root_edit,
            self.software_log_root_edit,
            self.netdisk_local_prefix_edit,
            self.netdisk_http_prefix_edit,
            self.image_api_key_edit,
            self.video_api_key_edit,
            self.image_base_url_edit,
            self.video_base_url_edit,
        ]:
            edit.setMinimumWidth(0)
            edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 100)
        self.concurrency_spin.setValue(self.config.concurrency)
        self.image_concurrency_spin = QSpinBox()
        self.image_concurrency_spin.setRange(1, 100)
        self.image_concurrency_spin.setValue(self.config.image_concurrency)
        self.video_submit_concurrency_spin = QSpinBox()
        self.video_submit_concurrency_spin.setRange(1, 100)
        self.video_submit_concurrency_spin.setValue(self.config.video_submit_concurrency)
        self.poll_concurrency_spin = QSpinBox()
        self.poll_concurrency_spin.setRange(1, 100)
        self.poll_concurrency_spin.setValue(self.config.poll_concurrency)
        self.download_concurrency_spin = QSpinBox()
        self.download_concurrency_spin.setRange(1, 50)
        self.download_concurrency_spin.setValue(self.config.download_concurrency)
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(0, 10)
        self.retry_spin.setValue(self.config.retry_count)
        self.retry_interval_spin = QSpinBox()
        self.retry_interval_spin.setRange(1, 120)
        self.retry_interval_spin.setValue(self.config.retry_interval_seconds)
        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(1, 60)
        self.poll_spin.setValue(self.config.poll_interval_seconds)
        self.max_poll_spin = QSpinBox()
        self.max_poll_spin.setRange(1, 1000)
        self.max_poll_spin.setValue(self.config.max_poll_count)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 600)
        self.timeout_spin.setValue(self.config.request_timeout_seconds)
        self.regenerate_image_check = QCheckBox("重新生成已有图片")
        self.regenerate_image_check.setChecked(self.config.regenerate_existing_images)
        self.auto_download_video_check = QCheckBox("视频完成后自动下载")
        self.auto_download_video_check.setChecked(self.config.auto_download_video)
        self.auto_save_image_assets_check = QCheckBox("图生图完成后自动保存图片资料")
        self.auto_save_image_assets_check.setChecked(self.config.auto_save_image_assets)
        self.group_by_owner_check = QCheckBox("下载时按负责人分目录")
        self.group_by_owner_check.setChecked(self.config.group_by_owner)
        self.restore_tasks_check = QCheckBox("启动时恢复上次任务")
        self.restore_tasks_check.setChecked(self.config.restore_last_tasks_on_startup and self.config.auto_load_last_batch_on_startup)
        self.enable_netdisk_http_mapping_check = QCheckBox("启用网盘路径转 Http")
        self.enable_netdisk_http_mapping_check.setChecked(self.config.enable_netdisk_http_mapping)
        self.enable_manual_poll_check = QCheckBox("启用手动立即轮询按钮")
        self.enable_manual_poll_check.setChecked(self.config.enable_manual_poll_button)
        self.manual_poll_ignore_max_check = QCheckBox("手动轮询不受最大轮询次数限制")
        self.manual_poll_ignore_max_check.setChecked(self.config.manual_poll_ignore_max_count)
        self.manual_poll_include_timeout_check = QCheckBox("手动轮询包含超时任务")
        self.manual_poll_include_timeout_check.setChecked(self.config.manual_poll_include_timeout_tasks)
        self.manual_poll_include_failed_check = QCheckBox("手动轮询包含失败但仍有 task_id 的任务")
        self.manual_poll_include_failed_check.setChecked(self.config.manual_poll_include_failed_tasks)
        self.auto_retry_workflow_check = QCheckBox("失败流程默认自动重试")
        self.auto_retry_workflow_check.setChecked(self.config.auto_retry_failed_workflow_enabled)
        self.auto_retry_image_check = QCheckBox("图生图失败自动重试")
        self.auto_retry_image_check.setChecked(self.config.auto_retry_image_nodes)
        self.auto_retry_video_check = QCheckBox("图生视频失败自动重试")
        self.auto_retry_video_check.setChecked(self.config.auto_retry_video_nodes)
        self.auto_retry_download_check = QCheckBox("视频下载失败自动重试/刷新链接")
        self.auto_retry_download_check.setChecked(self.config.auto_retry_video_download)
        self.export_visible_only_check = QCheckBox("导出时仅导出当前显示字段")
        self.export_visible_only_check.setChecked(self.config.export_only_visible_columns)
        self.table_text_color_edit = QLineEdit(self.config.table_text_color)

        form.addRow("任务 Excel 默认路径", self._file_row(self.default_excel_edit, "选择 Excel", "Excel 文件 (*.xlsx *.xls)"))
        form.addRow("图生图 API 平台", self.image_provider_combo)
        form.addRow("图生图模型", self.image_model_combo)
        form.addRow("图生图 API Key", self.image_api_key_edit)
        form.addRow("图生图 API Base URL", self.image_base_url_edit)
        form.addRow("图生视频 API 平台", self.video_provider_combo)
        form.addRow("图生视频模型", self.video_model_combo)
        form.addRow("图生视频 API Key", self.video_api_key_edit)
        form.addRow("图生视频 API Base URL", self.video_base_url_edit)
        form.addRow("视频默认下载根目录", self._directory_row(self.video_download_root_edit, "选择视频下载目录"))
        form.addRow("图片任务资料根目录", self._directory_row(self.image_assets_root_edit, "选择图片任务资料目录"))
        form.addRow("软件日志根目录", self._directory_row(self.software_log_root_edit, "选择软件日志目录"))
        form.addRow("网盘路径本地前缀", self.netdisk_local_prefix_edit)
        form.addRow("网盘路径 Http 前缀", self.netdisk_http_prefix_edit)
        form.addRow("总并发数量", self.concurrency_spin)
        form.addRow("图生图并发数量", self.image_concurrency_spin)
        form.addRow("视频提交并发数量", self.video_submit_concurrency_spin)
        form.addRow("视频轮询并发数量", self.poll_concurrency_spin)
        form.addRow("下载并发数量", self.download_concurrency_spin)
        form.addRow("失败重试次数", self.retry_spin)
        form.addRow("失败重试间隔秒数", self.retry_interval_spin)
        form.addRow("轮询间隔秒数", self.poll_spin)
        form.addRow("最大轮询次数", self.max_poll_spin)
        form.addRow("请求超时时间秒数", self.timeout_spin)
        form.addRow("表格字体颜色", self.table_text_color_edit)
        form.addRow("", self.regenerate_image_check)
        form.addRow("", self.auto_download_video_check)
        form.addRow("", self.auto_save_image_assets_check)
        form.addRow("", self.group_by_owner_check)
        form.addRow("", self.restore_tasks_check)
        form.addRow("", self.enable_netdisk_http_mapping_check)
        form.addRow("", self.enable_manual_poll_check)
        form.addRow("", self.manual_poll_ignore_max_check)
        form.addRow("", self.manual_poll_include_timeout_check)
        form.addRow("", self.manual_poll_include_failed_check)
        form.addRow("", self.auto_retry_workflow_check)
        form.addRow("", self.auto_retry_image_check)
        form.addRow("", self.auto_retry_video_check)
        form.addRow("", self.auto_retry_download_check)
        form.addRow("", self.export_visible_only_check)
        api_configured = all(
            [
                self.config.image_provider,
                self.config.image_model_logical_key,
                self.config.image_api_key,
                self.config.video_provider,
                self.config.video_model_logical_key,
                self.config.video_api_key,
            ]
        )
        self.show_api_docs_check = QCheckBox("显示高级设置 / API 文档解析")
        self.show_api_docs_check.setChecked(bool(self.config.show_api_doc_paths_in_main_settings or not api_configured))
        form.addRow("", self.show_api_docs_check)
        advanced = QGroupBox("高级设置 / API 文档解析")
        self.api_docs_advanced_group = advanced
        advanced_layout = QFormLayout(advanced)
        advanced_layout.setContentsMargins(12, 12, 12, 12)
        advanced_layout.addRow("图生图 API 配置文档路径", self._file_row(self.image_doc_edit, "选择图生图文档", "文档 (*.md *.txt *.pdf);;所有文件 (*.*)"))
        advanced_layout.addRow("图生视频 API 配置文档路径", self._file_row(self.video_doc_edit, "选择图生视频文档", "文档 (*.md *.txt *.pdf);;所有文件 (*.*)"))
        self.reparse_api_docs_btn = QPushButton("重新解析 API 文档")
        self.reparse_api_docs_btn.clicked.connect(lambda: self.append_log("INFO", "API 文档解析入口已保留，当前平台模型由 Provider Registry 管理"))
        advanced_layout.addRow("", self.reparse_api_docs_btn)
        advanced.setVisible(self.show_api_docs_check.isChecked())
        self.show_api_docs_check.stateChanged.connect(lambda *_: advanced.setVisible(self.show_api_docs_check.isChecked()))
        form.addRow("", advanced)
        self.save_settings_btn = QPushButton("保存设置")
        self.save_settings_btn.setObjectName("primaryButton")
        self.save_settings_btn.clicked.connect(self.save_settings)
        form.addRow("", self.save_settings_btn)
        box.setMinimumWidth(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setMinimumSize(0, 0)
        scroll.setWidget(box)
        layout.addWidget(scroll, 1)
        self._populate_provider_combos()
        return page

    def _directory_row(self, edit: QLineEdit, title: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        btn = QPushButton("选择")
        btn.clicked.connect(lambda: self.choose_directory_for_edit(edit, title))
        layout.addWidget(edit, 1)
        layout.addWidget(btn)
        return row

    def _file_row(self, edit: QLineEdit, title: str, file_filter: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        btn = QPushButton("选择")
        btn.clicked.connect(lambda: self.choose_file_for_edit(edit, title, file_filter))
        layout.addWidget(edit, 1)
        layout.addWidget(btn)
        return row

    def choose_directory_for_edit(self, edit: QLineEdit, title: str) -> None:
        path = QFileDialog.getExistingDirectory(self, title, edit.text() or str(Path.home()))
        if path:
            edit.setText(path)

    def choose_file_for_edit(self, edit: QLineEdit, title: str, file_filter: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, title, edit.text() or str(Path.home()), file_filter)
        if path:
            edit.setText(path)

    def _populate_provider_combos(self) -> None:
        self.image_provider_combo.blockSignals(True)
        self.video_provider_combo.blockSignals(True)
        self.image_provider_combo.clear()
        self.video_provider_combo.clear()
        for key, name in provider_display_items(IMAGE_PROVIDERS):
            self.image_provider_combo.addItem(name, key)
        for key, name in provider_display_items(VIDEO_PROVIDERS):
            self.video_provider_combo.addItem(name, key)
        self._set_combo_value(self.image_provider_combo, self.config.image_provider)
        self._set_combo_value(self.video_provider_combo, self.config.video_provider)
        self._refresh_combo_box_display(self.image_provider_combo)
        self._refresh_combo_box_display(self.video_provider_combo)
        self.image_provider_combo.blockSignals(False)
        self.video_provider_combo.blockSignals(False)
        self.image_provider_combo.currentIndexChanged.connect(self._refresh_image_models)
        self.video_provider_combo.currentIndexChanged.connect(self._refresh_video_models)
        self._refresh_image_models()
        self._refresh_video_models()

    def _refresh_image_models(self) -> None:
        provider_key = str(self.image_provider_combo.currentData() or self.config.image_provider)
        provider = get_image_provider(provider_key)
        profile = get_api_profile(self.config, "image", provider_key)
        selected_model = str(profile.get("last_model_logical_key") or self.config.image_model_logical_key)
        self.image_model_combo.blockSignals(True)
        self.image_model_combo.clear()
        for key, name in model_display_items(provider):
            self.image_model_combo.addItem(name, key)
        self._set_combo_value(self.image_model_combo, selected_model)
        self._refresh_combo_box_display(self.image_model_combo)
        self.image_model_combo.blockSignals(False)
        self._fill_api_profile_fields("image", provider_key)

    def _refresh_video_models(self) -> None:
        provider_key = str(self.video_provider_combo.currentData() or self.config.video_provider)
        provider = get_video_provider(provider_key)
        profile = get_api_profile(self.config, "video", provider_key)
        selected_model = str(profile.get("last_model_logical_key") or self.config.video_model_logical_key)
        self.video_model_combo.blockSignals(True)
        self.video_model_combo.clear()
        for key, name in model_display_items(provider):
            self.video_model_combo.addItem(name, key)
        self._set_combo_value(self.video_model_combo, selected_model)
        self._refresh_combo_box_display(self.video_model_combo)
        self.video_model_combo.blockSignals(False)
        self._fill_api_profile_fields("video", provider_key)

    def _fill_api_profile_fields(self, kind: str, provider_key: str) -> None:
        profile = get_api_profile(self.config, kind, provider_key)
        if kind == "image" and hasattr(self, "image_api_key_edit"):
            self.image_api_key_edit.setText(str(profile.get("api_key") or ""))
            if hasattr(self, "image_base_url_edit") and profile.get("base_url"):
                self.image_base_url_edit.setText(str(profile.get("base_url") or ""))
            if not profile.get("api_key"):
                self.show_status("这个图生图平台还没有保存过 API Key，输入一次并保存后会自动记住。", timeout_ms=5000)
        elif kind == "video" and hasattr(self, "video_api_key_edit"):
            self.video_api_key_edit.setText(str(profile.get("api_key") or ""))
            if hasattr(self, "video_base_url_edit") and profile.get("base_url"):
                self.video_base_url_edit.setText(str(profile.get("base_url") or ""))
            if not profile.get("api_key"):
                self.show_status("这个图生视频平台还没有保存过 API Key，输入一次并保存后会自动记住。", timeout_ms=5000)

    def _remember_api_profiles_from_config(self) -> None:
        update_api_profile(
            self.config,
            "image",
            self.config.image_provider,
            api_key=self.config.image_api_key,
            last_model_logical_key=self.config.image_model_logical_key,
            base_url=self.config.image_api_base_url,
        )
        update_api_profile(
            self.config,
            "video",
            self.config.video_provider,
            api_key=self.config.video_api_key,
            last_model_logical_key=self.config.video_model_logical_key,
            base_url=self.config.video_api_base_url,
        )

    @staticmethod
    def _set_combo_value(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif combo.count():
            combo.setCurrentIndex(0)

    def _build_table(self) -> QTableWidget:
        self.table = QTableWidget(0, len(TABLE_HEADERS))
        self.table.setObjectName("taskTable")
        self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setMinimumSectionSize(54)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn if self.config.table_horizontal_scrollbar_always_on else Qt.ScrollBarAsNeeded)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setSizeAdjustPolicy(QAbstractScrollArea.AdjustIgnored)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setMinimumSize(0, 0)
        self.table.setMinimumHeight(300)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setColumnWidth(0, 58)
        self.table.setColumnWidth(1, 105)
        self.table.setColumnWidth(COL_INDEX["负责人"], 90)
        self.table.setColumnWidth(COL_INDEX["网盘路径"], 210)
        self.table.setColumnWidth(COL_INDEX["Http路径"], 210)
        self.table.setColumnWidth(COL_INDEX["产品白底图路径"], 190)
        self.table.setColumnWidth(COL_INDEX["产品白底图URL"], 190)
        self.table.setColumnWidth(COL_INDEX["图片提示词"], 180)
        self.table.setColumnWidth(COL_INDEX["视频提示词"], 180)
        self.table.setColumnWidth(COL_INDEX["手动轮询次数"], 110)
        self.table.setColumnWidth(COL_INDEX["最后手动轮询时间"], 160)
        self.table.setColumnWidth(COL_INDEX["最后手动轮询结果"], 140)
        self.table.setColumnWidth(COL_INDEX["生成图片路径"], 190)
        self.table.setColumnWidth(COL_INDEX["生成图片URL"], 190)
        self.table.setColumnWidth(COL_INDEX["视频链接"], 190)
        self.table.setColumnWidth(COL_INDEX["视频本地路径"], 190)
        self.table.setColumnWidth(COL_INDEX["视频下载状态"], 130)
        self.table.setColumnWidth(COL_INDEX["视频下载尝试次数"], 120)
        self.table.setColumnWidth(COL_INDEX["最后视频下载错误"], 260)
        self.restore_table_column_widths()
        self.table.horizontalHeader().sectionResized.connect(self.on_table_column_resized)
        self.table.cellClicked.connect(self.on_cell_clicked)
        self.table.cellDoubleClicked.connect(self.on_cell_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_task_context_menu)
        self.apply_column_visibility()
        return self.table

    def _build_preview_panel(self) -> QFrame:
        panel = self._card()
        self.preview_panel = panel
        panel.setMinimumWidth(180)
        panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(self._title_block("任务详情 / 预览", "选择任务后查看图片、视频和错误详情"))
        self.preview_tabs = QTabWidget()
        preview_page = QWidget()
        preview_layout = QVBoxLayout(preview_page)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(10)
        self.preview_label = QLabel("选择任务后查看图片、视频和错误详情")
        self.preview_label.setObjectName("mutedLabel")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(140)
        self.preview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet("border: 1px solid rgba(255,255,255,0.10); border-radius: 12px; background: rgba(0,0,0,0.20);")
        preview_layout.addWidget(self.preview_label, 1)

        self.preview_info_label = QLabel("")
        self.preview_info_label.setObjectName("mutedLabel")
        self.preview_info_label.setWordWrap(True)
        preview_layout.addWidget(self.preview_info_label)

        if HAS_MEDIA_PLAYER:
            self.video_widget = QVideoWidget()
            self.video_widget.setMinimumHeight(100)
            self.video_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.video_widget.setStyleSheet("background: #000000; border: 1px solid rgba(255,255,255,0.10); border-radius: 12px;")
            if hasattr(self.video_widget, "setAspectRatioMode"):
                self.video_widget.setAspectRatioMode(Qt.KeepAspectRatio)
            self.video_widget.hide()
            self.media_player = QMediaPlayer(self)
            self.audio_output = QAudioOutput(self)
            self.media_player.setAudioOutput(self.audio_output)
            self.media_player.setVideoOutput(self.video_widget)
            preview_layout.addWidget(self.video_widget, 1)
        else:
            self.video_widget = None
            self.media_player = None
            self.audio_output = None

        buttons = QHBoxLayout()
        self.open_preview_btn = QPushButton("打开")
        self.open_preview_folder_btn = QPushButton("打开所在文件夹")
        self.copy_preview_link_btn = QPushButton("复制链接")
        self.open_preview_btn.clicked.connect(self.open_current_preview)
        self.open_preview_folder_btn.clicked.connect(self.open_current_preview_folder)
        self.copy_preview_link_btn.clicked.connect(self.copy_current_preview_link)
        for btn in [self.open_preview_btn, self.open_preview_folder_btn, self.copy_preview_link_btn]:
            buttons.addWidget(btn)
        preview_layout.addLayout(buttons)
        self.preview_tabs.addTab(preview_page, "预览")

        task_log_page = QWidget()
        task_log_layout = QVBoxLayout(task_log_page)
        task_log_layout.setContentsMargins(0, 0, 0, 0)
        task_log_layout.setSpacing(8)
        self.task_log_preview_text = QTextEdit()
        self.task_log_preview_text.setReadOnly(True)
        self.task_log_preview_text.setFont(QFont("Consolas", 10))
        self.task_log_preview_text.setPlainText("选择任务后查看该任务日志。")
        task_log_layout.addWidget(self.task_log_preview_text, 1)
        self.preview_tabs.addTab(task_log_page, "任务日志")
        layout.addWidget(self.preview_tabs, 1)
        self.current_preview_value = ""
        self.current_preview_kind = ""
        return panel

    def _build_log_box(self) -> QFrame:
        box = self._card()
        self.log_panel = box
        box.setMinimumHeight(110)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        header = QHBoxLayout()
        title = QLabel("运行事件")
        title.setObjectName("sectionTitle")
        self.log_status_label = QLabel("最近日志：暂无")
        self.log_status_label.setObjectName("mutedLabel")
        self.log_status_label.setMinimumWidth(0)
        self.log_status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.log_level_checks: dict[str, QCheckBox] = {}
        levels = list((getattr(self.config, "log_panel", {}) or {}).get("visible_levels", ["INFO", "WARN", "WARNING", "ERROR"]))
        for label in ["INFO", "WARN", "ERROR"]:
            check = QCheckBox(label)
            check.setChecked(label in levels or (label == "WARN" and "WARNING" in levels))
            check.stateChanged.connect(lambda *_: self.refresh_log_panel())
            self.log_level_checks[label] = check
        self.log_auto_scroll_check = QCheckBox("自动滚动")
        self.log_auto_scroll_check.setChecked(bool((getattr(self.config, "log_panel", {}) or {}).get("auto_scroll", True)))
        self.log_auto_scroll_check.stateChanged.connect(lambda *_: self.refresh_log_panel())
        self.log_copy_btn = QPushButton("复制日志")
        self.log_clear_btn = QPushButton("清空显示")
        self.log_open_btn = QPushButton("打开日志文件")
        self.log_workbench_btn = QPushButton("日志工作台")
        self.log_copy_error_btn = QPushButton("复制错误")
        self.log_toggle_btn = QPushButton("折叠")
        self.log_copy_btn.clicked.connect(self.copy_visible_logs)
        self.log_clear_btn.clicked.connect(self.clear_log_display)
        self.log_open_btn.clicked.connect(self.open_log_file)
        self.log_workbench_btn.clicked.connect(self.open_log_workbench)
        self.log_copy_error_btn.clicked.connect(self.copy_error_logs)
        self.log_toggle_btn.clicked.connect(self.toggle_log_panel)
        header.addWidget(title)
        header.addWidget(self.log_status_label, 1)
        for check in self.log_level_checks.values():
            header.addWidget(check)
        header.addWidget(self.log_auto_scroll_check)
        for button in [
            self.log_copy_btn,
            self.log_clear_btn,
            self.log_workbench_btn,
            self.log_copy_error_btn,
            self.log_open_btn,
            self.log_toggle_btn,
        ]:
            button.setMinimumWidth(0)
            header.addWidget(button)
        layout.addLayout(header)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.document().setMaximumBlockCount(int((getattr(self.config, "log_panel", {}) or {}).get("max_visible_lines", 100)))
        font = QFont("Consolas", 10)
        self.log_text.setFont(font)
        layout.addWidget(self.log_text)
        self.log_expanded_only_widgets = [
            *self.log_level_checks.values(),
            self.log_auto_scroll_check,
            self.log_copy_btn,
            self.log_clear_btn,
        ]
        self.log_compact_only_widgets = [
            self.log_status_label,
            self.log_workbench_btn,
            self.log_copy_error_btn,
            self.log_open_btn,
        ]
        self._apply_log_panel_state()
        return box

    def _load_initial_state(self) -> None:
        self.reconcile_stale_runtime_state(force=True)
        if not self.config.auto_load_last_batch_on_startup:
            return
        batch_id = self.config.last_view_batch_id or self.config.last_selected_batch_id or self.batch_manager.latest_batch_id()
        if not batch_id:
            if self.manager.has_state():
                self.migrate_legacy_state_to_batch()
            return
        if self.load_batch(batch_id, prompt_poll=True):
            self.append_log("INFO", f"已加载最近批次: {batch_id}")

    def migrate_legacy_state_to_batch(self) -> None:
        try:
            self.manager.load_state()
            if not self.manager.tasks:
                return
            batch = self.batch_manager.create_batch(self.manager.tasks, self.config.excel_path, "旧版状态迁移批次")
            self.current_batch_id = batch.batch_id
            self.current_batch = batch
            self.config.last_selected_batch_id = batch.batch_id
            self.config.state_path = self.batch_manager.task_state_path(batch.batch_id)
            self.manager.update_state_path(self.config.state_path)
            self.apply_batch_fields_to_tasks(batch)
            self.manager.save_state(force_backup=True)
            self.update_current_batch_info()
            self.refresh_batch_combo()
            self.load_batch(batch.batch_id, prompt_poll=True)
            self.append_log("INFO", f"已将旧版状态迁移为批次: {batch.batch_id}")
        except Exception as exc:
            self.append_log("ERROR", f"旧版状态迁移失败: {exc}")

    def refresh_batch_combo(self) -> None:
        if hasattr(self, "output_edit"):
            root = self.output_edit.text().strip() or str(self.config.software_log_root)
            self.batch_manager.reconfigure(root, self.config.batch_root_dir_name)
        self.refresh_batch_table()
        self.update_status_summary()
        if not hasattr(self, "batch_combo"):
            return
        current = self.current_batch_id or self.config.last_view_batch_id or self.config.last_selected_batch_id
        self._loading_batch_combo = True
        self.batch_combo.clear()
        for batch in self.batch_manager.load_index():
            batch = self.display_batch_for_runtime_state(batch)
            label = f"{batch.batch_name}｜{batch.batch_id}｜{batch.task_count}条｜{batch.progress_percent:.2f}%｜{batch_status_text(batch.status)}"
            self.batch_combo.addItem(label, batch.batch_id)
            if batch.batch_id == current:
                self.batch_combo.setCurrentIndex(self.batch_combo.count() - 1)
        self._loading_batch_combo = False

    def refresh_batch_table(self) -> None:
        with self.ui_diag.measure("refresh_batch_table",
            batch_count=len(getattr(self, "batch_card_batches", {}) or {}),
            active_threads=threading.active_count(),
        ):
            self._refresh_batch_table_impl()

    def _refresh_batch_table_impl(self) -> None:
        if not hasattr(self, "batch_list"):
            return
        search = self.batch_search_edit.text().strip().lower() if hasattr(self, "batch_search_edit") else ""
        quick_filter = getattr(self, "batch_quick_filter", "全部")
        batches = self.batch_manager.load_index()
        filtered = []
        for raw_batch in batches:
            batch = self.display_batch_for_runtime_state(raw_batch)
            haystack = f"{batch.batch_name} {batch.batch_id} {batch.source_excel_path} {batch.remark or ''}".lower()
            if search and search not in haystack:
                continue
            if quick_filter in {"执行中", "运行中"} and batch.status != "RUNNING":
                continue
            if quick_filter == "失败" and batch.status not in {"FAILED", "PARTIAL_FAILED"}:
                continue
            if quick_filter == "已完成" and batch.status != "COMPLETED":
                continue
            if quick_filter == "已停止" and batch.status != "STOPPED":
                continue
            filtered.append(batch)
        self._loading_batch_table = True
        selected_id = self.selected_batch_list_id or self.current_batch_id
        self.batch_list.clear()
        self.batch_card_batches = {}
        selected_row = -1
        for row, batch in enumerate(filtered):
            if batch.batch_id == self.active_running_batch_id and self.running_manager is not None:
                batch = self.batch_manager.update_stats(
                    batch,
                    self.running_manager.tasks,
                    status_override=self.active_running_status_override,
                    save=False,
                )
                batch = self._stable_batch_card_stats(batch.batch_id, batch)
            self.batch_card_batches[batch.batch_id] = batch
            item = QListWidgetItem()
            item.setData(Qt.UserRole, batch.batch_id)
            item.setSizeHint(QSize(1, 90))
            self.batch_list.addItem(item)
            is_selected = batch.batch_id == selected_id or (not selected_id and batch.batch_id == self.current_batch_id)
            is_running = batch.batch_id == self.active_running_batch_id and self.is_background_running()
            widget = BatchCardWidget(batch, selected=is_selected, running=is_running)
            self.batch_list.setItemWidget(item, widget)
            if is_selected:
                selected_row = row
        # Empty-state guidance: when no batches match the current quick filter,
        # show a friendly placeholder card so the user knows what to do next.
        if not filtered and bool(getattr(self.config, "enable_empty_state_guidance", True)):
            placeholder = QListWidgetItem()
            placeholder.setFlags(Qt.NoItemFlags)
            placeholder.setSizeHint(QSize(1, 120))
            self.batch_list.addItem(placeholder)
            if batches and (search or quick_filter != "全部"):
                guidance = "当前筛选条件下没有匹配的批次\n清空搜索词或切换到「全部」试试"
            else:
                guidance = "还没有任务批次\n点击顶部「导入新批次」，从 Excel 开始一次新的生成流程"
            empty_widget = QLabel(guidance)
            empty_widget.setAlignment(Qt.AlignCenter)
            empty_widget.setWordWrap(True)
            empty_widget.setStyleSheet(
                "QLabel { color: #8a92a5; background-color: #10141d; border: 1px dashed #2a3142; "
                "border-radius: 6px; padding: 18px; margin: 4px; font-size: 12px; }"
            )
            self.batch_list.setItemWidget(placeholder, empty_widget)
        self._loading_batch_table = False
        if selected_row >= 0:
            self.batch_list.setCurrentRow(selected_row)

    def set_batch_quick_filter(self, name: str) -> None:
        self.batch_quick_filter = name
        for key, button in getattr(self, "batch_filter_buttons", {}).items():
            button.setChecked(key == name)
        self.refresh_batch_table()

    def on_batch_card_clicked(self, item: QListWidgetItem) -> None:
        self.selected_batch_list_id = str(item.data(Qt.UserRole) or "")
        self.update_batch_card_styles()

    def update_batch_card_styles(self) -> None:
        if not hasattr(self, "batch_list"):
            return
        for row in range(self.batch_list.count()):
            item = self.batch_list.item(row)
            batch_id = str(item.data(Qt.UserRole) or "")
            widget = self.batch_list.itemWidget(item)
            batch = getattr(self, "batch_card_batches", {}).get(batch_id)
            if isinstance(widget, BatchCardWidget) and batch:
                widget.update_batch(
                    batch,
                    selected=batch_id == self.selected_batch_list_id or batch_id == self.current_batch_id,
                    running=batch_id == self.active_running_batch_id and self.is_background_running(),
                )

    def selected_batch_id(self) -> str:
        if hasattr(self, "batch_list"):
            item = self.batch_list.currentItem()
            if item:
                return str(item.data(Qt.UserRole) or "")
        if getattr(self, "selected_batch_list_id", ""):
            return self.selected_batch_list_id
        return self.current_batch_id

    def view_selected_batch(self) -> None:
        batch_id = self.selected_batch_id()
        if not batch_id:
            self.show_status("请先选择批次", log=True)
            return
        self.view_batch_by_id(batch_id, self.view_batch_btn if hasattr(self, "view_batch_btn") else None)

    def view_batch_by_id(self, batch_id: str, button: QPushButton | None = None) -> None:
        self.selected_batch_list_id = batch_id
        self.update_batch_card_styles()
        self._switch_page(0)
        # Let the page switch and loading overlay paint before the heavier
        # batch load/table refresh starts.
        QTimer.singleShot(0, lambda bid=batch_id, btn=button: self.load_batch_async(bid, button=btn, prompt_poll=False))

    def load_batch_async(self, batch_id: str, button: QPushButton | None = None, prompt_poll: bool = False) -> None:
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            self.show_status("请先选择批次", log=True)
            return
        if self.batch_load_worker and self.batch_load_worker.isRunning():
            self.show_warning_toast("正在加载另一个批次，请稍等一下")
            return
        self._batch_switch_in_progress = True
        self._suspend_live_refresh_until = time.monotonic() + 8.0
        # Drop stale table refreshes from the previously viewed running batch.
        # The loaded batch gets an explicit refresh in apply_loaded_batch_payload().
        self._table_refresh_pending = False
        self._pending_task = None
        self._pending_stats = None
        self._stats_refresh_pending = False
        state = self.begin_loading("加载批次任务", button)
        worker = BatchLoadWorker(
            self.batch_manager,
            batch_id,
            self.config,
            self.active_running_batch_id,
            self.running_manager,
            self,
        )
        self.batch_load_worker = worker
        worker.loaded.connect(lambda payload, s=state, p=prompt_poll: self.finish_load_batch_async(payload, s, p))
        worker.failed.connect(lambda message, s=state: self.fail_load_batch_async(message, s))
        worker.start()

    def finish_load_batch_async(self, payload: dict, state: tuple[QPushButton | None, str, bool, str], prompt_poll: bool = False) -> None:
        try:
            loaded = self.apply_loaded_batch_payload(payload, prompt_poll=prompt_poll)
            self.end_loading(state, "加载批次任务完成" if loaded else "加载批次任务失败")
        except Exception as exc:
            self._batch_switch_in_progress = False
            self._suspend_live_refresh_until = 0.0
            self.end_loading(state, "加载批次任务失败")
            self.append_log("ERROR", f"加载批次任务失败: {exc}")
            self.show_error_toast(f"加载批次任务失败：{exc}")
        finally:
            if self.batch_load_worker:
                self.batch_load_worker.deleteLater()
                self.batch_load_worker = None

    def fail_load_batch_async(self, message: str, state: tuple[QPushButton | None, str, bool, str]) -> None:
        self._batch_switch_in_progress = False
        self._suspend_live_refresh_until = 0.0
        self.end_loading(state, "加载批次任务失败")
        self.append_log("ERROR", f"加载批次任务失败: {message}")
        self.show_error_toast(f"加载批次任务失败：{message}")
        if self.batch_load_worker:
            self.batch_load_worker.deleteLater()
            self.batch_load_worker = None

    def apply_loaded_batch_payload(self, payload: dict, prompt_poll: bool = False) -> bool:
        batch = payload.get("batch") if isinstance(payload, dict) else None
        manager = payload.get("manager") if isinstance(payload, dict) else None
        if not batch or not manager:
            self._batch_switch_in_progress = False
            self._suspend_live_refresh_until = 0.0
            return False
        self._table_refresh_pending = False
        self._pending_task = None
        self._pending_stats = None
        self._stats_refresh_pending = False
        self.current_batch_id = batch.batch_id
        self.current_view_batch_id = batch.batch_id
        self.selected_batch_list_id = batch.batch_id
        self.current_batch = batch
        self.config.last_selected_batch_id = batch.batch_id
        self.config.last_view_batch_id = batch.batch_id
        self.config.excel_path = Path(batch.source_excel_path) if batch.source_excel_path else self.config.excel_path
        self.excel_edit.setText(str(self.config.excel_path))
        if hasattr(self, "default_excel_edit"):
            self.default_excel_edit.setText(str(self.config.excel_path))
        self.config.state_path = self.batch_manager.task_state_path(batch.batch_id)
        self.manager = manager
        pre_norm_fingerprint = self._task_path_fingerprint()
        self.apply_batch_fields_to_tasks(batch)
        self.normalize_task_paths()
        post_norm_fingerprint = self._task_path_fingerprint()
        if pre_norm_fingerprint != post_norm_fingerprint:
            try:
                self.manager.save_state()
            except OSError as exc:
                self.append_log("WARNING", f"批次状态规范化保存失败，已跳过本次启动保存，不影响读取：{exc}")
        self.logger, self.log_path = setup_logger(self.batch_manager.batch_dir(batch.batch_id), self.batch_manager.log_path(batch.batch_id))
        if hasattr(self, "log_text"):
            self._set_ui_log_entries_from_text(str(payload.get("log_text") or ""))
        for warning in payload.get("warnings") or []:
            self.append_log("WARNING", str(warning))
        self.refresh_table()
        self._last_full_table_refresh = time.monotonic()
        self.update_stats(self.manager.stats())
        self.refresh_filter_value_options()
        self.refresh_batch_combo()
        QTimer.singleShot(0, lambda bid=batch.batch_id: self.start_archive_video_repair(bid))
        save_config(self.config)
        self._batch_switch_in_progress = False
        self._suspend_live_refresh_until = time.monotonic() + 1.0
        if prompt_poll and self.has_unfinished_polling_tasks():
            answer = QMessageBox.question(self, "继续轮询", "最近批次存在已提交但未完成的视频任务，是否立即继续轮询？")
            if answer == QMessageBox.Yes:
                self.start_worker(False, True)
        return True

    def start_archive_video_repair(self, batch_id: str) -> None:
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            return
        if batch_id == self.active_running_batch_id and self.is_process_worker_running():
            return
        if self.archive_repair_worker is not None and self.archive_repair_worker.isRunning():
            return
        worker = ArchiveVideoRepairWorker(self.batch_manager, batch_id, self.config, self)
        self.archive_repair_worker = worker
        worker.repaired.connect(self.finish_archive_video_repair)
        worker.failed.connect(self.fail_archive_video_repair)
        worker.start()

    def finish_archive_video_repair(self, payload: dict) -> None:
        batch_id = str((payload or {}).get("batch_id") or "")
        repaired = int((payload or {}).get("repaired") or 0)
        task_updates = (payload or {}).get("tasks") if isinstance(payload, dict) else []
        if batch_id == self.current_batch_id and isinstance(task_updates, list) and task_updates:
            by_key = {task.task_uid or TaskManager.task_uid_for(task): task for task in self.manager.tasks}
            for task_data in task_updates:
                if not isinstance(task_data, dict):
                    continue
                key = str(task_data.get("task_uid") or "").strip()
                if not key:
                    try:
                        key = TaskManager.task_uid_for(TaskItem.model_validate(task_data))
                    except Exception:
                        continue
                existing = by_key.get(key)
                if existing is not None:
                    merge_task_patch(existing, task_data)
            self.refresh_table()
            self.update_stats(self.manager.stats(), update_batch=False)
        if repaired:
            self.refresh_batch_combo()
            self.show_success_toast(f"已恢复 {repaired} 个已归档视频的下载状态")
        if self.archive_repair_worker:
            self.archive_repair_worker.deleteLater()
            self.archive_repair_worker = None

    def fail_archive_video_repair(self, batch_id: str, message: str) -> None:
        if batch_id == self.current_batch_id:
            self.show_status(f"已下载视频状态后台恢复暂时失败：{message}", timeout_ms=3500)
        if self.archive_repair_worker:
            self.archive_repair_worker.deleteLater()
            self.archive_repair_worker = None

    def show_batch_context_menu(self, pos) -> None:
        item = self.batch_list.itemAt(pos) if hasattr(self, "batch_list") else None
        if not item:
            return
        self.batch_list.setCurrentItem(item)
        self.on_batch_card_clicked(item)
        batch_id = self.selected_batch_id()
        menu = QMenu(self)
        menu.addAction("查看该批次任务", lambda: self.view_batch_by_id(batch_id))
        menu.addAction("开始执行该批次", lambda: self.start_batch_by_id(batch_id, False, False))
        menu.addAction("继续执行该批次", lambda: self.start_batch_by_id(batch_id, False, False))
        menu.addAction("继续轮询该批次", lambda: self.start_batch_by_id(batch_id, False, True))
        menu.addAction("手动轮询该批次", lambda: self.manual_poll_batch(batch_id))
        menu.addAction("切换该批次 API", lambda: self.switch_batch_api_by_id(batch_id))
        menu.addAction("暂停该批次", lambda: self.pause_batch_by_id(batch_id))
        menu.addAction("停止该批次", lambda: self.stop_batch_by_id(batch_id))
        menu.addAction("导出该批次结果", lambda: self.export_batch_by_id(batch_id))
        menu.addAction("打开批次目录", lambda: open_path(str(self.batch_manager.batch_dir(batch_id))))
        menu.addAction("重命名批次", lambda: self.rename_batch_by_id(batch_id))
        menu.addAction("修改批次备注", lambda: self.edit_batch_remark(batch_id))
        menu.addAction("删除批次记录", lambda: self.delete_batch_record_by_id(batch_id))
        menu.addSeparator()
        menu.addAction("复制批次ID", lambda: QApplication.clipboard().setText(batch_id))
        menu.addAction("复制批次路径", lambda: QApplication.clipboard().setText(str(self.batch_manager.batch_dir(batch_id))))
        batch = self.batch_manager.load_batch(batch_id)
        if batch and batch.source_excel_path:
            menu.addAction("打开来源Excel所在目录", lambda: open_path(str(Path(batch.source_excel_path).parent)))
        menu.exec(self.batch_list.viewport().mapToGlobal(pos))

    def on_batch_combo_changed(self, index: int) -> None:
        if self._loading_batch_combo or index < 0:
            return
        batch_id = self.batch_combo.itemData(index)
        if batch_id and batch_id != self.current_batch_id:
            self.load_batch_async(str(batch_id), prompt_poll=False)

    def load_batch(self, batch_id: str, prompt_poll: bool = False) -> bool:
        batch = self.batch_manager.load_batch(batch_id)
        if not batch:
            return False
        self.current_batch_id = batch.batch_id
        self.current_view_batch_id = batch.batch_id
        self.selected_batch_list_id = batch.batch_id
        self.current_batch = batch
        self.config.last_selected_batch_id = batch.batch_id
        self.config.last_view_batch_id = batch.batch_id
        self.config.excel_path = Path(batch.source_excel_path) if batch.source_excel_path else self.config.excel_path
        self.excel_edit.setText(str(self.config.excel_path))
        if hasattr(self, "default_excel_edit"):
            self.default_excel_edit.setText(str(self.config.excel_path))
        self.config.state_path = self.batch_manager.task_state_path(batch.batch_id)
        if self.active_running_batch_id == batch.batch_id and self.running_manager is not None:
            self.manager = self.running_manager
        else:
            self.manager = self.load_manager_for_batch(batch.batch_id)
        # Snapshot a cheap fingerprint of every task before normalization so we
        # only re-write the state file if normalize actually changed something.
        # Without this guard, every batch card click rewrote the entire state
        # JSON synchronously on the GUI thread — the single biggest contributor
        # to UI freezes when switching between batches.
        pre_norm_fingerprint = self._task_path_fingerprint()
        self.apply_batch_fields_to_tasks(batch)
        self.normalize_task_paths()
        post_norm_fingerprint = self._task_path_fingerprint()
        if pre_norm_fingerprint != post_norm_fingerprint:
            self.manager.save_state()
        self.logger, self.log_path = setup_logger(self.batch_manager.batch_dir(batch.batch_id), self.batch_manager.log_path(batch.batch_id))
        self.load_batch_log_text()
        self.refresh_table()
        self.update_stats(self.manager.stats())
        self.refresh_filter_value_options()
        self.refresh_batch_combo()
        QTimer.singleShot(0, lambda bid=batch.batch_id: self.start_archive_video_repair(bid))
        save_config(self.config)
        if prompt_poll and self.has_unfinished_polling_tasks():
            answer = QMessageBox.question(self, "继续轮询", "最近批次存在已提交但未完成的视频任务，是否立即继续轮询？")
            if answer == QMessageBox.Yes:
                self.start_worker(False, True)
        return True

    def load_manager_for_batch(self, batch_id: str) -> TaskManager:
        with self.ui_diag.measure("load_manager_for_batch",
            batch_id=batch_id,
            active_threads=threading.active_count(),
        ):
            return self._load_manager_for_batch_impl(batch_id)

    def _load_manager_for_batch_impl(self, batch_id: str) -> TaskManager:
        manager = TaskManager(self.batch_manager.task_state_path(batch_id))
        batch = self.batch_manager.load_batch(batch_id)
        if batch:
            manager.configure_batch_context(batch.batch_id, batch.batch_name, batch.imported_at, batch.source_excel_path)
        manager.configure_defaults(
            self.config.image_provider,
            self.config.image_model_logical_key,
            self.config.video_provider,
            self.config.video_model_logical_key,
        )
        tasks = manager.load_state(allow_legacy=False, save_after_load=False)
        if tasks and batch:
            mismatched = [task for task in tasks if task.batch_id != batch_id]
            if mismatched:
                backup_tasks = []
                try:
                    backup_tasks = manager.load_tasks_from_state_path(manager.backup_state_path)
                except Exception:
                    backup_tasks = []
                backup_matches = backup_tasks and not any(task.batch_id and task.batch_id != batch_id for task in backup_tasks)
                if backup_matches:
                    manager.tasks = backup_tasks
                    tasks = backup_tasks
                    self.append_log("WARNING", f"批次 {batch_id} 主状态批次字段异常，已从备份状态恢复 {len(tasks)} 条")
                else:
                    for task in mismatched:
                        task.batch_id = batch.batch_id
                        task.batch_name = batch.batch_name
                        task.imported_at = task.imported_at or batch.imported_at
                        task.source_excel_path = task.source_excel_path or batch.source_excel_path
                        task.task_uid = TaskManager.task_uid_for(task)
                    self.append_log("WARNING", f"批次 {batch_id} 的任务状态字段已按批次目录修复，保留原执行记录 {len(tasks)} 条")
                # Only force-save when we ACTUALLY mutated state (mismatch
                # repair or backup restore). The previous unconditional
                # force-backup save in the else branch wrote the whole state
                # file + copied it to .bak on every batch card click — on a
                # network share with multi-MB state files that froze the GUI
                # for seconds at a time.
                manager.save_state(force_backup=True)
        if not tasks and batch and batch.task_count and batch.source_excel_path:
            source = Path(batch.source_excel_path)
            if source.exists():
                try:
                    recovered = load_tasks_from_excel(source)
                    batch_config = dict(batch.batch_config or {})
                    image_provider = get_image_provider(batch_config.get("image_provider") or self.config.image_provider)
                    video_provider = get_video_provider(batch_config.get("video_provider") or self.config.video_provider)
                    for task in recovered:
                        task.batch_id = batch.batch_id
                        task.batch_name = batch.batch_name
                        task.task_uid = TaskManager.task_uid_for(task)
                        task.imported_at = batch.imported_at
                        task.source_excel_path = batch.source_excel_path
                        task.task_added_date = task.task_added_date or (batch.batch_id[:10] if len(batch.batch_id) >= 10 else today_text())
                        task.batch_date = task.batch_date or task.task_added_date
                        task.image_provider = batch_config.get("image_provider") or self.config.image_provider
                        task.image_model_logical_key = batch_config.get("image_model_logical_key") or self.config.image_model_logical_key
                        task.image_model_display = model_display_name(image_provider, task.image_model_logical_key)
                        task.video_provider = batch_config.get("video_provider") or self.config.video_provider
                        task.video_model_logical_key = batch_config.get("video_model_logical_key") or self.config.video_model_logical_key
                        task.video_model_display = model_display_name(video_provider, task.video_model_logical_key)
                        task.netdisk_original_path = task.netdisk_original_path or task.netdisk_path
                        task.netdisk_http_path = convert_netdisk_path_to_http(task.netdisk_original_path, self.config)
                    manager.set_tasks(recovered)
                    self.batch_manager.update_batch(batch_id, manager.tasks)
                except Exception as exc:
                    self.append_log("ERROR", f"批次状态缺失，尝试从 Excel 恢复失败: {exc}")
        return manager

    def apply_batch_fields_to_tasks(self, batch: TaskBatch) -> None:
        for task in self.manager.tasks:
            task.batch_id = batch.batch_id
            task.batch_name = batch.batch_name
            task.task_uid = TaskManager.task_uid_for(task)
            task.imported_at = task.imported_at or batch.imported_at
            task.source_excel_path = task.source_excel_path or batch.source_excel_path

    def has_unfinished_polling_tasks(self) -> bool:
        return any(
            task.status in {TaskStatus.VIDEO_SUBMITTED, TaskStatus.VIDEO_POLLING}
            and task.video_task_id
            and not task.video_url
            for task in self.manager.tasks
        )

    def load_batch_log_text(self) -> None:
        if not hasattr(self, "log_text"):
            return
        if not self.current_batch_id:
            self._set_ui_log_entries_from_text("")
            return
        path = self.batch_manager.log_path(self.current_batch_id)
        try:
            if path.exists() and path.stat().st_size > 0:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-500:]
                self._set_ui_log_entries_from_text("\n".join(lines))
            else:
                self._set_ui_log_entries_from_text("")
        except OSError:
            pass

    def update_current_batch_info(self, status_override: str | None = None) -> None:
        if not self.current_batch_id:
            return
        batch = self.batch_manager.update_batch(self.current_batch_id, self.manager.tasks, status_override=status_override)
        if batch:
            self.current_batch = batch
            self.apply_batch_fields_to_tasks(batch)
            self.refresh_batch_combo()

    def rename_current_batch(self) -> None:
        if not self.current_batch_id or not self.current_batch:
            QMessageBox.information(self, "没有批次", "当前没有可重命名的批次")
            return
        name, ok = QInputDialog.getText(self, "重命名批次", "批次名称", text=self.current_batch.batch_name)
        if not ok:
            return
        batch = self.batch_manager.rename_batch(self.current_batch_id, name)
        if not batch:
            return
        self.current_batch = batch
        for task in self.manager.tasks:
            task.batch_name = batch.batch_name
        self.manager.save_state()
        self.update_current_batch_info()
        self.refresh_batch_combo()
        self.update_stats(self.manager.stats())

    def delete_current_batch_record(self) -> None:
        if not self.current_batch_id:
            return
        self.delete_batch_record_by_id(self.current_batch_id)

    def open_current_batch_dir(self) -> None:
        if self.current_batch_id:
            open_path(str(self.batch_manager.batch_dir(self.current_batch_id)))

    def start_selected_batch(self, failed_only: bool = False, poll_only: bool = False, button: QPushButton | None = None) -> None:
        batch_id = self.selected_batch_id()
        if not batch_id:
            self.show_status("请先选择要执行的批次", log=True)
            return
        self.start_batch_by_id(batch_id, failed_only, poll_only, button)

    def start_batch_by_id(self, batch_id: str, failed_only: bool = False, poll_only: bool = False, button: QPushButton | None = None) -> None:
        self.run_with_feedback(
            "启动批次执行" if not poll_only else "启动批次轮询",
            lambda: self.start_worker(failed_only=failed_only, poll_only=poll_only, batch_id=batch_id),
            button,
        )

    def retry_failed_current_batch(self, button: QPushButton | None = None) -> None:
        batch_id = self.current_batch_id
        if not batch_id:
            self.show_status("请先选择要重试的批次", log=True)
            return
        selected_tasks = self._selected_tasks()
        selected_failed = [task for task in selected_tasks if self._task_needs_retry(task)]
        selected_keys = {
            task.task_uid or TaskManager.task_uid_for(task)
            for task in selected_failed
            if task.task_uid or TaskManager.task_uid_for(task)
        } or None
        action = "重试所选失败任务" if selected_keys else "重试失败任务"
        self.run_with_feedback(
            action,
            lambda: self.start_worker(failed_only=True, poll_only=False, batch_id=batch_id, selected_task_keys=selected_keys),
            button,
        )

    def pause_batch_by_id(self, batch_id: str) -> None:
        if batch_id != self.active_running_batch_id:
            self.show_status("选中的批次当前没有在执行", log=True)
            return
        self.pause_worker()

    def stop_batch_by_id(self, batch_id: str) -> None:
        if batch_id != self.active_running_batch_id:
            self.show_status("选中的批次当前没有在执行", log=True)
            return
        self.stop_worker(user_initiated=True)

    def start_image_only_current_batch(self) -> None:
        self.run_with_feedback(
            "启动仅生成图片",
            lambda: self.start_worker(False, False, execution_mode="image_only"),
            self.image_only_btn if hasattr(self, "image_only_btn") else None,
        )

    def start_video_only_current_batch(self) -> None:
        self.run_with_feedback(
            "启动图生视频",
            lambda: self.start_worker(False, False, execution_mode="video_only"),
            self.video_only_btn if hasattr(self, "video_only_btn") else None,
        )

    def start_selected_video_tasks(self) -> None:
        tasks = self._selected_tasks()
        if not tasks:
            QMessageBox.information(self, "未选择任务", "请先在任务表中选择要提交图生视频的任务")
            return
        keys = {task.task_uid or TaskManager.task_uid_for(task) for task in tasks}
        self.run_with_feedback(
            "启动所选任务图生视频",
            lambda: self.start_worker(False, False, execution_mode="video_only", selected_task_keys=keys),
            self.selected_video_btn if hasattr(self, "selected_video_btn") else None,
        )

    def switch_batch_api_by_id(self, batch_id: str, button: QPushButton | None = None) -> None:
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            self.show_warning_toast("请先选择要切换 API 的批次")
            return
        batch = self.batch_manager.load_batch(batch_id)
        if not batch:
            QMessageBox.warning(self, "批次不存在", "批次记录不存在或无法读取")
            return
        override = self.confirm_runtime_api_switch_dialog(batch)
        if not override:
            return
        if self.is_background_running() and self.active_running_batch_id == batch_id:
            answer = QMessageBox.question(
                self,
                "切换运行中批次 API",
                "当前批次正在执行。需要先暂停当前后台提交，切换 API 后再自动继续执行。\n是否现在切换？",
            )
            if answer != QMessageBox.Yes:
                return
            self.pending_api_switch_request = (batch_id, override, True)
            self.stop_worker(user_initiated=False)
            self._schedule_fast_process_worker_shutdown_for_api_switch(batch_id)
            self.show_status("已请求暂停当前批次，停止后会切换 API 并自动继续执行", log=True)
            self.show_warning_toast("正在准备切换 API，当前批次会先暂停提交")
            return

        self.run_with_feedback(
            "切换批次 API",
            lambda bid=batch_id, data=override: self.apply_runtime_api_switch(bid, data, restart=False),
            button,
        )

    def confirm_runtime_api_switch_dialog(self, batch: TaskBatch) -> dict | None:
        snapshot = dict(batch.batch_config or {})
        dialog = QDialog(self)
        dialog.setWindowTitle("切换批次 API")
        dialog.setMinimumWidth(560)
        layout = QVBoxLayout(dialog)
        hint = QLabel("只会影响未完成、失败、阻塞或待执行的节点；已完成结果会保留。切换图生图 API 时，未完成的图生图节点会改用新 API 重跑；已提交的视频 task_id 默认继续轮询。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        apply_image_check = QCheckBox("切换图生图 API")
        apply_image_check.setChecked(True)
        apply_video_check = QCheckBox("切换图生视频 API")
        apply_video_check.setChecked(False)
        force_task_id_check = QCheckBox("放弃未完成 task_id 并用新 API 重跑（可能增加成本）")
        force_task_id_check.setChecked(False)

        image_provider_combo = self._setup_combo_box(QComboBox(), 280, 16)
        image_model_combo = self._setup_combo_box(QComboBox(), 260, 14)
        video_provider_combo = self._setup_combo_box(QComboBox(), 280, 16)
        video_model_combo = self._setup_combo_box(QComboBox(), 320, 14)
        for key, name in provider_display_items(IMAGE_PROVIDERS):
            image_provider_combo.addItem(name, key)
        for key, name in provider_display_items(VIDEO_PROVIDERS):
            video_provider_combo.addItem(name, key)

        def fill_models(provider_combo: QComboBox, model_combo: QComboBox, providers, selected_key: str) -> None:
            provider = providers.get(str(provider_combo.currentData() or "")) or next(iter(providers.values()))
            model_combo.blockSignals(True)
            model_combo.clear()
            for key, name in model_display_items(provider):
                model_combo.addItem(name, key)
            self._set_combo_value(model_combo, selected_key)
            self._refresh_combo_box_display(model_combo)
            model_combo.blockSignals(False)

        image_provider_value = str(snapshot.get("image_provider") or self.config.image_provider)
        image_model_value = str(snapshot.get("image_model_logical_key") or self.config.image_model_logical_key)
        video_provider_value = str(snapshot.get("video_provider") or self.config.video_provider)
        video_model_value = str(snapshot.get("video_model_logical_key") or self.config.video_model_logical_key)
        self._set_combo_value(image_provider_combo, image_provider_value)
        self._set_combo_value(video_provider_combo, video_provider_value)

        image_key_edit = QLineEdit()
        image_key_edit.setEchoMode(QLineEdit.Password)
        image_base_edit = QLineEdit()
        video_key_edit = QLineEdit()
        video_key_edit.setEchoMode(QLineEdit.Password)
        video_base_edit = QLineEdit()

        def fill_profile(kind: str) -> None:
            if kind == "image":
                provider_key = str(image_provider_combo.currentData() or image_provider_value)
                profile = get_api_profile(self.config, "image", provider_key)
                fill_models(
                    image_provider_combo,
                    image_model_combo,
                    IMAGE_PROVIDERS,
                    str(profile.get("last_model_logical_key") or image_model_value),
                )
                if profile.get("api_key"):
                    image_key_edit.setText(str(profile.get("api_key") or ""))
                elif provider_key == image_provider_value:
                    image_key_edit.setText(str(snapshot.get("image_api_key") or self.config.image_api_key or ""))
                else:
                    image_key_edit.clear()
                if profile.get("base_url"):
                    image_base_edit.setText(str(profile.get("base_url") or ""))
                elif provider_key == image_provider_value:
                    image_base_edit.setText(str(snapshot.get("image_api_base_url") or self.config.image_api_base_url or ""))
                else:
                    image_base_edit.clear()
                if not profile.get("api_key"):
                    image_key_edit.setPlaceholderText("该平台还没有保存过 API Key")
            else:
                provider_key = str(video_provider_combo.currentData() or video_provider_value)
                profile = get_api_profile(self.config, "video", provider_key)
                fill_models(
                    video_provider_combo,
                    video_model_combo,
                    VIDEO_PROVIDERS,
                    str(profile.get("last_model_logical_key") or video_model_value),
                )
                if profile.get("api_key"):
                    video_key_edit.setText(str(profile.get("api_key") or ""))
                elif provider_key == video_provider_value:
                    video_key_edit.setText(str(snapshot.get("video_api_key") or self.config.video_api_key or ""))
                else:
                    video_key_edit.clear()
                if profile.get("base_url"):
                    video_base_edit.setText(str(profile.get("base_url") or ""))
                elif provider_key == video_provider_value:
                    video_base_edit.setText(str(snapshot.get("video_api_base_url") or self.config.video_api_base_url or ""))
                else:
                    video_base_edit.clear()
                if not profile.get("api_key"):
                    video_key_edit.setPlaceholderText("该平台还没有保存过 API Key")

        image_provider_combo.currentIndexChanged.connect(lambda *_: fill_profile("image"))
        video_provider_combo.currentIndexChanged.connect(lambda *_: fill_profile("video"))
        fill_profile("image")
        fill_profile("video")
        self._refresh_combo_box_display(image_provider_combo)
        self._refresh_combo_box_display(video_provider_combo)

        form.addRow("", apply_image_check)
        form.addRow("图生图 API 平台", image_provider_combo)
        form.addRow("图生图模型", image_model_combo)
        form.addRow("图生图 API Key", image_key_edit)
        form.addRow("图生图 Base URL", image_base_edit)
        form.addRow("", apply_video_check)
        form.addRow("图生视频 API 平台", video_provider_combo)
        form.addRow("图生视频模型", video_model_combo)
        form.addRow("图生视频 API Key", video_key_edit)
        form.addRow("图生视频 Base URL", video_base_edit)
        form.addRow("", force_task_id_check)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确认切换")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return None
        return {
            "apply_image": apply_image_check.isChecked(),
            "image_provider": str(image_provider_combo.currentData() or ""),
            "image_model_logical_key": str(image_model_combo.currentData() or ""),
            "image_api_key": image_key_edit.text().strip(),
            "image_api_base_url": image_base_edit.text().strip(),
            "apply_video": apply_video_check.isChecked(),
            "video_provider": str(video_provider_combo.currentData() or ""),
            "video_model_logical_key": str(video_model_combo.currentData() or ""),
            "video_api_key": video_key_edit.text().strip(),
            "video_api_base_url": video_base_edit.text().strip(),
            "force_restart_submitted": force_task_id_check.isChecked(),
        }

    def apply_runtime_api_switch(self, batch_id: str, override: dict, restart: bool = False) -> bool:
        batch = self.batch_manager.load_batch(batch_id)
        if not batch:
            self.show_error_toast("批次不存在，无法切换 API")
            return False
        manager = self.load_manager_for_batch(batch_id)
        workflow = self.batch_manager.resolve_batch_workflow(batch_id)
        summary = apply_runtime_provider_switch(batch, manager.tasks, override, workflow)
        manager.save_state(force_backup=True)
        self.batch_manager.save_batch(batch)
        updated_batch = self.batch_manager.update_batch(batch_id, manager.tasks, status_override="RUNNING" if restart else None)
        if updated_batch:
            batch = updated_batch
        if batch_id == self.current_batch_id:
            self.current_batch = batch
            self.manager = manager
            self.refresh_table()
            self.update_stats(self.manager.stats())
        self.refresh_batch_combo()
        self.append_log(
            "INFO",
            "[API_SWITCH] "
            f"batch={batch_id} tasks={summary['tasks_updated']} "
            f"image_nodes={summary['image_nodes_reset']} video_nodes={summary['video_nodes_reset']} "
            f"skip_task_id={summary['skipped_task_id_nodes']} skip_completed={summary['skipped_completed_nodes']}",
        )
        self.show_success_toast(
            f"API 已切换：更新 {summary['tasks_updated']} 条任务，"
            f"图生图节点 {summary['image_nodes_reset']}，视频节点 {summary['video_nodes_reset']}"
        )
        if restart:
            self.start_worker(False, False, batch_id=batch_id)
        return True

    def export_batch_by_id(self, batch_id: str) -> None:
        previous = self.current_batch_id
        if batch_id != self.current_batch_id:
            self.load_batch(batch_id, prompt_poll=False)
        self.export_results()
        if previous and previous != batch_id:
            self.load_batch(previous, prompt_poll=False)

    def rename_batch_by_id(self, batch_id: str) -> None:
        batch = self.batch_manager.load_batch(batch_id)
        if not batch:
            return
        name, ok = QInputDialog.getText(self, "重命名批次", "批次名称", text=batch.batch_name)
        if ok:
            self.batch_manager.rename_batch(batch_id, name)
            if batch_id == self.current_batch_id:
                self.load_batch(batch_id, prompt_poll=False)
            self.refresh_batch_combo()

    def edit_batch_remark(self, batch_id: str) -> None:
        batch = self.batch_manager.load_batch(batch_id)
        if not batch:
            return
        remark, ok = QInputDialog.getMultiLineText(self, "修改批次备注", "备注", batch.remark or "")
        if ok:
            self.batch_manager.update_remark(batch_id, remark)
            self.refresh_batch_combo()

    def delete_batch_record_by_id(self, batch_id: str) -> None:
        if not batch_id:
            return
        answer = QMessageBox.question(
            self,
            "删除批次记录",
            "是否删除该批次记录？\n此操作只会删除软件中的批次索引和状态文件，不会删除已生成图片或视频文件。",
        )
        if answer != QMessageBox.Yes:
            return
        remove_files = QMessageBox.question(self, "删除日志和导出", "是否同时删除该批次日志和导出文件？") == QMessageBox.Yes
        self.batch_manager.remove_batch_record(batch_id, remove_batch_files=remove_files)
        if batch_id == self.current_batch_id:
            self.current_batch_id = ""
            self.current_view_batch_id = ""
            self.selected_batch_list_id = ""
            self.current_batch = None
            self.manager.tasks = []
            self.refresh_table()
            self.update_stats(self.manager.stats())
        self.refresh_batch_combo()

    def refresh_running_batch_snapshot(self) -> None:
        if not self.active_running_batch_id:
            return
        if self.active_running_batch_id and self.running_manager is not None:
            self.queue_batch_update(
                self.active_running_batch_id,
                self.running_manager,
                self.active_running_status_override,
            )

    def normalize_task_paths(self) -> None:
        for task in self.manager.tasks:
            task.netdisk_original_path = task.netdisk_original_path or task.netdisk_path
            converted = convert_netdisk_path_to_http(task.netdisk_original_path, self.config)
            if not task.netdisk_http_path or task.netdisk_http_path == task.netdisk_path or task.netdisk_http_path == task.netdisk_original_path:
                task.netdisk_http_path = converted
            sync_task_urls_from_paths(task, self.config)

    def _task_path_fingerprint(self) -> tuple:
        """Tiny tuple summarising every task's path-related fields.

        Used to detect whether normalize_task_paths() actually mutated state
        so we can skip a redundant full-state save on each batch card click.
        """
        return tuple(
            (
                getattr(task, "task_uid", "") or getattr(task, "pid", ""),
                getattr(task, "netdisk_path", "") or "",
                getattr(task, "netdisk_original_path", "") or "",
                getattr(task, "netdisk_http_path", "") or "",
                getattr(task, "product_image_url", "") or "",
                getattr(task, "generated_image_url", "") or "",
                getattr(task, "video_url", "") or "",
                getattr(task, "batch_id", "") or "",
                getattr(task, "batch_name", "") or "",
            )
            for task in self.manager.tasks
        )

    def _sync_config_from_ui(self, prefer_settings: bool = False) -> None:
        self.config.excel_path = Path(self.excel_edit.text().strip())
        self.config.software_log_root = Path(self.output_edit.text().strip())
        if hasattr(self, "default_excel_edit"):
            if prefer_settings:
                excel_value = self.default_excel_edit.text().strip() or self.excel_edit.text().strip()
            else:
                excel_value = self.excel_edit.text().strip() or self.default_excel_edit.text().strip()
            self.config.excel_path = Path(excel_value)
            self.excel_edit.setText(str(self.config.excel_path))
            self.default_excel_edit.setText(str(self.config.excel_path))
            self.config.image_doc_path = Path(self.image_doc_edit.text().strip())
            self.config.video_doc_path = Path(self.video_doc_edit.text().strip())
            self.config.video_download_root = Path(self.video_download_root_edit.text().strip())
            self.config.image_assets_root = Path(self.image_assets_root_edit.text().strip())
            self.config.netdisk_local_prefix = self.netdisk_local_prefix_edit.text().strip()
            self.config.netdisk_http_prefix = normalize_netdisk_http_prefix(self.netdisk_http_prefix_edit.text())
            self.netdisk_http_prefix_edit.setText(self.config.netdisk_http_prefix)
            if prefer_settings:
                software_value = self.software_log_root_edit.text().strip() or self.output_edit.text().strip()
            else:
                software_value = self.output_edit.text().strip() or self.software_log_root_edit.text().strip()
            self.config.software_log_root = Path(software_value)
            self.output_edit.setText(str(self.config.software_log_root))
            self.software_log_root_edit.setText(str(self.config.software_log_root))
            self.config.image_provider = str(self.image_provider_combo.currentData() or self.config.image_provider)
            self.config.image_model_logical_key = str(self.image_model_combo.currentData() or self.config.image_model_logical_key)
            self.config.video_provider = str(self.video_provider_combo.currentData() or self.config.video_provider)
            self.config.video_model_logical_key = str(self.video_model_combo.currentData() or self.config.video_model_logical_key)
            self.config.image_api_key = self.image_api_key_edit.text().strip()
            self.config.video_api_key = self.video_api_key_edit.text().strip()
            self.config.image_api_base_url = self.image_base_url_edit.text().strip().rstrip("/")
            self.config.video_api_base_url = self.video_base_url_edit.text().strip().rstrip("/")
            reconcile_api_key_with_provider_profile(self.config, "image")
            reconcile_api_key_with_provider_profile(self.config, "video")
            self._remember_api_profiles_from_config()
        self.config.concurrency = self.concurrency_spin.value()
        self.config.image_concurrency = self.image_concurrency_spin.value()
        self.config.video_submit_concurrency = self.video_submit_concurrency_spin.value()
        self.config.poll_concurrency = self.poll_concurrency_spin.value()
        self.config.download_concurrency = self.download_concurrency_spin.value()
        self.config.retry_count = self.retry_spin.value()
        self.config.retry_interval_seconds = self.retry_interval_spin.value()
        self.config.poll_interval_seconds = self.poll_spin.value()
        self.config.max_poll_count = self.max_poll_spin.value()
        self.config.request_timeout_seconds = self.timeout_spin.value()
        self.config.regenerate_existing_images = self.regenerate_image_check.isChecked()
        self.config.auto_download_video = self.auto_download_video_check.isChecked()
        self.config.auto_save_image_assets = self.auto_save_image_assets_check.isChecked()
        self.config.group_by_owner = self.group_by_owner_check.isChecked()
        self.config.restore_last_tasks_on_startup = self.restore_tasks_check.isChecked()
        self.config.auto_load_last_batch_on_startup = self.restore_tasks_check.isChecked()
        self.config.enable_netdisk_http_mapping = self.enable_netdisk_http_mapping_check.isChecked()
        self.config.enable_manual_poll_button = self.enable_manual_poll_check.isChecked()
        self.config.manual_poll_ignore_max_count = self.manual_poll_ignore_max_check.isChecked()
        self.config.manual_poll_include_timeout_tasks = self.manual_poll_include_timeout_check.isChecked()
        self.config.manual_poll_include_failed_tasks = self.manual_poll_include_failed_check.isChecked()
        self.config.auto_retry_failed_workflow_enabled = self.auto_retry_workflow_check.isChecked()
        self.config.auto_retry_image_nodes = self.auto_retry_image_check.isChecked()
        self.config.auto_retry_video_nodes = self.auto_retry_video_check.isChecked()
        self.config.auto_retry_video_download = self.auto_retry_download_check.isChecked()
        self.config.export_only_visible_columns = self.export_visible_only_check.isChecked()
        self.config.show_api_doc_paths_in_main_settings = self.show_api_docs_check.isChecked() if hasattr(self, "show_api_docs_check") else False
        self.config.active_filters = {field: sorted(values) for field, values in self.active_filters.items()}
        self.config.enable_group_view = self.group_enabled_check.isChecked() if hasattr(self, "group_enabled_check") else self.config.enable_group_view
        self.config.group_by_fields = list(self.group_by_fields)
        self.config.visible_columns = self.current_visible_columns()
        self.config.table_text_color = self.table_text_color_edit.text().strip() or "#111111"
        self.batch_manager.reconfigure(self.config.software_log_root, self.config.batch_root_dir_name)
        manager_is_running = self.running_manager is not None and self.manager is self.running_manager
        if self.current_batch_id:
            self.config.output_dir = self.batch_manager.batch_dir(self.current_batch_id)
            self.config.state_path = self.batch_manager.task_state_path(self.current_batch_id)
        else:
            refresh_runtime_paths(self.config, None)
        ensure_runtime_dirs(self.config)
        if not manager_is_running:
            self.manager.configure_defaults(
                self.config.image_provider,
                self.config.image_model_logical_key,
                self.config.video_provider,
                self.config.video_model_logical_key,
            )
            self.manager.update_state_path(self.config.state_path)

    def save_settings(self) -> None:
        state = self.begin_loading("保存设置", self.save_settings_btn if hasattr(self, "save_settings_btn") else None)
        try:
            self._sync_config_from_ui(prefer_settings=True)
            self._refresh_image_models()
            self._refresh_video_models()
            path = save_config(self.config)
            if self.current_batch_id:
                self.logger, self.log_path = setup_logger(self.batch_manager.batch_dir(self.current_batch_id), self.batch_manager.log_path(self.current_batch_id))
            else:
                self.logger, self.log_path = setup_logger(self.config.output_dir)
            self.append_log("INFO", f"设置已保存: {path}")
            self.end_loading(state, "设置保存完成")
            self.show_success_toast("设置已保存，下次启动会自动生效")
        except Exception as exc:
            self.end_loading(state, "设置保存失败")
            self.append_log("ERROR", f"保存设置失败: {exc}")
            self.show_error_toast(f"设置保存失败：{exc}")

    def choose_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 Excel", str(Path.home()), "Excel 文件 (*.xlsx *.xls)")
        if path:
            self.excel_edit.setText(path)
            if hasattr(self, "default_excel_edit"):
                self.default_excel_edit.setText(path)

    def _dropped_excel_path(self, event) -> Path | None:
        mime = event.mimeData()
        if not mime or not mime.hasUrls():
            return None
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
                return path
        return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not getattr(self.config, "enable_excel_drag_drop_import", True):
            event.ignore()
            return
        path = self._dropped_excel_path(event)
        if path:
            event.acceptProposedAction()
            self.show_status("松开鼠标，导入 Excel 任务批次")
        else:
            event.ignore()
            self.show_status("请拖入 Excel 文件（.xlsx / .xls）")

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._dropped_excel_path(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.show_status("已取消拖拽导入")
        event.accept()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt API
        path = self._dropped_excel_path(event)
        if not path:
            self.show_warning_toast("请拖入 Excel 文件（.xlsx / .xls）")
            event.ignore()
            return
        event.acceptProposedAction()
        self.excel_edit.setText(str(path))
        if hasattr(self, "default_excel_edit"):
            self.default_excel_edit.setText(str(path))
        self.config.excel_path = path
        self.show_status(f"已收到拖拽文件：{path.name}，准备创建新批次", log=True)
        self.load_tasks(self.load_btn if hasattr(self, "load_btn") else None)

    def choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择软件日志目录", self.output_edit.text())
        if path:
            self.output_edit.setText(path)
            if hasattr(self, "software_log_root_edit"):
                self.software_log_root_edit.setText(path)

    def batch_config_snapshot(self, config: AppConfig | None = None) -> dict:
        cfg = config or self.config
        image_provider = get_image_provider(cfg.image_provider)
        video_provider = get_video_provider(cfg.video_provider)
        return {
            "image_provider": cfg.image_provider,
            "image_model_logical_key": cfg.image_model_logical_key,
            "image_model_display_name": model_display_name(image_provider, cfg.image_model_logical_key),
            "image_api_key": cfg.image_api_key,
            "image_api_base_url": cfg.image_api_base_url,
            "image_size": cfg.image_size,
            "video_provider": cfg.video_provider,
            "video_model_logical_key": cfg.video_model_logical_key,
            "video_model_display_name": model_display_name(video_provider, cfg.video_model_logical_key),
            "video_api_key": cfg.video_api_key,
            "video_api_base_url": cfg.video_api_base_url,
            "video_download_root": str(cfg.video_download_root),
            "image_assets_root": str(cfg.image_assets_root),
            "software_log_root": str(cfg.software_log_root),
            "auto_download_video": cfg.auto_download_video,
            "auto_save_image_assets": cfg.auto_save_image_assets,
            "group_by_owner": cfg.group_by_owner,
            "poll_interval_seconds": cfg.poll_interval_seconds,
            "max_poll_count": cfg.max_poll_count,
            "retry_count": cfg.retry_count,
            "retry_interval_seconds": cfg.retry_interval_seconds,
            "request_timeout_seconds": cfg.request_timeout_seconds,
            "image_concurrency": cfg.image_concurrency,
            "video_submit_concurrency": cfg.video_submit_concurrency,
            "poll_concurrency": cfg.poll_concurrency,
            "download_concurrency": cfg.download_concurrency,
            "enable_stage_based_workflow": cfg.enable_stage_based_workflow,
            "workflow_engine_version": cfg.workflow_engine_version,
            "enable_manual_poll_button": cfg.enable_manual_poll_button,
            "manual_poll_ignore_max_count": cfg.manual_poll_ignore_max_count,
            "manual_poll_include_timeout_tasks": cfg.manual_poll_include_timeout_tasks,
            "manual_poll_include_failed_tasks": cfg.manual_poll_include_failed_tasks,
            "auto_retry_failed_workflow_enabled": cfg.auto_retry_failed_workflow_enabled,
            "auto_retry_image_nodes": cfg.auto_retry_image_nodes,
            "auto_retry_video_nodes": cfg.auto_retry_video_nodes,
            "auto_retry_video_download": cfg.auto_retry_video_download,
        }

    def config_from_batch(self, batch: TaskBatch | None) -> AppConfig:
        cfg = copy.copy(self.config)
        snapshot = dict((batch.batch_config or {}) if batch else {})
        for key in [
            "image_provider",
            "image_model_logical_key",
            "image_api_key",
            "image_api_base_url",
            "image_size",
            "video_provider",
            "video_model_logical_key",
            "video_api_key",
            "video_api_base_url",
            "auto_download_video",
            "auto_save_image_assets",
            "group_by_owner",
            "poll_interval_seconds",
            "max_poll_count",
            "retry_count",
            "retry_interval_seconds",
            "request_timeout_seconds",
            "image_concurrency",
            "video_submit_concurrency",
            "poll_concurrency",
            "download_concurrency",
            "enable_stage_based_workflow",
            "workflow_engine_version",
            "enable_manual_poll_button",
            "manual_poll_ignore_max_count",
            "manual_poll_include_timeout_tasks",
            "manual_poll_include_failed_tasks",
            "auto_retry_failed_workflow_enabled",
            "auto_retry_image_nodes",
            "auto_retry_video_nodes",
            "auto_retry_video_download",
        ]:
            if key in snapshot and snapshot[key] not in (None, ""):
                setattr(cfg, key, snapshot[key])
        reconcile_api_key_with_provider_profile(cfg, "image", force_profile=True)
        reconcile_api_key_with_provider_profile(cfg, "video", force_profile=True)
        for key in ["video_download_root", "image_assets_root", "software_log_root"]:
            if snapshot.get(key):
                setattr(cfg, key, Path(str(snapshot[key])))
        if batch:
            cfg.output_dir = self.batch_manager.batch_dir(batch.batch_id)
            cfg.state_path = self.batch_manager.task_state_path(batch.batch_id)
        return cfg

    def apply_batch_config_to_global(self, snapshot: dict) -> None:
        for key, value in snapshot.items():
            if not hasattr(self.config, key):
                continue
            if key in {"video_download_root", "image_assets_root", "software_log_root"}:
                setattr(self.config, key, Path(str(value)))
            else:
                setattr(self.config, key, value)
        reconcile_api_key_with_provider_profile(self.config, "image", force_profile=True)
        reconcile_api_key_with_provider_profile(self.config, "video", force_profile=True)
        self._remember_api_profiles_from_config()
        if hasattr(self, "image_provider_combo"):
            self._set_combo_value(self.image_provider_combo, self.config.image_provider)
            self._refresh_image_models()
            self._set_combo_value(self.image_model_combo, self.config.image_model_logical_key)
            self.image_api_key_edit.setText(self.config.image_api_key)
            self.image_base_url_edit.setText(self.config.image_api_base_url)
        if hasattr(self, "video_provider_combo"):
            self._set_combo_value(self.video_provider_combo, self.config.video_provider)
            self._refresh_video_models()
            self._set_combo_value(self.video_model_combo, self.config.video_model_logical_key)
            self.video_api_key_edit.setText(self.config.video_api_key)
            self.video_base_url_edit.setText(self.config.video_api_base_url)

    def confirm_batch_config_dialog(self, source_excel_path: Path) -> dict | None:
        reserved_batch_id = self.batch_manager.unique_batch_id()
        if not self.config.show_batch_config_dialog_on_import:
            return {
                "batch_id": reserved_batch_id,
                "batch_name": self.batch_manager.default_batch_name(reserved_batch_id),
                "source_excel_path": str(source_excel_path),
                "batch_config": self.batch_config_snapshot(),
                "save_global": False,
                "execute_now": False,
            }
        dialog = QDialog(self)
        dialog.setWindowTitle("批次任务配置确认")
        dialog.resize(720, 640)
        layout = QVBoxLayout(dialog)
        notice = QLabel("即将创建全新批次，不会覆盖任何历史批次记录。")
        notice.setWordWrap(True)
        notice.setObjectName("hintLabel")
        layout.addWidget(notice)
        form_box = QWidget()
        form = QFormLayout(form_box)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        batch_name_edit = QLineEdit(self.batch_manager.default_batch_name(reserved_batch_id))
        excel_edit = QLineEdit(str(source_excel_path))

        image_provider_combo = self._setup_combo_box(QComboBox(), 280, 16)
        image_model_combo = self._setup_combo_box(QComboBox(), 260, 14)
        video_provider_combo = self._setup_combo_box(QComboBox(), 280, 16)
        video_model_combo = self._setup_combo_box(QComboBox(), 300, 14)
        for key, name in provider_display_items(IMAGE_PROVIDERS):
            image_provider_combo.addItem(name, key)
        for key, name in provider_display_items(VIDEO_PROVIDERS):
            video_provider_combo.addItem(name, key)
        image_provider_combo.setCurrentIndex(max(0, image_provider_combo.findData(self.config.image_provider)))
        video_provider_combo.setCurrentIndex(max(0, video_provider_combo.findData(self.config.video_provider)))
        image_key_edit = QLineEdit()
        video_key_edit = QLineEdit()
        image_key_edit.setEchoMode(QLineEdit.Password)
        video_key_edit.setEchoMode(QLineEdit.Password)
        image_base_edit = QLineEdit(self.config.image_api_base_url)
        video_base_edit = QLineEdit(self.config.video_api_base_url)

        def fill_models(provider_combo: QComboBox, model_combo: QComboBox, providers, selected: str) -> None:
            provider = providers.get(provider_combo.currentData()) or next(iter(providers.values()))
            model_combo.clear()
            for key, name in model_display_items(provider):
                model_combo.addItem(name, key)
            idx = model_combo.findData(selected)
            model_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self._refresh_combo_box_display(model_combo)

        def fill_profile(kind: str) -> None:
            if kind == "image":
                provider_key = str(image_provider_combo.currentData() or self.config.image_provider)
                profile = get_api_profile(self.config, "image", provider_key)
                fill_models(
                    image_provider_combo,
                    image_model_combo,
                    IMAGE_PROVIDERS,
                    str(profile.get("last_model_logical_key") or self.config.image_model_logical_key),
                )
                image_key_edit.setText(str(profile.get("api_key") or ""))
                image_base_edit.setText(str(profile.get("base_url") or self.config.image_api_base_url or ""))
                if not profile.get("api_key"):
                    image_key_edit.setPlaceholderText("该平台还没有保存过 API Key")
            else:
                provider_key = str(video_provider_combo.currentData() or self.config.video_provider)
                profile = get_api_profile(self.config, "video", provider_key)
                fill_models(
                    video_provider_combo,
                    video_model_combo,
                    VIDEO_PROVIDERS,
                    str(profile.get("last_model_logical_key") or self.config.video_model_logical_key),
                )
                video_key_edit.setText(str(profile.get("api_key") or ""))
                video_base_edit.setText(str(profile.get("base_url") or self.config.video_api_base_url or ""))
                if not profile.get("api_key"):
                    video_key_edit.setPlaceholderText("该平台还没有保存过 API Key")

        image_provider_combo.currentIndexChanged.connect(lambda *_: fill_profile("image"))
        video_provider_combo.currentIndexChanged.connect(lambda *_: fill_profile("video"))
        fill_profile("image")
        fill_profile("video")
        self._refresh_combo_box_display(image_provider_combo)
        self._refresh_combo_box_display(video_provider_combo)

        video_root_edit = QLineEdit(str(self.config.video_download_root))
        image_root_edit = QLineEdit(str(self.config.image_assets_root))
        log_root_edit = QLineEdit(str(self.config.software_log_root))
        auto_video_check = QCheckBox("视频完成后自动下载")
        auto_video_check.setChecked(self.config.auto_download_video)
        auto_image_check = QCheckBox("保存图片任务资料")
        auto_image_check.setChecked(self.config.auto_save_image_assets)
        owner_check = QCheckBox("按负责人分目录")
        owner_check.setChecked(self.config.group_by_owner)
        remember_api_key_check = QCheckBox("记住此平台 API Key")
        remember_api_key_check.setChecked(bool(getattr(self.config, "remember_batch_dialog_api_key", True)))
        save_global_check = QCheckBox("同时保存为全局默认设置")
        save_global_check.setChecked(self.config.save_batch_dialog_settings_as_global_default)
        poll_spin = QSpinBox()
        poll_spin.setRange(1, 120)
        poll_spin.setValue(self.config.poll_interval_seconds)
        max_poll_spin = QSpinBox()
        max_poll_spin.setRange(1, 2000)
        max_poll_spin.setValue(self.config.max_poll_count)
        retry_spin = QSpinBox()
        retry_spin.setRange(0, 20)
        retry_spin.setValue(self.config.retry_count)
        timeout_spin = QSpinBox()
        timeout_spin.setRange(10, 1000)
        timeout_spin.setValue(self.config.request_timeout_seconds)

        form.addRow("批次名称", batch_name_edit)
        form.addRow("来源 Excel 路径", excel_edit)
        form.addRow("图生图 API 平台", image_provider_combo)
        form.addRow("图生图模型", image_model_combo)
        form.addRow("图生图 API Key", image_key_edit)
        form.addRow("图生图 API Base URL", image_base_edit)
        form.addRow("图生视频 API 平台", video_provider_combo)
        form.addRow("图生视频模型", video_model_combo)
        form.addRow("图生视频 API Key", video_key_edit)
        form.addRow("图生视频 API Base URL", video_base_edit)
        form.addRow("视频下载根目录", video_root_edit)
        form.addRow("图片任务资料根目录", image_root_edit)
        form.addRow("软件日志根目录", log_root_edit)
        form.addRow("轮询间隔秒", poll_spin)
        form.addRow("最大轮询次数", max_poll_spin)
        form.addRow("失败重试次数", retry_spin)
        form.addRow("请求超时秒", timeout_spin)
        form.addRow("", auto_video_check)
        form.addRow("", auto_image_check)
        form.addRow("", owner_check)
        form.addRow("", remember_api_key_check)
        form.addRow("", save_global_check)
        layout.addWidget(form_box)

        result: dict = {}

        def accept(execute_now: bool) -> None:
            image_provider = get_image_provider(str(image_provider_combo.currentData()))
            video_provider = get_video_provider(str(video_provider_combo.currentData()))
            image_model_key = str(image_model_combo.currentData())
            video_model_key = str(video_model_combo.currentData())
            result.update(
                {
                    "batch_name": batch_name_edit.text().strip(),
                    "batch_id": reserved_batch_id,
                    "source_excel_path": excel_edit.text().strip(),
                    "save_global": save_global_check.isChecked(),
                    "execute_now": execute_now,
                    "batch_config": {
                        "image_provider": str(image_provider_combo.currentData()),
                        "image_model_logical_key": image_model_key,
                        "image_model_display_name": model_display_name(image_provider, image_model_key),
                        "image_api_key": image_key_edit.text().strip(),
                        "image_api_base_url": image_base_edit.text().strip().rstrip("/"),
                        "image_size": self.config.image_size,
                        "video_provider": str(video_provider_combo.currentData()),
                        "video_model_logical_key": video_model_key,
                        "video_model_display_name": model_display_name(video_provider, video_model_key),
                        "video_api_key": video_key_edit.text().strip(),
                        "video_api_base_url": video_base_edit.text().strip().rstrip("/"),
                        "video_download_root": video_root_edit.text().strip(),
                        "image_assets_root": image_root_edit.text().strip(),
                        "software_log_root": log_root_edit.text().strip(),
                        "auto_download_video": auto_video_check.isChecked(),
                        "auto_save_image_assets": auto_image_check.isChecked(),
                        "group_by_owner": owner_check.isChecked(),
                        "poll_interval_seconds": poll_spin.value(),
                        "max_poll_count": max_poll_spin.value(),
                        "retry_count": retry_spin.value(),
                        "retry_interval_seconds": self.config.retry_interval_seconds,
                        "request_timeout_seconds": timeout_spin.value(),
                        "image_concurrency": self.config.image_concurrency,
                        "video_submit_concurrency": self.config.video_submit_concurrency,
                        "poll_concurrency": self.config.poll_concurrency,
                        "download_concurrency": self.config.download_concurrency,
                        "enable_stage_based_workflow": self.config.enable_stage_based_workflow,
                        "workflow_engine_version": self.config.workflow_engine_version,
                        "enable_manual_poll_button": self.config.enable_manual_poll_button,
                        "manual_poll_ignore_max_count": self.config.manual_poll_ignore_max_count,
                        "manual_poll_include_timeout_tasks": self.config.manual_poll_include_timeout_tasks,
                        "manual_poll_include_failed_tasks": self.config.manual_poll_include_failed_tasks,
                        "auto_retry_failed_workflow_enabled": self.config.auto_retry_failed_workflow_enabled,
                        "auto_retry_image_nodes": self.config.auto_retry_image_nodes,
                        "auto_retry_video_nodes": self.config.auto_retry_video_nodes,
                        "auto_retry_video_download": self.config.auto_retry_video_download,
                    },
                    "remember_api_key": remember_api_key_check.isChecked(),
                }
            )
            dialog.accept()

        button_row = QHBoxLayout()
        cancel_btn = QPushButton("取消")
        create_btn = QPushButton("确认创建批次")
        create_run_btn = QPushButton("确认创建并立即执行")
        create_btn.setObjectName("primaryButton")
        create_run_btn.setObjectName("primaryButton")
        cancel_btn.clicked.connect(dialog.reject)
        create_btn.clicked.connect(lambda: accept(False))
        create_run_btn.clicked.connect(lambda: accept(True))
        button_row.addWidget(cancel_btn)
        button_row.addStretch(1)
        button_row.addWidget(create_btn)
        button_row.addWidget(create_run_btn)
        layout.addLayout(button_row)

        if dialog.exec() != QDialog.Accepted:
            return None
        return result

    def load_tasks(self, button: QPushButton | None = None) -> None:
        try:
            self._sync_config_from_ui()
            dialog_result = self.confirm_batch_config_dialog(Path(self.config.excel_path))
            if not dialog_result:
                self.show_status("已取消导入批次", log=True)
                return
            source_excel_path = Path(dialog_result["source_excel_path"])
            self.import_loading_state = self.begin_loading("导入 Excel 创建批次", button or (self.load_btn if hasattr(self, "load_btn") else None))
            self.excel_import_worker = ExcelImportWorker(source_excel_path, dialog_result, self)
            self.excel_import_worker.loaded.connect(self._finish_import_tasks)
            self.excel_import_worker.failed.connect(self._handle_import_failed)
            self.excel_import_worker.finished.connect(lambda: setattr(self, "excel_import_worker", None))
            self.excel_import_worker.start()
        except Exception as exc:
            self.append_log("ERROR", f"加载任务失败: {exc}")
            self.show_error_toast(f"导入批次失败：{exc}")

    def _remember_profiles_from_batch_config(self, batch_config: dict) -> None:
        update_api_profile(
            self.config,
            "image",
            str(batch_config.get("image_provider") or self.config.image_provider),
            api_key=str(batch_config.get("image_api_key") or ""),
            last_model_logical_key=str(batch_config.get("image_model_logical_key") or ""),
            base_url=str(batch_config.get("image_api_base_url") or ""),
        )
        update_api_profile(
            self.config,
            "video",
            str(batch_config.get("video_provider") or self.config.video_provider),
            api_key=str(batch_config.get("video_api_key") or ""),
            last_model_logical_key=str(batch_config.get("video_model_logical_key") or ""),
            base_url=str(batch_config.get("video_api_base_url") or ""),
        )

    def _finish_import_tasks(self, tasks: list[TaskItem], dialog_result: dict) -> None:
        state = self.import_loading_state
        try:
            source_excel_path = Path(dialog_result["source_excel_path"])
            batch_config = dict(dialog_result["batch_config"])
            self.config.save_batch_dialog_settings_as_global_default = bool(dialog_result.get("save_global"))
            if dialog_result.get("remember_api_key", True):
                self._remember_profiles_from_batch_config(batch_config)
            if dialog_result.get("save_global"):
                self.apply_batch_config_to_global(batch_config)
                save_config(self.config)
                self.show_status("已保存为全局默认配置", log=True)
            batch_date = today_text()
            batch = self.batch_manager.create_batch(
                tasks,
                source_excel_path,
                dialog_result.get("batch_name"),
                batch_config,
                batch_id=dialog_result.get("batch_id") or None,
            )
            self.current_batch_id = batch.batch_id
            self.current_view_batch_id = batch.batch_id
            self.selected_batch_list_id = batch.batch_id
            self.current_batch = batch
            self.config.last_selected_batch_id = batch.batch_id
            self.config.last_view_batch_id = batch.batch_id
            self.config.excel_path = source_excel_path
            self.excel_edit.setText(str(source_excel_path))
            if hasattr(self, "default_excel_edit"):
                self.default_excel_edit.setText(str(source_excel_path))
            self.config.state_path = self.batch_manager.task_state_path(batch.batch_id)
            self.logger, self.log_path = setup_logger(self.batch_manager.batch_dir(batch.batch_id), self.batch_manager.log_path(batch.batch_id))
            new_manager = TaskManager(self.config.state_path)
            new_manager.configure_batch_context(batch.batch_id, batch.batch_name, batch.imported_at, batch.source_excel_path)
            new_manager.configure_defaults(
                batch_config.get("image_provider") or self.config.image_provider,
                batch_config.get("image_model_logical_key") or self.config.image_model_logical_key,
                batch_config.get("video_provider") or self.config.video_provider,
                batch_config.get("video_model_logical_key") or self.config.video_model_logical_key,
            )
            image_provider = get_image_provider(batch_config.get("image_provider") or self.config.image_provider)
            video_provider = get_video_provider(batch_config.get("video_provider") or self.config.video_provider)
            for task in tasks:
                task.task_added_date = batch_date
                task.batch_date = batch_date
                task.batch_id = batch.batch_id
                task.batch_name = batch.batch_name
                task.task_uid = TaskManager.task_uid_for(task)
                task.imported_at = batch.imported_at
                task.source_excel_path = batch.source_excel_path
                task.netdisk_original_path = task.netdisk_original_path or task.netdisk_path
                task.netdisk_http_path = convert_netdisk_path_to_http(task.netdisk_original_path, self.config)
                task.image_provider = batch_config.get("image_provider") or self.config.image_provider
                task.image_model_logical_key = batch_config.get("image_model_logical_key") or self.config.image_model_logical_key
                task.image_model_display = model_display_name(image_provider, task.image_model_logical_key)
                task.video_provider = batch_config.get("video_provider") or self.config.video_provider
                task.video_model_logical_key = batch_config.get("video_model_logical_key") or self.config.video_model_logical_key
                task.video_model_display = model_display_name(video_provider, task.video_model_logical_key)
                append_task_log(task, "INFO", "TASK", "任务已导入新批次", node_id=None)
            new_manager.set_tasks(tasks)
            self.manager = new_manager
            self.update_current_batch_info()
            save_config(self.config)
            self.refresh_batch_combo()
            self.refresh_table()
            self.update_stats(self.manager.stats())
            self.append_log("INFO", f"新批次创建成功: {batch.batch_name} / {batch.batch_id}，共 {len(tasks)} 条；历史批次记录已保留")
            if state:
                self.end_loading(state, f"新批次创建成功：{batch.batch_id}，历史批次记录已保留")
            self.import_loading_state = None
            legacy_count = sum(1 for t in tasks if bool(getattr(t, "legacy_prompt_compat_mode", False)))
            if legacy_count and legacy_count == len(tasks):
                self.show_warning_toast(
                    f"批次导入成功（{len(tasks)} 条），但当前 Excel 使用旧版字段，阶段3 / 阶段4 提示词为空"
                )
            elif legacy_count:
                self.show_warning_toast(
                    f"批次导入成功（{len(tasks)} 条），其中 {legacy_count} 条使用旧版字段兼容模式"
                )
            else:
                self.show_success_toast(f"批次创建成功（{len(tasks)} 条），已为你保留历史记录")
            if dialog_result.get("execute_now"):
                self.start_batch_by_id(batch.batch_id, False, False)
        except Exception as exc:
            self._handle_import_failed(str(exc), dialog_result)

    def _handle_import_failed(self, message: str, dialog_result: object = None) -> None:
        state = self.import_loading_state
        if state:
            self.end_loading(state, "导入批次失败")
        self.import_loading_state = None
        self.append_log("ERROR", f"加载任务失败: {message}")
        self.show_error_toast(f"导入没有成功，但不会影响已有批次记录。原因：{message}")

    def _start_process_worker(
        self,
        batch: TaskBatch,
        running_manager: TaskManager,
        failed_only: bool,
        poll_only: bool,
        execution_mode: str,
        selected_task_keys: set[str] | None,
    ) -> None:
        events_path, commands_path = self.process_runtime_paths(batch.batch_id)
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text("", encoding="utf-8")
        commands_path.write_text("", encoding="utf-8")
        selected_json = json.dumps(sorted(selected_task_keys or []), ensure_ascii=False)
        command = [
            sys.executable,
            "-m",
            "app.runtime.worker_process",
            "--batch-id",
            batch.batch_id,
            "--mode",
            execution_mode or "full",
            "--selected-task-keys-json",
            selected_json,
            "--events-path",
            str(events_path),
            "--commands-path",
            str(commands_path),
            "--parent-pid",
            str(os.getpid()),
        ]
        if failed_only:
            command.append("--failed-only")
        if poll_only:
            command.append("--poll-only")
        self.worker_events_path = events_path
        self.worker_commands_path = commands_path
        self.worker_event_offset = 0
        self.worker_process_finished_seen = False
        self.worker_process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
        )
        self.running_manager = running_manager

    def start_worker(
        self,
        failed_only: bool,
        poll_only: bool = False,
        batch_id: str | None = None,
        execution_mode: str = "full",
        selected_task_keys: set[str] | None = None,
        queued_start: bool = False,
    ) -> None:
        target_batch_id = batch_id or self.current_batch_id
        if not target_batch_id:
            QMessageBox.warning(self, "没有批次", "请先选择批次")
            return
        self._sync_config_from_ui()
        if self.is_background_running():
            if self.active_running_batch_id == target_batch_id:
                if failed_only and not poll_only:
                    payload = {"selected_task_keys": sorted(selected_task_keys or [])}
                    if self.worker_process is not None:
                        self.send_process_worker_command("retry_failed", payload)
                    elif self.worker is not None:
                        self.worker.request_retry_failed(payload["selected_task_keys"])
                    count_text = f"所选 {len(payload['selected_task_keys'])} 个失败任务" if payload["selected_task_keys"] else "当前批次失败任务"
                    self.show_status(f"已请求运行中重试：{count_text}", log=True)
                    self.append_log("INFO", f"[RETRY] running batch retry requested batch={target_batch_id} selected={len(payload['selected_task_keys'])}")
                    return "RETRY_REQUESTED"
                QMessageBox.information(self, "正在执行", "该批次正在执行中")
                return
            return self.enqueue_batch_run(
                target_batch_id,
                failed_only=failed_only,
                poll_only=poll_only,
                execution_mode=execution_mode,
                selected_task_keys=selected_task_keys,
                source="queued_start" if queued_start else "user",
            )
        if not queued_start:
            self.run_queue_paused_by_user_stop = False

        batch = self.current_batch if target_batch_id == self.current_batch_id and self.current_batch else self.batch_manager.load_batch(target_batch_id)
        if not batch:
            QMessageBox.warning(self, "批次不存在", "批次记录不存在或无法读取")
            return
        running_manager = self.manager if target_batch_id == self.current_batch_id else self.load_manager_for_batch(target_batch_id)
        running_config = self.config_from_batch(batch)
        if not running_manager.tasks:
            QMessageBox.warning(self, "没有任务", "请先加载任务")
            return
        candidates = [
            task
            for task in running_manager.tasks
            if not selected_task_keys or (task.task_uid or TaskManager.task_uid_for(task)) in selected_task_keys
        ]
        v2_enabled = bool(getattr(running_config, "enable_stage_based_workflow", False))
        unfinished = [task for task in candidates if not TaskStatus.is_done(task.status)]
        if not v2_enabled and not poll_only and not failed_only and not unfinished:
            QMessageBox.information(self, "批次已完成", "该批次没有需要继续执行的任务")
            self.queue_batch_update(target_batch_id, running_manager)
            return
        if failed_only and not any(
            TaskStatus.is_failed_or_skipped(task.status)
            or any(str(state.get("status") or "") == "FAILED" for state in (task.node_states or {}).values() if isinstance(state, dict))
            for task in running_manager.tasks
        ):
            QMessageBox.information(self, "没有失败任务", "该批次没有需要重试的失败或跳过任务")
            return
        if not v2_enabled and execution_mode == "image_only" and not any(
            running_config.regenerate_existing_images or not (task.generated_image_path or task.generated_image_url)
            for task in candidates
            if not TaskStatus.is_done(task.status)
        ):
            QMessageBox.information(self, "没有待生成图片", "该批次没有需要生成图片的任务")
            return
        if not v2_enabled and execution_mode == "video_only" and not any(
            (task.generated_image_path or task.generated_image_url) and not task.video_task_id and not task.video_url
            for task in candidates
            if not TaskStatus.is_done(task.status)
        ):
            QMessageBox.information(self, "没有待提交视频", "当前没有可直接提交图生视频的任务")
            return
        if poll_only:
            if v2_enabled:
                pollable = [
                    task
                    for task in running_manager.tasks
                    if any(
                        isinstance(state, dict)
                        and state.get("task_id")
                        and not state.get("output_video_url")
                        and str(state.get("status") or "") in {"SUBMITTED", "POLLING", "RUNNING"}
                        for state in (task.node_states or {}).values()
                    )
                ]
            else:
                pollable = [
                    task
                    for task in running_manager.tasks
                    if task.status in {TaskStatus.VIDEO_SUBMITTED, TaskStatus.VIDEO_POLLING}
                    and task.video_task_id
                    and not task.video_url
                ]
            if not pollable:
                QMessageBox.information(self, "没有待轮询任务", "当前没有需要轮询的视频 task_id")
                return
        if poll_only or execution_mode == "video_only":
            if not running_config.video_api_key:
                QMessageBox.warning(self, "API Key 为空", "请先在参数配置中填写图生视频 API Key")
                return
        elif execution_mode == "image_only":
            if not running_config.image_api_key:
                QMessageBox.warning(self, "API Key 为空", "请先在参数配置中填写图生图 API Key")
                return
        elif not running_config.image_api_key or not running_config.video_api_key:
            QMessageBox.warning(self, "API Key 为空", "请先在参数配置中填写图生图和图生视频 API Key")
            return
        self.running_manager = running_manager
        self.running_config = running_config
        self.active_running_batch_id = target_batch_id
        self.active_running_status_override = "RUNNING"
        self.config.active_running_batch_id = target_batch_id
        save_config(self.config)
        if target_batch_id == self.current_batch_id:
            self.manager = running_manager
        self.worker = None
        self._start_process_worker(batch, running_manager, failed_only, poll_only, execution_mode, selected_task_keys)
        self._set_running_buttons(True)
        batch.status = "RUNNING"
        self.batch_card_batches[target_batch_id] = batch
        if target_batch_id == self.current_batch_id:
            self.current_batch = batch
        self.update_batch_card_styles()
        self.queue_batch_update(target_batch_id, running_manager, status_override="RUNNING")
        self.update_status_summary()
        self.show_status(f"正在执行批次：{batch.batch_name} / {target_batch_id}", log=True)
        return "BACKGROUND_STARTED"

    def custom_poll(self, button: QPushButton | None = None) -> None:
        self._sync_config_from_ui()
        if self.is_process_worker_running():
            self.send_process_worker_command("poll")
            self.append_log("INFO", "已触发后台进程立即轮询，结果会实时写入日志")
            self.show_success_toast("已通知后台执行进程立即轮询")
            return
        if self.worker and self.worker.isRunning():
            self.worker.request_poll_cycle()
            self.append_log("INFO", "已触发立即轮询，结果会实时写入日志")
            self.append_log("INFO", "已请求立即轮询当前所有未完成的视频任务；生成失败的任务默认自动重试")
            self.show_status("已触发立即轮询，轮询结果会直接进入日志", log=False)
            self.show_success_toast("已触发立即轮询，结果会进入日志")
            return
        self.append_log("INFO", "开始执行一轮立即轮询；生成失败的任务默认自动重试")
        self.run_with_feedback("启动继续轮询", lambda: self.start_worker(False, True), button)

    def manual_poll_batch(self, batch_id: str, button: QPushButton | None = None) -> None:
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            QMessageBox.warning(self, "没有批次", "请先选择批次")
            return
        if not self.config.enable_manual_poll_button:
            self.show_status("手动轮询按钮当前已在设置中关闭", log=True)
            return
        if self.manual_poll_worker and self.manual_poll_worker.isRunning():
            self.show_status("已有手动轮询正在执行，请等待完成后再点击", log=True)
            return
        self._sync_config_from_ui()
        batch = self.batch_manager.load_batch(batch_id)
        if not batch:
            QMessageBox.warning(self, "批次不存在", "批次记录不存在或无法读取")
            return
        manager = self.manager if batch_id == self.current_batch_id else self.load_manager_for_batch(batch_id)
        if batch_id == self.active_running_batch_id and self.running_manager is not None:
            manager = self.running_manager
        if not manager.tasks:
            QMessageBox.information(self, "没有任务", "该批次没有任务状态")
            return
        batch_config = self.config_from_batch(batch)
        if not batch_config.video_api_key:
            QMessageBox.warning(self, "API Key 为空", "请先在参数配置中填写图生视频 API Key")
            return
        statuses = {TaskStatus.VIDEO_SUBMITTED, TaskStatus.VIDEO_POLLING}
        if batch_config.manual_poll_include_timeout_tasks:
            statuses.add(TaskStatus.VIDEO_TIMEOUT)
        if batch_config.manual_poll_include_failed_tasks:
            statuses.add(TaskStatus.FAILED_VIDEO_API)
        candidates = [
            task
            for task in manager.tasks
            if task.video_task_id
            and not task.video_url
            and task.status in statuses
        ]
        if not candidates:
            QMessageBox.information(self, "没有可手动轮询任务", "当前批次没有已有 video_task_id 且未完成的视频任务")
            return
        self.manual_poll_batch_id = batch_id
        self.manual_poll_manager = manager
        loading_state = self.begin_loading("手动轮询当前批次", button)
        self.manual_poll_loading_state = None
        if batch_id == self.current_batch_id:
            self.append_log("INFO", f"[MANUAL_POLL] 开始手动轮询 batch_id={batch_id}，候选任务 {len(candidates)} 条")
        else:
            self.show_status(f"[MANUAL_POLL] 开始手动轮询 batch_id={batch_id}，候选任务 {len(candidates)} 条", log=False)
        logger = setup_logger(self.batch_manager.batch_dir(batch_id), self.batch_manager.log_path(batch_id))[0]
        self.manual_poll_worker = ManualPollWorker(
            manager,
            batch_config,
            logger,
            batch_id,
            polling_task_ids_in_progress=self.polling_task_ids_in_progress,
            polling_task_ids_lock=self.polling_task_ids_lock,
        )
        self.manual_poll_worker.task_updated.connect(self.update_manual_poll_task)
        self.manual_poll_worker.log_message.connect(self.append_manual_poll_log)
        self.manual_poll_worker.stats_updated.connect(lambda stats, bid=batch_id: self.update_manual_poll_stats(bid, stats))
        self.manual_poll_worker.finished_summary.connect(self.manual_poll_finished)
        try:
            self.manual_poll_worker.start()
        except Exception:
            self.end_loading(loading_state, "手动轮询启动失败")
            raise
        self.end_loading(loading_state, f"手动轮询已启动，候选任务 {len(candidates)} 条，后台会继续处理")
        self._set_running_buttons(self.is_background_running())

    def update_manual_poll_task(self, task: TaskItem) -> None:
        batch_id = self.manual_poll_batch_id
        manager = self.manual_poll_manager
        if not batch_id or manager is None:
            return
        self.queue_batch_update(
            batch_id,
            manager,
            self.active_running_status_override if batch_id == self.active_running_batch_id else None,
        )
        if batch_id == self.current_batch_id:
            self.manager = manager
            self.queue_table_refresh(task, manager.stats())

    def update_manual_poll_stats(self, batch_id: str, stats: dict) -> None:
        manager = self.manual_poll_manager
        if manager is not None:
            self.queue_batch_update(
                batch_id,
                manager,
                self.active_running_status_override if batch_id == self.active_running_batch_id else None,
            )
        if batch_id == self.current_batch_id:
            if manager is not None:
                self.manager = manager
            self._pending_stats = stats
            self._stats_refresh_pending = True
        self._batch_refresh_pending = True

    def append_manual_poll_log(self, level: str, message: str, task: object | None = None) -> None:
        if self.manual_poll_batch_id == self.current_batch_id:
            self.queue_log_line(level, message)
        else:
            self._background_log_counter += 1
            if self._background_log_counter % 50 == 0:
                self.show_status("后台手动轮询正在写入日志", timeout_ms=1800)

    def manual_poll_finished(self, summary: object) -> None:
        batch_id = getattr(summary, "batch_id", self.manual_poll_batch_id)
        manager = self.manual_poll_manager
        if manager is not None:
            self.batch_manager.update_batch(
                batch_id,
                manager.tasks,
                status_override=self.active_running_status_override if batch_id == self.active_running_batch_id else None,
            )
            if batch_id == self.current_batch_id:
                self.manager = manager
                self.refresh_table()
                self.update_stats(self.manager.stats())
        self.refresh_batch_combo()
        message = (
            f"手动轮询完成：检查 {getattr(summary, 'total_checked', 0)} 条，"
            f"完成 {getattr(summary, 'completed', 0)} 条，"
            f"仍在生成 {getattr(summary, 'still_processing', 0)} 条，"
            f"失败 {getattr(summary, 'failed', 0)} 条，"
            f"异常 {getattr(summary, 'error_count', 0)} 条"
        )
        if getattr(summary, "skipped_in_progress", 0):
            message += f"，跳过正在轮询 {getattr(summary, 'skipped_in_progress', 0)} 条"
        if batch_id == self.current_batch_id:
            self.append_log("INFO", f"[MANUAL_POLL] {message}")
        state = self.manual_poll_loading_state
        self.manual_poll_worker = None
        self.manual_poll_manager = None
        self.manual_poll_batch_id = ""
        self.manual_poll_loading_state = None
        if state:
            self.end_loading(state, message)
        else:
            self.show_status(message, log=True)
        self._set_running_buttons(self.is_background_running())

    def pause_worker(self) -> None:
        if self.is_process_worker_running():
            self.send_process_worker_command("pause")
            if self.active_running_batch_id and self.running_manager:
                self.active_running_status_override = "PAUSED"
                self.queue_batch_update(self.active_running_batch_id, self.running_manager, status_override="PAUSED")
                self.update_status_summary()
            self.show_status("已请求暂停后台执行批次", log=True)
            return
        if self.worker:
            self.worker.pause()
            if self.active_running_batch_id and self.running_manager:
                self.active_running_status_override = "PAUSED"
                self.batch_manager.update_batch(self.active_running_batch_id, self.running_manager.tasks, status_override="PAUSED")
                self.refresh_batch_combo()
                self.update_status_summary()
                self.show_status("已暂停当前执行批次", log=True)

    def resume_worker(self) -> None:
        if self.is_process_worker_running():
            self.send_process_worker_command("resume")
            if self.active_running_batch_id and self.running_manager:
                self.active_running_status_override = "RUNNING"
                self.queue_batch_update(self.active_running_batch_id, self.running_manager, status_override="RUNNING")
                self.update_status_summary()
            self.show_status("已请求继续后台执行批次", log=True)
            return
        if self.worker:
            self.worker.resume()
            if self.active_running_batch_id and self.running_manager:
                self.active_running_status_override = "RUNNING"
                self.batch_manager.update_batch(self.active_running_batch_id, self.running_manager.tasks, status_override="RUNNING")
                self.refresh_batch_combo()
                self.update_status_summary()
                self.show_status("已继续当前执行批次", log=True)

    def confirm_stop_worker(self) -> None:
        if not self.is_background_running():
            self.show_status("当前没有正在执行的批次")
            return
        answer = QMessageBox.question(
            self,
            "确认停止当前批次？",
            "停止后不会删除已生成结果，但会停止当前批次继续提交任务。\n\n确定要停止吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self.show_status("已取消停止操作", log=True)
            return
        self.append_log("WARNING", "用户确认停止当前执行批次")
        self.stop_worker(user_initiated=True)

    def stop_worker(self, user_initiated: bool = False) -> None:
        if user_initiated:
            self.run_queue_paused_by_user_stop = True
            if self.run_queue_count():
                self.show_status("已暂停运行队列：当前批次停止后不会自动启动下一批", log=True)
                self.show_warning_toast("运行队列已暂停，后续批次会继续留在队列中")
        if self.is_process_worker_running():
            self.send_process_worker_command("stop")
            self._schedule_process_worker_force_shutdown("停止请求")
            if self.active_running_batch_id and self.running_manager:
                self.active_running_status_override = "STOPPED"
                self.queue_batch_update(self.active_running_batch_id, self.running_manager, status_override="STOPPED")
                self.update_status_summary()
            self.show_status("已请求停止后台执行批次", log=True)
            return
        if self.worker:
            self.worker.stop()
            if self.active_running_batch_id and self.running_manager:
                self.active_running_status_override = "STOPPED"
                self.batch_manager.update_batch(self.active_running_batch_id, self.running_manager.tasks, status_override="STOPPED")
                self.refresh_batch_combo()
                self.update_status_summary()
                self.show_status("已请求停止当前执行批次", log=True)

    def worker_finished(self, message: str) -> None:
        self._set_running_buttons(False)
        finished_batch_id = self.active_running_batch_id
        if finished_batch_id == self.current_batch_id:
            self.append_log("INFO", message)
        else:
            self.show_status(message, log=False)
        # Friendly toast summary at run end. Use stats to pick the right tone.
        try:
            target_mgr = self.running_manager or self.manager
            if target_mgr is not None:
                stats = target_mgr.stats()
                done = int(stats.get("completed", 0))
                failed = int(stats.get("failed", 0))
                timeout = int(stats.get("timeout", 0))
                skipped = int(stats.get("skipped", 0))
                total = int(stats.get("total", 0))
                if total == 0:
                    pass
                elif failed == 0 and timeout == 0 and skipped == 0:
                    self.show_success_toast(f"批次执行完成，共完成 {done}/{total} 条 ✨")
                elif done > 0:
                    self.show_warning_toast(
                        f"批次执行结束：完成 {done}，失败 {failed}，超时 {timeout}，跳过 {skipped}（共 {total} 条）"
                    )
                else:
                    self.show_error_toast(
                        f"批次执行结束但成功条数为 0：失败 {failed}，超时 {timeout}，跳过 {skipped}（共 {total} 条）"
                    )
        except Exception:
            pass
        if finished_batch_id and self.running_manager:
            final_override = "STOPPED" if self.active_running_status_override == "STOPPED" else None
            self.batch_manager.update_batch(finished_batch_id, self.running_manager.tasks, status_override=final_override)
            if finished_batch_id == self.current_batch_id:
                self.manager = self.running_manager
                self.refresh_table()
                self.update_stats(self.manager.stats())
        if finished_batch_id:
            self._batch_card_live_stats_cache.pop(finished_batch_id, None)
        self.active_running_batch_id = ""
        self.active_running_status_override = None
        self.config.active_running_batch_id = ""
        save_config(self.config)
        self.running_manager = None
        self.running_config = None
        self.refresh_batch_combo()
        self.update_status_summary()
        if self.pending_api_switch_request:
            batch_id, override, restart = self.pending_api_switch_request
            self.pending_api_switch_request = None
            self.show_status("当前批次已停止，正在切换 API", log=True)
            if self.apply_runtime_api_switch(batch_id, override, restart=restart):
                return
        if self.start_next_queued_batch():
            return

    def update_running_stats(self, stats: dict) -> None:
        if self.active_running_batch_id:
            self._batch_card_live_stats_cache[self.active_running_batch_id] = dict(stats or {})
            self.apply_batch_card_stats_update(
                self.active_running_batch_id,
                stats,
                self.active_running_status_override,
            )
            if self.running_manager:
                self.queue_batch_update(
                    self.active_running_batch_id,
                    self.running_manager,
                    self.active_running_status_override,
                )
            self.update_status_summary()
        if self.active_running_batch_id == self.current_batch_id:
            self._pending_stats = stats
            self._stats_refresh_pending = True

    def export_results(self) -> None:
        state = self.begin_loading("导出 Excel", self.export_btn if hasattr(self, "export_btn") else None)
        self._sync_config_from_ui()
        try:
            output_dir = self.batch_manager.exports_dir(self.current_batch_id) if self.current_batch_id else exports_dir(self.config)
            path = self.manager.export_excel(
                output_dir,
                visible_columns=self.current_visible_columns(),
                only_visible_columns=self.config.export_only_visible_columns,
                filtered_keys=self.filtered_task_keys,
                group_by_fields=self.group_by_fields,
            )
            self.update_current_batch_info()
            self.append_log("INFO", f"结果已导出: {path}")
            self.end_loading(state, "结果导出完成")
            QMessageBox.information(self, "导出完成", path)
        except Exception as exc:
            self.end_loading(state, "结果导出失败")
            self.append_log("ERROR", f"导出失败: {exc}")
            QMessageBox.critical(self, "导出失败", str(exc))

    def download_selected_video(self) -> None:
        self._sync_config_from_ui()
        tasks = [task for task in self._selected_tasks() if task.video_url]
        if not tasks:
            QMessageBox.warning(self, "未选择任务", "请先选择已有视频链接的任务")
            return
        base_dir = QFileDialog.getExistingDirectory(self, "选择视频下载目录", str(self.config.video_download_root))
        if not base_dir:
            return
        state = self.begin_loading("下载选中任务视频", self.download_video_btn if hasattr(self, "download_video_btn") else None)
        group_by_owner = self.group_by_owner_check.isChecked()
        ok = skipped = failed = 0
        try:
            workers = max(1, min(int(self.config.download_concurrency), 50, len(tasks)))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {executor.submit(self._download_task_video, task, base_dir, group_by_owner): task for task in tasks}
                for future in self._iter_completed_futures(future_map):
                    task = future_map[future]
                    try:
                        path, downloaded = future.result()
                        ok += 1 if downloaded else 0
                        skipped += 0 if downloaded else 1
                        self.append_log("INFO", f"PID={task.pid} row={task.row_index} {'视频已下载' if downloaded else '视频已存在，跳过下载'}: {path}")
                    except Exception as exc:
                        failed += 1
                        task.error_message = f"视频下载失败: {exc}"
                        self.append_log("ERROR", f"PID={task.pid} row={task.row_index} 下载视频失败: {exc}")
                    QApplication.processEvents()
            self.manager.save_state()
            self.update_current_batch_info()
            self.refresh_table()
            self.end_loading(state, "选中任务视频下载完成")
            QMessageBox.information(self, "下载完成", f"新下载 {ok} 个，已存在跳过 {skipped} 个，失败 {failed} 个")
        except Exception:
            self.end_loading(state, "选中任务视频下载失败")
            raise

    def _iter_completed_futures(self, future_map: dict):
        pending = set(future_map)
        while pending:
            done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            if not done:
                QApplication.processEvents()
                continue
            for future in done:
                yield future
            QApplication.processEvents()

    def _download_task_video(self, task: TaskItem, base_dir: str | None = None, group_by_owner: bool | None = None) -> tuple[str, bool]:
        if base_dir:
            save_path = selected_video_output_path(base_dir, task, bool(group_by_owner))
        else:
            save_path = video_download_output_path(self.config.video_download_root, task, self.config.group_by_owner)
        if save_path.exists() and save_path.stat().st_size > 0:
            ok, message = validate_downloaded_file(save_path, expected_kind="video")
            if ok:
                task.video_file_path = str(save_path)
                task.video_download_status = "DOWNLOADED"
                task.last_video_download_error = None
                sync_task_urls_from_paths(task, self.config)
                task.set_status(TaskStatus.VIDEO_DOWNLOADED)
                return str(save_path), False
            try:
                save_path.unlink()
            except OSError:
                pass
            task.last_video_download_error = f"已有视频文件校验失败，已重新下载：{message}"
        task.video_download_status = "DOWNLOADING"
        task.last_video_download_time = now_text()
        task.video_download_attempt_count += 1
        provider = get_video_provider(task.video_provider or self.config.video_provider)
        downloader = getattr(provider, "download_video_content", None)
        if callable(downloader) and task.video_task_id and str(task.video_url or "").rstrip("/").endswith("/content"):
            path = str(
                downloader(
                    task_id=task.video_task_id,
                    api_key=self.config.video_api_key,
                    output_path=save_path,
                    extra_params={
                        "base_url": self.config.video_api_base_url,
                        "timeout": max(180, int(self.config.request_timeout_seconds)),
                    },
                )
            )
            downloaded = True
        else:
            path, downloaded = download_video_to_path(
                task.video_url or "",
                save_path,
                timeout=max(180, int(self.config.request_timeout_seconds)),
                retry_count=max(1, int(self.config.retry_count)),
                retry_interval_seconds=max(1, int(self.config.retry_interval_seconds)),
            )
        task.video_file_path = path
        task.video_download_status = "DOWNLOADED"
        task.last_video_download_error = None
        sync_task_urls_from_paths(task, self.config)
        task.set_status(TaskStatus.VIDEO_DOWNLOADED)
        return path, downloaded

    def download_selected_images(self) -> None:
        tasks = [task for task in self._selected_tasks() if task.generated_image_path or task.product_image_path]
        if not tasks:
            QMessageBox.warning(self, "未选择任务", "请先选择已有图片资料的任务")
            return
        base_dir = QFileDialog.getExistingDirectory(self, "选择图片资料下载目录", str(self.config.image_assets_root))
        if not base_dir:
            return
        state = self.begin_loading("下载选中任务图片资料", self.download_images_btn if hasattr(self, "download_images_btn") else None)
        group_by_owner = self.group_by_owner_check.isChecked()
        copied = failed = 0
        try:
            for task in tasks:
                try:
                    folder = self._copy_image_assets_to_dir(task, base_dir, group_by_owner)
                    copied += 1
                    self.append_log("INFO", f"PID={task.pid} row={task.row_index} 图片资料已保存: {folder}")
                except Exception as exc:
                    failed += 1
                    task.error_message = f"图片资料下载失败: {exc}"
                    self.append_log("ERROR", f"PID={task.pid} row={task.row_index} 图片资料下载失败: {exc}")
                QApplication.processEvents()
            self.manager.save_state()
            self.update_current_batch_info()
            self.refresh_table()
            self.end_loading(state, "选中任务图片资料下载完成")
            QMessageBox.information(self, "下载完成", f"已处理 {copied} 条，失败 {failed} 条")
        except Exception:
            self.end_loading(state, "选中任务图片资料下载失败")
            raise

    def _copy_image_assets_to_dir(self, task: TaskItem, base_dir: str | Path, group_by_owner: bool = False) -> Path:
        folder = selected_image_assets_dir(base_dir, task, group_by_owner)
        pid = safe_pid(task.pid)
        row = task.row_index
        product = Path(str(task.product_image_path or ""))
        if product.exists() and product.is_file():
            suffix = product.suffix or ".png"
            shutil.copy2(product, folder / f"{pid}_row{row}_product_image{suffix}")
        generated = Path(str(task.generated_image_path or ""))
        if generated.exists() and generated.is_file():
            shutil.copy2(generated, folder / f"{pid}_row{row}_generated_image{generated.suffix or '.png'}")
        (folder / f"{pid}_row{row}_image_prompt.txt").write_text(task.image_prompt or "", encoding="utf-8")
        (folder / f"{pid}_row{row}_video_prompt.txt").write_text(task.video_prompt or "", encoding="utf-8")
        (folder / f"{pid}_row{row}_metadata.json").write_text(json.dumps(task_metadata(task), ensure_ascii=False, indent=2), encoding="utf-8")
        return folder

    def _selected_tasks(self) -> list[TaskItem]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        tasks = []
        for row in rows:
            task = self._task_for_display_row(row)
            if task is not None:
                tasks.append(task)
        return tasks

    @staticmethod
    def _task_needs_retry(task: TaskItem) -> bool:
        if TaskStatus.is_failed_or_skipped(task.status):
            return True
        return any(
            isinstance(state, dict) and str(state.get("status") or "") == "FAILED"
            for state in (task.node_states or {}).values()
        )

    def check_tasks(self) -> None:
        self._sync_config_from_ui()
        tasks = list(self.manager.tasks)
        if not tasks:
            QMessageBox.information(self, "没有任务", "请先加载任务")
            return

        state = self.begin_loading("检查任务", self.check_btn if hasattr(self, "check_btn") else None)
        checked = 0
        try:
            workers = max(1, min(int(self.config.image_concurrency), 100, len(tasks)))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {executor.submit(find_product_image_for_task, task, self.config): task for task in tasks}
                for future in self._iter_completed_futures(future_map):
                    task = future_map[future]
                    try:
                        image, status, error = future.result()
                        task.product_image_path = image
                        sync_task_urls_from_paths(task, self.config, prefer_local_generated=False, prefer_local_video=False)
                        if status:
                            task.status = status
                            task.error_message = error
                        checked += 1
                    except Exception as exc:
                        task.status = TaskStatus.FAILED_UNKNOWN
                        task.error_message = f"检查任务失败: {exc}"
                    QApplication.processEvents()
            self.manager.save_state()
            self.update_current_batch_info()
            self.refresh_table()
            self.update_stats(self.manager.stats())
            self.append_log("INFO", f"检查任务完成: {checked} 条，并发 {workers}")
            self.end_loading(state, f"检查任务完成: {checked} 条")
        except Exception:
            self.end_loading(state, "检查任务失败")
            raise

    def batch_download_videos(self) -> None:
        base_dir = None
        answer = QMessageBox.question(self, "批量下载路径", "是否选择一个统一下载目录？选择后会按 PID 创建子目录。")
        if answer == QMessageBox.Yes:
            chosen = QFileDialog.getExistingDirectory(self, "选择批量下载目录", str(self.config.video_download_root))
            if chosen:
                base_dir = chosen

        tasks = [task for task in self.manager.tasks if task.video_url]
        if not tasks:
            QMessageBox.information(self, "没有视频链接", "当前没有可下载的视频链接")
            return

        state = self.begin_loading("下载全部视频", self.batch_download_video_btn if hasattr(self, "batch_download_video_btn") else None)
        ok = 0
        skipped = 0
        failed = 0
        group_by_owner = self.group_by_owner_check.isChecked()
        try:
            workers = max(1, min(int(self.config.download_concurrency), 50, len(tasks)))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {executor.submit(self._download_task_video, task, base_dir, group_by_owner): task for task in tasks}
                for future in self._iter_completed_futures(future_map):
                    task = future_map[future]
                    try:
                        path, downloaded = future.result()
                        if downloaded:
                            ok += 1
                        else:
                            skipped += 1
                        self.append_log("INFO", f"PID={task.pid} row={task.row_index} {'视频已下载' if downloaded else '视频已存在，跳过下载'}: {path}")
                    except Exception as exc:
                        failed += 1
                        task.error_message = f"视频下载失败: {exc}"
                        self.append_log("ERROR", f"PID={task.pid} row={task.row_index} 下载视频失败: {exc}")
                    QApplication.processEvents()
            self.manager.save_state()
            self.update_current_batch_info()
            self.refresh_table()
            self.update_stats(self.manager.stats())
            self.end_loading(state, "全部视频下载完成")
            QMessageBox.information(self, "批量下载完成", f"新下载 {ok} 个，已存在跳过 {skipped} 个，失败 {failed} 个")
        except Exception:
            self.end_loading(state, "全部视频下载失败")
            raise

    def refresh_table(self) -> None:
        with self.ui_diag.measure("refresh_table",
            task_count=len(getattr(self.manager, "tasks", []) or []),
            display_rows=len(getattr(self, "display_rows", []) or []),
            group_enabled=bool(self.group_enabled_check.isChecked()) if hasattr(self, "group_enabled_check") else False,
            active_filters=len(getattr(self, "active_filters", {}) or {}),
            active_threads=threading.active_count(),
        ):
            self._refresh_table_impl()

    def _refresh_table_impl(self) -> None:
        tasks = self.filtered_tasks()
        self.filtered_task_keys = {(task.row_index, task.pid) for task in tasks}
        self.display_rows = []
        self.table.setUpdatesEnabled(False)
        try:
            self.table.clearSpans()
            self.table.setRowCount(0)
            if self.group_enabled_check.isChecked() and self.group_by_fields:
                self._append_group_rows(tasks, self.group_by_fields, [], 0)
            else:
                for task in tasks:
                    self.display_rows.append({"type": "task", "task": task})

            self.table.setRowCount(len(self.display_rows))
            for row, row_data in enumerate(self.display_rows):
                if row_data["type"] == "group":
                    self._fill_group_row(row, row_data)
                else:
                    self._fill_row(row, row_data["task"])
            self.apply_column_visibility()
            self.ensure_table_scrollbar()
        finally:
            self.table.setUpdatesEnabled(True)
            self.table.viewport().update()
        self.update_filtered_stats(tasks)
        self.update_filter_group_labels()
        self._update_table_empty_state(tasks)

    def _update_table_empty_state(self, tasks: list) -> None:
        """Show a friendly placeholder above the task table when nothing is visible.

        Uses a one-shot QLabel overlay that we keep in sync with the
        viewport size. Reuses the existing widget if it already exists.
        """
        if not bool(getattr(self.config, "enable_empty_state_guidance", True)):
            return
        if not hasattr(self, "_task_empty_overlay"):
            self._task_empty_overlay = QLabel(self.table.viewport())
            self._task_empty_overlay.setAlignment(Qt.AlignCenter)
            self._task_empty_overlay.setWordWrap(True)
            self._task_empty_overlay.setStyleSheet(
                "QLabel { color: #8a92a5; background-color: rgba(16,20,29,210); "
                "border: 1px dashed #2a3142; border-radius: 8px; padding: 24px; "
                "font-size: 13px; }"
            )
            self._task_empty_overlay.hide()
            self.table.viewport().installEventFilter(self)
        if len(self.display_rows) == 0:
            has_filter = bool(self.active_filters) or any(self.group_collapsed)
            if has_filter:
                text = (
                    "当前筛选 / 分组下没有任务可显示\n"
                    "你可以点击顶部「清除全部筛选」恢复默认视图"
                )
            elif self.manager.tasks:
                text = "当前批次的所有任务都被过滤掉了\n试试调整筛选或分组设置"
            else:
                text = (
                    "当前批次还没有任务\n"
                    "你可以导入任务（顶部「导入新批次」），或切换到其他批次查看历史记录"
                )
            self._task_empty_overlay.setText(text)
            self._position_table_empty_overlay()
            self._task_empty_overlay.show()
            self._task_empty_overlay.raise_()
        else:
            self._task_empty_overlay.hide()

    def _position_table_empty_overlay(self) -> None:
        if not hasattr(self, "_task_empty_overlay"):
            return
        viewport = self.table.viewport()
        w = max(280, min(640, viewport.width() - 60))
        h = 130
        x = max(10, (viewport.width() - w) // 2)
        y = max(10, (viewport.height() - h) // 2)
        self._task_empty_overlay.setGeometry(x, y, w, h)

    def update_task_row(self, task: TaskItem) -> None:
        if self.active_running_batch_id == self.current_batch_id:
            self.queue_table_refresh(task)
        if self.active_running_batch_id and self.running_manager:
            self.queue_batch_update(
                self.active_running_batch_id,
                self.running_manager,
                self.active_running_status_override,
            )

    def filtered_tasks(self) -> list[TaskItem]:
        keyword = self.filter_text_edit.text().strip().lower() if hasattr(self, "filter_text_edit") else ""
        column = int(self.filter_column_combo.currentData()) if hasattr(self, "filter_column_combo") else -1
        result = []
        for task in self.manager.tasks:
            if keyword:
                if column >= 0:
                    matched = keyword in self.task_field_value(task, TABLE_HEADERS[column]).lower()
                else:
                    matched = any(keyword in self.task_field_value(task, header).lower() for header in TABLE_HEADERS)
                if not matched:
                    continue
            if not self.task_matches_active_filters(task):
                continue
            result.append(task)
        for field, descending in reversed(getattr(self, "sort_rules", [])):
            result.sort(key=lambda task, f=field: self.task_field_value(task, f), reverse=descending)
        return result

    def task_matches_active_filters(self, task: TaskItem) -> bool:
        for field, values in self.active_filters.items():
            if not values:
                continue
            if self.task_field_value(task, field) not in values:
                return False
        return True

    def _append_group_rows(self, tasks: list[TaskItem], fields: list[str], path: list[str], level: int) -> None:
        if not fields:
            for task in tasks:
                self.display_rows.append({"type": "task", "task": task, "level": level})
            return
        field = fields[0]
        grouped: dict[str, list[TaskItem]] = {}
        for task in tasks:
            grouped.setdefault(self.task_field_value(task, field) or "空", []).append(task)
        for value in sorted(grouped):
            group_path = path + [f"{field}:{value}"]
            key = "||".join(group_path)
            self.display_rows.append(
                {
                    "type": "group",
                    "key": key,
                    "title": f"{'  ' * level}{field}：{value}",
                    "tasks": grouped[value],
                    "level": level,
                }
            )
            if key not in self.group_collapsed:
                self._append_group_rows(grouped[value], fields[1:], group_path, level + 1)

    def _fill_group_row(self, row: int, row_data: dict) -> None:
        tasks = row_data["tasks"]
        stats = self.calc_task_stats(tasks)
        folded = row_data["key"] in self.group_collapsed
        prefix = "▶" if folded else "▼"
        title = (
            f"{prefix} {row_data['title']}｜总任务 {stats['total']}｜完成 {stats['completed']}｜失败 {stats['failed']}｜"
            f"跳过 {stats['skipped']}｜轮询中 {stats['polling']}｜完成率 {stats['completion_rate']:.1f}%｜成功率 {stats['success_rate']:.1f}%"
        )
        self.table.setSpan(row, 0, 1, self.table.columnCount())
        item = QTableWidgetItem(title)
        item.setBackground(QColor("#0f2d4d"))
        item.setForeground(QBrush(QColor("#ffffff")))
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(row, 0, item)

    def _stage_nodes_for_task(self, task: TaskItem | None = None) -> list[tuple[str, str, str, str]]:
        batch = self.current_batch
        if task is not None and task.batch_id and (batch is None or task.batch_id != batch.batch_id):
            try:
                batch = self.batch_manager.load_batch(task.batch_id)
            except Exception:
                batch = None
        if batch and isinstance(batch.workflow_definition, dict):
            return stage_nodes_from_workflow_definition(batch.workflow_definition)
        return list(STAGE_NODES)

    def _fill_row(self, row: int, task: TaskItem) -> None:
        # Per-stage cell content (icon + short label). Render the node statuses
        # directly so the user can see at a glance which阶段 is done / blocked.
        stage_nodes = self._stage_nodes_for_task(task)
        stage_cells = workflow_stage_cells(task, stage_nodes, max_columns=4)
        flow_text = task_flow_summary(task, stage_nodes)
        friendly_overall = task_overall_status_display(task, stage_nodes)
        values = [
            getattr(task, "task_name", "") or "",
            task.pid,
            task.owner,
            flow_text,
            stage_cells[0][0],
            stage_cells[1][0],
            stage_cells[2][0],
            stage_cells[3][0],
            task.netdisk_path,
            task.netdisk_http_path or "",
            task.product_image_path or "",
            task.product_image_url or "",
            getattr(task, "product_image_filename", "") or "",
            self._summary(task.image_prompt),
            self._summary(task.video_prompt),
            task.image_provider,
            task.image_model_display or task.image_model_logical_key,
            friendly_status_text(task.image_status) if task.image_status else ("已找到" if task.generated_image_path or task.generated_image_url else ""),
            task.generated_image_path or "",
            task.generated_image_url or "",
            task.video_provider,
            task.video_model_display or task.video_model_logical_key,
            task.video_task_id or "",
            str(task.video_poll_count),
            str(task.manual_poll_count),
            task.last_manual_poll_time or "",
            task.last_manual_poll_result or "",
            friendly_status_text(task.video_status) if task.video_status else "",
            task.video_url or "",
            task.video_file_path or "",
            task.video_download_status or "",
            str(task.video_download_attempt_count),
            task.last_video_download_error or "",
            friendly_overall,
            task.error_message or "",
            task.task_added_date or "",
            task.batch_id or "",
            task.started_at or "",
            task.ended_at or "",
            "" if task.elapsed_seconds is None else str(task.elapsed_seconds),
        ]
        status_color = STATUS_COLORS.get(task.status, QColor("#2c2f38"))
        configured_text = QColor(self.config.table_text_color or "")
        if configured_text.isValid() and self.config.table_text_color.lower() not in {"#111111", "#000000"}:
            text_color = configured_text
        else:
            text_color = QColor("#eef2ff")
        base_color = QColor("#10141d") if row % 2 == 0 else QColor("#151a24")
        stage_col_indexes = {
            COL_INDEX[f"阶段{idx + 1}"]: idx for idx in range(4) if f"阶段{idx + 1}" in COL_INDEX
        }
        for col, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setBackground(base_color)
            item.setForeground(QBrush(text_color))
            if col == COL_INDEX["PID"]:
                item.setForeground(QBrush(QColor("#7ab8ff")))
            # Per-stage column: colour-code the cell by node status + tooltip
            # with input/output/error so hovering tells the full story.
            if col in stage_col_indexes:
                stage_idx = stage_col_indexes[col]
                _display, status, node_id, short_label, node_name, prompt_attr = stage_cells[stage_idx]
                if status:
                    item.setBackground(stage_status_color(status))
                    item.setForeground(QBrush(QColor("#ffffff")))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                if node_id:
                    item.setToolTip(self._stage_tooltip(task, node_id, short_label, node_name, prompt_attr))
            if col == COL_INDEX["任务状态"] and value:
                item.setBackground(status_color)
                item.setForeground(QBrush(QColor("#ffffff")))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                hint = friendly_status_hint(task.status)
                tooltip = friendly_overall
                if task.error_message:
                    tooltip += f"\n\n错误信息：{task.error_message}"
                if hint:
                    tooltip += f"\n\n💡 建议：{hint}"
                item.setToolTip(tooltip)
            elif col in {COL_INDEX["图生图状态"], COL_INDEX["视频状态"]} and value:
                item.setBackground(status_color)
                item.setForeground(QBrush(QColor("#ffffff")))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            elif col == COL_INDEX["流程进度"]:
                item.setToolTip(self._task_flow_tooltip(task))
            elif col == COL_INDEX["错误信息"] and task.error_message:
                hint = friendly_status_hint(task.status)
                tooltip = task.error_message
                if hint:
                    tooltip += f"\n\n💡 建议：{hint}"
                item.setToolTip(tooltip)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, col, item)

    def _stage_tooltip(self, task: TaskItem, node_id: str, short_label: str, node_name: str, prompt_attr: str) -> str:
        state = task_node_state(task, node_id)
        status = str(state.get("status") or "")
        lines = [f"{short_label} · {node_name}"]
        lines.append(f"状态：{stage_status_icon(status)} {workflow_stage_status_label(task, node_id) or '待执行'}")
        prompt_value = str(getattr(task, prompt_attr, "") or "").strip()
        if prompt_value:
            lines.append(f"提示词：{self._summary(prompt_value, 60)}")
        else:
            lines.append("提示词：（未填写）")
        inputs = state.get("input_images") or []
        if inputs:
            lines.append("输入图：")
            for value in inputs[:3]:
                lines.append(f"  · {self._summary(str(value), 80)}")
            if len(inputs) > 3:
                lines.append(f"  · …共 {len(inputs)} 张")
        if state.get("output_image_path") or state.get("output_image_url"):
            lines.append(f"输出图：{state.get('output_image_path') or state.get('output_image_url')}")
        if state.get("output_video_url"):
            lines.append(f"视频链接：{state.get('output_video_url')}")
        if state.get("output_video_local_path"):
            lines.append(f"视频本地：{state.get('output_video_local_path')}")
        if state.get("task_id"):
            lines.append(f"task_id：{state.get('task_id')}")
        if state.get("poll_count"):
            lines.append(f"自动轮询次数：{state.get('poll_count')}")
        error = state.get("error_message")
        if error:
            lines.append(f"\n错误：{error}")
            if status == "BLOCKED":
                lines.append("💡 完成上一步后，这里会自动变为可执行；也可以右键手动指定输入图")
            elif status == "WAITING_INPUT":
                lines.append("💡 请先补全提示词，或为该节点手动指定输入图")
            elif status == "FAILED":
                lines.append("💡 右键 → 重试此节点 重新跑一次")
        elif status == "BLOCKED":
            lines.append("\n💡 此节点依赖前置图片结果，完成上一步后会自动变为可执行")
        elif status == "WAITING_INPUT":
            lines.append("\n💡 缺少输入图片或提示词")
        return "\n".join(lines)

    def _task_flow_tooltip(self, task: TaskItem) -> str:
        lines = [f"PID {task.pid}  ·  row {task.row_index}", ""]
        for node_id, short_label, node_name, _ in self._stage_nodes_for_task(task):
            state = task_node_state(task, node_id)
            status = str(state.get("status") or "")
            display = friendly_status_text(status) or "待执行"
            lines.append(f"{short_label}  {stage_status_icon(status)}  {display}  —  {node_name}")
        return "\n".join(lines)

    def task_field_value(self, task: TaskItem, field: str) -> str:
        # v2 stage columns first — `阶段1..4` and `流程进度`. Returned as the
        # display string (icon + label) so filter/search by "阶段3 失败" works
        # naturally even though there's no underlying flat attribute.
        stage_nodes = self._stage_nodes_for_task(task)
        for idx, (node_id, _short_label, _name, _attr) in enumerate(stage_nodes[:4]):
            if field == f"阶段{idx + 1}":
                state = task_node_state(task, node_id)
                status = str(state.get("status") or "")
                label = workflow_stage_status_label(task, node_id) if status else "待执行"
                return f"{stage_status_icon(status)} {label}".strip()
        if field in {"阶段1", "阶段2", "阶段3", "阶段4"}:
            return ""
        if field == "流程进度":
            return task_flow_summary(task, stage_nodes)
        mapping = {
            "任务名称": getattr(task, "task_name", "") or "",
            "PID": task.pid,
            "负责人": task.owner,
            "网盘路径": task.netdisk_path,
            "Http路径": task.netdisk_http_path,
            "产品白底图路径": task.product_image_path or "",
            "产品白底图URL": task.product_image_url or "",
            "指定白底图文件名": getattr(task, "product_image_filename", "") or "",
            "图片提示词": task.image_prompt,
            "视频提示词": task.video_prompt,
            "图生图平台": task.image_provider,
            "图生图模型": task.image_model_display or task.image_model_logical_key,
            "图生图状态": task.image_status,
            "生成图片路径": task.generated_image_path or "",
            "生成图片URL": task.generated_image_url or "",
            "图生视频平台": task.video_provider,
            "图生视频模型": task.video_model_display or task.video_model_logical_key,
            "video_task_id": task.video_task_id or "",
            "视频轮询次数": str(task.video_poll_count),
            "手动轮询次数": str(task.manual_poll_count),
            "最后手动轮询时间": task.last_manual_poll_time or "",
            "最后手动轮询结果": task.last_manual_poll_result or "",
            "视频状态": task.video_status,
            "视频链接": task.video_url or "",
            "视频本地路径": task.video_file_path or "",
            "视频下载状态": task.video_download_status or "",
            "视频下载尝试次数": str(task.video_download_attempt_count),
            "最后视频下载错误": task.last_video_download_error or "",
            "任务状态": task_overall_status_display(task, stage_nodes),
            "错误信息": task.error_message or "",
            "任务添加日期": task.task_added_date,
            "批次ID": task.batch_id,
            "开始时间": task.started_at or "",
            "结束时间": task.ended_at or "",
            "耗时秒数": "" if task.elapsed_seconds is None else str(task.elapsed_seconds),
            "是否有视频链接": "是" if task.video_url else "否",
            "是否有错误信息": "是" if task.error_message else "否",
        }
        return str(mapping.get(field, ""))

    def calc_task_stats(self, tasks: list[TaskItem]) -> dict[str, float | int]:
        total = len(tasks)
        completed = sum(1 for t in tasks if TaskStatus.is_done(t.status))
        failed = sum(1 for t in tasks if t.status.startswith("FAILED") or t.status == TaskStatus.VIDEO_FAILED)
        skipped = sum(1 for t in tasks if t.status.startswith("SKIPPED"))
        timeout = sum(1 for t in tasks if t.status == TaskStatus.VIDEO_TIMEOUT)
        polling = sum(1 for t in tasks if t.status == TaskStatus.VIDEO_POLLING)
        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "timeout": timeout,
            "polling": polling,
            "completion_rate": completed / total * 100 if total else 0,
            "success_rate": completed / total * 100 if total else 0,
        }

    def update_filtered_stats(self, tasks: list[TaskItem]) -> None:
        stats = self.calc_task_stats(tasks)
        text = (
            f"显示: {stats['total']}/{len(self.manager.tasks)}｜完成 {stats['completed']}｜失败 {stats['failed']}｜"
            f"跳过 {stats['skipped']}｜轮询中 {stats['polling']}｜完成率 {stats['completion_rate']:.1f}%｜成功率 {stats['success_rate']:.1f}%"
        )
        self.filter_result_label.setText(text)

    def update_stats(self, stats: dict, update_batch: bool = True) -> None:
        stats = self._stable_dashboard_stats(stats)
        if update_batch:
            self.update_current_batch_info()
        labels = {
            "total": "总任务数",
            "pending": "待提交",
            "image_running": "图片生成中",
            "submitted": "视频已提交",
            "polling": "视频轮询中",
            "completed": "已完成",
            "failed": "失败",
            "timeout": "超时",
            "skipped": "已跳过",
        }
        for key, text in labels.items():
            if key in self.stat_labels:
                self.stat_labels[key].setText(f"{text}: {stats.get(key, 0)}")
        total = int(stats.get("total", 0) or 0)
        completed = int(stats.get("completed", 0) or 0)
        failed = int(stats.get("failed", 0) or 0)
        timeout = int(stats.get("timeout", 0) or 0)
        skipped = int(stats.get("skipped", 0) or 0)
        remaining = max(total - completed, 0)
        progress_rate = (completed / total * 100) if total else 0
        success_rate = (completed / total * 100) if total else 0
        if self.current_batch and hasattr(self, "stat_labels"):
            batch_labels = {
                "batch_name": f"当前批次: {self.current_batch.batch_name}",
                "batch_id": f"批次ID: {self.current_batch.batch_id}",
                "imported_at": f"导入时间: {self.current_batch.imported_at}",
                "progress": f"完成百分比: {progress_rate:.2f}%",
                "success_rate": f"成功率: {success_rate:.2f}%",
            }
            for key, text in batch_labels.items():
                if key in self.stat_labels:
                    self.stat_labels[key].setText(text)
        if hasattr(self, "metric_labels"):
            if "progress" in self.metric_labels:
                self.metric_labels["progress"].setText(f"{progress_rate:.1f}%")
            if "concurrency" in self.metric_labels:
                active = int(stats.get("running", 0) or stats.get("image_running", 0) or 0)
                self.metric_labels["concurrency"].setText(f"{active}/{self.config.concurrency}")
            if "pending" in self.metric_labels:
                self.metric_labels["pending"].setText(str(remaining))
            if "failed" in self.metric_labels:
                self.metric_labels["failed"].setText(str(failed + timeout))
            if "total" in self.metric_labels:
                self.metric_labels["total"].setText(str(total))
        if hasattr(self, "current_batch_status_label"):
            self.current_batch_status_label.setText(self.current_batch_status_text(total, completed, failed, timeout, skipped))

    def _stable_dashboard_stats(self, stats: dict) -> dict:
        batch_id = self.current_batch_id or ""
        same_batch = bool(batch_id and batch_id == self._dashboard_stats_batch_id)
        active_running = bool(batch_id and batch_id == self.active_running_batch_id and self.is_background_running())
        stable = stabilize_live_completion_stats(
            self._dashboard_stats_snapshot if same_batch else None,
            stats,
            same_batch=same_batch,
            active_running=active_running,
        )
        self._dashboard_stats_batch_id = batch_id
        self._dashboard_stats_snapshot = dict(stable)
        return stable

    def _stable_batch_card_stats(self, batch_id: str, batch: TaskBatch) -> TaskBatch:
        active_running = bool(batch_id and batch_id == self.active_running_batch_id and self.is_background_running())
        previous = self._batch_card_stats_snapshots.get(batch_id)
        incoming = {
            "total": batch.task_count,
            "completed": batch.completed_count,
            "failed": batch.failed_count,
            "skipped": batch.skipped_count,
            "timeout": batch.timeout_count,
        }
        stable = stabilize_live_completion_stats(
            previous,
            incoming,
            same_batch=bool(previous),
            active_running=active_running,
        )
        if active_running:
            self._batch_card_stats_snapshots[batch_id] = dict(stable)
        else:
            self._batch_card_stats_snapshots.pop(batch_id, None)

        total = max(0, int(stable.get("total") or batch.task_count or 0))
        completed = min(max(0, int(stable.get("completed") or 0)), total) if total else 0
        remaining = max(0, total - completed)
        failed = min(max(0, int(stable.get("failed") or 0)), remaining)
        remaining = max(0, remaining - failed)
        skipped = min(max(0, int(stable.get("skipped") or 0)), remaining)
        remaining = max(0, remaining - skipped)
        timeout = min(max(0, int(stable.get("timeout") or 0)), remaining)

        batch.task_count = total
        batch.completed_count = completed
        batch.failed_count = failed
        batch.skipped_count = skipped
        batch.timeout_count = timeout
        batch.progress_percent = round(completed / total * 100, 2) if total else 0.0
        batch.success_rate = batch.progress_percent
        return batch

    def append_log(self, level: str, message: str, task: object | None = None) -> None:
        self._add_ui_log_line(level, message)

    @staticmethod
    def _parse_log_line(line: str) -> tuple[str, str]:
        text = str(line or "")
        stamped = re.match(r"^\[[^\]]+\]\s+\[([A-Z]+)\]\s*(.*)$", text)
        if stamped:
            return stamped.group(1).strip().upper() or "INFO", stamped.group(2).strip()
        if text.startswith("[") and "]" in text:
            level, message = text[1:].split("]", 1)
            return level.strip().upper() or "INFO", message.strip()
        return "INFO", text

    def _set_ui_log_entries_from_text(self, text: str) -> None:
        lines = [line for line in str(text or "").splitlines() if line.strip()]
        max_lines = max(400, int((getattr(self.config, "log_panel", {}) or {}).get("max_visible_lines", 100)) * 8)
        self._ui_log_entries = [self._parse_log_line(line) for line in lines[-max_lines:]]
        self._update_log_status_label()
        if hasattr(self, "log_text"):
            self.refresh_log_panel()

    def _update_log_status_label(self) -> None:
        if not hasattr(self, "log_status_label"):
            return
        entries = list(getattr(self, "_ui_log_entries", []) or [])
        if not entries:
            self.log_status_label.setText("最近日志：暂无")
            return
        level, message = entries[-1]
        self.log_status_label.setText(f"最近日志：[{level}] {message[:160]}")

    def _visible_log_levels(self) -> set[str]:
        if not hasattr(self, "log_level_checks"):
            return {"INFO", "WARN", "WARNING", "ERROR"}
        levels = {level for level, check in self.log_level_checks.items() if check.isChecked()}
        if "WARN" in levels:
            levels.add("WARNING")
        return levels

    def _add_ui_log_line(self, level: str, message: str) -> None:
        level = str(level or "INFO").upper()
        message = str(message or "")
        if not hasattr(self, "_ui_log_entries"):
            self._ui_log_entries = []
        self._ui_log_entries.append((level, message))
        max_lines = int((getattr(self.config, "log_panel", {}) or {}).get("max_visible_lines", 100))
        if len(self._ui_log_entries) > max(max_lines * 4, 400):
            del self._ui_log_entries[: len(self._ui_log_entries) - max(max_lines * 4, 400)]
        if hasattr(self, "log_status_label"):
            self._update_log_status_label()
        if hasattr(self, "log_text") and level in self._visible_log_levels():
            line = f"[{level}] {message}"
            color = "#ff9b9b" if level == "ERROR" else ("#ffd27a" if level in {"WARN", "WARNING"} else "#dfe7f7")
            self.log_text.append(f"<span style='color:{color};'>{html.escape(line)}</span>")
            if not hasattr(self, "log_auto_scroll_check") or self.log_auto_scroll_check.isChecked():
                self.log_text.ensureCursorVisible()

    def refresh_log_panel(self) -> None:
        if not hasattr(self, "log_text"):
            return
        max_lines = int((getattr(self.config, "log_panel", {}) or {}).get("max_visible_lines", 100))
        visible = self._visible_log_levels()
        try:
            panel_cfg = dict(getattr(self.config, "log_panel", {}) or {})
            panel_cfg["visible_levels"] = sorted(visible)
            panel_cfg["auto_scroll"] = bool(getattr(self, "log_auto_scroll_check", None).isChecked()) if hasattr(self, "log_auto_scroll_check") else True
            self.config.log_panel = panel_cfg
            save_config(self.config)
        except Exception:
            pass
        lines = [(level, msg) for level, msg in getattr(self, "_ui_log_entries", []) if level in visible][-max_lines:]
        self.log_text.clear()
        for level, msg in lines:
            color = "#ff9b9b" if level == "ERROR" else ("#ffd27a" if level in {"WARN", "WARNING"} else "#dfe7f7")
            line = f"[{level}] {msg}"
            self.log_text.append(f"<span style='color:{color};'>{html.escape(line)}</span>")
        if not hasattr(self, "log_auto_scroll_check") or self.log_auto_scroll_check.isChecked():
            self.log_text.ensureCursorVisible()

    def copy_visible_logs(self) -> None:
        if hasattr(self, "log_text"):
            QApplication.clipboard().setText(self.log_text.toPlainText())
            self.show_status("已复制当前显示的日志")

    def copy_error_logs(self) -> None:
        entries = [
            f"[{level}] {message}"
            for level, message in getattr(self, "_ui_log_entries", [])
            if str(level or "").upper() == "ERROR"
        ][-100:]
        if not entries:
            self.show_status("当前显示缓存中没有 ERROR 日志", log=False)
            return
        QApplication.clipboard().setText("\n".join(entries))
        self.show_status(f"已复制最近 {len(entries)} 条错误日志")

    def clear_log_display(self) -> None:
        if hasattr(self, "log_text"):
            self.log_text.clear()
            self.show_status("已清空日志面板显示，真实日志文件未删除", log=False)

    def open_log_workbench(self) -> None:
        entries = list(getattr(self, "_ui_log_entries", []) or [])
        if not entries and hasattr(self, "log_text"):
            entries = [self._parse_log_line(line) for line in self.log_text.toPlainText().splitlines() if line.strip()]
        dialog = LogWorkbenchDialog(entries, self, open_log_callback=self.open_log_file)
        dialog.exec()

    def open_log_file(self) -> None:
        try:
            path = Path(getattr(self, "log_path", "") or "")
            if path.exists():
                open_path(str(path))
            elif self.current_batch_id:
                batch_log = self.batch_manager.batch_dir(self.current_batch_id) / "run.log"
                open_path(str(batch_log.parent if not batch_log.exists() else batch_log))
            else:
                self.show_warning_toast("当前还没有可打开的日志文件")
        except Exception as exc:
            self.show_error_toast(f"打开日志文件失败：{exc}")

    def toggle_log_panel(self) -> None:
        self._log_panel_user_toggled = True
        self._log_panel_collapsed = not bool(getattr(self, "_log_panel_collapsed", False))
        self._apply_log_panel_state()
        self.show_status("已折叠运行日志" if self._log_panel_collapsed else "已展开运行日志", log=True)

    def focus_log_panel(self) -> None:
        self._switch_page(0)
        self._log_panel_user_toggled = True
        self._log_panel_collapsed = False
        self._apply_log_panel_state()
        if hasattr(self, "log_text"):
            self.log_text.setFocus()

    def append_worker_log(self, level: str, message: str, task: object | None = None) -> None:
        if self.active_running_batch_id == self.current_batch_id:
            self.queue_log_line(level, message)
        else:
            self._background_log_counter += 1
            if self._background_log_counter % 50 == 0:
                self.show_status("后台批次正在写入日志", timeout_ms=1800)

    @staticmethod
    def _is_url(value: str) -> bool:
        return value.strip().lower().startswith(("http://", "https://"))

    @staticmethod
    def _is_image_path(value: str) -> bool:
        from urllib.parse import urlparse

        return Path(urlparse(value).path if value.startswith(("http://", "https://")) else value).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

    @staticmethod
    def _is_video_path(value: str) -> bool:
        from urllib.parse import urlparse

        return Path(urlparse(value).path if value.startswith(("http://", "https://")) else value).suffix.lower() in {".mp4", ".mov", ".m4v", ".webm"}

    def preview_value(self, value: str) -> None:
        value = (value or "").strip()
        self.current_preview_value = value
        self.current_preview_kind = ""
        if self.media_player:
            self.media_player.stop()
        if self.video_widget:
            self.video_widget.hide()
        self.preview_label.show()
        if not value:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("暂无预览")
            self.preview_info_label.setText("")
            return

        if not self._is_url(value) and self._is_image_path(value) and Path(value).exists():
            pixmap = QPixmap(value)
            if pixmap.isNull():
                self.preview_label.setPixmap(QPixmap())
                self.preview_label.setText("图片无法加载")
            else:
                scaled = pixmap.scaled(
                    max(self.preview_label.width(), 320),
                    max(self.preview_label.height(), 240),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
                self.preview_label.setPixmap(scaled)
            self.current_preview_kind = "image"
            self.preview_info_label.setText(f"{value}\n尺寸: {pixmap.width()} x {pixmap.height()}")
            return

        if self._is_url(value) and self._is_image_path(value):
            self.current_preview_kind = "image_url"
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("远程图片 URL")
            self.preview_info_label.setText(value)
            return

        if self._is_url(value) or self._is_video_path(value):
            self.current_preview_kind = "video" if self._is_video_path(value) or self._is_url(value) else "link"
            self.preview_label.setPixmap(QPixmap())
            self.preview_info_label.setText(value)
            if self.media_player:
                self.preview_label.hide()
                if self.video_widget:
                    self.video_widget.show()
                source = QUrl(value) if self._is_url(value) else QUrl.fromLocalFile(value)
                self.media_player.setSource(source)
                self.media_player.play()
            else:
                self.preview_label.setText("当前环境不支持内嵌视频预览，可使用打开按钮")
            return

        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText("无法预览，可使用打开按钮")
        self.preview_info_label.setText(value)

    def open_value(self, value: str) -> None:
        value = (value or "").strip()
        if not value:
            return
        if self._is_url(value):
            QDesktopServices.openUrl(QUrl(value))
            return
        open_path(value)

    def open_current_preview(self) -> None:
        try:
            self.open_value(self.current_preview_value)
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", str(exc))

    def open_current_preview_folder(self) -> None:
        value = (self.current_preview_value or "").strip()
        if not value or self._is_url(value):
            return
        path = Path(value)
        folder = path if path.is_dir() else path.parent
        if folder.exists():
            open_path(str(folder))
        else:
            QMessageBox.warning(self, "路径不存在", "路径不存在，无法打开")

    def copy_current_preview_link(self) -> None:
        if self.current_preview_value:
            value = self.current_preview_value
            QApplication.clipboard().setText(path_to_http_url(value, self.config) or value)
            self.append_log("INFO", "已复制当前预览路径/链接")

    def show_task_context_menu(self, pos) -> None:
        row = self.table.indexAt(pos).row()
        if row < 0 or row >= len(self.display_rows):
            return
        row_data = self.display_rows[row]
        if row_data["type"] == "group":
            key = row_data["key"]
            if key in self.group_collapsed:
                self.group_collapsed.remove(key)
            else:
                self.group_collapsed.add(key)
            self.refresh_table()
            return
        task = row_data["task"]
        column = self.table.indexAt(pos).column()
        header = TABLE_HEADERS[column] if 0 <= column < len(TABLE_HEADERS) else ""
        cell_value = self.task_field_value(task, header)
        menu = QMenu(self)

        def add_action(text: str, callback) -> None:
            action = QAction(text, self)
            action.triggered.connect(callback)
            menu.addAction(action)

        add_action("打开 PID 商品链接", lambda: self.open_pid_link(task))
        add_action("打开网盘路径", lambda: self.open_local_folder(task.netdisk_path))
        add_action("打开产品图", lambda: self.open_value(task.product_image_path or ""))
        add_action("打开生成图片", lambda: self.open_value(task.generated_image_path or task.generated_image_url or ""))
        add_action("打开视频", lambda: self.open_value(task.video_file_path or task.video_url or ""))
        add_action("打开视频所在文件夹", lambda: self.open_local_folder(str(Path(task.video_file_path or "").parent) if task.video_file_path else ""))
        add_action("复制 Http 路径", lambda: QApplication.clipboard().setText(task.netdisk_http_path or ""))
        add_action("复制产品白底图 URL", lambda: QApplication.clipboard().setText(task.product_image_url or ""))
        add_action("打开产品白底图 URL", lambda: self.open_value(task.product_image_url or ""))
        add_action("复制 PID", lambda: QApplication.clipboard().setText(task.pid or ""))
        add_action("复制视频链接", lambda: QApplication.clipboard().setText(task.video_url or ""))
        add_action("复制错误信息", lambda: QApplication.clipboard().setText(task.error_message or ""))
        add_action("查看任务运行日志", lambda: self.show_task_log_dialog(task))
        add_action("导出任务日志", lambda: self.export_task_log_files(task))
        add_action("复制任务错误日志摘要", lambda: QApplication.clipboard().setText(task_error_log_summary(task)))
        menu.addSeparator()
        add_action("按该列筛选", lambda: self.add_column_filter(header))
        add_action("按该值筛选", lambda: self.add_value_filter(header, cell_value))
        add_action("清除此列筛选", lambda: self.clear_field_filter(header))
        add_action("按该列分组", lambda: self.add_group_field(header))
        add_action("取消该列分组", lambda: self.remove_group_field(header))
        add_action("隐藏该列", lambda: self.hide_column_by_name(header))
        add_action("字段显示设置", self.open_column_visibility_dialog)
        menu.addSeparator()
        # v2: per-stage manual control submenu — read/skip/retry per node.
        if bool(getattr(self.config, "enable_node_level_manual_control", True)):
            stage_menu = menu.addMenu("阶段节点操作")
            for node_id, short_label, node_name, prompt_attr in self._stage_nodes_for_task(task):
                node_state = task_node_state(task, node_id)
                status = str(node_state.get("status") or "")
                icon = stage_status_icon(status)
                label_text = friendly_status_text(status) or "待执行"
                sub = stage_menu.addMenu(f"{short_label}  {icon}  {label_text}  ·  {node_name}")
                node_type = "video" if "video" in node_id else "image"
                # Retry: reset and re-run pipeline for this task.
                retry_action = QAction("重试此节点（重新执行）", self)
                retry_action.triggered.connect(lambda _checked=False, t=task, n=node_id: self.retry_workflow_node(t, n))
                sub.addAction(retry_action)
                # Skip
                skip_action = QAction("跳过此节点", self)
                skip_action.triggered.connect(lambda _checked=False, t=task, n=node_id: self.skip_workflow_node(t, n))
                sub.addAction(skip_action)
                # Manual poll for video nodes that have a task_id.
                if node_type == "video" and node_state.get("task_id") and not node_state.get("output_video_url"):
                    poll_action = QAction("手动轮询此视频节点", self)
                    poll_action.triggered.connect(lambda _checked=False, t=task, n=node_id: self.manual_poll_workflow_node(t, n))
                    sub.addAction(poll_action)
                sub.addSeparator()
                # View output
                view_action = QAction("查看此节点输出", self)
                view_action.triggered.connect(lambda _checked=False, s=node_state: self.view_workflow_node_output(s))
                sub.addAction(view_action)
                open_dir_action = QAction("打开此节点输出目录", self)
                open_dir_action.triggered.connect(lambda _checked=False, t=task, n=node_id: self.open_workflow_node_output_dir(t, n))
                sub.addAction(open_dir_action)
                sub.addSeparator()
                attach_action = QAction("为此节点指定输入图片…", self)
                attach_action.triggered.connect(lambda _checked=False, t=task, n=node_id: self.attach_workflow_node_input(t, n))
                sub.addAction(attach_action)
                clear_action = QAction("清除手动输入图片", self)
                clear_action.triggered.connect(lambda _checked=False, t=task, n=node_id: self.clear_workflow_node_input(t, n))
                sub.addAction(clear_action)
        menu.addSeparator()
        add_action("重新执行该任务", lambda: self.reset_task_for_rerun(task))
        add_action("下载该任务视频到指定目录", lambda: self.download_one_video_to_dir(task))
        add_action("下载该任务图片资料到指定目录", lambda: self.download_one_images_to_dir(task))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _batch_dir_for_task(self, task: TaskItem) -> Path:
        batch_id = task.batch_id or self.current_batch_id
        if batch_id:
            return self.batch_manager.batch_dir(batch_id)
        return Path(self.config.output_dir)

    def export_task_log_files(self, task: TaskItem) -> dict[str, Path] | None:
        try:
            paths = export_task_logs(task, self._batch_dir_for_task(task))
            self.show_success_toast(f"任务日志已导出：{paths['txt']}")
            self.show_status(f"任务日志已导出: {paths['txt']}", log=True)
            return paths
        except Exception as exc:
            self.show_error_toast(f"导出任务日志失败：{exc}")
            return None

    def show_task_log_dialog(self, task: TaskItem) -> None:
        if not getattr(self.config, "enable_task_log_popup", True):
            self.show_status("任务日志弹窗已在配置中关闭")
            return
        self.show_status("正在打开任务日志...", log=True)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"任务运行日志｜PID：{task.pid}｜行号：{task.row_index}")
        dialog.resize(900, 620)
        layout = QVBoxLayout(dialog)
        info = QLabel(
            f"任务名称：{task.task_name or '-'}\n"
            f"PID：{task.pid or '-'}    负责人：{task.owner or '-'}    批次：{task.batch_id or '-'}\n"
            f"当前状态：{task.status or '-'}\n"
            f"当前错误：{task.error_message or '-'}"
        )
        info.setWordWrap(True)
        info.setObjectName("hintLabel")
        layout.addWidget(info)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setFont(QFont("Consolas", 10))
        log_text = task_logs_to_text(task).strip()
        if not log_text:
            log_text = "这条任务暂时还没有详细日志。\n你可以先执行任务，运行过程会自动记录在这里。"
        text.setPlainText(log_text)
        layout.addWidget(text, 1)
        row = QHBoxLayout()
        copy_all_btn = QPushButton("复制全部日志")
        copy_error_btn = QPushButton("复制错误信息")
        export_txt_btn = QPushButton("导出日志 TXT")
        export_json_btn = QPushButton("导出日志 JSON")
        open_dir_btn = QPushButton("打开批次日志目录")
        close_btn = QPushButton("关闭")
        copy_all_btn.clicked.connect(lambda: QApplication.clipboard().setText(text.toPlainText()))
        copy_error_btn.clicked.connect(lambda: QApplication.clipboard().setText(task.error_message or task_error_log_summary(task)))

        def export_and_option(which: str) -> None:
            paths = self.export_task_log_files(task)
            if paths and which in paths:
                self.show_status(f"已导出任务日志：{paths[which]}")

        export_txt_btn.clicked.connect(lambda: export_and_option("txt"))
        export_json_btn.clicked.connect(lambda: export_and_option("json"))
        open_dir_btn.clicked.connect(lambda: open_path(str(self._batch_dir_for_task(task))))
        close_btn.clicked.connect(dialog.accept)
        for btn in [copy_all_btn, copy_error_btn, export_txt_btn, export_json_btn, open_dir_btn]:
            row.addWidget(btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        layout.addLayout(row)
        dialog.exec()

    def open_pid_link(self, task: TaskItem) -> None:
        if task.pid:
            QDesktopServices.openUrl(QUrl(f"https://www.tiktok.com/shop/sg/pdp/{task.pid}?source=product_detail"))

    def open_local_folder(self, value: str) -> None:
        if not value:
            return
        path = Path(value)
        if not path.exists():
            QMessageBox.warning(self, "路径不存在", "路径不存在，无法打开")
            return
        open_path(str(path))

    def reset_task_for_rerun(self, task: TaskItem) -> None:
        task.product_image_path = None
        task.generated_image_path = None
        task.generated_image_url = None
        task.image_raw_response = None
        task.video_url = None
        task.video_file_path = None
        task.video_task_id = None
        task.video_submit_time = None
        task.video_poll_start_time = None
        task.video_poll_end_time = None
        task.video_poll_count = 0
        task.video_raw_response = None
        task.auto_retry_count = 0
        task.last_auto_retry_time = None
        task.last_auto_retry_stage = None
        task.last_auto_retry_reason = None
        task.image_status = ""
        task.video_status = ""
        task.status = TaskStatus.PENDING
        task.error_message = None
        task.started_at = None
        task.ended_at = None
        task.elapsed_seconds = None
        # Reset any v2 workflow node states too so the next run starts fresh.
        if isinstance(getattr(task, "node_states", None), dict):
            for _node in task.node_states.values():
                if isinstance(_node, dict):
                    _node["status"] = "PENDING"
                    _node["error_message"] = None
                    _node["started_at"] = None
                    _node["ended_at"] = None
                    _node["poll_count"] = 0
                    _node["task_id"] = None
                    _node["output_image_path"] = None
                    _node["output_image_url"] = None
                    _node["output_video_url"] = None
                    _node["output_video_local_path"] = None
                    _node["raw_response"] = None
        task.touch()
        self.manager.save_state(force_backup=True)
        self.update_current_batch_info()
        self.refresh_table()
        self.append_log("INFO", f"已重置任务等待重新执行: PID={task.pid} row={task.row_index}")
        self.show_success_toast("已重置该任务，可以重新执行了")

    # --- v2 node-level manual handlers ---------------------------------------
    def _stage_label_for(self, node_id: str) -> tuple[str, str]:
        for nid, short_label, node_name, _ in self._stage_nodes_for_task(None):
            if nid == node_id:
                return short_label, node_name
        default = DEFAULT_STAGE_NODE_BY_ID.get(node_id)
        if default:
            return default[0], default[1]
        return node_id, node_id

    def retry_workflow_node(self, task: TaskItem, node_id: str) -> None:
        short_label, node_name = self._stage_label_for(node_id)
        state = task_node_state(task, node_id)
        if not state:
            self.show_warning_toast(f"任务尚未初始化节点状态，无法重试 {short_label}")
            return
        state["status"] = "PENDING"
        state["error_message"] = None
        state["started_at"] = None
        state["ended_at"] = None
        state["poll_count"] = 0
        state["task_id"] = None
        state["output_image_path"] = None
        state["output_image_url"] = None
        state["output_video_url"] = None
        state["output_video_local_path"] = None
        state["raw_response"] = None
        if task.status.startswith("FAILED") or task.status.startswith("SKIPPED") or task.status in {TaskStatus.VIDEO_FAILED, TaskStatus.VIDEO_TIMEOUT, TaskStatus.COMPLETED, TaskStatus.VIDEO_DOWNLOADED}:
            task.status = TaskStatus.PENDING
            task.error_message = None
        task.touch()
        self.manager.save_state()
        self.refresh_table()
        self.append_log("INFO", f"已重置 {short_label}（{node_name}）等待重新执行: PID={task.pid} row={task.row_index}")
        self.show_success_toast(f"已收到重试请求，{short_label} 已重置为待执行；点击「开始」或「重试失败」会重新跑")

    def skip_workflow_node(self, task: TaskItem, node_id: str) -> None:
        short_label, node_name = self._stage_label_for(node_id)
        state = task_node_state(task, node_id)
        if not state:
            self.show_warning_toast(f"任务尚未初始化节点状态，无法跳过 {short_label}")
            return
        state["status"] = "SKIPPED"
        state["ended_at"] = state.get("ended_at") or now_text()
        state["error_message"] = "用户手动跳过该节点"
        task.touch()
        self.manager.save_state()
        self.refresh_table()
        self.append_log("INFO", f"已跳过 {short_label}（{node_name}）: PID={task.pid} row={task.row_index}")
        self.show_status(f"已跳过 {short_label}：{node_name}")

    def manual_poll_workflow_node(self, task: TaskItem, node_id: str) -> None:
        short_label, _ = self._stage_label_for(node_id)
        state = task_node_state(task, node_id)
        task_id = str((state or {}).get("task_id") or "").strip()
        if not task_id:
            self.show_warning_toast(f"{short_label} 还没有 task_id，无法手动轮询")
            return
        # For video_stage_2 the legacy mirror (task.video_task_id) only tracks
        # video_stage_1. Temporarily reuse the task.video_task_id field so the
        # existing manual-poll worker can find this node.
        if node_id == "video_stage_2":
            task.video_task_id = task_id
            task.touch()
            self.manager.save_state()
        self.show_status(f"已收到手动轮询请求：{short_label} …")
        # Prefer: if a worker is running, kick it. Otherwise fall back to
        # batch-wide manual poll which iterates eligible tasks.
        try:
            if self.is_process_worker_running():
                self.send_process_worker_command("poll")
                self.show_success_toast(f"已通知后台执行进程立即轮询 {short_label}")
                return
            if self.worker is not None and self.worker.isRunning():
                self.worker.request_poll_cycle()
                self.show_success_toast(f"已通知执行中的 worker 立即轮询 {short_label}")
                return
            batch_id = task.batch_id or self.current_batch_id
            if not batch_id:
                self.show_warning_toast("无法确定批次，无法触发手动轮询")
                return
            self.manual_poll_batch(batch_id)
            self.show_success_toast(f"已启动手动轮询，{short_label} 会随当前批次一起更新")
        except Exception as exc:
            self.show_error_toast(f"手动轮询失败：{exc}")

    def view_workflow_node_output(self, state: dict) -> None:
        if not state:
            self.show_warning_toast("尚无节点输出可查看")
            return
        target = state.get("output_image_path") or state.get("output_image_url") or state.get("output_video_local_path") or state.get("output_video_url")
        if not target:
            self.show_status("该节点还没有输出")
            return
        self.preview_value(target) if hasattr(self, "preview_value") else self.open_value(target)

    def open_workflow_node_output_dir(self, task: TaskItem, node_id: str) -> None:
        state = task_node_state(task, node_id)
        candidate = state.get("output_image_path") or state.get("output_video_local_path")
        if candidate:
            folder = str(Path(candidate).parent)
            if Path(folder).exists():
                open_path(folder)
                return
        # Fall back to the stage assets directory under the image-assets root.
        try:
            from app.file_utils import stage_assets_dir
            folder = str(stage_assets_dir(self.config.image_assets_root, task))
            open_path(folder)
        except Exception as exc:
            self.show_warning_toast(f"无法打开节点输出目录：{exc}")

    def attach_workflow_node_input(self, task: TaskItem, node_id: str) -> None:
        short_label, node_name = self._stage_label_for(node_id)
        paths, _ = QFileDialog.getOpenFileNames(self, f"为 {short_label}（{node_name}）选择输入图片", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*)")
        if not paths:
            return
        state = task_node_state(task, node_id)
        if not state:
            # Lazily create a placeholder node state so the choice is preserved
            # even before the workflow engine has run on this task.
            states = getattr(task, "node_states", None)
            if not isinstance(states, dict):
                states = {}
                task.node_states = states
            state = states.setdefault(node_id, {
                "node_id": node_id,
                "status": "PENDING",
                "manual_input_images": [],
                "manual_input_urls": [],
            })
        manual_imgs = list(state.get("manual_input_images") or [])
        for p in paths:
            if p and p not in manual_imgs:
                manual_imgs.append(p)
        state["manual_input_images"] = manual_imgs
        task.touch()
        self.manager.save_state()
        self.refresh_table()
        self.append_log("INFO", f"已为 {short_label}（{node_name}）添加 {len(paths)} 张手动输入图片: PID={task.pid} row={task.row_index}")
        self.show_success_toast(f"{short_label} 已记录 {len(paths)} 张手动输入图，下次执行该节点会自动使用")

    def clear_workflow_node_input(self, task: TaskItem, node_id: str) -> None:
        short_label, _ = self._stage_label_for(node_id)
        state = task_node_state(task, node_id)
        if not state:
            return
        state["manual_input_images"] = []
        state["manual_input_urls"] = []
        task.touch()
        self.manager.save_state()
        self.refresh_table()
        self.show_status(f"已清除 {short_label} 的手动输入图片")

    def download_one_video_to_dir(self, task: TaskItem) -> None:
        if not task.video_url:
            QMessageBox.warning(self, "没有视频链接", "当前任务还没有视频链接")
            return
        base_dir = QFileDialog.getExistingDirectory(self, "选择视频下载目录", str(self.config.video_download_root))
        if not base_dir:
            return
        try:
            path, downloaded = self._download_task_video(task, base_dir, self.group_by_owner_check.isChecked())
            self.manager.save_state()
            self.update_current_batch_info()
            self.refresh_table()
            self.append_log("INFO", f"PID={task.pid} row={task.row_index} {'视频已下载' if downloaded else '视频已存在，跳过下载'}: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "下载失败", str(exc))

    def download_one_images_to_dir(self, task: TaskItem) -> None:
        base_dir = QFileDialog.getExistingDirectory(self, "选择图片资料下载目录", str(self.config.image_assets_root))
        if not base_dir:
            return
        try:
            folder = self._copy_image_assets_to_dir(task, base_dir, self.group_by_owner_check.isChecked())
            self.append_log("INFO", f"PID={task.pid} row={task.row_index} 图片资料已保存: {folder}")
        except Exception as exc:
            QMessageBox.critical(self, "下载失败", str(exc))

    def on_cell_clicked(self, row: int, column: int) -> None:
        if row >= len(self.display_rows):
            return
        task = self._task_for_display_row(row)
        if task is None:
            return
        self.stat_labels["pid"].setText(f"当前 PID: {task.pid}")
        self.stat_labels["row"].setText(f"当前行号: {task.row_index}")
        self.update_detail_panel_for_task(task)
        if column in {
            COL_INDEX["产品白底图路径"],
            COL_INDEX["产品白底图URL"],
            COL_INDEX["生成图片路径"],
            COL_INDEX["生成图片URL"],
            COL_INDEX["视频本地路径"],
            COL_INDEX["视频链接"],
        }:
            value = self.table.item(row, column).text() if self.table.item(row, column) else ""
            self.preview_value(value)

    def update_detail_panel_for_task(self, task: TaskItem) -> None:
        if not hasattr(self, "preview_info_label"):
            return
        lines = [
            f"任务：{getattr(task, 'task_name', '') or '-'}",
            f"PID：{task.pid or '-'}｜负责人：{task.owner or '-'}",
            f"状态：{task_overall_status_display(task, self._stage_nodes_for_task(task))}",
        ]
        if task.generated_image_path or task.generated_image_url:
            lines.append(f"图片结果：{task.generated_image_path or task.generated_image_url}")
        if task.video_file_path or task.video_url:
            lines.append(f"视频结果：{task.video_file_path or task.video_url}")
        prompt = task.prompt_stage_1 or task.image_prompt or ""
        if prompt:
            lines.append(f"Prompt：{self._summary(prompt, 90)}")
        if task.error_message:
            lines.append(f"错误：{self._summary(task.error_message, 160)}")
        self.preview_info_label.setText("\n".join(lines))
        if hasattr(self, "task_log_preview_text"):
            log_text = task_logs_to_text(task).strip()
            if not log_text:
                log_text = "这条任务暂时还没有详细日志。"
            self.task_log_preview_text.setPlainText(log_text)

    def on_cell_double_clicked(self, row: int, column: int) -> None:
        item = self.table.item(row, column)
        if not item:
            return
        if row < len(self.display_rows) and self.display_rows[row]["type"] == "group":
            key = self.display_rows[row]["key"]
            if key in self.group_collapsed:
                self.group_collapsed.remove(key)
            else:
                self.group_collapsed.add(key)
            self.refresh_table()
            return
        task = self._task_for_display_row(row)
        if column == COL_INDEX.get("错误信息") and task is not None:
            self.show_task_log_dialog(task)
            return
        if column == COL_INDEX["PID"]:
            pid = item.text().strip()
            if pid:
                QDesktopServices.openUrl(QUrl(f"https://www.tiktok.com/shop/sg/pdp/{pid}?source=product_detail"))
            return
        if column == COL_INDEX["网盘路径"]:
            path = item.text().strip()
            if not path:
                return
            if not Path(path).exists():
                QMessageBox.warning(self, "路径不存在", "路径不存在，无法打开")
                return
            try:
                open_path(path)
            except Exception as exc:
                QMessageBox.warning(self, "打开失败", str(exc))
            return
        if column in {
            COL_INDEX["产品白底图路径"],
            COL_INDEX["产品白底图URL"],
            COL_INDEX["生成图片路径"],
            COL_INDEX["生成图片URL"],
            COL_INDEX["视频本地路径"],
            COL_INDEX["视频链接"],
        } and item.text():
            try:
                self.open_value(item.text())
            except Exception as exc:
                QMessageBox.warning(self, "打开失败", str(exc))

    def _show_popup_dialog(self, anchor: QWidget, title: str, width: int = 420) -> QDialog:
        dialog = QDialog(self, Qt.Popup)
        dialog.setObjectName("popupPanel")
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(width)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        header = QLabel(title)
        header.setObjectName("sectionTitle")
        layout.addWidget(header)
        pos = anchor.mapToGlobal(QPoint(0, anchor.height() + 6))
        dialog.move(pos)
        self._active_popup = dialog
        return dialog

    def filter_summary_text(self) -> str:
        parts = [f"{field} in [{', '.join(sorted(values))}]" for field, values in self.active_filters.items() if values]
        return "筛选: " + ("；".join(parts) if parts else "无")

    def group_summary_text(self) -> str:
        return "分组: " + (" → ".join(self.group_by_fields) if self.group_by_fields else "无")

    def show_filter_popup(self, anchor: QWidget) -> None:
        dialog = self._show_popup_dialog(anchor, "设置筛选条件", 520)
        layout = dialog.layout()
        self.filter_column_combo = self._setup_combo_box(QComboBox(), 280, 14)
        self.filter_column_combo.addItem("全部列", -1)
        for index, header in enumerate(TABLE_HEADERS):
            self.filter_column_combo.addItem(header, index)
        self.filter_text_edit = QLineEdit()
        self.filter_text_edit.setPlaceholderText("输入筛选内容，支持部分匹配")
        self.multi_filter_field_combo = self._setup_combo_box(QComboBox(), 260, 14)
        self.multi_filter_field_combo.addItems(FILTER_FIELDS)
        self.multi_filter_value_combo = self._setup_combo_box(QComboBox(), 320, 14)
        self.multi_filter_value_combo.setEditable(True)
        self._refresh_combo_box_display(self.filter_column_combo)
        self._refresh_combo_box_display(self.multi_filter_field_combo)
        condition_label = QLabel(self.filter_summary_text())
        condition_label.setObjectName("chipLabel")

        def refresh_values() -> None:
            self.refresh_filter_value_options()

        def add_condition() -> None:
            field = self.multi_filter_field_combo.currentText()
            value = self.multi_filter_value_combo.currentText().strip()
            if field and value:
                self.active_filters.setdefault(field, set()).add(value)
                condition_label.setText(self.filter_summary_text())
                self.refresh_table()
                self.update_filter_group_labels()

        self.multi_filter_field_combo.currentTextChanged.connect(lambda *_: refresh_values())
        self.filter_text_edit.returnPressed.connect(lambda: (self.apply_table_filter(), dialog.close()))
        form = QGridLayout()
        form.addWidget(QLabel("列"), 0, 0)
        form.addWidget(self.filter_column_combo, 0, 1)
        form.addWidget(QLabel("内容"), 0, 2)
        form.addWidget(self.filter_text_edit, 0, 3)
        form.addWidget(QLabel("筛选字段"), 1, 0)
        form.addWidget(self.multi_filter_field_combo, 1, 1)
        form.addWidget(QLabel("筛选选项"), 1, 2)
        form.addWidget(self.multi_filter_value_combo, 1, 3)
        form.setColumnStretch(3, 1)
        layout.addLayout(form)
        layout.addWidget(condition_label)

        buttons = QHBoxLayout()
        self.add_filter_btn = QPushButton("+ 添加条件")
        self.apply_filter_btn = QPushButton("应用筛选")
        self.apply_filter_btn.setObjectName("primaryButton")
        self.clear_filter_btn = QPushButton("清空筛选")
        self.save_filter_btn = QPushButton("保存筛选方案")
        self.load_filter_btn = QPushButton("加载筛选方案")
        self.add_filter_btn.clicked.connect(add_condition)
        self.apply_filter_btn.clicked.connect(lambda: (self.apply_table_filter(), dialog.close()))
        self.clear_filter_btn.clicked.connect(lambda: (self.clear_table_filter(), condition_label.setText(self.filter_summary_text())))
        self.save_filter_btn.clicked.connect(self.save_filter_scheme)
        self.load_filter_btn.clicked.connect(lambda: (self.load_filter_scheme(), condition_label.setText(self.filter_summary_text())))
        for button in [self.add_filter_btn, self.apply_filter_btn, self.clear_filter_btn, self.save_filter_btn, self.load_filter_btn]:
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.refresh_filter_value_options()
        dialog.show()

    def show_group_popup(self, anchor: QWidget) -> None:
        dialog = self._show_popup_dialog(anchor, "设置分组", 460)
        layout = dialog.layout()
        enable_check = QCheckBox("启用分组")
        enable_check.setChecked(self.group_enabled_check.isChecked())
        self.group_field_combo = self._setup_combo_box(QComboBox(), 260, 14)
        self.group_field_combo.addItems(GROUP_FIELDS)
        self._refresh_combo_box_display(self.group_field_combo)
        current_label = QLabel(self.group_summary_text())
        current_label.setObjectName("chipLabel")

        def refresh_label() -> None:
            current_label.setText(self.group_summary_text())
            self.update_filter_group_labels()

        self.add_group_btn = QPushButton("+ 添加分组字段")
        self.remove_group_btn = QPushButton("移除字段")
        self.clear_group_btn = QPushButton("清空分组")
        self.expand_all_groups_btn = QPushButton("全部展开")
        self.collapse_all_groups_btn = QPushButton("全部折叠")
        apply_btn = QPushButton("应用分组")
        apply_btn.setObjectName("primaryButton")
        enable_check.stateChanged.connect(lambda *_: self.group_enabled_check.setChecked(enable_check.isChecked()))
        self.add_group_btn.clicked.connect(lambda: (self.add_group_field(self.group_field_combo.currentText()), refresh_label()))
        self.remove_group_btn.clicked.connect(lambda: (self.remove_group_field(self.group_field_combo.currentText()), refresh_label()))
        self.clear_group_btn.clicked.connect(lambda: (self.clear_group_fields(), refresh_label()))
        self.expand_all_groups_btn.clicked.connect(self.expand_all_groups)
        self.collapse_all_groups_btn.clicked.connect(self.collapse_all_groups)
        apply_btn.clicked.connect(lambda: (self.group_enabled_check.setChecked(enable_check.isChecked()), self.refresh_table(), dialog.close()))

        row = QHBoxLayout()
        row.addWidget(QLabel("分组字段"))
        row.addWidget(self.group_field_combo, 1)
        row.addWidget(self.add_group_btn)
        layout.addWidget(enable_check)
        layout.addLayout(row)
        layout.addWidget(current_label)
        buttons = QGridLayout()
        for index, button in enumerate([self.remove_group_btn, self.clear_group_btn, self.expand_all_groups_btn, self.collapse_all_groups_btn, apply_btn]):
            buttons.addWidget(button, index // 3, index % 3)
        layout.addLayout(buttons)
        dialog.show()

    def show_sort_popup(self, anchor: QWidget) -> None:
        dialog = self._show_popup_dialog(anchor, "设置排序", 360)
        layout = dialog.layout()
        field_combo = self._setup_combo_box(QComboBox(), 300, 14)
        field_combo.addItems(TABLE_HEADERS)
        order_combo = self._setup_combo_box(QComboBox(), 220, 8)
        order_combo.addItems(["升序", "降序"])
        self._refresh_combo_box_display(field_combo)
        self._refresh_combo_box_display(order_combo)
        apply_btn = QPushButton("应用排序")
        clear_btn = QPushButton("清空排序")
        apply_btn.setObjectName("primaryButton")

        def apply_sort() -> None:
            self.sort_rules = [(field_combo.currentText(), order_combo.currentText() == "降序")]
            self.refresh_table()
            dialog.close()

        def clear_sort() -> None:
            self.sort_rules = []
            self.refresh_table()
            dialog.close()

        form = QFormLayout()
        form.addRow("排序字段", field_combo)
        form.addRow("排序方式", order_combo)
        layout.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(clear_btn)
        row.addStretch(1)
        row.addWidget(apply_btn)
        layout.addLayout(row)
        apply_btn.clicked.connect(apply_sort)
        clear_btn.clicked.connect(clear_sort)
        dialog.show()

    def show_row_height_popup(self, anchor: QWidget) -> None:
        menu = QMenu(self)
        menu.addAction("紧凑", lambda: self.set_task_row_height(24))
        menu.addAction("标准", lambda: self.set_task_row_height(30))
        menu.addAction("宽松", lambda: self.set_task_row_height(38))
        menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))

    def set_task_row_height(self, height: int) -> None:
        self.table.verticalHeader().setDefaultSectionSize(height)
        self.refresh_table()

    def apply_table_filter(self) -> None:
        if hasattr(self, "filter_text_edit"):
            state = self.begin_loading("应用筛选", self.apply_filter_btn if hasattr(self, "apply_filter_btn") else None)
            self.refresh_table()
            self.end_loading(state, "筛选已应用")

    def clear_table_filter(self) -> None:
        state = self.begin_loading("清空筛选", self.clear_filter_btn if hasattr(self, "clear_filter_btn") else None)
        if hasattr(self, "filter_text_edit"):
            self.filter_text_edit.clear()
        if hasattr(self, "filter_column_combo"):
            self.filter_column_combo.setCurrentIndex(0)
        self.active_filters.clear()
        self.refresh_table()
        self.end_loading(state, "筛选已清空")

    def _task_for_display_row(self, row: int) -> TaskItem | None:
        if 0 <= row < len(self.display_rows):
            row_data = self.display_rows[row]
            if row_data.get("type") == "task":
                return row_data.get("task")
        return None

    def refresh_filter_value_options(self) -> None:
        if not hasattr(self, "multi_filter_value_combo"):
            return
        field = self.multi_filter_field_combo.currentText()
        values = sorted({self.task_field_value(task, field) for task in self.manager.tasks if self.task_field_value(task, field)})
        self.multi_filter_value_combo.clear()
        self.multi_filter_value_combo.addItems(values)
        self._refresh_combo_box_display(self.multi_filter_value_combo)

    def add_active_filter(self) -> None:
        field = self.multi_filter_field_combo.currentText()
        value = self.multi_filter_value_combo.currentText().strip()
        if not field or not value:
            return
        self.active_filters.setdefault(field, set()).add(value)
        self.refresh_table()
        self.show_status(f"已添加筛选条件：{field} = {value}", log=True)

    def add_column_filter(self, field: str) -> None:
        if field in FILTER_FIELDS:
            self.multi_filter_field_combo.setCurrentText(field)
            self.refresh_filter_value_options()

    def add_value_filter(self, field: str, value: str) -> None:
        if field and value:
            self.active_filters.setdefault(field, set()).add(value)
            self.refresh_table()

    def clear_field_filter(self, field: str) -> None:
        self.active_filters.pop(field, None)
        self.refresh_table()

    def save_filter_scheme(self) -> None:
        self.config.active_filters = {field: sorted(values) for field, values in self.active_filters.items()}
        save_config(self.config)
        self.show_status("筛选方案已保存到本地配置", log=True)

    def load_filter_scheme(self) -> None:
        state = self.begin_loading("加载筛选方案", self.load_filter_btn if hasattr(self, "load_filter_btn") else None)
        self.active_filters = {
            str(field): {str(value) for value in values}
            for field, values in dict(self.config.active_filters or {}).items()
            if isinstance(values, list)
        }
        self.refresh_table()
        self.end_loading(state, "筛选方案已加载")

    def add_group_field(self, field: str | None = None) -> None:
        field = field or self.group_field_combo.currentText()
        if field in GROUP_FIELDS and field not in self.group_by_fields:
            self.group_by_fields.append(field)
            self.group_enabled_check.setChecked(True)
            self.refresh_table()
            self.show_status(f"已添加分组字段：{field}", log=True)

    def remove_group_field(self, field: str | None = None) -> None:
        field = field or self.group_field_combo.currentText()
        if field in self.group_by_fields:
            self.group_by_fields.remove(field)
            self.refresh_table()
            self.show_status(f"已移除分组字段：{field}", log=True)

    def clear_group_fields(self) -> None:
        state = self.begin_loading("清空分组", self.clear_group_btn if hasattr(self, "clear_group_btn") else None)
        self.group_by_fields.clear()
        self.group_collapsed.clear()
        self.refresh_table()
        self.end_loading(state, "分组已清空")

    def expand_all_groups(self) -> None:
        state = self.begin_loading("展开全部分组", self.expand_all_groups_btn if hasattr(self, "expand_all_groups_btn") else None)
        self.group_collapsed.clear()
        self.refresh_table()
        self.end_loading(state, "已展开全部分组")

    def collapse_all_groups(self) -> None:
        state = self.begin_loading("折叠全部分组", self.collapse_all_groups_btn if hasattr(self, "collapse_all_groups_btn") else None)
        self.group_collapsed = {row["key"] for row in self.display_rows if row.get("type") == "group"}
        self.refresh_table()
        self.end_loading(state, "已折叠全部分组")

    def update_filter_group_labels(self) -> None:
        if hasattr(self, "active_filter_label"):
            self.active_filter_label.setText(self.filter_summary_text())
        if hasattr(self, "group_label"):
            self.group_label.setText(self.group_summary_text())

    def current_visible_columns(self) -> list[str]:
        mode_key = self._active_column_config_key()
        table_cfg = getattr(self.config, "task_table_columns", {}) or {}
        cols = self._normalise_visible_columns(table_cfg.get(mode_key, []))
        if cols:
            return cols
        configured = self._normalise_visible_columns(getattr(self.config, "visible_columns", []))
        if configured:
            return configured
        if mode_key == "compact_visible":
            return ["任务名称", "任务状态", "流程进度", "阶段1", "阶段2", "错误信息"]
        return list(DEFAULT_VISIBLE_COLUMNS)

    def _active_column_config_key(self) -> str:
        return "compact_visible" if getattr(self, "_layout_mode", "") in {"compact", "extra_compact"} else "large_visible"

    def _normalise_visible_columns(self, columns) -> list[str]:
        visible: list[str] = []
        for raw in columns or []:
            name = "任务名称" if str(raw) == "序号" else str(raw)
            if name in TABLE_HEADERS and name not in visible:
                visible.append(name)
        return visible

    def _set_visible_columns_for_current_mode(self, columns) -> list[str]:
        visible = self._normalise_visible_columns(columns)
        if not visible:
            visible = ["任务名称"]
        table_cfg = dict(getattr(self.config, "task_table_columns", {}) or {})
        table_cfg[self._active_column_config_key()] = visible
        table_cfg.setdefault("compact_visible", ["任务名称", "任务状态", "流程进度", "阶段1", "阶段2", "错误信息"])
        table_cfg.setdefault("large_visible", list(DEFAULT_VISIBLE_COLUMNS))
        self.config.task_table_columns = table_cfg
        self.config.visible_columns = list(visible)
        return visible

    def apply_column_visibility(self) -> None:
        visible = set(self.current_visible_columns())
        for index, header in enumerate(TABLE_HEADERS):
            self.table.setColumnHidden(index, header not in visible)
        self.restore_table_column_widths()
        self.ensure_table_scrollbar()

    def default_table_column_widths(self) -> dict[str, int]:
        return {
            "任务名称": 180,
            "PID": 160,
            "负责人": 100,
            "网盘路径": 280,
            "Http路径": 320,
            "产品白底图路径": 280,
            "产品白底图URL": 320,
            "指定白底图文件名": 180,
            "图片提示词": 360,
            "视频提示词": 360,
            "生成图片路径": 320,
            "生成图片URL": 320,
            "video_task_id": 220,
            "视频链接": 320,
            "视频本地路径": 320,
            "任务状态": 160,
            "错误信息": 320,
            "批次ID": 180,
        }

    def restore_table_column_widths(self) -> None:
        if not hasattr(self, "table"):
            return
        self._restoring_column_widths = True
        widths = self.default_table_column_widths()
        configured = getattr(self.config, "table_column_widths", {}) or {}
        for header, value in configured.items():
            try:
                widths[str(header)] = int(value)
            except (TypeError, ValueError):
                continue
        for header, width in widths.items():
            if header in COL_INDEX:
                self.table.setColumnWidth(COL_INDEX[header], max(54, int(width)))
        self._restoring_column_widths = False

    def on_table_column_resized(self, logical_index: int, old_size: int, new_size: int) -> None:
        if self._restoring_column_widths or logical_index < 0 or logical_index >= len(TABLE_HEADERS):
            return
        if self.table.isColumnHidden(logical_index) or new_size < 54:
            return
        header = TABLE_HEADERS[logical_index]
        widths = dict(getattr(self.config, "table_column_widths", {}) or {})
        widths[header] = int(new_size)
        self.config.table_column_widths = widths
        save_config(self.config)
        self.ensure_table_scrollbar()

    def ensure_table_scrollbar(self) -> None:
        if not hasattr(self, "table"):
            return
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn if self.config.table_horizontal_scrollbar_always_on else Qt.ScrollBarAsNeeded)
        self.table.setSizeAdjustPolicy(QAbstractScrollArea.AdjustIgnored)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.updateGeometry()
        self.table.viewport().update()

    def hide_column_by_name(self, header: str) -> None:
        if header in TABLE_HEADERS:
            visible = self.current_visible_columns()
            if header in visible and len(visible) > 1:
                visible.remove(header)
                self._set_visible_columns_for_current_mode(visible)
                self.apply_column_visibility()
                save_config(self.config)

    def open_column_visibility_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("字段显示设置")
        layout = QVBoxLayout(dialog)
        checks: dict[str, QCheckBox] = {}
        current = set(self.current_visible_columns())
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        grid = QGridLayout(content)
        for index, header in enumerate(TABLE_HEADERS):
            check = QCheckBox(header)
            check.setChecked(header in current)
            checks[header] = check
            grid.addWidget(check, index // 3, index % 3)
        scroll.setWidget(content)
        layout.addWidget(scroll)

        quick = QGridLayout()
        presets = {
            "显示全部": list(TABLE_HEADERS),
            "隐藏全部": ["PID"],
            "恢复默认字段": list(DEFAULT_VISIBLE_COLUMNS),
            "只看执行状态": ["任务名称", "PID", "负责人", "图生图状态", "视频状态", "任务状态", "错误信息"],
            "只看下载结果": ["任务名称", "PID", "负责人", "视频链接", "视频本地路径", "任务状态"],
            "只看错误任务": ["任务名称", "PID", "负责人", "任务状态", "错误信息"],
        }

        def apply_preset(cols: list[str]) -> None:
            selected = set(cols)
            for name, check in checks.items():
                check.setChecked(name in selected)

        for index, (name, cols) in enumerate(presets.items()):
            btn = QPushButton(name)
            btn.clicked.connect(lambda _=False, c=cols: apply_preset(c))
            quick.addWidget(btn, index // 3, index % 3)
        layout.addLayout(quick)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            selected = [name for name, check in checks.items() if check.isChecked()]
            state = self.begin_loading("应用字段显示设置", self.field_visibility_btn if hasattr(self, "field_visibility_btn") else None)
            try:
                self._set_visible_columns_for_current_mode(selected or ["任务名称"])
                self.apply_column_visibility()
                save_config(self.config)
                self.end_loading(state, "字段显示设置已应用")
            except Exception:
                self.end_loading(state, "字段显示设置应用失败")
                raise

    def _set_running_buttons(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        if hasattr(self, "image_only_btn"):
            self.image_only_btn.setEnabled(not running)
        if hasattr(self, "video_only_btn"):
            self.video_only_btn.setEnabled(not running)
        if hasattr(self, "selected_video_btn"):
            self.selected_video_btn.setEnabled(not running)
        self.custom_poll_btn.setEnabled(True)
        if hasattr(self, "manual_poll_btn"):
            manual_running = bool(self.manual_poll_worker and self.manual_poll_worker.isRunning())
            self.manual_poll_btn.setEnabled(bool(self.config.enable_manual_poll_button) and not manual_running)
        self.retry_failed_btn.setEnabled(True)
        if hasattr(self, "switch_api_btn"):
            self.switch_api_btn.setEnabled(True)
        self.pause_btn.setEnabled(running)
        self.resume_btn.setEnabled(running)
        self.stop_btn.setEnabled(running)

    @staticmethod
    def _summary(text: str, length: int = 80) -> str:
        return text if len(text) <= length else text[:length] + "..."


def main() -> None:
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    for icon_path in [
        PROJECT_ROOT / "assets" / "app_icon.ico",
        Path(getattr(sys, "_MEIPASS", "")) / "assets" / "app_icon.ico",
    ]:
        if icon_path.exists():
            app.setWindowIcon(QIcon(str(icon_path)))
            break
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
