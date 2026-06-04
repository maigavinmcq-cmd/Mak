from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from light_image_tool.api_adapter import LightImageApiAdapter
from light_image_tool.config import (
    DEFAULT_WHITE_BG_PROMPT,
    PROJECT_ROOT,
    load_light_image_tool_config,
    save_light_image_tool_config,
)
from light_image_tool.image_selector import is_supported_image, parse_custom_names
from light_image_tool.log_manager import LightImageLogManager
from light_image_tool.models import LightImageTask, LightImageTaskStatus, PIDScanResult
from light_image_tool.pid_scanner import PIDScanner, SCAN_STATUS_TEXT, parse_pid_input
from light_image_tool.queue_manager import LightImageQueueManager
from light_image_tool.session_store import (
    load_light_image_tool_session,
    restore_session_into_queue,
    save_light_image_tool_session,
)
from light_image_tool.task_executor import LightImageTaskExecutor
from light_image_tool.ui.widgets import ImageDropList


TASK_COLUMNS = ["序号", "类型", "PID", "参考图", "状态", "进度", "生成次数", "输出文件", "错误信息", "操作"]
SCAN_COLUMNS = ["PID", "扫描状态", "PID 目录", "product_info 状态", "选中图片数", "输出目录", "错误信息", "操作"]
STATUS_COLORS = {
    LightImageTaskStatus.WAITING: "#303846",
    LightImageTaskStatus.QUEUED: "#365076",
    LightImageTaskStatus.GENERATING: "#8a6500",
    LightImageTaskStatus.DOWNLOADING: "#8a6500",
    LightImageTaskStatus.COMPLETED: "#1f7a3f",
    LightImageTaskStatus.FAILED: "#8b1e2d",
    LightImageTaskStatus.SKIPPED: "#4a4a4d",
    LightImageTaskStatus.STOPPED: "#6b2f2f",
}


class LightImageSignals(QObject):
    task_updated = Signal(object)
    log_added = Signal(str, str)
    done = Signal()
    scan_finished = Signal(object)
    scan_failed = Signal(str)


class LightImageToolWindow(QMainWindow):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("白底图 / 指定图生图批量生成工具")
        self.resize(1480, 860)
        self.setMinimumSize(900, 560)
        self.config = load_light_image_tool_config()
        self.api_adapter = LightImageApiAdapter()
        self.queue_manager = LightImageQueueManager()
        session = load_light_image_tool_session() if self.config.restore_last_session else {}
        restored_scan_results = restore_session_into_queue(self.queue_manager, session) if session else []
        self.log_manager = LightImageLogManager(self.queue_manager.state.queue_id)
        self.signals = LightImageSignals()
        self.executor = self._create_executor()
        self.manual_image_paths: list[str] = list(self.config.manual_image_paths or [])
        self.scan_results: list[PIDScanResult] = restored_scan_results
        self.selected_task_uid = str(session.get("selected_task_uid") or self.config.last_selected_task_uid or "")
        self._log_entries: list[tuple[str, str]] = []
        self._log_panel_collapsed = bool(self.config.log_panel_collapsed)
        self._detail_panel_collapsed = bool(self.config.detail_panel_collapsed)
        self._closing = False
        self._scan_thread: threading.Thread | None = None
        self._runtime_refresh_ticks = 0
        self.runtime_refresh_timer = QTimer(self)
        self.runtime_refresh_timer.setInterval(1000)
        self.runtime_refresh_timer.timeout.connect(self.on_runtime_refresh)

        self.signals.task_updated.connect(self.on_task_updated)
        self.signals.log_added.connect(self.append_log)
        self.signals.done.connect(self.on_executor_done)
        self.signals.scan_finished.connect(self.on_scan_finished)
        self.signals.scan_failed.connect(self.on_scan_failed)

        self._install_style()
        self._build_ui()
        self.refresh_provider_controls()
        self.apply_saved_ui_state()
        self.refresh_task_table()
        self.refresh_scan_table()
        self.refresh_kpis()
        self.append_log("INFO", "轻量图生图工具已就绪")

    def _create_executor(self) -> LightImageTaskExecutor:
        return LightImageTaskExecutor(
            self.queue_manager,
            self.api_adapter,
            self.log_manager,
            task_failed_retry_count=self.config.api_task_failed_retry_count,
            task_failed_retry_interval_seconds=self.config.api_task_failed_retry_interval_seconds,
            on_task_updated=self.signals.task_updated.emit,
            on_log=self.signals.log_added.emit,
            on_done=self.signals.done.emit,
        )

    def _install_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget#root { background: #0f1218; color: #f2f5fa; font-family: "Microsoft YaHei UI"; }
            QFrame#topBar, QFrame#panel, QFrame#detailPanel, QFrame#logPanel, QGroupBox {
                background: #1b1f27; border: 1px solid #343a46; border-radius: 8px;
            }
            QGroupBox { margin-top: 18px; padding-top: 12px; font-weight: 700; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #cbd5e1; }
            QLabel#title { font-size: 20px; font-weight: 800; color: #ffffff; }
            QLabel#muted, QLabel#smallMuted { color: #9aa4b2; }
            QLabel#smallMuted { font-size: 12px; }
            QLabel#kpiValue { font-size: 20px; font-weight: 800; color: #ffffff; }
            QLabel#kpiLabel { color: #9aa4b2; font-size: 12px; }
            QLineEdit, QTextEdit, QSpinBox, QComboBox, QTableWidget, QListWidget {
                background: #111722; color: #f2f5fa; border: 1px solid #343a46; border-radius: 6px;
                selection-background-color: #168bff; selection-color: #ffffff;
            }
            QTextEdit { padding: 8px; }
            QLineEdit, QSpinBox, QComboBox { min-height: 30px; padding: 3px 8px; }
            QPushButton {
                background: #252d3a; color: #f2f5fa; border: 1px solid #3a4250; border-radius: 6px;
                min-height: 30px; padding: 4px 10px;
            }
            QPushButton:hover { background: #303a4b; }
            QPushButton#primaryButton { background: #168bff; border-color: #168bff; color: white; font-weight: 700; }
            QPushButton#dangerButton { background: #8b1e2d; border-color: #b42336; color: white; font-weight: 700; }
            QPushButton#ghostButton { background: transparent; }
            QTabWidget::pane { border: 1px solid #343a46; border-radius: 8px; }
            QTabBar::tab { background: #1b1f27; color: #9aa4b2; padding: 8px 14px; border-top-left-radius: 6px; border-top-right-radius: 6px; }
            QTabBar::tab:selected { background: #263044; color: #ffffff; }
            QHeaderView::section { background: #202734; color: #cbd5e1; border: none; padding: 7px; }
            QTableWidget { gridline-color: #283040; }
            QFrame#dropPanel { background: #121824; border: 1px dashed #465166; border-radius: 8px; }
            QLabel#dropHint { color: #9aa4b2; }
            QLabel#previewImage {
                background: #111722; border: 1px solid #343a46; border-radius: 6px; color: #9aa4b2;
            }
            """
        )

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)
        root_layout.addWidget(self._build_top_bar())

        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setChildrenCollapsible(True)
        self.left_panel = self._build_left_panel()
        self.center_panel = self._build_center_panel()
        self.detail_panel = self._build_detail_panel()
        self.main_splitter.addWidget(self.left_panel)
        self.main_splitter.addWidget(self.center_panel)
        self.main_splitter.addWidget(self.detail_panel)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 0)
        detail_width = int((self.config.ui_layout or {}).get("detail_panel_width", 360))
        self.main_splitter.setSizes([360, 820, detail_width])
        root_layout.addWidget(self.main_splitter, 1)

    def _build_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topBar")
        layout = QGridLayout(bar)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        title = QLabel("轻量图生图工具")
        title.setObjectName("title")
        subtitle = QLabel("指定图片生成 + PID 白底图批量生成")
        subtitle.setObjectName("muted")
        self.provider_combo = QComboBox()
        self.model_combo = QComboBox()
        self.api_status_label = QLabel("API：检查中")
        self.api_status_label.setObjectName("smallMuted")
        self.global_output_edit = QLineEdit(self.config.default_save_dir)
        self.global_output_edit.setPlaceholderText("默认保存目录为空时使用 outputs/light_image_outputs")
        self.global_output_btn = QPushButton("选择")
        self.global_output_btn.clicked.connect(self.choose_global_output_dir)
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, max(1, int(self.config.max_concurrency)))
        self.concurrency_spin.setValue(min(int(self.config.concurrency), int(self.config.max_concurrency)))
        self.start_btn = QPushButton("开始生成")
        self.start_btn.setObjectName("primaryButton")
        self.pause_btn = QPushButton("暂停")
        self.resume_btn = QPushButton("继续")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("dangerButton")
        self.open_output_btn = QPushButton("打开输出目录")
        self.save_config_btn = QPushButton("保存配置")
        self.test_api_btn = QPushButton("测试 API")
        self.toggle_detail_btn = QPushButton("详情面板")
        self.toggle_log_btn = QPushButton("日志面板")

        self.provider_combo.currentIndexChanged.connect(self.on_provider_changed)
        self.model_combo.currentIndexChanged.connect(self.on_model_changed)
        self.start_btn.clicked.connect(self.start_queue)
        self.pause_btn.clicked.connect(self.pause_queue)
        self.resume_btn.clicked.connect(self.resume_queue)
        self.stop_btn.clicked.connect(self.confirm_stop_queue)
        self.open_output_btn.clicked.connect(self.open_default_output_dir)
        self.save_config_btn.clicked.connect(self.save_current_config)
        self.test_api_btn.clicked.connect(self.test_api_status)
        self.toggle_detail_btn.clicked.connect(self.toggle_detail_panel)
        self.toggle_log_btn.clicked.connect(self.toggle_log_panel)

        layout.addWidget(title, 0, 0)
        layout.addWidget(subtitle, 1, 0)
        layout.addWidget(QLabel("API 平台"), 0, 1)
        layout.addWidget(self.provider_combo, 0, 2)
        layout.addWidget(QLabel("模型"), 0, 3)
        layout.addWidget(self.model_combo, 0, 4)
        layout.addWidget(self.api_status_label, 0, 5)
        layout.addWidget(QLabel("输出目录"), 1, 1)
        layout.addWidget(self.global_output_edit, 1, 2, 1, 3)
        layout.addWidget(self.global_output_btn, 1, 5)
        layout.addWidget(QLabel("并发"), 0, 6)
        layout.addWidget(self.concurrency_spin, 0, 7)
        action_row = QHBoxLayout()
        for btn in [
            self.start_btn,
            self.pause_btn,
            self.resume_btn,
            self.stop_btn,
            self.open_output_btn,
            self.save_config_btn,
            self.test_api_btn,
            self.toggle_detail_btn,
            self.toggle_log_btn,
        ]:
            action_row.addWidget(btn)
        layout.addLayout(action_row, 1, 6, 1, 4)
        layout.setColumnStretch(4, 1)
        return bar

    def _build_left_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setMinimumWidth(320)
        panel.setMaximumWidth(440)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        self.mode_tabs = QTabWidget()
        self.mode_tabs.addTab(self._build_manual_tab(), "指定图片生成")
        self.mode_tabs.addTab(self._build_pid_tab(), "PID 白底图生成")
        layout.addWidget(self.mode_tabs, 1)
        return panel

    def _build_manual_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        self.drop_list = ImageDropList()
        self.drop_list.files_added.connect(self.add_manual_images)
        self.drop_list.rejected.connect(lambda files: self.append_log("WARN", f"已忽略非图片文件 {len(files)} 个"))
        choose_btn = QPushButton("选择图片")
        remove_btn = QPushButton("移除选中")
        clear_btn = QPushButton("清空图片")
        choose_btn.setObjectName("primaryButton")
        choose_btn.clicked.connect(self.choose_manual_images)
        remove_btn.clicked.connect(self.remove_selected_manual_image)
        clear_btn.clicked.connect(self.clear_manual_images)
        row = QHBoxLayout()
        row.addWidget(choose_btn)
        row.addWidget(remove_btn)
        row.addWidget(clear_btn)
        self.manual_prompt_edit = QTextEdit()
        self.manual_prompt_edit.setPlainText(self.config.manual_prompt_text)
        self.manual_prompt_edit.setPlaceholderText("输入图生图提示词")
        self.manual_generation_spin = QSpinBox()
        self.manual_generation_spin.setRange(1, 50)
        self.manual_generation_spin.setValue(int(self.config.manual_generation_count or self.config.generation_count))
        self.manual_save_dir_edit = QLineEdit(self.config.manual_save_dir or self.config.default_save_dir)
        self.manual_save_dir_edit.setPlaceholderText("为空时使用顶部输出目录或默认目录")
        manual_save_btn = QPushButton("选择保存目录")
        manual_save_btn.clicked.connect(lambda: self.choose_directory_into(self.manual_save_dir_edit))
        add_queue_btn = QPushButton("加入队列")
        add_queue_btn.setObjectName("primaryButton")
        add_queue_btn.clicked.connect(self.add_manual_tasks_to_queue)

        layout.addWidget(self.drop_list, 2)
        layout.addLayout(row)
        layout.addWidget(QLabel("提示词"))
        layout.addWidget(self.manual_prompt_edit, 2)
        layout.addWidget(QLabel("生成次数"))
        layout.addWidget(self.manual_generation_spin)
        layout.addWidget(QLabel("保存目录"))
        save_row = QHBoxLayout()
        save_row.addWidget(self.manual_save_dir_edit, 1)
        save_row.addWidget(manual_save_btn)
        layout.addLayout(save_row)
        layout.addWidget(add_queue_btn)
        return page

    def _build_pid_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        self.pid_input_edit = QTextEdit()
        self.pid_input_edit.setPlainText(self.config.pid_input_text)
        self.pid_input_edit.setPlaceholderText("输入单个 PID、多行 PID 或逗号分隔 PID")
        self.pid_input_edit.setMaximumHeight(100)
        self.product_root_edit = QLineEdit(self.config.product_root_dir)
        self.product_info_subdir_edit = QLineEdit(self.config.product_info_subdir)
        self.output_subdir_edit = QLineEdit(self.config.default_output_subdir)
        root_btn = QPushButton("选择根目录")
        root_btn.clicked.connect(lambda: self.choose_directory_into(self.product_root_edit))
        self.pid_match_combo = QComboBox()
        self.pid_match_combo.addItem("精确匹配", "exact")
        self.pid_match_combo.addItem("忽略大小写", "case_insensitive")
        self.pid_match_combo.addItem("包含匹配", "contains")
        self.set_combo_value(self.pid_match_combo, self.config.pid_match_mode)
        self.image_rule_combo = QComboBox()
        for label, value in [
            ("前 10 张", "first_10"),
            ("前 5 张", "first_5"),
            ("后 10 张", "last_10"),
            ("全部图片", "all"),
            ("指定文件名", "custom_names"),
            ("自定义范围", "custom_range"),
        ]:
            self.image_rule_combo.addItem(label, value)
        self.set_combo_value(self.image_rule_combo, self.image_rule_combo_value_from_config())
        self.custom_names_edit = QTextEdit("\n".join(self.config.custom_image_names))
        self.custom_names_edit.setPlaceholderText("指定文件名，每行一个")
        self.custom_names_edit.setMaximumHeight(72)
        self.range_start_spin = QSpinBox()
        self.range_start_spin.setRange(1, 999)
        self.range_start_spin.setValue(int(self.config.custom_range_start or 1))
        self.range_end_spin = QSpinBox()
        self.range_end_spin.setRange(1, 999)
        self.range_end_spin.setValue(int(self.config.custom_range_end or 10))
        self.pid_generation_spin = QSpinBox()
        self.pid_generation_spin.setRange(1, 50)
        self.pid_generation_spin.setValue(int(self.config.pid_generation_count or self.config.generation_count))
        self.white_prompt_edit = QTextEdit(self.config.white_bg_prompt_template or DEFAULT_WHITE_BG_PROMPT)
        self.white_prompt_edit.setMinimumHeight(120)
        save_template_btn = QPushButton("保存模板")
        restore_template_btn = QPushButton("恢复默认")
        insert_vars_btn = QPushButton("插入变量")
        save_template_btn.clicked.connect(self.save_current_config)
        restore_template_btn.clicked.connect(lambda: self.white_prompt_edit.setPlainText(DEFAULT_WHITE_BG_PROMPT))
        insert_vars_btn.clicked.connect(lambda: self.white_prompt_edit.insertPlainText("{pid} {image_name} {image_index}/{total_images}"))
        scan_btn = QPushButton("扫描 PID")
        scan_btn.setObjectName("primaryButton")
        add_queue_btn = QPushButton("加入队列")
        add_queue_btn.setObjectName("primaryButton")
        scan_btn.clicked.connect(self.scan_pids)
        add_queue_btn.clicked.connect(self.add_pid_tasks_to_queue)

        form = QGridLayout()
        form.addWidget(QLabel("PID 队列"), 0, 0, 1, 3)
        form.addWidget(self.pid_input_edit, 1, 0, 1, 3)
        form.addWidget(QLabel("资料根目录"), 2, 0)
        form.addWidget(self.product_root_edit, 2, 1)
        form.addWidget(root_btn, 2, 2)
        form.addWidget(QLabel("图片子目录"), 3, 0)
        form.addWidget(self.product_info_subdir_edit, 3, 1, 1, 2)
        form.addWidget(QLabel("输出子目录"), 4, 0)
        form.addWidget(self.output_subdir_edit, 4, 1, 1, 2)
        form.addWidget(QLabel("PID 匹配"), 5, 0)
        form.addWidget(self.pid_match_combo, 5, 1, 1, 2)
        form.addWidget(QLabel("图片规则"), 6, 0)
        form.addWidget(self.image_rule_combo, 6, 1, 1, 2)
        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("起"))
        range_row.addWidget(self.range_start_spin)
        range_row.addWidget(QLabel("止"))
        range_row.addWidget(self.range_end_spin)
        form.addWidget(QLabel("自定义范围"), 7, 0)
        form.addLayout(range_row, 7, 1, 1, 2)
        form.addWidget(QLabel("指定文件名"), 8, 0, 1, 3)
        form.addWidget(self.custom_names_edit, 9, 0, 1, 3)
        form.addWidget(QLabel("生成次数"), 10, 0)
        form.addWidget(self.pid_generation_spin, 10, 1, 1, 2)
        layout.addLayout(form)
        layout.addWidget(QLabel("白底图生成模板"))
        layout.addWidget(self.white_prompt_edit, 1)
        template_row = QHBoxLayout()
        template_row.addWidget(save_template_btn)
        template_row.addWidget(restore_template_btn)
        template_row.addWidget(insert_vars_btn)
        layout.addLayout(template_row)
        action_row = QHBoxLayout()
        action_row.addWidget(scan_btn)
        action_row.addWidget(add_queue_btn)
        layout.addLayout(action_row)
        return page

    def _build_center_panel(self) -> QSplitter:
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(True)
        top = QFrame()
        top.setObjectName("panel")
        layout = QVBoxLayout(top)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.addWidget(self._build_kpi_row())
        layout.addWidget(self._build_queue_actions())
        self.tables_tabs = QTabWidget()
        self.task_table = QTableWidget(0, len(TASK_COLUMNS))
        self.task_table.setHorizontalHeaderLabels(TASK_COLUMNS)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.task_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.task_table.itemSelectionChanged.connect(self.on_task_selection_changed)
        self.task_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.task_table.horizontalHeader().setStretchLastSection(False)
        self.task_table.verticalHeader().setDefaultSectionSize(int(self.config.ui_layout.get("task_table_row_height", 36)))
        self.scan_table = QTableWidget(0, len(SCAN_COLUMNS))
        self.scan_table.setHorizontalHeaderLabels(SCAN_COLUMNS)
        self.scan_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.scan_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.scan_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.tables_tabs.addTab(self.task_table, "任务队列")
        self.tables_tabs.addTab(self.scan_table, "扫描结果")
        layout.addWidget(self.tables_tabs, 1)
        splitter.addWidget(top)
        self.log_panel = self._build_log_panel()
        splitter.addWidget(self.log_panel)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([680, int(self.config.ui_layout.get("log_panel_height", 150))])
        self.center_splitter = splitter
        return splitter

    def _build_kpi_row(self) -> QWidget:
        row = QWidget()
        layout = QGridLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        self.kpi_labels: dict[str, QLabel] = {}
        for index, key in enumerate(["队列总数", "等待中", "生成中", "已完成", "失败/跳过"]):
            card = QFrame()
            card.setObjectName("panel")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 8, 10, 8)
            value = QLabel("0")
            value.setObjectName("kpiValue")
            label = QLabel(key)
            label.setObjectName("kpiLabel")
            card_layout.addWidget(value)
            card_layout.addWidget(label)
            self.kpi_labels[key] = value
            layout.addWidget(card, 0, index)
        return row

    def _build_queue_actions(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        self.retry_selected_btn = QPushButton("重试所选")
        self.retry_failed_btn = QPushButton("重试失败")
        self.retry_skipped_btn = QPushButton("重试跳过")
        self.clear_completed_btn = QPushButton("清空已完成")
        self.clear_queue_btn = QPushButton("清空队列")
        self.clear_queue_btn.setObjectName("dangerButton")
        self.export_results_btn = QPushButton("导出结果")
        for btn in [
            self.retry_selected_btn,
            self.retry_failed_btn,
            self.retry_skipped_btn,
            self.clear_completed_btn,
            self.export_results_btn,
            self.clear_queue_btn,
        ]:
            layout.addWidget(btn)
        layout.addStretch(1)
        self.retry_selected_btn.clicked.connect(self.retry_selected_task)
        self.retry_failed_btn.clicked.connect(self.retry_failed_tasks)
        self.retry_skipped_btn.clicked.connect(self.retry_skipped_tasks)
        self.clear_completed_btn.clicked.connect(self.clear_completed_tasks)
        self.clear_queue_btn.clicked.connect(self.confirm_clear_queue)
        self.export_results_btn.clicked.connect(self.export_results)
        return row

    def _build_detail_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("detailPanel")
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(460)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        title = QLabel("任务详情 / 预览")
        title.setObjectName("title")
        self.source_preview = QLabel("首张参考图预览")
        self.source_preview.setObjectName("previewImage")
        self.source_preview.setAlignment(Qt.AlignCenter)
        self.source_preview.setMinimumHeight(160)
        self.result_preview = QLabel("生成结果预览")
        self.result_preview.setObjectName("previewImage")
        self.result_preview.setAlignment(Qt.AlignCenter)
        self.result_preview.setMinimumHeight(160)
        self.detail_text = QTextEdit("选择任务后查看图片、结果和错误详情")
        self.detail_text.setReadOnly(True)
        detail_btn_row = QGridLayout()
        buttons = [
            ("打开首张参考图", self.open_selected_source),
            ("打开结果", self.open_selected_result),
            ("打开目录", self.open_selected_output_dir),
            ("复制路径", self.copy_selected_output_path),
            ("复制错误", self.copy_selected_error),
            ("完整日志", self.show_selected_task_log),
        ]
        for index, (text, slot) in enumerate(buttons):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            detail_btn_row.addWidget(btn, index // 2, index % 2)
        layout.addWidget(title)
        layout.addWidget(self.source_preview)
        layout.addWidget(self.result_preview)
        layout.addWidget(self.detail_text, 1)
        layout.addLayout(detail_btn_row)
        return panel

    def _build_log_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("logPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 10)
        header = QHBoxLayout()
        title = QLabel("运行日志")
        title.setObjectName("title")
        self.info_filter = QCheckBox("INFO")
        self.warn_filter = QCheckBox("WARN")
        self.error_filter = QCheckBox("ERROR")
        self.auto_scroll_check = QCheckBox("自动滚动")
        for cb in [self.info_filter, self.warn_filter, self.error_filter, self.auto_scroll_check]:
            cb.setChecked(True)
            cb.stateChanged.connect(self.refresh_log_text)
        self.copy_log_btn = QPushButton("复制日志")
        self.clear_log_btn = QPushButton("清空显示")
        self.open_log_btn = QPushButton("打开日志文件")
        self.copy_log_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.log_text.toPlainText()))
        self.clear_log_btn.clicked.connect(self.clear_log_display)
        self.open_log_btn.clicked.connect(lambda: self.open_path(str(self.log_manager.run_log_path)))
        for widget in [title, self.info_filter, self.warn_filter, self.error_filter, self.auto_scroll_check, self.copy_log_btn, self.clear_log_btn, self.open_log_btn]:
            header.addWidget(widget)
        header.addStretch(1)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addLayout(header)
        layout.addWidget(self.log_text, 1)
        return panel

    def refresh_provider_controls(self) -> None:
        default_provider, default_model = self.api_adapter.default_provider_model()
        provider = self.config.selected_image_provider or default_provider
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        for key, name in self.api_adapter.provider_options():
            self.provider_combo.addItem(name, key)
        self.provider_combo.blockSignals(False)
        self.set_combo_value(self.provider_combo, provider)
        self.refresh_model_combo(self.config.selected_image_model or default_model)
        self.refresh_api_status()

    def refresh_model_combo(self, selected: str | None = None) -> None:
        provider = self.current_provider()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for key, name in self.api_adapter.model_options(provider):
            self.model_combo.addItem(name, key)
        self.model_combo.blockSignals(False)
        self.set_combo_value(self.model_combo, selected)

    def on_provider_changed(self) -> None:
        self.refresh_model_combo(None)
        self.refresh_api_status()

    def on_model_changed(self) -> None:
        self.config.selected_image_model = self.current_model()

    def current_provider(self) -> str:
        return str(self.provider_combo.currentData() or "")

    def current_model(self) -> str:
        return str(self.model_combo.currentData() or "")

    @staticmethod
    def set_combo_value(combo: QComboBox, value: str | None) -> None:
        if not value:
            return
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return

    def image_rule_combo_value_from_config(self) -> str:
        rule = str(self.config.image_select_rule or "first_n")
        count = int(self.config.image_select_count or 10)
        if rule == "first_n" and count == 5:
            return "first_5"
        if rule == "first_n":
            return "first_10"
        if rule == "last_n":
            return "last_10"
        if rule == "all":
            return "all"
        if rule == "custom_names":
            return "custom_names"
        if rule == "custom_range":
            return "custom_range"
        return "first_10"

    def apply_saved_ui_state(self) -> None:
        if self.manual_image_paths:
            self.drop_list.set_files(self.manual_image_paths)
        if hasattr(self, "mode_tabs"):
            self.mode_tabs.setCurrentIndex(max(0, min(self.mode_tabs.count() - 1, int(self.config.last_mode_index or 0))))
        if self._detail_panel_collapsed:
            self.detail_panel.setVisible(False)
        if self._log_panel_collapsed:
            self.log_panel.setVisible(False)

    def refresh_api_status(self) -> None:
        self.api_status_label.setText(f"API：{self.api_adapter.masked_api_key_status(self.current_provider())}")

    def add_manual_images(self, paths: list[str]) -> None:
        accepted = [path for path in paths if Path(path).is_file() and is_supported_image(path)]
        ignored = len(paths) - len(accepted)
        for path in accepted:
            if path not in self.manual_image_paths:
                self.manual_image_paths.append(path)
        self.drop_list.set_files(self.manual_image_paths)
        self.append_log("INFO", f"已选择图片 {len(accepted)} 张")
        if ignored:
            self.append_log("WARN", f"已忽略非图片文件 {ignored} 个")
        self.save_current_config(silent=True)

    def choose_manual_images(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择图片",
            "",
            "图片文件 (*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff);;所有文件 (*.*)",
        )
        if files:
            self.add_manual_images(files)

    def remove_selected_manual_image(self) -> None:
        selected = self.drop_list.list_widget.selectedItems()
        remove_paths = {str(item.data(Qt.UserRole) or "") for item in selected}
        self.manual_image_paths = [path for path in self.manual_image_paths if path not in remove_paths]
        self.drop_list.set_files(self.manual_image_paths)
        self.save_current_config(silent=True)

    def clear_manual_images(self) -> None:
        self.manual_image_paths.clear()
        self.drop_list.set_files([])
        self.save_current_config(silent=True)

    def add_manual_tasks_to_queue(self) -> None:
        prompt = self.manual_prompt_edit.toPlainText().strip()
        if not self.manual_image_paths:
            self.warn("请先选择图片")
            return
        if not prompt:
            self.warn("请先输入提示词")
            return
        output_dir = self.manual_save_dir_edit.text().strip() or self.global_output_edit.text().strip() or self.default_output_dir()
        tasks = self.queue_manager.add_manual_tasks(
            image_paths=list(self.manual_image_paths),
            prompt=prompt,
            generation_count=self.manual_generation_spin.value(),
            output_dir=output_dir,
            provider=self.current_provider(),
            model=self.current_model(),
        )
        self.queue_manager.save_state(self.log_manager.queue_dir)
        self.append_log("INFO", f"已加入指定图片任务 {len(tasks)} 个")
        self.refresh_task_table()
        self.refresh_kpis()
        self.save_current_config(silent=True)
        self.statusBar().showMessage(f"已加入队列：{len(tasks)} 个任务", 4000)

    def scan_pids(self) -> None:
        if self._scan_thread and self._scan_thread.is_alive():
            self.warn("PID 扫描仍在进行中")
            return
        pids = parse_pid_input(self.pid_input_edit.toPlainText())
        if not pids:
            self.warn("请先输入 PID")
            return
        rule, count, range_start, range_end = self.current_image_rule()
        custom_names = parse_custom_names(self.custom_names_edit.toPlainText())
        scanner = PIDScanner(
            product_root_dir=self.product_root_edit.text().strip(),
            product_info_subdir=self.product_info_subdir_edit.text().strip() or "01.product_info",
            output_subdir=self.output_subdir_edit.text().strip() or "01.产品白底图",
            pid_match_mode=str(self.pid_match_combo.currentData() or "exact"),
        )
        self.append_log("INFO", f"开始扫描 PID：{len(pids)} 个")

        def worker() -> None:
            try:
                results = scanner.scan_many(pids, rule, count, custom_names, range_start, range_end)
                self.signals.scan_finished.emit(results)
            except Exception as exc:
                self.signals.scan_failed.emit(str(exc))

        self._scan_thread = threading.Thread(target=worker, daemon=True)
        self._scan_thread.start()

    def current_image_rule(self) -> tuple[str, int, int | None, int | None]:
        value = str(self.image_rule_combo.currentData() or "first_10")
        if value == "first_5":
            return "first_n", 5, None, None
        if value == "first_10":
            return "first_n", 10, None, None
        if value == "last_10":
            return "last_n", 10, None, None
        if value == "all":
            return "all", 999, None, None
        if value == "custom_names":
            return "custom_names", 999, None, None
        if value == "custom_range":
            start = self.range_start_spin.value()
            end = self.range_end_spin.value()
            return "custom_range", max(1, end - start + 1), start, end
        return "first_n", 10, None, None

    def on_scan_finished(self, results: list[PIDScanResult]) -> None:
        self.scan_results = results
        success = sum(1 for item in results if item.status == "found")
        skipped = len(results) - success
        self.append_log("INFO", f"PID 扫描完成：成功 {success}，跳过 {skipped}")
        for item in results:
            if item.error_message:
                level = "WARN" if item.status == "found" else "ERROR"
                self.append_log(level, f"{item.pid}：{item.error_message}")
        self.refresh_scan_table()
        self.tables_tabs.setCurrentWidget(self.scan_table)
        self.save_current_config(silent=True)

    def on_scan_failed(self, message: str) -> None:
        self.append_log("ERROR", f"PID 扫描失败：{message}")
        self.warn(f"PID 扫描失败：{message}")

    def add_pid_tasks_to_queue(self) -> None:
        if not self.scan_results:
            self.warn("请先扫描 PID")
            return
        tasks = self.queue_manager.add_pid_scan_tasks(
            scan_results=self.scan_results,
            prompt_template=self.white_prompt_edit.toPlainText(),
            generation_count=self.pid_generation_spin.value(),
            provider=self.current_provider(),
            model=self.current_model(),
        )
        if not tasks:
            self.warn("没有可加入队列的 PID 图片")
            return
        self.queue_manager.save_state(self.log_manager.queue_dir)
        self.append_log("INFO", f"已加入 PID 白底图任务 {len(tasks)} 个")
        self.refresh_task_table()
        self.refresh_kpis()
        self.tables_tabs.setCurrentWidget(self.task_table)
        self.save_current_config(silent=True)

    def start_queue(self) -> None:
        if not self.queue_manager.tasks:
            self.warn("队列为空，请先加入任务")
            return
        if self.executor.running:
            self.warn("队列已经在运行")
            return
        waiting_count = sum(1 for task in self.queue_manager.tasks if task.status == LightImageTaskStatus.WAITING)
        if waiting_count <= 0:
            recovered = self.queue_manager.reset_active_to_waiting()
            if recovered:
                self.append_log("WARN", f"已恢复残留排队任务 {recovered} 个")
                waiting_count = recovered
        if waiting_count <= 0:
            self.warn("没有等待中的任务，请先重试失败任务或重新加入队列")
            return
        concurrency = min(self.concurrency_spin.value(), int(self.config.max_concurrency))
        if not self.executor.start(concurrency):
            self.warn("队列未能启动，请稍后重试")
            return
        self.statusBar().showMessage("队列已开始生成", 4000)
        self.append_log("INFO", f"已提交等待任务 {waiting_count} 个，并发 {concurrency}")
        self.refresh_task_table()
        self.refresh_kpis()
        self.update_buttons()
        self.start_runtime_refresh_timer()
        self.save_current_config(silent=True)

    def pause_queue(self) -> None:
        self.executor.pause()
        self.update_buttons()

    def resume_queue(self) -> None:
        self.executor.resume()
        self.update_buttons()

    def confirm_stop_queue(self) -> None:
        if not self.executor.running:
            return
        if QMessageBox.question(self, "确认停止", "停止后不再提交新任务，运行中的任务会自然结束。是否继续？") == QMessageBox.Yes:
            self.executor.stop("用户确认停止")
            self.update_buttons()

    def on_executor_done(self) -> None:
        self.runtime_refresh_timer.stop()
        self.refresh_task_table()
        self.refresh_kpis()
        self.update_buttons()
        self.save_current_config(silent=True)

    def on_task_updated(self, task: LightImageTask | None) -> None:
        if self._closing:
            return
        self.refresh_task_table()
        self.refresh_kpis()
        if task is not None and task.task_uid == self.selected_task_uid:
            self.show_task_detail(task)
        self.save_current_session()

    def start_runtime_refresh_timer(self) -> None:
        self._runtime_refresh_ticks = 0
        if not self.runtime_refresh_timer.isActive():
            self.runtime_refresh_timer.start()

    def on_runtime_refresh(self) -> None:
        self.refresh_task_table()
        self.refresh_kpis()
        self.update_buttons()
        if self.selected_task_uid:
            task = next((item for item in self.queue_manager.tasks if item.task_uid == self.selected_task_uid), None)
            if task is not None:
                self.show_task_detail(task)
        self._runtime_refresh_ticks += 1
        if self._runtime_refresh_ticks % 3 == 0:
            self.save_current_session()
        if not self.executor.running:
            self.runtime_refresh_timer.stop()
            self.save_current_session()

    def update_buttons(self) -> None:
        running = self.executor.running
        self.start_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running and not self.executor.paused)
        self.resume_btn.setEnabled(running and self.executor.paused)
        self.stop_btn.setEnabled(running)

    @staticmethod
    def task_reference_summary(task: LightImageTask) -> str:
        references = task.reference_image_paths or [task.source_image_path]
        first_name = Path(references[0]).name if references else task.source_image_name
        if len(references) <= 1:
            return first_name
        return f"{first_name} 等 {len(references)} 张"

    @staticmethod
    def task_reference_detail(task: LightImageTask) -> str:
        references = task.reference_image_paths or [task.source_image_path]
        if not references:
            return "-"
        shown = "\n".join(f"{index}. {path}" for index, path in enumerate(references[:20], start=1))
        if len(references) > 20:
            shown += f"\n... 另有 {len(references) - 20} 张"
        return shown

    def refresh_task_table(self) -> None:
        tasks = self.queue_manager.tasks
        self.task_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            values = [
                str(row + 1),
                "白底图" if task.task_type == "pid_white_bg" else "指定图生图",
                task.pid or "",
                self.task_reference_summary(task),
                task.status,
                "100%" if task.status in LightImageTaskStatus.TERMINAL else ("50%" if task.status in LightImageTaskStatus.ACTIVE else "0%"),
                f"{task.generation_index}/{task.generation_count}",
                Path(task.output_file_path or "").name if task.output_file_path else "",
                task.error_message or task.skip_reason or "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(Qt.UserRole, task.task_uid)
                if col == 4:
                    item.setBackground(QColor(STATUS_COLORS.get(task.status, "#303846")))
                self.task_table.setItem(row, col, item)
            action_widget = QWidget()
            action_layout = QHBoxLayout(action_widget)
            action_layout.setContentsMargins(2, 2, 2, 2)
            detail_btn = QPushButton("详情")
            retry_btn = QPushButton("重试")
            detail_btn.clicked.connect(lambda _=False, t=task: self.show_task_detail(t))
            retry_btn.clicked.connect(lambda _=False, t=task: self.retry_task(t))
            action_layout.addWidget(detail_btn)
            action_layout.addWidget(retry_btn)
            self.task_table.setCellWidget(row, 9, action_widget)
        self.task_table.resizeColumnsToContents()
        self.task_table.setColumnWidth(3, max(160, self.task_table.columnWidth(3)))
        self.task_table.setColumnWidth(8, max(220, self.task_table.columnWidth(8)))

    def refresh_scan_table(self) -> None:
        self.scan_table.setRowCount(len(self.scan_results))
        for row, result in enumerate(self.scan_results):
            values = [
                result.pid,
                SCAN_STATUS_TEXT.get(result.status, result.status),
                result.pid_dir or "",
                "存在" if result.product_info_dir and result.status in {"found", "no_images"} else "",
                str(len(result.selected_images)),
                result.output_dir or "",
                result.error_message or "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 1 and result.status != "found":
                    item.setBackground(QColor("#6b2f2f"))
                self.scan_table.setItem(row, col, item)
            action_widget = QWidget()
            action_layout = QHBoxLayout(action_widget)
            action_layout.setContentsMargins(2, 2, 2, 2)
            open_pid_btn = QPushButton("PID")
            open_info_btn = QPushButton("图片")
            open_out_btn = QPushButton("输出")
            open_pid_btn.clicked.connect(lambda _=False, r=result: self.open_path(r.pid_dir or ""))
            open_info_btn.clicked.connect(lambda _=False, r=result: self.open_path(r.product_info_dir or ""))
            open_out_btn.clicked.connect(lambda _=False, r=result: self.open_path(r.output_dir or ""))
            action_layout.addWidget(open_pid_btn)
            action_layout.addWidget(open_info_btn)
            action_layout.addWidget(open_out_btn)
            self.scan_table.setCellWidget(row, 7, action_widget)
        self.scan_table.resizeColumnsToContents()

    def refresh_kpis(self) -> None:
        state = self.queue_manager.refresh_state()
        self.kpi_labels["队列总数"].setText(str(state.total_count))
        self.kpi_labels["等待中"].setText(str(state.pending_count))
        self.kpi_labels["生成中"].setText(str(state.running_count))
        self.kpi_labels["已完成"].setText(str(state.completed_count))
        self.kpi_labels["失败/跳过"].setText(str(state.failed_count + state.skipped_count))

    def on_task_selection_changed(self) -> None:
        row = self.task_table.currentRow()
        if row < 0 or row >= len(self.queue_manager.tasks):
            return
        self.show_task_detail(self.queue_manager.tasks[row])

    def selected_task(self) -> LightImageTask | None:
        if self.selected_task_uid:
            for task in self.queue_manager.tasks:
                if task.task_uid == self.selected_task_uid:
                    return task
        row = self.task_table.currentRow()
        if 0 <= row < len(self.queue_manager.tasks):
            return self.queue_manager.tasks[row]
        return None

    def show_task_detail(self, task: LightImageTask) -> None:
        self.selected_task_uid = task.task_uid
        reference_count = len(task.reference_image_paths or [task.source_image_path])
        reference_detail = self.task_reference_detail(task)
        text = (
            f"任务类型：{'白底图' if task.task_type == 'pid_white_bg' else '指定图生图'}\n"
            f"PID：{task.pid or '-'}\n"
            f"状态：{task.status}\n"
            f"参考图数量：{reference_count}\n"
            f"参考图：\n{reference_detail}\n"
            f"输出文件：{task.output_file_path or '-'}\n"
            f"API 平台：{task.api_provider or '-'}\n"
            f"模型：{task.api_model or '-'}\n"
            f"错误原因：{task.error_message or task.skip_reason or '-'}\n\n"
            f"Prompt：\n{task.prompt}\n\n"
            f"任务日志：\n" + "\n".join(f"[{entry.level}] {entry.message}" for entry in task.logs[-20:])
        )
        self.detail_text.setPlainText(text)
        self.load_preview(self.source_preview, task.source_image_path, "首张参考图预览")
        self.load_preview(self.result_preview, task.output_file_path or "", "生成结果预览")
        if self._detail_panel_collapsed:
            self.toggle_detail_panel()

    @staticmethod
    def load_preview(label: QLabel, path_text: str, fallback: str) -> None:
        path = Path(str(path_text or ""))
        if not path.exists() or not path.is_file():
            label.setText(fallback)
            label.setPixmap(QPixmap())
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            label.setText(fallback)
            return
        label.setText("")
        label.setPixmap(pixmap.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def append_log(self, level: str, message: str) -> None:
        level = "WARN" if level == "WARNING" else level
        self._log_entries.append((level, message))
        self.refresh_log_text()
        self.statusBar().showMessage(message, 3500)

    def refresh_log_text(self) -> None:
        visible = set()
        if self.info_filter.isChecked():
            visible.add("INFO")
        if self.warn_filter.isChecked():
            visible.update({"WARN", "WARNING"})
        if self.error_filter.isChecked():
            visible.add("ERROR")
        filtered = [(level, text) for level, text in self._log_entries if level in visible]
        max_lines = int(self.config.ui_layout.get("log_max_visible_lines", 100))
        lines = [f"[{level}] {text}" for level, text in filtered[-max_lines:]]
        self.log_text.setPlainText("\n".join(lines))
        if self.auto_scroll_check.isChecked():
            self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def clear_log_display(self) -> None:
        self._log_entries.clear()
        self.refresh_log_text()
        self.statusBar().showMessage("已清空日志面板显示，真实日志文件未删除", 4000)

    def retry_task(self, task: LightImageTask) -> None:
        count = self.queue_manager.retry_tasks([task])
        self.append_log("INFO", f"已重试所选任务 {count} 个")
        self.refresh_task_table()
        self.refresh_kpis()
        self.save_current_config(silent=True)

    def retry_selected_task(self) -> None:
        task = self.selected_task()
        if task:
            self.retry_task(task)

    def retry_failed_tasks(self) -> None:
        count = self.queue_manager.retry_failed()
        self.append_log("INFO", f"失败任务已重新入队：{count} 个")
        self.refresh_task_table()
        self.refresh_kpis()
        self.save_current_config(silent=True)

    def retry_skipped_tasks(self) -> None:
        count = self.queue_manager.retry_skipped()
        self.append_log("INFO", f"跳过任务已重新入队：{count} 个")
        self.refresh_task_table()
        self.refresh_kpis()
        self.save_current_config(silent=True)

    def clear_completed_tasks(self) -> None:
        count = self.queue_manager.clear_completed()
        self.append_log("INFO", f"已清空完成任务 {count} 个")
        self.refresh_task_table()
        self.refresh_kpis()
        self.save_current_config(silent=True)

    def confirm_clear_queue(self) -> None:
        if self.executor.running:
            self.warn("队列运行中，不能清空")
            return
        if QMessageBox.question(self, "确认清空队列", "清空队列会移除当前所有任务显示，不删除已生成文件。是否继续？") != QMessageBox.Yes:
            return
        self.runtime_refresh_timer.stop()
        self.queue_manager.clear_all()
        self.log_manager = LightImageLogManager(self.queue_manager.state.queue_id)
        self.executor = self._create_executor()
        self.selected_task_uid = ""
        self.refresh_task_table()
        self.refresh_kpis()
        self.update_buttons()
        self.append_log("WARN", "队列已清空")
        self.save_current_config(silent=True)

    def export_results(self) -> None:
        self.log_manager.exports_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_manager.exports_dir / f"light_image_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        payload = [asdict(task) for task in self.queue_manager.tasks]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.append_log("INFO", f"结果已导出：{path}")

    def save_current_config(self, silent: bool = False) -> None:
        self.config.product_root_dir = self.product_root_edit.text().strip() if hasattr(self, "product_root_edit") else self.config.product_root_dir
        self.config.product_info_subdir = self.product_info_subdir_edit.text().strip() if hasattr(self, "product_info_subdir_edit") else self.config.product_info_subdir
        self.config.default_output_subdir = self.output_subdir_edit.text().strip() if hasattr(self, "output_subdir_edit") else self.config.default_output_subdir
        self.config.default_save_dir = self.global_output_edit.text().strip()
        self.config.manual_prompt_text = self.manual_prompt_edit.toPlainText() if hasattr(self, "manual_prompt_edit") else self.config.manual_prompt_text
        self.config.manual_image_paths = list(self.manual_image_paths)
        self.config.manual_save_dir = self.manual_save_dir_edit.text().strip() if hasattr(self, "manual_save_dir_edit") else self.config.manual_save_dir
        self.config.manual_generation_count = self.manual_generation_spin.value() if hasattr(self, "manual_generation_spin") else self.config.manual_generation_count
        self.config.pid_input_text = self.pid_input_edit.toPlainText() if hasattr(self, "pid_input_edit") else self.config.pid_input_text
        self.config.pid_generation_count = self.pid_generation_spin.value() if hasattr(self, "pid_generation_spin") else self.config.pid_generation_count
        self.config.generation_count = self.config.manual_generation_count
        self.config.concurrency = self.concurrency_spin.value()
        self.config.pid_match_mode = str(self.pid_match_combo.currentData() or "exact") if hasattr(self, "pid_match_combo") else self.config.pid_match_mode
        rule, count, _range_start, _range_end = self.current_image_rule() if hasattr(self, "image_rule_combo") else (self.config.image_select_rule, self.config.image_select_count, None, None)
        self.config.image_select_rule = rule
        self.config.image_select_count = count
        self.config.custom_range_start = self.range_start_spin.value() if hasattr(self, "range_start_spin") else self.config.custom_range_start
        self.config.custom_range_end = self.range_end_spin.value() if hasattr(self, "range_end_spin") else self.config.custom_range_end
        self.config.custom_image_names = parse_custom_names(self.custom_names_edit.toPlainText()) if hasattr(self, "custom_names_edit") else self.config.custom_image_names
        self.config.white_bg_prompt_template = self.white_prompt_edit.toPlainText() if hasattr(self, "white_prompt_edit") else self.config.white_bg_prompt_template
        self.config.selected_image_provider = self.current_provider()
        self.config.selected_image_model = self.current_model()
        self.config.last_mode_index = self.mode_tabs.currentIndex() if hasattr(self, "mode_tabs") else self.config.last_mode_index
        self.config.last_selected_task_uid = self.selected_task_uid
        self.config.detail_panel_collapsed = self._detail_panel_collapsed
        self.config.log_panel_collapsed = self._log_panel_collapsed
        save_light_image_tool_config(self.config)
        self.save_current_session()
        if not silent:
            self.append_log("INFO", "轻量工具配置已保存")

    def save_current_session(self) -> None:
        try:
            save_light_image_tool_session(
                queue_manager=self.queue_manager,
                scan_results=self.scan_results,
                selected_task_uid=self.selected_task_uid,
                ui_state={
                    "mode_index": self.mode_tabs.currentIndex() if hasattr(self, "mode_tabs") else 0,
                    "detail_panel_collapsed": self._detail_panel_collapsed,
                    "log_panel_collapsed": self._log_panel_collapsed,
                },
            )
        except OSError:
            pass

    def test_api_status(self) -> None:
        status = self.api_adapter.masked_api_key_status(self.current_provider())
        if status == "未配置":
            self.warn("图生图 API Key 未配置，请先在主程序参数配置中保存 image profile")
        else:
            self.append_log("INFO", f"API Key 状态：{status}")

    def choose_global_output_dir(self) -> None:
        self.choose_directory_into(self.global_output_edit)

    def choose_directory_into(self, edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择目录", edit.text().strip() or str(PROJECT_ROOT))
        if path:
            edit.setText(path)

    def default_output_dir(self) -> str:
        day = datetime.now().strftime("%Y-%m-%d")
        path = PROJECT_ROOT / "outputs" / "light_image_outputs" / day / self.queue_manager.state.queue_id
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def open_default_output_dir(self) -> None:
        task = self.selected_task()
        if task and task.output_file_path:
            self.open_path(str(Path(task.output_file_path).parent))
            return
        path = self.global_output_edit.text().strip() or self.default_output_dir()
        self.open_path(path)

    def open_selected_source(self) -> None:
        task = self.selected_task()
        if task:
            self.open_path(task.source_image_path)

    def open_selected_result(self) -> None:
        task = self.selected_task()
        if task:
            self.open_path(task.output_file_path or "")

    def open_selected_output_dir(self) -> None:
        task = self.selected_task()
        if task:
            target = Path(task.output_file_path).parent if task.output_file_path else Path(task.output_dir)
            self.open_path(str(target))

    def copy_selected_output_path(self) -> None:
        task = self.selected_task()
        if task:
            QApplication.clipboard().setText(task.output_file_path or task.output_dir or "")
            self.statusBar().showMessage("已复制输出路径", 3000)

    def copy_selected_error(self) -> None:
        task = self.selected_task()
        if task:
            QApplication.clipboard().setText(task.error_message or task.skip_reason or "")
            self.statusBar().showMessage("已复制错误信息", 3000)

    def show_selected_task_log(self) -> None:
        task = self.selected_task()
        if not task:
            return
        QApplication.clipboard().setText("\n".join(f"[{entry.time}] [{entry.level}] {entry.message}" for entry in task.logs))
        self.statusBar().showMessage("任务日志已复制", 3000)

    def toggle_detail_panel(self) -> None:
        self._detail_panel_collapsed = not self._detail_panel_collapsed
        self.detail_panel.setVisible(not self._detail_panel_collapsed)
        self.save_current_config(silent=True)

    def toggle_log_panel(self) -> None:
        self._log_panel_collapsed = not self._log_panel_collapsed
        self.log_panel.setVisible(not self._log_panel_collapsed)
        self.save_current_config(silent=True)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        compact = self.width() < int(self.config.ui_layout.get("compact_breakpoint", 1500))
        if compact and self.config.ui_layout.get("detail_panel_collapsed_in_compact", True) and not self._detail_panel_collapsed:
            self._detail_panel_collapsed = True
            self.detail_panel.setVisible(False)
        if compact and self.config.ui_layout.get("log_panel_collapsed_in_compact", True) and not self._log_panel_collapsed:
            self._log_panel_collapsed = True
            self.log_panel.setVisible(False)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True
        self.runtime_refresh_timer.stop()
        self.save_current_config(silent=True)
        if self.executor.running:
            self.executor.stop("窗口关闭")
            self.executor.detach_ui_callbacks()
        event.accept()

    def warn(self, message: str) -> None:
        self.append_log("WARN", message)
        QMessageBox.warning(self, "提示", message)

    @staticmethod
    def open_path(path_or_url: str) -> None:
        value = str(path_or_url or "").strip()
        if not value:
            return
        try:
            if value.startswith(("http://", "https://")):
                QDesktopServices.openUrl(QUrl(value))
            else:
                os.startfile(value)  # type: ignore[attr-defined]
        except OSError:
            pass
