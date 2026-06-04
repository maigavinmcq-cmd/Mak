from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from light_image_tool.config import PROJECT_ROOT
from light_image_tool.models import (
    LightImageQueueState,
    LightImageTask,
    LightImageTaskLogEntry,
    LightImageTaskStatus,
    PIDScanResult,
)
from light_image_tool.queue_manager import LightImageQueueManager


SESSION_PATH = PROJECT_ROOT / "config" / "light_image_tool_session.json"


def save_light_image_tool_session(
    path: str | Path = SESSION_PATH,
    *,
    queue_manager: LightImageQueueManager,
    scan_results: list[PIDScanResult] | None = None,
    selected_task_uid: str = "",
    ui_state: dict[str, Any] | None = None,
) -> Path:
    session_path = Path(path)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    queue_manager.refresh_state()
    payload = {
        "session_version": "light_image_tool_session_v1",
        "queue_state": asdict(queue_manager.state),
        "tasks": [asdict(task) for task in queue_manager.tasks],
        "scan_results": [asdict(item) for item in (scan_results or [])],
        "selected_task_uid": selected_task_uid,
        "ui_state": ui_state or {},
    }
    session_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return session_path


def load_light_image_tool_session(path: str | Path = SESSION_PATH) -> dict[str, Any]:
    session_path = Path(path)
    if not session_path.exists() or session_path.stat().st_size <= 0:
        return {}
    try:
        loaded = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def restore_session_into_queue(queue_manager: LightImageQueueManager, session: dict[str, Any]) -> list[PIDScanResult]:
    tasks = [_task_from_dict(item) for item in _as_dict_list(session.get("tasks"))]
    state = _queue_state_from_dict(session.get("queue_state"))
    scan_results = [_scan_result_from_dict(item) for item in _as_dict_list(session.get("scan_results"))]
    with queue_manager._lock:  # noqa: SLF001 - restore is part of queue persistence boundary.
        queue_manager.tasks = tasks
        if state is not None:
            queue_manager.state = state
        queue_manager.refresh_state()
    return scan_results


def _task_from_dict(data: dict[str, Any]) -> LightImageTask:
    allowed = {field.name for field in fields(LightImageTask)}
    payload = {key: value for key, value in data.items() if key in allowed and key != "logs"}
    status = str(payload.get("status") or "")
    if status in LightImageTaskStatus.ACTIVE:
        payload["status"] = LightImageTaskStatus.STOPPED
        payload["skip_reason"] = payload.get("skip_reason") or "上次关闭时任务未完成"
    payload["logs"] = [_log_entry_from_dict(item) for item in _as_dict_list(data.get("logs"))]
    return LightImageTask(**payload)


def _log_entry_from_dict(data: dict[str, Any]) -> LightImageTaskLogEntry:
    allowed = {field.name for field in fields(LightImageTaskLogEntry)}
    payload = {key: value for key, value in data.items() if key in allowed}
    defaults = {"time": "", "level": "INFO", "category": "session", "task_uid": None, "message": "", "detail": None}
    defaults.update(payload)
    return LightImageTaskLogEntry(**defaults)


def _scan_result_from_dict(data: dict[str, Any]) -> PIDScanResult:
    allowed = {field.name for field in fields(PIDScanResult)}
    payload = {key: value for key, value in data.items() if key in allowed}
    defaults = {
        "pid": "",
        "status": "not_found",
        "pid_dir": None,
        "product_info_dir": None,
        "output_dir": None,
        "selected_images": [],
        "error_message": None,
        "candidate_dirs": [],
        "missing_image_names": [],
    }
    defaults.update(payload)
    return PIDScanResult(**defaults)


def _queue_state_from_dict(data: Any) -> LightImageQueueState | None:
    if not isinstance(data, dict):
        return None
    allowed = {field.name for field in fields(LightImageQueueState)}
    payload = {key: value for key, value in data.items() if key in allowed}
    try:
        default_state = LightImageQueueState.empty()
        defaults = asdict(default_state)
        defaults.update(payload)
        return LightImageQueueState(**defaults)
    except Exception:
        return None


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
