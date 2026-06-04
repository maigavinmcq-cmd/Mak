from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path

from light_image_tool.models import (
    LightImageQueueState,
    LightImageTask,
    LightImageTaskStatus,
    PIDScanResult,
    now_text,
)
from light_image_tool.output_manager import make_output_path


class LightImageQueueManager:
    def __init__(self) -> None:
        self.tasks: list[LightImageTask] = []
        self.state = LightImageQueueState.empty()
        self._lock = threading.RLock()

    def add_manual_tasks(
        self,
        *,
        image_paths: list[str],
        prompt: str,
        generation_count: int,
        output_dir: str,
        provider: str | None,
        model: str | None,
    ) -> list[LightImageTask]:
        created: list[LightImageTask] = []
        count = max(1, int(generation_count or 1))
        references = [str(path) for path in image_paths if str(path or "").strip()]
        if not references:
            return created
        with self._lock:
            for generation_index in range(1, count + 1):
                task = LightImageTask.create(
                    task_type="manual_image",
                    source_image_path=references[0],
                    reference_image_paths=references,
                    prompt=prompt,
                    generation_index=generation_index,
                    generation_count=count,
                    output_dir=output_dir,
                    api_provider=provider,
                    api_model=model,
                )
                created.append(task)
            self.tasks.extend(created)
            self.refresh_state()
        return created

    def add_pid_scan_tasks(
        self,
        *,
        scan_results: list[PIDScanResult],
        prompt_template: str,
        generation_count: int,
        provider: str | None,
        model: str | None,
    ) -> list[LightImageTask]:
        created: list[LightImageTask] = []
        count = max(1, int(generation_count or 1))
        with self._lock:
            for result in scan_results:
                if result.status != "found":
                    continue
                references = [str(path) for path in result.selected_images if str(path or "").strip()]
                if not references:
                    continue
                total = len(references)
                image_name = ", ".join(Path(image_path).name for image_path in references)
                prompt = render_prompt_template(
                    prompt_template,
                    pid=result.pid,
                    image_name=image_name,
                    image_index=1,
                    total_images=total,
                )
                for generation_index in range(1, count + 1):
                    task = LightImageTask.create(
                        task_type="pid_white_bg",
                        pid=result.pid,
                        source_image_path=references[0],
                        reference_image_paths=references,
                        prompt=prompt,
                        prompt_template_name="白底图生成模板",
                        generation_index=generation_index,
                        generation_count=count,
                        output_dir=result.output_dir or "",
                        api_provider=provider,
                        api_model=model,
                    )
                    created.append(task)
            self.tasks.extend(created)
            self.refresh_state()
        return created

    def next_waiting_task(self) -> LightImageTask | None:
        with self._lock:
            for task in self.tasks:
                if task.status == LightImageTaskStatus.WAITING:
                    self.update_task_status(task, LightImageTaskStatus.QUEUED)
                    return task
        return None

    def update_task_status(
        self,
        task: LightImageTask,
        status: str,
        *,
        error_message: str | None = None,
        skip_reason: str | None = None,
        output_file_path: str | None = None,
        output_url: str | None = None,
    ) -> None:
        with self._lock:
            task.status = status
            task.updated_at = now_text()
            if status in {LightImageTaskStatus.GENERATING, LightImageTaskStatus.DOWNLOADING} and not task.started_at:
                task.started_at = task.updated_at
            if status in LightImageTaskStatus.TERMINAL:
                task.ended_at = task.updated_at
            if error_message is not None:
                task.error_message = error_message
            if skip_reason is not None:
                task.skip_reason = skip_reason
            if output_file_path is not None:
                task.output_file_path = output_file_path
            if output_url is not None:
                task.output_url = output_url
            self.refresh_state()

    def prepare_output_path(self, task: LightImageTask, suffix: str = ".png") -> Path:
        return make_output_path(
            output_dir=task.output_dir,
            pid=task.pid,
            source_image_name=task.source_image_name,
            task_type=task.task_type,
            generation_index=task.generation_index,
            suffix=suffix,
        )

    def retry_tasks(self, tasks: list[LightImageTask]) -> int:
        count = 0
        with self._lock:
            for task in tasks:
                if task.status in {LightImageTaskStatus.FAILED, LightImageTaskStatus.SKIPPED, LightImageTaskStatus.STOPPED}:
                    old_error = task.error_message
                    task.add_log("INFO", "retry", "任务已重新进入等待队列", old_error)
                    task.status = LightImageTaskStatus.WAITING
                    task.error_message = None
                    task.skip_reason = None
                    task.started_at = None
                    task.ended_at = None
                    count += 1
            self.refresh_state()
        return count

    def retry_failed(self) -> int:
        return self.retry_tasks([task for task in self.tasks if task.status == LightImageTaskStatus.FAILED])

    def retry_skipped(self) -> int:
        return self.retry_tasks([task for task in self.tasks if task.status == LightImageTaskStatus.SKIPPED])

    def mark_waiting_as_stopped(self, reason: str = "用户停止后未提交") -> int:
        count = 0
        stamp = now_text()
        with self._lock:
            for task in self.tasks:
                if task.status != LightImageTaskStatus.WAITING:
                    continue
                task.status = LightImageTaskStatus.STOPPED
                task.skip_reason = reason
                task.ended_at = stamp
                task.updated_at = stamp
                count += 1
            if count:
                self.refresh_state()
        return count

    def reset_active_to_waiting(self, reason: str = "检测到未运行的残留活跃状态，重新排队") -> int:
        count = 0
        with self._lock:
            for task in self.tasks:
                if task.status not in LightImageTaskStatus.ACTIVE:
                    continue
                task.add_log("WARN", "queue", reason)
                task.status = LightImageTaskStatus.WAITING
                task.started_at = None
                task.ended_at = None
                task.updated_at = now_text()
                count += 1
            if count:
                self.refresh_state()
        return count

    def clear_completed(self) -> int:
        with self._lock:
            before = len(self.tasks)
            self.tasks = [task for task in self.tasks if task.status != LightImageTaskStatus.COMPLETED]
            removed = before - len(self.tasks)
            self.refresh_state()
            return removed

    def clear_all(self) -> None:
        with self._lock:
            self.tasks.clear()
            self.state = LightImageQueueState.empty()

    def refresh_state(self) -> LightImageQueueState:
        total = len(self.tasks)
        pending = sum(1 for task in self.tasks if task.status == LightImageTaskStatus.WAITING)
        running = sum(1 for task in self.tasks if task.status in LightImageTaskStatus.ACTIVE)
        completed = sum(1 for task in self.tasks if task.status == LightImageTaskStatus.COMPLETED)
        failed = sum(1 for task in self.tasks if task.status == LightImageTaskStatus.FAILED)
        skipped = sum(1 for task in self.tasks if task.status == LightImageTaskStatus.SKIPPED)
        stopped = sum(1 for task in self.tasks if task.status == LightImageTaskStatus.STOPPED)
        done = completed + failed + skipped + stopped
        self.state.total_count = total
        self.state.pending_count = pending
        self.state.running_count = running
        self.state.completed_count = completed
        self.state.failed_count = failed
        self.state.skipped_count = skipped
        self.state.stopped_count = stopped
        self.state.progress_percent = round((done / total) * 100, 1) if total else 0.0
        self.state.updated_at = now_text()
        if running:
            self.state.status = "运行中"
        elif pending:
            self.state.status = "等待中"
        elif total and done == total:
            self.state.status = "已完成"
        else:
            self.state.status = "空闲"
        return self.state

    def save_state(self, queue_dir: str | Path) -> None:
        root = Path(queue_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / "queue_info.json").write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "task_state.json").write_text(
            json.dumps([asdict(task) for task in self.tasks], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def render_prompt_template(template: str, *, pid: str, image_name: str, image_index: int, total_images: int) -> str:
    values = {
        "pid": pid,
        "image_name": image_name,
        "image_index": image_index,
        "total_images": total_images,
    }
    try:
        return str(template or "").format(**values)
    except Exception:
        return str(template or "")
