# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state_index import StateIndex


class GateController:
    """
    Queue Gate: if thresholds reached, freeze starting new tasks.
    Uses StateIndex for O(1) counting instead of full O(N) scans.
    """
    def __init__(self, enabled_var, remote_limit_var, link_limit_var, state_index: "StateIndex | None" = None):
        self.enabled_var = enabled_var
        self.remote_limit_var = remote_limit_var
        self.link_limit_var = link_limit_var
        self.idx = state_index
        self.queue_frozen = False
        self.popup_shown = False

    def set_index(self, state_index: "StateIndex"):
        """Set the StateIndex reference (can be set after construction)."""
        self.idx = state_index

    def counts(self, tasks=None) -> tuple[int, int]:
        """
        Get (remote_id_count, video_url_count).
        Uses O(1) indexed lookup if StateIndex is available.
        Falls back to O(N) scan if not (for backwards compatibility).
        """
        if self.idx is not None:
            return self.idx.gate_counts()

        # Fallback: O(N) scan (legacy behavior)
        remote_set = set()
        link_set = set()
        if tasks:
            for t in tasks.values():
                rid = (t.remote_id or "").strip()
                if rid:
                    remote_set.add(rid)
                if t.status == "Success" and (t.video_url or "").strip():
                    link_set.add(t.video_url.strip())
        return len(remote_set), len(link_set)

    def reached(self, tasks=None) -> bool:
        """Check if gate thresholds are reached."""
        if not bool(self.enabled_var.get()):
            return False

        r_count, l_count = self.counts(tasks)

        try:
            r_lim = int(self.remote_limit_var.get() or 0)
        except (ValueError, TypeError):
            r_lim = 0

        try:
            l_lim = int(self.link_limit_var.get() or 0)
        except (ValueError, TypeError):
            l_lim = 0

        cond_r = (r_lim > 0 and r_count >= r_lim)
        cond_l = (l_lim > 0 and l_count >= l_lim)
        return cond_r or cond_l
