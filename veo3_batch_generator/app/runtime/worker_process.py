from __future__ import annotations

import argparse
import copy
import os
import json
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

from PySide6.QtCore import Qt

from app.config import reconcile_api_key_with_provider_profile
from app.runtime.worker_events import append_event, append_events, read_events


class BufferedEventWriter:
    def __init__(
        self,
        path: Path,
        *,
        flush_interval: float = 0.2,
        max_batch: int = 200,
        max_file_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        self.path = Path(path)
        self.flush_interval = max(0.05, float(flush_interval))
        self.max_batch = max(1, int(max_batch))
        self.max_file_bytes = max(1, int(max_file_bytes or 128 * 1024 * 1024))
        self._queue: queue.Queue[dict] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def emit(self, event_type: str, payload: dict | None = None) -> None:
        event = dict(payload or {})
        event["type"] = event_type
        event.setdefault("created_at", time.time())
        self._queue.put(event)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._flush_all()

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            batch: list[dict] = []
            deadline = time.monotonic() + self.flush_interval
            while len(batch) < self.max_batch:
                timeout = max(0.0, deadline - time.monotonic())
                try:
                    event = self._queue.get(timeout=timeout)
                except queue.Empty:
                    break
                batch.append(event)
                if time.monotonic() >= deadline:
                    break
            if batch:
                self._append_batch(batch)

    def _flush_all(self) -> None:
        batch: list[dict] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
                if len(batch) >= self.max_batch:
                    append_events(self.path, batch)
                    batch = []
            except queue.Empty:
                break
        if batch:
            self._append_batch(batch)

    def _append_batch(self, batch: list[dict]) -> None:
        self._rotate_if_needed()
        append_events(self.path, batch)

    def _rotate_if_needed(self) -> None:
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_file_bytes:
                return
            rotated = self.path.with_name(self.path.name + ".1")
            try:
                if rotated.exists():
                    rotated.unlink()
                self.path.replace(rotated)
            except OSError:
                self.path.write_text("", encoding="utf-8")
        except OSError:
            return


def config_from_batch(base_config, batch):
    cfg = copy.copy(base_config)
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
        "file_io_concurrency",
        "max_inflight_video_tasks",
        "max_inflight_image_tasks",
        "auto_stop_on_consecutive_failures",
        "max_consecutive_failures_before_pause",
        "pause_on_auth_error",
        "pause_on_rate_limit",
        "retry_base_delay_seconds",
        "retry_max_delay_seconds",
        "rate_limit_backoff_seconds",
        "enable_stage_based_workflow",
        "workflow_engine_version",
        "enable_manual_poll_button",
        "manual_poll_ignore_max_count",
        "manual_poll_include_timeout_tasks",
        "manual_poll_include_failed_tasks",
    ]:
        if key in snapshot and snapshot[key] not in (None, ""):
            setattr(cfg, key, snapshot[key])
    reconcile_api_key_with_provider_profile(cfg, "image", force_profile=True)
    reconcile_api_key_with_provider_profile(cfg, "video", force_profile=True)
    for key in ["video_download_root", "image_assets_root", "software_log_root"]:
        if snapshot.get(key):
            setattr(cfg, key, Path(str(snapshot[key])))
    return cfg


def load_manager_for_batch(config, batch_manager, batch):
    from app.file_utils import convert_netdisk_path_to_http
    from app.task_manager import TaskManager

    manager = TaskManager(batch_manager.task_state_path(batch.batch_id))
    manager.configure_batch_context(batch.batch_id, batch.batch_name, batch.imported_at, batch.source_excel_path)
    manager.configure_defaults(
        config.image_provider,
        config.image_model_logical_key,
        config.video_provider,
        config.video_model_logical_key,
    )
    tasks = manager.load_state(allow_legacy=False, save_after_load=False)
    for task in tasks:
        if task.batch_id != batch.batch_id:
            task.batch_id = batch.batch_id
            task.batch_name = batch.batch_name
            task.imported_at = task.imported_at or batch.imported_at
            task.source_excel_path = task.source_excel_path or batch.source_excel_path
            task.task_uid = TaskManager.task_uid_for(task)
        if not task.netdisk_http_path:
            task.netdisk_original_path = task.netdisk_original_path or task.netdisk_path
            task.netdisk_http_path = convert_netdisk_path_to_http(task.netdisk_original_path, config)
    return manager


def compact_task(manager, task) -> dict:
    try:
        return manager._compact_task_dump(task)
    except Exception:
        data = task.model_dump()
        data["logs"] = list(data.get("logs") or [])[-20:]
        return data


def _compact_node_states_for_event(node_states) -> dict:
    if not isinstance(node_states, dict):
        return {}
    keep = {
        "node_id",
        "node_name",
        "node_type",
        "status",
        "input_images",
        "output_image_path",
        "output_image_url",
        "output_video_url",
        "output_video_local_path",
        "task_id",
        "poll_count",
        "manual_poll_count",
        "error_message",
        "started_at",
        "ended_at",
        "download_status",
        "download_attempt_count",
        "last_download_error",
        "auto_retrying",
        "auto_retry_count",
        "last_auto_retry_time",
        "last_auto_retry_reason",
    }
    compact: dict[str, dict] = {}
    for node_id, state in node_states.items():
        if not isinstance(state, dict):
            continue
        node_patch = {key: state.get(key) for key in keep if key in state}
        for list_key in ["input_images"]:
            value = node_patch.get(list_key)
            if isinstance(value, list):
                node_patch[list_key] = [
                    f"<omitted data url length={len(str(item))}>"
                    if str(item).startswith("data:image") or len(str(item)) > 1000
                    else str(item)
                    for item in value[:10]
                ]
        compact[str(node_id)] = node_patch
    return compact


def compact_task_event(task) -> dict:
    """Small task patch for UI refresh events.

    Full task snapshots can contain long prompts and provider raw responses.
    Sending those through the UI event file made large batches produce GB-size
    jsonl files and froze the frontend while it parsed them.
    """

    data = task.model_dump()
    keep = [
        "task_uid",
        "row_index",
        "task_name",
        "pid",
        "batch_id",
        "status",
        "image_status",
        "video_status",
        "product_image_path",
        "product_image_url",
        "generated_image_path",
        "generated_image_url",
        "image_task_id",
        "image_provider",
        "image_model_logical_key",
        "image_model_display",
        "video_task_id",
        "video_provider",
        "video_model_logical_key",
        "video_model_display",
        "video_url",
        "video_file_path",
        "video_poll_count",
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
        "error_message",
        "started_at",
        "ended_at",
        "elapsed_seconds",
        "updated_at",
    ]
    patch = {key: data.get(key) for key in keep if key in data}
    patch["node_states"] = _compact_node_states_for_event(data.get("node_states"))
    return patch


def watch_commands(worker, commands_path: Path, stop_event: threading.Event) -> None:
    offset = 0
    while not stop_event.is_set():
        events, offset = read_events(commands_path, offset)
        for event in events:
            command = str(event.get("command") or event.get("type") or "").lower()
            if command == "pause":
                worker.pause()
            elif command == "resume":
                worker.resume()
            elif command == "stop":
                worker.stop()
            elif command in {"poll", "poll_now", "manual_poll"}:
                worker.request_poll_cycle()
            elif command in {"retry_failed", "retry_failed_tasks"}:
                worker.request_retry_failed(event.get("selected_task_keys") or event.get("task_keys"))
        time.sleep(0.25)


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if sys.platform.startswith("win"):
        try:
            import ctypes

            synchronize = 0x00100000
            wait_timeout = 0x00000102
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(synchronize, False, int(pid))
            if not handle:
                return False
            try:
                return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def watch_parent_process(
    worker,
    parent_pid: int,
    stop_event: threading.Event,
    event_writer: BufferedEventWriter,
    *,
    grace_seconds: float = 20.0,
) -> None:
    if int(parent_pid or 0) <= 0:
        return
    while not stop_event.is_set():
        if _process_is_alive(parent_pid):
            time.sleep(2.0)
            continue
        event_writer.emit(
            "log",
            {
                "level": "WARNING",
                "message": f"GUI 父进程 {parent_pid} 已退出，后台执行进程将自动停止，避免残留进程继续占用资源",
                "task_uid": "",
            },
        )
        worker.stop()
        deadline = time.monotonic() + max(1.0, float(grace_seconds))
        while time.monotonic() < deadline and not stop_event.is_set():
            time.sleep(0.5)
        if not stop_event.is_set():
            event_writer.emit(
                "error",
                {
                    "message": f"GUI 父进程 {parent_pid} 已退出，后台进程未能在宽限期内停止，正在强制退出",
                },
            )
            event_writer.emit("process_done", {"return_code": 3})
            time.sleep(0.5)
            os._exit(3)
        return


def run_worker(args: argparse.Namespace) -> int:
    events_path = Path(args.events_path)
    commands_path = Path(args.commands_path)
    event_writer = BufferedEventWriter(events_path)
    event_writer.start()
    event_writer.emit("started", {"batch_id": args.batch_id})
    stop_commands = threading.Event()
    try:
        from app.batch_manager import BatchManager
        from app.config import load_config
        from app.worker import BatchWorker

        config = load_config()
        batch_manager = BatchManager(config.software_log_root, config.batch_root_dir_name)
        batch = batch_manager.load_batch(args.batch_id)
        if not batch:
            append_event(events_path, "error", {"message": f"批次不存在: {args.batch_id}", "batch_id": args.batch_id})
            return 2

        runtime_config = config_from_batch(config, batch)
        manager = load_manager_for_batch(runtime_config, batch_manager, batch)
        selected_task_keys = set(json.loads(args.selected_task_keys_json or "[]"))
        workflow_definition = batch_manager.resolve_batch_workflow(batch.batch_id)

        worker = BatchWorker(
            manager,
            runtime_config,
            None,
            failed_only=bool(args.failed_only),
            regenerate_existing_images=runtime_config.regenerate_existing_images,
            poll_only=bool(args.poll_only),
            execution_mode=args.mode or "full",
            selected_task_keys=selected_task_keys,
            workflow_definition=workflow_definition,
            log_dir=batch_manager.batch_dir(batch.batch_id),
            log_path=batch_manager.log_path(batch.batch_id),
        )

        worker.log_message.connect(
            lambda level, message, task=None: event_writer.emit(
                "log",
                {
                    "batch_id": batch.batch_id,
                    "level": level,
                    "message": message,
                    "task_uid": getattr(task, "task_uid", "") if task is not None else "",
                },
            ),
            type=Qt.ConnectionType.DirectConnection,
        )
        worker.stats_updated.connect(
            lambda stats: event_writer.emit("stats", {"batch_id": batch.batch_id, "stats": dict(stats or {})}),
            type=Qt.ConnectionType.DirectConnection,
        )
        worker.task_updated.connect(
            lambda task: event_writer.emit(
                "task_updated",
                {"batch_id": batch.batch_id, "task": compact_task_event(task)},
            ),
            type=Qt.ConnectionType.DirectConnection,
        )
        worker.finished_message.connect(
            lambda message: event_writer.emit("finished", {"batch_id": batch.batch_id, "message": message}),
            type=Qt.ConnectionType.DirectConnection,
        )

        command_thread = threading.Thread(target=watch_commands, args=(worker, commands_path, stop_commands), daemon=True)
        command_thread.start()
        parent_thread = threading.Thread(
            target=watch_parent_process,
            args=(worker, int(args.parent_pid or 0), stop_commands, event_writer),
            daemon=True,
        )
        parent_thread.start()
        worker.run()
        stop_commands.set()
        try:
            batch_manager.update_batch(batch.batch_id, manager.tasks)
        except OSError as exc:
            event_writer.emit(
                "log",
                {
                    "batch_id": batch.batch_id,
                    "level": "WARNING",
                    "message": f"批次统计暂时无法写入网络盘，执行结果已保留在任务状态中：{exc}",
                    "task_uid": "",
                },
            )
        event_writer.emit("process_done", {"batch_id": batch.batch_id, "return_code": 0})
        return 0
    except Exception as exc:
        event_writer.emit("error", {"batch_id": args.batch_id, "message": str(exc), "traceback": traceback.format_exc()})
        event_writer.emit("process_done", {"batch_id": args.batch_id, "return_code": 1})
        return 1
    finally:
        stop_commands.set()
        event_writer.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Veo3 batch execution outside the UI process.")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--mode", default="full")
    parser.add_argument("--failed-only", action="store_true")
    parser.add_argument("--poll-only", action="store_true")
    parser.add_argument("--selected-task-keys-json", default="[]")
    parser.add_argument("--events-path", required=True)
    parser.add_argument("--commands-path", required=True)
    parser.add_argument("--parent-pid", type=int, default=0)
    return parser


def main() -> int:
    return run_worker(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
