# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
import json
import math
import re

from .settings import (
    TASKS_STORE_FILE,
    TASKS_STORE_MAX,
    TASKS_STORE_DIR,
    TASKS_STORE_SHARD_SIZE,
    TASK_HISTORY_FILE,
    TASK_HISTORY_DIR,
)
from .utils import safe_json_loads, now_str
from .models import TaskItem


def _normalize_history_item(item: dict) -> dict:
    return {
        "task_id": item.get("task_id", "") or "",
        "history_remote_id": item.get("history_remote_id", item.get("remote_id", "")) or "",
        "batch_name": item.get("batch_name", "") or "",
        "provider": item.get("provider", "") or "",
        "base_url": item.get("base_url", "") or "",
        "model": item.get("model", "") or "",
        "prompt": item.get("prompt", "") or "",
        "image_path": item.get("image_path", "") or "",
        "note": item.get("note", "") or "",
        "group": item.get("group", "") or "",
        "status": item.get("status", "") or "",
        "status_msg": item.get("status_msg", "") or "",
        "progress": float(item.get("progress", 0.0) or 0.0),
        "remote_id": item.get("remote_id", "") or "",
        "video_url": item.get("video_url", "") or "",
        "completed_at": item.get("completed_at", "") or "",
        "last_error": item.get("last_error", "") or "",
        "log_file": item.get("log_file", "") or "",
        "log_text": item.get("log_text", "") or "",
        "added_at": item.get("added_at", "") or "",
        "deleted_at": item.get("deleted_at", "") or "",
    }


def _history_record_summary(rec: dict) -> tuple[int, int, str]:
    items = rec.get("items", [])
    if not isinstance(items, list):
        return 0, 0, ""
    total = len(items)
    deleted = 0
    deleted_vals: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        deleted_at = (item.get("deleted_at") or "").strip()
        if deleted_at:
            deleted += 1
            deleted_vals.append(deleted_at)
    return total, deleted, (max(deleted_vals) if deleted_vals else "")


def _normalize_history_record(rec: dict, *, keep_items: bool = True) -> dict:
    items = rec.get("items", [])
    if not isinstance(items, list):
        items = []
    norm_items = [_normalize_history_item(x) for x in items if isinstance(x, dict)]
    batch_id = (rec.get("batch_id") or rec.get("history_id") or "LEGACY").strip() or "LEGACY"
    added_at = (rec.get("added_at") or "").strip()
    if not added_at:
        added_at = min((x.get("added_at", "") for x in norm_items if x.get("added_at", "")), default="")
    batch_name = (rec.get("batch_name", "") or "").strip()
    if not batch_name:
        batch_name = next((x.get("batch_name", "") for x in norm_items if x.get("batch_name", "")), "")
    total, deleted, last_deleted_at = _history_record_summary({"items": norm_items})
    out = {
        "history_id": batch_id,
        "batch_id": batch_id,
        "batch_name": batch_name,
        "source": (rec.get("source") or "未知来源").strip() or "未知来源",
        "added_at": added_at,
        "total": int(rec.get("total", total) or total),
        "deleted": int(rec.get("deleted", deleted) or deleted),
        "last_deleted_at": (rec.get("last_deleted_at") or last_deleted_at).strip(),
    }
    if keep_items:
        out["items"] = norm_items
    return out


def _json_dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _atomic_write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_json_dump(payload), encoding="utf-8")
    tmp.replace(path)


def _safe_slug(value: str, fallback: str) -> str:
    s = (value or "").strip()
    s = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", s)
    s = re.sub(r"_+", "_", s).strip(" ._")
    return s[:80] or fallback


def _tasks_store_dir() -> Path:
    return Path(TASKS_STORE_DIR)


def _task_history_dir() -> Path:
    return Path(TASK_HISTORY_DIR)


def _task_history_index_file() -> Path:
    return _task_history_dir() / "index.json"


def _task_history_record_file(batch_id: str) -> Path:
    safe_id = _safe_slug(batch_id, "LEGACY")
    return _task_history_dir() / f"{safe_id}.json"


def _read_json(path: Path) -> dict:
    return safe_json_loads(path.read_text(encoding="utf-8")) or {}


def _load_tasks_from_shards(log_cb) -> tuple[dict[str, TaskItem], int]:
    store_dir = _tasks_store_dir()
    tasks: dict[str, TaskItem] = {}
    restored = 0
    max_id_num = 0
    shard_files = sorted(store_dir.glob("active_*.json"))
    if not shard_files:
        return {}, 1
    for shard_path in shard_files:
        try:
            obj = _read_json(shard_path)
            items = obj.get("tasks", [])
            if not isinstance(items, list):
                continue
            for d in items:
                try:
                    t = TaskItem.from_persist_dict(d)
                    if not t.task_id:
                        continue
                    m = re.match(r"T(\d+)", t.task_id)
                    if m:
                        max_id_num = max(max_id_num, int(m.group(1)))
                    if t.status == "Running":
                        t.status = "Pending(Check)" if (t.remote_id or "").strip() else "Queued"
                    tasks[t.task_id] = t
                    restored += 1
                except Exception:
                    continue
        except Exception as e:
            log_cb(f"⚠️ 读取任务分片失败：{shard_path.name} | {e}\n")
    if restored:
        log_cb(f"✅ 已恢复任务：{restored} 条（来自 {store_dir}）\n")
    return tasks, max_id_num + 1


def load_tasks_store(log_cb) -> tuple[dict[str, TaskItem], int]:
    """
    Returns: (tasks_by_id, next_counter_start)
    """
    store_dir = _tasks_store_dir()
    if store_dir.exists():
        tasks, next_counter = _load_tasks_from_shards(log_cb)
        if tasks:
            return tasks, next_counter
        shard_files = list(store_dir.glob("active_*.json"))
        if shard_files or (store_dir / "index.json").exists():
            log_cb(f"已恢复任务：0（来源 {store_dir}）\n")
            return {}, 1

    p = Path(TASKS_STORE_FILE)
    if not p.exists():
        log_cb("ℹ️ 未发现任务存储文件（首次使用正常）。\n")
        return {}, 1

    tasks: dict[str, TaskItem] = {}
    restored = 0
    max_id_num = 0
    try:
        obj = _read_json(p)
        items = obj.get("tasks", [])
        if not isinstance(items, list):
            items = []
        for d in items:
            try:
                t = TaskItem.from_persist_dict(d)
                if not t.task_id:
                    continue
                m = re.match(r"T(\d+)", t.task_id)
                if m:
                    max_id_num = max(max_id_num, int(m.group(1)))
                if t.status == "Running":
                    t.status = "Pending(Check)" if (t.remote_id or "").strip() else "Queued"
                tasks[t.task_id] = t
                restored += 1
            except Exception:
                continue
        log_cb(f"✅ 已恢复任务：{restored} 条（来自 {TASKS_STORE_FILE}）\n")
        return tasks, max_id_num + 1
    except Exception as e:
        log_cb(f"⚠️ 读取任务存储失败：{e}\n")
        return {}, 1


def save_tasks_store(tasks: dict[str, TaskItem]):
    save_tasks_store_items([t.to_persist_dict() for t in tasks.values()])


def save_tasks_store_items(tasks_list: list[dict]):
    try:
        items = list(tasks_list or [])
        if len(items) > TASKS_STORE_MAX:
            items = items[-TASKS_STORE_MAX:]

        store_dir = _tasks_store_dir()
        store_dir.mkdir(parents=True, exist_ok=True)
        shard_size = max(100, int(TASKS_STORE_SHARD_SIZE or 400))
        shard_count = max(1, int(math.ceil(len(items) / float(shard_size)))) if items else 1

        manifest = {
            "version": 5,
            "saved_at": now_str(),
            "shard_size": shard_size,
            "task_count": len(items),
            "shards": [],
        }

        expected_names: set[str] = set()
        for shard_idx in range(shard_count):
            start = shard_idx * shard_size
            end = start + shard_size
            shard_items = items[start:end]
            shard_name = f"active_{shard_idx + 1:03d}.json"
            shard_path = store_dir / shard_name
            expected_names.add(shard_name)
            payload = {
                "version": 5,
                "saved_at": now_str(),
                "shard_index": shard_idx + 1,
                "tasks": shard_items,
            }
            _atomic_write_json(shard_path, payload)
            manifest["shards"].append({"file": shard_name, "count": len(shard_items)})

        for old in store_dir.glob("active_*.json"):
            if old.name not in expected_names:
                try:
                    old.unlink()
                except Exception:
                    pass

        _atomic_write_json(store_dir / "index.json", manifest)
        _atomic_write_json(Path(TASKS_STORE_FILE), {
            "version": 5,
            "saved_at": now_str(),
            "tasks": items,
        })
    except Exception:
        pass


def export_tasks_to(path: str, tasks: dict[str, TaskItem]):
    tasks_list = [t.to_persist_dict() for t in tasks.values()]
    payload = {"version": 4, "exported_at": now_str(), "tasks": tasks_list}
    Path(path).write_text(_json_dump(payload), encoding="utf-8")


def import_tasks_from(path: str) -> list[TaskItem]:
    obj = safe_json_loads(Path(path).read_text(encoding="utf-8")) or {}
    items = obj.get("tasks", [])
    if not isinstance(items, list):
        return []
    return [TaskItem.from_persist_dict(d) for d in items if isinstance(d, dict)]


def load_history_record_items(rec: dict) -> dict:
    if not isinstance(rec, dict):
        return rec
    items = rec.get("items", None)
    if isinstance(items, list):
        return rec
    record_file = (rec.get("_history_file") or "").strip()
    if not record_file:
        return rec
    try:
        raw = _read_json(Path(record_file))
        loaded = _normalize_history_record(raw, keep_items=True)
        loaded["_history_file"] = record_file
        return loaded
    except Exception:
        return rec


def load_task_history(log_cb) -> list[dict]:
    history_dir = _task_history_dir()
    index_file = _task_history_index_file()
    if index_file.exists():
        try:
            obj = _read_json(index_file)
            records = obj.get("records", [])
            if not isinstance(records, list):
                records = []
            out: list[dict] = []
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                item = _normalize_history_record(rec, keep_items=False)
                item["_history_file"] = rec.get("_history_file") or str(_task_history_record_file(item["batch_id"]))
                out.append(item)
            log_cb(f"已恢复历史任务记录：{len(out)} 批（来自 {history_dir}）\n")
            return out
        except Exception as e:
            log_cb(f"读取历史任务索引失败：{e}\n")

    p = Path(TASK_HISTORY_FILE)
    if not p.exists():
        return []
    try:
        obj = _read_json(p)
        records = obj.get("records", [])
        if not isinstance(records, list):
            records = []
        out = [_normalize_history_record(r) for r in records if isinstance(r, dict)]
        log_cb(f"已恢复历史任务记录：{len(out)} 批（来源 {TASK_HISTORY_FILE}）\n")
        return out
    except Exception as e:
        log_cb(f"读取历史任务记录失败：{e}\n")
        return []


def save_task_history(records: list[dict]):
    try:
        history_dir = _task_history_dir()
        history_dir.mkdir(parents=True, exist_ok=True)
        summaries: list[dict] = []
        expected_paths: set[Path] = set()
        for rec in records or []:
            if not isinstance(rec, dict):
                continue
            full_rec = load_history_record_items(rec)
            full_rec = _normalize_history_record(full_rec, keep_items=True)
            record_file = _task_history_record_file(full_rec["batch_id"])
            expected_paths.add(record_file)
            _atomic_write_json(record_file, {
                "version": 1,
                "saved_at": now_str(),
                **full_rec,
            })
            total, deleted, last_deleted_at = _history_record_summary(full_rec)
            summaries.append({
                "history_id": full_rec["batch_id"],
                "batch_id": full_rec["batch_id"],
                "batch_name": full_rec.get("batch_name", "") or "",
                "source": full_rec.get("source", "") or "",
                "added_at": full_rec.get("added_at", "") or "",
                "total": total,
                "deleted": deleted,
                "last_deleted_at": last_deleted_at,
                "_history_file": str(record_file),
            })

        for old in history_dir.glob("*.json"):
            if old.name == "index.json":
                continue
            if old not in expected_paths:
                try:
                    old.unlink()
                except Exception:
                    pass

        _atomic_write_json(_task_history_index_file(), {
            "version": 2,
            "saved_at": now_str(),
            "records": summaries,
        })
    except Exception:
        pass
