from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.api.upload_api import upload_image_for_public_url
from app.api.provider_registry import get_image_provider, get_video_provider, model_display_name
from app.config import AppConfig
from app.diagnostics import DiagnosticsLogger
from app.file_utils import (
    download_video_to_path,
    find_product_image_for_task,
    generated_image_asset_path,
    save_image_task_assets,
    save_video_task_metadata,
    sync_task_urls_from_paths,
    video_download_output_path,
)
from app.logger import setup_logger
from app.models.task import TaskItem, TaskStatus, now_text
from app.task_logs import append_task_log
from app.task_manager import TaskManager
from app.workflow import (
    NODE_STATUS_BLOCKED,
    NODE_STATUS_COMPLETED,
    NODE_STATUS_FAILED,
    NODE_STATUS_PENDING,
    NODE_STATUS_POLLING,
    NODE_STATUS_READY,
    NODE_STATUS_RUNNING,
    NODE_STATUS_SKIPPED,
    NODE_STATUS_SUBMITTED,
    NODE_STATUS_WAITING_INPUT,
    NODE_TYPE_IMAGE,
    NODE_TYPE_VIDEO,
    TERMINAL_NODE_STATUSES,
    clone_workflow_definition,
    default_workflow_definition,
    ensure_task_node_states,
    workflow_nodes,
)
from app.workflow_executor import WorkflowExecutor, auto_migrate_tasks_to_workflow, _poll_result_is_explicit_terminal_failed


@dataclass
class ManualPollSummary:
    batch_id: str
    total_checked: int = 0
    skipped_no_task_id: int = 0
    skipped_in_progress: int = 0
    still_processing: int = 0
    completed: int = 0
    failed: int = 0
    error_count: int = 0


def _download_video_for_task_with_provider(task: TaskItem, config: AppConfig, save_path: Path) -> str:
    provider = get_video_provider(task.video_provider or config.video_provider)
    downloader = getattr(provider, "download_video_content", None)
    video_url = str(task.video_url or "").strip()
    task_id = str(task.video_task_id or "").strip()
    if callable(downloader) and task_id and video_url.rstrip("/").endswith("/content"):
        return str(
            downloader(
                task_id=task_id,
                api_key=config.video_api_key,
                output_path=save_path,
                extra_params={
                    "base_url": config.video_api_base_url,
                    "timeout": max(180, int(config.request_timeout_seconds)),
                },
            )
        )
    path, _downloaded = download_video_to_path(
        video_url,
        save_path,
        timeout=max(180, int(config.request_timeout_seconds)),
        retry_count=max(1, int(config.retry_count)),
        retry_interval_seconds=max(1, int(config.retry_interval_seconds)),
    )
    return path


class BatchWorker(QThread):
    task_updated = Signal(object)
    log_message = Signal(str, str, object)
    stats_updated = Signal(dict)
    finished_message = Signal(str)

    def __init__(
        self,
        manager: TaskManager,
        config: AppConfig,
        logger: logging.Logger | None,
        failed_only: bool = False,
        regenerate_existing_images: bool = False,
        poll_only: bool = False,
        execution_mode: str = "full",
        selected_task_keys: set[str] | None = None,
        polling_task_ids_in_progress: set[str] | None = None,
        polling_task_ids_lock: threading.Lock | None = None,
        workflow_definition: dict | None = None,
        log_dir: str | Path | None = None,
        log_path: str | Path | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.manager = manager
        self.config = config
        self.logger = logger or logging.getLogger(f"veo3_batch_generator.worker.{id(self)}")
        if logger is None:
            self.logger.handlers.clear()
            self.logger.addHandler(logging.NullHandler())
            self.logger.setLevel(logging.INFO)
        self._logger_ready = logger is not None
        self.log_dir = Path(log_dir) if log_dir else None
        self.log_path = Path(log_path) if log_path else None
        self.diagnostics = DiagnosticsLogger((self.log_dir / "diagnostics.log") if self.log_dir else None)
        self.failed_only = failed_only
        self.regenerate_existing_images = regenerate_existing_images
        self.poll_only = poll_only
        self.execution_mode = execution_mode or "full"
        self.selected_task_keys = selected_task_keys or set()
        self.workflow_definition = clone_workflow_definition(workflow_definition or default_workflow_definition())
        self._pause_event = threading.Event()
        self._stop_event = threading.Event()
        self._pause_event.set()
        self._state_lock = threading.Lock()
        self._pid_locks: dict[str, threading.Lock] = {}
        self._poll_thread: threading.Thread | None = None
        self._poll_now_event = threading.Event()
        self._last_state_save_time = 0.0
        self._state_dirty = False
        self._state_save_interval_seconds = 2.0
        self._download_executor: ThreadPoolExecutor | None = None
        self._download_task_ids_in_progress: set[str] = set()
        self._download_task_ids_lock = threading.Lock()
        self._workflow_drive_executor: ThreadPoolExecutor | None = None
        self._workflow_drive_task_keys_in_progress: set[str] = set()
        self._workflow_drive_lock = threading.Lock()
        self._workflow_runtime_target_keys: set[str] | None = None
        self._workflow_force_full_mode_task_keys: set[str] = set()
        self._manual_poll_now_requested = False
        self._manual_poll_now_lock = threading.Lock()
        self.polling_task_ids_in_progress = polling_task_ids_in_progress if polling_task_ids_in_progress is not None else set()
        self.polling_task_ids_lock = polling_task_ids_lock or threading.Lock()
        self._workflow_executor: WorkflowExecutor | None = None
        self._retry_failed_now_requested = False
        self._retry_failed_selected_keys: set[str] | None = None
        self._retry_failed_now_lock = threading.Lock()
        # Per-task signal emit throttling. Without this the Qt event queue
        # backs up on big batches and the GUI freezes — every non-forced
        # state mutation used to push a task_updated + stats_updated signal,
        # and stats_updated re-scanned the entire task list every time.
        self._emit_min_interval_seconds = 0.7
        self._last_emit_monotonic_per_task: dict[str, float] = {}
        self._emit_throttle_lock = threading.Lock()
        # Cached stats payload reused across throttled emits (only recomputed
        # when forced or when the cache has gone stale).
        self._stats_cache: dict | None = None
        self._stats_cache_monotonic: float = 0.0
        self._stats_cache_ttl_seconds = 0.7
        self.stop_source: str | None = None
        self.stop_reason: str | None = None
        self.last_worker_error: str | None = None
        self.last_exception_traceback: str | None = None
        self._last_perf_snapshot_monotonic: float = 0.0

    def _mark_stop(self, source: str, reason: str | None = None, *, set_event: bool = False) -> None:
        self.stop_source = source
        self.stop_reason = reason
        self.diagnostics.event("STOP_REASON", source=source, reason=reason or "")
        if set_event:
            self._stop_event.set()

    def _pending_download_hint(self) -> int:
        keys: set[str] = set()
        for task in self.manager.tasks:
            if task.video_url and not task.video_file_path:
                keys.add(f"{task.task_uid or task.pid}::legacy")
            for node_id, state in (task.node_states or {}).items():
                if not isinstance(state, dict):
                    continue
                if state.get("output_video_url") and not state.get("output_video_local_path"):
                    keys.add(f"{task.task_uid or task.pid}::{node_id}")
        return len(keys)

    def _effective_download_concurrency(self, pending_hint: int | None = None) -> int:
        configured = max(1, int(getattr(self.config, "download_concurrency", 20) or 20))
        pending = max(0, int(pending_hint if pending_hint is not None else self._pending_download_hint()))
        # Video URLs can expire quickly. Keep a practical floor for backlog
        # batches even if an old batch snapshot still says "3".
        backlog_floor = 20 if pending >= 20 else configured
        backlog_scaled = min(max(pending, 0), 50) if pending else configured
        return max(1, min(max(configured, backlog_floor, backlog_scaled), 100))

    def _create_download_executor(self, pending_hint: int | None = None) -> ThreadPoolExecutor:
        workers = self._effective_download_concurrency(pending_hint)
        configured = max(1, int(getattr(self.config, "download_concurrency", 20) or 20))
        pending = self._pending_download_hint() if pending_hint is None else max(0, int(pending_hint))
        self._log("INFO", f"[DOWNLOAD] download workers={workers}, configured={configured}, pending_hint={pending}")
        return ThreadPoolExecutor(max_workers=workers)

    def _ensure_file_logger(self) -> None:
        if self._logger_ready:
            return
        try:
            log_dir = self.log_dir or Path(self.config.output_dir)
            logger, _path = setup_logger(log_dir, self.log_path)
            self.logger = logger
            self._logger_ready = True
        except Exception as exc:
            # Keep the worker alive even if the network log directory stalls or
            # refuses writes. The GUI still receives log_message signals.
            self.logger = logging.getLogger(f"veo3_batch_generator.worker.fallback.{id(self)}")
            self.logger.handlers.clear()
            self.logger.addHandler(logging.NullHandler())
            self.logger.setLevel(logging.INFO)
            self._logger_ready = True
            self.log_message.emit("WARNING", f"批次日志文件暂时无法打开，执行会继续：{exc}", None)

    def _acquire_poll_lock(self, task_id: str) -> bool:
        task_id = str(task_id or "").strip()
        if not task_id:
            return True
        with self.polling_task_ids_lock:
            if task_id in self.polling_task_ids_in_progress:
                return False
            self.polling_task_ids_in_progress.add(task_id)
            return True

    def _release_poll_lock(self, task_id: str) -> None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        with self.polling_task_ids_lock:
            self.polling_task_ids_in_progress.discard(task_id)

    def _download_key(self, task: TaskItem) -> str:
        return str(task.video_task_id or f"{task.pid}:{task.row_index}").strip()

    def _acquire_download_lock(self, task: TaskItem) -> bool:
        key = self._download_key(task)
        if not key:
            return True
        with self._download_task_ids_lock:
            if key in self._download_task_ids_in_progress:
                return False
            self._download_task_ids_in_progress.add(key)
            return True

    def _release_download_lock(self, task: TaskItem) -> None:
        key = self._download_key(task)
        if not key:
            return
        with self._download_task_ids_lock:
            self._download_task_ids_in_progress.discard(key)

    def pause(self) -> None:
        self._pause_event.clear()
        self._log("INFO", "已请求暂停：暂停提交新任务和轮询视频")

    def resume(self) -> None:
        self._pause_event.set()
        self._log("INFO", "继续执行：恢复提交和轮询")

    def stop(self) -> None:
        self._mark_stop("USER_CLICK_STOP", "user requested stop", set_event=False)
        self._stop_event.set()
        self._pause_event.set()
        self._poll_now_event.set()
        self._log("INFO", "已请求停止：停止提交新任务和轮询")

    def request_poll_cycle(self) -> None:
        self._log("INFO", "Immediate poll requested")
        with self._manual_poll_now_lock:
            self._manual_poll_now_requested = True
        self._poll_now_event.set()

    def request_retry_failed(self, selected_task_keys: list[str] | tuple[str, ...] | set[str] | None = None) -> None:
        keys = {str(key) for key in (selected_task_keys or []) if str(key)}
        with self._retry_failed_now_lock:
            self._retry_failed_now_requested = True
            self._retry_failed_selected_keys = keys or None
        suffix = f" selected={len(keys)}" if keys else " all_failed"
        self._log("INFO", f"Runtime retry failed requested:{suffix}")
        self._poll_now_event.set()

    def _consume_retry_failed_request(self) -> set[str] | None | bool:
        with self._retry_failed_now_lock:
            if not self._retry_failed_now_requested:
                return False
            keys = self._retry_failed_selected_keys
            self._retry_failed_now_requested = False
            self._retry_failed_selected_keys = None
            return keys

    def _consume_manual_poll_now_request(self) -> bool:
        with self._manual_poll_now_lock:
            requested = bool(self._manual_poll_now_requested)
            self._manual_poll_now_requested = False
            return requested

    def _wait_for_next_poll_interval(self, interval: int | float) -> None:
        deadline = time.monotonic() + max(0.0, float(interval or 0))
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            signaled = self._poll_now_event.wait(timeout=remaining)
            if not signaled:
                return
            self._poll_now_event.clear()
            if self._consume_manual_poll_now_request():
                return

    def _max_auto_retries(self) -> int:
        return max(1, int(getattr(self.config, "retry_count", 1) or 1))

    def _is_failure_status(self, status: str) -> bool:
        return status.startswith("FAILED") or status in {TaskStatus.VIDEO_FAILED, TaskStatus.VIDEO_TIMEOUT}

    def _can_auto_retry(self, task: TaskItem) -> bool:
        return int(task.auto_retry_count or 0) < self._max_auto_retries()

    def _clear_failure_for_retry(self, task: TaskItem, stage: str, reason: str | None = None) -> None:
        if task.status in {TaskStatus.FAILED_VIDEO_API, TaskStatus.VIDEO_FAILED, TaskStatus.VIDEO_TIMEOUT}:
            stage = "video_submit" if stage == "image" else stage
        elif task.status == TaskStatus.FAILED_IMAGE_API:
            stage = "image"
        task.auto_retry_count = int(task.auto_retry_count or 0) + 1
        task.last_auto_retry_time = now_text()
        task.last_auto_retry_stage = stage
        task.last_auto_retry_reason = reason or task.error_message or task.status
        task.error_message = None
        task.ended_at = None
        task.elapsed_seconds = None
        if stage in {"video_submit", "video_poll"} or task.status in {TaskStatus.FAILED_VIDEO_API, TaskStatus.VIDEO_FAILED, TaskStatus.VIDEO_TIMEOUT}:
            task.video_task_id = None
            task.video_url = None
            task.video_file_path = None
            task.video_submit_time = None
            task.video_poll_start_time = None
            task.video_poll_end_time = None
            task.video_poll_count = 0
            task.video_download_status = ""
            task.last_video_download_error = None
            task.video_status = ""
            retry_status = TaskStatus.IMAGE_DONE if (task.generated_image_path or task.generated_image_url) else TaskStatus.PENDING
        elif stage == "image":
            if not (task.generated_image_path or task.generated_image_url):
                task.image_task_id = None
                task.image_status = ""
            retry_status = TaskStatus.PENDING
        else:
            retry_status = TaskStatus.PENDING
        task.set_status(retry_status)
        self._log(
            "INFO",
            f"[RETRY] PID={task.pid} row={task.row_index} 清除失败状态并重新入队 "
            f"stage={stage} retry={task.auto_retry_count}/{self._max_auto_retries()} reason={task.last_auto_retry_reason}",
            task,
        )
        self._save_emit(task, force=True)

    def _retry_or_keep_failed(self, task: TaskItem, stage: str, reason: str | None = None) -> bool:
        if not self._is_failure_status(task.status):
            return False
        if not self._can_auto_retry(task):
            self._log(
                "ERROR",
                f"[RETRY] PID={task.pid} row={task.row_index} 自动重试次数已用完 "
                f"{task.auto_retry_count}/{self._max_auto_retries()}，保留失败状态: {task.error_message}",
                task,
            )
            return False
        self._clear_failure_for_retry(task, stage, reason)
        delay = max(0, int(getattr(self.config, "retry_interval_seconds", 0) or 0))
        if delay:
            time.sleep(min(delay, 30))
        return True

    def _reset_failed_task_for_queue(self, task: TaskItem, stage: str) -> bool:
        if not self._is_failure_status(task.status):
            return True
        if self.failed_only and not self._can_auto_retry(task):
            task.auto_retry_count = 0
        if not self._can_auto_retry(task):
            self._log(
                "ERROR",
                f"[RETRY] PID={task.pid} row={task.row_index} 自动重试次数已用完 "
                f"{task.auto_retry_count}/{self._max_auto_retries()}，不再重新入队",
                task,
            )
            return False
        self._clear_failure_for_retry(task, stage, task.error_message or task.status)
        return True

    def _select_targets(self) -> list[TaskItem]:
        candidates = [
            task
            for task in self.manager.tasks
            if not self.selected_task_keys or (task.task_uid or TaskManager.task_uid_for(task)) in self.selected_task_keys
        ]
        if self.failed_only:
            return [t for t in candidates if TaskStatus.is_failed_or_skipped(t.status)]
        if self.execution_mode == "image_only":
            return [
                t
                for t in candidates
                if not TaskStatus.is_done(t.status)
                and (self.regenerate_existing_images or not (t.generated_image_path or t.generated_image_url))
            ]
        if self.execution_mode == "video_only":
            return [
                t
                for t in candidates
                if not TaskStatus.is_done(t.status)
                and not t.video_task_id
                and not t.video_url
                and (t.generated_image_path or t.generated_image_url)
            ]
        excluded = {
            TaskStatus.VIDEO_DOWNLOADED,
            TaskStatus.VIDEO_SUBMITTED,
            TaskStatus.VIDEO_POLLING,
        }
        return [t for t in candidates if t.status not in excluded]

    def _select_workflow_targets(self) -> list[TaskItem]:
        candidates = [
            task
            for task in self.manager.tasks
            if not self.selected_task_keys or (task.task_uid or TaskManager.task_uid_for(task)) in self.selected_task_keys
        ]
        if self.failed_only:
            return [
                task
                for task in candidates
                if TaskStatus.is_failed_or_skipped(task.status)
                or any(str(state.get("status") or "") == "FAILED" for state in (task.node_states or {}).values() if isinstance(state, dict))
            ]
        return candidates

    def _ensure_generated_image(self, task: TaskItem, product_image: str, start: float) -> bool:
        task.image_provider = task.image_provider or self.config.image_provider
        task.image_model_logical_key = task.image_model_logical_key or self.config.image_model_logical_key
        image_provider = get_image_provider(task.image_provider)
        task.image_model_display = task.image_model_display or model_display_name(image_provider, task.image_model_logical_key)
        existing_image_path = task.generated_image_path or ""
        existing_image_ok = bool(existing_image_path)
        if existing_image_path:
            try:
                existing_image_ok = Path(existing_image_path).exists()
            except OSError:
                # Network shares may reject metadata checks while the path is still valid for the API flow.
                existing_image_ok = True
        existing_image_url_ok = bool(task.generated_image_url and task.generated_image_url.startswith(("http://", "https://", "data:image")))
        if (existing_image_ok or existing_image_url_ok) and not self.regenerate_existing_images:
            sync_task_urls_from_paths(task, self.config)
            self._set_status(task, TaskStatus.IMAGE_DONE)
            self._log("INFO", f"[SUBMIT] PID={task.pid} row={task.row_index} reuse generated image", task)
            return True

        if task.image_task_id and not self.regenerate_existing_images:
            poll_image_task = getattr(image_provider, "poll_image_task", None)
            if callable(poll_image_task):
                self._set_status(task, TaskStatus.GENERATING_IMAGE)
                self._log("INFO", f"[IMAGE] PID={task.pid} row={task.row_index} resume image polling, task_id={task.image_task_id}", task)
                image_result = poll_image_task(
                    task.image_task_id,
                    self.config.image_api_key,
                    {
                        "output_path": str(self._image_save_path(task)),
                        "base_url": self.config.image_api_base_url,
                        "timeout": self.config.request_timeout_seconds,
                        "poll_interval_seconds": self.config.poll_interval_seconds,
                        "max_poll_count": self.config.max_poll_count,
                    },
                )
                task.image_raw_response = image_result.raw_response
                if image_result.success:
                    task.generated_image_path = image_result.image_path
                    task.generated_image_url = image_result.image_url
                    if image_result.task_id:
                        task.image_task_id = image_result.task_id
                    sync_task_urls_from_paths(task, self.config)
                    self._set_status(task, TaskStatus.IMAGE_DONE, force=True)
                    self._refresh_image_assets_metadata(task)
                    self._log("INFO", f"[IMAGE] PID={task.pid} row={task.row_index} resumed image task completed", task)
                    return True
                if image_result.task_id and str(image_result.status or "").lower() in {"queued", "pending", "submitted", "in_progress", "processing", "running", "timeout", "poll_timeout"}:
                    task.image_task_id = image_result.task_id
                    task.image_raw_response = image_result.raw_response
                    self._set_status(task, TaskStatus.GENERATING_IMAGE, force=True)
                    self._log("INFO", f"[IMAGE] PID={task.pid} row={task.row_index} image still processing, task_id={task.image_task_id}", task)
                    return False
                self._set_status(task, TaskStatus.FAILED_IMAGE_API, image_result.error_message or "图生图任务续跑轮询失败")
                self._finish_failed(task, start)
                return False

        task.image_model_display = model_display_name(image_provider, task.image_model_logical_key)

        self._set_status(task, TaskStatus.GENERATING_IMAGE)
        self._log(
            "INFO",
            f"[SUBMIT] PID={task.pid} row={task.row_index} start image generation, provider={image_provider.provider_name}, model={task.image_model_display}",
            task,
        )
        image_result = image_provider.generate_image(
            product_image_path=product_image,
            prompt=task.image_prompt,
            model_logical_key=task.image_model_logical_key,
            api_key=self.config.image_api_key,
            extra_params={
                "output_path": str(self._image_save_path(task)),
                "base_url": self.config.image_api_base_url,
                "size": self.config.image_size,
                "retry_count": self.config.retry_count,
                "retry_interval_seconds": self.config.retry_interval_seconds,
                "timeout": self.config.request_timeout_seconds,
                "poll_interval_seconds": self.config.poll_interval_seconds,
                "max_poll_count": self.config.max_poll_count,
                "on_task_id": lambda task_id, raw=None: self._record_image_task_id(task, task_id, raw),
            },
        )
        if not image_result.success:
            if image_result.task_id and str(image_result.status or "").lower() in {"queued", "pending", "submitted", "in_progress", "processing", "running", "timeout", "poll_timeout"}:
                self._record_image_task_id(task, image_result.task_id, image_result.raw_response)
                self._set_status(task, TaskStatus.GENERATING_IMAGE, force=True)
                self._log("INFO", f"[IMAGE] PID={task.pid} row={task.row_index} image still processing, task_id={task.image_task_id}", task)
                return False
            self._set_status(task, TaskStatus.FAILED_IMAGE_API, image_result.error_message)
            self._finish_failed(task, start)
            return False
        task.generated_image_path = image_result.image_path
        task.generated_image_url = image_result.image_url
        if image_result.task_id:
            self._record_image_task_id(task, image_result.task_id, image_result.raw_response)
        task.image_raw_response = image_result.raw_response
        sync_task_urls_from_paths(task, self.config)
        self._set_status(task, TaskStatus.IMAGE_DONE)
        if self.config.auto_save_image_assets:
            try:
                save_image_task_assets(task, self.config.image_assets_root)
            except Exception as exc:
                self._log("ERROR", f"[ASSETS] PID={task.pid} row={task.row_index} save image assets failed: {exc}", task)
        self._log("INFO", f"[SUBMIT] PID={task.pid} row={task.row_index} image generation finished", task)

        return True

    def _record_image_task_id(self, task: TaskItem, task_id: str | None, raw_response=None) -> None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        if task.image_task_id == task_id:
            return
        task.image_task_id = task_id
        if raw_response is not None:
            task.image_raw_response = raw_response
        self._log("INFO", f"[IMAGE] PID={task.pid} row={task.row_index} image task_id saved: {task_id}", task)
        self._save_emit(task, force=True)

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wait_if_paused()
            if self._stop_event.is_set():
                break
            self._poll_cycle()
            interval = max(1, int(self.config.poll_interval_seconds))
            self._wait_for_next_poll_interval(interval)

    def _poll_cycle(self) -> None:
        self._queue_pending_video_downloads()
        tasks = [
            task
            for task in self.manager.tasks
            if task.status in {TaskStatus.VIDEO_SUBMITTED, TaskStatus.VIDEO_POLLING}
            and task.video_task_id
            and not task.video_url
        ]
        if not tasks:
            self._log("INFO", "No video tasks need polling")
            return
        self._log("INFO", f"Start polling video tasks: {len(tasks)}；提示：生成失败的任务默认自动重试")
        max_workers = max(1, min(len(tasks), int(self.config.poll_concurrency), 100))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(self._poll_one, task) for task in tasks]
            for future in as_completed(futures):
                if self._stop_event.is_set():
                    break
                try:
                    future.result()
                except Exception as exc:
                    self._log("ERROR", f"Video polling thread failed: {exc}")

    def _poll_one(self, task: TaskItem) -> None:
        task_id = str(task.video_task_id or "").strip()
        if not self._acquire_poll_lock(task_id):
            self._log("INFO", f"[POLL] task_id={task_id} is already polling, skip this auto cycle", task)
            return
        try:
            self._poll_one_unlocked(task)
        finally:
            self._release_poll_lock(task_id)

    def _poll_one_unlocked(self, task: TaskItem) -> None:
        task.video_poll_start_time = task.video_poll_start_time or now_text()
        task.video_poll_count += 1
        self._set_status(task, TaskStatus.VIDEO_POLLING)
        self._log("INFO", f"[POLL] PID={task.pid} row={task.row_index} round={task.video_poll_count} task_id={task.video_task_id}", task)

        task.video_provider = task.video_provider or self.config.video_provider
        video_provider = get_video_provider(task.video_provider)
        result = video_provider.poll_video_task(
            task_id=task.video_task_id or "",
            api_key=self.config.video_api_key,
            extra_params={
                "base_url": self.config.video_api_base_url,
                "timeout": self.config.request_timeout_seconds,
            },
        )
        task.video_raw_response = result.raw_response
        if result.finished and result.video_url:
            task.video_url = result.video_url
            task.video_poll_end_time = now_text()
            task.video_download_status = ""
            task.last_video_download_error = None
            self._set_status(task, TaskStatus.VIDEO_DOWNLOAD_PENDING, force=True)
            self._refresh_image_assets_metadata(task)
            self._queue_completed_video_download(task)
            self._log("INFO", f"[POLL] PID={task.pid} row={task.row_index} video completed: {task.video_url}", task)
            return
        if result.failed:
            task.video_poll_end_time = now_text()
            task.error_message = result.error_message
            self._set_status(task, TaskStatus.FAILED_VIDEO_API, result.error_message, force=True)
            self._log("ERROR", f"[POLL] PID={task.pid} row={task.row_index} video failed: {result.error_message}", task)
            if self._retry_or_keep_failed(task, "video_poll", result.error_message):
                self._run_video_submit_stage(task)
            return
        self._set_status(task, TaskStatus.VIDEO_POLLING)
        self._log("INFO", f"[POLL] PID={task.pid} row={task.row_index} video not ready, status={result.status}", task)

    def _resubmit_video_from_poll(self, task: TaskItem) -> None:
        self._set_status(task, TaskStatus.VIDEO_SUBMITTING)
        task.video_provider = task.video_provider or self.config.video_provider
        task.video_model_logical_key = task.video_model_logical_key or self.config.video_model_logical_key
        video_provider = get_video_provider(task.video_provider)
        task.video_model_display = model_display_name(video_provider, task.video_model_logical_key)
        image_source = self._preferred_video_image_source(task, video_provider)
        if bool(getattr(video_provider, "requires_remote_image_url", False)) and not image_source:
            self._set_status(task, TaskStatus.FAILED_VIDEO_API, task.error_message or "Remote-image provider requires a public image URL")
            return
        submit_result = video_provider.submit_video_task(
            image_source=image_source,
            prompt=task.video_prompt,
            model_logical_key=task.video_model_logical_key,
            api_key=self.config.video_api_key,
            extra_params={
                "base_url": self.config.video_api_base_url,
                "orientation": self.config.video_orientation,
                "resolution": self.config.video_resolution,
                "retry_count": self.config.retry_count,
                "retry_interval_seconds": self.config.retry_interval_seconds,
                "timeout": self.config.request_timeout_seconds,
            },
        )
        task.video_raw_response = submit_result.raw_response
        if submit_result.success and submit_result.task_id:
            task.video_task_id = submit_result.task_id
            task.video_submit_time = now_text()
            task.video_poll_start_time = None
            self._set_status(task, TaskStatus.VIDEO_SUBMITTED)
            self._refresh_image_assets_metadata(task)
            self._log("INFO", f"[SUBMIT] PID={task.pid} row={task.row_index} resubmit success: {task.video_task_id}", task)
        else:
            self._set_status(task, TaskStatus.FAILED_VIDEO_API, submit_result.error_message or "视频任务重新提交失败")

    def _local_video_fallback_root(self) -> Path:
        return Path.home() / "Downloads" / "Veo3下载视频"

    def _download_video_to_root(self, task: TaskItem, root: str | Path) -> str:
        save_path = video_download_output_path(root, task, self.config.group_by_owner)
        path = _download_video_for_task_with_provider(task, self.config, save_path)
        task.video_file_path = path
        task.video_download_status = "DOWNLOADED"
        task.last_video_download_error = None
        sync_task_urls_from_paths(task, self.config)
        save_video_task_metadata(task, root, self.config.group_by_owner)
        return path

    def _download_completed_video(self, task: TaskItem) -> bool:
        if not task.video_url or not self.config.auto_download_video:
            return False
        task.video_download_status = "DOWNLOADING"
        task.last_video_download_time = now_text()
        task.video_download_attempt_count += 1
        try:
            self._download_video_to_root(task, self.config.video_download_root)
            return True
        except Exception as primary_exc:
            fallback_root = self._local_video_fallback_root()
            try:
                self._log(
                    "ERROR",
                    f"[DOWNLOAD] PID={task.pid} row={task.row_index} primary archive failed, trying local fallback: {primary_exc}",
                    task,
                )
                self._download_video_to_root(task, fallback_root)
                self._log("INFO", f"[DOWNLOAD] PID={task.pid} row={task.row_index} saved to local fallback: {task.video_file_path}", task)
                return True
            except Exception as fallback_exc:
                exc = RuntimeError(f"primary={primary_exc}; local_fallback={fallback_exc}")
            task.last_video_download_time = now_text()
            task.last_video_download_error = str(exc)
            task.error_message = f"Video download failed; will retry before URL expires: {exc}"
            if task.video_task_id:
                old_url = task.video_url
                task.video_url = None
                task.video_download_status = "WAITING_NEW_URL"
                task.set_status(TaskStatus.VIDEO_SUBMITTED, task.error_message)
                task.video_status = TaskStatus.VIDEO_SUBMITTED
                self._log(
                    "ERROR",
                    f"[DOWNLOAD] PID={task.pid} row={task.row_index} failed after retries; old URL may expire, re-poll task_id={task.video_task_id}: {old_url}",
                    task,
                )
            else:
                task.video_download_status = "FAILED_FINAL"
                self._log("ERROR", f"[DOWNLOAD] PID={task.pid} row={task.row_index} failed and no task_id is available for URL refresh: {exc}", task)
            return False

    def _queue_completed_video_download(self, task: TaskItem) -> None:
        if not task.video_url or not self.config.auto_download_video:
            return
        if task.video_file_path:
            return
        if not self._acquire_download_lock(task):
            return
        if not self._download_executor:
            try:
                downloaded = self._download_completed_video(task)
                if downloaded:
                    self._set_status(task, TaskStatus.VIDEO_DOWNLOADED, force=True)
                else:
                    self._save_emit(task, force=True)
            finally:
                self._release_download_lock(task)
            return
        self._download_executor.submit(self._download_task_and_emit, task)

    def _download_task_and_emit(self, task: TaskItem) -> None:
        try:
            downloaded = self._download_completed_video(task)
            if downloaded:
                self._set_status(task, TaskStatus.VIDEO_DOWNLOADED, force=True)
                self._log("INFO", f"[DOWNLOAD] PID={task.pid} row={task.row_index} saved: {task.video_file_path}", task)
            else:
                self._save_emit(task, force=True)
                if task.error_message:
                    self._log("ERROR", f"[DOWNLOAD] PID={task.pid} row={task.row_index} failed: {task.error_message}", task)
        finally:
            self._release_download_lock(task)

    def _image_save_path(self, task: TaskItem) -> Path:
        return generated_image_asset_path(self.config.image_assets_root, task)

    def _video_save_path(self, task: TaskItem) -> Path:
        return video_download_output_path(self.config.video_download_root, task, self.config.group_by_owner)

    def _preferred_video_image_source(self, task: TaskItem, video_provider) -> str:
        """Pick the best image_source for video submission.

        Preference is to feed the LOCAL generated image directly so the provider can
        encode the bytes (e.g. data URL) without any netdisk->URL conversion roundtrip.
        Providers that require a remote URL (e.g. JimmyAI Veo) report it via the
        `requires_remote_image_url` attribute and we fall back to the HTTP URL.
        """
        local_path = (task.generated_image_path or "").strip()
        remote_url = (task.generated_image_url or "").strip()
        requires_remote = bool(getattr(video_provider, "requires_remote_image_url", False))
        if requires_remote:
            if remote_url:
                return remote_url
            if local_path:
                return self._upload_generated_image_for_remote_video(task, local_path)
            return ""
        if local_path:
            try:
                if Path(local_path).exists():
                    return local_path
            except OSError:
                # Network share may transiently refuse stat; still try the local path
                # because file_to_data_url will reopen and surface a concrete error.
                return local_path
        return remote_url or local_path

    def _remote_video_input_upload_api_url(self) -> str:
        return str(getattr(self.config, "image_upload_api_url", "") or "").strip() or "oss://sora2-mission"

    def _upload_generated_image_for_remote_video(self, task: TaskItem, local_path: str) -> str:
        path_text = str(local_path or "").strip()
        if not path_text:
            return ""
        try:
            if not Path(path_text).exists():
                task.error_message = f"OSS fallback upload failed: local image does not exist: {path_text}"
                self._log("ERROR", f"[SUBMIT] PID={task.pid} row={task.row_index} {task.error_message}", task)
                return ""
        except OSError:
            # Network shares may transiently refuse stat; the uploader will
            # reopen the file and return the exact failure if needed.
            pass

        try:
            timeout = max(30, int(getattr(self.config, "request_timeout_seconds", 120) or 120))
        except (TypeError, ValueError):
            timeout = 120
        public_url, raw_response, error = upload_image_for_public_url(
            path_text,
            self._remote_video_input_upload_api_url(),
            api_key=str(
                getattr(self.config, "image_upload_api_key", "")
                or getattr(self.config, "image_api_key", "")
                or ""
            ),
            file_field=str(getattr(self.config, "image_upload_file_field", "") or "file"),
            timeout=timeout,
        )
        if error or not public_url:
            task.error_message = f"OSS fallback upload failed: {error or 'upload API returned no URL'}"
            self._log("ERROR", f"[SUBMIT] PID={task.pid} row={task.row_index} {task.error_message}", task)
            return ""

        task.generated_image_url = public_url
        self._refresh_image_assets_metadata(task)
        self._save_emit(task, force=True)
        self._log(
            "INFO",
            f"[SUBMIT] PID={task.pid} row={task.row_index} OSS fallback uploaded generated image for remote video provider",
            task,
        )
        return public_url

    def _has_polling_tasks(self) -> bool:
        return any(
            task.status in {TaskStatus.VIDEO_SUBMITTED, TaskStatus.VIDEO_POLLING}
            and bool(task.video_task_id)
            and not task.video_url
            for task in self.manager.tasks
        )

    def _has_pending_video_downloads(self) -> bool:
        if not self.config.auto_download_video:
            return False
        return any(
            bool(task.video_url)
            and not task.video_file_path
            and task.video_download_status != "FAILED_FINAL"
            for task in self.manager.tasks
        )

    def _queue_pending_video_downloads(self) -> None:
        if not self.config.auto_download_video:
            return
        for task in self.manager.tasks:
            if task.video_url and not task.video_file_path and task.video_download_status != "FAILED_FINAL":
                self._queue_completed_video_download(task)

    def _finish_task(self, task: TaskItem, status: str, error: str | None, start: float) -> None:
        task.ended_at = now_text()
        task.elapsed_seconds = round(time.time() - start, 2)
        self._set_status(task, status, error, force=True)

    def _finish_failed(self, task: TaskItem, start: float) -> None:
        task.ended_at = now_text()
        task.elapsed_seconds = round(time.time() - start, 2)
        self._save_emit(task, force=True)
        self._log("ERROR", f"PID={task.pid} row={task.row_index} failed: {task.error_message}", task)

    def run(self) -> None:
        self._ensure_file_logger()
        self.diagnostics.event(
            "BATCH_RUN_START",
            task_count=len(self.manager.tasks),
            mode=self.execution_mode,
            poll_only=self.poll_only,
            failed_only=self.failed_only,
            image_concurrency=getattr(self.config, "image_concurrency", None),
            video_submit_concurrency=getattr(self.config, "video_submit_concurrency", None),
            poll_concurrency=getattr(self.config, "poll_concurrency", None),
            download_concurrency=getattr(self.config, "download_concurrency", None),
        )
        try:
            self._run_impl()
        except Exception as exc:
            tb = traceback.format_exc()
            self.last_worker_error = str(exc)
            self.last_exception_traceback = tb
            self._mark_stop("WORKER_EXCEPTION", str(exc), set_event=True)
            self.diagnostics.event("WORKER_CRASH", error=str(exc), traceback=tb)
            self._log("ERROR", f"[WORKER_CRASH] 执行线程异常退出，已保留当前进度，可点击继续执行：{exc}\n{tb}")
            try:
                self._flush_state()
            except Exception:
                self.diagnostics.event("WORKER_CRASH_FLUSH_FAILED", traceback=traceback.format_exc())
            self.finished_message.emit("执行线程异常退出，已保留当前进度，可点击继续执行")
        finally:
            self.diagnostics.event(
                "BATCH_RUN_END",
                stop_source=self.stop_source or "",
                stop_reason=self.stop_reason or "",
                worker_error=self.last_worker_error or "",
            )
            self.diagnostics.close()

    def _run_impl(self) -> None:
        # v2 workflow engine: drives execution via WorkflowExecutor walking the
        # per-batch workflow_definition. Keep image-only / video-only in v2 as
        # well; otherwise the legacy path can submit video directly from the
        # wrong source for the corrected multi-stage flow.
        use_v2 = bool(getattr(self.config, "enable_stage_based_workflow", False))
        if use_v2:
            self._run_workflow_engine_run()
            return

        if self.poll_only:
            self._log("INFO", "Start poll-only mode")
            self._download_executor = self._create_download_executor()
            self._poll_cycle()
            if self._download_executor:
                self._download_executor.shutdown(wait=True)
                self._download_executor = None
            self._flush_state()
            self.finished_message.emit("Poll-only finished")
            return

        self._download_executor = self._create_download_executor()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        targets = self._select_targets()
        if targets:
            image_workers = max(1, min(int(self.config.image_concurrency), 100))
            video_workers = max(1, min(int(self.config.video_submit_concurrency), 100))
            if self.execution_mode == "image_only":
                self._log("INFO", f"Start image-only pipeline: {len(targets)} tasks, image_concurrency={image_workers}")
                if self._poll_thread:
                    self._stop_event.set()
                    self._poll_thread.join(timeout=3)
                    self._poll_thread = None
                    self._stop_event.clear()
                with ThreadPoolExecutor(max_workers=max(1, min(image_workers, len(targets)))) as image_executor:
                    for future in as_completed([image_executor.submit(self._run_image_stage, task) for task in targets]):
                        if self._stop_event.is_set():
                            break
                        try:
                            future.result()
                        except Exception as exc:
                            self._log("ERROR", f"Image-only thread failed: {exc}")
                if self._download_executor:
                    self._download_executor.shutdown(wait=True)
                    self._download_executor = None
                self._flush_state()
                self.finished_message.emit("Image-only generation finished")
                return
            if self.execution_mode == "video_only":
                self._log("INFO", f"Start video-only submit pipeline: {len(targets)} tasks, video_submit_concurrency={video_workers}")
                with ThreadPoolExecutor(max_workers=max(1, min(video_workers, len(targets)))) as video_executor:
                    for future in as_completed([video_executor.submit(self._run_video_submit_stage, task) for task in targets]):
                        if self._stop_event.is_set():
                            break
                        try:
                            future.result()
                        except Exception as exc:
                            self._log("ERROR", f"Video-only submit thread failed: {exc}")
            else:
                self._log("INFO", f"Start image/video pipeline: {len(targets)} tasks, image_concurrency={image_workers}, video_submit_concurrency={video_workers}")
                self._run_pipeline(targets, image_workers, video_workers)
        else:
            self._log("INFO", "No new tasks to submit; polling existing video tasks only")

        self._queue_pending_video_downloads()
        while not self._stop_event.is_set() and (self._has_polling_tasks() or self._has_pending_video_downloads()):
            self._queue_pending_video_downloads()
            time.sleep(0.5)
        if not self._stop_event.is_set():
            self._mark_stop("NO_ACTIVE_WORK", "legacy queues drained", set_event=True)
        if self._poll_thread:
            self._poll_thread.join(timeout=3)
        if self._download_executor:
            self._download_executor.shutdown(wait=True)
            self._download_executor = None
        self._flush_state()
        self.finished_message.emit("Task submit and video polling finished")

    def _run_pipeline(self, targets: list[TaskItem], image_workers: int, video_workers: int) -> None:
        video_queue: queue.Queue[TaskItem | None] = queue.Queue()
        image_workers = max(1, min(image_workers, len(targets)))
        video_workers = max(1, min(video_workers, len(targets)))

        def video_consumer() -> None:
            while True:
                task = video_queue.get()
                try:
                    if task is None:
                        return
                    self._run_video_submit_stage(task)
                finally:
                    video_queue.task_done()

        with ThreadPoolExecutor(max_workers=image_workers) as image_executor, ThreadPoolExecutor(max_workers=video_workers) as video_executor:
            consumers = [video_executor.submit(video_consumer) for _ in range(video_workers)]
            image_futures = [image_executor.submit(self._run_image_stage, task) for task in targets]
            for future in as_completed(image_futures):
                if self._stop_event.is_set():
                    break
                try:
                    task = future.result()
                    if task is not None:
                        video_queue.put(task)
                except Exception as exc:
                    self._log("ERROR", f"Image stage thread failed: {exc}")
            for _ in consumers:
                video_queue.put(None)
            video_queue.join()
            for future in as_completed(consumers):
                try:
                    future.result()
                except Exception as exc:
                    self._log("ERROR", f"Video submit thread failed: {exc}")

    def _run_image_stage(self, task: TaskItem) -> TaskItem | None:
        while not self._stop_event.is_set():
            if not self._reset_failed_task_for_queue(task, "image"):
                return None
            result = self._run_image_stage_once(task)
            if result is not None or not self._retry_or_keep_failed(task, "image"):
                return result
        return None

    def _run_image_stage_once(self, task: TaskItem) -> TaskItem | None:
        if self._stop_event.is_set() or TaskStatus.is_done(task.status):
            return None
        if self.failed_only and task.status in {TaskStatus.FAILED_VIDEO_API, TaskStatus.VIDEO_FAILED, TaskStatus.VIDEO_TIMEOUT}:
            task.video_task_id = None
            task.video_url = None
            task.video_file_path = None
            task.video_poll_count = 0
            self._save_emit(task)

        self._wait_if_paused()
        if self._stop_event.is_set():
            return None

        start = time.time()
        task.started_at = task.started_at or now_text()
        task.ended_at = None
        task.error_message = None
        try:
            if task.video_task_id and not task.video_url:
                self._set_status(task, TaskStatus.VIDEO_SUBMITTED)
                self._poll_now_event.set()
                return None

            self._set_status(task, TaskStatus.CHECKING_PRODUCT_IMAGE)
            product_image, skipped_status, error = find_product_image_for_task(task, self.config)
            if skipped_status:
                self._finish_task(task, skipped_status, error, start)
                return None
            task.product_image_path = product_image
            sync_task_urls_from_paths(task, self.config, prefer_local_generated=False, prefer_local_video=False)
            self._save_emit(task)

            if not task.image_prompt.strip() or (self.execution_mode != "image_only" and not task.video_prompt.strip()):
                self._finish_task(task, TaskStatus.SKIPPED_EMPTY_PROMPT, "图片提示词或视频提示词为空", start)
                return None

            if not self._ensure_generated_image(task, product_image or "", start):
                return None
            return task
        except Exception as exc:
            self._set_status(task, TaskStatus.FAILED_UNKNOWN, str(exc))
            self._finish_failed(task, start)
            return None

    def _run_video_submit_stage(self, task: TaskItem) -> None:
        while not self._stop_event.is_set():
            if not self._reset_failed_task_for_queue(task, "video_submit"):
                return
            self._run_video_submit_stage_once(task)
            if not self._retry_or_keep_failed(task, "video_submit"):
                return

    def _run_video_submit_stage_once(self, task: TaskItem) -> None:
        if self._stop_event.is_set() or TaskStatus.is_done(task.status):
            return
        self._wait_if_paused()
        if self._stop_event.is_set():
            return

        start = time.time()
        task.started_at = task.started_at or now_text()
        task.ended_at = None
        try:
            if task.video_task_id and not task.video_url:
                self._set_status(task, TaskStatus.VIDEO_SUBMITTED)
                self._poll_now_event.set()
                return

            task.video_provider = task.video_provider or self.config.video_provider
            task.video_model_logical_key = task.video_model_logical_key or self.config.video_model_logical_key
            video_provider = get_video_provider(task.video_provider)
            task.video_model_display = model_display_name(video_provider, task.video_model_logical_key)
            self._set_status(task, TaskStatus.VIDEO_SUBMITTING)
            self._log(
                "INFO",
                f"[SUBMIT] PID={task.pid} row={task.row_index} submit video task, provider={video_provider.provider_name}, model={task.video_model_display}",
                task,
            )
            image_source = self._preferred_video_image_source(task, video_provider)
            if bool(getattr(video_provider, "requires_remote_image_url", False)) and not image_source:
                self._set_status(task, TaskStatus.FAILED_VIDEO_API, task.error_message or "Remote-image provider requires a public image URL")
                self._finish_failed(task, start)
                return
            submit_result = video_provider.submit_video_task(
                image_source=image_source,
                prompt=task.video_prompt,
                model_logical_key=task.video_model_logical_key,
                api_key=self.config.video_api_key,
                extra_params={
                    "base_url": self.config.video_api_base_url,
                    "orientation": self.config.video_orientation,
                    "resolution": self.config.video_resolution,
                    "retry_count": self.config.retry_count,
                    "retry_interval_seconds": self.config.retry_interval_seconds,
                    "timeout": self.config.request_timeout_seconds,
                },
            )
            task.video_raw_response = submit_result.raw_response
            if not submit_result.success:
                self._set_status(task, TaskStatus.FAILED_VIDEO_API, submit_result.error_message or "视频任务提交失败")
                self._finish_failed(task, start)
                return
            if submit_result.video_url and not submit_result.task_id:
                task.video_url = submit_result.video_url
                self._finish_task(task, TaskStatus.VIDEO_DOWNLOAD_PENDING, None, start)
                self._refresh_image_assets_metadata(task)
                self._queue_completed_video_download(task)
                return
            if not submit_result.task_id:
                self._set_status(task, TaskStatus.FAILED_VIDEO_API, "未获取到 video_task_id")
                self._finish_failed(task, start)
                return

            task.video_task_id = submit_result.task_id
            task.video_submit_time = now_text()
            task.video_poll_count = 0
            task.video_poll_start_time = None
            task.video_poll_end_time = None
            self._set_status(task, TaskStatus.VIDEO_SUBMITTED, force=True)
            self._refresh_image_assets_metadata(task)
            self._log("INFO", f"[SUBMIT] PID={task.pid} row={task.row_index} video task id: {task.video_task_id}", task)
            self._poll_now_event.set()
        except Exception as exc:
            self._set_status(task, TaskStatus.FAILED_UNKNOWN, str(exc))
            self._finish_failed(task, start)

    def _wait_if_paused(self) -> None:
        while not self._pause_event.is_set() and not self._stop_event.is_set():
            time.sleep(0.2)

    def _set_status(self, task: TaskItem, status: str, error: str | None = None, force: bool = False) -> None:
        task.set_status(status, error)
        if status in {
            TaskStatus.CHECKING_PRODUCT_IMAGE,
            TaskStatus.GENERATING_IMAGE,
            TaskStatus.IMAGE_DONE,
            TaskStatus.FAILED_IMAGE_API,
        }:
            task.image_status = status
        if status in {
            TaskStatus.VIDEO_SUBMITTING,
            TaskStatus.VIDEO_SUBMITTED,
            TaskStatus.VIDEO_POLLING,
            TaskStatus.VIDEO_DOWNLOAD_PENDING,
            TaskStatus.VIDEO_DOWNLOADING,
            TaskStatus.COMPLETED,
            TaskStatus.VIDEO_DOWNLOADED,
            TaskStatus.VIDEO_FAILED,
            TaskStatus.VIDEO_TIMEOUT,
            TaskStatus.FAILED_VIDEO_API,
        }:
            task.video_status = status
        terminal = status in {
            TaskStatus.VIDEO_DOWNLOADED,
            TaskStatus.VIDEO_TIMEOUT,
            TaskStatus.FAILED_IMAGE_API,
            TaskStatus.FAILED_VIDEO_API,
            TaskStatus.FAILED_UNKNOWN,
            TaskStatus.SKIPPED_NO_PRODUCT_IMAGE_FOLDER,
            TaskStatus.SKIPPED_NO_PRODUCT_IMAGE,
            TaskStatus.SKIPPED_NO_PRODUCT_IMAGE_URL,
            TaskStatus.SKIPPED_EMPTY_PROMPT,
        }
        self._save_emit(task, force=force or terminal)

    def _refresh_image_assets_metadata(self, task: TaskItem) -> None:
        if not self.config.auto_save_image_assets:
            return
        try:
            save_image_task_assets(task, self.config.image_assets_root)
        except Exception as exc:
            self._log("ERROR", f"[ASSETS] PID={task.pid} row={task.row_index} update image metadata failed: {exc}", task)

    def _save_emit(self, task: TaskItem, force: bool = False) -> None:
        # GUI-thread protection: throttle per-task emits so the Qt event queue
        # never backs up on tight inner loops (video polling, node mutations,
        # download progress). Forced emits (terminal transitions, video
        # task_id captured, download done, hard failure) always go through so
        # the user never misses meaningful updates.
        key = str(getattr(task, "task_uid", "") or f"{task.pid}::row_{task.row_index}")
        now = time.monotonic()
        if not force:
            with self._emit_throttle_lock:
                last = self._last_emit_monotonic_per_task.get(key, 0.0)
                if now - last < self._emit_min_interval_seconds:
                    # Mark state dirty so the background poll/save loop still
                    # persists this change on its next scheduled tick.
                    with self._state_lock:
                        self._state_dirty = True
                    return
                self._last_emit_monotonic_per_task[key] = now

        stats: dict | None = None
        state_save_error: Exception | None = None
        state_save_elapsed_ms: int | None = None
        with self._state_lock:
            self._state_dirty = True
            if force or (now - self._last_state_save_time >= self._state_save_interval_seconds):
                try:
                    save_started = time.perf_counter()
                    saved = self.manager.save_state(force_backup=force)
                    state_save_elapsed_ms = int((time.perf_counter() - save_started) * 1000)
                    if saved:
                        self._last_state_save_time = now
                        self._state_dirty = False
                    else:
                        state_save_error = RuntimeError(self.manager.last_save_error or "task_state save failed")
                        self._state_dirty = True
                except Exception as exc:
                    # A flaky network share must not prevent live UI updates.
                    # Keep state dirty so a later flush can retry persistence.
                    state_save_error = exc
                    self._state_dirty = True
            # Cache stats() — computing it scans every task and used to fire on
            # every save_emit, which is the worst offender for GUI lag.
            if force or self._stats_cache is None or now - self._stats_cache_monotonic >= self._stats_cache_ttl_seconds:
                self._stats_cache = self.manager.stats()
                self._stats_cache_monotonic = now
            stats = self._stats_cache
        self.task_updated.emit(task)
        if stats is not None:
            self.stats_updated.emit(stats)
        if state_save_error is not None:
            self.logger.warning("task_state save deferred; live UI update was still emitted: %s", state_save_error)
            self.diagnostics.event("STATE_SAVE_DEFERRED", error=str(state_save_error), task_uid=key)
        if state_save_elapsed_ms is not None:
            self.diagnostics.event("STATE_SAVE", duration_ms=state_save_elapsed_ms, force=force, task_uid=key)
            if state_save_elapsed_ms > 1000:
                self.diagnostics.event("PERF_WARNING", metric="state_save_ms", value=state_save_elapsed_ms, task_uid=key)

    def _flush_state(self) -> None:
        state_save_error: Exception | None = None
        state_save_elapsed_ms: int | None = None
        with self._state_lock:
            if self._state_dirty:
                try:
                    save_started = time.perf_counter()
                    saved = self.manager.save_state(force_backup=True)
                    state_save_elapsed_ms = int((time.perf_counter() - save_started) * 1000)
                    if saved:
                        self._last_state_save_time = time.monotonic()
                        self._state_dirty = False
                    else:
                        state_save_error = RuntimeError(self.manager.last_save_error or "task_state save failed")
                        self._state_dirty = True
                except Exception as exc:
                    state_save_error = exc
                    self._state_dirty = True
            # Always recompute fresh stats at flush time — this only runs at
            # run end or major boundaries, so it's fine to bypass the cache.
            self._stats_cache = self.manager.stats()
            self._stats_cache_monotonic = time.monotonic()
            stats = self._stats_cache
        self.stats_updated.emit(stats)
        if state_save_error is not None:
            self.logger.warning("task_state final flush deferred after network write failure: %s", state_save_error)
            self.diagnostics.event("STATE_FLUSH_DEFERRED", error=str(state_save_error))
        if state_save_elapsed_ms is not None:
            self.diagnostics.event("STATE_FLUSH", duration_ms=state_save_elapsed_ms)
            if state_save_elapsed_ms > 1000:
                self.diagnostics.event("PERF_WARNING", metric="state_flush_ms", value=state_save_elapsed_ms)

    def _log(self, level: str, message: str, task: TaskItem | None = None) -> None:
        if task is not None:
            append_task_log(task, level, None, message)
        getattr(self.logger, level.lower(), self.logger.info)(message)
        self.log_message.emit(level, message, task)

    # ====================================================================== #
    # V2 workflow engine entry point + drivers
    # ====================================================================== #
    def _make_workflow_executor(self) -> WorkflowExecutor:
        return WorkflowExecutor(
            self.config,
            self.workflow_definition,
            log_callback=self._log,
            save_emit_callback=self._save_emit,
            regenerate_existing_images=self.regenerate_existing_images,
            stop_event=self._stop_event,
            pause_event=self._pause_event,
        )

    def _run_workflow_engine_run(self) -> None:
        """v2 entry point. Mirrors the legacy run() but drives execution
        through a WorkflowExecutor and node-aware poll/download loops.
        """
        # Auto-migrate any pre-v2 state so old batches resume cleanly.
        try:
            migrated = auto_migrate_tasks_to_workflow(self.manager.tasks, self.workflow_definition)
            if migrated:
                self._log("INFO", f"[V2] 自动迁移 {migrated} 条历史任务的状态到节点式工作流")
        except Exception as exc:
            self._log("ERROR", f"[V2] 历史任务迁移失败：{exc}")

        self._workflow_executor = self._make_workflow_executor()
        self._repair_workflow_nodes_for_all_tasks()

        if self.poll_only:
            self._log("INFO", "[V2] Start poll-only mode")
            self._download_executor = self._create_download_executor()
            self._poll_cycle_workflow_images()
            self._poll_cycle_workflow()
            if self._download_executor:
                self._download_executor.shutdown(wait=True)
                self._download_executor = None
            self._flush_state()
            self.finished_message.emit("Poll-only finished")
            return

        self._download_executor = self._create_download_executor()
        image_workers = max(1, min(int(self.config.image_concurrency), 100))
        video_workers = max(1, min(int(getattr(self.config, "video_submit_concurrency", image_workers) or image_workers), 100))
        drive_workers = max(1, min(image_workers + video_workers, 150))
        self._workflow_drive_executor = ThreadPoolExecutor(max_workers=drive_workers)
        self._poll_thread = threading.Thread(target=self._poll_loop_workflow, daemon=True)
        self._poll_thread.start()

        targets = self._select_workflow_targets()
        if self.failed_only and targets:
            self._reset_failed_workflow_targets(self._workflow_executor, targets)
        if targets:
            self._log(
                "INFO",
                f"[V2] Start node-based pipeline: {len(targets)} tasks, "
                f"image_concurrency={image_workers}, video_submit_concurrency={video_workers}, "
                f"drive_workers={drive_workers}, workflow={self.workflow_definition.get('workflow_version')}",
            )
            self.diagnostics.event(
                "WORKFLOW_START",
                target_count=len(targets),
                image_workers=image_workers,
                video_submit_workers=video_workers,
                drive_workers=drive_workers,
                workflow=self.workflow_definition.get("workflow_version"),
            )
            self._run_workflow_pipeline(targets, image_workers)
        else:
            self._workflow_runtime_target_keys = set()
            self._log("INFO", "[V2] No new tasks to submit; polling existing video nodes only")

        self._queue_pending_workflow_downloads()
        idle_no_active_cycles = 0
        while not self._stop_event.is_set():
            if self._handle_retry_failed_request_if_any():
                idle_no_active_cycles = 0
            self._emit_workflow_perf_snapshot()
            if not self._workflow_has_active_work():
                recovered = self._recover_idle_workflow_work()
                if recovered:
                    idle_no_active_cycles = 0
                    continue
                unfinished = self._workflow_unfinished_runtime_count()
                if unfinished:
                    idle_no_active_cycles += 1
                    self.diagnostics.event(
                        "NO_ACTIVE_WORK_WITH_UNFINISHED",
                        unfinished=unfinished,
                        idle_cycles=idle_no_active_cycles,
                        node_status=self._workflow_node_status_summary(self._workflow_executor, self._workflow_runtime_tasks()) if self._workflow_executor else "",
                    )
                    if idle_no_active_cycles < 5:
                        time.sleep(0.5)
                        continue
                    self._mark_stop(
                        "NO_ACTIVE_WORK_WITH_UNFINISHED",
                        f"scheduler found no active work but {unfinished} runtime task(s) remain unfinished",
                        set_event=False,
                    )
                else:
                    self._mark_stop("NO_ACTIVE_WORK", "workflow queues drained", set_event=False)
                break
            self._schedule_actionable_workflow_tasks()
            self._queue_pending_workflow_downloads()
            time.sleep(0.2)
        if not self._stop_event.is_set():
            self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=3)
        if self._workflow_drive_executor:
            self._workflow_drive_executor.shutdown(wait=True)
            self._workflow_drive_executor = None
        if self._download_executor:
            self._download_executor.shutdown(wait=True)
            self._download_executor = None
        self._emit_workflow_perf_snapshot(force=True)
        self._flush_state()
        self.finished_message.emit("Task submit and video polling finished (v2 workflow)")

    def _repair_workflow_nodes_for_all_tasks(self) -> None:
        executor = self._workflow_executor
        if executor is None:
            return
        repaired = 0
        for task in self.manager.tasks:
            try:
                repaired += int(executor.repair_task_without_source_lookup(task) or 0)
            except Exception as exc:
                self._log("ERROR", f"[V2] PID={task.pid} row={task.row_index} workflow repair failed: {exc}", task)
        if repaired:
            self._log("INFO", f"[V2] 已修正 {repaired} 个旧流程/中断遗留节点，后续会重新入队或继续轮询")
            self._flush_state()

    def _reset_failed_workflow_targets(self, executor: WorkflowExecutor | None, targets: list[TaskItem], *, force: bool = False) -> int:
        """Manual retry for v2 workflow tasks.

        The v2 scheduler intentionally skips FAILED nodes during normal runs.
        When the user clicks "重试失败", reset those failed nodes first so the
        dependency scheduler can pick them up again in the same worker run.
        """
        if executor is None or (not self.failed_only and not force):
            return 0
        reset_count = 0
        for task in targets:
            ensure_task_node_states(task, self.workflow_definition)
            task.auto_retry_count = 0
            task.error_message = None
            task.ended_at = None
            task.elapsed_seconds = None
            task.last_auto_retry_time = now_text()
            task.last_auto_retry_stage = "workflow_failed_nodes"
            task.last_auto_retry_reason = "manual retry failed"
            reset_task = False
            for node in executor.nodes():
                node_id = str(node.get("node_id") or "")
                state = task.node_states.get(node_id) or {}
                status = str(state.get("status") or NODE_STATUS_PENDING)
                if (
                    str(node.get("node_type") or "") == NODE_TYPE_IMAGE
                    and status != NODE_STATUS_COMPLETED
                    and executor.complete_image_node_from_existing_output(task, node_id, state)
                ):
                    reset_task = True
                    self._log(
                        "INFO",
                        f"[RETRY] PID={task.pid} row={task.row_index} node={node_id} 已检测到已有图片输出，保留图片并跳过重新生图",
                        task,
                    )
                    continue
                if status == NODE_STATUS_FAILED:
                    reason = state.get("error_message") or task.error_message or task.status
                    outcome = executor.manual_reset_node(task, node_id)
                    if outcome.success:
                        reset_count += 1
                        reset_task = True
                        self._log(
                            "INFO",
                            f"[RETRY] PID={task.pid} row={task.row_index} node={node_id} 已重置为待执行，准备重新入队 reason={reason}",
                            task,
                        )
                    continue
                if status in {NODE_STATUS_BLOCKED, NODE_STATUS_WAITING_INPUT}:
                    state["status"] = NODE_STATUS_PENDING
                    state["error_message"] = None
                    reset_task = True
            if reset_task:
                executor.refresh_runnable_statuses(task)
                task.set_status(executor.aggregate_task_status(task))
                self._save_emit(task, force=True)
        if reset_count:
            self._log("INFO", f"[V2][RETRY] 已重置 {reset_count} 个失败节点，开始重新执行")
            self._flush_state()
        return reset_count

    def retry_failed_workflow_targets_now(self, selected_task_keys: set[str] | None = None) -> int:
        executor = self._workflow_executor
        if executor is None:
            return 0
        selected = {str(key) for key in (selected_task_keys or set()) if str(key)}
        targets = [
            task
            for task in self.manager.tasks
            if not selected or self._task_runtime_key(task) in selected
        ]
        if self._workflow_runtime_target_keys is not None:
            self._workflow_runtime_target_keys.update(self._task_runtime_key(task) for task in targets)
        reset_count = self._reset_failed_workflow_targets(executor, targets, force=True)
        if self._workflow_runtime_target_keys is not None:
            self._workflow_runtime_target_keys.update(self._task_runtime_key(task) for task in targets)
        self._workflow_force_full_mode_task_keys.update(self._task_runtime_key(task) for task in targets)
        scheduled = self._schedule_actionable_workflow_tasks(targets)
        self._queue_pending_workflow_downloads()
        self._poll_now_event.set()
        if reset_count or scheduled:
            self.diagnostics.event("RUNTIME_RETRY_FAILED", reset_count=reset_count, scheduled=scheduled, selected_count=len(selected))
            self._log("INFO", f"[V2][RETRY] 运行中重试请求已处理：重置 {reset_count} 个失败节点，调度 {scheduled} 个任务")
        else:
            self._log("INFO", "[V2][RETRY] 运行中重试请求已处理：没有可重试的失败节点")
        return reset_count

    def _handle_retry_failed_request_if_any(self) -> int:
        requested = self._consume_retry_failed_request()
        if requested is False:
            return 0
        selected = requested if isinstance(requested, set) else None
        return self.retry_failed_workflow_targets_now(selected)

    def _auto_retry_failed_workflow_targets(self, executor: WorkflowExecutor | None, targets: list[TaskItem]) -> int:
        if executor is None:
            return 0
        auto_retry_enabled = bool(getattr(self.config, "auto_retry_failed_workflow_enabled", False))
        default_video_submit_retry = any(executor.has_default_video_submit_retry_node(task) for task in targets)
        if not auto_retry_enabled and not default_video_submit_retry:
            return 0
        reset_count = 0
        for task in targets:
            try:
                reset_count += executor.reset_failed_nodes_for_auto_retry(task)
            except Exception as exc:
                self._log("ERROR", f"[AUTO_RETRY] PID={task.pid} row={task.row_index} reset failed: {exc}", task)
        if reset_count:
            self._log("INFO", f"[V2][AUTO_RETRY] 已自动重置 {reset_count} 个失败节点，继续重新入队执行")
        return reset_count

    def _run_workflow_pipeline(self, targets: list[TaskItem], image_workers: int) -> None:
        """Prime the node pipeline and let the scheduler keep it moving.

        The old implementation drove the DAG in bounded full-batch passes. On
        large batches that made every phase wait for pass boundaries. Here we
        prepare all target tasks once, schedule all currently actionable nodes,
        then the background scheduler / poll callbacks wake dependent nodes as
        soon as their inputs are ready.
        """
        executor = self._workflow_executor
        if executor is None:
            return
        self._workflow_runtime_target_keys = {self._task_runtime_key(task) for task in targets}
        # Prepare tasks concurrently and schedule each task as soon as its
        # source image / legacy repair is ready. This removes the old batch-wide
        # barrier where 1,000+ product-image lookups had to finish before the
        # first API submission could start.
        prepared_count = 0
        actionable_count = 0
        scheduled = 0
        with ThreadPoolExecutor(max_workers=max(1, min(image_workers, len(targets)))) as prep_pool:
            futures = {prep_pool.submit(self._safe_prepare, executor, task): task for task in targets}
            for future in as_completed(futures):
                if self._stop_event.is_set():
                    break
                task = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    self._log("ERROR", f"[V2] PID={task.pid} row={task.row_index} prepare thread failed: {exc}", task)
                    continue
                prepared_count += 1
                self._auto_retry_failed_workflow_targets(executor, [task])
                if executor.has_actionable_nodes(task):
                    actionable_count += 1
                    if self._schedule_workflow_drive(executor, task):
                        scheduled += 1
                for node in executor.find_downloadable_nodes(task):
                    self._queue_workflow_node_download(task, node)

        self._log(
            "INFO",
            f"[V2] Runnable workflow tasks: {actionable_count}/{prepared_count}/{len(targets)} prepared; "
            f"node_status={self._workflow_node_status_summary(executor, targets)}",
        )
        if scheduled:
            self._log("INFO", f"[V2] Scheduled {scheduled} task driver(s) for node pipeline")
        elif not actionable_count:
            self._log(
                "INFO",
                f"[V2] No actionable workflow nodes; "
                f"node_status={self._workflow_node_status_summary(executor, targets)}",
            )

    @staticmethod
    def _task_runtime_key(task: TaskItem) -> str:
        return str(getattr(task, "task_uid", "") or f"{task.pid}::row_{task.row_index}")

    def _workflow_runtime_tasks(self) -> list[TaskItem]:
        if self._workflow_runtime_target_keys is None:
            return list(self.manager.tasks)
        return [
            task
            for task in self.manager.tasks
            if self._task_runtime_key(task) in self._workflow_runtime_target_keys
        ]

    def _schedule_actionable_workflow_tasks(self, tasks: list[TaskItem] | None = None) -> int:
        executor = self._workflow_executor
        if executor is None:
            return 0
        scheduled = 0
        for task in (tasks if tasks is not None else self._workflow_runtime_tasks()):
            if self._stop_event.is_set():
                break
            if executor.has_actionable_nodes(task):
                if self._schedule_workflow_drive(executor, task):
                    scheduled += 1
        return scheduled

    def _schedule_workflow_drive(self, executor: WorkflowExecutor, task: TaskItem) -> bool:
        key = self._task_runtime_key(task)
        with self._workflow_drive_lock:
            if key in self._workflow_drive_task_keys_in_progress:
                return False
            self._workflow_drive_task_keys_in_progress.add(key)

        def _runner() -> None:
            try:
                self._drive_task_once(executor, task)
            except Exception as exc:
                tb = traceback.format_exc()
                self.diagnostics.event("WORKER_CRASH", worker_name="workflow_driver", task_uid=key, error=str(exc), traceback=tb)
                self._log("ERROR", f"[V2] task driver failed: {exc}\n{tb}")
            finally:
                with self._workflow_drive_lock:
                    self._workflow_drive_task_keys_in_progress.discard(key)
                self._poll_now_event.set()

        if self._workflow_drive_executor is None:
            try:
                _runner()
            finally:
                return True
        try:
            self._workflow_drive_executor.submit(_runner)
        except RuntimeError:
            with self._workflow_drive_lock:
                self._workflow_drive_task_keys_in_progress.discard(key)
            return False
        return True

    def _safe_prepare(self, executor: WorkflowExecutor, task: TaskItem) -> None:
        try:
            executor.prepare_task(task)
        except Exception as exc:
            self._log("ERROR", f"[V2] PID={task.pid} row={task.row_index} prepare failed: {exc}")

    def _workflow_node_status_summary(self, executor: WorkflowExecutor, targets: list[TaskItem]) -> str:
        counts: dict[str, int] = {}
        for task in targets:
            for node in executor.nodes():
                state = (task.node_states or {}).get(str(node.get("node_id") or "")) or {}
                status = str(state.get("status") or "UNKNOWN")
                counts[status] = counts.get(status, 0) + 1
        if not counts:
            return "none"
        return ", ".join(f"{key}={counts[key]}" for key in sorted(counts))

    def _drive_task_once(self, executor: WorkflowExecutor, task: TaskItem) -> None:
        """Process all currently-ready image + video nodes for one task.

        Image nodes run synchronously inside this thread. Video nodes submit
        and return immediately (the poll loop takes over).
        Downloads for already-completed videos are queued onto the download
        executor so they don't block the driver.
        """
        if self._stop_event.is_set():
            return
        task_key = self._task_runtime_key(task)
        execution_mode = "full" if task_key in self._workflow_force_full_mode_task_keys else self.execution_mode
        try:
            self._wait_if_paused()
            if self._stop_event.is_set():
                return
            # Per-task safety: prevent two drivers running concurrently on the
            # same task (e.g. driver thread + manual retry click).
            with self._workflow_task_lock(task):
                # Walk the DAG: run all currently-ready image nodes, then submit
                # all currently-ready video nodes. Loop until no more ready
                # nodes appear (which means new ones depend on async work).
                while not self._stop_event.is_set():
                    self._wait_if_paused()
                    executor.refresh_runnable_statuses(task)
                    progressed = False
                    if execution_mode in {"full", "image_only"}:
                        for node in executor.find_ready_image_nodes(task):
                            if self._stop_event.is_set():
                                return
                            outcome = executor.run_image_node(task, node)
                            progressed = progressed or outcome.success or outcome.submitted or outcome.blocked
                            if outcome.submitted:
                                self._poll_now_event.set()
                    if execution_mode in {"full", "video_only"}:
                        for node in executor.find_ready_video_nodes(task):
                            if self._stop_event.is_set():
                                return
                            outcome = executor.submit_video_node(task, node)
                            progressed = progressed or outcome.success or outcome.submitted or outcome.blocked
                            if outcome.submitted:
                                self._poll_now_event.set()
                    # Queue downloads for any already-completed videos.
                    for node in executor.find_downloadable_nodes(task):
                        self._queue_workflow_node_download(task, node)
                    if not progressed:
                        break
        except Exception as exc:
            tb = traceback.format_exc()
            self.diagnostics.event("WORKER_CRASH", worker_name="drive_task_once", task_uid=self._task_runtime_key(task), error=str(exc), traceback=tb)
            self._log("ERROR", f"[V2] PID={task.pid} row={task.row_index} driver crashed: {exc}\n{tb}")

    def _workflow_task_lock(self, task: TaskItem):
        """Reuse the executor's internal per-task lock so manual operations
        and the driver thread don't stomp on each other.
        """
        executor = self._workflow_executor
        if executor is None:
            return _NullContext()
        lock = executor._node_lock(task)  # private but intentional sibling
        return lock

    def _poll_loop_workflow(self) -> None:
        while not self._stop_event.is_set():
            self._wait_if_paused()
            if self._stop_event.is_set():
                break
            self._poll_cycle_workflow_images()
            self._poll_cycle_workflow()
            interval = max(1, int(self.config.poll_interval_seconds))
            self._wait_for_next_poll_interval(interval)

    def _poll_cycle_workflow(self) -> None:
        executor = self._workflow_executor
        if executor is None:
            return
        self._queue_pending_workflow_downloads()
        targets: list[tuple[TaskItem, dict]] = []
        for task in self.manager.tasks:
            for node in executor.find_polling_nodes(task):
                targets.append((task, node))
        if not targets:
            return
        self._log("INFO", f"[V2] Polling {len(targets)} video node(s)")
        max_workers = max(1, min(len(targets), int(self.config.poll_concurrency), 100))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(self._poll_one_workflow_node, task, node)
                for task, node in targets
            ]
            for future in as_completed(futures):
                if self._stop_event.is_set():
                    break
                try:
                    future.result()
                except Exception as exc:
                    tb = traceback.format_exc()
                    self.diagnostics.event("WORKER_CRASH", worker_name="video_poll_thread", error=str(exc), traceback=tb)
                    self._log("ERROR", f"[V2] poll node thread failed: {exc}\n{tb}")

    def _poll_cycle_workflow_images(self) -> None:
        executor = self._workflow_executor
        if executor is None:
            return
        targets: list[tuple[TaskItem, dict]] = []
        for task in self.manager.tasks:
            for node in executor.find_polling_image_nodes(task):
                targets.append((task, node))
        if not targets:
            return
        self._log("INFO", f"[V2] Polling {len(targets)} image node(s)")
        max_workers = max(1, min(len(targets), int(self.config.poll_concurrency), 100))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(self._poll_one_workflow_image_node, task, node)
                for task, node in targets
            ]
            for future in as_completed(futures):
                if self._stop_event.is_set():
                    break
                try:
                    future.result()
                except Exception as exc:
                    tb = traceback.format_exc()
                    self.diagnostics.event("WORKER_CRASH", worker_name="image_poll_thread", error=str(exc), traceback=tb)
                    self._log("ERROR", f"[V2] poll image node thread failed: {exc}\n{tb}")

    def _poll_one_workflow_image_node(self, task: TaskItem, node: dict) -> None:
        executor = self._workflow_executor
        if executor is None:
            return
        state = task.node_states.get(node["node_id"]) or {}
        task_id = str(state.get("task_id") or "").strip()
        if not self._acquire_poll_lock(task_id):
            return
        try:
            with self._workflow_task_lock(task):
                outcome = executor.run_image_node(task, node)
            if outcome.completed:
                self._poll_now_event.set()
                if not self.poll_only:
                    self._schedule_workflow_drive(executor, task)
            elif outcome.status == NODE_STATUS_FAILED:
                if self._auto_retry_failed_workflow_targets(executor, [task]):
                    self._schedule_workflow_drive(executor, task)
        finally:
            self._release_poll_lock(task_id)

    def _poll_one_workflow_node(self, task: TaskItem, node: dict) -> None:
        executor = self._workflow_executor
        if executor is None:
            return
        state = task.node_states.get(node["node_id"]) or {}
        task_id = str(state.get("task_id") or "").strip()
        if not self._acquire_poll_lock(task_id):
            return
        try:
            outcome = executor.poll_video_node(task, node)
            if outcome.completed:
                # Queue download for this node specifically.
                self._queue_workflow_node_download(task, node)
                # Re-evaluate the driver because a downstream node may now be READY.
                self._poll_now_event.set()
                if not self.poll_only:
                    self._schedule_workflow_drive(executor, task)
            elif outcome.status == NODE_STATUS_FAILED:
                if self._auto_retry_failed_workflow_targets(executor, [task]):
                    self._schedule_workflow_drive(executor, task)
        finally:
            self._release_poll_lock(task_id)

    def _wait_for_polling_progress(self, executor: WorkflowExecutor, targets: list[TaskItem]) -> None:
        """Sleep briefly to let the background poll loop advance async nodes."""
        deadline = time.monotonic() + max(1, int(self.config.poll_interval_seconds))
        snapshot = {
            (t.task_uid, n["node_id"]): (t.node_states.get(n["node_id"]) or {}).get("status")
            for t in targets
            for n in (executor.find_polling_image_nodes(t) + executor.find_polling_nodes(t))
        }
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return
            time.sleep(0.5)
            for t in targets:
                for n in (executor.find_polling_image_nodes(t) + executor.find_polling_nodes(t)):
                    cur = (t.node_states.get(n["node_id"]) or {}).get("status")
                    if snapshot.get((t.task_uid, n["node_id"])) != cur:
                        return
            # Also exit if download-able or new-ready nodes appeared.
            if any(executor.find_downloadable_nodes(t) or executor.find_ready_image_nodes(t) or executor.find_ready_video_nodes(t) for t in targets):
                return

    def _queue_workflow_node_download(self, task: TaskItem, node: dict) -> None:
        executor = self._workflow_executor
        if executor is None or not self.config.auto_download_video:
            return
        state = task.node_states.get(node["node_id"]) or {}
        if not state.get("output_video_url") or state.get("output_video_local_path"):
            return
        key = f"{task.task_uid}::{node['node_id']}"
        with self._download_task_ids_lock:
            if key in self._download_task_ids_in_progress:
                return
            self._download_task_ids_in_progress.add(key)
        if state.get("download_status") not in {"QUEUED", "DOWNLOADING"}:
            state["download_status"] = "QUEUED"
            executor._mirror_video_node_to_legacy_fields(task, str(node.get("node_id") or ""), state)
            executor._save_emit(task, force=False)
        if not self._download_executor:
            try:
                executor.download_node(task, node)
            finally:
                with self._download_task_ids_lock:
                    self._download_task_ids_in_progress.discard(key)
            return
        def _runner() -> None:
            try:
                executor.download_node(task, node)
            finally:
                with self._download_task_ids_lock:
                    self._download_task_ids_in_progress.discard(key)
        self._download_executor.submit(_runner)

    def _queue_pending_workflow_downloads(self) -> None:
        executor = self._workflow_executor
        if executor is None or not self.config.auto_download_video:
            return
        for task in self.manager.tasks:
            for node in executor.find_downloadable_nodes(task):
                self._queue_workflow_node_download(task, node)

    def _workflow_has_polling_nodes(self) -> bool:
        executor = self._workflow_executor
        if executor is None:
            return False
        return any(executor.find_polling_image_nodes(t) or executor.find_polling_nodes(t) for t in self.manager.tasks)

    def _workflow_has_pending_downloads(self) -> bool:
        executor = self._workflow_executor
        if executor is None or not self.config.auto_download_video:
            return False
        return any(executor.find_downloadable_nodes(t) for t in self.manager.tasks)

    def _workflow_has_driving_tasks(self) -> bool:
        with self._workflow_drive_lock:
            return bool(self._workflow_drive_task_keys_in_progress)

    def _workflow_has_actionable_nodes(self) -> bool:
        executor = self._workflow_executor
        if executor is None:
            return False
        return any(executor.has_actionable_nodes(task) for task in self._workflow_runtime_tasks())

    def _workflow_unfinished_runtime_count(self) -> int:
        executor = self._workflow_executor
        if executor is None:
            return 0
        unfinished = 0
        terminal_task_statuses = {
            TaskStatus.VIDEO_DOWNLOADED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED_IMAGE_API,
            TaskStatus.FAILED_VIDEO_API,
            TaskStatus.FAILED_UNKNOWN,
            TaskStatus.VIDEO_TIMEOUT,
            TaskStatus.SKIPPED_NO_PRODUCT_IMAGE_FOLDER,
            TaskStatus.SKIPPED_NO_PRODUCT_IMAGE,
            TaskStatus.SKIPPED_NO_PRODUCT_IMAGE_URL,
            TaskStatus.SKIPPED_EMPTY_PROMPT,
        }
        for task in self._workflow_runtime_tasks():
            states = task.node_states or {}
            if any(
                str(state.get("status") or "") in {
                    NODE_STATUS_PENDING,
                    NODE_STATUS_READY,
                    NODE_STATUS_RUNNING,
                    NODE_STATUS_SUBMITTED,
                    NODE_STATUS_POLLING,
                    NODE_STATUS_WAITING_INPUT,
                    NODE_STATUS_BLOCKED,
                }
                for state in states.values()
                if isinstance(state, dict)
            ):
                unfinished += 1
                continue
            if task.status not in terminal_task_statuses:
                unfinished += 1
        return unfinished

    def _recover_idle_workflow_work(self) -> bool:
        executor = self._workflow_executor
        if executor is None:
            return False
        tasks = self._workflow_runtime_tasks()
        reset_count = self._auto_retry_failed_workflow_targets(executor, tasks)
        for task in tasks:
            try:
                executor.refresh_runnable_statuses(task)
            except Exception as exc:
                self._log("ERROR", f"[V2] PID={task.pid} row={task.row_index} idle refresh failed: {exc}", task)
        scheduled = self._schedule_actionable_workflow_tasks(tasks)
        self._queue_pending_workflow_downloads()
        if reset_count or scheduled or self._workflow_has_active_work():
            self.diagnostics.event("IDLE_RECOVERED", reset_count=reset_count, scheduled=scheduled)
            return True
        return False

    def _emit_workflow_perf_snapshot(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_perf_snapshot_monotonic < 5:
            return
        self._last_perf_snapshot_monotonic = now
        executor = self._workflow_executor
        runtime_tasks = self._workflow_runtime_tasks()
        node_counts: dict[str, int] = {}
        for task in runtime_tasks:
            for state in (task.node_states or {}).values():
                if isinstance(state, dict):
                    status = str(state.get("status") or "UNKNOWN")
                    node_counts[status] = node_counts.get(status, 0) + 1
        snapshot = {
            "tasks": len(runtime_tasks),
            "drivers": len(self._workflow_drive_task_keys_in_progress),
            "polling_locks": len(self.polling_task_ids_in_progress),
            "download_locks": len(self._download_task_ids_in_progress),
            "actionable": int(self._workflow_has_actionable_nodes()) if executor else 0,
            "polling": int(self._workflow_has_polling_nodes()) if executor else 0,
            "pending_downloads": int(self._workflow_has_pending_downloads()) if executor else 0,
            "unfinished": self._workflow_unfinished_runtime_count() if executor else 0,
            "node_counts": node_counts,
        }
        self.diagnostics.event("PERF", **snapshot)
        self._log(
            "INFO",
            "[PERF] "
            f"tasks={snapshot['tasks']} drivers={snapshot['drivers']} "
            f"polling={snapshot['polling_locks']} downloading={snapshot['download_locks']} "
            f"actionable={snapshot['actionable']} pending_downloads={snapshot['pending_downloads']} "
            f"unfinished={snapshot['unfinished']} node_status={node_counts}",
        )

    def _workflow_has_active_work(self) -> bool:
        return bool(
            self._workflow_has_driving_tasks()
            or self._workflow_has_actionable_nodes()
            or self._workflow_has_polling_nodes()
            or self._workflow_has_pending_downloads()
        )


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


class ManualPollWorker(QThread):
    task_updated = Signal(object)
    log_message = Signal(str, str, object)
    stats_updated = Signal(dict)
    finished_summary = Signal(object)

    def __init__(
        self,
        manager: TaskManager,
        config: AppConfig,
        logger: logging.Logger,
        batch_id: str,
        polling_task_ids_in_progress: set[str] | None = None,
        polling_task_ids_lock: threading.Lock | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.manager = manager
        self.config = config
        self.logger = logger
        self.batch_id = batch_id
        self.polling_task_ids_in_progress = polling_task_ids_in_progress if polling_task_ids_in_progress is not None else set()
        self.polling_task_ids_lock = polling_task_ids_lock or threading.Lock()
        self._state_lock = threading.Lock()
        self._download_executor: ThreadPoolExecutor | None = None
        self._last_state_save_time = 0.0
        self._state_dirty = False
        self._state_save_interval_seconds = 2.0
        self._emit_min_interval_seconds = 0.7
        self._last_emit_monotonic_per_task: dict[str, float] = {}
        self._stats_cache: dict | None = None
        self._stats_cache_monotonic = 0.0
        self._stats_cache_ttl_seconds = 0.7

    def _acquire_poll_lock(self, task_id: str) -> bool:
        task_id = str(task_id or "").strip()
        if not task_id:
            return True
        with self.polling_task_ids_lock:
            if task_id in self.polling_task_ids_in_progress:
                return False
            self.polling_task_ids_in_progress.add(task_id)
            return True

    def _release_poll_lock(self, task_id: str) -> None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        with self.polling_task_ids_lock:
            self.polling_task_ids_in_progress.discard(task_id)

    def _candidate_statuses(self) -> set[str]:
        statuses = {TaskStatus.VIDEO_SUBMITTED, TaskStatus.VIDEO_POLLING}
        if self.config.manual_poll_include_timeout_tasks:
            statuses.add(TaskStatus.VIDEO_TIMEOUT)
        if self.config.manual_poll_include_failed_tasks:
            statuses.add(TaskStatus.FAILED_VIDEO_API)
        return statuses

    def _manual_poll_candidates(self) -> list[TaskItem]:
        statuses = self._candidate_statuses()
        return [
            task
            for task in self.manager.tasks
            if task.video_task_id
            and not task.video_url
            and task.status in statuses
        ]

    def run(self) -> None:
        summary = ManualPollSummary(batch_id=self.batch_id)
        summary.skipped_no_task_id = sum(
            1
            for task in self.manager.tasks
            if not task.video_task_id and not task.video_url and not TaskStatus.is_done(task.status)
        )
        candidates = self._manual_poll_candidates()
        self._log("INFO", f"[MANUAL_POLL] start batch_id={self.batch_id}, candidates={len(candidates)}")
        if self.config.auto_download_video:
            workers = max(1, min(max(int(getattr(self.config, "download_concurrency", 20) or 20), 20), 100))
            self._download_executor = ThreadPoolExecutor(max_workers=workers)
        try:
            if candidates:
                max_poll_workers = max(1, min(len(candidates), int(getattr(self.config, "poll_concurrency", 20) or 20), 100))
                self._log("INFO", f"[MANUAL_POLL] polling workers={max_poll_workers}, configured={self.config.poll_concurrency}")
                with ThreadPoolExecutor(max_workers=max_poll_workers) as pool:
                    futures = [pool.submit(self._poll_manual_candidate, task) for task in candidates]
                    for future in as_completed(futures):
                        result = future.result()
                        if result == "SKIPPED_IN_PROGRESS":
                            summary.skipped_in_progress += 1
                        else:
                            summary.total_checked += 1
                            if result == "COMPLETED":
                                summary.completed += 1
                            elif result == "FAILED":
                                summary.failed += 1
                            elif result == "ERROR":
                                summary.error_count += 1
                            else:
                                summary.still_processing += 1
        finally:
            if self._download_executor:
                self._download_executor.shutdown(wait=True)
                self._download_executor = None
            self.manager.save_state(force_backup=True)
            self.stats_updated.emit(self.manager.stats())
            self._log(
                "INFO",
                "[MANUAL_POLL] finished "
                f"batch_id={self.batch_id}, checked={summary.total_checked}, completed={summary.completed}, "
                f"processing={summary.still_processing}, failed={summary.failed}, errors={summary.error_count}, "
                f"skipped_in_progress={summary.skipped_in_progress}",
            )
            self.finished_summary.emit(summary)

    def _poll_manual_candidate(self, task: TaskItem) -> str:
        task_id = str(task.video_task_id or "").strip()
        if not self._acquire_poll_lock(task_id):
            self._log("INFO", f"[MANUAL_POLL] task_id={task_id} 当前正在自动轮询，已跳过本次手动轮询", task)
            return "SKIPPED_IN_PROGRESS"
        try:
            return self._poll_one_manual(task)
        finally:
            self._release_poll_lock(task_id)

    def _poll_one_manual(self, task: TaskItem) -> str:
        original_status = task.status
        original_video_status = task.video_status
        task.manual_poll_count += 1
        task.last_manual_poll_time = now_text()
        task.video_poll_start_time = task.video_poll_start_time or task.last_manual_poll_time
        task.video_poll_count += 1
        self._set_status(task, TaskStatus.VIDEO_POLLING)
        self._log("INFO", f"[MANUAL_POLL] PID={task.pid} row={task.row_index} round={task.manual_poll_count} task_id={task.video_task_id}", task)
        try:
            task.video_provider = task.video_provider or self.config.video_provider
            video_provider = get_video_provider(task.video_provider)
            result = video_provider.poll_video_task(
                task_id=task.video_task_id or "",
                api_key=self.config.video_api_key,
                extra_params={
                    "base_url": self.config.video_api_base_url,
                    "timeout": self.config.request_timeout_seconds,
                },
            )
            task.video_raw_response = result.raw_response
            if result.finished and result.video_url:
                task.video_url = result.video_url
                task.video_poll_end_time = now_text()
                task.video_download_status = ""
                task.last_video_download_error = None
                task.last_manual_poll_result = "COMPLETED"
                self._set_status(task, TaskStatus.VIDEO_DOWNLOAD_PENDING, force=True)
                task.video_status = TaskStatus.VIDEO_DONE
                self._refresh_image_assets_metadata(task)
                self._queue_completed_video_download(task)
                self._save_emit(task, force=True)
                self._log("INFO", f"[MANUAL_POLL] PID={task.pid} row={task.row_index} video completed: {task.video_url}", task)
                return "COMPLETED"
            if result.failed:
                if getattr(result, "retryable_failure", False) or not _poll_result_is_explicit_terminal_failed(result):
                    task.error_message = result.error_message
                    task.last_manual_poll_result = "ERROR"
                    task.status = original_status
                    task.video_status = original_video_status
                    task.touch()
                    self._save_emit(task, force=True)
                    self._log(
                        "WARNING",
                        f"[MANUAL_POLL] PID={task.pid} row={task.row_index} non-terminal poll error; "
                        f"keep polling existing task_id={task.video_task_id}: {result.error_message}",
                        task,
                    )
                    return "ERROR"
                task.video_poll_end_time = now_text()
                task.error_message = result.error_message
                task.last_manual_poll_result = "FAILED"
                self._set_status(task, TaskStatus.FAILED_VIDEO_API, result.error_message, force=True)
                self._log("ERROR", f"[MANUAL_POLL] PID={task.pid} row={task.row_index} video failed: {result.error_message}", task)
                return "FAILED"
            if result.error_message and str(result.status or "").lower() in {"network_retry", "request_error"}:
                task.error_message = result.error_message
                task.last_manual_poll_result = "ERROR"
                task.status = original_status
                task.video_status = original_video_status
                task.touch()
                self._save_emit(task, force=True)
                self._log("ERROR", f"[MANUAL_POLL] PID={task.pid} row={task.row_index} poll error: {result.error_message}", task)
                return "ERROR"
            task.last_manual_poll_result = "PROCESSING"
            self._set_status(task, TaskStatus.VIDEO_POLLING)
            self._log("INFO", f"[MANUAL_POLL] PID={task.pid} row={task.row_index} video not ready, status={result.status}", task)
            return "PROCESSING"
        except Exception as exc:
            task.last_manual_poll_result = "ERROR"
            task.error_message = str(exc)
            task.status = original_status
            task.video_status = original_video_status
            task.touch()
            self._save_emit(task, force=True)
            self._log("ERROR", f"[MANUAL_POLL] PID={task.pid} row={task.row_index} exception: {exc}", task)
            return "ERROR"

    def _set_status(self, task: TaskItem, status: str, error: str | None = None, force: bool = False) -> None:
        task.set_status(status, error)
        if status in {
            TaskStatus.VIDEO_SUBMITTING,
            TaskStatus.VIDEO_SUBMITTED,
            TaskStatus.VIDEO_POLLING,
            TaskStatus.VIDEO_DOWNLOAD_PENDING,
            TaskStatus.VIDEO_DOWNLOADING,
            TaskStatus.COMPLETED,
            TaskStatus.VIDEO_DOWNLOADED,
            TaskStatus.VIDEO_FAILED,
            TaskStatus.VIDEO_TIMEOUT,
            TaskStatus.FAILED_VIDEO_API,
        }:
            task.video_status = status
        self._save_emit(task, force=force)

    def _save_emit(self, task: TaskItem, force: bool = False) -> None:
        key = str(getattr(task, "task_uid", "") or f"{task.pid}::row_{task.row_index}")
        now = time.monotonic()
        if not force:
            last = self._last_emit_monotonic_per_task.get(key, 0.0)
            if now - last < self._emit_min_interval_seconds:
                with self._state_lock:
                    self._state_dirty = True
                return
            self._last_emit_monotonic_per_task[key] = now

        stats: dict | None = None
        with self._state_lock:
            self._state_dirty = True
            if force or now - self._last_state_save_time >= self._state_save_interval_seconds:
                try:
                    saved = self.manager.save_state(force_backup=force)
                    if saved:
                        self._last_state_save_time = now
                        self._state_dirty = False
                except Exception as exc:
                    self._state_dirty = True
                    self.logger.warning("manual poll task_state save deferred: %s", exc)
            if force or self._stats_cache is None or now - self._stats_cache_monotonic >= self._stats_cache_ttl_seconds:
                self._stats_cache = self.manager.stats()
                self._stats_cache_monotonic = now
            stats = self._stats_cache
        self.task_updated.emit(task)
        if stats is not None:
            self.stats_updated.emit(stats)

    def _refresh_image_assets_metadata(self, task: TaskItem) -> None:
        if not self.config.auto_save_image_assets:
            return
        try:
            save_image_task_assets(task, self.config.image_assets_root)
        except Exception as exc:
            self._log("ERROR", f"[ASSETS] PID={task.pid} row={task.row_index} update image metadata failed: {exc}", task)

    def _video_save_path(self, task: TaskItem) -> Path:
        return video_download_output_path(self.config.video_download_root, task, self.config.group_by_owner)

    def _local_video_fallback_root(self) -> Path:
        return Path.home() / "Downloads" / "Veo3下载视频"

    def _download_video_to_root(self, task: TaskItem, root: str | Path) -> str:
        save_path = video_download_output_path(root, task, self.config.group_by_owner)
        path = _download_video_for_task_with_provider(task, self.config, save_path)
        task.video_file_path = path
        task.video_download_status = "DOWNLOADED"
        task.last_video_download_error = None
        sync_task_urls_from_paths(task, self.config)
        save_video_task_metadata(task, root, self.config.group_by_owner)
        return path

    def _download_completed_video(self, task: TaskItem) -> bool:
        if not task.video_url or not self.config.auto_download_video:
            return False
        task.video_download_status = "DOWNLOADING"
        task.last_video_download_time = now_text()
        task.video_download_attempt_count += 1
        try:
            self._download_video_to_root(task, self.config.video_download_root)
            return True
        except Exception as primary_exc:
            fallback_root = self._local_video_fallback_root()
            try:
                self._log(
                    "ERROR",
                    f"[DOWNLOAD] PID={task.pid} row={task.row_index} primary archive failed, trying local fallback: {primary_exc}",
                    task,
                )
                self._download_video_to_root(task, fallback_root)
                self._log("INFO", f"[DOWNLOAD] PID={task.pid} row={task.row_index} saved to local fallback: {task.video_file_path}", task)
                return True
            except Exception as fallback_exc:
                exc = RuntimeError(f"primary={primary_exc}; local_fallback={fallback_exc}")
            task.last_video_download_time = now_text()
            task.last_video_download_error = str(exc)
            task.error_message = f"Video download failed; will retry before URL expires: {exc}"
            if task.video_task_id:
                old_url = task.video_url
                task.video_url = None
                task.video_download_status = "WAITING_NEW_URL"
                task.set_status(TaskStatus.VIDEO_SUBMITTED, task.error_message)
                task.video_status = TaskStatus.VIDEO_SUBMITTED
                self._log(
                    "ERROR",
                    f"[DOWNLOAD] PID={task.pid} row={task.row_index} failed after retries; old URL may expire, re-poll task_id={task.video_task_id}: {old_url}",
                    task,
                )
            else:
                task.video_download_status = "FAILED_FINAL"
                self._log("ERROR", f"[DOWNLOAD] PID={task.pid} row={task.row_index} failed and no task_id is available for URL refresh: {exc}", task)
            return False

    def _queue_completed_video_download(self, task: TaskItem) -> None:
        if not task.video_url:
            return
        if not self.config.auto_download_video:
            return
        if not self._download_executor:
            downloaded = self._download_completed_video(task)
            if downloaded:
                self._set_status(task, TaskStatus.VIDEO_DOWNLOADED, force=True)
            else:
                self._save_emit(task, force=True)
            return
        self._download_executor.submit(self._download_task_and_emit, task)

    def _download_task_and_emit(self, task: TaskItem) -> None:
        downloaded = self._download_completed_video(task)
        if downloaded:
            self._set_status(task, TaskStatus.VIDEO_DOWNLOADED, force=True)
            self._log("INFO", f"[DOWNLOAD] PID={task.pid} row={task.row_index} saved: {task.video_file_path}", task)
        else:
            self._save_emit(task, force=True)
            if task.error_message:
                self._log("ERROR", f"[DOWNLOAD] PID={task.pid} row={task.row_index} failed: {task.error_message}", task)

    def _log(self, level: str, message: str, task: TaskItem | None = None) -> None:
        if task is not None:
            append_task_log(task, level, None, message)
        getattr(self.logger, level.lower(), self.logger.info)(message)
        self.log_message.emit(level, message, task)
