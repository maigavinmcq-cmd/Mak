# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass
import threading
from concurrent.futures import Future


@dataclass
class TaskItem:
    task_id: str
    provider: str
    base_url: str
    model: str
    prompt: str
    image_path: str
    note: str
    group: str
    log_file: str
    created_at: str
    completed_at: str = ""
    status_msg: str = ""
    poll_terminal_fail: bool = False
    replacement_spawned: bool = False
    batch_id: str = "LEGACY"
    batch_name: str = ""
    route_slot: int = -1

    status: str = "Queued"
    progress: float = 0.0
    video_url: str | None = None
    attempts: int = 0
    next_retry_at: str | None = None
    last_error: str = ""

    remote_id: str | None = None
    lost_reason: str = ""
    last_check_at: str = ""
    run_attempt: int = 0

    charged_once: bool = False
    refunded_once: bool = False
    actual_cost_marked: bool = False
    charged_at: str = ""
    refunded_at: str = ""
    actual_cost_marked_at: str = ""

    stop_event: threading.Event | None = None
    future: Future | None = None

    def to_persist_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "prompt": self.prompt,
            "image_path": self.image_path,
            "note": self.note,
            "group": self.group,
            "log_file": self.log_file,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "status_msg": self.status_msg,
            "poll_terminal_fail": bool(self.poll_terminal_fail),
            "replacement_spawned": bool(self.replacement_spawned),
            "batch_id": self.batch_id,
            "batch_name": self.batch_name,
            "route_slot": int(self.route_slot or -1),
            "status": self.status,
            "progress": float(self.progress),
            "video_url": self.video_url,
            "remote_id": self.remote_id,
            "lost_reason": self.lost_reason,
            "last_check_at": self.last_check_at,
            "attempts": int(self.attempts),
            "next_retry_at": self.next_retry_at,
            "last_error": self.last_error,
            "run_attempt": int(self.run_attempt or 0),
            "charged_once": bool(self.charged_once),
            "refunded_once": bool(self.refunded_once),
            "actual_cost_marked": bool(self.actual_cost_marked),
            "charged_at": self.charged_at,
            "refunded_at": self.refunded_at,
            "actual_cost_marked_at": self.actual_cost_marked_at,
        }

    @staticmethod
    def from_persist_dict(d: dict) -> "TaskItem":
        t = TaskItem(
            task_id=d.get("task_id", ""),
            provider=d.get("provider", "auto"),
            base_url=d.get("base_url", ""),
            model=d.get("model", ""),
            prompt=d.get("prompt", ""),
            image_path=d.get("image_path", ""),
            note=d.get("note", ""),
            group=d.get("group", d.get("note", "") or "Ungrouped"),
            log_file=d.get("log_file", ""),
            created_at=d.get("created_at", ""),
            completed_at=d.get("completed_at", "") or "",
            status_msg=d.get("status_msg", "") or "",
            poll_terminal_fail=bool(d.get("poll_terminal_fail", False)),
            replacement_spawned=bool(d.get("replacement_spawned", False)),
            batch_id=(d.get("batch_id") or "LEGACY"),
            batch_name=d.get("batch_name", "") or "",
            route_slot=int(d.get("route_slot", -1) or -1),
            status=d.get("status", "Queued"),
            progress=float(d.get("progress", 0.0) or 0.0),
            video_url=d.get("video_url"),
            remote_id=d.get("remote_id"),
            lost_reason=d.get("lost_reason", "") or "",
            last_check_at=d.get("last_check_at", "") or "",
            run_attempt=int(d.get("run_attempt", 0) or 0),
            attempts=int(d.get("attempts", 0) or 0),
            next_retry_at=d.get("next_retry_at"),
            last_error=d.get("last_error", "") or "",
            charged_once=bool(d.get("charged_once", False)),
            refunded_once=bool(d.get("refunded_once", False)),
            actual_cost_marked=bool(d.get("actual_cost_marked", False)),
            charged_at=d.get("charged_at", "") or "",
            refunded_at=d.get("refunded_at", "") or "",
            actual_cost_marked_at=d.get("actual_cost_marked_at", "") or "",
        )
        t.stop_event = threading.Event()
        t.future = None
        return t
