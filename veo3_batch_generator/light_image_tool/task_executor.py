from __future__ import annotations

import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from light_image_tool.api_adapter import LightImageApiAdapter
from light_image_tool.log_manager import LightImageLogManager, redact_secrets
from light_image_tool.models import LightImageTask, LightImageTaskStatus
from light_image_tool.output_manager import download_image_url, suffix_from_url, write_image_bytes
from light_image_tool.queue_manager import LightImageQueueManager


TaskCallback = Callable[[LightImageTask], None]
LogCallback = Callable[[str, str], None]
DoneCallback = Callable[[], None]


class LightImageTaskExecutor:
    def __init__(
        self,
        queue_manager: LightImageQueueManager,
        api_adapter: LightImageApiAdapter,
        log_manager: LightImageLogManager,
        *,
        task_failed_retry_count: int = 3,
        task_failed_retry_interval_seconds: int = 8,
        on_task_updated: TaskCallback | None = None,
        on_log: LogCallback | None = None,
        on_done: DoneCallback | None = None,
    ) -> None:
        self.queue_manager = queue_manager
        self.api_adapter = api_adapter
        self.log_manager = log_manager
        self.on_task_updated = on_task_updated
        self.on_log = on_log
        self.on_done = on_done
        self.task_failed_retry_count = max(0, min(10, int(task_failed_retry_count or 0)))
        self.task_failed_retry_interval_seconds = max(1, int(task_failed_retry_interval_seconds or 1))
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._running_lock = threading.Lock()
        self._running = False

    @property
    def running(self) -> bool:
        with self._running_lock:
            return self._running

    @property
    def paused(self) -> bool:
        return self._pause_event.is_set()

    def start(self, concurrency: int) -> bool:
        if self.running:
            return False
        self._stop_event.clear()
        self._pause_event.clear()
        self._thread = threading.Thread(target=self._run_scheduler, args=(max(1, int(concurrency or 1)),), daemon=True)
        with self._running_lock:
            self._running = True
        self._thread.start()
        self._log("INFO", "队列已开始")
        return True

    def pause(self) -> None:
        self._pause_event.set()
        self._log("WARN", "队列已暂停，将不再提交新任务")

    def resume(self) -> None:
        self._pause_event.clear()
        self._log("INFO", "队列已继续")

    def stop(self, reason: str = "用户停止") -> None:
        self._stop_event.set()
        self.queue_manager.state.stop_requested = True
        self.queue_manager.state.stop_reason = reason
        self._log("WARN", f"已请求停止：{reason}")

    def detach_ui_callbacks(self) -> None:
        self.on_task_updated = None
        self.on_log = None
        self.on_done = None

    def _run_scheduler(self, concurrency: int) -> None:
        start_time = time.perf_counter()
        self.log_manager.log("INFO", "queue", f"线程启动，并发数：{concurrency}", diagnostics=True)
        futures: dict[Future, LightImageTask] = {}
        try:
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="light-image") as pool:
                while True:
                    if self._stop_event.is_set() and not futures:
                        self._mark_waiting_as_stopped()
                        break
                    while (
                        not self._stop_event.is_set()
                        and not self._pause_event.is_set()
                        and len(futures) < concurrency
                    ):
                        task = self.queue_manager.next_waiting_task()
                        if task is None:
                            break
                        self._emit_task(task)
                        futures[pool.submit(self._run_one, task)] = task
                    if not futures:
                        if self.queue_manager.state.pending_count <= 0:
                            break
                        time.sleep(0.15)
                        continue
                    done, _pending = wait(futures.keys(), timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in done:
                        task = futures.pop(future)
                        try:
                            future.result()
                        except Exception as exc:
                            self.queue_manager.update_task_status(task, LightImageTaskStatus.FAILED, error_message=str(exc))
                            task.add_log("ERROR", "executor", "任务执行异常", str(exc))
                            self.log_manager.exception("executor", "任务执行异常", exc, task.task_uid)
                            self._emit_task(task)
                if self._stop_event.is_set():
                    self.queue_manager.state.status = "已停止"
                self.queue_manager.save_state(self.log_manager.queue_dir)
        finally:
            elapsed = round(time.perf_counter() - start_time, 3)
            self.log_manager.log("INFO", "queue", f"线程退出，耗时 {elapsed}s", diagnostics=True)
            with self._running_lock:
                self._running = False
            if self.on_done:
                self.on_done()
            self._log("INFO", "队列执行结束")

    def _run_one(self, task: LightImageTask) -> None:
        if self._stop_event.is_set():
            self.queue_manager.update_task_status(task, LightImageTaskStatus.STOPPED, skip_reason="停止后未提交")
            self._emit_task(task)
            return
        reference_images = task.reference_image_paths or [task.source_image_path]
        missing_images = [image for image in reference_images if not _is_http_url(image) and not Path(image).exists()]
        if missing_images:
            message = f"源图片不存在：{missing_images[0]}"
            self.queue_manager.update_task_status(task, LightImageTaskStatus.FAILED, error_message=message)
            task.add_log("ERROR", "validate", message)
            self._emit_task(task)
            return
        try:
            output_path = self.queue_manager.prepare_output_path(task)
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            message = f"输出目录无法创建：{exc}"
            self.queue_manager.update_task_status(task, LightImageTaskStatus.FAILED, error_message=message)
            task.add_log("ERROR", "output", message)
            self._emit_task(task)
            return

        self.queue_manager.update_task_status(task, LightImageTaskStatus.GENERATING)
        task.add_log("INFO", "api", "API 调用开始")
        self._emit_task(task)
        api_start = time.perf_counter()
        result = self._generate_with_retry(
            task=task,
            output_path=output_path,
            reference_images=reference_images,
        )
        elapsed = round(time.perf_counter() - api_start, 3)
        self.log_manager.log("INFO", "api", f"API 请求耗时 {elapsed}s", task_uid=task.task_uid, diagnostics=True)
        self._record_request_summary(task, result.get("request_summary"))
        if not result.get("success"):
            message = str(result.get("error_message") or "图生图 API 返回失败")
            self.queue_manager.update_task_status(task, LightImageTaskStatus.FAILED, error_message=message)
            task.add_log("ERROR", "api", "API 调用失败", message)
            self._log("ERROR", f"任务失败：{task.source_image_name}｜{message}")
            self._emit_task(task)
            return

        self.queue_manager.update_task_status(task, LightImageTaskStatus.DOWNLOADING, output_url=result.get("image_url"))
        self._emit_task(task)
        try:
            image_path = str(result.get("image_path") or "")
            if image_path and Path(image_path).exists():
                final_path = image_path
            elif result.get("image_bytes"):
                final_path = write_image_bytes(result["image_bytes"], output_path)
            elif result.get("image_url"):
                url = str(result["image_url"])
                download_path = output_path.with_suffix(suffix_from_url(url))
                final_path = download_image_url(url, download_path)
            else:
                raise RuntimeError("API 成功但没有返回图片路径、图片二进制或图片 URL")
        except Exception as exc:
            message = f"图片保存失败：{exc}"
            self.queue_manager.update_task_status(task, LightImageTaskStatus.FAILED, error_message=message)
            task.add_log("ERROR", "download", message)
            self._emit_task(task)
            return
        self.queue_manager.update_task_status(task, LightImageTaskStatus.COMPLETED, output_file_path=final_path)
        task.add_log("INFO", "output", "图片保存成功", final_path)
        self._log("INFO", f"任务完成：{Path(final_path).name}")
        self._emit_task(task)

    def _generate_with_retry(
        self,
        *,
        task: LightImageTask,
        output_path: Path,
        reference_images: list[str],
    ) -> dict:
        max_attempts = 1 + self.task_failed_retry_count
        last_result: dict | None = None
        for attempt in range(1, max_attempts + 1):
            result = self.api_adapter.generate_image(
                input_image_path=task.source_image_path,
                prompt=task.prompt,
                provider=task.api_provider,
                model=task.api_model,
                extra_params={
                    "output_path": str(output_path),
                    "stop_event": self._stop_event,
                    "input_images": reference_images,
                    "requires_reference_image": True,
                },
            )
            last_result = result
            if result.get("success"):
                if attempt > 1:
                    task.add_log("INFO", "api", f"API 重试成功：第 {attempt} 次")
                return result
            if attempt >= max_attempts or not self._is_retryable_api_failure(result):
                return result
            if self._stop_event.is_set():
                return result
            delay = self._retry_delay_seconds(attempt)
            message = f"API 返回可重试失败，{delay}s 后自动重试 ({attempt}/{self.task_failed_retry_count})"
            detail = str(result.get("error_message") or "")
            task.add_log("WARN", "api_retry", message, detail)
            self.log_manager.log("WARN", "api_retry", message, task_uid=task.task_uid, detail=detail, diagnostics=True)
            if self._sleep_or_stopped(delay):
                return result
        return last_result or {"success": False, "error_message": "API 调用失败"}

    def _retry_delay_seconds(self, attempt: int) -> int:
        jitter = int(self.queue_manager.state.total_count or 0) % 3
        return min(120, self.task_failed_retry_interval_seconds * attempt + jitter)

    def _sleep_or_stopped(self, seconds: int) -> bool:
        deadline = time.monotonic() + max(0, int(seconds or 0))
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return True
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        return self._stop_event.is_set()

    @staticmethod
    def _is_retryable_api_failure(result: dict) -> bool:
        if not isinstance(result, dict) or result.get("success"):
            return False
        message = str(result.get("error_message") or "")
        raw = str(result.get("raw_response") or "")
        text = f"{message}\n{raw}".lower()
        retry_markers = [
            "task_failed",
            "没有按照预期生成图片",
            "please adjust",
            "unexpected",
        ]
        return any(marker.lower() in text for marker in retry_markers)

    def _record_request_summary(self, task: LightImageTask, summary: object) -> None:
        if not isinstance(summary, dict):
            return
        detail = json.dumps(summary, ensure_ascii=False, indent=2)
        self.log_manager.log(
            "INFO",
            "api_payload",
            "API 请求摘要",
            task_uid=task.task_uid,
            detail=detail,
            diagnostics=True,
        )
        compact = (
            f"provider={summary.get('provider') or '-'}, "
            f"model={summary.get('model') or '-'}, "
            f"provider_model={summary.get('provider_model') or '-'}, "
            f"size={summary.get('size') or '-'}, "
            f"urls={summary.get('url_count', 0)}/{summary.get('reference_count', 0)}"
        )
        urls = summary.get("urls")
        if isinstance(urls, list) and urls:
            compact = f"{compact}; first_url={urls[0]}"
        task.add_log("INFO", "api_payload", "API 请求摘要", redact_secrets(compact))

    def _mark_waiting_as_stopped(self) -> None:
        count = self.queue_manager.mark_waiting_as_stopped("用户停止后未提交")
        if not count:
            return
        self.queue_manager.save_state(self.log_manager.queue_dir)
        if self.on_task_updated:
            self.on_task_updated(None)  # type: ignore[arg-type]

    def _emit_task(self, task: LightImageTask) -> None:
        self.queue_manager.save_state(self.log_manager.queue_dir)
        if self.on_task_updated:
            self.on_task_updated(task)

    def _log(self, level: str, message: str) -> None:
        self.log_manager.log(level, "queue", message)
        if self.on_log:
            self.on_log(level, message)


def _is_http_url(value: str) -> bool:
    return str(value or "").strip().lower().startswith(("http://", "https://"))
