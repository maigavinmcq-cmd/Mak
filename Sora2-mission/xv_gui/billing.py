# -*- coding: utf-8 -*-
from __future__ import annotations
from pathlib import Path

from .settings import BILLING_LEDGER_FILE, COST_PER_REQUEST, EV_CHARGE_REMOTE_ID, EV_FINAL_ACTUAL_COST
from .utils import now_str, today_str, safe_json_loads

class BillingManager:
    """
    Append-only JSONL ledger; safe for crashes; stats computed from ledger.
    IMPORTANT: Your business rule:
      - provider charges 0.4 only after remote_id is returned (server side cost)
      - failed tasks get refunded (net +0.4 back) -> record as negative amount
      - "actual spend" is computed when BOTH remote_id AND video_url exist.
    """
    def __init__(self):
        BILLING_LEDGER_FILE.parent.mkdir(exist_ok=True, parents=True)

    def append(self, payload: dict):
        payload = dict(payload)
        payload.setdefault("ts", now_str())
        payload.setdefault("date", today_str())
        try:
            with open(BILLING_LEDGER_FILE, "a", encoding="utf-8") as f:
                f.write(__import__("json").dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def today_stats(self) -> dict:
        stats = {
            "date": today_str(),
            "net_amount": 0.0,
            "charge_count": 0,
            "refund_count": 0,
            "actual_paid_count": 0,
            "by_provider": {},
        }
        p = Path(BILLING_LEDGER_FILE)
        if not p.exists():
            return stats

        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                obj = safe_json_loads(line) or {}
                if obj.get("date") != stats["date"]:
                    continue
                event = (obj.get("event") or "").strip()
                amt = float(obj.get("amount", 0) or 0.0)
                stats["net_amount"] += amt

                if event == EV_CHARGE_REMOTE_ID:
                    stats["charge_count"] += 1
                    pv = obj.get("provider", "unknown")
                    stats["by_provider"][pv] = stats["by_provider"].get(pv, 0) + 1

                if event.startswith("refund_"):
                    stats["refund_count"] += 1

                if event == EV_FINAL_ACTUAL_COST:
                    stats["actual_paid_count"] += 1
        except Exception:
            pass

        stats["net_amount"] = round(stats["net_amount"], 2)
        return stats

    # ---- per-task dedupe helpers ----
    def charge_remote_id_once(self, task, provider: str, model: str, remote_id: str):
        if getattr(task, "charged_once", False):
            return
        task.charged_once = True
        task.charged_at = now_str()
        self.append({
            "task_id": task.task_id,
            "provider": provider,
            "event": EV_CHARGE_REMOTE_ID,
            "amount": COST_PER_REQUEST,
            "remote_id": remote_id,
            "model": model,
            "attempt": task.run_attempt,
        })

    def refund_failed_once(self, task, provider: str, model: str, remote_id: str, event_name: str):
        if getattr(task, "refunded_once", False):
            return
        if not getattr(task, "charged_once", False):
            return
        task.refunded_once = True
        task.refunded_at = now_str()
        self.append({
            "task_id": task.task_id,
            "provider": provider,
            "event": event_name,
            "amount": -COST_PER_REQUEST,
            "remote_id": remote_id,
            "model": model,
            "attempt": task.run_attempt,
        })

    def mark_actual_cost_once(self, task, provider: str, model: str, remote_id: str, video_url: str):
        if getattr(task, "actual_cost_marked", False):
            return
        if (not remote_id) or (not video_url):
            return
        task.actual_cost_marked = True
        task.actual_cost_marked_at = now_str()
        self.append({
            "task_id": task.task_id,
            "provider": provider,
            "event": EV_FINAL_ACTUAL_COST,
            "amount": 0.0,
            "remote_id": remote_id,
            "video_url": video_url,
            "model": model,
            "attempt": task.run_attempt,
        })
