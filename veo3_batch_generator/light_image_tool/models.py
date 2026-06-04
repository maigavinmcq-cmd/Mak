from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_queue_id() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_" + uuid4().hex[:6]


class LightImageTaskStatus:
    WAITING = "等待中"
    QUEUED = "排队中"
    GENERATING = "生成中"
    DOWNLOADING = "下载中"
    COMPLETED = "已完成"
    FAILED = "失败"
    SKIPPED = "已跳过"
    STOPPED = "已停止"

    ACTIVE = {QUEUED, GENERATING, DOWNLOADING}
    TERMINAL = {COMPLETED, FAILED, SKIPPED, STOPPED}


@dataclass
class LightImageToolConfig:
    config_version: str = "light_image_tool_v1"
    product_root_dir: str = r"\\192.168.1.6\004.短视频运营中心\01.产品信息\SG"
    default_output_subdir: str = "01.产品白底图"
    product_info_subdir: str = "01.product_info"
    white_bg_prompt_template: str = ""
    image_select_rule: str = "first_n"
    image_select_count: int = 10
    custom_image_names: list[str] = field(default_factory=list)
    pid_match_mode: str = "exact"
    generation_count: int = 1
    concurrency: int = 1
    max_concurrency: int = 100
    default_save_dir: str = ""
    auto_open_output_dir: bool = False
    reuse_veo3_image_api_profile: bool = True
    selected_image_provider: str | None = None
    selected_image_model: str | None = None
    manual_prompt_text: str = ""
    manual_image_paths: list[str] = field(default_factory=list)
    manual_save_dir: str = ""
    manual_generation_count: int = 1
    pid_input_text: str = ""
    pid_generation_count: int = 1
    custom_range_start: int = 1
    custom_range_end: int = 10
    api_task_failed_retry_count: int = 3
    api_task_failed_retry_interval_seconds: int = 8
    last_mode_index: int = 0
    restore_last_session: bool = True
    last_selected_task_uid: str = ""
    detail_panel_collapsed: bool = False
    log_panel_collapsed: bool = False
    ui_layout: dict[str, Any] = field(default_factory=dict)


@dataclass
class LightImageTaskLogEntry:
    time: str
    level: str
    category: str
    task_uid: str | None
    message: str
    detail: str | None = None


@dataclass
class LightImageTask:
    task_uid: str
    task_type: str
    status: str
    pid: str | None
    source_image_path: str
    source_image_name: str
    prompt: str
    prompt_template_name: str | None
    generation_index: int
    generation_count: int
    output_dir: str
    output_file_path: str | None
    output_url: str | None
    api_provider: str | None
    api_model: str | None
    error_message: str | None
    skip_reason: str | None
    created_at: str
    started_at: str | None
    ended_at: str | None
    updated_at: str
    reference_image_paths: list[str] = field(default_factory=list)
    logs: list[LightImageTaskLogEntry] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        *,
        task_type: str,
        source_image_path: str,
        prompt: str,
        generation_index: int,
        generation_count: int,
        output_dir: str,
        pid: str | None = None,
        prompt_template_name: str | None = None,
        api_provider: str | None = None,
        api_model: str | None = None,
        reference_image_paths: list[str] | None = None,
    ) -> "LightImageTask":
        stamp = now_text()
        references = [str(path) for path in (reference_image_paths or [source_image_path]) if str(path or "").strip()]
        return cls(
            task_uid=uuid4().hex,
            task_type=task_type,
            status=LightImageTaskStatus.WAITING,
            pid=pid,
            source_image_path=source_image_path,
            source_image_name=str(source_image_path).replace("\\", "/").rsplit("/", 1)[-1],
            prompt=prompt,
            prompt_template_name=prompt_template_name,
            generation_index=generation_index,
            generation_count=generation_count,
            output_dir=output_dir,
            output_file_path=None,
            output_url=None,
            api_provider=api_provider,
            api_model=api_model,
            error_message=None,
            skip_reason=None,
            created_at=stamp,
            started_at=None,
            ended_at=None,
            updated_at=stamp,
            reference_image_paths=references,
            logs=[],
        )

    def add_log(self, level: str, category: str, message: str, detail: str | None = None) -> None:
        entry = LightImageTaskLogEntry(now_text(), level, category, self.task_uid, message, detail)
        self.logs.append(entry)
        self.updated_at = entry.time


@dataclass
class PIDScanResult:
    pid: str
    status: str
    pid_dir: str | None
    product_info_dir: str | None
    output_dir: str | None
    selected_images: list[str]
    error_message: str | None
    candidate_dirs: list[str] = field(default_factory=list)
    missing_image_names: list[str] = field(default_factory=list)


@dataclass
class LightImageQueueState:
    queue_id: str
    created_at: str
    updated_at: str
    total_count: int
    pending_count: int
    running_count: int
    completed_count: int
    failed_count: int
    skipped_count: int
    stopped_count: int
    progress_percent: float
    status: str
    stop_requested: bool = False
    stop_reason: str | None = None

    @classmethod
    def empty(cls) -> "LightImageQueueState":
        stamp = now_text()
        return cls(
            queue_id=new_queue_id(),
            created_at=stamp,
            updated_at=stamp,
            total_count=0,
            pending_count=0,
            running_count=0,
            completed_count=0,
            failed_count=0,
            skipped_count=0,
            stopped_count=0,
            progress_percent=0.0,
            status="空闲",
        )
