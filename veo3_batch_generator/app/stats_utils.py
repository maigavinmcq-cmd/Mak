from __future__ import annotations

from typing import Any

from app.models.batch import TaskBatch


def stabilize_live_completion_stats(
    previous: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
    *,
    same_batch: bool,
    active_running: bool,
) -> dict[str, Any]:
    """Keep live completion counters from regressing on stale snapshots.

    Worker threads can emit stats out of order: an older snapshot may reach the
    GUI after a newer completion has already been displayed. For the same active
    batch, full workflow completion counters should not move backwards on the
    dashboard or batch cards. Batch switches and idle reloads still show
    persisted data as-is.
    """

    result = dict(incoming or {})
    if not previous or not same_batch or not active_running:
        return result

    previous_total = _to_int(previous.get("total"))
    incoming_total = _to_int(result.get("total"))
    if previous_total and incoming_total and previous_total != incoming_total:
        return result

    for key in ("completed",):
        previous_value = _to_int(previous.get(key))
        incoming_value = _to_int(result.get(key))
        if incoming_value < previous_value:
            result[key] = previous_value
    return result


def apply_stats_to_batch_snapshot(
    batch: TaskBatch,
    stats: dict[str, Any] | None,
    *,
    status_override: str | None = None,
    active_running: bool = False,
) -> TaskBatch:
    """Apply a live stats event directly to a batch card snapshot."""

    payload = dict(stats or {})
    total = max(0, _to_int(payload.get("total") if "total" in payload else batch.task_count))
    completed = min(max(0, _to_int(payload.get("completed"))), total) if total else 0
    failed = min(max(0, _to_int(payload.get("failed"))), max(0, total - completed)) if total else 0
    skipped = min(max(0, _to_int(payload.get("skipped"))), max(0, total - completed - failed)) if total else 0
    timeout = min(max(0, _to_int(payload.get("timeout"))), max(0, total - completed - failed - skipped)) if total else 0
    polling = max(0, _to_int(payload.get("polling")))
    pending = max(0, _to_int(payload.get("pending")))
    running = max(0, _to_int(payload.get("running")))
    image_running = max(0, _to_int(payload.get("image_running")))
    submitted = max(0, _to_int(payload.get("submitted")))

    batch.task_count = total
    batch.completed_count = completed
    batch.failed_count = failed
    batch.skipped_count = skipped
    batch.timeout_count = timeout
    batch.polling_count = polling
    batch.pending_count = pending
    batch.progress_percent = round(completed / total * 100, 2) if total else 0.0
    batch.success_rate = batch.progress_percent
    batch.status = status_override or _infer_batch_status_from_stats(
        total,
        completed,
        failed,
        skipped,
        timeout,
        pending,
        polling,
        running + image_running + submitted,
        active_running=active_running,
        fallback=batch.status,
    )
    return batch


def _infer_batch_status_from_stats(
    total: int,
    completed: int,
    failed: int,
    skipped: int,
    timeout: int,
    pending: int,
    polling: int,
    running: int,
    *,
    active_running: bool,
    fallback: str,
) -> str:
    if active_running or running or polling:
        return "RUNNING"
    ended = completed + failed + skipped + timeout
    if total and ended >= total:
        if completed == total and not (failed or skipped or timeout):
            return "COMPLETED"
        if completed == 0 and failed + timeout >= total:
            return "FAILED"
        return "PARTIAL_FAILED"
    if failed or skipped or timeout:
        return "PARTIAL_FAILED"
    if pending:
        return "CREATED"
    return fallback or "CREATED"


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
