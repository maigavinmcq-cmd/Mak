# -*- coding: utf-8 -*-
from __future__ import annotations
import time
from collections import deque
import copy
import re
import os
import json
import queue
import threading
import datetime
import webbrowser
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    import requests  # noqa: F401
except Exception as e:
    raise RuntimeError("requests is required: pip install requests") from e

from .settings import (
    APP_TITLE, LOG_DIR, TASKS_STORE_FILE, TASKS_STORE_MAX, TASK_HISTORY_FILE,
    APIYI_DEFAULT_BASE, APIYI_MODELS,
    XINTIAN_DEFAULT_BASE, XINTIAN_MODELS,
    LINGKE_DEFAULT_BASE, LINGKE_MODELS,
    TOAPIS_DEFAULT_BASE, TOAPIS_MODELS,
    BAOYOUHUYU_DEFAULT_BASE, BAOYOUHUYU_MODELS,
    JIMMY_DEFAULT_BASE, JIMMY_MODELS,
    DYUAPI_DEFAULT_BASE, DYUAPI_MODELS,
    PROVIDER_DEFAULT_MODELS,
    PROVIDERS,
    MISSION_ROOT_DEFAULT, DOWNLOAD_ROOT_DEFAULT,
    AUTO_RETRY_ENABLED, AUTO_RETRY_TICK_SEC, AUTO_RETRY_MAX_RETRIES,
    AUTO_RETRY_BASE_DELAY_SEC, AUTO_RETRY_MAX_DELAY_SEC,
    AUTO_RETRY_INCLUDE_DONE_NO_URL,
    EV_MANUAL_CANCEL, EV_RETRY_STARTED,
    COST_PER_REQUEST,
    DOWNLOAD_INDEX_FILE,
)

from .utils import (
    now_str, norm_path,
    parse_progress_percent,
    is_poll_terminal_failure,
)

from .models import TaskItem
from .persistence import (
    load_tasks_store, save_tasks_store, export_tasks_to, import_tasks_from,
    load_task_history, save_task_history, save_tasks_store_items, load_history_record_items,
)
from .billing import BillingManager
from .gate import GateController
from .downloader import run_batch_download
from .state_index import StateIndex

from .providers.apiyi import run_apiyi_sse, ApiyiConfig
from .providers.xintian import (
    run_xintian_upload_and_poll,
    run_xintian_poll_existing,
    run_xintian_create_only,
    XintianConfig,
)
from .providers.lingke import run_lingke_create_and_poll, LingkeConfig
from .providers.toapis import run_toapis_create_and_poll, ToapisConfig
from .providers.baoyouhuyu import (
    BaoyouhuyuConfig,
    run_baoyouhuyu_create_and_poll,
    run_baoyouhuyu_create_only,
    run_baoyouhuyu_poll_existing,
    run_baoyouhuyu_poll_once,
)
from .providers.jimmy import (
    JimmyConfig,
    run_jimmy_create_and_poll,
    run_jimmy_create_only,
    run_jimmy_poll_existing,
    run_jimmy_poll_once,
)
from .providers.dyuapi import (
    DyuapiConfig,
    run_dyuapi_create_and_poll,
    run_dyuapi_create_only,
    run_dyuapi_poll_existing,
    run_dyuapi_poll_once,
)

# Crypto optional
CRYPTO_OK = True
try:
    from .crypto_store import load_encrypted_config, save_encrypted_config
except Exception:
    CRYPTO_OK = False
    load_encrypted_config = None
    save_encrypted_config = None

PLAIN_CONFIG_FILE = "config.json"
USER_PREFS_DIR = Path("user_config")
USER_PREFS_FILE = USER_PREFS_DIR / "ui_prefs.json"

import re

SAFE_MODE_MAX_TOTAL_TASKS = 8000
SAFE_MODE_MAX_ADD_COUNT = 50
SAFE_MODE_MAX_REPEAT = 10
SAFE_MODE_MAX_IMPORT_PER_DIR = 50
SAFE_MODE_DEFAULT_IMPORT_PER_DIR = 20
SAFE_MODE_CONFIRM_THRESHOLD = 30


def compact_status_msg(err: str) -> str:
    """Compress noisy provider errors into a short message."""
    s = (err or "").strip()
    if not s:
        return ""

    def _extract_nested_json_message(text: str) -> str:
        try:
            payload = json.loads(text)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            if isinstance(payload.get("error"), dict):
                e = payload["error"]
                msg = str(e.get("message") or "").strip()
                typ = str(e.get("type") or "").strip()
                code2 = str(e.get("code") or "").strip()
                extra = " / ".join(x for x in (typ, code2) if x)
                return f"{msg} ({extra})".strip() if extra else msg
            if isinstance(payload.get("message"), str):
                inner = payload.get("message", "").strip()
                if inner.startswith("{"):
                    return _extract_nested_json_message(inner)
                return inner
        return ""

    prefix_match = re.match(r"^(create_http|poll_http|task_failed):(.+)$", s, flags=re.I | re.S)
    if prefix_match:
        kind = prefix_match.group(1).lower()
        body = prefix_match.group(2).strip()
        nested_msg = _extract_nested_json_message(body)
        if nested_msg:
            if kind == "create_http":
                return f"创建失败: {nested_msg}"
            if kind == "poll_http":
                return f"查询失败: {nested_msg}"
            return nested_msg

    friendly_map = [
        (r"dyuapi_image_size_not_allowed:(\d+x\d+)", r"Hellobabygo 图片尺寸不支持：\1，仅支持 1280x720 或 720x1280"),
        (r"dyuapi_size_not_allowed:(\d+x\d+)", r"Hellobabygo size 不支持：\1，仅支持 1280x720 或 720x1280"),
        (r"dyuapi_invalid_size_format:(.+)", r"Hellobabygo size 格式无效：\1"),
        (r"dyuapi_image_not_found:(.+)", r"参考图不存在：\1"),
        (r"completed_but_no_video_url", r"任务完成但未返回可用视频地址"),
        (r"create_http:Unable to process request", r"请求参数不符合 Hellobabygo 要求"),
        (r"poll_http:Unable to process request", r"查询请求参数不符合 Hellobabygo 要求"),
    ]
    for pat, repl in friendly_map:
        m = re.search(pat, s, flags=re.I)
        if m:
            return re.sub(pat, repl, s, count=1, flags=re.I)

    s = re.sub(r"\(request id:.*?\)", "", s, flags=re.I).strip()

    s = re.sub(r"\(\s*distributor\s*\)\s*$", "", s, flags=re.I).strip()

    m = re.search(r"HTTP\s*(\d{3})", s, flags=re.I)
    code = m.group(1) if m else ""

    m_cf = re.search(
        r"(?:^|\b)(create[_\s-]*fail(?:ed)?|create failed)\s*:\s*([^\n\r]+)",
        s,
        flags=re.I,
    )
    if m_cf:
        core = m_cf.group(2).strip()
        core = re.sub(r"\(\s*distributor\s*\)\s*$", "", core, flags=re.I).strip()
        return f"HTTP {code} {core}".strip() if code else core

    m2 = re.search(r"(Unauthorized|Invalid token|invalid_token)", s, flags=re.I)
    if m2:
        reason = m2.group(1)
        return f"HTTP {code} {reason}".strip() if code else reason

    m3 = re.search(r"Create failed:\s*([^\n\r]+)", s, flags=re.I)
    if m3:
        reason = m3.group(1).strip()
        reason = re.sub(r"\(\s*distributor\s*\)\s*$", "", reason, flags=re.I).strip()
        return f"HTTP {code} {reason}".strip() if code else reason

    line = s.splitlines()[0][:160].strip()
    line = re.sub(r"\(\s*distributor\s*\)\s*$", "", line, flags=re.I).strip()
    if code and f"HTTP {code}" not in line:
        return f"HTTP {code} {line}".strip()
    return line


def display_last_part(p: str) -> str:
    s = (p or "").strip().rstrip("\\/ ")
    if not s:
        return "未分组"
    return os.path.basename(s)


def _safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def _norm_maybe_url(p: str) -> str:
    """URL  norm_path"""
    if not isinstance(p, str):
        return ""
    s = p.strip()
    if s.lower().startswith(("http://", "https://")):
        return s
    return norm_path(s)


def _split_balanced_values(raw: str, *, normalize_url: bool = False) -> list[str]:
    parts = re.split(r"[\n,;，；|]+", (raw or "").strip())
    out: list[str] = []
    seen = set()
    for part in parts:
        value = part.strip()
        if not value:
            continue
        if normalize_url:
            value = _norm_maybe_url(value)
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def is_terminal_failure(msg: str) -> bool:
    s = (msg or "").strip().lower()
    if not s:
        return False

    terminal_patterns = [
        r"\bhttp\s*401\b", r"\b401\b",
        r"\bhttp\s*403\b", r"\b403\b",
        r"\bhttp\s*400\b", r"\b400\b",
        r"\bhttp\s*404\b", r"\b404\b",
        r"invalid[_\s-]*token", r"unauthorized",
        r"forbidden", r"permission\s*denied", r"not\s*allowed",
        r"\bnot\s*found\b", r"heavy_load",
        r"missing\s*api\s*key",
    ]

    for pat in terminal_patterns:
        if re.search(pat, s, flags=re.I):
            return True

    retryable_patterns = [
        r"\bhttp\s*503\b", r"\b503\b",
        r"no\s*available\s*channel",
        r"distributor", r"capacity", r"busy", r"try\s*again",
    ]
    for pat in retryable_patterns:
        if re.search(pat, s, flags=re.I):
            return False

    return False




class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self._apply_initial_window_geometry()
        self._closing = False
        self._main_thread = threading.current_thread()
        self._ui_event_q: queue.Queue[tuple] = queue.Queue()
        self._ui_pump_interval_ms = 50
        self._ui_pump_scheduled = False
        self.replace_terminal_var = tk.BooleanVar(value=True)
        self._log_buf: list[str] = []
        self._log_flush_pending = False

        self._dirty_task_ids = set()
        self._ui_flush_pending = False
        self._last_billing_ts = 0.0

        Path(LOG_DIR).mkdir(exist_ok=True, parents=True)
        # 
        self.max_concurrency_var = tk.StringVar(value="6")
        self.auto_replace_terminal_var = tk.BooleanVar(value=True)
        # state
        self.executor = ThreadPoolExecutor(max_workers=10)
        self.save_executor = ThreadPoolExecutor(max_workers=1)
        # create_only uses an independent high-capacity pool so remote_id creation is not blocked by max_concurrency
        self.create_executor = ThreadPoolExecutor(max_workers=256)
        # polling uses a small fixed pool; actual queueing is done in app logic
        self.poll_executor = ThreadPoolExecutor(max_workers=12)
        self._active_futures_lock = threading.Lock()
        self._active_create_task_ids: set[str] = set()
        self._active_poll_task_ids: set[str] = set()
        self._poll_timer_id = None
        self.tasks: dict[str, TaskItem] = {}
        self.task_counter = 1
        self.current_config = {}
        self.billing = BillingManager()

        # UI vars
        self.passphrase_var = tk.StringVar(value="")
        self._passphrase_verified = False
        self.key_loaded_var = tk.BooleanVar(value=False)
        self.pause_new_tasks_var = tk.BooleanVar(value=False)
        self.start_poll_only_var = tk.BooleanVar(value=False)

        self.provider_var = tk.StringVar(value="jimmy")
        self.base_url_var = tk.StringVar(value=JIMMY_DEFAULT_BASE)
        self.model_var = tk.StringVar(value=JIMMY_MODELS[0])
        self.api_key_var = tk.StringVar(value="")

        self.mission_root_var = tk.StringVar(value=str(MISSION_ROOT_DEFAULT))
        self.download_root_var = tk.StringVar(value=str(DOWNLOAD_ROOT_DEFAULT))

        self.prompt_var = tk.StringVar(value="")
        self.image_path_var = tk.StringVar(value="")

        self.note_var = tk.StringVar(value="")
        self.group_var = tk.StringVar(value="")
        self.batch_name_var = tk.StringVar(value="")
        self.batch_name_preview_var = tk.StringVar(value="")
        self.task_input_mode_var = tk.StringVar(value="single")
        self.mission_n_each_var = tk.IntVar(value=0)  # 0=
        self.repeat_add_var = tk.IntVar(value=1)
        self.subdir_name_list_var = tk.StringVar(value="")
        self.require_existing_subdir_var = tk.BooleanVar(value=False)
        self.auto_collapse_batch_var = tk.BooleanVar(value=False)

        self._dirty_task_ids: set[str] = set()
        self._ui_flush_pending: bool = False
        self._last_billing_update_ts: float = 0.0
        self.page_size = 500
        self.current_page = 1
        self._filtered_total_count = 0
        self._total_pages = 1

        # filters
        self.filter_status_var = tk.StringVar(value="全部")
        self.filter_provider_var = tk.StringVar(value="全部")
        self.search_var = tk.StringVar(value="")
        self.group_view_mode_var = tk.StringVar(value="按大组")
        self._status_filter_map = {
            "全部": "ALL",
            "排队中": "Queued",
            "运行中": "Running",
            "轮询中": "Pending(Check)",
            "等待检查": "Pending(Check)",
            "成功": "Success",
            "已下载": "Downloaded",
            "完成(无链接)": "Done(No URL)",
            "失败": "Failed",
            "已停止": "Stopped",
            "终态失败": "Failed(Terminal)",
        }
        self._status_text_map = {
            "Queued": "排队中",
            "Running": "运行中",
            "Pending(Check)": "轮询中",
            "Success": "成功",
            "Downloaded": "已下载",
            "Done(No URL)": "完成(无链接)",
            "Failed": "失败",
            "Stopped": "已停止",
            "Failed(Terminal)": "终态失败",
        }

        # auto retry
        self.auto_retry_enabled_var = tk.BooleanVar(value=AUTO_RETRY_ENABLED)
        self.auto_retry_include_done_no_url_var = tk.BooleanVar(value=AUTO_RETRY_INCLUDE_DONE_NO_URL)
        self.safe_mode_var = tk.BooleanVar(value=True)
        self.admin_mode_var = tk.BooleanVar(value=False)
        self.advanced_visible_var = tk.BooleanVar(value=False)
        self.novice_mode_var = tk.BooleanVar(value=True)

        # state index (must be before gate)
        self.idx = StateIndex()

        # gate
        self.gate_enabled_var = tk.BooleanVar(value=False)
        self.gate_remote_limit_var = tk.StringVar(value="0")
        self.gate_link_limit_var = tk.StringVar(value="0")
        self.gate = GateController(self.gate_enabled_var, self.gate_remote_limit_var, self.gate_link_limit_var, self.idx)

        # download options
        self.dl_workers_var = tk.StringVar(value="10")
        self.dl_retries_var = tk.StringVar(value="2")
        self.dl_skip_dup_var = tk.BooleanVar(value=True)

        # marker file for one-time plain key visibility behavior
        self._key_marker_file = Path(".key_saved.marker")

        # BatchGate state
        self.batch_counter = 1
        self.active_batch_id: str | None = None
        self.task_history: list[dict] = []
        self._collapsed_group_keys_by_mode: dict[str, set[str]] = {"按大组": set(), "按小分组": set()}
        self._history_save_pending = False
        self._history_save_reschedule = False
        self._save_pending = False
        self._save_reschedule = False
        self._tasks_dirty = False
        self._tasks_save_timer_id = None
        self._save_delay_ms = 20000
        self._poll_queue: deque[str] = deque()
        self._poll_enqueued: set[str] = set()
        self._table_render_token = 0
        self._table_render_plan = []
        self._table_render_index = 0
        self._table_render_scroll = None
        self._route_rr_state: dict[str, int] = {}

        # BatchGate dedupe memory (must be initialized in __init__)
        self._bg_last_active_batch_logged: str | None = None
        self._bg_last_release_logged: str | None = None
        self._bg_last_started_logged: tuple[str, int] | None = None
        # create-only chain control:
        # if one create step fails, block launching new tasks until this one succeeds.
        self._create_blocked_tid: str | None = None
        self._create_retry_after_ts: float = 0.0

        # async per-task file log writer
        self._task_log_write_q: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=10000)
        self._task_log_writer_stop = threading.Event()
        self._task_log_writer = threading.Thread(target=self._task_log_writer_loop, daemon=True)
        self._task_log_writer.start()

        self._manual_col_resize = False
        self._tree_sep_dragging = False
        self._prefs_loading = False
        self._prefs_save_timer = None

        self._build_ui()
        self._bind_events()
        self._load_user_prefs()

        # restore tasks
        self._restore_tasks()

        # startup default: paused
        self.log_line("启动默认已暂停。点击“开始执行”后再运行任务。\n")

        # two-phase mode: create remote_id first, then 60s polling
        self.two_phase_mode = True
        self.poll_interval_ms = 60_000  # 60s
        self._poll_timer_started = False
        # startup resume remote_id tasks
        # self.root.after(800, self._resume_remote_id_tasks_on_startup)

        # timers
        self._schedule_ui_pump()
        self.root.after(1200, self._tick_autosave)
        self.root.after(1200, self._tick_auto_retry)
        self.root.after(1500, self._tick_gate)

        # autorun queue
        self.root.after(1200, self._tick_autorun_queue)
        # always keep 60s polling ticker alive (polling itself decides what to do)
        self._schedule_poll_tick(1500, force=True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- helpers ----------
    def _apply_initial_window_geometry(self):
        try:
            sw = max(1024, int(self.root.winfo_screenwidth() or 1380))
            sh = max(720, int(self.root.winfo_screenheight() or 820))
            width = min(1320, max(940, int(sw * 0.88)))
            height = min(760, max(620, int(sh * 0.78)))
            if width > sw:
                width = sw
            if height > sh:
                height = sh
            x = max(0, (sw - width) // 2)
            y = max(0, (sh - height) // 2)
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self.root.minsize(940, 620)
        except Exception:
            self.root.geometry("1380x820")

    def _fit_geometry_to_screen(self, geom: str) -> str:
        s = (geom or "").strip()
        m = re.match(r"^\s*(\d+)x(\d+)(?:\+(-?\d+)\+(-?\d+))?\s*$", s)
        if not m:
            return s
        sw = max(1024, int(self.root.winfo_screenwidth() or 1380))
        sh = max(720, int(self.root.winfo_screenheight() or 820))
        width = min(sw, max(940, int(m.group(1))))
        height = min(sh, max(620, int(m.group(2))))
        x = int(m.group(3) or max(0, (sw - width) // 2))
        y = int(m.group(4) or max(0, (sh - height) // 2))
        x = min(max(0, x), max(0, sw - width))
        y = min(max(0, y), max(0, sh - height))
        return f"{width}x{height}+{x}+{y}"

    def _on_main_thread(self) -> bool:
        return threading.current_thread() is self._main_thread

    def _enqueue_ui(self, fn, *args, **kwargs):
        if self._closing:
            return
        self._ui_event_q.put((fn, args, kwargs))

    def _schedule_ui_pump(self):
        if self._closing or self._ui_pump_scheduled:
            return
        self._ui_pump_scheduled = True
        self.root.after(self._ui_pump_interval_ms, self._drain_ui_queue)

    def _drain_ui_queue(self):
        self._ui_pump_scheduled = False
        if self._closing:
            return
        processed = 0
        while processed < 500:
            try:
                fn, args, kwargs = self._ui_event_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args, **kwargs)
            except Exception:
                pass
            processed += 1
        if not self._closing:
            self._schedule_ui_pump()

    def _schedule_poll_tick(self, delay_ms: int | None = None, force: bool = False):
        if self._closing:
            return
        delay = self.poll_interval_ms if delay_ms is None else max(0, int(delay_ms))
        if self._poll_timer_id is not None:
            if not force:
                return
            try:
                self.root.after_cancel(self._poll_timer_id)
            except Exception:
                pass
            self._poll_timer_id = None
        self._poll_timer_id = self.root.after(delay, self._run_poll_tick)

    def _run_poll_tick(self):
        self._poll_timer_id = None
        self._tick_poll_once_all()

    def _active_count(self, bucket: str) -> int:
        with self._active_futures_lock:
            if bucket == "create":
                return len(self._active_create_task_ids)
            if bucket == "poll":
                return len(self._active_poll_task_ids)
            return 0

    def _try_acquire_active_slot(self, bucket: str, task_id: str, limit: int) -> bool:
        with self._active_futures_lock:
            active = self._active_create_task_ids if bucket == "create" else self._active_poll_task_ids
            if task_id in active:
                return False
            if len(active) >= max(1, int(limit)):
                return False
            active.add(task_id)
            return True

    def _release_active_slot(self, bucket: str, task_id: str):
        with self._active_futures_lock:
            active = self._active_create_task_ids if bucket == "create" else self._active_poll_task_ids
            active.discard(task_id)

    def _create_submit_limit(self) -> int:
        base = max(1, _safe_int(self.max_concurrency_var.get(), 6))
        return min(24, max(2, base))

    def _poll_submit_limit(self) -> int:
        base = max(1, _safe_int(self.max_concurrency_var.get(), 6))
        return min(10, max(6, base))

    def _new_batch_id(self) -> str:
        bid = f"B{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{self.batch_counter:03d}"
        self.batch_counter += 1
        return bid

    def _resolve_batch_name(self, batch_id: str, fallback: str = "") -> str:
        name = (self.batch_name_var.get() or "").strip()
        if name:
            return name
        fb = (fallback or "").strip()
        if fb:
            return fb
        return batch_id

    def _tick_poll_once_all(self):
        if self._closing:
            return
        try:
            candidate_ids = list(self.idx.needs_poll)
            self._enqueue_poll_task_ids(candidate_ids)
            self._drain_poll_queue()

        finally:
            if not self._closing:
                self._schedule_poll_tick(self.poll_interval_ms)

    def _should_poll_task(self, t: TaskItem | None) -> bool:
        if not t:
            return False
        rid = (t.remote_id or "").strip()
        if not rid:
            self.idx.needs_poll.discard(getattr(t, "task_id", ""))
            return False
        if (t.video_url or "").strip():
            self.idx.needs_poll.discard(t.task_id)
            return False
        if getattr(t, "poll_terminal_fail", False):
            self.idx.needs_poll.discard(t.task_id)
            self.idx.terminal_failed.add(t.task_id)
            return False
        msg = (t.status_msg or t.last_error or "").strip()
        if msg and is_poll_terminal_failure(msg):
            self._mark_terminal_failure(t)
            self._set_task_status(t, "Failed(Terminal)")
            if not (t.status_msg or "").strip():
                t.status_msg = compact_status_msg(msg) or msg
            self._ui_update_task_row(t)
            self._maybe_enqueue_replacement(t, msg)
            return False
        return True

    def _enqueue_poll_task_ids(self, task_ids: list[str]):
        for tid in task_ids:
            if not tid or tid in self._poll_enqueued:
                continue
            t = self.tasks.get(tid)
            if not self._should_poll_task(t):
                continue
            self._poll_queue.append(tid)
            self._poll_enqueued.add(tid)

    def _drain_poll_queue(self):
        if self._closing:
            return
        limit = self._poll_submit_limit()
        while self._active_count("poll") < limit and self._poll_queue:
            tid = self._poll_queue.popleft()
            self._poll_enqueued.discard(tid)
            t = self.tasks.get(tid)
            if not self._should_poll_task(t):
                continue
            try:
                if getattr(t, "future", None) and t.future and not t.future.done():
                    continue
            except Exception:
                pass
            self._poll_once_task(t)

    def _task_batch_id(self, t: TaskItem) -> str:
        return (getattr(t, "batch_id", None) or "LEGACY")

    def _retry_due(self, t: TaskItem) -> bool:
        nra = getattr(t, "next_retry_at", None)
        if not (isinstance(nra, str) and nra.strip()):
            return True
        try:
            dt = datetime.datetime.strptime(nra.strip(), "%Y-%m-%d %H:%M:%S")
            return datetime.datetime.now() >= dt
        except Exception:
            return True

    def apply_concurrency(self):
        n = _safe_int(self.max_concurrency_var.get(), 6)
        if n < 1:
            n = 1
        if n > 64:
            n = 64

        try:
            old = self.executor
            self.executor = ThreadPoolExecutor(max_workers=n)
            try:
                old.shutdown(wait=False, cancel_futures=False)
            except Exception:
                pass
            poll_workers = max(6, min(12, max(6, n)))
            old_poll = self.poll_executor
            self.poll_executor = ThreadPoolExecutor(max_workers=poll_workers)
            try:
                old_poll.shutdown(wait=False, cancel_futures=False)
            except Exception:
                pass
            self.log_line(f"已应用最大并发线程数={n}\n")
        except Exception as e:
            messagebox.showerror("错误", str(e))

    def _alloc_task_id(self) -> str:
        # allocate next task id from existing Txxxxx ids
        max_n = 0
        for k in self.tasks.keys():
            if isinstance(k, str) and k.startswith("T") and k[1:].isdigit():
                max_n = max(max_n, int(k[1:]))
        return f"T{max_n + 1:05d}"

    def _make_task_log_file(self, task_id: str) -> str:
        #  log_dir/
        # fallback task log path under project logs dir
        try:
            log_dir = os.path.dirname(next(iter(self.tasks.values())).log_file) if self.tasks else ""
        except Exception:
            log_dir = ""
        if not log_dir:
            log_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, f"{task_id}.log.txt")

    def _maybe_enqueue_replacement(self, t: TaskItem, reason: str):
        if not self.auto_retry_enabled_var.get():
            return
        try:
            self._ui_call(lambda: self._recreate_task_after_poll_failed(t, reason))
        except Exception:
            pass

    def mark_dirty(self, task_id: str):
        """Mark one task row dirty for batched UI refresh."""
        if not task_id:
            return
        self._dirty_task_ids.add(task_id)
        self._request_ui_flush()

    def _request_ui_flush(self):
        """Schedule batched row flush every 100ms."""
        if self._ui_flush_pending:
            return
        self._ui_flush_pending = True
        self.root.after(100, self._flush_dirty_rows)

    def _flush_dirty_rows(self):
        self._ui_flush_pending = False
        if not self._dirty_task_ids:
            self._maybe_update_billing_label()
            return
        dirty_ids = list(self._dirty_task_ids)
        self._dirty_task_ids.clear()
        if getattr(self, "_table_render_index", 0) < len(getattr(self, "_table_render_plan", [])):
            self.refresh_table()
        else:
            fallback_refresh = False
            for task_id in dirty_ids:
                if not getattr(self, "tree", None):
                    fallback_refresh = True
                    break
                if self._is_group_iid(task_id):
                    fallback_refresh = True
                    break
                t = self.tasks.get(task_id)
                if not t:
                    continue
                try:
                    if self.tree.exists(task_id):
                        self._apply_row_values_fast(t)
                    else:
                        fallback_refresh = True
                        break
                except Exception:
                    fallback_refresh = True
                    break
            if fallback_refresh:
                self.refresh_table()
        self._maybe_update_billing_label()

    def _maybe_update_billing_label(self):
        """Throttle footer updates (billing + status overview)."""
        now = time.time()
        if now - self._last_billing_update_ts < 0.5:
            return
        self._last_billing_update_ts = now
        try:
            self._update_billing_label()
        except Exception:
            pass
        try:
            self._update_status_overview_label()
        except Exception:
            pass

    def _task_passes_filter(self, t: TaskItem) -> bool:
        status_filter_raw = self.filter_status_var.get()
        status_filter = self._status_filter_map.get(status_filter_raw, status_filter_raw)
        provider_filter_raw = self.filter_provider_var.get()
        provider_filter = "ALL" if provider_filter_raw == "全部" else provider_filter_raw
        search_query = (self.search_var.get() or "").strip().lower()

        if status_filter != "ALL" and t.status != status_filter:
            return False
        if provider_filter != "ALL" and (t.provider or "") != provider_filter:
            return False
        if search_query:
            haystack = f"{t.task_id} {t.note} {t.group} {getattr(t, 'batch_name', '')} {t.model} {t.prompt}".lower()
            if search_query not in haystack:
                return False
        return True

    def _group_iid(self, group_name: str) -> str:
        g_name = (group_name or "未分组").strip() or "未分组"
        return f"group_{hashlib.md5(g_name.encode('utf-8')).hexdigest()[:10]}"

    def _batch_iid(self, batch_id: str) -> str:
        return self._group_iid(batch_id or "LEGACY")

    @staticmethod
    def _task_batch_name(t: TaskItem) -> str:
        name = (getattr(t, "batch_name", "") or "").strip()
        if name:
            return name
        return (getattr(t, "batch_id", "") or "LEGACY").strip() or "LEGACY"

    def _batch_display_name(self, batch_id: str, task_list: list[TaskItem]) -> str:
        if task_list:
            name = self._task_batch_name(task_list[0])
            if name:
                return name
        return (batch_id or "LEGACY").strip() or "LEGACY"

    def _current_group_mode(self) -> str:
        mode = (self.group_view_mode_var.get() or "").strip()
        return mode if mode in ("按大组", "按小分组") else "按大组"

    def _group_display_name(self, key: str, task_list: list[TaskItem], mode: str) -> str:
        if mode == "按小分组":
            return display_last_part(key)
        return self._batch_display_name(key, task_list)

    def _find_existing_batch(self, batch_name: str) -> tuple[str, str] | None:
        wanted = (batch_name or "").strip()
        if not wanted:
            return None
        for t in sorted(self.tasks.values(), key=lambda x: (self._task_batch_name(x), x.batch_id, x.task_id)):
            existing_name = self._task_batch_name(t)
            if existing_name != wanted:
                continue
            bid = (getattr(t, "batch_id", None) or "LEGACY").strip() or "LEGACY"
            return bid, existing_name
        return None

    @staticmethod
    def _batch_date_suffix() -> str:
        return datetime.datetime.now().strftime("%Y%m%d")

    def _default_batch_name_for_group(self, group_name: str) -> str:
        base = display_last_part(group_name or "").strip()
        if not base or base == "未分组":
            base = "手动批次"
        return f"{base}_{self._batch_date_suffix()}"

    def _default_batch_name_for_dir(self, dir_path: str) -> str:
        raw = (dir_path or "").strip()
        try:
            p = Path(raw)
        except Exception:
            p = None
        current_name = display_last_part(raw).strip()
        parent_name = ""
        try:
            if p is not None:
                parent_name = (p.parent.name or "").strip()
        except Exception:
            parent_name = ""
        parts = [x for x in (parent_name, current_name) if x and x != "未分组"]
        base = "_".join(parts).strip("_")
        if not base:
            base = "扫描批次"
        return f"{base}_{self._batch_date_suffix()}"

    def _make_available_batch_name(self, base_name: str) -> str:
        base = (base_name or "").strip() or f"批次_{self._batch_date_suffix()}"
        if not self._find_existing_batch(base):
            return base
        i = 2
        while True:
            cand = f"{base}_{i:02d}"
            if not self._find_existing_batch(cand):
                return cand
            i += 1

    def _update_batch_name_preview(self):
        manual_name = (self.batch_name_var.get() or "").strip()
        if manual_name:
            self.batch_name_preview_var.set(f"{manual_name}  (手动指定)")
            return
        subdirs = self._parse_subdir_names()
        if subdirs:
            self.batch_name_preview_var.set(self._default_batch_name_for_dir(self.mission_root_var.get()))
            return
        group_name = (self.group_var.get() or "").strip() or "手动批次"
        self.batch_name_preview_var.set(self._default_batch_name_for_group(group_name))

    def _confirm_merge_existing_batch(self, batch_name: str) -> bool:
        return bool(messagebox.askyesno(
            "检测到同名大组",
            f"已存在同名大组：\n{batch_name}\n\n"
            "是否将本次新增任务合并到之前的大组中？\n"
            "是=合并到原大组\n否=新建一个同名日期批次的变体"
        ))

    def _resolve_target_batch_for_name(self, desired_name: str, *, ask_merge: bool = True) -> tuple[str, str, bool]:
        wanted = (desired_name or "").strip()
        existing = self._find_existing_batch(wanted) if wanted else None
        if existing:
            if ask_merge and not self._confirm_merge_existing_batch(existing[1]):
                batch_id = self._new_batch_id()
                return batch_id, self._make_available_batch_name(existing[1]), False
            return existing[0], existing[1], True
        batch_id = self._new_batch_id()
        batch_name = wanted or self._resolve_batch_name(batch_id, "")
        return batch_id, batch_name, False

    def _resolve_target_batch(self, fallback_name: str) -> tuple[str, str, bool]:
        requested_name = (self.batch_name_var.get() or "").strip()
        desired_name = requested_name or self._default_batch_name_for_group(fallback_name)
        return self._resolve_target_batch_for_name(desired_name, ask_merge=True)

    def _group_key_for_task(self, t: TaskItem, mode: str | None = None) -> str:
        gm = mode or self._current_group_mode()
        if gm == "按小分组":
            return (t.group or "未分组").strip() or "未分组"
        return (getattr(t, "batch_id", None) or "LEGACY").strip() or "LEGACY"

    def _ensure_group_row_for_key(self, key: str, mode: str, sample_task: TaskItem | None = None) -> str:
        gid = self._group_iid(key)
        meta = getattr(self, "_tree_group_meta", None)
        if not isinstance(meta, dict):
            self._tree_group_meta = {}
            meta = self._tree_group_meta
        meta[gid] = (mode, key)
        if sample_task is not None:
            display_name = self._group_display_name(key, [sample_task], mode)
        else:
            display_name = display_last_part(key) if mode == "按小分组" else key
        collapsed = key in self._collapsed_group_keys_by_mode.setdefault(mode, set())
        if not self.tree.exists(gid):
            self.tree.insert("", "end", iid=gid, text=f"[{display_name}] (0)", values=("",) * len(self.cols), open=(not collapsed))
        return gid

    def _update_group_row_count(self, gid: str):
        if not self.tree.exists(gid):
            return
        cnt = len(self.tree.get_children(gid))
        if cnt <= 0:
            self.tree.delete(gid)
            try:
                self._tree_group_meta.pop(gid, None)
            except Exception:
                pass
            return
        meta = getattr(self, "_tree_group_meta", {}).get(gid, (self._current_group_mode(), "未分组"))
        if isinstance(meta, dict):
            mode = meta.get("mode", self._current_group_mode())
            key = meta.get("collapse_key", "未分组")
        else:
            mode, key = meta
        children = list(self.tree.get_children(gid))
        sample_task = self.tasks.get(children[0]) if children else None
        display_name = self._group_display_name(key, [sample_task] if sample_task else [], mode)
        self.tree.item(gid, text=f"[{display_name}] ({cnt})")

    def _remove_task_row(self, task_id: str):
        if not self.tree.exists(task_id):
            return
        parent_gid = self.tree.parent(task_id)
        self.tree.delete(task_id)
        if parent_gid and self.tree.exists(parent_gid):
            self._update_group_row_count(parent_gid)

    def _ensure_task_row_visible(self, t: TaskItem):
        try:
            if not self._task_passes_filter(t):
                self._remove_task_row(t.task_id)
                return

            mode = self._current_group_mode()
            if mode == "按大组":
                batch_key = (getattr(t, "batch_id", None) or "LEGACY").strip() or "LEGACY"
                batch_gid = self._ensure_group_row_for_key(batch_key, mode, t)
                subgroup = (t.group or "未分组").strip() or "未分组"
                collapse_key = f"batch::{batch_key}::group::{subgroup}"
                gid = self._group_iid(collapse_key)
                self._tree_group_meta[gid] = {"mode": mode, "collapse_key": collapse_key}
                collapsed = collapse_key in self._collapsed_group_keys_by_mode.setdefault(mode, set())
                if not self.tree.exists(gid):
                    self.tree.insert(
                        batch_gid,
                        "end",
                        iid=gid,
                        text=f"[{display_last_part(subgroup)}] (0)",
                        values=("",) * len(self.cols),
                        open=(not collapsed),
                    )
            else:
                g_key = self._group_key_for_task(t, mode)
                gid = self._ensure_group_row_for_key(g_key, mode, t)
            old_parent = self.tree.parent(t.task_id) if self.tree.exists(t.task_id) else ""

            if not self.tree.exists(t.task_id):
                self.tree.insert(gid, "end", iid=t.task_id, text=t.task_id, values=self._row_values(t))
            elif old_parent != gid:
                self.tree.move(t.task_id, gid, "end")

            self._update_group_row_count(gid)
            if mode == "按大组":
                parent_gid = self.tree.parent(gid)
                if parent_gid and self.tree.exists(parent_gid):
                    self._update_group_row_count(parent_gid)
            if old_parent and old_parent != gid and self.tree.exists(old_parent):
                self._update_group_row_count(old_parent)
        except Exception as e:
            try:
                self.log_line(f"确保任务行可见失败：{e}\n")
            except Exception:
                pass

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=4)

        self.lbl_passphrase = ttk.Label(top, text="口令")
        self.lbl_passphrase.pack(side="left")
        self.ent_passphrase = ttk.Entry(top, textvariable=self.passphrase_var, width=22, show="*")
        self.ent_passphrase.pack(side="left", padx=6)
        self.btn_load_key = ttk.Button(top, text="读取密钥", command=self.load_keys)
        self.btn_load_key.pack(side="left")
        self.btn_save_key = ttk.Button(top, text="保存密钥", command=self.save_keys)
        self.btn_save_key.pack(side="left", padx=6)
        self.key_state = ttk.Label(top, text="密钥未加载", foreground="red")
        self.key_state.pack(side="left", padx=8)
        self.role_state_label = ttk.Label(top, text="角色：普通用户", foreground="blue")
        self.role_state_label.pack(side="left", padx=(16, 4))
        self.btn_unlock_admin = ttk.Button(top, text="管理员解锁", command=self.unlock_admin_mode)
        self.btn_unlock_admin.pack(side="left", padx=4)
        self.btn_lock_normal = ttk.Button(top, text="切回普通", command=self.lock_to_normal_mode, state="disabled")
        self.btn_lock_normal.pack(side="left", padx=4)
        self.btn_restore_admin_full = ttk.Button(
            top, text="恢复完整管理员界面", command=self.restore_full_admin_layout
        )
        self.btn_restore_admin_full.pack(side="left", padx=6)
        self.chk_novice_mode = ttk.Checkbutton(
            top, text="一键新手模式", variable=self.novice_mode_var, command=self._apply_novice_mode
        )
        self.chk_novice_mode.pack(side="left", padx=(10, 0))
        self.chk_safe_mode_quick = ttk.Checkbutton(top, text="傻瓜防浪费模式", variable=self.safe_mode_var)
        self.chk_safe_mode_quick.pack(side="left", padx=(10, 0))

        ttk.Separator(self.root).pack(fill="x", padx=8, pady=4)

        self.beginner_status_frame = ttk.LabelFrame(self.root, text="当前状态")
        self.beginner_status_frame.pack(fill="x", padx=8, pady=(0, 4))
        self.primary_status_label = ttk.Label(self.beginner_status_frame, text="状态：-")
        self.primary_status_label.pack(side="left", padx=(10, 12), pady=8)
        self.primary_hint_label = ttk.Label(self.beginner_status_frame, text="下一步：先添加任务，再点击“开始执行”。")
        self.primary_hint_label.pack(side="left", padx=(0, 12), pady=8)
        self.primary_counts_label = ttk.Label(self.beginner_status_frame, text="任务统计：-")
        self.primary_counts_label.pack(side="right", padx=10, pady=8)

        cfg = ttk.LabelFrame(self.root, text="第1步：准备文件夹")
        cfg.pack(fill="x", padx=8, pady=(0, 4))

        self.lbl_provider = ttk.Label(cfg, text="平台:")
        self.lbl_provider.grid(row=0, column=0, sticky="w")
        self.provider_cb = ttk.Combobox(
            cfg, textvariable=self.provider_var,
            values=[p[1] for p in PROVIDERS], width=14, state="readonly"
        )
        self.provider_cb.grid(row=0, column=1, padx=6, sticky="w")

        self.lbl_base_url = ttk.Label(cfg, text="接口地址(支持多项):")
        self.lbl_base_url.grid(row=0, column=2, sticky="w")
        self.base_url_entry = ttk.Entry(cfg, textvariable=self.base_url_var, width=36)
        self.base_url_entry.grid(row=0, column=3, padx=6, sticky="w")
        self.base_url_entry.configure()

        self.lbl_model = ttk.Label(cfg, text="模型:")
        self.lbl_model.grid(row=0, column=4, sticky="w")
        self.model_cb = ttk.Combobox(cfg, textvariable=self.model_var, values=XINTIAN_MODELS, width=26,
                                     state="readonly")
        self.model_cb.grid(row=0, column=5, padx=6, sticky="w")

        self.lbl_api_key = ttk.Label(cfg, text="接口密钥(支持多项):")
        self.lbl_api_key.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.api_key_entry = ttk.Entry(cfg, textvariable=self.api_key_var, width=55, show="*")
        self.api_key_entry.grid(row=1, column=1, columnspan=3, padx=6, sticky="w", pady=(6, 0))
        self.api_key_entry.configure()

        self.lbl_mission_root = ttk.Label(cfg, text="任务文件夹:")
        self.lbl_mission_root.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.ent_mission_root = ttk.Entry(cfg, textvariable=self.mission_root_var, width=55)
        self.ent_mission_root.grid(row=2, column=1, columnspan=3, padx=6, sticky="w", pady=(6, 0))
        self.btn_pick_mission_root = ttk.Button(cfg, text="选择", command=self.pick_mission_root)
        self.btn_pick_mission_root.grid(row=2, column=4, sticky="w", pady=(6, 0))

        self.lbl_download_root = ttk.Label(cfg, text="下载文件夹:")
        self.lbl_download_root.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.ent_download_root = ttk.Entry(cfg, textvariable=self.download_root_var, width=55)
        self.ent_download_root.grid(row=3, column=1, columnspan=3, padx=6, sticky="w", pady=(6, 0))
        self.btn_pick_download_root = ttk.Button(cfg, text="选择", command=self.pick_download_root)
        self.btn_pick_download_root.grid(row=3, column=4, sticky="w", pady=(6, 0))

        io = ttk.LabelFrame(self.root, text="第2步：确认任务内容")
        io.pack(fill="x", padx=8, pady=(0, 4))

        mode_bar = ttk.Frame(io)
        mode_bar.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(mode_bar, text="任务方式:").pack(side="left")
        ttk.Radiobutton(mode_bar, text="单个任务", value="single", variable=self.task_input_mode_var).pack(side="left", padx=(8, 4))
        ttk.Radiobutton(mode_bar, text="批量导入", value="batch", variable=self.task_input_mode_var).pack(side="left", padx=4)

        self.manual_input_frame = ttk.LabelFrame(io, text="单个任务")
        self.manual_input_frame.pack(fill="x", padx=8, pady=(4, 4))
        ttk.Label(self.manual_input_frame, text="提示词:").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.manual_input_frame, textvariable=self.prompt_var, width=110).grid(row=0, column=1, columnspan=3, padx=6, sticky="w")
        ttk.Label(self.manual_input_frame, text="图片路径:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(self.manual_input_frame, textvariable=self.image_path_var, width=110).grid(
            row=1, column=1, padx=6, sticky="w", pady=(6, 0)
        )
        ttk.Button(self.manual_input_frame, text="选择图片", command=self.pick_image).grid(row=1, column=2, padx=6, pady=(6, 0))
        ttk.Label(self.manual_input_frame, text="任务名称:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(self.manual_input_frame, textvariable=self.note_var, width=44).grid(row=2, column=1, padx=6, sticky="w", pady=(6, 0))
        ttk.Label(self.manual_input_frame, text="本次任务名称:").grid(row=2, column=2, sticky="e", pady=(6, 0))
        ttk.Entry(self.manual_input_frame, textvariable=self.batch_name_var, width=24).grid(row=2, column=3, sticky="w", padx=(2, 6), pady=(6, 0))

        self.single_extra_frame = ttk.Frame(self.manual_input_frame)
        self.single_extra_frame.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.single_extra_frame._grid_info_cache = dict(self.single_extra_frame.grid_info())
        self.lbl_group = ttk.Label(self.single_extra_frame, text="任务分类:")
        self.lbl_group.pack(side="left")
        self.ent_group = ttk.Entry(self.single_extra_frame, textvariable=self.group_var, width=26)
        self.ent_group.pack(side="left", padx=6)

        self.batch_input_frame = ttk.LabelFrame(io, text="批量导入")
        self.batch_input_frame.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(self.batch_input_frame, text="本次任务名称:").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.batch_input_frame, textvariable=self.batch_name_var, width=32).grid(row=0, column=1, padx=6, sticky="w")
        self.chk_auto_collapse = ttk.Checkbutton(self.batch_input_frame, text="添加后自动收起大组", variable=self.auto_collapse_batch_var)
        self.chk_auto_collapse.grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Label(self.batch_input_frame, text="默认大组名预览:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.batch_name_preview_label = ttk.Label(
            self.batch_input_frame,
            textvariable=self.batch_name_preview_var,
            foreground="#4b5563",
        )
        self.batch_name_preview_label.grid(row=1, column=1, columnspan=2, sticky="w", padx=6, pady=(6, 0))

        self.batch_advanced_frame = ttk.LabelFrame(io, text="批量设置（高级）")
        self.batch_advanced_frame.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(self.batch_advanced_frame, text="每目录最大:").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(self.batch_advanced_frame, from_=0, to=9999, width=6, textvariable=self.mission_n_each_var).grid(
            row=0, column=1, sticky="w", padx=(2, 6)
        )
        ttk.Label(self.batch_advanced_frame, text="(0=不限)").grid(row=0, column=2, sticky="w")
        ttk.Label(self.batch_advanced_frame, text="重复次数:").grid(row=0, column=3, sticky="e")
        ttk.Spinbox(self.batch_advanced_frame, from_=1, to=999, width=6, textvariable=self.repeat_add_var).grid(
            row=0, column=4, sticky="w", padx=(2, 6)
        )
        ttk.Label(self.batch_advanced_frame, text="子目录列表:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(self.batch_advanced_frame, textvariable=self.subdir_name_list_var, width=90).grid(
            row=1, column=1, columnspan=4, padx=6, sticky="w", pady=(6, 0)
        )
        ttk.Label(self.batch_advanced_frame, text="(逗号/分号/换行分隔)").grid(row=1, column=5, sticky="w", pady=(6, 0))
        ttk.Checkbutton(
            self.batch_advanced_frame,
            text="导入前校验当前任务列表中是否已存在这些子目录",
            variable=self.require_existing_subdir_var,
        ).grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))

        act = ttk.LabelFrame(self.root, text="第3步：开始执行")
        act.pack(fill="x", padx=8, pady=4)

        act_top = ttk.Frame(act)
        act_top.pack(fill="x", padx=6, pady=(4, 2))
        act_bottom = ttk.Frame(act)
        act_bottom.pack(fill="x", padx=6, pady=(0, 4))

        self.quick_ops_frame = ttk.LabelFrame(act_top, text="添加与导入")
        self.quick_ops_frame.pack(side="left", padx=(0, 8))
        self.run_ops_frame = ttk.LabelFrame(act_top, text="开始与暂停")
        self.run_ops_frame.pack(side="left", padx=(0, 8))
        self.group_ops_frame = ttk.LabelFrame(act_top, text="分组管理")
        self.group_ops_frame.pack(side="left", padx=(0, 8))
        self.danger_ops_frame = ttk.LabelFrame(act_top, text="危险操作")
        self.danger_ops_frame.pack(side="left")

        self.tools_ops_frame = ttk.LabelFrame(act_bottom, text="历史与工具")
        self.tools_ops_frame.pack(side="left", padx=(0, 8))
        self.download_ops_frame = ttk.LabelFrame(act_bottom, text="批量下载")
        self.download_ops_frame.pack(side="left", padx=(0, 8))
        self.advanced_toggle_frame = ttk.LabelFrame(act_bottom, text="更多")
        self.advanced_toggle_frame.pack(side="left")

        self.btn_add_task = ttk.Button(self.quick_ops_frame, text="添加任务", command=self.add_task)
        self.btn_add_task.pack(side="left", padx=6, pady=6)
        self.btn_load_latest = ttk.Button(self.quick_ops_frame, text="读取最新示例", command=self.load_from_mission_latest)
        self.btn_load_latest.pack(side="left", padx=6, pady=6)
        self.btn_load_latest._pack_info_cache = {"side": "left", "padx": 6, "pady": 6}
        self.btn_import_mission = ttk.Button(self.quick_ops_frame, text="批量导入", command=self.import_mission_all)
        self.btn_import_mission.pack(side="left", padx=6, pady=6)
        self.btn_import_mission._pack_info_cache = {"side": "left", "padx": 6, "pady": 6}

        self.btn_resume_new = ttk.Button(self.run_ops_frame, text="开始执行", command=self.resume_new_tasks)
        self.btn_resume_new.pack(side="left", padx=6, pady=6)
        self.btn_pause_new = ttk.Button(self.run_ops_frame, text="暂停执行", command=self.pause_new_tasks)
        self.btn_pause_new.pack(side="left", padx=6, pady=6)
        self.btn_poll_remote = ttk.Button(self.run_ops_frame, text="刷新选中状态", command=self.poll_selected_remote_id)
        self.btn_poll_remote.pack(side="left", padx=6, pady=6)
        self.chk_start_poll_only = ttk.Checkbutton(
            self.run_ops_frame, text="仅轮询(不发新请求)", variable=self.start_poll_only_var
        )
        self.chk_start_poll_only.pack(side="left", padx=6, pady=6)

        self.btn_collapse_batch = ttk.Button(self.group_ops_frame, text="收起所选分组", command=self.collapse_selected_batches)
        self.btn_collapse_batch.pack(side="left", padx=6, pady=6)
        self.btn_expand_batch = ttk.Button(self.group_ops_frame, text="展开所选分组", command=self.expand_selected_batches)
        self.btn_expand_batch.pack(side="left", padx=6, pady=6)
        self.btn_rename_batch = ttk.Button(self.group_ops_frame, text="修改大组名称", command=self.rename_selected_batch)
        self.btn_rename_batch.pack(side="left", padx=6, pady=6)
        self.btn_move_to_batch = ttk.Button(self.group_ops_frame, text="并入已有大组", command=self.move_selected_to_existing_batch)
        self.btn_move_to_batch.pack(side="left", padx=6, pady=6)
        self.btn_split_to_batch = ttk.Button(self.group_ops_frame, text="拆到新大组", command=self.move_selected_to_new_batch)
        self.btn_split_to_batch.pack(side="left", padx=6, pady=6)

        self.btn_stop_selected = ttk.Button(self.danger_ops_frame, text="停止选中", command=self.stop_selected)
        self.btn_stop_selected.pack(side="left", padx=6, pady=6)
        self.btn_retry_selected = ttk.Button(self.danger_ops_frame, text="重试选中", command=self.retry_selected)
        self.btn_retry_selected.pack(side="left", padx=6, pady=6)
        self.btn_delete_selected = ttk.Button(self.danger_ops_frame, text="删除选中", command=self.delete_selected)
        self.btn_delete_selected.pack(side="left", padx=6, pady=6)

        self.btn_view_history = ttk.Button(self.tools_ops_frame, text="查看历史", command=self.open_task_history_dialog)
        self.btn_view_history.pack(side="left", padx=6, pady=6)
        self.btn_export_tasks = ttk.Button(self.tools_ops_frame, text="导出 tasks.json", command=self.export_tasks_ui)
        self.btn_export_tasks.pack(side="left", padx=6, pady=6)
        self.btn_import_tasks = ttk.Button(self.tools_ops_frame, text="导入 tasks.json", command=self.import_tasks_ui)
        self.btn_import_tasks.pack(side="left", padx=6, pady=6)
        self.btn_self_check = ttk.Button(self.tools_ops_frame, text="自检", command=self.run_ui_self_check)
        self.btn_self_check.pack(side="left", padx=6, pady=6)

        self.download_mode_var = tk.StringVar(value="选中任务")
        self.lbl_download_mode = ttk.Label(self.download_ops_frame, text="下载范围:")
        self.lbl_download_mode.pack(side="left", padx=(6, 4), pady=6)
        self.dl_combo = ttk.Combobox(
            self.download_ops_frame, textvariable=self.download_mode_var, state="readonly", width=26,
            values=["选中任务", "筛选后成功", "筛选后成功或完成(无链接)"]
        )
        self.dl_combo.pack(side="left", padx=4, pady=6)
        ttk.Label(self.download_ops_frame, text="并发:").pack(side="left", padx=(8, 2), pady=6)
        ttk.Entry(self.download_ops_frame, textvariable=self.dl_workers_var, width=5).pack(side="left", pady=6)
        self.btn_download = ttk.Button(self.download_ops_frame, text="下载结果", command=self.batch_download_ui)
        self.btn_download.pack(side="left", padx=6, pady=6)

        self.btn_more_actions = ttk.Button(self.advanced_toggle_frame, text="更多操作", command=self.open_more_actions_dialog)
        self.btn_more_actions.pack(side="left", padx=6, pady=6)
        self.btn_toggle_adv = ttk.Button(self.advanced_toggle_frame, text="显示高级选项", command=self._toggle_advanced_panel)
        self.btn_toggle_adv.pack(side="left", padx=6, pady=6)

        filt = ttk.Frame(self.root)
        self.filt_frame = filt
        filt.pack(fill="x", padx=8, pady=2)

        ttk.Label(filt, text="状态筛选:").pack(side="left")
        self.status_cb = ttk.Combobox(
            filt, textvariable=self.filter_status_var, state="readonly", width=16,
            values=["全部", "排队中", "运行中", "轮询中", "成功", "已下载", "完成(无链接)", "失败", "已停止", "终态失败"]
        )
        self.status_cb.pack(side="left", padx=6)

        ttk.Label(filt, text="平台筛选:").pack(side="left")
        self.provider_filter_cb = ttk.Combobox(
            filt, textvariable=self.filter_provider_var, state="readonly", width=12,
            # include all providers in filter
            values=["全部", "auto", "apiyi", "xintian", "lingke", "toapis", "baoyouhuyu", "jimmy", "dyuapi"]
        )
        self.provider_filter_cb.pack(side="left", padx=6)
        ttk.Label(filt, text="视图:").pack(side="left", padx=(12, 4))
        self.group_view_mode_cb = ttk.Combobox(
            filt, textvariable=self.group_view_mode_var, state="readonly", width=10,
            values=["按大组"]
        )
        self.group_view_mode_cb.pack(side="left")

        ttk.Label(filt, text="搜索:").pack(side="left")
        ttk.Entry(filt, textvariable=self.search_var, width=34).pack(side="left", padx=6)
        ttk.Button(filt, text="应用", command=self.refresh_table_reset_page).pack(side="left")
        ttk.Button(filt, text="清空", command=self.clear_filters).pack(side="left", padx=6)
        ttk.Separator(filt, orient="vertical").pack(side="left", fill="y", padx=8)
        self.page_info_var = tk.StringVar(value="当前显示：全部任务")
        self.page_info_label = ttk.Label(filt, textvariable=self.page_info_var)
        self.page_info_label.pack(side="left", padx=6)

        opt = ttk.Frame(self.root)
        self.opt_frame = opt

        ttk.Checkbutton(opt, text="自动补位重试(默认开)", variable=self.auto_retry_enabled_var).pack(side="left")
        ttk.Checkbutton(opt, text="包含 完成(无链接)", variable=self.auto_retry_include_done_no_url_var).pack(
            side="left", padx=6
        )
        self.chk_safe_mode = ttk.Checkbutton(opt, text="傻瓜防浪费模式", variable=self.safe_mode_var)
        self.chk_safe_mode.pack(side="left", padx=6)
        ttk.Checkbutton(opt, text="终态失败自动补位", variable=self.auto_replace_terminal_var).pack(
            side="left")

        ttk.Separator(opt, orient="vertical").pack(side="left", fill="y", padx=10)

        ttk.Checkbutton(opt, text="队列闸门", variable=self.gate_enabled_var).pack(side="left")
        ttk.Label(opt, text="RemoteID 上限:").pack(side="left", padx=(8, 2))
        ttk.Entry(opt, textvariable=self.gate_remote_limit_var, width=6).pack(side="left")
        ttk.Label(opt, text="链接上限:").pack(side="left", padx=(8, 2))
        ttk.Entry(opt, textvariable=self.gate_link_limit_var, width=6).pack(side="left")
        ttk.Label(opt, text="最大并发:").pack(side="left", padx=(12, 2))
        ttk.Entry(opt, textvariable=self.max_concurrency_var, width=6).pack(side="left")
        ttk.Button(opt, text="应用", command=self.apply_concurrency).pack(side="left", padx=6)

        ttk.Separator(self.root).pack(fill="x", padx=8, pady=4)

        mid = ttk.Panedwindow(self.root, orient="horizontal")
        mid.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.Frame(mid)
        right = ttk.Frame(mid)

        # keep table wider than log panel
        mid.add(left, weight=3)
        mid.add(right, weight=2)

        self.cols = (
            "status",
            "progress",
            "task_count",
            "done_count",
            "running_count",
            "queued_count",
            "failed_count",
            "downloaded_count",
            "status_msg",
            "created_at",
            "completed_at",
        )
        self.tree = ttk.Treeview(left, columns=self.cols, show="tree headings", height=13)
        self.tree.heading("#0", text="大组任务")

        self.tree.column("#0", width=360, minwidth=280, stretch=True, anchor="w")

        col_titles = {
            "status": "整体状态",
            "progress": "完成百分比",
            "task_count": "任务总数",
            "done_count": "已完成",
            "running_count": "进行中",
            "queued_count": "等待中",
            "failed_count": "失败/停止",
            "downloaded_count": "已下载",
            "status_msg": "状态信息",
            "created_at": "开始时间",
            "completed_at": "完成时间",
        }

        # base widths
        self._col_base = {
            "status": 120,
            "progress": 100,
            "task_count": 80,
            "done_count": 80,
            "running_count": 80,
            "queued_count": 80,
            "failed_count": 90,
            "downloaded_count": 80,
            "status_msg": 260,
            "created_at": 140,
            "completed_at": 140,
        }

        self._col_flex_weight = {
            "status_msg": 3,
            "status": 1,
        }

        for c in self.cols:
            self.tree.heading(c, text=col_titles.get(c, c))
            base_w = int(self._col_base.get(c, 120))
            self.tree.column(
                c,
                width=base_w,
                minwidth=60,
                stretch=(c in self._col_flex_weight),
                anchor="w"
            )

        ysb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        self.log = tk.Text(right, height=13, wrap="word")
        self.log.pack(fill="both", expand=True)

        foot = ttk.Frame(self.root)
        self.pause_label = ttk.Label(foot, text="")
        self.pause_label.pack(side="left", padx=12)
        self.status_overview_label = ttk.Label(foot, text="状态总览: -")
        self.status_overview_label.pack(side="left", padx=12)
        foot.pack(fill="x", padx=8, pady=4)
        self.bill_label = ttk.Label(foot, text="计费: -")
        self.bill_label.pack(side="left")
        ttk.Button(foot, text="打开视频", command=self.open_selected_url).pack(side="right")

        # role-based controls
        self._admin_only_widgets = [
            self.provider_cb, self.model_cb, self.base_url_entry, self.api_key_entry,
            self.btn_pick_mission_root, self.btn_pick_download_root,
        ]
        self._tech_grid_widgets = [
            self.lbl_provider, self.provider_cb,
            self.lbl_base_url, self.base_url_entry,
            self.lbl_model, self.model_cb,
            self.lbl_api_key, self.api_key_entry,
        ]
        self._path_grid_widgets = [
            self.lbl_mission_root, self.ent_mission_root, self.btn_pick_mission_root,
            self.lbl_download_root, self.ent_download_root, self.btn_pick_download_root,
        ]
        for w in self._tech_grid_widgets + self._path_grid_widgets:
            try:
                w._grid_info_cache = dict(w.grid_info())
            except Exception:
                pass
        self._normal_lock_buttons = [
            self.btn_stop_selected, self.btn_retry_selected, self.btn_delete_selected,
            self.btn_export_tasks, self.btn_import_tasks, self.btn_view_history,
        ]
        self._apply_role_mode()

    def _bind_events(self):
        self.provider_cb.bind("<<ComboboxSelected>>", lambda e: self._sync_provider_ui(reset_base=True, reset_model=True))
        self.status_cb.bind("<<ComboboxSelected>>", lambda e: self._ui_call(self.refresh_table_reset_page))
        self.provider_filter_cb.bind("<<ComboboxSelected>>", lambda e: self._ui_call(self.refresh_table_reset_page))
        self.group_view_mode_cb.bind("<<ComboboxSelected>>", lambda e: self._ui_call(self.refresh_table_reset_page))
        self.search_var.trace_add("write", lambda *args: self._debounced_refresh())
        self.batch_name_var.trace_add("write", lambda *_: self._update_batch_name_preview())
        self.group_var.trace_add("write", lambda *_: self._update_batch_name_preview())
        self.subdir_name_list_var.trace_add("write", lambda *_: self._update_batch_name_preview())
        self.task_input_mode_var.trace_add("write", lambda *_: self._apply_task_input_mode())
        self.tree.bind("<ButtonPress-1>", self._on_tree_mouse_down, add="+")
        self.tree.bind("<ButtonRelease-1>", self._on_tree_mouse_up, add="+")
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select_fill_form)
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open_close)
        self.tree.bind("<<TreeviewClose>>", self._on_tree_open_close)
        self.root.bind("<Configure>", lambda e: self._schedule_autofit_columns())
        for v in (
                self.mission_root_var,
                self.download_root_var,
                self.provider_var,
                self.base_url_var,
                self.model_var,
                self.max_concurrency_var,
                self.filter_status_var,
                self.filter_provider_var,
                self.download_mode_var,
        ):
            v.trace_add("write", lambda *_: self._schedule_save_user_prefs())
        self._update_batch_name_preview()
        self._apply_task_input_mode()

    def _apply_task_input_mode(self):
        mode = (self.task_input_mode_var.get() or "single").strip()
        is_batch = (mode == "batch")
        self._set_widget_visible(self.manual_input_frame, not is_batch, fill="x", padx=8, pady=(4, 4))
        self._set_widget_visible(self.batch_input_frame, is_batch, fill="x", padx=8, pady=(0, 4))
        self._set_widget_visible(self.batch_advanced_frame, is_batch and (not self.novice_mode_var.get()), fill="x", padx=8, pady=(0, 6))
        self._set_widget_visible(self.single_extra_frame, (not is_batch) and (not self.novice_mode_var.get()))
        try:
            if is_batch:
                self.primary_hint_label.configure(text="下一步：检查批量导入参数，然后点击“批量导入”。")
            else:
                self.primary_hint_label.configure(text="下一步：填写提示词和图片，然后点击“添加任务”。")
        except Exception:
            pass

    def _on_tree_mouse_down(self, event):
        try:
            region = self.tree.identify_region(event.x, event.y)
            self._tree_sep_dragging = (region == "separator")
        except Exception:
            self._tree_sep_dragging = False

    def _on_tree_mouse_up(self, event):
        try:
            if getattr(self, "_tree_sep_dragging", False):
                self._manual_col_resize = True
                self._schedule_save_user_prefs()
        finally:
            self._tree_sep_dragging = False

    def _load_user_prefs(self):
        p = USER_PREFS_FILE
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
        except Exception:
            return

        self._prefs_loading = True
        try:
            geom = (data.get("window_geometry", "") or "").strip()
            if geom:
                try:
                    self.root.geometry(self._fit_geometry_to_screen(geom))
                except Exception:
                    pass

            v = (data.get("mission_root", "") or "").strip()
            if v:
                self.mission_root_var.set(norm_path(v))
            v = (data.get("download_root", "") or "").strip()
            if v:
                self.download_root_var.set(norm_path(v))

            provider = (data.get("provider", "") or "").strip()
            if provider:
                self.provider_var.set(provider)
                self._sync_provider_ui()
            base_url = (data.get("base_url", "") or "").strip()
            if base_url:
                self.base_url_var.set(base_url)
            model = (data.get("model", "") or "").strip()
            if model:
                self.model_var.set(model)

            maxc = str(data.get("max_concurrency", "") or "").strip()
            if maxc:
                self.max_concurrency_var.set(maxc)

            fs = (data.get("filter_status", "") or "").strip()
            if fs:
                self.filter_status_var.set(fs)
            fp = (data.get("filter_provider", "") or "").strip()
            if fp:
                self.filter_provider_var.set(fp)

            dm = (data.get("download_mode", "") or "").strip()
            if dm:
                self.download_mode_var.set(dm)

            c0 = data.get("col0_width")
            if isinstance(c0, int) and c0 > 80:
                try:
                    self.tree.column("#0", width=int(c0))
                except Exception:
                    pass
            col_widths = data.get("col_widths", {})
            if isinstance(col_widths, dict):
                for c in self.cols:
                    w = col_widths.get(c)
                    if isinstance(w, int) and w > 40:
                        try:
                            self.tree.column(c, width=int(w))
                        except Exception:
                            pass
            self._manual_col_resize = bool(data.get("manual_col_resize", False)) or bool(col_widths)
        finally:
            self._prefs_loading = False

    def _collect_user_prefs(self) -> dict:
        col_widths = {}
        for c in self.cols:
            try:
                col_widths[c] = int(self.tree.column(c, "width"))
            except Exception:
                pass
        try:
            c0 = int(self.tree.column("#0", "width"))
        except Exception:
            c0 = 0
        return {
            "window_geometry": str(self.root.winfo_geometry()),
            "mission_root": (self.mission_root_var.get() or "").strip(),
            "download_root": (self.download_root_var.get() or "").strip(),
            "provider": (self.provider_var.get() or "").strip(),
            "base_url": (self.base_url_var.get() or "").strip(),
            "model": (self.model_var.get() or "").strip(),
            "max_concurrency": (self.max_concurrency_var.get() or "").strip(),
            "filter_status": (self.filter_status_var.get() or "").strip(),
            "filter_provider": (self.filter_provider_var.get() or "").strip(),
            "download_mode": (self.download_mode_var.get() or "").strip(),
            "manual_col_resize": bool(self._manual_col_resize),
            "col0_width": c0,
            "col_widths": col_widths,
            "saved_at": now_str(),
        }

    def _save_user_prefs(self):
        if self._prefs_loading or self._closing:
            return
        try:
            USER_PREFS_DIR.mkdir(parents=True, exist_ok=True)
            p = USER_PREFS_FILE
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._collect_user_prefs(), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass

    def _schedule_save_user_prefs(self, delay_ms: int = 400):
        if self._prefs_loading or self._closing:
            return
        try:
            if self._prefs_save_timer is not None:
                self.root.after_cancel(self._prefs_save_timer)
        except Exception:
            pass
        self._prefs_save_timer = self.root.after(delay_ms, self._save_user_prefs)

    def _ui_call(self, fn, *args, **kwargs):
        """Run UI work on the Tk main thread."""
        if self._closing:
            return
        if self._on_main_thread():
            try:
                fn(*args, **kwargs)
            except Exception:
                pass
            return
        self._enqueue_ui(fn, *args, **kwargs)

    def _debounced_refresh(self):
        if self._closing:
            return
        if hasattr(self, "_refresh_timer"):
            self.root.after_cancel(self._refresh_timer)
        self._refresh_timer = self.root.after(250, self.refresh_table)

    def _request_flush_dirty(self):
        if self._closing:
            return
        if self._ui_flush_pending:
            return
        self._ui_flush_pending = True
        self.root.after(100, self._flush_dirty_rows)

    def _apply_row_values_fast(self, t: TaskItem):
        st = (t.status or "").strip()
        if st in ("Queued", "Running", "Pending(Check)", "Success", "Downloaded", "Done(No URL)", "Stopped"):
            if st != "Failed":
                t.status_msg = ""

        provider = (t.provider or "").strip()
        model = (t.model or "").strip()
        status_msg = (t.status_msg or "").strip()
        note = (t.note or "").strip()
        group = (t.group or "").strip()
        created_at = (t.created_at or "").strip()
        completed_at = (getattr(t, "completed_at", "") or "").strip()
        next_retry_at = (getattr(t, "next_retry_at", "") or "").strip()
        remote_id = (t.remote_id or "").strip()
        video_url = (t.video_url or "").strip()

        try:
            prog = float(getattr(t, "progress", 0.0) or 0.0)
        except Exception:
            prog = 0.0

        self.tree.item(t.task_id, values=(
            t.task_id,
            self._display_status(t),
            next_retry_at,
            provider,
            model,
            status_msg,
            f"{prog:.1f}%",
            note,
            group,
            created_at,
            completed_at,
            remote_id,
            video_url
        ))

    @staticmethod
    def _is_group_iid(iid: str) -> bool:
        return isinstance(iid, str) and iid.startswith("group_")

    @staticmethod
    def _task_counts_as_completed(t: TaskItem) -> bool:
        return (t.status or "") in ("Success", "Downloaded", "Done(No URL)")

    @staticmethod
    def _task_counts_as_failed(t: TaskItem) -> bool:
        return (t.status or "") in ("Failed", "Failed(Terminal)", "Stopped")

    def _batch_progress_percent(self, task_list: list[TaskItem]) -> float:
        total = len(task_list)
        if total <= 0:
            return 0.0
        done = sum(1 for t in task_list if self._task_counts_as_completed(t))
        return round((done * 100.0) / total, 1)

    def _batch_status_text(self, task_list: list[TaskItem]) -> str:
        if not task_list:
            return "无任务"
        total = len(task_list)
        done = sum(1 for t in task_list if self._task_counts_as_completed(t))
        running = sum(1 for t in task_list if (t.status or "") in ("Running", "Pending(Check)"))
        queued = sum(1 for t in task_list if (t.status or "") == "Queued")
        failed = sum(1 for t in task_list if self._task_counts_as_failed(t))
        if done >= total:
            downloaded = sum(1 for t in task_list if (t.status or "") == "Downloaded")
            if downloaded >= total:
                return "已全部下载"
            return "已全部完成"
        if running > 0:
            return "执行中"
        if queued > 0 and done == 0 and failed == 0:
            return "等待执行"
        if failed > 0 and done == 0 and running == 0:
            return "执行异常"
        if done > 0:
            return "部分完成"
        return "等待执行"

    def _batch_status_message(self, task_list: list[TaskItem]) -> str:
        if not task_list:
            return ""
        running_msgs = [
            (getattr(t, "status_msg", "") or "").strip()
            for t in task_list
            if (t.status or "") in ("Running", "Pending(Check)")
            and (getattr(t, "status_msg", "") or "").strip()
        ]
        if running_msgs:
            return running_msgs[0]
        error_msgs = [
            (getattr(t, "status_msg", "") or "").strip()
            for t in task_list
            if self._task_counts_as_failed(t) and (getattr(t, "status_msg", "") or "").strip()
        ]
        if error_msgs:
            return error_msgs[0]
        if all(self._task_counts_as_completed(t) for t in task_list):
            return "大组内任务已全部完成，可查看明细或下载结果。"
        queued = sum(1 for t in task_list if (t.status or "") == "Queued")
        if queued:
            return f"还有 {queued} 个任务等待执行。"
        return ""

    def _batch_completed_at(self, task_list: list[TaskItem]) -> str:
        completed_times = [
            (getattr(t, "completed_at", "") or "").strip()
            for t in task_list
            if (getattr(t, "completed_at", "") or "").strip()
        ]
        if not completed_times:
            return ""
        if all(self._task_counts_as_completed(t) for t in task_list):
            return max(completed_times)
        return ""

    def _batch_row_values(self, task_list: list[TaskItem]) -> tuple:
        total = len(task_list)
        done = sum(1 for t in task_list if self._task_counts_as_completed(t))
        running = sum(1 for t in task_list if (t.status or "") in ("Running", "Pending(Check)"))
        queued = sum(1 for t in task_list if (t.status or "") == "Queued")
        failed = sum(1 for t in task_list if self._task_counts_as_failed(t))
        downloaded = sum(1 for t in task_list if (t.status or "") == "Downloaded")
        created_times = [(t.created_at or "").strip() for t in task_list if (t.created_at or "").strip()]
        return (
            self._batch_status_text(task_list),
            f"{self._batch_progress_percent(task_list):.1f}%",
            str(total),
            str(done),
            str(running),
            str(queued),
            str(failed),
            str(downloaded),
            self._batch_status_message(task_list),
            min(created_times) if created_times else "",
            self._batch_completed_at(task_list),
        )

    def _row_values(self, t: TaskItem) -> tuple:
        return (
            self._display_status(t),
            f"{float(getattr(t, 'progress', 0.0) or 0.0):.1f}%",
            "1",
            "1" if self._task_counts_as_completed(t) else "0",
            "1" if (t.status or "") in ("Running", "Pending(Check)") else "0",
            "1" if (t.status or "") == "Queued" else "0",
            "1" if self._task_counts_as_failed(t) else "0",
            "1" if (t.status or "") == "Downloaded" else "0",
            getattr(t, "status_msg", "") or "",
            t.created_at,
            getattr(t, "completed_at", "") or "",
        )

    # ---------- double click ----------
    def _on_tree_double_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if self._is_group_iid(item):
            meta = getattr(self, "_tree_group_meta", {}).get(item) or {}
            batch_id = (meta.get("batch_id", "") or "").strip()
            if batch_id:
                self.open_current_batch_detail_window(batch_id, meta.get("task_ids") or [])
            return
        try:
            t = self.tasks.get(item)
            if not t:
                return
            url = (getattr(t, "video_url", "") or "").strip()
            if url:
                webbrowser.open(url)
        except Exception:
            return

    def _on_tree_open_close(self, event=None):
        try:
            iid = self.tree.focus()
        except Exception:
            return
        if not self._is_group_iid(iid):
            return
        try:
            is_open = bool(self.tree.item(iid, "open"))
        except Exception:
            return
        meta = getattr(self, "_tree_group_meta", {}).get(iid)
        if not meta:
            return
        mode = meta.get("mode", self._current_group_mode())
        group_key = meta.get("collapse_key", "")
        if not group_key:
            return
        if is_open:
            self._collapsed_group_keys_by_mode.setdefault(mode, set()).discard(group_key)
        else:
            self._collapsed_group_keys_by_mode.setdefault(mode, set()).add(group_key)

    def collapse_selected_batches(self):
        changed = 0
        for iid in list(self.tree.selection() or []):
            if not self._is_group_iid(iid):
                continue
            meta = getattr(self, "_tree_group_meta", {}).get(iid)
            if not meta:
                continue
            mode = meta.get("mode", self._current_group_mode())
            group_key = meta.get("collapse_key", "")
            if not group_key:
                continue
            self._collapsed_group_keys_by_mode.setdefault(mode, set()).add(group_key)
            try:
                self.tree.item(iid, open=False)
            except Exception:
                pass
            changed += 1
        if changed:
            self.log_line(f"已收起大组：{changed}\n")

    def expand_selected_batches(self):
        changed = 0
        for iid in list(self.tree.selection() or []):
            if not self._is_group_iid(iid):
                continue
            meta = getattr(self, "_tree_group_meta", {}).get(iid)
            if not meta:
                continue
            mode = meta.get("mode", self._current_group_mode())
            group_key = meta.get("collapse_key", "")
            if not group_key:
                continue
            self._collapsed_group_keys_by_mode.setdefault(mode, set()).discard(group_key)
            try:
                self.tree.item(iid, open=True)
            except Exception:
                pass
            changed += 1
        if changed:
            self.log_line(f"已展开大组：{changed}\n")

    def _all_batch_choices(self) -> list[tuple[str, str]]:
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for t in sorted(self.tasks.values(), key=lambda x: (self._task_batch_name(x), x.batch_id, x.task_id)):
            batch_id = (getattr(t, "batch_id", None) or "LEGACY").strip() or "LEGACY"
            if batch_id in seen:
                continue
            seen.add(batch_id)
            out.append((batch_id, self._task_batch_name(t)))
        return out

    def _choose_existing_batch_dialog(self, choices: list[tuple[str, str]]) -> tuple[str, str] | None:
        if not choices:
            return None
        dlg = tk.Toplevel(self.root)
        dlg.title("选择已有大组")
        dlg.geometry("560x420")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text="请选择要加入的大组：").pack(anchor="w", padx=10, pady=(10, 6))
        frame = ttk.Frame(dlg)
        frame.pack(fill="both", expand=True, padx=10, pady=6)
        lb = tk.Listbox(frame, selectmode="browse")
        sb = ttk.Scrollbar(frame, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for bid, name in choices:
            lb.insert("end", f"{name} | {bid}")
        if choices:
            lb.selection_set(0)

        result: dict[str, tuple[str, str] | None] = {"value": None}

        def _ok():
            sel = lb.curselection()
            if not sel:
                return
            bid, name = choices[sel[0]]
            if not messagebox.askyesno("确认加入", f"确认将选中的任务加入大组：\n\n{name}\n{bid}"):
                return
            result["value"] = (bid, name)
            dlg.destroy()

        def _cancel():
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btns, text="取消", command=_cancel).pack(side="right")
        ttk.Button(btns, text="确认加入", command=_ok).pack(side="right", padx=(0, 6))

        dlg.wait_window()
        return result["value"]

    def _reassign_selected_tasks_to_batch(self, batch_id: str, batch_name: str):
        tids = self.selected_task_ids()
        if not tids:
            return
        changed = 0
        batch_id = (batch_id or "").strip() or "LEGACY"
        batch_name = (batch_name or "").strip() or batch_id
        for tid in tids:
            t = self.tasks.get(tid)
            if not t:
                continue
            old_batch = (getattr(t, "batch_id", None) or "LEGACY").strip() or "LEGACY"
            old_name = (getattr(t, "batch_name", "") or "").strip()
            if old_batch == batch_id and old_name == batch_name:
                continue
            self.idx.remove_task(t)
            t.batch_id = batch_id
            t.batch_name = batch_name
            self.idx.add_task(t)
            self.mark_dirty(tid)
            changed += 1
        if changed:
            self._mark_tasks_dirty()
            self._ui_call(self.refresh_table)
            self.log_line(f"已移动任务到大组：{batch_name} | 数量={changed}\n")

    def move_selected_to_existing_batch(self):
        if not self._require_admin("合并到已有大组"):
            return
        tids = self.selected_task_ids()
        if not tids:
            return
        choices = self._all_batch_choices()
        if not choices:
            messagebox.showinfo("无可选大组", "当前没有可用的大组。")
            return
        picked = self._choose_existing_batch_dialog(choices)
        if not picked:
            return
        bid, name = picked
        self._reassign_selected_tasks_to_batch(bid, name)

    def move_selected_to_new_batch(self):
        if not self._require_admin("拆分到新大组"):
            return
        tids = self.selected_task_ids()
        if not tids:
            return
        batch_name = simpledialog.askstring("选中新建大组", "请输入新的大组名称：")
        if not batch_name:
            return
        batch_name = batch_name.strip()
        if not batch_name:
            return
        batch_id = self._new_batch_id()
        self._reassign_selected_tasks_to_batch(batch_id, batch_name)

    def rename_selected_batch(self):
        if not self._require_admin("修改大组名称"):
            return
        tids = self.selected_task_ids()
        if not tids:
            return
        batch_ids = {
            (getattr(self.tasks.get(tid), "batch_id", None) or "LEGACY").strip() or "LEGACY"
            for tid in tids if self.tasks.get(tid)
        }
        if len(batch_ids) != 1:
            messagebox.showwarning("无法修改", "请只选择同一个大组内的任务或该大组本身。")
            return
        batch_id = next(iter(batch_ids))
        sample = next((self.tasks.get(tid) for tid in tids if self.tasks.get(tid)), None)
        old_name = self._task_batch_name(sample) if sample else batch_id
        new_name = simpledialog.askstring("修改大组名称", "请输入新的大组名称：", initialvalue=old_name)
        if not new_name:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old_name:
            return
        changed = 0
        for t in self.tasks.values():
            bid = (getattr(t, "batch_id", None) or "LEGACY").strip() or "LEGACY"
            if bid != batch_id:
                continue
            t.batch_name = new_name
            changed += 1
        for rec in self.task_history:
            if (rec.get("batch_id", "") or "").strip() != batch_id:
                continue
            rec["batch_name"] = new_name
            items = rec.get("items", [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        item["batch_name"] = new_name
        if changed:
            self._mark_tasks_dirty()
            self._save_task_history()
            self._ui_call(self.refresh_table)
            self.log_line(f"已修改大组名称：{old_name} -> {new_name} | 任务数={changed}\n")

    def _restore_tasks_from_snapshot(self, task_dicts: list[dict], task_counter: int):
        self.tasks = {}
        for d in task_dicts:
            try:
                t = TaskItem.from_persist_dict(d)
                self.tasks[t.task_id] = t
            except Exception:
                continue
        self.task_counter = task_counter
        self.idx.build_from_tasks(self.tasks)
        self.refresh_table()

    def run_ui_self_check(self):
        if not self._require_admin("运行自检"):
            return

        snapshot_tasks = []
        for t in self.tasks.values():
            try:
                snapshot_tasks.append(t.to_persist_dict())
            except Exception:
                pass
        snapshot_counter = self.task_counter
        snapshot_view_mode = self._current_group_mode()
        snapshot_collapsed = {
            k: set(v) for k, v in (self._collapsed_group_keys_by_mode or {}).items()
        }

        failures: list[str] = []
        checks: list[str] = []

        def expect(cond: bool, label: str):
            if cond:
                checks.append(label)
            else:
                failures.append(label)

        def mk_task(*, tid: str, batch_id: str, batch_name: str, group: str, note: str) -> TaskItem:
            t = TaskItem(
                task_id=tid,
                provider="jimmy",
                base_url="https://example.invalid",
                model="self-check-model",
                prompt=f"prompt for {note}",
                image_path="C:\\self-check.png",
                note=note,
                group=group,
                log_file=str(Path(LOG_DIR) / f"{tid}.log.txt"),
                created_at=now_str(),
                batch_id=batch_id,
                batch_name=batch_name,
            )
            self._init_task_fields(t)
            t.status = "Queued"
            return t

        try:
            self.tasks = {}
            self.idx.clear()
            self.task_counter = 900000
            self._collapsed_group_keys_by_mode = {"按大组": set(), "按小分组": set()}

            tmp_tasks = [
                mk_task(tid="T90001", batch_id="B_SELF_1", batch_name="大组甲", group="小组A", note="a1"),
                mk_task(tid="T90002", batch_id="B_SELF_1", batch_name="大组甲", group="小组B", note="a2"),
                mk_task(tid="T90003", batch_id="B_SELF_2", batch_name="大组乙", group="小组A", note="b1"),
                mk_task(tid="T90004", batch_id="B_SELF_2", batch_name="大组乙", group="小组B", note="b2"),
            ]
            for t in tmp_tasks:
                self.tasks[t.task_id] = t
                self.idx.add_task(t)

            self.group_view_mode_var.set("按大组")
            self.refresh_table()
            top_groups = list(self.tree.get_children())
            expect(len(top_groups) == 2, "按大组显示 2 个顶层组")

            if top_groups:
                self.tree.selection_set(top_groups[0])
                expect(len(self.selected_task_ids()) == 2, "选中一个大组时返回该组全部任务")

            t1 = self.tasks["T90001"]
            self._set_task_status(t1, "Running")
            self.mark_dirty(t1.task_id)
            self._flush_dirty_rows()
            vals = self.tree.item(t1.task_id, "values")
            expect(bool(vals) and ("运行中" in str(vals[1]) or "Running" in str(vals[1])), "状态脏更新后仍在当前父组内")

            self._reassign_selected_tasks_to_batch("B_SELF_2", "大组乙")
            self.refresh_table()
            top_groups = list(self.tree.get_children())
            counts = []
            for x in top_groups:
                self.tree.selection_set(x)
                counts.append(len(self.selected_task_ids()))
            expect(len(top_groups) == 1 and counts == [4], "合并到已有大组后顶层计数正确")

            self.group_view_mode_var.set("按小分组")
            self.refresh_table()
            top_groups = list(self.tree.get_children())
            expect(len(top_groups) == 2, "按小分组显示 2 个顶层组")

            self.tree.selection_set("T90004")
            self._reassign_selected_tasks_to_batch("B_SELF_3", "大组丙")
            self.group_view_mode_var.set("按大组")
            self.refresh_table()
            top_groups = list(self.tree.get_children())
            counts = []
            for x in top_groups:
                self.tree.selection_set(x)
                counts.append(len(self.selected_task_ids()))
            counts.sort()
            expect(len(top_groups) == 2 and counts == [1, 3], "拆分到新大组后顶层计数正确")

            t4 = self.tasks.pop("T90004", None)
            if t4:
                self.idx.remove_task(t4)
            self.mark_dirty("T90004")
            self._flush_dirty_rows()
            top_groups = list(self.tree.get_children())
            counts = []
            for x in top_groups:
                self.tree.selection_set(x)
                counts.append(len(self.selected_task_ids()))
            counts.sort()
            expect(len(top_groups) == 1 and counts == [3], "删除任务后空大组会自动移除")

            result = "通过" if not failures else "失败"
            detail = "\n".join(f"- {x}" for x in (checks if not failures else failures[:12]))
            self.log_line(
                f"UI自检完成：{result} | 成功 {len(checks)} 项 | 失败 {len(failures)} 项\n"
            )
            if failures:
                messagebox.showwarning("UI自检失败", f"失败 {len(failures)} 项：\n{detail}")
            else:
                messagebox.showinfo("UI自检通过", f"通过 {len(checks)} 项检查：\n{detail}")
        finally:
            self._collapsed_group_keys_by_mode = snapshot_collapsed
            self.group_view_mode_var.set(snapshot_view_mode)
            self._restore_tasks_from_snapshot(snapshot_tasks, snapshot_counter)

    # ---------- pause/resume ----------
    def pause_new_tasks(self):
        self.pause_new_tasks_var.set(True)
        self.log_line("\n已暂停：调度器不会启动新的排队任务。\n")

    def resume_new_tasks(self):
        poll_only = bool(self.start_poll_only_var.get())
        if not self._ensure_runtime_api_key("开始运行任务"):
            return
        if poll_only:
            self.pause_new_tasks_var.set(True)
            self.log_line("\n已进入仅轮询模式：不会启动新任务，只轮询已有 remote_id。\n")
            self._schedule_poll_tick(0, force=True)
        else:
            self.pause_new_tasks_var.set(False)
            self.log_line("\n已恢复：调度器将分批创建远端ID并轮询。\n")
            # in two-phase mode this submits create_only jobs (no immediate polling)
            self.start_all_queued()

        # ensure poll timer is running
        self._ensure_poll_timer()

    def _ensure_poll_timer(self):
        self._schedule_poll_tick(self.poll_interval_ms)

    def _tick_poll_batch(self):
        # Backward-compat entrypoint; centralized polling tick is _tick_poll_once_all.
        self._tick_poll_once_all()

    def _poll_once_task(self, t: TaskItem, api_key=None):
        # 1)  api_key / rid 
        rid = (t.remote_id or "").strip()
        if not rid:
            print(">>> poll skip: empty remote_id", t.task_id)
            return

        # prevent duplicate polling while future is still running
        if getattr(t, "future", None) is not None:
            try:
                if t.future and not t.future.done():
                    print(">>> poll skip: future running", t.task_id)
                    return
            except Exception:
                pass

        provider = (t.provider or "").strip()
        if provider == "auto":
            provider = "xintian"
        if not provider:
            provider = "xintian"
        if provider not in ("xintian", "baoyouhuyu", "jimmy", "dyuapi"):
            print(">>> poll skip: provider not supported", provider, t.task_id)
            return
        api_key, resolved_base_url = self._resolve_task_route(t, provider, api_key)
        api_key = (api_key or "").strip()
        if not api_key:
            print(">>> poll skip: empty api_key", t.task_id)
            return
        if not self._try_acquire_active_slot("poll", t.task_id, self._poll_submit_limit()):
            return

        # 4) Enter polling state
        if not getattr(t, "stop_event", None):
            t.stop_event = threading.Event()

        self._set_task_status(t, "Pending(Check)")
        self._ui_update_task_row(t)

        def on_text(s: str):
            self._append_task_log(t, s)
            p = parse_progress_percent(s)
            if p is None:
                m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", s or "")
                if m:
                    try:
                        p = float(m.group(1))
                    except Exception:
                        p = None
            if p is not None:
                try:
                    if p < 0:
                        p = 0.0
                    if p > 100:
                        p = 100.0
                    t.progress = float(p)
                    self._ui_update_task_row(t)
                except Exception:
                    pass

        def on_done_or_none(video_url: str | None):
            if video_url:
                self._set_video_url(t, video_url)
                self._set_task_status(t, "Success")
                t.progress = 100.0
            else:
                self._set_task_status(t, "Pending(Check)")
            self._ui_update_task_row(t)

        def on_error(err: str):
            self._set_task_status(t, "Failed")
            t.last_error = (err or "").strip()
            t.status_msg = compact_status_msg(t.last_error) or "轮询失败"
            self._append_task_log(t, f"\nERROR(poll_once): {t.last_error}\n")

            if (t.remote_id or "").strip() and t.last_error.startswith("task_failed:"):
                self._recreate_task_after_poll_failed(t, t.last_error)
                return

            # Terminal error with remote_id -> stop polling + spawn replacement
            if (t.remote_id or "").strip() and is_poll_terminal_failure(t.last_error):
                self._mark_terminal_failure(t)
                self._set_task_status(t, "Failed(Terminal)")
                self._maybe_enqueue_replacement(t, t.last_error)
            else:
                self._ensure_retry_scheduled(t)

            self._ui_update_task_row(t)

        def run():
            try:
                if provider == "xintian":
                    cfg = XintianConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                    )

                    # local import to avoid missing symbol issues
                    from xv_gui.providers.xintian import run_xintian_poll_once

                    # compatible call signatures
                    try:
                        run_xintian_poll_once(cfg, rid, on_text, on_done_or_none, on_error, t.stop_event)
                    except TypeError:
                        run_xintian_poll_once(cfg, rid, on_text, on_done_or_none, on_error)
                elif provider == "baoyouhuyu":
                    cfg = BaoyouhuyuConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                    )
                    try:
                        run_baoyouhuyu_poll_once(
                            cfg, rid, on_text, on_done_or_none, on_error, t.stop_event
                        )
                    except TypeError:
                        run_baoyouhuyu_poll_once(cfg, rid, on_text, on_done_or_none, on_error)
                elif provider == "jimmy":
                    cfg = JimmyConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                    )
                    try:
                        run_jimmy_poll_once(cfg, rid, on_text, on_done_or_none, on_error, t.stop_event)
                    except TypeError:
                        run_jimmy_poll_once(cfg, rid, on_text, on_done_or_none, on_error)
                else:
                    cfg = DyuapiConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                    )
                    try:
                        run_dyuapi_poll_once(cfg, rid, on_text, on_done_or_none, on_error, t.stop_event)
                    except TypeError:
                        run_dyuapi_poll_once(cfg, rid, on_text, on_done_or_none, on_error)

            except Exception as e:
                on_error(str(e))
            finally:
                self._release_active_slot("poll", t.task_id)
                self._ui_call(self._drain_poll_queue)

        t.future = self.poll_executor.submit(run)

    # ---------- select fill ----------
    def _on_tree_select_fill_form(self, event=None):
        sels = list(self.tree.selection() or [])
        if not sels:
            return
        iid = sels[0]
        if self._is_group_iid(iid):
            return
        t = self.tasks.get(iid)
        if not t:
            return

        self.provider_var.set(t.provider if t.provider else "jimmy")
        self.model_var.set(getattr(t, "model", "") or "")
        self.prompt_var.set(getattr(t, "prompt", "") or "")
        self.image_path_var.set(norm_path(getattr(t, "image_path", "") or ""))
        self.note_var.set(getattr(t, "note", "") or "")
        self.group_var.set(display_last_part(getattr(t, "group", "") or ""))
        self._sync_provider_ui()

    # ---------- logging ----------
    def log_line(self, s: str):
        # worker threads enqueue log work; Text mutation stays on the Tk thread
        if not self._on_main_thread():
            self._enqueue_ui(self.log_line, s)
            return
        try:
            if not s.endswith("\n"):
                s += "\n"
            self._log_buf.append(s)
            if self._log_flush_pending:
                return
            self._log_flush_pending = True
            self.root.after(200, self._flush_log_buf)
        except Exception:
            pass

    def _flush_log_buf(self):
        self._log_flush_pending = False
        if not getattr(self, "_log_buf", None):
            return
        if not self._log_buf:
            return
        chunk = "".join(self._log_buf)
        self._log_buf.clear()
        try:
            self.log.insert("end", chunk)
            self.log.see("end")
        except Exception:
            pass

    def _task_log_writer_loop(self):
        while not self._task_log_writer_stop.is_set():
            try:
                log_file, msg = self._task_log_write_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                Path(log_file).parent.mkdir(exist_ok=True, parents=True)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(msg)
            except Exception:
                pass
            finally:
                self._task_log_write_q.task_done()

    # ---------- restore/autosave ----------
    def _restore_tasks(self):
        self.task_history = load_task_history(self.log_line)
        tasks, next_counter = load_tasks_store(self.log_line)
        self.tasks = tasks
        self.task_counter = next_counter
        self._normalize_all_paths()

        # Build indices from loaded tasks
        self.idx.build_from_tasks(self.tasks)
        for tid in list(self.idx.get_failed()):
            t = self.tasks.get(tid)
            if t:
                self._ensure_retry_scheduled(t)
        for tid in list(self.idx.by_status.get("Done(No URL)", set())):
            t = self.tasks.get(tid)
            if t:
                self._ensure_retry_scheduled(t)

        self._ui_call(self.refresh_table)
        self.log_line(f"已恢复任务：{len(self.tasks)}（来源 {TASKS_STORE_FILE}）\n")
        self.pause_new_tasks_var.set(True)
        self.log_line("启动默认已暂停。点击“开始执行”后再运行任务。\n")

    def _tick_autosave(self):
        if self._closing:
            return
        self._update_pause_label()
        self._update_billing_label()
        self._update_status_overview_label()
        if not self._closing:
            self.root.after(5000, self._tick_autosave)

    def _mark_tasks_dirty(self):
        self._tasks_dirty = True
        self._schedule_tasks_save()

    def _schedule_tasks_save(self, delay_ms: int | None = None):
        if self._closing:
            return
        delay = self._save_delay_ms if delay_ms is None else max(0, int(delay_ms))
        if self._tasks_save_timer_id is not None:
            try:
                self.root.after_cancel(self._tasks_save_timer_id)
            except Exception:
                pass
            self._tasks_save_timer_id = None
        self._tasks_save_timer_id = self.root.after(delay, self._request_async_save_tasks)

    def _request_async_save_tasks(self):
        self._tasks_save_timer_id = None
        if self._closing:
            return
        if self._save_pending:
            self._save_reschedule = True
            return
        if not self._tasks_dirty:
            return
        self._save_pending = True
        self._tasks_dirty = False
        tasks_list = []
        for _tid, t in list(self.tasks.items()):
            try:
                tasks_list.append(t.to_persist_dict())
            except Exception:
                pass
        self.save_executor.submit(self._async_save_tasks, tasks_list)

    def _save_now(self):
        if self._tasks_save_timer_id is not None:
            try:
                self.root.after_cancel(self._tasks_save_timer_id)
            except Exception:
                pass
            self._tasks_save_timer_id = None
        tasks_list = []
        for _tid, t in list(self.tasks.items()):
            try:
                tasks_list.append(t.to_persist_dict())
            except Exception:
                pass
        future = self.save_executor.submit(save_tasks_store_items, tasks_list)
        try:
            future.result(timeout=10)
        except Exception:
            pass
        self._tasks_dirty = False
        self._save_pending = False
        self._save_reschedule = False

    def _update_pause_label(self):
        try:
            if self.start_poll_only_var.get():
                self.pause_label.configure(text="仅轮询：不启动新任务", foreground="blue")
                self.primary_status_label.configure(text="状态：仅刷新已有任务", foreground="blue")
                self.primary_hint_label.configure(text="下一步：如果要继续生成新任务，请取消“仅轮询”后点击“开始执行”。")
            elif self.pause_new_tasks_var.get():
                self.pause_label.configure(text="已暂停：不会启动新任务", foreground="red")
                self.primary_status_label.configure(text="状态：已暂停", foreground="red")
                self.primary_hint_label.configure(text="下一步：添加任务后，点击“开始执行”。")
            else:
                self.pause_label.configure(text="运行中：可启动新任务", foreground="green")
                self.primary_status_label.configure(text="状态：执行中", foreground="green")
                if len(self.tasks) == 0:
                    self.primary_hint_label.configure(text="下一步：先添加任务或批量导入任务。")
                else:
                    self.primary_hint_label.configure(text="下一步：等待任务完成，或在完成后点击“下载结果”。")
        except Exception:
            pass

    def _async_save_tasks(self, tasks_list: list[dict]):
        try:
            save_tasks_store_items(tasks_list)
        except Exception:
            pass
        finally:
            def _finish():
                self._save_pending = False
                if self._save_reschedule or self._tasks_dirty:
                    self._save_reschedule = False
                    self._schedule_tasks_save(1000)
            self._ui_call(_finish)

    def _tick_gate(self):
        if self._closing:
            return
        # O(1): Gate now uses indexed counters via self.idx
        if self.gate.reached():
            if not self.gate.queue_frozen:
                self.gate.queue_frozen = True
                self.log_line("\n队列闸门已触发。\n")
        else:
            self.gate.queue_frozen = False
        if not self._closing:
            self.root.after(1200, self._tick_gate)

    # ---------- autorun queue ----------
    def _running_count(self) -> int:
        """O(1) running count using indexed lookups."""
        return self.idx.running_count()

    def _maybe_release_batch_gate_on_remote_id(self, t: TaskItem):
        try:
            if not self.active_batch_id:
                return
            if self._task_batch_id(t) != self.active_batch_id:
                return
            create_left = self.idx.create_needed_by_batch.get(self.active_batch_id, set())
            if not create_left:
                bid = self.active_batch_id
                self.active_batch_id = None
                # do not reset dedupe markers
                self.log_line(f"BatchGate: batch {bid} reached all remote IDs (release).\n")
                if not self._closing:
                    self.root.after(100, self._tick_autorun_queue)
        except Exception:
            pass

    def _tick_autorun_queue(self):
        if self._closing:
            return
        try:
            if self.pause_new_tasks_var.get() or self.gate.queue_frozen:
                if not self._closing:
                    self.root.after(1000, self._tick_autorun_queue)
                return

            # If create chain is blocked by a failed task, retry it first and do not launch new tasks.
            blocked_tid = (self._create_blocked_tid or "").strip()
            if blocked_tid:
                if not self.auto_retry_enabled_var.get():
                    self._create_blocked_tid = None
                else:
                    bt = self.tasks.get(blocked_tid)
                    if (not bt) or (getattr(bt, "remote_id", "") or "").strip():
                        self._create_blocked_tid = None
                    else:
                        try:
                            if getattr(bt, "future", None) and bt.future and (not bt.future.done()):
                                if not self._closing:
                                    self.root.after(1000, self._tick_autorun_queue)
                                return
                        except Exception:
                            pass
                        if time.time() < float(self._create_retry_after_ts or 0.0):
                            if not self._closing:
                                self.root.after(1000, self._tick_autorun_queue)
                            return
                        if bt.status != "Queued":
                            self._set_task_status(bt, "Queued")
                        self._append_task_log(bt, "\ncreate_only 重试（阻断模式）\n")
                        self._start_task_create_only(bt)
                        if not self._closing:
                            self.root.after(1000, self._tick_autorun_queue)
                        return

            # Throttle create_only submissions so the executor queue does not grow without bound.
            create_ids = list(self.idx.create_needed)
            if not create_ids:
                if not self._closing:
                    self.root.after(1000, self._tick_autorun_queue)
                return

            create_capacity = max(0, self._create_submit_limit() - self._active_count("create"))
            if create_capacity <= 0:
                if not self._closing:
                    self.root.after(1000, self._tick_autorun_queue)
                return

            started = 0
            for tid in create_ids:
                if started >= create_capacity:
                    break
                t = self.tasks.get(tid)
                if not t:
                    continue
                if self._start_task_create_only(t):
                    started += 1

            if started:
                key = ("all", int(started))
                if key != self._bg_last_started_logged:
                    self._bg_last_started_logged = key
                    self.log_line(f" create_only启动: started={started} (all create-needed)\n")

        except Exception:
            pass

        if not self._closing:
            self.root.after(1000, self._tick_autorun_queue)

    # ---------- auto retry ----------
    def _tick_auto_retry(self):
        if self._closing:
            return
        if self.pause_new_tasks_var.get() or (not self.auto_retry_enabled_var.get()):
            if not self._closing:
                self.root.after(max(1000, int(AUTO_RETRY_TICK_SEC * 1000)), self._tick_auto_retry)
            return

        include_done = bool(self.auto_retry_include_done_no_url_var.get())
        now_ts = time.time()
        max_per_tick = 20
        capacity = max(0, max(1, _safe_int(self.max_concurrency_var.get(), 1)) - self._running_count())
        if capacity <= 0:
            if not self._closing:
                self.root.after(max(1000, int(AUTO_RETRY_TICK_SEC * 1000)), self._tick_auto_retry)
            return

        due_task_ids = self.idx.pop_due_retries(now_ts, min(max_per_tick, capacity))
        for tid in due_task_ids:
            t = self.tasks.get(tid)
            if not t:
                continue

            if not (t.status == "Failed" or (include_done and t.status == "Done(No URL)")):
                continue
            self._execute_retry(t)

        if not self._closing:
            self.root.after(max(1000, int(AUTO_RETRY_TICK_SEC * 1000)), self._tick_auto_retry)

    def _execute_retry(self, t: TaskItem):
        """Execute a retry for a task that is due."""
        attempts = getattr(t, "attempts", 0) or 0
        now_dt = datetime.datetime.now()
        status_now = (getattr(t, "status", "") or "").strip()

        # Calculate next delay for after this attempt
        delay = float(AUTO_RETRY_BASE_DELAY_SEC) * (2 ** max(0, attempts))
        delay = min(delay, float(AUTO_RETRY_MAX_DELAY_SEC))
        next_dt = now_dt + datetime.timedelta(seconds=delay)

        has_remote_id = bool((t.remote_id or "").strip())

        if has_remote_id:
            if status_now == "Done(No URL)":
                t.attempts = attempts + 1
                t.next_retry_at = next_dt.strftime("%Y-%m-%d %H:%M:%S")
                self._append_task_log(
                    t, f"\n auto-retry: RemoteID  | attempts={t.attempts} | next={t.next_retry_at}\n"
                )
                self._resume_task_by_remote_id(t)
                self._schedule_retry(t, delay)
                return

            t.attempts = attempts + 1
            t.next_retry_at = next_dt.strftime("%Y-%m-%d %H:%M:%S")
            self._append_task_log(
                t, f"\nauto-retry: refresh remote_id | attempts={t.attempts} | next={t.next_retry_at}\n"
            )
            self._recreate_task_after_poll_failed(t, getattr(t, "last_error", "") or getattr(t, "status_msg", "") or "auto_retry_refresh_remote_id")
        else:
            # No remote_id - re-queue and start
            t.attempts = attempts + 1
            t.next_retry_at = next_dt.strftime("%Y-%m-%d %H:%M:%S")
            self._append_task_log(
                t, f"\nauto-retry: re-execute | attempts={t.attempts}\n"
            )
            self._set_task_status(t, "Queued")
            t.progress = 0.0
            if not self.pause_new_tasks_var.get() and not self.gate.queue_frozen:
                self._start_task(t)
            # Schedule next retry in case this attempt also fails
            self._schedule_retry(t, delay)

    # ---------- billing label ----------
    def _update_billing_label(self):
        try:
            st = self.billing.today_stats()
            self.bill_label.configure(
                text=f"计费 {st['date']}：净额={st['net_amount']} | 计费次数={st['charge_count']} | 实付次数={st['actual_paid_count']}"
            )
        except Exception:
            self.bill_label.configure(text="计费：-")

    def _update_status_overview_label(self):
        total = len(self.tasks)
        order = ("Queued", "Running", "Pending(Check)", "Success", "Downloaded", "Done(No URL)", "Failed", "Failed(Terminal)", "Stopped")
        parts = []
        for st in order:
            n = len(self.idx.by_status.get(st, set()))
            if n:
                parts.append(f"{self._status_text_map.get(st, st)}={n}")
        detail = " | ".join(parts) if parts else "无任务"
        self.status_overview_label.configure(text=f"状态总览: 总数={total} | {detail}")
        try:
            waiting = len(self.idx.by_status.get("Queued", set()))
            running = len(self.idx.by_status.get("Running", set())) + len(self.idx.by_status.get("Pending(Check)", set()))
            done = len(self.idx.by_status.get("Success", set())) + len(self.idx.by_status.get("Downloaded", set()))
            self.primary_counts_label.configure(
                text=f"任务统计：共 {total} 个 | 等待 {waiting} | 进行中 {running} | 已完成 {done}"
            )
        except Exception:
            pass
        try:
            overall_percent = round((done * 100.0 / total), 1) if total else 0.0
            self.status_overview_label.configure(
                text=f"状态总览: 总数={total} | 整体完成={overall_percent:.1f}% | {detail}"
            )
        except Exception:
            self.status_overview_label.configure(text=f"状态总览: 总数={total} | {detail}")

    # ---------- provider sync ----------
    def _sync_provider_ui(self, reset_base: bool = False, reset_model: bool = False):
        pv = self.provider_var.get()
        current_base = (self.base_url_var.get() or "").strip()
        default_base = APIYI_DEFAULT_BASE
        model_values = APIYI_MODELS
        if pv == "xintian":
            default_base = XINTIAN_DEFAULT_BASE
            model_values = XINTIAN_MODELS
        elif pv == "lingke":
            default_base = LINGKE_DEFAULT_BASE
            model_values = LINGKE_MODELS
        elif pv == "toapis":
            default_base = TOAPIS_DEFAULT_BASE
            model_values = TOAPIS_MODELS
        elif pv == "baoyouhuyu":
            default_base = BAOYOUHUYU_DEFAULT_BASE
            model_values = BAOYOUHUYU_MODELS
        elif pv == "jimmy":
            default_base = JIMMY_DEFAULT_BASE
            model_values = JIMMY_MODELS
        elif pv == "dyuapi":
            default_base = DYUAPI_DEFAULT_BASE
            model_values = DYUAPI_MODELS

        if reset_base or not current_base:
            self.base_url_var.set(default_base)
        self.model_cb["values"] = model_values
        default_model = str(PROVIDER_DEFAULT_MODELS.get(pv, "") or "").strip()
        if default_model not in model_values:
            default_model = model_values[0] if model_values else ""
        if model_values and (reset_model or self.model_var.get() not in model_values):
            self.model_var.set(default_model)

    def _resolve_task_route(self, t: TaskItem, provider: str, api_key: str | None = None) -> tuple[str, str]:
        api_keys = _split_balanced_values(api_key if api_key is not None else self.api_key_var.get())
        base_urls = _split_balanced_values(self.base_url_var.get(), normalize_url=True)
        if not base_urls:
            base_urls = _split_balanced_values(getattr(t, "base_url", ""), normalize_url=True)
        if not api_keys:
            return "", (base_urls[0] if base_urls else _norm_maybe_url(getattr(t, "base_url", "") or ""))

        capacity = max(len(api_keys), len(base_urls), 1)
        slot = getattr(t, "route_slot", -1)
        if slot is None or int(slot) < 0:
            rr_key = (provider or "").strip() or "default"
            next_slot = int(self._route_rr_state.get(rr_key, 0) or 0)
            slot = next_slot % capacity
            self._route_rr_state[rr_key] = next_slot + 1
        else:
            slot = int(slot) % capacity
        t.route_slot = slot

        resolved_api_key = api_keys[slot % len(api_keys)]
        if base_urls:
            resolved_base_url = base_urls[slot % len(base_urls)]
        else:
            resolved_base_url = _norm_maybe_url(getattr(t, "base_url", "") or "")
        t.base_url = resolved_base_url
        return resolved_api_key, resolved_base_url

    # ---------- key persistence ----------
    def _ask_passphrase(self, title: str = "请输入口令", prompt: str = "请输入口令：") -> str:
        v = simpledialog.askstring(title, prompt, show="*", parent=self.root)
        return (v or "").strip()

    def _try_load_api_key_with_passphrase(self, passphrase: str) -> bool:
        passphrase = (passphrase or "").strip()
        if not passphrase:
            return False
        try:
            if CRYPTO_OK:
                cfg = load_encrypted_config(passphrase)
            else:
                p = Path(PLAIN_CONFIG_FILE)
                cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
            if not cfg:
                return False
            api_key = (cfg.get("api_key", "") or "").strip()
            if not api_key:
                return False
            self.passphrase_var.set(passphrase)
            self._passphrase_verified = True
            self.current_config = cfg
            self.api_key_var.set(api_key)
            self.key_loaded_var.set(True)
            self.key_state.configure(text="密钥已加载", foreground="green")
            return True
        except Exception:
            return False

    def _verify_passphrase_only(self, passphrase: str) -> bool:
        passphrase = (passphrase or "").strip()
        if not passphrase:
            return False
        if not CRYPTO_OK:
            self.passphrase_var.set(passphrase)
            self._passphrase_verified = True
            return True
        try:
            cfg = load_encrypted_config(passphrase)
            ok = bool(cfg)
            if ok:
                self.passphrase_var.set(passphrase)
                self._passphrase_verified = True
            return ok
        except Exception:
            return False

    def _ensure_runtime_api_key(self, reason: str = "启动任务") -> bool:
        if (self.api_key_var.get() or "").strip():
            return True
        pp = self._ask_passphrase("启动口令", f"要{reason}，请输入口令：")
        if not pp:
            self.log_line(f"已取消：未输入口令（{reason}）。")
            return False
        if self._try_load_api_key_with_passphrase(pp):
            self.log_line("已自动加载接口密钥。")
            return True
        messagebox.showerror("无法继续", "口令不正确，或未保存可用的接口密钥。")
        return False

    def load_keys(self):
        passphrase = self.passphrase_var.get().strip()
        if not passphrase:
            passphrase = self._ask_passphrase("读取密钥", "请输入口令以读取密钥：")
            if not passphrase:
                return
            self.passphrase_var.set(passphrase)
        try:
            if CRYPTO_OK:
                cfg = load_encrypted_config(passphrase)
            else:
                p = Path(PLAIN_CONFIG_FILE)
                cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

            if not cfg:
                messagebox.showinfo("无配置", "未找到已保存的密钥配置。")
                return

            self.current_config = cfg
            self.api_key_var.set(cfg.get("api_key", ""))
            self._passphrase_verified = True
            self.key_loaded_var.set(True)
            self.key_state.configure(text="密钥已加载", foreground="green")
            self.log_line("密钥已加载\n")

            try:
                self.api_key_entry.configure(show="*")
            except Exception:
                pass

        except Exception as e:
            messagebox.showerror("加载失败", str(e))

    def save_keys(self):
        passphrase = self.passphrase_var.get().strip()
        if not passphrase:
            passphrase = self._ask_passphrase("保存密钥", "请输入口令以保存密钥：")
            if not passphrase:
                return
            self.passphrase_var.set(passphrase)
        cfg = {"api_key": (self.api_key_var.get() or "").strip()}
        try:
            if CRYPTO_OK:
                save_encrypted_config(passphrase, cfg)
            else:
                Path(PLAIN_CONFIG_FILE).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

            self._passphrase_verified = True
            self.key_loaded_var.set(True)
            self.key_state.configure(text="密钥已保存", foreground="green")
            self.log_line("密钥已保存\n")

            first_time = not self._key_marker_file.exists()
            try:
                self._key_marker_file.write_text("saved", encoding="utf-8")
            except Exception:
                pass

            if first_time:
                try:
                    self.api_key_entry.configure(show="")
                    self.root.after(10000, lambda: self.api_key_entry.configure(show="*"))
                except Exception:
                    pass
            else:
                try:
                    self.api_key_entry.configure(show="*")
                except Exception:
                    pass

        except Exception as e:
            messagebox.showerror("错误", str(e))

    # ---------- mission IO ----------
    def pick_mission_root(self):
        p = filedialog.askdirectory(title="选择任务根目录")
        if p:
            self.mission_root_var.set(norm_path(p))

    def pick_download_root(self):
        p = filedialog.askdirectory(title="选择下载根目录")
        if p:
            self.download_root_var.set(norm_path(p))

    def pick_image(self):
        p = filedialog.askopenfilename(
            title="",
            filetypes=[("图片", "*.png;*.jpg;*.jpeg;*.webp"), ("全部文件", "*.*")]
        )
        if p:
            self.image_path_var.set(norm_path(p))

    def load_from_mission_latest(self):
        root = (self.mission_root_var.get() or "").strip()
        if not root or not os.path.isdir(root):
            self.log_line("任务根目录无效，请重新选择有效目录。\n")
            return

        rootp = Path(root)
        subdirs = [p for p in rootp.iterdir() if p.is_dir()]
        if not subdirs:
            self.log_line("任务根目录下没有子目录。\n")
            return

        def dir_latest_mtime(d: Path) -> float:
            latest = d.stat().st_mtime
            try:
                for fp in d.rglob("*"):
                    if fp.is_file():
                        mt = fp.stat().st_mtime
                        if mt > latest:
                            latest = mt
            except Exception:
                pass
            return latest

        latest_dir = max(subdirs, key=dir_latest_mtime)

        prompt_text = ""
        prompt_file = latest_dir / "prompt.txt"
        if prompt_file.exists():
            try:
                prompt_text = prompt_file.read_text(encoding="utf-8", errors="ignore").strip()
            except Exception:
                prompt_text = ""
        else:
            txts = [p for p in sorted(latest_dir.glob("*.txt")) if p.name.lower() != "url.txt"]
            if txts:
                try:
                    prompt_text = txts[0].read_text(encoding="utf-8", errors="ignore").strip()
                except Exception:
                    prompt_text = ""

        imgs = []
        for pat in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.PNG", "*.JPG", "*.JPEG", "*.WEBP"):
            imgs.extend(list(latest_dir.glob(pat)))
        img_path = str(max(imgs, key=lambda p: p.stat().st_mtime)) if imgs else ""

        self.prompt_var.set(prompt_text)
        self.image_path_var.set(norm_path(img_path))
        self.group_var.set(os.path.basename(latest_dir.name))
        if not (self.note_var.get() or "").strip():
            self.note_var.set(os.path.basename(latest_dir.name))
        self._normalize_all_paths()

        self.log_line(f"已加载最新任务文件夹（未入队）：{latest_dir}\n")
        if not prompt_text:
            self.log_line("未找到提示词文本（prompt.txt 或 非 url.txt 的 *.txt）。\n")
        if not img_path:
            self.log_line("未找到图片文件（png/jpg/jpeg/webp）。\n")

    # ---------- table ----------
    def refresh_table(self):
        try:
            y_view = self.tree.yview()
        except Exception:
            y_view = None

        # refresh_table 
        for iid in self.tree.get_children():
            self.tree.delete(iid)

        group_mode = "按大组"
        items = list(self._filtered_tasks())
        self._tree_group_meta = {}
        plan: list[dict] = []

        def _queue_group_node(iid: str, text: str, batch_id: str, task_ids: list[str], values: tuple):
            self._tree_group_meta[iid] = {
                "mode": group_mode,
                "collapse_key": f"batch::{batch_id}",
                "batch_id": batch_id,
                "task_ids": list(task_ids),
            }
            plan.append({
                "kind": "group",
                "parent": "",
                "iid": iid,
                "text": text,
                "open": False,
                "values": values,
            })

        batches: dict[str, list[TaskItem]] = {}
        batch_order: list[str] = []
        for t in items:
            bid = (getattr(t, "batch_id", None) or "LEGACY").strip() or "LEGACY"
            if bid not in batches:
                batches[bid] = []
                batch_order.append(bid)
            batches[bid].append(t)
        self._filtered_total_count = len(items)
        self._total_pages = 1
        self.current_page = 1
        self._update_page_info()
        for bid in batch_order:
            task_list = batches[bid]
            task_list.sort(key=lambda x: ((x.created_at or ""), (x.group or ""), x.task_id))
            batch_name = self._batch_display_name(bid, task_list)
            iid = self._group_iid(f"batch::{bid}")
            _queue_group_node(
                iid,
                f"[{batch_name}]",
                bid,
                [t.task_id for t in task_list],
                self._batch_row_values(task_list),
            )

        self._table_render_token += 1
        self._table_render_plan = plan
        self._table_render_index = 0
        self._table_render_scroll = y_view
        self.root.after_idle(lambda tok=self._table_render_token: self._render_table_chunk(tok))

    def _render_table_chunk(self, token: int, chunk_size: int = 250):
        if token != self._table_render_token or self._closing:
            return

        plan = self._table_render_plan
        end = min(len(plan), self._table_render_index + chunk_size)
        while self._table_render_index < end:
            node = plan[self._table_render_index]
            self._table_render_index += 1
            if node["kind"] == "group":
                self.tree.insert(
                    node["parent"],
                    "end",
                    iid=node["iid"],
                    text=node["text"],
                    values=node.get("values", ("",) * len(self.cols)),
                    open=node["open"],
                )
            else:
                self.tree.insert(
                    node["parent"],
                    "end",
                    iid=node["iid"],
                    text=node["text"],
                    values=node["values"],
                )

        if self._table_render_index < len(plan):
            self.root.after(10, lambda tok=token: self._render_table_chunk(tok, chunk_size))
            return

        y_view = self._table_render_scroll
        if y_view is not None:
            try:
                self.tree.yview_moveto(y_view[0])
            except Exception:
                pass

    def _refresh_groups_and_batch_insert(self, grouped, y_view, group_mode):
        # Reset per-group incremental cursors from previous refresh; otherwise
        # subsequent refreshes can skip child row insertion and show groups only.
        stale_keys = [k for k in list(self.__dict__.keys()) if isinstance(k, str) and k.startswith("_gpos_")]
        for k in stale_keys:
            try:
                delattr(self, k)
            except Exception:
                pass
        self._tree_group_meta = {}
        self._group_order = list(grouped.items())
        self._group_i = 0
        self._y_view_restore = y_view
        self._batch_grouped = grouped
        self._group_mode_rendering = group_mode
        self._insert_next_batch()

    def _insert_next_batch(self, batch_size=200):
        if self._closing:
            return
        start = time.time()
        inserted = 0

        while self._group_i < len(self._group_order) and inserted < batch_size:
            g_name, task_list = self._group_order[self._group_i]
            display_name = self._group_display_name(g_name, task_list, getattr(self, "_group_mode_rendering", "按大组"))
            gid = self._batch_iid(g_name)
            mode = getattr(self, "_group_mode_rendering", "按大组")
            self._tree_group_meta[gid] = (mode, g_name)
            collapsed = g_name in self._collapsed_group_keys_by_mode.setdefault(mode, set())
            if not self.tree.exists(gid):
                self.tree.insert(
                    "",
                    "end",
                    iid=gid,
                    text=f"[{display_name}] ({len(task_list)})",
                    values=("",) * len(self.cols),
                    open=(not collapsed),
                )

            # group-level cursor for incremental inserts
            idx_key = f"_gpos_{gid}"
            gpos = getattr(self, idx_key, 0)

            while gpos < len(task_list) and inserted < batch_size:
                t = task_list[gpos]
                if not self.tree.exists(t.task_id):
                    self.tree.insert(gid, "end", iid=t.task_id, text=t.task_id, values=self._row_values(t))
                gpos += 1
                inserted += 1

            setattr(self, idx_key, gpos)

            if gpos >= len(task_list):
                self._group_i += 1

            #  while 
            if time.time() - start > 0.03:  # 30ms  UI
                break

        self._maybe_update_billing_label()

        if self._group_i < len(self._group_order):
            if not self._closing:
                self.root.after(10, self._insert_next_batch)
        else:
            # restore scroll position after full insert
            y_view = getattr(self, "_y_view_restore", None)
            if y_view is not None:
                try:
                    self.tree.yview_moveto(y_view[0])
                except Exception:
                    pass

    def _schedule_autofit_columns(self):
        if self._closing:
            return
        if getattr(self, "_manual_col_resize", False):
            return
        if getattr(self, "_building_table", False):
            return
        # debounce while resizing window
        if hasattr(self, "_autofit_timer"):
            try:
                self.root.after_cancel(self._autofit_timer)
            except Exception:
                pass
        self._autofit_timer = self.root.after(120, self._autofit_columns)

    def _autofit_columns(self):
        if not getattr(self, "tree", None):
            return
        try:
            total_w = self.tree.winfo_width()
            if total_w <= 50:
                return
        except Exception:
            return

        # available width = total width - tree column - scrollbar
        try:
            w0 = int(self.tree.column("#0", "width"))
        except Exception:
            w0 = 220
        avail = max(0, total_w - w0 - 22)

        # sum widths of fixed columns
        fixed = 0
        for c in self.cols:
            if c not in self._col_flex_weight:
                fixed += int(self._col_base.get(c, 120))

        remain = max(0, avail - fixed)
        weight_sum = sum(self._col_flex_weight.values()) or 1

        for c in self.cols:
            base_w = int(self._col_base.get(c, 120))
            if c in self._col_flex_weight:
                extra = int(remain * (self._col_flex_weight[c] / weight_sum))
                w = base_w + extra
            else:
                w = base_w
            try:
                self.tree.column(c, width=w)
            except Exception:
                pass

    def clear_filters(self):
        self.filter_status_var.set("全部")
        self.filter_provider_var.set("全部")
        self.search_var.set("")
        self.current_page = 1
        self._ui_call(self.refresh_table)

    def refresh_table_reset_page(self):
        self.current_page = 1
        self.refresh_table()

    def _update_page_info(self):
        total_tasks = int(getattr(self, "_filtered_total_count", 0) or 0)
        batch_ids = set()
        for t in self._filtered_tasks():
            bid = (getattr(t, "batch_id", None) or "LEGACY").strip() or "LEGACY"
            batch_ids.add(bid)
        self.current_page = 1
        self._total_pages = 1
        self.page_info_var.set(f"当前显示：全部任务 | 大组 {len(batch_ids)} 个 | 任务 {total_tasks} 个")

    def prev_page(self):
        self.refresh_table()

    def next_page(self):
        self.refresh_table()

    def _paginate_tasks(self, items: list[TaskItem]) -> list[TaskItem]:
        total = len(items)
        self._filtered_total_count = total
        self._total_pages = 1
        self.current_page = 1
        self._update_page_info()
        return items

    def _filtered_tasks(self) -> list[TaskItem]:
        status_filter_raw = self.filter_status_var.get()
        status_filter = self._status_filter_map.get(status_filter_raw, status_filter_raw)
        provider_filter_raw = self.filter_provider_var.get()
        provider_filter = "ALL" if provider_filter_raw == "全部" else provider_filter_raw
        search_query = (self.search_var.get() or "").strip().lower()

        out: list[TaskItem] = []
        for t in self.tasks.values():
            if status_filter != "ALL" and t.status != status_filter:
                continue
            if provider_filter != "ALL" and (t.provider or "") != provider_filter:
                continue
            if search_query:
                prompt_part = (t.prompt or "")[:200]
                haystack = f"{t.task_id} {t.note} {t.group} {getattr(t, 'batch_name', '')} {t.model} {prompt_part}".lower()
                if search_query not in haystack:
                    continue
            out.append(t)

        out.sort(key=lambda x: x.created_at or "")
        return out

    def selected_task_ids(self) -> list[str]:
        def _collect_desc_task_ids(node_id: str) -> list[str]:
            out2: list[str] = []
            for child in self.tree.get_children(node_id):
                if self._is_group_iid(child):
                    out2.extend(_collect_desc_task_ids(child))
                else:
                    out2.append(child)
            return out2

        out: list[str] = []
        for iid in self.tree.selection():
            if self._is_group_iid(iid):
                meta = getattr(self, "_tree_group_meta", {}).get(iid) or {}
                task_ids = list(meta.get("task_ids") or [])
                if task_ids:
                    out.extend(task_ids)
                else:
                    out.extend(_collect_desc_task_ids(iid))
            else:
                out.append(iid)
        seen = set()
        return [x for x in out if not (x in seen or seen.add(x))]

    def _display_status(self, t: TaskItem) -> str:
        st = t.status or ""
        st = self._status_text_map.get(st, st)
        return st

    # ---------- init task fields ----------
    def _init_task_fields(self, t: TaskItem):
        if not getattr(t, "status", None):
            t.status = "Queued"
        if getattr(t, "progress", None) is None:
            t.progress = 0.0
        if getattr(t, "attempts", None) is None:
            t.attempts = 0
        if getattr(t, "run_attempt", None) is None:
            t.run_attempt = 0
        if getattr(t, "next_retry_at", None) is None:
            t.next_retry_at = None
        if getattr(t, "stop_event", None) is None:
            t.stop_event = threading.Event()
        if getattr(t, "status_msg", None) is None:
            t.status_msg = ""

    # ---------- indexed state change helpers ----------
    def _set_task_status(self, t: TaskItem, new_status: str):
        """Update task status and maintain indices."""
        old_status = t.status or "Queued"
        if old_status == new_status:
            return
        t.status = new_status
        self.idx.update_status(t, old_status, new_status)
        if new_status not in ("Failed", "Done(No URL)"):
            t.next_retry_at = None
            self.idx.cancel_retry(t.task_id)
        self._sync_history_task_snapshot(t, save=True)
        self._mark_tasks_dirty()

    def _set_remote_id(self, t: TaskItem, new_remote_id: str):
        """Update task remote_id and maintain indices."""
        old_remote_id = (t.remote_id or "").strip()
        new_remote_id = (new_remote_id or "").strip()
        if old_remote_id == new_remote_id:
            return
        t.remote_id = new_remote_id
        self.idx.update_remote_id(t, old_remote_id, new_remote_id)
        self._sync_history_task_snapshot(t, save=True)
        self._mark_tasks_dirty()

    def _set_video_url(self, t: TaskItem, new_url: str):
        """Update task video_url and maintain indices."""
        old_url = (t.video_url or "").strip()
        new_url = (new_url or "").strip()
        if old_url == new_url:
            return
        t.video_url = new_url
        if new_url:
            t.completed_at = now_str()
        else:
            t.completed_at = ""
        self.idx.update_video_url(t, old_url, new_url)
        self._sync_history_task_snapshot(t, save=True)
        self._mark_tasks_dirty()

    def _mark_terminal_failure(self, t: TaskItem):
        """Mark task as terminal failure."""
        t.poll_terminal_fail = True
        self.idx.mark_terminal_failure(t)
        self._mark_tasks_dirty()

    def _recreate_task_after_poll_failed(self, t: TaskItem, err: str):
        """Reuse the same task and reacquire a new remote_id."""
        msg = (err or "").strip()
        old_rid = (getattr(t, "remote_id", "") or "").strip()
        self._append_task_log(
            t,
            f"\n原任务重试，准备重新获取 remote_id"
            f"{(' | old_remote_id=' + old_rid) if old_rid else ''}\n"
            f"原因: {msg}\n",
        )
        # reset terminal marker/index first, then re-queue creation
        t.poll_terminal_fail = False
        self.idx.terminal_failed.discard(t.task_id)
        t.replacement_spawned = False
        self._set_video_url(t, "")
        self._set_remote_id(t, "")
        self._set_task_status(t, "Queued")
        t.progress = 0.0
        t.last_error = msg
        t.status_msg = compact_status_msg(msg) or msg or "轮询失败，已转为重建"
        t.next_retry_at = None
        self.idx.cancel_retry(t.task_id)
        # block create pipeline to prioritize this failed task first
        self._create_blocked_tid = t.task_id
        self._create_retry_after_ts = time.time() + 1.0
        self._ui_update_task_row(t)
        self._ui_call(self._tick_autorun_queue)

    def _schedule_retry(self, t: TaskItem, delay_sec: float):
        """Schedule task for retry after delay_sec seconds."""
        next_time = time.time() + delay_sec
        t.next_retry_at = datetime.datetime.fromtimestamp(next_time).strftime("%Y-%m-%d %H:%M:%S")
        self.idx.schedule_retry(t, next_time)

    def _ensure_retry_scheduled(self, t: TaskItem):
        if getattr(t, "poll_terminal_fail", False):
            return
        if not self.auto_retry_enabled_var.get():
            return
        st = (t.status or "").strip()
        include_done = bool(self.auto_retry_include_done_no_url_var.get())
        if st not in ("Failed", "Done(No URL)"):
            return
        if st == "Done(No URL)" and not include_done:
            return
        attempts = getattr(t, "attempts", 0) or 0
        max_retries = int(AUTO_RETRY_MAX_RETRIES) if AUTO_RETRY_MAX_RETRIES else 50
        if attempts >= max_retries:
            return
        delay = float(AUTO_RETRY_BASE_DELAY_SEC) * (2 ** max(0, attempts))
        delay = min(delay, float(AUTO_RETRY_MAX_DELAY_SEC))
        self._schedule_retry(t, delay)

    def _get_next_tid(self) -> str:
        while True:
            tid = f"T{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}_{self.task_counter:04d}"
            self.task_counter += 1
            if tid not in self.tasks:
                return tid

    def _create_task_and_add(
            self,
            *,
            provider: str,
            base_url: str,
            model: str,
            prompt: str,
            image_path: str,
            note: str,
            group: str,
            batch_id: str,
            batch_name: str,
    ) -> TaskItem:
        tid = self._get_next_tid()
        log_file = norm_path(str(Path(LOG_DIR) / f"{tid}.log.txt"))

        t = TaskItem(
            task_id=tid,
            provider=(provider or "").strip() or "xintian",
            base_url=(_split_balanced_values(base_url, normalize_url=True) or [_norm_maybe_url(base_url)])[0],
            model=(model or "").strip(),
            prompt=(prompt or "").strip(),
            image_path=norm_path(image_path),
            note=(note or "").strip(),
            group=(group or "").strip() or "手动",
            log_file=log_file,
            created_at=now_str(),
            batch_id=(batch_id or "LEGACY"),
            batch_name=(batch_name or "").strip(),
        )
        self._init_task_fields(t)
        # Note: Set status before adding to index, idx.add_task will index it
        t.status = "Queued"
        t.progress = 0.0
        t.next_retry_at = None
        self.tasks[tid] = t

        # Add to index (must be after task is in self.tasks)
        self.idx.add_task(t)
        self._mark_tasks_dirty()

        return t

    def _save_task_history(self):
        try:
            snapshot = copy.deepcopy(self.task_history)
        except Exception:
            snapshot = list(self.task_history)

        if getattr(self, "_history_save_pending", False):
            self._history_save_reschedule = True
            return

        self._history_save_pending = True
        self.save_executor.submit(self._async_save_task_history, snapshot)

    def _async_save_task_history(self, snapshot: list[dict]):
        try:
            save_task_history(snapshot)
        finally:
            def _finish():
                self._history_save_pending = False
                if self._history_save_reschedule:
                    self._history_save_reschedule = False
                    self._save_task_history()
            self._ui_call(_finish)

    @staticmethod
    def _read_history_log_snapshot(log_file: str, limit_chars: int = 12000) -> str:
        p = (log_file or "").strip()
        if not p:
            return ""
        try:
            text = Path(p).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
        text = text.strip()
        if len(text) <= limit_chars:
            return text
        return text[-limit_chars:]

    def _history_task_payload(self, t: TaskItem) -> dict:
        return {
            "task_id": t.task_id,
            "history_remote_id": getattr(t, "remote_id", "") or "",
            "batch_name": getattr(t, "batch_name", "") or "",
            "provider": t.provider,
            "base_url": t.base_url,
            "model": t.model,
            "prompt": t.prompt,
            "image_path": t.image_path,
            "note": t.note,
            "group": t.group,
            "status": getattr(t, "status", "") or "",
            "status_msg": getattr(t, "status_msg", "") or "",
            "progress": float(getattr(t, "progress", 0.0) or 0.0),
            "remote_id": getattr(t, "remote_id", "") or "",
            "video_url": getattr(t, "video_url", "") or "",
            "completed_at": getattr(t, "completed_at", "") or "",
            "last_error": getattr(t, "last_error", "") or "",
            "log_file": getattr(t, "log_file", "") or "",
            "log_text": self._read_history_log_snapshot(getattr(t, "log_file", "") or ""),
            "added_at": t.created_at,
            "deleted_at": "",
        }

    def _history_item_summary(self, item: dict) -> dict:
        data = dict(item or {})
        log_text = (data.get("log_text", "") or "").strip()
        if log_text and len(log_text) > 4000:
            data["log_text"] = log_text[-4000:]
        return data

    @staticmethod
    def _history_safe_name(name: str) -> str:
        s = (name or "").strip()
        if not s:
            return ""
        s = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", s)
        s = re.sub(r"_+", "_", s).strip(" ._")
        return s[:80]

    def _sync_history_task_snapshot(self, t: TaskItem, *, include_log: bool = False, save: bool = True) -> bool:
        tid = (getattr(t, "task_id", "") or "").strip()
        if not tid:
            return False
        changed = False
        for idx_rec, rec in enumerate(self.task_history):
            rec = self._ensure_history_record_loaded(rec)
            self.task_history[idx_rec] = rec
            items = rec.get("items", [])
            if not isinstance(items, list):
                continue
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                if (item.get("task_id", "") or "").strip() != tid:
                    continue
                payload = self._history_task_payload(t)
                if not include_log:
                    payload["log_text"] = item.get("log_text", "") or ""
                deleted_at = (item.get("deleted_at", "") or "").strip()
                if deleted_at:
                    payload["deleted_at"] = deleted_at
                if self._history_item_summary(item) == self._history_item_summary(payload):
                    return False
                items[idx] = payload
                changed = True
                break
            if changed:
                break
        if changed and save:
            self._save_task_history()
        return changed

    def _record_history_batch(self, *, batch_id: str, source: str, tasks: list[TaskItem]):
        if not tasks:
            return
        added_at = min(((t.created_at or "").strip() for t in tasks if (t.created_at or "").strip()), default=now_str())
        record = {
            "history_id": batch_id,
            "batch_id": batch_id,
            "batch_name": self._task_batch_name(tasks[0]),
            "source": source,
            "added_at": added_at,
            "items": [self._history_task_payload(t) for t in tasks],
        }
        self.task_history.insert(0, record)
        self._save_task_history()

    def _find_history_record(self, batch_id: str) -> dict | None:
        key = (batch_id or "").strip() or "LEGACY"
        for rec in self.task_history:
            if (rec.get("batch_id", "") or "").strip() == key:
                return rec
        return None

    def _upsert_history_tasks(self, *, batch_id: str, source: str, tasks: list[TaskItem]) -> tuple[int, int]:
        if not tasks:
            return 0, 0
        key = (batch_id or "").strip() or "LEGACY"
        rec = self._find_history_record(key)
        added_at = min(((t.created_at or "").strip() for t in tasks if (t.created_at or "").strip()), default=now_str())
        if rec is None:
            rec = {
                "history_id": key,
                "batch_id": key,
                "batch_name": self._task_batch_name(tasks[0]),
                "source": source,
                "added_at": added_at,
                "items": [],
            }
            self.task_history.append(rec)
        else:
            if not (rec.get("batch_name") or "").strip():
                rec["batch_name"] = self._task_batch_name(tasks[0])
            if not (rec.get("source") or "").strip():
                rec["source"] = source
            if not (rec.get("added_at") or "").strip() or ((rec.get("added_at") or "") > added_at):
                rec["added_at"] = added_at

        items = rec.get("items", [])
        if not isinstance(items, list):
            items = []
            rec["items"] = items

        existing_ids = {
            (x.get("task_id", "") or "").strip()
            for x in items if isinstance(x, dict) and (x.get("task_id", "") or "").strip()
        }
        appended = 0
        for t in tasks:
            tid = (t.task_id or "").strip()
            if not tid or tid in existing_ids:
                continue
            items.append(self._history_task_payload(t))
            existing_ids.add(tid)
            appended += 1

        items.sort(key=lambda x: ((x.get("added_at", "") or ""), (x.get("task_id", "") or "")))
        return (1 if appended > 0 and len(items) == appended else 0), appended

    def _mark_history_deleted_for_task_ids(self, task_ids: list[str]):
        if not task_ids:
            return
        targets = {x for x in task_ids if x}
        if not targets:
            return
        deleted_at = now_str()
        changed = False
        for tid in list(targets):
            t = self.tasks.get(tid)
            if t:
                self._sync_history_task_snapshot(t, include_log=True, save=False)
        for rec in self.task_history:
            items = rec.get("items", [])
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("task_id") not in targets:
                    continue
                if not (item.get("deleted_at") or "").strip():
                    item["deleted_at"] = deleted_at
                    changed = True
                if not (item.get("log_text", "") or "").strip():
                    item["log_text"] = self._read_history_log_snapshot(item.get("log_file", "") or "")
                    changed = True
        if changed:
            self._save_task_history()

    def backfill_task_history(self):
        if not self._require_admin("回补旧历史"):
            return
        if not self.tasks:
            messagebox.showinfo("无可回补任务", "当前没有已加载任务，无法从旧数据回补历史。")
            return
        if not messagebox.askyesno(
                "回补旧历史",
                "将根据当前已加载任务（主要来自旧版 tasks.json）回补历史记录。\n"
                "旧版本通常无法可靠恢复删除时间，缺失项会保留为空。\n\n"
                "是否继续？"
        ):
            return

        grouped: dict[str, list[TaskItem]] = {}
        for t in self.tasks.values():
            bid = (getattr(t, "batch_id", None) or "LEGACY").strip() or "LEGACY"
            grouped.setdefault(bid, []).append(t)

        created_batches = 0
        added_tasks = 0
        for batch_id, items in grouped.items():
            items.sort(key=lambda x: ((x.created_at or ""), x.task_id))
            cb, at = self._upsert_history_tasks(batch_id=batch_id, source="旧数据回补", tasks=items)
            created_batches += cb
            added_tasks += at

        if created_batches or added_tasks:
            self.task_history.sort(key=lambda x: (x.get("added_at", "") or "", x.get("batch_id", "") or ""), reverse=True)
            self._save_task_history()
        self.log_line(
            f"历史回补完成：新增批次={created_batches}，新增任务记录={added_tasks}。\n"
            "说明：旧版本如未记录删除事件，则删除时间保留为空。\n"
        )
        messagebox.showinfo(
            "回补完成",
            f"新增批次：{created_batches}\n新增任务记录：{added_tasks}\n\n旧版本缺失的删除时间不会伪造。"
        )

    @staticmethod
    def _history_record_summary(rec: dict) -> tuple[int, int]:
        items = rec.get("items", None)
        if not isinstance(items, list):
            total = _safe_int(rec.get("total", 0), 0)
            deleted = _safe_int(rec.get("deleted", 0), 0)
            return total, deleted
        total = len(items)
        deleted = 0
        for item in items:
            if isinstance(item, dict) and (item.get("deleted_at") or "").strip():
                deleted += 1
        return total, deleted

    @staticmethod
    def _history_last_deleted_at(rec: dict) -> str:
        items = rec.get("items", None)
        if not isinstance(items, list):
            return (rec.get("last_deleted_at", "") or "").strip()
        vals = []
        for item in items:
            if not isinstance(item, dict):
                continue
            dt = (item.get("deleted_at") or "").strip()
            if dt:
                vals.append(dt)
        return max(vals) if vals else ""

    def _ensure_history_record_loaded(self, rec: dict) -> dict:
        loaded = load_history_record_items(rec)
        if loaded is rec:
            return rec
        key = (loaded.get("batch_id", "") or loaded.get("history_id", "")).strip()
        for idx, item in enumerate(self.task_history):
            if not isinstance(item, dict):
                continue
            cur_key = (item.get("batch_id", "") or item.get("history_id", "")).strip()
            if cur_key != key:
                continue
            self.task_history[idx] = loaded
            return loaded
        return loaded

    def _restore_history_record_to_tasks(self, rec: dict) -> int:
        rec = self._ensure_history_record_loaded(rec)
        if not isinstance(rec, dict):
            return 0
        items = rec.get("items", [])
        if not isinstance(items, list):
            return 0

        batch_id = (rec.get("batch_id", "") or "").strip() or self._new_batch_id()
        batch_name = (rec.get("batch_name", "") or "").strip() or batch_id
        restored = 0
        created_tasks: list[TaskItem] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            t = self._create_task_and_add(
                provider=(item.get("provider", "") or "").strip() or (self.provider_var.get() or "jimmy"),
                base_url=(item.get("base_url", "") or "").strip() or (self.base_url_var.get() or "").strip(),
                model=(item.get("model", "") or "").strip() or (self.model_var.get() or "").strip(),
                prompt=(item.get("prompt", "") or "").strip(),
                image_path=(item.get("image_path", "") or "").strip(),
                note=(item.get("note", "") or "").strip(),
                group=(item.get("group", "") or "").strip() or "历史恢复",
                batch_id=batch_id,
                batch_name=batch_name,
            )
            t.remote_id = (item.get("remote_id", "") or "").strip() or None
            t.video_url = (item.get("video_url", "") or "").strip() or None
            t.completed_at = (item.get("completed_at", "") or "").strip()
            t.status_msg = (item.get("status_msg", "") or "").strip()
            t.last_error = (item.get("last_error", "") or "").strip()
            try:
                t.progress = float(item.get("progress", 0.0) or 0.0)
            except Exception:
                t.progress = 0.0
            restored_status = (item.get("status", "") or "").strip()
            if restored_status in ("Success", "Downloaded", "Done(No URL)"):
                t.status = restored_status
            elif t.video_url:
                t.status = "Success"
            elif t.remote_id:
                t.status = "Pending(Check)"
            created_tasks.append(t)
            restored += 1

        if created_tasks:
            self.idx.build_from_tasks(self.tasks)
            self.mark_all_dirty()
            self._save_now()
            self.log_line(f"已从历史批次导回任务：{batch_name} | {restored} 个\n")
        return restored

    def _history_item_to_task(self, item: dict, *, task_id_prefix: str = "HIST") -> TaskItem:
        src_task_id = (item.get("task_id", "") or "").strip() or "UNKNOWN"
        task_id = f"{task_id_prefix}_{src_task_id}"
        t = TaskItem(
            task_id=task_id,
            provider=(item.get("provider", "") or "").strip() or "jimmy",
            base_url=(item.get("base_url", "") or "").strip(),
            model=(item.get("model", "") or "").strip(),
            prompt=(item.get("prompt", "") or "").strip(),
            image_path=(item.get("image_path", "") or "").strip(),
            note=(item.get("note", "") or "").strip(),
            group=(item.get("group", "") or "").strip() or "历史任务",
            log_file=(item.get("log_file", "") or "").strip(),
            created_at=(item.get("added_at", "") or "").strip() or now_str(),
            batch_name=(item.get("batch_name", "") or "").strip(),
        )
        self._init_task_fields(t)
        t.status = (item.get("status", "") or "").strip() or ("Success" if (item.get("video_url", "") or "").strip() else "Queued")
        t.status_msg = (item.get("status_msg", "") or "").strip()
        t.remote_id = (item.get("remote_id", "") or item.get("history_remote_id", "") or "").strip() or None
        t.video_url = (item.get("video_url", "") or "").strip() or None
        t.completed_at = (item.get("completed_at", "") or "").strip()
        t.last_error = (item.get("last_error", "") or "").strip()
        try:
            t.progress = float(item.get("progress", 0.0) or 0.0)
        except Exception:
            t.progress = 0.0
        return t

    def _open_history_log_viewer(self, batch_name: str, item: dict):
        dlg = tk.Toplevel(self.root)
        task_id = (item.get("task_id", "") or "").strip()
        dlg.title(f"历史日志 - {batch_name} - {task_id}")
        dlg.geometry("980x620")
        dlg.transient(self.root)

        top = ttk.Frame(dlg)
        top.pack(fill="x", padx=10, pady=(10, 6))
        log_file = (item.get("log_file", "") or "").strip()
        ttk.Label(top, text=f"任务: {task_id}").pack(side="left")
        if log_file:
            ttk.Label(top, text=f"日志文件: {log_file}").pack(side="left", padx=(10, 0))

        txt = tk.Text(dlg, wrap="word")
        y = ttk.Scrollbar(dlg, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=y.set)
        txt.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        y.pack(side="right", fill="y", padx=(0, 10), pady=(0, 10))

        content = self._read_history_log_snapshot(log_file, limit_chars=200000) if log_file else ""
        if not content:
            content = (item.get("log_text", "") or "").strip()
        if not content:
            content = "暂无历史日志内容。"
        txt.insert("1.0", content)
        txt.configure(state="disabled")

    def open_history_batch_detail_window(self, rec: dict):
        if not isinstance(rec, dict):
            return
        rec = self._ensure_history_record_loaded(rec)
        batch_name = (rec.get("batch_name", "") or rec.get("batch_id", "") or "历史批次").strip()
        items = rec.get("items", [])
        if not isinstance(items, list):
            items = []

        dlg = tk.Toplevel(self.root)
        dlg.title(f"历史批次详情 - {batch_name}")
        dlg.geometry("1320x760")
        dlg.transient(self.root)

        top = ttk.Frame(dlg)
        top.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(top, text=f"批次: {batch_name}").pack(side="left")
        ttk.Label(top, text=f"任务数: {len(items)}").pack(side="left", padx=(12, 0))

        body = ttk.Panedwindow(dlg, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=6)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=2)

        cols = ("task_id", "status", "group", "remote_id", "completed_at", "video_url")
        tree = ttk.Treeview(left, columns=cols, show="headings", height=20)
        widths = {"task_id": 110, "status": 90, "group": 140, "remote_id": 220, "completed_at": 145, "video_url": 420}
        titles = {"task_id": "任务ID", "status": "状态", "group": "分组", "remote_id": "远端ID", "completed_at": "完成时间", "video_url": "视频链接"}
        for c in cols:
            tree.heading(c, text=titles[c])
            tree.column(c, width=widths[c], anchor="w")
        sy = ttk.Scrollbar(left, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sy.set)
        tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")

        detail = tk.Text(right, wrap="word")
        sy2 = ttk.Scrollbar(right, orient="vertical", command=detail.yview)
        detail.configure(yscrollcommand=sy2.set)
        detail.pack(side="left", fill="both", expand=True)
        sy2.pack(side="right", fill="y")

        bottom = ttk.Frame(dlg)
        bottom.pack(fill="x", padx=10, pady=(6, 10))
        status_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=status_var).pack(side="left")

        item_map: dict[str, dict] = {}

        def _render_detail(item: dict | None):
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            if not item:
                detail.configure(state="disabled")
                status_var.set("未选择历史任务。")
                return
            prompt = (item.get("prompt", "") or "").strip()
            detail.insert("end", f"任务ID: {item.get('task_id', '')}\n")
            detail.insert("end", f"状态: {item.get('status', '') or '-'}\n")
            detail.insert("end", f"状态信息: {item.get('status_msg', '') or '-'}\n")
            detail.insert("end", f"分组: {item.get('group', '') or '-'}\n")
            detail.insert("end", f"备注: {item.get('note', '') or '-'}\n")
            detail.insert("end", f"添加时间: {item.get('added_at', '') or '-'}\n")
            detail.insert("end", f"完成时间: {item.get('completed_at', '') or '-'}\n")
            detail.insert("end", f"删除时间: {item.get('deleted_at', '') or '-'}\n")
            detail.insert("end", f"Provider: {item.get('provider', '') or '-'}\n")
            detail.insert("end", f"模型: {item.get('model', '') or '-'}\n")
            detail.insert("end", f"remote_id: {item.get('remote_id', '') or '-'}\n")
            detail.insert("end", f"history_remote_id: {item.get('history_remote_id', '') or '-'}\n")
            detail.insert("end", f"视频链接: {item.get('video_url', '') or '-'}\n")
            detail.insert("end", f"图片: {item.get('image_path', '') or '-'}\n")
            detail.insert("end", f"日志文件: {item.get('log_file', '') or '-'}\n\n")
            detail.insert("end", "提示词:\n")
            detail.insert("end", prompt or "-")
            log_text = (item.get("log_text", "") or "").strip()
            if not log_text and (item.get("log_file", "") or "").strip():
                log_text = self._read_history_log_snapshot(item.get("log_file", "") or "")
            if log_text:
                detail.insert("end", "\n\n日志摘录:\n")
                detail.insert("end", log_text[-3000:])
            detail.configure(state="disabled")
            status_var.set(f"任务 {item.get('task_id', '')} | 状态 {item.get('status', '') or '-'}")

        def _selected_items() -> list[dict]:
            out = []
            for iid in tree.selection():
                item = item_map.get(iid)
                if item:
                    out.append(item)
            return out

        def _selected_one() -> dict | None:
            selected = _selected_items()
            return selected[0] if selected else None

        def _rebuild():
            tree.delete(*tree.get_children())
            item_map.clear()
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                iid = str(idx)
                item_map[iid] = item
                remote_id_display = (item.get("history_remote_id", "") or item.get("remote_id", "") or "").strip()
                tree.insert("", "end", iid=iid, values=(
                    item.get("task_id", ""),
                    item.get("status", ""),
                    item.get("group", ""),
                    remote_id_display,
                    item.get("completed_at", ""),
                    item.get("video_url", ""),
                ))
            first = tree.get_children()
            if first:
                tree.selection_set(first[0])
                tree.focus(first[0])
                _render_detail(item_map.get(first[0]))
            else:
                _render_detail(None)

        def _open_url():
            item = _selected_one()
            if not item:
                return
            url = (item.get("video_url", "") or "").strip()
            if not url:
                messagebox.showinfo("无链接", "所选历史任务没有可用的视频链接。")
                return
            webbrowser.open(url)

        def _view_log():
            item = _selected_one()
            if not item:
                return
            self._open_history_log_viewer(batch_name, item)

        def _download_history_items(selected_only: bool):
            target_items = _selected_items() if selected_only else [x for x in items if isinstance(x, dict)]
            if not target_items:
                messagebox.showinfo("无可下载任务", "没有可下载的历史任务。")
                return
            tasks_to_download = [
                self._history_item_to_task(x, task_id_prefix=self._history_safe_name(batch_name) or "HIST")
                for x in target_items
                if (x.get("video_url", "") or "").strip()
            ]
            if not tasks_to_download:
                messagebox.showinfo("无可下载任务", "所选历史任务没有视频链接。")
                return
            self._run_batch_download_logic(
                tasks_to_download,
                folder_name_override=self._history_safe_name(batch_name) or batch_name,
            )

        def _restore_batch():
            total = len([x for x in items if isinstance(x, dict)])
            if total <= 0:
                messagebox.showinfo("无可恢复任务", "所选历史批次没有可导回的任务。")
                return
            if not messagebox.askyesno(
                "导回历史任务",
                f"确认将历史批次“{batch_name}”中的 {total} 个任务导回当前任务列表吗？\n导回后会以新的任务ID重新进入当前列表。"
            ):
                return
            restored = self._restore_history_record_to_tasks(rec)
            if restored:
                messagebox.showinfo("导回完成", f"已导回 {restored} 个任务到当前任务列表。")
            else:
                messagebox.showwarning("导回失败", "未能从所选历史批次恢复任何任务。")

        tree.bind("<<TreeviewSelect>>", lambda _evt=None: _render_detail(_selected_one()))
        ttk.Button(bottom, text="打开视频链接", command=_open_url).pack(side="right")
        ttk.Button(bottom, text="查看日志", command=_view_log).pack(side="right", padx=(0, 6))
        ttk.Button(bottom, text="下载整批视频", command=lambda: _download_history_items(False)).pack(side="right", padx=(0, 6))
        ttk.Button(bottom, text="下载选中视频", command=lambda: _download_history_items(True)).pack(side="right", padx=(0, 6))
        ttk.Button(bottom, text="导回当前任务列表", command=_restore_batch).pack(side="right", padx=(0, 6))
        ttk.Button(bottom, text="关闭", command=dlg.destroy).pack(side="right", padx=(0, 6))

        _rebuild()

    def open_current_batch_detail_window(self, batch_id: str, task_ids: list[str] | None = None):
        batch_id = (batch_id or "").strip()
        if not batch_id:
            return
        task_id_set = set(task_ids or [])
        items = []
        for t in self.tasks.values():
            bid = (getattr(t, "batch_id", None) or "LEGACY").strip() or "LEGACY"
            if bid != batch_id:
                continue
            if task_id_set and t.task_id not in task_id_set:
                continue
            items.append(t)
        if not items:
            messagebox.showinfo("无明细", "当前大组下没有可展示的任务。")
            return

        items.sort(key=lambda x: ((x.created_at or ""), (x.group or ""), x.task_id))
        batch_name = self._batch_display_name(batch_id, items)

        dlg = tk.Toplevel(self.root)
        dlg.title(f"当前大组执行明细 - {batch_name}")
        dlg.geometry("1240x700")
        dlg.transient(self.root)

        top = ttk.Frame(dlg)
        top.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(top, text=f"大组: {batch_name}").pack(side="left")
        ttk.Label(top, text=f"批次ID: {batch_id}").pack(side="left", padx=(12, 0))
        ttk.Label(top, text=f"整体完成: {self._batch_progress_percent(items):.1f}% | 总数: {len(items)}").pack(side="left", padx=(12, 0))

        body = ttk.Panedwindow(dlg, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=6)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=2)

        cols = ("task_id", "status", "progress", "group", "note", "remote_id", "created_at", "completed_at", "video_url")
        tree = ttk.Treeview(left, columns=cols, show="tree headings", height=22)
        tree.heading("#0", text="小组 / 任务")
        tree.column("#0", width=180, minwidth=140, stretch=True, anchor="w")
        titles = {
            "task_id": "任务ID",
            "status": "状态",
            "progress": "进度",
            "group": "小组",
            "note": "备注",
            "remote_id": "远端ID",
            "created_at": "创建时间",
            "completed_at": "完成时间",
            "video_url": "视频链接",
        }
        widths = {
            "task_id": 140,
            "status": 90,
            "progress": 70,
            "group": 130,
            "note": 130,
            "remote_id": 220,
            "created_at": 145,
            "completed_at": 145,
            "video_url": 300,
        }
        for c in cols:
            tree.heading(c, text=titles[c])
            tree.column(c, width=widths[c], minwidth=60, stretch=(c == "video_url"), anchor="w")
        y1 = ttk.Scrollbar(left, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=y1.set)
        tree.pack(side="left", fill="both", expand=True)
        y1.pack(side="right", fill="y")

        detail = tk.Text(right, wrap="word")
        y2 = ttk.Scrollbar(right, orient="vertical", command=detail.yview)
        detail.configure(yscrollcommand=y2.set)
        detail.pack(side="left", fill="both", expand=True)
        y2.pack(side="right", fill="y")

        item_map = {t.task_id: t for t in items}
        group_map: dict[str, list[TaskItem]] = {}
        for t in items:
            group_name = display_last_part(getattr(t, "group", "") or "").strip() or "未分组"
            group_map.setdefault(group_name, []).append(t)
        for group_name, group_items in sorted(group_map.items(), key=lambda kv: kv[0]):
            group_items.sort(key=lambda x: ((x.created_at or ""), x.task_id))
            group_iid = self._group_iid(f"current_batch::{batch_id}::group::{group_name}")
            group_done = sum(1 for t in group_items if self._task_counts_as_completed(t))
            group_total = len(group_items)
            tree.insert(
                "",
                "end",
                iid=group_iid,
                text=f"[{group_name}]",
                open=True,
                values=(
                    "",
                    self._batch_status_text(group_items),
                    f"{self._batch_progress_percent(group_items):.1f}%",
                    f"{group_total} 个任务",
                    f"已完成 {group_done}",
                    "",
                    "",
                    "",
                    self._batch_status_message(group_items),
                ),
            )
            for t in group_items:
                tree.insert(
                    group_iid,
                    "end",
                    iid=t.task_id,
                    text=t.task_id,
                    values=(
                        "",
                        self._display_status(t),
                        f"{float(getattr(t, 'progress', 0.0) or 0.0):.1f}%",
                        display_last_part(getattr(t, "group", "") or ""),
                        getattr(t, "note", "") or "",
                        getattr(t, "remote_id", "") or "",
                        getattr(t, "created_at", "") or "",
                        getattr(t, "completed_at", "") or "",
                        getattr(t, "video_url", "") or "",
                    ),
                )

        def _render_detail(t: TaskItem | None):
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            if t is None:
                detail.configure(state="disabled")
                return
            prompt = (getattr(t, "prompt", "") or "").strip()
            detail.insert("end", f"任务ID: {t.task_id}\n")
            detail.insert("end", f"状态: {self._display_status(t)}\n")
            detail.insert("end", f"状态信息: {getattr(t, 'status_msg', '') or '-'}\n")
            detail.insert("end", f"进度: {float(getattr(t, 'progress', 0.0) or 0.0):.1f}%\n")
            detail.insert("end", f"小组: {getattr(t, 'group', '') or '-'}\n")
            detail.insert("end", f"备注: {getattr(t, 'note', '') or '-'}\n")
            detail.insert("end", f"Provider: {getattr(t, 'provider', '') or '-'}\n")
            detail.insert("end", f"模型: {getattr(t, 'model', '') or '-'}\n")
            detail.insert("end", f"远端ID: {getattr(t, 'remote_id', '') or '-'}\n")
            detail.insert("end", f"创建时间: {getattr(t, 'created_at', '') or '-'}\n")
            detail.insert("end", f"完成时间: {getattr(t, 'completed_at', '') or '-'}\n")
            detail.insert("end", f"视频链接: {getattr(t, 'video_url', '') or '-'}\n")
            detail.insert("end", f"图片: {getattr(t, 'image_path', '') or '-'}\n")
            detail.insert("end", f"日志文件: {getattr(t, 'log_file', '') or '-'}\n\n")
            detail.insert("end", "提示词:\n")
            detail.insert("end", prompt or "-")
            log_tail = ""
            try:
                log_file = Path(getattr(t, "log_file", "") or "")
                if log_file.exists():
                    log_tail = log_file.read_text(encoding="utf-8", errors="ignore")[-3000:]
            except Exception:
                log_tail = ""
            if log_tail:
                detail.insert("end", "\n\n日志摘录:\n")
                detail.insert("end", log_tail)
            detail.configure(state="disabled")

        def _selected_task() -> TaskItem | None:
            sel = tree.selection()
            if not sel:
                return None
            iid = sel[0]
            if self._is_group_iid(iid):
                children = tree.get_children(iid)
                if not children:
                    return None
                return item_map.get(children[0])
            return item_map.get(iid)

        def _open_selected_video():
            t = _selected_task()
            if not t:
                return
            url = (getattr(t, "video_url", "") or "").strip()
            if not url:
                messagebox.showinfo("无链接", "该任务当前还没有可用的视频链接。")
                return
            webbrowser.open(url)

        def _on_detail_double_click(_evt=None):
            sel = tree.selection()
            if not sel:
                return
            iid = sel[0]
            if self._is_group_iid(iid):
                try:
                    is_open = bool(tree.item(iid, "open"))
                    tree.item(iid, open=(not is_open))
                except Exception:
                    pass
                return
            _open_selected_video()

        tree.bind("<<TreeviewSelect>>", lambda _evt=None: _render_detail(_selected_task()))
        tree.bind("<Double-1>", _on_detail_double_click)

        bottom = ttk.Frame(dlg)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bottom, text="打开所选视频", command=_open_selected_video).pack(side="right")
        ttk.Button(bottom, text="关闭", command=dlg.destroy).pack(side="right", padx=(0, 6))

        children = tree.get_children("")
        if children:
            tree.selection_set(children[0])
            tree.focus(children[0])
            _render_detail(_selected_task())

    def open_task_history_dialog(self):
        if not self._require_admin("查看历史任务"):
            return

        dlg = tk.Toplevel(self.root)
        dlg.title(f"历史任务记录 - {TASK_HISTORY_FILE}")
        dlg.geometry("1180x700")
        dlg.transient(self.root)

        top = ttk.Frame(dlg)
        top.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(top, text=f"历史批次：{len(self.task_history)}").pack(side="left")

        body = ttk.Panedwindow(dlg, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=6)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=2)
        body.add(right, weight=3)

        cols = ("batch_id", "source", "added_at", "deleted_at", "total", "deleted")
        tree = ttk.Treeview(left, columns=cols, show="headings", height=18)
        headings = {
            "batch_id": "大组名称",
            "source": "来源",
            "added_at": "添加时间",
            "deleted_at": "最近删除时间",
            "total": "任务数",
            "deleted": "已删",
        }
        widths = {"batch_id": 180, "source": 100, "added_at": 145, "deleted_at": 145, "total": 60, "deleted": 60}
        for c in cols:
            tree.heading(c, text=headings[c])
            tree.column(c, width=widths[c], anchor="w")
        y1 = ttk.Scrollbar(left, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=y1.set)
        tree.pack(side="left", fill="both", expand=True)
        y1.pack(side="right", fill="y")

        detail = tk.Text(right, wrap="word")
        y2 = ttk.Scrollbar(right, orient="vertical", command=detail.yview)
        detail.configure(yscrollcommand=y2.set)
        detail.pack(side="left", fill="both", expand=True)
        y2.pack(side="right", fill="y")

        bottom = ttk.Frame(dlg)
        bottom.pack(fill="x", padx=10, pady=(6, 10))
        status_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=status_var).pack(side="left")

        def _render_detail(rec: dict | None):
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            if not rec:
                detail.configure(state="disabled")
                status_var.set("未选择历史批次。")
                return
            rec = self._ensure_history_record_loaded(rec)
            total, deleted = self._history_record_summary(rec)
            last_deleted_at = self._history_last_deleted_at(rec) or "-"
            detail.insert("end", f"大组名称: {rec.get('batch_name', '') or rec.get('batch_id', '')}\n")
            detail.insert("end", f"批次ID: {rec.get('batch_id', '')}\n")
            detail.insert("end", f"来源: {rec.get('source', '')}\n")
            detail.insert("end", f"添加时间: {rec.get('added_at', '')}\n")
            detail.insert("end", f"最近删除时间: {last_deleted_at}\n")
            detail.insert("end", f"任务数: {total} | 已删除: {deleted}\n\n")
            items = rec.get("items", [])
            for idx, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    continue
                prompt = (item.get("prompt", "") or "").replace("\r", " ").replace("\n", " ").strip()
                if len(prompt) > 160:
                    prompt = prompt[:160] + "..."
                detail.insert("end", f"{idx}. task_id={item.get('task_id', '')}\n")
                detail.insert("end", f"   分组={item.get('group', '')} | 备注={item.get('note', '')}\n")
                detail.insert("end", f"   添加时间={item.get('added_at', '')} | 删除时间={item.get('deleted_at', '') or '-'}\n")
                detail.insert("end", f"   平台={item.get('provider', '')} | 模型={item.get('model', '')}\n")
                detail.insert("end", f"   图片={item.get('image_path', '')}\n")
                detail.insert("end", f"   提示词={prompt}\n\n")
            detail.configure(state="disabled")
            status_var.set(f"批次 {rec.get('batch_id', '')} | 任务 {total} | 已删除 {deleted}")

        def _selected_record() -> tuple[int | None, dict | None]:
            sel = tree.selection()
            if not sel:
                return None, None
            iid = sel[0]
            try:
                idx = int(iid)
            except Exception:
                return None, None
            if idx < 0 or idx >= len(self.task_history):
                return None, None
            return idx, self.task_history[idx]

        def _rebuild():
            tree.delete(*tree.get_children())
            for idx, rec in enumerate(self.task_history):
                total, deleted = self._history_record_summary(rec)
                tree.insert("", "end", iid=str(idx), values=(
                    rec.get("batch_name", "") or rec.get("batch_id", ""),
                    rec.get("source", ""),
                    rec.get("added_at", ""),
                    self._history_last_deleted_at(rec) or "-",
                    total,
                    deleted,
                ))
            if self.task_history:
                first = tree.get_children()
                if first:
                    tree.selection_set(first[0])
                    tree.focus(first[0])
                    _render_detail(self.task_history[0])
            else:
                _render_detail(None)

        def _on_pick(_evt=None):
            _, rec = _selected_record()
            _render_detail(rec)

        def _delete_history_batch():
            idx, rec = _selected_record()
            if rec is None or idx is None:
                return
            batch_id = rec.get("batch_id", "")
            if not messagebox.askyesno("删除历史批次", f"确认删除历史批次 {batch_id} 吗？\n这只删除本地历史记录，不影响当前任务列表。"):
                return
            self.task_history.pop(idx)
            self._save_task_history()
            self.log_line(f"已删除历史批次记录：{batch_id}\n")
            _rebuild()

        def _restore_history_batch():
            _, rec = _selected_record()
            if rec is None:
                return
            batch_name = rec.get("batch_name", "") or rec.get("batch_id", "")
            total, _ = self._history_record_summary(rec)
            if total <= 0:
                messagebox.showinfo("无可恢复任务", "所选历史批次没有可导回的任务。")
                return
            if not messagebox.askyesno(
                "导回历史任务",
                f"确认将历史批次“{batch_name}”中的 {total} 个任务导回当前任务列表吗？\n"
                "导回后会以新的任务ID重新进入当前列表。"
            ):
                return
            restored = self._restore_history_record_to_tasks(rec)
            if restored:
                messagebox.showinfo("导回完成", f"已导回 {restored} 个任务到当前任务列表。")
            else:
                messagebox.showwarning("导回失败", "未能从所选历史批次恢复任何任务。")

        def _open_history_batch():
            _, rec = _selected_record()
            if rec is None:
                return
            self.open_history_batch_detail_window(rec)

        tree.bind("<<TreeviewSelect>>", _on_pick)
        tree.bind("<Double-1>", lambda _evt=None: _open_history_batch())
        ttk.Button(bottom, text="删除所选历史批次", command=_delete_history_batch).pack(side="right")
        ttk.Button(bottom, text="打开所选批次", command=_open_history_batch).pack(side="right", padx=(0, 6))
        ttk.Button(bottom, text="导回当前任务列表", command=_restore_history_batch).pack(side="right", padx=(0, 6))
        ttk.Button(bottom, text="回补旧历史", command=lambda: (self.backfill_task_history(), _rebuild())).pack(side="right", padx=(0, 6))
        ttk.Button(bottom, text="关闭", command=dlg.destroy).pack(side="right", padx=(0, 6))

        _rebuild()

    # ---------- import/export ----------
    def export_tasks_ui(self):
        if not self._require_admin("导出任务文件"):
            return
        p = filedialog.asksaveasfilename(
            title="任务文件 tasks.json",
            defaultextension=".json",
            filetypes=[("JSON文件", "*.json")]
        )
        if not p:
            return
        export_tasks_to(p, self.tasks)
        self.log_line(f"任务已导出到：{p}\n")

    def import_tasks_ui(self):
        if not self._require_admin("导入任务文件"):
            return
        p = filedialog.askopenfilename(
            title="任务文件 tasks.json",
            filetypes=[("JSON文件", "*.json")]
        )
        if not p:
            return

        items = import_tasks_from(p)
        added = 0
        for t in items:
            if not t.task_id or t.task_id in self.tasks:
                t.task_id = self._get_next_tid()
            self._init_task_fields(t)
            self.tasks[t.task_id] = t
            # Add to index
            self.idx.add_task(t)
            added += 1

        self.log_line(f"已导入任务：{added}\n")
        self._normalize_all_paths()
        self._mark_tasks_dirty()
        self._ui_call(self.refresh_table)

    # ---------- batch download ----------
    def _default_download_folder_name(self, tasks: list[TaskItem]) -> str:
        if not tasks:
            return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        batch_names = []
        batch_seen = set()
        for t in tasks:
            name = self._task_batch_name(t).strip()
            if not name:
                name = (getattr(t, "batch_id", None) or "LEGACY").strip() or "LEGACY"
            if name in batch_seen:
                continue
            batch_seen.add(name)
            batch_names.append(name)
        if len(batch_names) == 1:
            return batch_names[0]
        return ""

    def batch_download_ui(self):
        mode = self.download_mode_var.get()
        selected = [self.tasks[tid] for tid in self.selected_task_ids() if tid in self.tasks]
        filtered = self._filtered_tasks()

        tasks_to_download: list[TaskItem] = []
        if mode == "选中任务":
            tasks_to_download = [t for t in selected if t.status == "Success" and (t.video_url or "").strip()]
        elif mode == "筛选后成功":
            tasks_to_download = [t for t in filtered if t.status == "Success" and (t.video_url or "").strip()]
        else:
            tasks_to_download = [
                t for t in filtered
                if (t.status == "Success" and (t.video_url or "").strip()) or (t.status == "Done(No URL)")
            ]

        if not tasks_to_download:
            messagebox.showinfo("无可下载项", "当前选择范围内没有可下载的视频链接。")
            return

        # baoyouhuyu /content requires Bearer token in download request headers.
        need_auth_download = any(
            ((getattr(t, "provider", "") or "").strip().lower() == "baoyouhuyu")
            for t in tasks_to_download
        )
        if need_auth_download and not (self.api_key_var.get() or "").strip():
            if not self._ensure_runtime_api_key("下载受保护视频"):
                self.log_line("已取消下载：未提供可用接口密钥。")
                return

        initial_dir = norm_path((self.download_root_var.get() or "").strip())
        pick_dir = initial_dir
        if not pick_dir:
            pick_dir = filedialog.askdirectory(
                title="选择本次下载目录",
                initialdir=None,
            )
            if not pick_dir:
                self.log_line("已取消下载：未选择下载目录。")
                return
            pick_dir = norm_path(pick_dir)
            self.download_root_var.set(pick_dir)

        default_folder_name = self._default_download_folder_name(tasks_to_download)
        folder_name = default_folder_name

        total = len(tasks_to_download)
        with_url = sum(1 for t in tasks_to_download if (t.video_url or "").strip())
        done_no_url = sum(1 for t in tasks_to_download if t.status == "Done(No URL)")
        retries = _safe_int(self.dl_retries_var.get(), 2)
        workers = max(1, _safe_int(self.dl_workers_var.get(), 10))
        skip_dup = bool(self.dl_skip_dup_var.get())
        final_dir_preview = str(Path(pick_dir) / folder_name) if folder_name else str(Path(pick_dir))

        confirm_msg = (
            f"请确认下载信息：\n\n"
            f"下载模式：{mode}\n"
            f"任务总数：{total}\n"
            f"可下载URL任务：{with_url}\n"
            f"完成(无链接)任务：{done_no_url}\n"
            f"下载目录：{pick_dir}\n"
            f"下载文件夹名（自动生成）：{folder_name or '直接使用下载根目录'}\n"
            f"最终输出路径：{final_dir_preview}\n"
            f"下载线程：{workers}\n"
            f"失败重试：{retries}\n"
            f"跳过重复：{'是' if skip_dup else '否'}\n\n"
            f"确认后将开始下载。"
        )
        if not messagebox.askyesno("确认下载", confirm_msg):
            self.log_line("已取消下载：用户未确认。")
            return

        self._run_batch_download_logic(
            tasks_to_download,
            base_root_override=pick_dir,
            folder_name_override=folder_name,
        )

    def _run_batch_download_logic(
            self,
            tasks: list[TaskItem],
            base_root_override: str | None = None,
            folder_name_override: str | None = None,
    ):
        # 1) 选择下载根目录（可由本次下载自定义）
        base_root_raw = (base_root_override or self.download_root_var.get() or "").strip()
        base_root = Path(base_root_raw)
        base_root.mkdir(parents=True, exist_ok=True)

        # 单大组时：下载根目录/大组名
        # 多大组时：直接使用下载根目录，下面再按大组名归档
        folder_name = (folder_name_override or "").strip()
        out_root = (base_root / folder_name) if folder_name else base_root
        out_root.mkdir(parents=True, exist_ok=True)

        # 3) index 
        index_path = out_root / DOWNLOAD_INDEX_FILE

        stop_flag = threading.Event()

        dlg = tk.Toplevel(self.root)
        dlg.title("批量下载")
        dlg.geometry("560x240")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(
            dlg,
            text=f"正在准备下载：{len(tasks)} 个任务\n输出目录：{out_root}"
        ).pack(pady=10)

        overall = ttk.Progressbar(dlg, length=500, mode="determinate")
        overall.pack(pady=6)
        current_lbl = ttk.Label(dlg, text="准备中...")
        current_lbl.pack(pady=6)

        btns = ttk.Frame(dlg)
        btns.pack(pady=10)
        ttk.Button(btns, text="停止下载", command=lambda: stop_flag.set()).pack()

        def ui_update_overall(done, total):
            def _():
                overall.configure(maximum=total, value=done)
                current_lbl.configure(text=f"进度：{done} / {total}")

            self._enqueue_ui(_)

        def ui_done(ok, skipped, failed):
            def _():
                downloaded_urls = set()
                try:
                    idx_obj = json.loads(index_path.read_text(encoding="utf-8", errors="ignore")) if index_path.exists() else {}
                    if isinstance(idx_obj, dict):
                        downloaded_urls = {str(k).strip() for k in idx_obj.keys() if str(k).strip()}
                except Exception:
                    downloaded_urls = set()

                if downloaded_urls:
                    for t in tasks:
                        try:
                            vurl = (getattr(t, "video_url", "") or "").strip()
                            if not vurl:
                                continue
                            if vurl not in downloaded_urls:
                                continue
                            if t.task_id in self.tasks:
                                self._set_task_status(t, "Downloaded")
                                t.status_msg = "已下载"
                                self._ui_update_task_row(t)
                            else:
                                t.status = "Downloaded"
                                t.status_msg = "已下载"
                        except Exception:
                            pass
                try:
                    dlg.destroy()
                except Exception:
                    pass
                messagebox.showinfo(
                    "下载完成",
                    f"成功：{ok}\n跳过：{skipped}\n失败：{failed}\n\n输出目录：\n{out_root}"
                )

            self._enqueue_ui(_)

        def log_cb(msg: str):
            self._enqueue_ui(self.log_line, msg)

        dl_workers = max(1, _safe_int(self.dl_workers_var.get(), 10))
        retries = _safe_int(self.dl_retries_var.get(), 2)
        skip = bool(self.dl_skip_dup_var.get())

        download_tasks: list[TaskItem] = []
        batch_names = []
        for t in tasks:
            name = self._task_batch_name(t).strip() or ((getattr(t, "batch_id", None) or "LEGACY").strip() or "LEGACY")
            if name not in batch_names:
                batch_names.append(name)
        single_batch_name = batch_names[0] if len(batch_names) == 1 else ""
        for t in tasks:
            try:
                import copy
                t2 = copy.copy(t)
                small_group = (getattr(t, "group", "") or "").strip() or "未分组"
                t2.group = small_group
                resolved_batch_name = self._task_batch_name(t).strip() or ((getattr(t, "batch_id", None) or "LEGACY").strip() or "LEGACY")
                t2.batch_name = "" if (single_batch_name and resolved_batch_name == single_batch_name) else resolved_batch_name
                download_tasks.append(t2)
            except Exception:
                download_tasks.append(t)

        #
        self.log_line(f"批量下载输出目录：{out_root}\n")
        if len(batch_names) == 1:
            self.log_line(f"下载归档结构：{batch_names[0]} / 小组名 / 视频文件\n")
        else:
            self.log_line("下载归档结构：大组名 / 小组名 / 视频文件\n")

        thread = threading.Thread(
            target=run_batch_download,
            args=(
                self.root,
                download_tasks,
                out_root,
                index_path,
                retries,
                skip,
                dl_workers,
                stop_flag,
                log_cb,
                ui_update_overall,
                None,
                ui_done,
                (self.api_key_var.get() or "").strip(),
            ),
            daemon=True
        )
        thread.start()

    def _task_signature_key(self, group: str, prompt: str, image_path: str) -> tuple[str, str, str]:
        """Signature for duplicate detection."""
        return ((group or "").strip(), (prompt or "").strip(), norm_path(image_path or ""))

    @staticmethod
    def _norm_group_key(s: str) -> str:
        return norm_path((s or "").strip()).replace("\\", "/").strip("/").lower()

    def _parse_subdir_names(self) -> list[str]:
        raw = (self.subdir_name_list_var.get() or "").strip()
        if not raw:
            return []
        parts = re.split(r"[\n,;，；]+", raw)
        out: list[str] = []
        seen = set()
        for p in parts:
            v = p.strip()
            if not v:
                continue
            k = v.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(v)
        return out

    @staticmethod
    def _candidate_summary(c: dict) -> str:
        g = display_last_part(c.get("group", "") or "")
        note = (c.get("note", "") or "").strip()
        prompt = (c.get("prompt", "") or "").replace("\n", " ").strip()
        cnt = _safe_int(c.get("repeat_count", 1), 1)
        if cnt < 1:
            cnt = 1
        if len(prompt) > 48:
            prompt = prompt[:48] + "..."
        return f"[{g}] {note} | {prompt} | 重复数量: {cnt}"

    @staticmethod
    def _strip_repeat_suffix(note: str) -> str:
        s = (note or "").strip()
        if not s:
            return ""
        s = re.sub(r"\s*#\d+/\d+\s*$", "", s).strip()
        return s

    def _aggregate_scan_candidates(self, items: list[dict]) -> list[dict]:
        merged: dict[tuple[str, str, str], dict] = {}
        for c in items:
            key = self._task_signature_key(c.get("group", ""), c.get("prompt", ""), c.get("image_path", ""))
            cnt = _safe_int(c.get("repeat_count", 1), 1)
            if cnt < 1:
                cnt = 1
            note = self._strip_repeat_suffix((c.get("note", "") or "").strip())
            hit = merged.get(key)
            if hit is None:
                cc = dict(c)
                cc["repeat_count"] = cnt
                cc["_notes"] = [note] if note else []
                merged[key] = cc
                continue
            hit["repeat_count"] = _safe_int(hit.get("repeat_count", 1), 1) + cnt
            if note:
                notes = hit.get("_notes") or []
                if note not in notes:
                    notes.append(note)
                hit["_notes"] = notes

        out: list[dict] = []
        for c in merged.values():
            notes = [x for x in (c.get("_notes") or []) if x]
            if len(notes) >= 2:
                c["note"] = f"{self._strip_repeat_suffix(notes[0])}等{len(notes)}项"
            elif len(notes) == 1:
                c["note"] = self._strip_repeat_suffix(notes[0])
            c.pop("_notes", None)
            out.append(c)
        return out

    def _pick_candidates_dialog(self, candidates: list[dict]) -> list[dict] | None:
        """Scan result picker: search + multi-select. Return None when canceled."""
        if not candidates:
            return []

        dlg = tk.Toplevel(self.root)
        dlg.title("选择要加入的任务")
        dlg.geometry("980x620")
        dlg.transient(self.root)
        dlg.grab_set()

        selected_actual: set[int] = set()
        filtered_idx: list[int] = []
        search_var = tk.StringVar(value="")
        result: dict[str, list[dict] | None] = {"selected": None}
        total_task_count = sum(max(1, _safe_int(c.get("repeat_count", 1), 1)) for c in candidates)

        top = ttk.Frame(dlg)
        top.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(top, text=f"扫描到任务候选：{len(candidates)} 组 / {total_task_count} 个任务").pack(side="left")
        ttk.Label(top, text="关键字搜索:").pack(side="left", padx=(16, 4))
        ent = ttk.Entry(top, textvariable=search_var, width=40)
        ent.pack(side="left")

        mid = ttk.Frame(dlg)
        mid.pack(fill="both", expand=True, padx=10, pady=6)
        lb = tk.Listbox(mid, selectmode="extended")
        sb = ttk.Scrollbar(mid, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        foot = ttk.Frame(dlg)
        foot.pack(fill="x", padx=10, pady=(6, 10))
        status_lbl = ttk.Label(foot, text="可见: 0 | 已选: 0组 | 任务数: 0")
        status_lbl.pack(side="left")

        def _update_status():
            sel_total = 0
            for idx in selected_actual:
                if 0 <= idx < len(candidates):
                    sel_total += max(1, _safe_int(candidates[idx].get("repeat_count", 1), 1))
            status_lbl.configure(text=f"可见: {len(filtered_idx)} | 已选: {len(selected_actual)}组 | 任务数: {sel_total}")

        def _capture_visible_selection():
            for pos in lb.curselection():
                if 0 <= pos < len(filtered_idx):
                    selected_actual.add(filtered_idx[pos])

        def _rebuild():
            _capture_visible_selection()
            q = (search_var.get() or "").strip().lower()
            lb.delete(0, "end")
            filtered_idx.clear()
            for i, c in enumerate(candidates):
                hay = f"{c.get('group', '')} {c.get('note', '')} {c.get('prompt', '')} {c.get('image_path', '')}".lower()
                if q and (q not in hay):
                    continue
                filtered_idx.append(i)
                lb.insert("end", self._candidate_summary(c))
            for pos, idx in enumerate(filtered_idx):
                if idx in selected_actual:
                    lb.selection_set(pos)
            _update_status()

        def _select_all_visible():
            for idx in filtered_idx:
                selected_actual.add(idx)
            _rebuild()

        def _clear_all():
            selected_actual.clear()
            _rebuild()

        def _ok():
            _capture_visible_selection()
            if not selected_actual:
                messagebox.showwarning("未选择任务", "请先选择至少一个任务再继续。")
                return
            picked = [candidates[i] for i in sorted(selected_actual)]
            result["selected"] = picked
            dlg.destroy()

        def _cancel():
            result["selected"] = None
            dlg.destroy()

        btns = ttk.Frame(foot)
        btns.pack(side="right")
        ttk.Button(btns, text="全选可见", command=_select_all_visible).pack(side="left", padx=4)
        ttk.Button(btns, text="清空选择", command=_clear_all).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=_cancel).pack(side="left", padx=10)
        ttk.Button(btns, text="确认加入", command=_ok).pack(side="left")

        search_var.trace_add("write", lambda *_: _rebuild())
        ent.focus_set()
        _rebuild()
        dlg.wait_window()
        return result["selected"]

    def _safe_mode_precheck(self, planned_add: int, source: str) -> bool:
        """Central anti-waste guard for task creation."""
        if planned_add <= 0:
            return False
        if not self.safe_mode_var.get():
            return True

        total_now = len(self.tasks)
        if total_now + planned_add > SAFE_MODE_MAX_TOTAL_TASKS:
            messagebox.showwarning(
                "安全模式限制",
                f"当前任务数={total_now}，本次计划新增={planned_add}。\n"
                f"超过上限 {SAFE_MODE_MAX_TOTAL_TASKS}，已阻止添加。"
            )
            return False

        if planned_add > SAFE_MODE_MAX_ADD_COUNT:
            messagebox.showwarning(
                "安全模式限制",
                f"{source} 本次计划新增 {planned_add} 个任务，超过上限 {SAFE_MODE_MAX_ADD_COUNT}。\n"
                f"请缩小范围后再试。"
            )
            return False

        if planned_add >= SAFE_MODE_CONFIRM_THRESHOLD:
            queued_n = len(self.idx.by_status.get("Queued", set()))
            running_n = len(self.idx.by_status.get("Running", set()))
            if not messagebox.askyesno(
                    "批量添加确认",
                    f"{source} 即将新增 {planned_add} 个任务。\n"
                    f"当前排队={queued_n}，运行中={running_n}。\n"
                    f"继续可能增加 API 调度开销，是否继续？"
            ):
                return False
        return True

    def _set_widgets_state(self, widgets: list, state: str):
        for w in widgets:
            try:
                w.configure(state=state)
            except Exception:
                pass

    def _toggle_advanced_panel(self):
        if self.advanced_visible_var.get():
            try:
                self.opt_frame.pack_forget()
            except Exception:
                pass
            self.advanced_visible_var.set(False)
            try:
                self.btn_toggle_adv.configure(text="显示高级选项")
            except Exception:
                pass
        else:
            try:
                self.opt_frame.pack(fill="x", padx=8, pady=4, after=self.filt_frame)
            except Exception:
                pass
            self.advanced_visible_var.set(True)
            try:
                self.btn_toggle_adv.configure(text="隐藏高级选项")
            except Exception:
                pass

    def _apply_role_mode(self):
        is_admin = bool(self.admin_mode_var.get())
        try:
            self.role_state_label.configure(
                text=("角色：管理员" if is_admin else "角色：普通用户"),
                foreground=("green" if is_admin else "blue"),
            )
        except Exception:
            pass

        if is_admin:
            self._set_widgets_state(getattr(self, "_admin_only_widgets", []), "normal")
            self._set_widgets_state(getattr(self, "_normal_lock_buttons", []), "normal")
            self.btn_unlock_admin.configure(state="disabled")
            self.btn_lock_normal.configure(state="normal")
            self._set_widget_visible(self.btn_unlock_admin, False)
            self._set_widget_visible(self.btn_lock_normal, True, side="left", padx=4)
            self._set_widget_visible(self.btn_restore_admin_full, True, side="left", padx=6)
            # show technical controls for admin
            self._set_widget_visible(self.lbl_passphrase, True, side="left")
            self._set_widget_visible(self.ent_passphrase, True, side="left", padx=6)
            self._set_widget_visible(self.btn_load_key, True, side="left")
            self._set_widget_visible(self.btn_save_key, True, side="left", padx=6)
            self._set_widget_visible(self.key_state, True, side="left", padx=8)
            for w in getattr(self, "_tech_grid_widgets", []):
                self._set_widget_visible(w, True)
            for w in getattr(self, "_path_grid_widgets", []):
                self._set_widget_visible(w, True)
        else:
            self.safe_mode_var.set(True)
            self._set_widgets_state(getattr(self, "_admin_only_widgets", []), "disabled")
            self._set_widgets_state(getattr(self, "_normal_lock_buttons", []), "disabled")
            self.btn_unlock_admin.configure(state="normal")
            self.btn_lock_normal.configure(state="disabled")
            self._set_widget_visible(self.btn_unlock_admin, True, side="left", padx=4)
            self._set_widget_visible(self.btn_lock_normal, False)
            self._set_widget_visible(self.btn_restore_admin_full, False)
            # hide technical controls in normal mode
            self._set_widget_visible(self.lbl_passphrase, False)
            self._set_widget_visible(self.ent_passphrase, False)
            self._set_widget_visible(self.btn_load_key, False)
            self._set_widget_visible(self.btn_save_key, False)
            self._set_widget_visible(self.key_state, False)
            for w in getattr(self, "_tech_grid_widgets", []):
                self._set_widget_visible(w, False)
            for w in getattr(self, "_path_grid_widgets", []):
                self._set_widget_visible(w, True)
        self._apply_novice_mode()

    def _set_widget_visible(self, w, visible: bool, **pack_kw):
        try:
            mgr = w.winfo_manager()
            if visible:
                if mgr:
                    return
                # If caller gives pack args, treat as pack widget.
                if pack_kw:
                    w.pack(**pack_kw)
                    return
                cached_pack = getattr(w, "_pack_info_cache", None)
                if cached_pack:
                    w.pack(**cached_pack)
                    return
                # Default: try restoring as grid widget first (for grid_remove case).
                try:
                    gi = getattr(w, "_grid_info_cache", None)
                    if gi:
                        w.grid(**gi)
                    else:
                        w.grid()
                except Exception:
                    w.pack()
            else:
                if mgr == "grid":
                    w.grid_remove()
                elif mgr == "pack":
                    w.pack_forget()
        except Exception:
            pass

    def _apply_novice_mode(self):
        novice = bool(self.novice_mode_var.get())
        if not novice:
            self._set_widget_visible(self.beginner_status_frame, True, fill="x", padx=8, pady=(0, 4))
            self._set_widget_visible(self.single_extra_frame, self.task_input_mode_var.get() != "batch")
            self._set_widget_visible(self.batch_advanced_frame, self.task_input_mode_var.get() == "batch", fill="x", padx=8, pady=(0, 6))
            self._set_widget_visible(self.quick_ops_frame, True, side="left", padx=(0, 8))
            self._set_widget_visible(self.run_ops_frame, True, side="left", padx=(0, 8))
            self._set_widget_visible(self.group_ops_frame, True, side="left", padx=(0, 8))
            self._set_widget_visible(self.danger_ops_frame, True, side="left")
            self._set_widget_visible(self.tools_ops_frame, True, side="left", padx=(0, 8))
            self._set_widget_visible(self.download_ops_frame, True, side="left", padx=(0, 8))
            self._set_widget_visible(self.advanced_toggle_frame, True, side="left")
            self._set_widget_visible(self.btn_more_actions, True, side="left", padx=6, pady=6)
            self._set_widget_visible(self.btn_toggle_adv, True, side="left", padx=6, pady=6)
            self._set_widget_visible(self.filt_frame, True, fill="x", padx=10, pady=4)
            self._set_widget_visible(self.btn_load_latest, True, side="left", padx=6, pady=6)
            self._set_widget_visible(self.btn_poll_remote, True, side="left", padx=6, pady=6)
            self._set_widget_visible(self.chk_start_poll_only, True, side="left", padx=6, pady=6)
            return

        self._set_widget_visible(self.beginner_status_frame, True, fill="x", padx=8, pady=(0, 4))
        self._set_widget_visible(self.single_extra_frame, False)
        self._set_widget_visible(self.batch_advanced_frame, False)
        self._set_widget_visible(self.group_ops_frame, False)
        self._set_widget_visible(self.danger_ops_frame, False)
        self._set_widget_visible(self.tools_ops_frame, False)
        self._set_widget_visible(self.filt_frame, False)
        self._set_widget_visible(self.advanced_toggle_frame, True, side="left")
        self._set_widget_visible(self.btn_more_actions, True, side="left", padx=6, pady=6)
        self._set_widget_visible(self.filt_frame, False)
        self._set_widget_visible(self.btn_load_latest, False)
        self._set_widget_visible(self.btn_poll_remote, False)
        self._set_widget_visible(self.chk_start_poll_only, False)
        self._set_widget_visible(self.btn_toggle_adv, False)
        self._apply_task_input_mode()

    def open_more_actions_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("更多操作")
        dlg.geometry("520x340")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(
            dlg,
            text="这里放的是分组、筛选、手动重试、历史和管理员功能。\n日常使用只需要“添加任务 / 开始执行 / 下载结果”。",
            justify="left",
        ).pack(fill="x", padx=12, pady=(12, 8))

        group = ttk.LabelFrame(dlg, text="分组操作")
        group.pack(fill="x", padx=12, pady=6)
        ttk.Button(group, text="收起所选分组", command=self.collapse_selected_batches).pack(side="left", padx=6, pady=6)
        ttk.Button(group, text="展开所选分组", command=self.expand_selected_batches).pack(side="left", padx=6, pady=6)
        ttk.Button(group, text="修改大组名称", command=self.rename_selected_batch).pack(side="left", padx=6, pady=6)

        task = ttk.LabelFrame(dlg, text="任务操作")
        task.pack(fill="x", padx=12, pady=6)
        ttk.Button(task, text="刷新选中状态", command=self.poll_selected_remote_id).pack(side="left", padx=6, pady=6)
        ttk.Button(task, text="重试选中", command=self.retry_selected).pack(side="left", padx=6, pady=6)
        ttk.Button(task, text="停止选中", command=self.stop_selected).pack(side="left", padx=6, pady=6)
        ttk.Button(task, text="删除选中", command=self.delete_selected).pack(side="left", padx=6, pady=6)

        tools = ttk.LabelFrame(dlg, text="历史与工具")
        tools.pack(fill="x", padx=12, pady=6)
        history_state = "normal" if self.admin_mode_var.get() else "disabled"
        history_tip = "历史任务仅管理员可查看" if not self.admin_mode_var.get() else ""
        ttk.Button(tools, text="查看历史", command=self.open_task_history_dialog, state=history_state).pack(side="left", padx=6, pady=6)
        ttk.Button(tools, text="导出任务", command=self.export_tasks_ui, state=history_state).pack(side="left", padx=6, pady=6)
        ttk.Button(tools, text="导入任务", command=self.import_tasks_ui, state=history_state).pack(side="left", padx=6, pady=6)
        ttk.Button(tools, text="自检", command=self.run_ui_self_check, state=history_state).pack(side="left", padx=6, pady=6)
        if history_tip:
            ttk.Label(tools, text=history_tip, foreground="#b45309").pack(anchor="w", padx=8, pady=(0, 6))

        view = ttk.LabelFrame(dlg, text="界面")
        view.pack(fill="x", padx=12, pady=6)
        ttk.Button(view, text="显示筛选和高级选项", command=lambda: (self._set_widget_visible(self.filt_frame, True, fill="x", padx=10, pady=4), self._toggle_advanced_panel() if not self.advanced_visible_var.get() else None)).pack(side="left", padx=6, pady=6)
        ttk.Button(view, text="切换完整界面", command=self.restore_full_admin_layout).pack(side="left", padx=6, pady=6)
        ttk.Button(view, text="关闭", command=dlg.destroy).pack(side="right", padx=6, pady=6)

    def unlock_admin_mode(self):
        if not self._passphrase_verified:
            entered = (self.passphrase_var.get() or "").strip()
            if not entered:
                entered = self._ask_passphrase("管理员解锁", "请输入口令：")
            if not entered:
                return
            if not self._verify_passphrase_only(entered):
                messagebox.showerror("解锁失败", "口令不正确。")
                return
        self.admin_mode_var.set(True)
        # Admin unlock should restore full interface, not novice simplified view.
        self.novice_mode_var.set(False)
        self._apply_role_mode()
        self.log_line("已进入管理员模式。")

    def lock_to_normal_mode(self):
        self.admin_mode_var.set(False)
        self._apply_role_mode()
        self.log_line("已切回普通用户模式（安全模式已强制开启）。")

    def restore_full_admin_layout(self):
        """One-click hard restore for full admin UI layout."""
        if not self.admin_mode_var.get():
            self.unlock_admin_mode()
            if not self.admin_mode_var.get():
                return

        self.novice_mode_var.set(False)
        self.advanced_visible_var.set(True)
        self._apply_role_mode()

        try:
            self.filt_frame.pack_forget()
        except Exception:
            pass
        try:
            self.filt_frame.pack(fill="x", padx=8, pady=2)
        except Exception:
            pass
        try:
            self.opt_frame.pack_forget()
        except Exception:
            pass
        try:
            self.opt_frame.pack(fill="x", padx=8, pady=4, after=self.filt_frame)
        except Exception:
            pass
        try:
            self.btn_toggle_adv.configure(text="隐藏高级选项")
        except Exception:
            pass

        self.log_line("已恢复完整管理员界面。")

    def _confirm_budget(self, planned_add: int, source: str) -> bool:
        if planned_add <= 0:
            return False
        if not self.safe_mode_var.get():
            return True
        est = round(float(planned_add) * float(COST_PER_REQUEST), 2)
        return messagebox.askyesno(
            "预算确认",
            f"{source} 计划新增任务：{planned_add}\n"
            f"按单次成本 {COST_PER_REQUEST} 估算，潜在调用成本约：{est}\n\n"
            f"是否继续？"
        )

    def _require_admin(self, action_name: str) -> bool:
        if self.admin_mode_var.get():
            return True
        messagebox.showwarning("权限不足", f"当前为普通用户模式，无法执行：{action_name}")
        return False

    def _diff_task_params(self, exist: TaskItem, cand: dict) -> list[str]:
        """Show provider/base/model differences for duplicates."""
        diffs = []
        checks = [
            ("provider", "provider"),
            ("base_url", "base_url"),
            ("model", "model"),
        ]
        for k_exist, k_cand in checks:
            a = (getattr(exist, k_exist, "") or "").strip()
            b = (cand.get(k_cand, "") or "").strip()
            if k_exist == "base_url":
                a = _norm_maybe_url(a)
                b = _norm_maybe_url(b)
            if a != b:
                diffs.append(f"{k_exist}: '{a}' -> '{b}'")
        return diffs

    def _check_dups_before_bulk_add(self, candidates: list[dict]) -> tuple[bool, bool]:
        """Return (proceed, allow_dups) for bulk import duplicate handling."""
        idx: dict[tuple[str, str, str], list[TaskItem]] = {}
        for t in self.tasks.values():
            key = self._task_signature_key(t.group or "", t.prompt or "", t.image_path or "")
            idx.setdefault(key, []).append(t)

        dup_exact = []
        dup_modified = []
        for c in candidates:
            key = self._task_signature_key(c.get("group", ""), c.get("prompt", ""), c.get("image_path", ""))
            exist_list = idx.get(key) or []
            if not exist_list:
                continue

            e0 = exist_list[0]
            diffs = self._diff_task_params(e0, c)
            if diffs:
                dup_modified.append((c, diffs, len(exist_list)))
            else:
                dup_exact.append((c, len(exist_list)))

        if not dup_exact and not dup_modified:
            return True, True

        lines = ["检测到重复任务："]
        if dup_exact:
            lines.append(f"- 完全重复：{len(dup_exact)}")
        if dup_modified:
            lines.append(f"- 内容相同但参数不同：{len(dup_modified)}")
        lines.append("")
        show_n = 5

        if dup_modified:
            lines.append("[示例：签名相同但参数不同]")
            for (c, diffs, existed_n) in dup_modified[:show_n]:
                lines.append(f"  - group={c.get('group', '')} | note={c.get('note', '')}")
                lines.append(f"    已存在重复数：{existed_n}")
                for d in diffs[:6]:
                    lines.append(f"    - {d}")
            if len(dup_modified) > show_n:
                lines.append(f"  - 以及另外 {len(dup_modified) - show_n} 条")
            lines.append("")

        if dup_exact:
            lines.append("[示例：完全重复]")
            for (c, existed_n) in dup_exact[:show_n]:
                lines.append(f"  - group={c.get('group', '')} | note={c.get('note', '')} | existing={existed_n}")
            if len(dup_exact) > show_n:
                lines.append(f"  - 以及另外 {len(dup_exact) - show_n} 条")

        lines.append("")
        lines.append("是否继续导入重复任务？")
        lines.append("是=全部导入，否=跳过重复，取消=终止本次导入")

        ans = messagebox.askyesnocancel("重复任务", "\n".join(lines))
        if ans is None:
            return False, False
        if ans is False:
            return True, False
        return True, True

    # ---------- add/import mission () ----------
    def import_mission_all(self):
        """Scan mission root and enqueue tasks from txt+image folders."""
        root_dir = (self.mission_root_var.get() or "").strip()
        root_dir = norm_path(root_dir)
        if not root_dir or not os.path.isdir(root_dir):
            messagebox.showerror("路径错误", "任务根目录不存在，请选择有效目录。")
            return

        # if key is missing, allow queueing but keep scheduler paused
        if (not self.key_loaded_var.get()) and (not (self.api_key_var.get() or "").strip()):
            self.pause_new_tasks_var.set(True)
            self.log_line("已进入安全等待：任务可先加入，真正开始时会弹窗输入口令。\n")

        limit_each = self.mission_n_each_var.get()
        limit_each = int(limit_each) if isinstance(limit_each, int) else 0
        if limit_each < 0:
            limit_each = 0  # 0=
        if self.safe_mode_var.get():
            if limit_each == 0:
                limit_each = SAFE_MODE_DEFAULT_IMPORT_PER_DIR
                self.log_line(f"安全模式：每目录最大未设置，自动限制为 {limit_each}")
            elif limit_each > SAFE_MODE_MAX_IMPORT_PER_DIR:
                limit_each = SAFE_MODE_MAX_IMPORT_PER_DIR
                self.log_line(f"安全模式：每目录最大已限制为 {limit_each}")

        repeat = _safe_int(self.repeat_add_var.get(), 1)
        if repeat < 1:
            repeat = 1
        if self.safe_mode_var.get() and repeat > SAFE_MODE_MAX_REPEAT:
            self.log_line(f"安全模式：重复次数从 {repeat} 限制为 {SAFE_MODE_MAX_REPEAT}")
            repeat = SAFE_MODE_MAX_REPEAT

        provider = (self.provider_var.get() or "").strip()
        base_url = (self.base_url_var.get() or "").strip()
        model = (self.model_var.get() or "").strip()
        requested_subdirs = self._parse_subdir_names()
        req_full_keys = {self._norm_group_key(x) for x in requested_subdirs}
        req_last_keys = {
            (Path(x.replace("\\", "/")).name.strip().lower()) for x in requested_subdirs if x.strip()
        }
        if requested_subdirs:
            self.log_line(f"子目录筛选已启用：{len(requested_subdirs)} 项")

        if requested_subdirs and self.require_existing_subdir_var.get():
            existing_full = set()
            existing_last = set()
            for t in self.tasks.values():
                g = (getattr(t, "group", "") or "").strip()
                if not g:
                    continue
                g_key = self._norm_group_key(g)
                if g_key:
                    existing_full.add(g_key)
                g_last = Path(g.replace("\\", "/")).name.strip().lower()
                if g_last:
                    existing_last.add(g_last)

            missing_subdirs: list[str] = []
            for name in requested_subdirs:
                nk = self._norm_group_key(name)
                nl = Path(name.replace("\\", "/")).name.strip().lower()
                if (nk not in existing_full) and (nl not in existing_last):
                    missing_subdirs.append(name)

            if missing_subdirs:
                preview = "\n".join(f"- {x}" for x in missing_subdirs[:20])
                more = "" if len(missing_subdirs) <= 20 else f"\n... 另外 {len(missing_subdirs) - 20} 项"
                msg = (
                    "以下子目录在当前任务列表中未找到对应任务：\n\n"
                    f"{preview}{more}\n\n"
                    "是否仍继续添加？"
                )
                if not messagebox.askyesno("子目录校验提醒", msg):
                    self.log_line("批量入队已取消：存在未找到的子目录任务。")
                    return

        #  txt 
        try:
            from .utils import read_text_safely  # type: ignore
        except Exception:
            def read_text_safely(p: Path) -> str:
                return p.read_text(encoding="utf-8", errors="ignore")

        root_path = Path(root_dir)
        self.log_line(f"扫描目录：{root_path}\n")
        self.log_line(f"重复次数={repeat} | 每目录上限={limit_each if limit_each != 0 else '不限'}\n")

        # Scan by first-level mission folders, then search recursively inside each
        # folder so nested prompt/image layouts are still importable.
        all_dirs = [p for p in sorted(root_path.iterdir()) if p.is_dir()]
        if not all_dirs and root_path.is_dir():
            all_dirs = [root_path]
        desired_batch_name = (self.batch_name_var.get() or "").strip() or self._default_batch_name_for_dir(str(root_path))
        batch_id, batch_name, reused_existing_batch = self._resolve_target_batch_for_name(desired_batch_name, ask_merge=True)

        candidates: list[dict] = []
        skipped_dirs: list[str] = []
        for sub in all_dirs:
            txt_files = [p for p in sorted(sub.rglob("*.txt")) if p.name.lower() != "url.txt"]
            if not txt_files:
                skipped_dirs.append(f"{sub.name}: 缺少 txt")
                continue

            img_files: list[Path] = []
            for pat in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.PNG", "*.JPG", "*.JPEG", "*.WEBP"):
                img_files.extend(list(sub.rglob(pat)))
            if not img_files:
                skipped_dirs.append(f"{sub.name}: 缺少图片")
                continue

            img_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            default_img = norm_path(str(img_files[0]))
            if not default_img or not Path(default_img).exists():
                skipped_dirs.append(f"{sub.name}: 图片路径无效")
                continue

            group_name = sub.name
            if requested_subdirs:
                g_key = self._norm_group_key(group_name)
                g_last = Path(group_name.replace("\\", "/")).name.strip().lower()
                if (g_key not in req_full_keys) and (g_last not in req_last_keys):
                    continue
            produce_txts = txt_files if limit_each == 0 else txt_files[:limit_each]
            added_for_sub = 0
            empty_prompt_count = 0

            for txtp in produce_txts:
                try:
                    prompt_content = read_text_safely(txtp).strip()
                except Exception:
                    prompt_content = ""

                if not prompt_content:
                    empty_prompt_count += 1
                    continue

                candidates.append({
                    "provider": provider,
                    "base_url": base_url,
                    "model": model,
                    "prompt": prompt_content,
                    "image_path": default_img,
                    "note": self._strip_repeat_suffix(txtp.stem),
                    "group": group_name,
                    "batch_id": batch_id,
                    "batch_name": batch_name,
                    "repeat_count": repeat,
                })
                added_for_sub += 1

            if added_for_sub == 0:
                if empty_prompt_count > 0:
                    skipped_dirs.append(f"{sub.name}: txt 内容为空")
                else:
                    skipped_dirs.append(f"{sub.name}: 未生成有效任务")

        if not candidates:
            self.log_line("未发现可导入的 txt+图片 任务。\n")
            return

        if skipped_dirs:
            preview = "；".join(skipped_dirs[:10])
            more = "" if len(skipped_dirs) <= 10 else f"；另外 {len(skipped_dirs) - 10} 个目录被跳过"
            self.log_line(f"扫描时跳过目录：{preview}{more}\n")

        candidates = self._aggregate_scan_candidates(candidates)
        picked = self._pick_candidates_dialog(candidates)
        if picked is None:
            self.log_line("批量入队已取消：用户关闭了任务选择窗口。")
            return
        if not picked:
            self.log_line("批量入队已取消：未选择任何任务。")
            return
        candidates = picked
        planned_tasks = sum(max(1, _safe_int(c.get("repeat_count", 1), 1)) for c in candidates)
        self.log_line(f"手动选择后待入队：{len(candidates)} 组，共 {planned_tasks} 个任务")

        if not self._safe_mode_precheck(planned_tasks, "批量扫描入队"):
            self.log_line("批量入队已取消：触发安全模式保护。")
            return
        if not self._confirm_budget(planned_tasks, "批量扫描入队"):
            self.log_line("批量入队已取消：未通过预算确认。")
            return

        # 用户已在候选窗口手动确认，本批次默认不过滤重复。
        allow_dups = True
        hist_sig = set()
        if not allow_dups:
            for t in self.tasks.values():
                hist_sig.add(self._task_signature_key(t.group or "", t.prompt or "", t.image_path or ""))

        created_count = 0
        skipped_dup = 0
        created_tasks: list[TaskItem] = []

        for c in candidates:
            sig = self._task_signature_key(c["group"], c["prompt"], c["image_path"])

            if (not allow_dups) and (sig in hist_sig):
                skipped_dup += max(1, _safe_int(c.get("repeat_count", 1), 1))
                continue

            rep = max(1, _safe_int(c.get("repeat_count", 1), 1))
            note_base = self._strip_repeat_suffix((c.get("note", "") or "").strip())
            for i in range(rep):
                note_i = note_base
                if rep > 1:
                    note_i = f"{note_base} #{i + 1}/{rep}" if note_base else f"#{i + 1}/{rep}"
                t_new = self._create_task_and_add(
                    provider=c["provider"],
                    base_url=c["base_url"],
                    model=c["model"],
                    prompt=c["prompt"],
                    image_path=c["image_path"],
                    note=note_i,
                    group=c["group"],
                    batch_id=c["batch_id"],
                    batch_name=(c.get("batch_name", "") or "").strip(),
                )
                created_tasks.append(t_new)
                created_count += 1

            if not allow_dups:
                hist_sig.add(sig)

        self._ui_call(self.refresh_table)
        reuse_tag = " | 追加到现有大组" if reused_existing_batch else ""
        self.log_line(f"扫描完成：已创建={created_count}，跳过重复={skipped_dup} | 大组={batch_name}{reuse_tag}\n")
        self._record_history_batch(batch_id=batch_id, source="扫描批量添加", tasks=created_tasks)
        if self.auto_collapse_batch_var.get():
            self._collapsed_group_keys_by_mode.setdefault("按大组", set()).add(batch_id)
        if self.auto_collapse_batch_var.get():
            self._ui_call(self.refresh_table)
        self.batch_name_var.set("")
        if self.pause_new_tasks_var.get():
            self.log_line("当前处于暂停状态：请加载密钥并点击“开始执行”以运行排队任务。\n")
        else:
            self.log_line("队列会自动启动，无需额外点击其他开始按钮。\n")

    def add_task(self):
        if (not self.pause_new_tasks_var.get()) and (not (self.api_key_var.get() or "").strip()):
            if not self._ensure_runtime_api_key("立即运行新任务"):
                return

        prompt = (self.prompt_var.get() or "").strip()
        img = norm_path((self.image_path_var.get() or "").strip())

        if not prompt or not img or not Path(img).exists():
            messagebox.showwarning("输入错误", "请输入有效提示词和存在的图片路径。")
            return

        provider = (self.provider_var.get() or "").strip()
        base_url = (self.base_url_var.get() or "").strip()
        model = (self.model_var.get() or "").strip()
        note = (self.note_var.get() or "").strip()
        group = (self.group_var.get() or "").strip() or "手动"

        repeat = _safe_int(self.repeat_add_var.get(), 1)
        if repeat < 1:
            repeat = 1
        if self.safe_mode_var.get() and repeat > SAFE_MODE_MAX_REPEAT:
            self.log_line(f"安全模式：重复次数从 {repeat} 限制为 {SAFE_MODE_MAX_REPEAT}")
            repeat = SAFE_MODE_MAX_REPEAT

        if not self._safe_mode_precheck(repeat, "手动添加任务"):
            self.log_line("手动添加已取消：触发安全模式保护。")
            return
        if not self._confirm_budget(repeat, "手动添加任务"):
            self.log_line("手动添加已取消：未通过预算确认。")
            return

        batch_id, batch_name, reused_existing_batch = self._resolve_target_batch(group or "手动批次")

        created = 0
        created_tasks: list[TaskItem] = []
        for i in range(repeat):
            note_i = note
            if repeat > 1:
                note_i = f"{note} #{i + 1}/{repeat}".strip()

            t_new = self._create_task_and_add(
                provider=provider,
                base_url=base_url,
                model=model,
                prompt=prompt,
                image_path=img,
                note=note_i,
                group=group,
                batch_id=batch_id,
                batch_name=batch_name,
            )
            created_tasks.append(t_new)
            created += 1

        reuse_tag = " | 追加到现有大组" if reused_existing_batch else ""
        self.log_line(f"已添加任务={created} | 大组={batch_name} | 批次={batch_id}{reuse_tag} | {provider} | {model} | 分组={group}\n")
        self._record_history_batch(batch_id=batch_id, source="手动添加", tasks=created_tasks)
        if self.auto_collapse_batch_var.get():
            self._collapsed_group_keys_by_mode.setdefault("按大组", set()).add(batch_id)
            self._ui_call(self.refresh_table)
        self.batch_name_var.set("")
        for tid in self.idx.tasks_by_batch.get(batch_id, set()):
            self.mark_dirty(tid)

    # ---------- delete/stop/retry/open ----------
    def delete_selected(self):
        if not self._require_admin("删除任务"):
            return
        tids = self.selected_task_ids()
        if not tids:
            return

        selected_tasks = [self.tasks.get(tid) for tid in tids if self.tasks.get(tid)]
        linked_tasks = [t for t in selected_tasks if (getattr(t, "video_url", "") or "").strip()]
        pending_download_tasks = [
            t for t in linked_tasks
            if (getattr(t, "status", "") or "").strip() != "Downloaded"
        ]

        if linked_tasks:
            if not messagebox.askyesno(
                "确认删除带视频链接的任务",
                f"选中的任务里有 {len(linked_tasks)} 个已经拿到视频链接。\n"
                "删除后这些任务会从当前列表移除，只能通过历史任务窗口回查。\n\n"
                "确认继续删除吗？"
            ):
                return

        if pending_download_tasks:
            if not messagebox.askyesno(
                "二次确认删除未下载任务",
                f"其中有 {len(pending_download_tasks)} 个任务虽然已有视频链接，但状态还不是“已下载”。\n"
                "如果现在删除，后续只能从历史任务里再找回并下载。\n\n"
                "仍然确认删除吗？"
            ):
                return

        if not messagebox.askyesno("确认删除", f"确认删除选中的 {len(tids)} 个任务吗？"):
            return

        self._mark_history_deleted_for_task_ids(tids)
        for tid in tids:
            t = self.tasks.get(tid)
            if t:
                try:
                    if getattr(t, "stop_event", None):
                        t.stop_event.set()
                except Exception:
                    pass
                # Remove from index before removing from tasks dict
                self.idx.remove_task(t)
                self.tasks.pop(tid, None)

        for tid in tids:
            self._remove_task_row(tid)
        self._mark_tasks_dirty()
        self._save_now()
        self.log_line(f"已删除任务：{len(tids)}\n")

    def open_selected_url(self):
        tids = self.selected_task_ids()
        if not tids:
            return
        url = ""
        for tid in tids:
            t = self.tasks.get(tid)
            if not t:
                continue
            url = (getattr(t, "video_url", "") or "").strip()
            if url:
                break
        if url:
            webbrowser.open(url)
        else:
            messagebox.showinfo("无链接", "所选大组当前还没有可用的视频链接。")

    def stop_selected(self):
        if not self._require_admin("停止任务"):
            return
        tids = self.selected_task_ids()
        for tid in tids:
            t = self.tasks.get(tid)
            if not t:
                continue
            if getattr(t, "stop_event", None):
                t.stop_event.set()
            if t.status in ("Running", "Pending(Check)"):
                self._set_task_status(t, "Stopped")
                self.billing.append(
                    {"task_id": t.task_id, "event": EV_MANUAL_CANCEL, "amount": 0, "provider": t.provider})
                self._append_task_log(t, "\n手动停止\n")
                self._ui_update_task_row(t)
        for tid in tids:
            self.mark_dirty(tid)

    def retry_selected(self):
        if not self._require_admin("手动重试任务"):
            return
        tids = self.selected_task_ids()
        retried_any = False
        first_retried_tid: str | None = None
        for tid in tids:
            t = self.tasks.get(tid)
            if not t:
                continue
            if t.status in ("Failed", "Done(No URL)", "Stopped", "Failed(Terminal)", "Queued"):
                # manual retry should fully unfreeze terminal-failed tasks
                t.poll_terminal_fail = False
                self.idx.terminal_failed.discard(t.task_id)
                t.replacement_spawned = False
                self._set_video_url(t, "")
                self._set_remote_id(t, "")
                self._set_task_status(t, "Queued")
                t.progress = 0.0
                t.next_retry_at = None
                t.status_msg = ""
                t.last_error = ""
                self.idx.cancel_retry(t.task_id)
                self.billing.append(
                    {"task_id": t.task_id, "event": EV_RETRY_STARTED, "amount": 0, "provider": t.provider})
                self._append_task_log(t, "\n手动重新入队\n")
                retried_any = True
                if first_retried_tid is None:
                    first_retried_tid = t.task_id
        for tid in tids:
            self.mark_dirty(tid)
        if retried_any:
            # Give manually retried task execution priority and trigger create scheduling immediately.
            if first_retried_tid:
                self._create_blocked_tid = first_retried_tid
                self._create_retry_after_ts = 0.0
            self._ui_call(self._tick_autorun_queue)

    # ---------- start queued ----------
    def start_all_queued(self):
        if self.pause_new_tasks_var.get():
            self.log_line("已暂停：不会启动新的排队任务。\n")
            return
        if self.gate.queue_frozen:
            self.log_line("队列闸门已触发，暂不启动新任务。\n")
            return

        queued = [self.tasks[tid] for tid in self.idx.create_needed if tid in self.tasks]
        if not queued:
            self.log_line("没有需要创建远端ID的排队任务。\n")
            return
        if not self._ensure_runtime_api_key("开始排队任务"):
            return

        # 统一交由 autorun tick 调度 create_only，确保失败阻断与优先重试规则生效。
        self.log_line(f"已加入 create_only 调度队列：{len(queued)} 个任务\n")
        self._ui_call(self._tick_autorun_queue)

    # ---------- normalize paths ----------
    def _normalize_all_paths(self):
        self.mission_root_var.set(norm_path(self.mission_root_var.get()))
        self.download_root_var.set(norm_path(self.download_root_var.get()))
        self.image_path_var.set(norm_path(self.image_path_var.get()))
        for t in self.tasks.values():
            try:
                t.image_path = norm_path(getattr(t, "image_path", "") or "")
            except Exception:
                pass
            try:
                t.log_file = norm_path(getattr(t, "log_file", "") or "")
            except Exception:
                pass
            try:
                t.base_url = _norm_maybe_url(getattr(t, "base_url", "") or "")
            except Exception:
                pass

    # ---------- task log + ui update ----------
    def _append_task_log(self, t: TaskItem, msg: str):
        try:
            self._task_log_write_q.put_nowait((t.log_file, msg))
        except Exception:
            pass
        self._enqueue_ui(self.log_line, f"[{t.task_id}] {msg}")

    def _ui_update_task_row(self, t: TaskItem):
        # any thread can mark dirty; actual update is batched in flush
        self._enqueue_ui(self.mark_dirty, t.task_id)

    # ---------- remote_id polling ----------
    def poll_selected_remote_id(self):
        tids = self.selected_task_ids()
        if not tids:
            return
        if not self._ensure_runtime_api_key("刷新任务状态"):
            return

        valid_tids = []
        for tid in tids:
            t = self.tasks.get(tid)
            if not t:
                continue
            if not (getattr(t, "remote_id", "") or "").strip():
                continue
            valid_tids.append(tid)
        self._enqueue_poll_task_ids(valid_tids)
        self._drain_poll_queue()
        count = len(valid_tids)
        self.log_line(f"已手动单次轮询：{count} 个任务\n")

    def _resume_remote_id_tasks_on_startup(self):
        resumed = 0
        for t in self.tasks.values():
            rid = (getattr(t, "remote_id", "") or "").strip()
            if not rid:
                continue
            if t.status in ("Running", "Pending(Check)", "Queued"):
                self._resume_task_by_remote_id(t)
                resumed += 1
        if resumed:
            self.log_line(f"启动时已恢复远端ID轮询：{resumed} 个任务\n")

    def _resume_task_by_remote_id(self, t: TaskItem):
        if self.pause_new_tasks_var.get() or self.gate.queue_frozen:
            return
        try:
            if getattr(t, "future", None) and t.future and not t.future.done():
                return
        except Exception:
            pass

        provider = (t.provider or "").strip()
        if provider == "auto":
            provider = "xintian" if "sora-2" in (t.model or "") else "apiyi"
        if not (getattr(t, "remote_id", "") or "").strip():
            return

        t.stop_event = threading.Event()
        self._set_task_status(t, "Pending(Check)")
        self._ui_update_task_row(t)
        self._append_task_log(t, f"\n恢复远端ID轮询(队列模式) | {now_str()}\n")
        self.idx.needs_poll.add(t.task_id)
        self._enqueue_poll_task_ids([t.task_id])
        self._drain_poll_queue()

    # ---------- core runner ----------
    # ---------------- core runner ----------------
    def _start_task(self, t: TaskItem):
        # Pause check
        if self.pause_new_tasks_var.get():
            return

        # Gate frozen check
        if self.gate.queue_frozen:
            return

        # Already running check
        if getattr(t, "future", None) is not None:
            try:
                if t.future and not t.future.done():
                    return
            except Exception:
                pass

        provider = (t.provider or "").strip() or "xintian"
        if provider == "auto":
            provider = "xintian" if ("sora-2" in (t.model or "")) else "apiyi"

        # Missing API key check
        api_key, resolved_base_url = self._resolve_task_route(t, provider)
        api_key = (api_key or "").strip()
        if not api_key:
            self._set_task_status(t, "Failed")
            t.last_error = "missing_api_key"
            t.status_msg = "Missing API Key"
            self._append_task_log(t, "\nMissing 接口密钥: load or enter key, then retry.\n")
            self._ensure_retry_scheduled(t)
            self._ui_update_task_row(t)
            return

        # Reset runtime fields
        t.stop_event = threading.Event()
        self._set_task_status(t, "Running")
        t.progress = 0.0
        t.last_error = ""
        t.status_msg = ""
        t.run_attempt = (getattr(t, "run_attempt", 0) or 0) + 1

        self._append_task_log(t, f"\nstart run | attempt={t.run_attempt} | {now_str()}\n")

        def on_text(s: str):
            self._append_task_log(t, s)
            p = parse_progress_percent(s)
            if p is not None:
                try:
                    t.progress = float(p)
                except Exception:
                    pass
                self._ui_update_task_row(t)

        def on_remote_id(rid: str):
            rid = (rid or "").strip()
            if not rid:
                return
            self._set_remote_id(t, rid)
            try:
                self.billing.charge_remote_id_once(t, provider, t.model, rid)
            except Exception:
                pass

            self._ui_update_task_row(t)
            self._maybe_release_batch_gate_on_remote_id(t)

        def on_done(video_url: str | None):
            if (video_url or "").strip():
                self._set_video_url(t, (video_url or "").strip())
                self._set_task_status(t, "Success")
                t.progress = 100.0
                try:
                    self.billing.mark_actual_cost_once(t, provider, t.model, (t.remote_id or ""), t.video_url)
                except Exception:
                    pass
            else:
                self._set_task_status(t, "Done(No URL)")
                t.progress = float(getattr(t, "progress", 0.0) or 0.0)
                self._ensure_retry_scheduled(t)

            self._ui_update_task_row(t)

        def on_error(err: str):
            err = (err or "").strip()
            t.last_error = err
            self._set_task_status(t, "Failed")
            t.status_msg = compact_status_msg(err) or err or "Failed"
            self._append_task_log(t, f"\nERROR(run): {err}\n")
            if self.auto_retry_enabled_var.get() and getattr(self, "two_phase_mode", False) and not (getattr(t, "remote_id", "") or "").strip():
                self._create_blocked_tid = t.task_id
                self._create_retry_after_ts = time.time() + 2.0

            # Refund only if remote_id exists
            if (t.remote_id or "").strip():
                try:
                    self.billing.refund_failed_once(t, provider, t.model, t.remote_id, "api_error")
                except Exception:
                    pass
            if (t.remote_id or "").strip() and is_poll_terminal_failure(err):
                self._mark_terminal_failure(t)
                self._set_task_status(t, "Failed(Terminal)")
                self._maybe_enqueue_replacement(t, err)
            else:
                self._ensure_retry_scheduled(t)

            self._ui_update_task_row(t)

        def run():
            try:
                if provider == "xintian":
                    cfg = XintianConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                    )

                    if getattr(self, "two_phase_mode", True):
                        # Two-phase mode: only create remote_id, no polling
                        from .providers.xintian import run_xintian_create_only

                        def _on_rid(rid: str):
                            rid = (rid or "").strip()
                            if not rid:
                                return
                            self._set_remote_id(t, rid)
                            self._set_task_status(t, "Pending(Check)")
                            if self._create_blocked_tid == t.task_id:
                                self._create_blocked_tid = None
                            t.progress = 0.0
                            try:
                                self.billing.charge_remote_id_once(t, provider, t.model, rid)
                            except Exception:
                                pass
                            self._ui_update_task_row(t)
                            self._maybe_release_batch_gate_on_remote_id(t)

                        run_xintian_create_only(cfg, on_text, _on_rid, on_error, t.stop_event)

                    else:
                        # create + 
                        run_xintian_upload_and_poll(cfg, on_text, on_remote_id, on_done, on_error, t.stop_event)


                elif provider == "lingke":
                    cfg = LingkeConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        preset=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                        verify_ssl=True,
                        poll_interval_sec=3,
                    )
                    run_lingke_create_and_poll(cfg, on_text, on_remote_id, on_done, on_error, t.stop_event)

                elif provider == "toapis":
                    cfg = ToapisConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        preset=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                        poll_interval_sec=2,
                        verify_ssl=True,
                        n=1,
                        watermark=False,
                        private=True,
                    )
                    run_toapis_create_and_poll(cfg, on_text, on_remote_id, on_done, on_error, t.stop_event)

                elif provider == "baoyouhuyu":
                    cfg = BaoyouhuyuConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                        seconds="8",
                        poll_interval_sec=3,
                        verify_ssl=True,
                    )
                    if getattr(self, "two_phase_mode", True):
                        def _on_rid(rid: str):
                            rid = (rid or "").strip()
                            if not rid:
                                return
                            self._set_remote_id(t, rid)
                            self._set_task_status(t, "Pending(Check)")
                            if self._create_blocked_tid == t.task_id:
                                self._create_blocked_tid = None
                            t.progress = 0.0
                            try:
                                self.billing.charge_remote_id_once(t, provider, t.model, rid)
                            except Exception:
                                pass
                            self._ui_update_task_row(t)
                            self._maybe_release_batch_gate_on_remote_id(t)

                        run_baoyouhuyu_create_only(cfg, on_text, _on_rid, on_error, t.stop_event)
                    else:
                        run_baoyouhuyu_create_and_poll(
                            cfg, on_text, on_remote_id, on_done, on_error, t.stop_event
                        )
                elif provider == "jimmy":
                    cfg = JimmyConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                    )
                    if getattr(self, "two_phase_mode", True):
                        def _on_rid(rid: str):
                            rid = (rid or "").strip()
                            if not rid:
                                return
                            self._set_remote_id(t, rid)
                            self._set_task_status(t, "Pending(Check)")
                            if self._create_blocked_tid == t.task_id:
                                self._create_blocked_tid = None
                            t.progress = 0.0
                            try:
                                self.billing.charge_remote_id_once(t, provider, t.model, rid)
                            except Exception:
                                pass
                            self._ui_update_task_row(t)
                            self._maybe_release_batch_gate_on_remote_id(t)

                        run_jimmy_create_only(cfg, on_text, _on_rid, on_error, t.stop_event)
                    else:
                        run_jimmy_create_and_poll(cfg, on_text, on_remote_id, on_done, on_error, t.stop_event)
                elif provider == "dyuapi":
                    cfg = DyuapiConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                    )
                    if getattr(self, "two_phase_mode", True):
                        def _on_rid(rid: str):
                            rid = (rid or "").strip()
                            if not rid:
                                return
                            self._set_remote_id(t, rid)
                            self._set_task_status(t, "Pending(Check)")
                            if self._create_blocked_tid == t.task_id:
                                self._create_blocked_tid = None
                            t.progress = 0.0
                            try:
                                self.billing.charge_remote_id_once(t, provider, t.model, rid)
                            except Exception:
                                pass
                            self._ui_update_task_row(t)
                            self._maybe_release_batch_gate_on_remote_id(t)

                        run_dyuapi_create_only(cfg, on_text, _on_rid, on_error, t.stop_event)
                    else:
                        run_dyuapi_create_and_poll(cfg, on_text, on_remote_id, on_done, on_error, t.stop_event)

                else:
                    cfg = ApiyiConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                    )
                    run_apiyi_sse(cfg, on_text, on_done, on_error, t.stop_event)

            except Exception as e:
                on_error(str(e))

        t.future = self.executor.submit(run)
        self._ui_update_task_row(t)

    def _start_task_create_only(self, t: TaskItem):
        if self.pause_new_tasks_var.get() or self.gate.queue_frozen:
            return False
        try:
            if getattr(t, "future", None) and t.future and (not t.future.done()):
                return False
        except Exception:
            pass
        if not self._try_acquire_active_slot("create", t.task_id, self._create_submit_limit()):
            return False

        provider = (t.provider or "").strip() or "xintian"
        if provider == "auto":
            provider = "xintian" if ("sora-2" in (t.model or "")) else "apiyi"
        api_key, resolved_base_url = self._resolve_task_route(t, provider)
        api_key = (api_key or "").strip()
        if not api_key:
            self._set_task_status(t, "Failed")
            t.last_error = "missing_api_key"
            t.status_msg = "Missing API Key"
            self._ensure_retry_scheduled(t)
            self._ui_update_task_row(t)
            self._release_active_slot("create", t.task_id)
            return False

        t.stop_event = threading.Event()
        self._set_task_status(t, "Running")
        t.progress = 0.0
        t.status_msg = ""
        t.last_error = ""

        def on_text(s: str):
            self._append_task_log(t, s)

        def on_remote_id(rid: str):
            rid = (rid or "").strip()
            if not rid:
                return
            self._set_remote_id(t, rid)
            self._set_task_status(t, "Pending(Check)")
            if self._create_blocked_tid == t.task_id:
                self._create_blocked_tid = None
            self._ui_update_task_row(t)
            self._append_task_log(t, f"\nremote_id acquired: {rid}\n")

        def on_error(err: str):
            self._set_task_status(t, "Failed")
            t.last_error = (err or "").strip()
            t.status_msg = compact_status_msg(t.last_error) or t.last_error or "创建失败"
            self._append_task_log(t, f"\nERROR(create_only): {t.last_error}\n")
            # create phase hard-stop: if this task has no remote_id, block launching new tasks
            # and retry this one first.
            if self.auto_retry_enabled_var.get() and not (getattr(t, "remote_id", "") or "").strip():
                self._create_blocked_tid = t.task_id
                self._create_retry_after_ts = time.time() + 2.0
            if is_terminal_failure(t.last_error):
                self._mark_terminal_failure(t)
                self._set_task_status(t, "Failed(Terminal)")
                self._maybe_enqueue_replacement(t, t.last_error)
            else:
                self._ensure_retry_scheduled(t)
            self._ui_update_task_row(t)

        def run():
            try:
                if provider == "xintian":
                    cfg = XintianConfig(api_key=api_key, base_url=resolved_base_url, model=t.model, prompt=t.prompt,
                                        image_path=t.image_path)
                    run_xintian_create_only(cfg, on_text, on_remote_id, on_error, t.stop_event)
                elif provider == "baoyouhuyu":
                    cfg = BaoyouhuyuConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                        seconds="8",
                    )
                    run_baoyouhuyu_create_only(cfg, on_text, on_remote_id, on_error, t.stop_event)
                elif provider == "jimmy":
                    cfg = JimmyConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                    )
                    run_jimmy_create_only(cfg, on_text, on_remote_id, on_error, t.stop_event)
                elif provider == "dyuapi":
                    cfg = DyuapiConfig(
                        api_key=api_key,
                        base_url=resolved_base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                    )
                    run_dyuapi_create_only(cfg, on_text, on_remote_id, on_error, t.stop_event)
                else:
                    on_error("provider does not support create_only")
            except Exception as e:
                on_error(str(e))
            finally:
                self._release_active_slot("create", t.task_id)

        t.future = self.create_executor.submit(run)
        self._ui_update_task_row(t)
        return True

    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        try:
            self._save_user_prefs()
        except Exception:
            pass
        self.pause_new_tasks_var.set(True)
        self.gate.queue_frozen = True

        for t in list(self.tasks.values()):
            try:
                if getattr(t, "stop_event", None) is None:
                    t.stop_event = threading.Event()
                t.stop_event.set()
            except Exception:
                pass

        try:
            self._task_log_writer_stop.set()
        except Exception:
            pass
        try:
            if self._poll_timer_id is not None:
                self.root.after_cancel(self._poll_timer_id)
                self._poll_timer_id = None
        except Exception:
            pass
        try:
            if self._tasks_save_timer_id is not None:
                self.root.after_cancel(self._tasks_save_timer_id)
                self._tasks_save_timer_id = None
        except Exception:
            pass
        try:
            self._save_now()
        except Exception:
            pass
        try:
            save_task_history(self.task_history)
        except Exception:
            pass

        try:
            self.executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            self.save_executor.shutdown(wait=False, cancel_futures=False)
        except Exception:
            pass
        try:
            self.create_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            self.poll_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

        try:
            self.root.destroy()
        except Exception:
            pass


def run_app():
    root = tk.Tk()
    App(root)
    root.mainloop()







