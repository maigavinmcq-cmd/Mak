# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass, field
import heapq
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import TaskItem


@dataclass
class StateIndex:
    """
    Central index manager for O(1) lookups by task state.
    Eliminates O(N) scans in tick functions for 5000+ task scale.
    """
    # Core indices: status -> set of task_ids
    by_status: dict[str, set[str]] = field(default_factory=dict)

    # Tasks that have remote_id
    with_remote_id: set[str] = field(default_factory=set)

    # Queued tasks without remote_id (create-only candidates)
    create_needed: set[str] = field(default_factory=set)

    # Tasks needing polling (has remote_id, not terminal/stopped, and no video_url yet)
    needs_poll: set[str] = field(default_factory=set)

    # Permanently failed tasks - never poll again
    terminal_failed: set[str] = field(default_factory=set)

    # Retry heap: min-heap by (next_retry_timestamp, task_id)
    retry_heap: list[tuple[float, str]] = field(default_factory=list)

    # Gate counters (incremental) - for O(1) gate counting
    unique_remote_ids: set[str] = field(default_factory=set)
    unique_video_urls: set[str] = field(default_factory=set)

    # Batch tracking
    tasks_by_batch: dict[str, set[str]] = field(default_factory=dict)
    create_needed_by_batch: dict[str, set[str]] = field(default_factory=dict)

    # Canonical retry schedule by task_id; heap entries are lazily invalidated.
    retry_due_at: dict[str, float] = field(default_factory=dict)

    def _should_need_poll(self, t: "TaskItem", status: str | None = None) -> bool:
        """Any task with remote_id should be polled unless it is terminal/stopped or already has url."""
        st = (status if status is not None else (t.status or "")).strip()
        remote_id = (getattr(t, "remote_id", None) or "").strip()
        poll_terminal = bool(getattr(t, "poll_terminal_fail", False))
        video_url = (getattr(t, "video_url", None) or "").strip()
        if not remote_id:
            return False
        if poll_terminal:
            return False
        if video_url:
            return False
        if st in ("Stopped", "Failed(Terminal)"):
            return False
        return True

    def clear(self):
        """Reset all indices."""
        self.by_status.clear()
        self.with_remote_id.clear()
        self.create_needed.clear()
        self.needs_poll.clear()
        self.terminal_failed.clear()
        self.retry_heap.clear()
        self.retry_due_at.clear()
        self.unique_remote_ids.clear()
        self.unique_video_urls.clear()
        self.tasks_by_batch.clear()
        self.create_needed_by_batch.clear()

    def build_from_tasks(self, tasks: dict[str, "TaskItem"]):
        """Build all indices from existing tasks dict. Called on load."""
        self.clear()

        for tid, t in tasks.items():
            self._index_task(t)

    def _index_task(self, t: "TaskItem"):
        """Add a single task to all relevant indices."""
        tid = t.task_id
        status = (t.status or "Queued").strip()

        # by_status index
        self.by_status.setdefault(status, set()).add(tid)

        # remote_id tracking
        remote_id = (getattr(t, "remote_id", None) or "").strip()
        if remote_id:
            self.with_remote_id.add(tid)
            self.unique_remote_ids.add(remote_id)

        # video_url tracking
        video_url = (getattr(t, "video_url", None) or "").strip()
        if video_url and status == "Success":
            self.unique_video_urls.add(video_url)

        # needs_poll
        poll_terminal = getattr(t, "poll_terminal_fail", False)
        if poll_terminal:
            self.terminal_failed.add(tid)
        elif self._should_need_poll(t, status):
            self.needs_poll.add(tid)

        # batch tracking
        batch_id = getattr(t, "batch_id", None) or "LEGACY"
        self.tasks_by_batch.setdefault(batch_id, set()).add(tid)
        self.create_needed_by_batch.setdefault(batch_id, set())

        # create_needed: queued + no remote_id + not terminal failure
        if (not poll_terminal) and (status == "Queued") and (not remote_id):
            self.create_needed.add(tid)
            self.create_needed_by_batch[batch_id].add(tid)

    def add_task(self, t: "TaskItem"):
        """Index a newly created task."""
        self._index_task(t)

    def remove_task(self, t: "TaskItem"):
        """Remove task from all indices."""
        tid = t.task_id
        status = (t.status or "Queued").strip()

        # by_status
        if status in self.by_status:
            self.by_status[status].discard(tid)

        # remote_id
        self.with_remote_id.discard(tid)
        remote_id = (getattr(t, "remote_id", None) or "").strip()
        # Note: We don't remove from unique_remote_ids as other tasks may have same id

        # video_url - same note as above

        # needs_poll
        self.needs_poll.discard(tid)
        self.terminal_failed.discard(tid)
        self.create_needed.discard(tid)

        # batch
        batch_id = getattr(t, "batch_id", None) or "LEGACY"
        if batch_id in self.tasks_by_batch:
            self.tasks_by_batch[batch_id].discard(tid)
        if batch_id in self.create_needed_by_batch:
            self.create_needed_by_batch[batch_id].discard(tid)

        # retry heap - mark as removed (lazy deletion)
        self.retry_due_at.pop(tid, None)

    def update_status(self, t: "TaskItem", old_status: str, new_status: str):
        """Update indices when task status changes."""
        if old_status == new_status:
            return

        tid = t.task_id

        # Update by_status
        if old_status in self.by_status:
            self.by_status[old_status].discard(tid)
        self.by_status.setdefault(new_status, set()).add(tid)

        # Update needs_poll based on new status
        remote_id = (getattr(t, "remote_id", None) or "").strip()
        poll_terminal = getattr(t, "poll_terminal_fail", False)
        batch_id = getattr(t, "batch_id", None) or "LEGACY"

        if poll_terminal:
            self.needs_poll.discard(tid)
            self.terminal_failed.add(tid)
        elif self._should_need_poll(t, new_status):
            if tid not in self.terminal_failed:
                self.needs_poll.add(tid)
        else:
            self.needs_poll.discard(tid)

        # Update create_needed based on status/remote_id/terminal
        if (not poll_terminal) and (new_status == "Queued") and (not remote_id):
            self.create_needed.add(tid)
            self.create_needed_by_batch.setdefault(batch_id, set()).add(tid)
        else:
            self.create_needed.discard(tid)
            if batch_id in self.create_needed_by_batch:
                self.create_needed_by_batch[batch_id].discard(tid)

        # If status becomes Success with video_url, add to unique_video_urls
        if new_status == "Success":
            video_url = (getattr(t, "video_url", None) or "").strip()
            if video_url:
                self.unique_video_urls.add(video_url)

    def update_remote_id(self, t: "TaskItem", old_remote_id: str, new_remote_id: str):
        """Update indices when remote_id changes."""
        tid = t.task_id

        if new_remote_id and new_remote_id.strip():
            self.with_remote_id.add(tid)
            self.unique_remote_ids.add(new_remote_id.strip())
            self.create_needed.discard(tid)

            # May need polling now
            status = (t.status or "").strip()
            if self._should_need_poll(t, status):
                self.needs_poll.add(tid)
        else:
            self.with_remote_id.discard(tid)
            self.needs_poll.discard(tid)
            status = (t.status or "").strip()
            poll_terminal = getattr(t, "poll_terminal_fail", False)
            if (not poll_terminal) and status == "Queued":
                self.create_needed.add(tid)

        # keep per-batch create index consistent
        batch_id = getattr(t, "batch_id", None) or "LEGACY"
        if tid in self.create_needed:
            self.create_needed_by_batch.setdefault(batch_id, set()).add(tid)
        elif batch_id in self.create_needed_by_batch:
            self.create_needed_by_batch[batch_id].discard(tid)

    def update_video_url(self, t: "TaskItem", old_url: str, new_url: str):
        """Update indices when video_url changes."""
        if new_url and new_url.strip():
            status = (t.status or "").strip()
            if status == "Success":
                self.unique_video_urls.add(new_url.strip())
            # Task with video_url doesn't need polling
            self.needs_poll.discard(t.task_id)

    def mark_terminal_failure(self, t: "TaskItem"):
        """Mark task as terminal failure - stops all polling."""
        tid = t.task_id
        self.terminal_failed.add(tid)
        self.needs_poll.discard(tid)
        self.retry_due_at.pop(tid, None)
        self.create_needed.discard(tid)
        batch_id = getattr(t, "batch_id", None) or "LEGACY"
        if batch_id in self.create_needed_by_batch:
            self.create_needed_by_batch[batch_id].discard(tid)

    def schedule_retry(self, t: "TaskItem", timestamp: float):
        """Add task to retry heap with given timestamp."""
        tid = t.task_id
        self.retry_due_at[tid] = float(timestamp)
        heapq.heappush(self.retry_heap, (timestamp, tid))

    def cancel_retry(self, task_id: str):
        """Cancel pending retry for a task."""
        self.retry_due_at.pop(task_id, None)

    def pop_due_retries(self, now: float, max_count: int = 20) -> list[str]:
        """Pop tasks whose retry time has passed. Returns list of task_ids."""
        result = []
        while self.retry_heap and len(result) < max_count:
            next_time, tid = self.retry_heap[0]
            if next_time > now:
                break
            heapq.heappop(self.retry_heap)
            current_due = self.retry_due_at.get(tid)
            if current_due is None:
                continue
            if abs(current_due - next_time) > 1e-6:
                continue
            self.retry_due_at.pop(tid, None)
            result.append(tid)
        return result

    # Convenience getters
    def get_queued(self) -> set[str]:
        """Get set of task_ids with status Queued."""
        return self.by_status.get("Queued", set())

    def get_running(self) -> set[str]:
        """Get set of task_ids with status Running."""
        return self.by_status.get("Running", set())

    def get_pending_check(self) -> set[str]:
        """Get set of task_ids with status Pending(Check)."""
        return self.by_status.get("Pending(Check)", set())

    def get_failed(self) -> set[str]:
        """Get set of task_ids with status Failed."""
        return self.by_status.get("Failed", set())

    def get_success(self) -> set[str]:
        """Get set of task_ids with status Success."""
        return self.by_status.get("Success", set())

    def running_count(self) -> int:
        """Get count of running + pending tasks."""
        return len(self.get_running()) + len(self.get_pending_check())

    def gate_counts(self) -> tuple[int, int]:
        """Get (remote_id_count, video_url_count) for gate checking."""
        return len(self.unique_remote_ids), len(self.unique_video_urls)
