from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from app.models.task import TaskItem, TaskStatus
from app.task_logs import task_error_log_summary
from app.workflow import (
    NODE_STATUS_POLLING,
    NODE_STATUS_RUNNING,
    NODE_STATUS_SUBMITTED,
    NODE_TYPE_IMAGE,
    NODE_TYPE_VIDEO,
)


class TaskManager:
    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path)
        self.backup_state_path = self.state_path.with_suffix(self.state_path.suffix + ".bak")
        self.legacy_state_path = Path(__file__).resolve().parent.parent / "state" / "task_state.json"
        self.legacy_backup_state_path = self.legacy_state_path.with_suffix(self.legacy_state_path.suffix + ".bak")
        self.tasks: list[TaskItem] = []
        self._last_backup_time = 0.0
        self.default_image_provider = ""
        self.default_image_model = ""
        self.default_video_provider = ""
        self.default_video_model = ""
        self.default_batch_id = ""
        self.default_batch_name = ""
        self.default_imported_at = ""
        self.default_source_excel_path = ""
        self.last_save_error = ""
        self.last_save_fallback_path = ""

    def configure_defaults(
        self,
        image_provider: str = "",
        image_model_logical_key: str = "",
        video_provider: str = "",
        video_model_logical_key: str = "",
    ) -> None:
        self.default_image_provider = image_provider
        self.default_image_model = image_model_logical_key
        self.default_video_provider = video_provider
        self.default_video_model = video_model_logical_key

    def configure_batch_context(
        self,
        batch_id: str = "",
        batch_name: str = "",
        imported_at: str = "",
        source_excel_path: str = "",
    ) -> None:
        self.default_batch_id = str(batch_id or "")
        self.default_batch_name = str(batch_name or "")
        self.default_imported_at = str(imported_at or "")
        self.default_source_excel_path = str(source_excel_path or "")

    def update_state_path(self, state_path: str | Path) -> None:
        self.state_path = Path(state_path)
        self.backup_state_path = self.state_path.with_suffix(self.state_path.suffix + ".bak")

    def set_tasks(self, tasks: list[TaskItem], preserve_existing: bool = False) -> None:
        for task in tasks:
            self._ensure_task_uid(task)
        if not preserve_existing:
            self.tasks = tasks
            self.save_state()
            return

        for task in self.tasks:
            self._ensure_task_uid(task)
        old_by_key = {self.task_key(task): task for task in self.tasks}
        merged: list[TaskItem] = []
        for task in tasks:
            old = old_by_key.get(self.task_key(task))
            if old:
                for field in [
                    "product_image_path",
                    "product_image_url",
                    "product_image_source_type",
                    "netdisk_original_path",
                    "netdisk_http_path",
                    "task_uid",
                    "batch_name",
                    "imported_at",
                    "source_excel_path",
                    "prompt_stage_1",
                    "prompt_stage_2",
                    "prompt_stage_3",
                    "prompt_stage_4",
                    "workflow_version",
                    "legacy_prompt_compat_mode",
                    "node_states",
                    "generated_image_path",
                    "generated_image_url",
                    "image_task_id",
                    "image_raw_response",
                    "image_provider",
                    "image_model_logical_key",
                    "image_model_display",
                    "image_status",
                    "video_url",
                    "video_file_path",
                    "video_task_id",
                    "video_provider",
                    "video_model_logical_key",
                    "video_model_display",
                    "video_status",
                    "video_submit_time",
                    "video_poll_start_time",
                    "video_poll_end_time",
                    "video_poll_count",
                    "video_raw_response",
                    "video_download_status",
                    "video_download_attempt_count",
                    "last_video_download_time",
                    "last_video_download_error",
                    "manual_poll_count",
                    "last_manual_poll_time",
                    "last_manual_poll_result",
                    "auto_retry_count",
                    "last_auto_retry_time",
                    "last_auto_retry_stage",
                    "last_auto_retry_reason",
                    "status",
                    "error_message",
                    "started_at",
                    "ended_at",
                    "elapsed_seconds",
                    "logs",
                    "task_logs",
                ]:
                    setattr(task, field, getattr(old, field))
            merged.append(task)
        self.tasks = merged
        self.save_state()

    @staticmethod
    def task_uid_for(task: TaskItem) -> str:
        batch_id = str(getattr(task, "batch_id", "") or "unbatched")
        pid = str(getattr(task, "pid", "") or "unknown_pid")
        row_index = int(getattr(task, "row_index", 0) or 0)
        return f"{batch_id}::{pid}::row_{row_index}"

    def _ensure_task_uid(self, task: TaskItem) -> None:
        expected = self.task_uid_for(task)
        if task.task_uid != expected:
            task.task_uid = expected

    def task_key(self, task: TaskItem) -> str:
        self._ensure_task_uid(task)
        return task.task_uid

    def save_state(self, force_backup: bool = False, raise_on_failure: bool = False) -> bool:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.last_save_error = ""
        self.last_save_fallback_path = ""
        expected_batch_id = self.expected_batch_id_from_state_path()
        if expected_batch_id:
            for task in self.tasks:
                if not task.batch_id:
                    task.batch_id = expected_batch_id
                if task.batch_id != expected_batch_id:
                    raise RuntimeError(
                        f"Refusing to save task_state for batch {expected_batch_id}: "
                        f"task row={task.row_index} pid={task.pid} belongs to batch {task.batch_id}"
                    )
                self._ensure_task_uid(task)
        payload = {
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tasks": [self._compact_task_dump(task) for task in self.tasks],
        }
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        now = time.monotonic()
        should_backup = force_backup or (now - self._last_backup_time >= 60)
        if should_backup and self.state_path.exists() and self.state_path.stat().st_size > 0:
            try:
                shutil.copy2(self.state_path, self.backup_state_path)
                self._last_backup_time = now
            except Exception:
                pass
        for attempt in range(5):
            tmp_path = self._unique_tmp_state_path()
            try:
                tmp_path.write_text(content, encoding="utf-8")
                tmp_path.replace(self.state_path)
                latest_path = self.state_path.parent / "task_state_latest.json"
                if latest_path != self.state_path:
                    shutil.copy2(self.state_path, latest_path)
                return True
            except FileNotFoundError as exc:
                self.last_save_error = f"{type(exc).__name__}: {exc}"
                if attempt >= 4:
                    # Last-resort fallback for flaky network shares. The normal
                    # path above is still atomic; this only prevents startup from
                    # crashing if the share removes the temp file between write
                    # and replace.
                    try:
                        self.state_path.write_text(content, encoding="utf-8")
                        return True
                    except Exception as direct_exc:
                        return self._handle_state_save_failure(content, direct_exc, raise_on_failure)
                time.sleep(0.2 * (attempt + 1))
            except PermissionError as exc:
                self.last_save_error = f"{type(exc).__name__}: {exc}"
                if attempt >= 4:
                    return self._handle_state_save_failure(content, exc, raise_on_failure)
                time.sleep(0.2 * (attempt + 1))
            except OSError as exc:
                self.last_save_error = f"{type(exc).__name__}: {exc}"
                if attempt >= 4:
                    return self._handle_state_save_failure(content, exc, raise_on_failure)
                time.sleep(0.2 * (attempt + 1))
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass
        return False

    def _handle_state_save_failure(self, content: str, exc: Exception, raise_on_failure: bool) -> bool:
        self.last_save_error = f"{type(exc).__name__}: {exc}"
        try:
            self.last_save_fallback_path = str(self._write_local_save_fallback(content))
        except Exception as fallback_exc:
            self.last_save_error = f"{self.last_save_error}; fallback failed: {type(fallback_exc).__name__}: {fallback_exc}"
        if raise_on_failure:
            raise exc
        return False

    def _write_local_save_fallback(self, content: str) -> Path:
        batch_id = self.expected_batch_id_from_state_path() or self.state_path.parent.name or "unbatched"
        safe_batch_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(batch_id))
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        fallback_dir = self.state_path.parent / "state_save_fallback"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        fallback_path = fallback_dir / f"{safe_batch_id}_task_state_{stamp}.json"
        fallback_path.write_text(content, encoding="utf-8")
        return fallback_path

    def _unique_tmp_state_path(self) -> Path:
        stamp = int(time.time() * 1000)
        return self.state_path.with_name(
            f"{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.{stamp}.tmp"
        )

    def expected_batch_id_from_state_path(self) -> str:
        path = self.state_path
        if path.name != "task_state.json":
            return ""
        parent = path.parent
        try:
            if parent.parent.name == "batches":
                return parent.name
        except IndexError:
            return ""
        return ""

    def _compact_task_dump(self, task: TaskItem) -> dict:
        self._ensure_task_uid(task)
        data = task.model_dump()
        data["logs"] = list(data.get("logs") or [])[-50:]
        data["task_logs"] = list(data.get("task_logs") or [])[-500:]
        if str(data.get("generated_image_url") or "").startswith("data:image"):
            data["generated_image_url"] = None
        data["image_raw_response"] = self._compact_raw_response(data.get("image_raw_response"))
        data["video_raw_response"] = self._compact_raw_response(data.get("video_raw_response"))
        data["node_states"] = self._compact_node_states(data.get("node_states"))
        return data

    def _compact_node_states(self, node_states):
        if not isinstance(node_states, dict):
            return node_states
        compact_states = {}
        for node_id, state in node_states.items():
            if not isinstance(state, dict):
                compact_states[node_id] = state
                continue
            compact_state = dict(state)
            compact_state["raw_response"] = self._compact_raw_response(compact_state.get("raw_response"))
            for key in ["input_images", "manual_input_images", "manual_input_urls"]:
                value = compact_state.get(key)
                if isinstance(value, list):
                    compact_state[key] = [self._compact_string(str(item), limit=500) for item in value[:10]]
            compact_states[node_id] = compact_state
        return compact_states

    def _compact_raw_response(self, value):
        if value is None:
            return None
        if isinstance(value, str):
            return self._compact_string(value)
        if isinstance(value, dict):
            keep_keys = {
                "id",
                "task_id",
                "status",
                "state",
                "url",
                "image_url",
                "video_url",
                "error",
                "message",
                "code",
                "type",
                "created",
                "model",
                "data",
                "output",
            }
            compact = {}
            for key, item in value.items():
                key_text = str(key)
                lowered = key_text.lower()
                if lowered in {"b64", "b64_json", "base64", "image_base64"}:
                    compact[key_text] = f"<omitted base64 length={len(str(item))}>"
                elif lowered in keep_keys or isinstance(item, (dict, list)):
                    compact[key_text] = self._compact_raw_response(item)
                else:
                    compact[key_text] = self._compact_string(str(item))
            return compact
        if isinstance(value, list):
            return [self._compact_raw_response(item) for item in value[:5]]
        return value

    @staticmethod
    def _compact_string(value: str, limit: int = 1000) -> str:
        if value.startswith("data:image") or len(value) > 50000:
            return f"<omitted large string length={len(value)}>"
        return value if len(value) <= limit else value[:limit] + f"...<truncated length={len(value)}>"

    def _compat_item(self, item: dict) -> dict:
        item = dict(item)
        now_date = datetime.now().strftime("%Y-%m-%d")
        item.setdefault("task_name", "")
        item.setdefault("owner", "")
        item.setdefault("netdisk_original_path", item.get("netdisk_path", ""))
        item.setdefault("netdisk_http_path", "")
        item.setdefault("product_image_url", None)
        item.setdefault("product_image_source_type", "")
        item.setdefault("image_task_id", None)
        item.setdefault("image_provider", self.default_image_provider)
        item.setdefault("image_model_logical_key", self.default_image_model)
        item.setdefault("video_provider", self.default_video_provider)
        item.setdefault("video_model_logical_key", self.default_video_model)
        item.setdefault("task_added_date", now_date)
        item.setdefault("batch_date", item.get("task_added_date") or now_date)
        item.setdefault("batch_id", self.default_batch_id or datetime.now().strftime("%Y-%m-%d_%H%M%S"))
        item.setdefault("task_uid", f"{item.get('batch_id') or 'unbatched'}::{item.get('pid') or 'unknown_pid'}::row_{item.get('row_index') or 0}")
        item.setdefault("batch_name", self.default_batch_name or f"批次_{item.get('batch_id', '')}")
        item.setdefault("imported_at", self.default_imported_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        item.setdefault("source_excel_path", self.default_source_excel_path)
        item.setdefault("image_status", "")
        item.setdefault("video_status", "")
        item.setdefault("video_download_status", "")
        item.setdefault("video_download_attempt_count", 0)
        item.setdefault("last_video_download_time", None)
        item.setdefault("last_video_download_error", None)
        item.setdefault("manual_poll_count", 0)
        item.setdefault("last_manual_poll_time", None)
        item.setdefault("last_manual_poll_result", None)
        item.setdefault("auto_retry_count", 0)
        item.setdefault("last_auto_retry_time", None)
        item.setdefault("last_auto_retry_stage", None)
        item.setdefault("last_auto_retry_reason", None)
        return item

    def load_tasks_from_state_path(self, source_path: str | Path) -> list[TaskItem]:
        path = Path(source_path)
        if not self._path_has_content(path):
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        tasks = [TaskItem(**self._compat_item(item)) for item in data.get("tasks", [])]
        for task in tasks:
            self._ensure_task_uid(task)
        return tasks

    def load_state(self, allow_legacy: bool = True, save_after_load: bool = True) -> list[TaskItem]:
        source_path = self.state_path
        if not self._path_has_content(source_path):
            if self._path_has_content(self.backup_state_path):
                source_path = self.backup_state_path
            elif allow_legacy and self._path_has_content(self.legacy_state_path):
                source_path = self.legacy_state_path
            elif allow_legacy and self._path_has_content(self.legacy_backup_state_path):
                source_path = self.legacy_backup_state_path
        if not self._path_has_content(source_path):
            return []
        try:
            self.tasks = self.load_tasks_from_state_path(source_path)
        except json.JSONDecodeError:
            if source_path != self.backup_state_path and self.backup_state_path.exists() and self.backup_state_path.stat().st_size > 0:
                self.tasks = self.load_tasks_from_state_path(self.backup_state_path)
            else:
                raise
        self.normalize_interrupted_states()
        # NOTE: v2 workflow auto-migration was previously done here on the GUI
        # thread, which froze the UI on large batches (every task got 4 node
        # states materialised + a synchronous save_state(force_backup=True)
        # writing the entire state file). Migration is now done lazily inside
        # BatchWorker (worker thread) when v2 actually engages — see
        # ``BatchWorker._run_workflow_engine_run`` / ``auto_migrate_tasks_to_workflow``.
        if save_after_load:
            self.save_state(force_backup=True)
        return self.tasks

    def normalize_interrupted_states(self) -> None:
        for task in self.tasks:
            if task.status in {TaskStatus.CHECKING_PRODUCT_IMAGE, TaskStatus.GENERATING_IMAGE}:
                task.status = TaskStatus.IMAGE_DONE if (task.generated_image_path or task.generated_image_url) else TaskStatus.PENDING
            elif task.status in {TaskStatus.VIDEO_SUBMITTING, TaskStatus.GENERATING_VIDEO}:
                if task.video_task_id and not task.video_url:
                    task.status = TaskStatus.VIDEO_SUBMITTED
                elif task.video_url:
                    task.status = TaskStatus.VIDEO_DOWNLOAD_PENDING if not task.video_file_path else TaskStatus.VIDEO_DOWNLOADED
                else:
                    task.status = TaskStatus.IMAGE_DONE if (task.generated_image_path or task.generated_image_url) else TaskStatus.PENDING
            elif task.status == TaskStatus.VIDEO_POLLING and task.video_task_id and not task.video_url:
                task.status = TaskStatus.VIDEO_SUBMITTED
            elif task.status == TaskStatus.VIDEO_DONE:
                task.status = TaskStatus.VIDEO_DOWNLOAD_PENDING if task.video_url and not task.video_file_path else (TaskStatus.VIDEO_DOWNLOADED if task.video_file_path else TaskStatus.VIDEO_SUBMITTED)
            task.touch()

    def has_state(self) -> bool:
        return any(
            self._path_has_content(path)
            for path in [
                self.state_path,
                self.backup_state_path,
                self.legacy_state_path,
                self.legacy_backup_state_path,
            ]
        )

    @staticmethod
    def _path_has_content(path: Path) -> bool:
        try:
            return path.exists() and path.stat().st_size > 0
        except OSError:
            return False

    def stats(self) -> dict[str, int]:
        total = len(self.tasks)
        completed = sum(1 for t in self.tasks if TaskStatus.is_done(t.status))
        skipped = sum(1 for t in self.tasks if t.status.startswith("SKIPPED"))
        failed = sum(1 for t in self.tasks if t.status.startswith("FAILED") or t.status == TaskStatus.VIDEO_FAILED)
        timeout = sum(1 for t in self.tasks if t.status == TaskStatus.VIDEO_TIMEOUT)
        submitted = sum(1 for t in self.tasks if self._task_has_video_node_status(t, {NODE_STATUS_SUBMITTED}) or t.status == TaskStatus.VIDEO_SUBMITTED)
        polling = sum(1 for t in self.tasks if self._task_has_video_node_status(t, {NODE_STATUS_POLLING}) or t.status == TaskStatus.VIDEO_POLLING)
        image_running = sum(
            1
            for t in self.tasks
            if self._task_has_image_node_status(t, {NODE_STATUS_RUNNING, NODE_STATUS_SUBMITTED, NODE_STATUS_POLLING})
            or t.status == TaskStatus.GENERATING_IMAGE
        )
        active_node_statuses = {NODE_STATUS_RUNNING, NODE_STATUS_SUBMITTED, NODE_STATUS_POLLING}
        running = sum(1 for t in self.tasks if TaskStatus.is_running(t.status) or self._task_has_any_node_status(t, active_node_statuses))
        pending = sum(
            1
            for t in self.tasks
            if t.status == TaskStatus.PENDING and not self._task_has_any_node_status(t, active_node_statuses)
        )
        return {
            "total": total,
            "pending": pending,
            "image_running": image_running,
            "submitted": submitted,
            "polling": polling,
            "completed": completed,
            "failed": failed,
            "timeout": timeout,
            "skipped": skipped,
            "running": running,
        }

    @staticmethod
    def _task_has_node_status(task: TaskItem, node_type: str, statuses: set[str]) -> bool:
        node_states = getattr(task, "node_states", {}) or {}
        if not isinstance(node_states, dict):
            return False
        normalized = {str(value or "") for value in statuses}
        for state in node_states.values():
            if not isinstance(state, dict):
                continue
            if str(state.get("node_type") or "") != node_type:
                continue
            if str(state.get("status") or "") in normalized:
                return True
        return False

    @staticmethod
    def _task_has_any_node_status(task: TaskItem, statuses: set[str]) -> bool:
        node_states = getattr(task, "node_states", {}) or {}
        if not isinstance(node_states, dict):
            return False
        normalized = {str(value or "") for value in statuses}
        return any(
            isinstance(state, dict) and str(state.get("status") or "") in normalized
            for state in node_states.values()
        )

    @classmethod
    def _task_has_image_node_status(cls, task: TaskItem, statuses: set[str]) -> bool:
        return cls._task_has_node_status(task, NODE_TYPE_IMAGE, statuses)

    @classmethod
    def _task_has_video_node_status(cls, task: TaskItem, statuses: set[str]) -> bool:
        return cls._task_has_node_status(task, NODE_TYPE_VIDEO, statuses)

    def export_excel(
        self,
        output_dir: str | Path,
        visible_columns: list[str] | None = None,
        only_visible_columns: bool = False,
        filtered_keys: set[tuple[int, str]] | None = None,
        group_by_fields: list[str] | None = None,
    ) -> str:
        result_dir = Path(output_dir)
        result_dir.mkdir(parents=True, exist_ok=True)
        batch_id = next((task.batch_id for task in self.tasks if task.batch_id), "")
        now_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"{batch_id}_{now_suffix}" if batch_id else datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = result_dir / f"Veo3视频生成结果_{suffix}.xlsx"
        rows = []
        filtered_keys = filtered_keys or set()
        group_text = " / ".join(group_by_fields or [])
        for task in self.tasks:
            node_states = getattr(task, "node_states", {}) or {}
            # Extract per-stage info for the v2 workflow nodes.
            stage_info = {}
            for node_id, prompt_attr in [
                ("image_stage_1", "prompt_stage_1"),
                ("video_stage_1", "prompt_stage_2"),
                ("image_stage_2", "prompt_stage_3"),
                ("video_stage_2", "prompt_stage_4"),
            ]:
                state = node_states.get(node_id) if isinstance(node_states, dict) else None
                state = state if isinstance(state, dict) else {}
                stage_info[node_id] = {
                    "status": state.get("status", ""),
                    "error": state.get("error_message", ""),
                    "image_path": state.get("output_image_path", ""),
                    "image_url": state.get("output_image_url", ""),
                    "video_url": state.get("output_video_url", ""),
                    "video_local_path": state.get("output_video_local_path", ""),
                    "task_id": state.get("task_id", ""),
                    "poll_count": state.get("poll_count", 0) or 0,
                }
            rows.append(
                {
                    "任务名称": getattr(task, "task_name", "") or "",
                    "PID": task.pid,
                    "任务UID": task.task_uid or self.task_uid_for(task),
                    "负责人": task.owner,
                    "网盘路径": task.netdisk_path,
                    "原始网盘路径": task.netdisk_original_path,
                    "Http网盘路径": task.netdisk_http_path,
                    "产品白底图路径": task.product_image_path,
                    "产品白底图URL": task.product_image_url,
                    "指定白底图文件名": getattr(task, "product_image_filename", "") or "",
                    "产品图来源类型": task.product_image_source_type,
                    "图片提示词": task.image_prompt,
                    "视频提示词": task.video_prompt,
                    "提示词【阶段1】": getattr(task, "prompt_stage_1", "") or "",
                    "提示词【阶段2】": getattr(task, "prompt_stage_2", "") or "",
                    "提示词【阶段3】": getattr(task, "prompt_stage_3", "") or "",
                    "提示词【阶段4】": getattr(task, "prompt_stage_4", "") or "",
                    "工作流版本": getattr(task, "workflow_version", "") or "",
                    "旧字段兼容模式": "是" if getattr(task, "legacy_prompt_compat_mode", False) else "否",
                    "阶段1状态": stage_info["image_stage_1"]["status"],
                    "阶段1输出图片": stage_info["image_stage_1"]["image_path"] or stage_info["image_stage_1"]["image_url"],
                    "阶段1错误信息": stage_info["image_stage_1"]["error"],
                    "阶段2状态": stage_info["video_stage_1"]["status"],
                    "阶段2视频task_id": stage_info["video_stage_1"]["task_id"],
                    "阶段2视频链接": stage_info["video_stage_1"]["video_url"],
                    "阶段2视频本地路径": stage_info["video_stage_1"]["video_local_path"],
                    "阶段2错误信息": stage_info["video_stage_1"]["error"],
                    "阶段3状态": stage_info["image_stage_2"]["status"],
                    "阶段3输出图片": stage_info["image_stage_2"]["image_path"] or stage_info["image_stage_2"]["image_url"],
                    "阶段3错误信息": stage_info["image_stage_2"]["error"],
                    "阶段4状态": stage_info["video_stage_2"]["status"],
                    "阶段4视频task_id": stage_info["video_stage_2"]["task_id"],
                    "阶段4视频链接": stage_info["video_stage_2"]["video_url"],
                    "阶段4视频本地路径": stage_info["video_stage_2"]["video_local_path"],
                    "阶段4错误信息": stage_info["video_stage_2"]["error"],
                    "图生图平台": task.image_provider,
                    "图生图模型": task.image_model_display or task.image_model_logical_key,
                    "图生图task_id": task.image_task_id,
                    "生成图片路径": task.generated_image_path,
                    "生成图片URL": task.generated_image_url,
                    "图生视频平台": task.video_provider,
                    "图生视频模型": task.video_model_display or task.video_model_logical_key,
                    "video_task_id": task.video_task_id,
                    "视频轮询次数": task.video_poll_count,
                    "视频下载状态": task.video_download_status,
                    "视频下载尝试次数": task.video_download_attempt_count,
                    "最后视频下载时间": task.last_video_download_time,
                    "最后视频下载错误": task.last_video_download_error,
                    "手动轮询次数": task.manual_poll_count,
                    "最后手动轮询时间": task.last_manual_poll_time,
                    "最后手动轮询结果": task.last_manual_poll_result,
                    "自动重试次数": task.auto_retry_count,
                    "最后自动重试时间": task.last_auto_retry_time,
                    "最后自动重试阶段": task.last_auto_retry_stage,
                    "最后自动重试原因": task.last_auto_retry_reason,
                    "视频链接": task.video_url,
                    "视频本地路径": task.video_file_path,
                    "任务状态": task.status,
                    "错误信息": task.error_message,
                    "任务添加日期": task.task_added_date,
                    "批次ID": task.batch_id,
                    "批次名称": task.batch_name,
                    "导入时间": task.imported_at,
                    "原始 Excel 路径": task.source_excel_path,
                    "开始时间": task.started_at,
                    "结束时间": task.ended_at,
                    "耗时秒数": task.elapsed_seconds,
                    "分组字段": group_text,
                    "是否命中当前筛选": "是" if (task.row_index, task.pid) in filtered_keys else "否",
                }
            )
        for row, task in zip(rows, self.tasks, strict=False):
            row["任务日志文件路径"] = str(self.state_path.parent / "task_logs" / f"{task.task_uid or self.task_uid_for(task)}.log")
            row["任务错误日志摘要"] = task_error_log_summary(task)
        df = pd.DataFrame(rows)
        if only_visible_columns and visible_columns:
            keep = [col for col in visible_columns if col in df.columns]
            for extra in ["批次ID", "批次名称", "导入时间", "任务添加日期", "原始 Excel 路径", "原始网盘路径", "Http网盘路径", "产品白底图URL", "产品图来源类型", "分组字段", "是否命中当前筛选", "负责人", "视频下载状态", "视频下载尝试次数", "最后视频下载时间", "最后视频下载错误", "手动轮询次数", "最后手动轮询时间", "最后手动轮询结果"]:
                if extra in df.columns and extra not in keep:
                    keep.append(extra)
            df = df[keep]
        df.to_excel(path, index=False)
        return str(path)
