from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.models.task import TaskItem, TaskLogEntry, now_text


MAX_TASK_LOG_ENTRIES = 500


def _safe_name(value: str) -> str:
    text = str(value or "").strip() or "task"
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)[:160]


def _infer_category(message: str, category: str | None = None) -> str:
    if category:
        return category.upper()
    text = str(message or "").lower()
    if "image" in text or "图生图" in text or "图片" in text:
        return "IMAGE"
    if "video" in text or "视频" in text:
        return "VIDEO"
    if "poll" in text or "轮询" in text:
        return "POLL"
    if "download" in text or "下载" in text or "saved" in text:
        return "FILE"
    if "api" in text or "http" in text:
        return "API"
    if "retry" in text or "重试" in text:
        return "NODE"
    return "TASK"


def _infer_node_id(message: str, node_id: str | None = None) -> str | None:
    if node_id:
        return node_id
    match = re.match(r"\[([A-Za-z0-9_]+)\]", str(message or "").strip())
    if match:
        value = match.group(1)
        if value in {"INFO", "ERROR", "WARNING", "DEBUG", "SUCCESS", "PERF", "DOWNLOAD", "AUTO_RETRY"}:
            return None
        return value
    return None


def append_task_log(
    task: TaskItem,
    level: str,
    category: str | None,
    message: str,
    detail: str | None = None,
    node_id: str | None = None,
) -> None:
    """Append a structured task log without letting logging failures break work."""

    if task is None:
        return
    try:
        entry = TaskLogEntry(
            time=now_text(),
            level=str(level or "INFO").upper(),
            category=_infer_category(message, category),
            node_id=_infer_node_id(message, node_id),
            message=str(message or ""),
            detail=str(detail) if detail is not None else None,
        )
        task.task_logs.append(entry)
        if len(task.task_logs) > MAX_TASK_LOG_ENTRIES:
            del task.task_logs[:-MAX_TASK_LOG_ENTRIES]

        legacy_line = f"[{entry.time}] [{entry.level}] [{entry.category}]"
        if entry.node_id:
            legacy_line += f" [{entry.node_id}]"
        legacy_line += f" {entry.message}"
        if entry.detail:
            legacy_line += f" | {entry.detail}"
        task.logs.append(legacy_line)
        if len(task.logs) > 50:
            del task.logs[:-50]
        task.touch()
    except Exception:
        pass


def task_logs_to_text(task: TaskItem) -> str:
    logs = list(getattr(task, "task_logs", []) or [])
    if not logs:
        legacy = list(getattr(task, "logs", []) or [])
        return "\n".join(str(line) for line in legacy)
    lines: list[str] = []
    for entry in logs:
        node = f" [{entry.node_id}]" if entry.node_id else ""
        detail = f"\n  详情: {entry.detail}" if entry.detail else ""
        lines.append(f"[{entry.time}] [{entry.level}] [{entry.category}]{node} {entry.message}{detail}")
    return "\n".join(lines)


def task_logs_to_jsonable(task: TaskItem) -> list[dict[str, Any]]:
    logs = list(getattr(task, "task_logs", []) or [])
    return [entry.model_dump(mode="json") if hasattr(entry, "model_dump") else dict(entry) for entry in logs]


def task_error_log_summary(task: TaskItem, limit: int = 500) -> str:
    errors = [
        f"[{entry.time}] {entry.message}" + (f" | {entry.detail}" if entry.detail else "")
        for entry in list(getattr(task, "task_logs", []) or [])
        if str(entry.level).upper() in {"ERROR", "WARNING"}
    ]
    if not errors and task.error_message:
        errors = [str(task.error_message)]
    text = "\n".join(errors[-5:])
    return text[:limit]


def task_log_file_stem(task: TaskItem) -> str:
    uid = getattr(task, "task_uid", "") or f"{getattr(task, 'batch_id', '')}_{getattr(task, 'pid', '')}_row_{getattr(task, 'row_index', '')}"
    return _safe_name(uid)


def export_task_logs(task: TaskItem, batch_dir: Path) -> dict[str, Path]:
    log_dir = Path(batch_dir) / "task_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stem = task_log_file_stem(task)
    txt_path = log_dir / f"{stem}.log"
    json_path = log_dir / f"{stem}.json"
    txt_path.write_text(task_logs_to_text(task) or "这条任务暂时还没有详细日志。", encoding="utf-8")
    payload = {
        "task": {
            "task_uid": task.task_uid or task_log_file_stem(task),
            "task_name": task.task_name,
            "pid": task.pid,
            "owner": task.owner,
            "row_index": task.row_index,
            "batch_id": task.batch_id,
            "batch_name": task.batch_name,
            "status": task.status,
            "error_message": task.error_message,
        },
        "logs": task_logs_to_jsonable(task),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"txt": txt_path, "json": json_path}
