# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import os
import json
import threading
import datetime
import webbrowser
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import requests  # noqa: F401
except Exception as e:
    raise RuntimeError("需要安装 requests：pip install requests") from e

from .settings import (
    APP_TITLE, LOG_DIR, TASKS_STORE_FILE, TASKS_STORE_MAX,
    APIYI_DEFAULT_BASE, APIYI_MODELS,
    XINTIAN_DEFAULT_BASE, XINTIAN_MODELS,
    LINGKE_DEFAULT_BASE, LINGKE_MODELS,
    TOAPIS_DEFAULT_BASE, TOAPIS_MODELS,
    PROVIDERS,
    MISSION_ROOT_DEFAULT, DOWNLOAD_ROOT_DEFAULT,
    AUTO_RETRY_ENABLED, AUTO_RETRY_TICK_SEC, AUTO_RETRY_MAX_RETRIES,
    AUTO_RETRY_BASE_DELAY_SEC, AUTO_RETRY_MAX_DELAY_SEC,
    AUTO_RETRY_INCLUDE_DONE_NO_URL,
    EV_MANUAL_CANCEL, EV_RETRY_STARTED,
    DOWNLOAD_INDEX_FILE,
)

from .utils import (
    now_str, norm_path,
    parse_progress_percent,
)

from .models import TaskItem
from .persistence import load_tasks_store, save_tasks_store, export_tasks_to, import_tasks_from
from .billing import BillingManager
from .gate import GateController
from .downloader import run_batch_download

from .providers.apiyi import run_apiyi_sse, ApiyiConfig
from .providers.xintian import (
    run_xintian_upload_and_poll,
    run_xintian_poll_existing,
    XintianConfig,
)
from .providers.lingke import run_lingke_create_and_poll, LingkeConfig
from .providers.toapis import run_toapis_create_and_poll, ToapisConfig

# Crypto optional
CRYPTO_OK = True
try:
    from .crypto_store import load_encrypted_config, save_encrypted_config
except Exception:
    CRYPTO_OK = False
    load_encrypted_config = None
    save_encrypted_config = None

PLAIN_CONFIG_FILE = "config.json"


def compact_status_msg(err: str) -> str:
    """
    ✅ 将错误压缩为：HTTP xxx + 核心原因（去掉 request id 等噪音）
    目标示例：HTTP 401 无效的令牌
    """
    s = (err or "").strip()
    if not s:
        return ""

    # 去掉 request id 等括号尾巴
    s = re.sub(r"\(request id:.*?\)", "", s, flags=re.I).strip()

    # 抽取 HTTP code
    m = re.search(r"HTTP\s*(\d{3})", s, flags=re.I)
    code = m.group(1) if m else ""

    # 抽取常见原因
    reason = ""
    m2 = re.search(r"(无效的令牌|令牌无效|Unauthorized|Invalid token|invalid_token)", s, flags=re.I)
    if m2:
        reason = m2.group(1)

    # 如果没有命中 reason，就尽量拿 “Create failed:” 后面的第一段
    if not reason:
        m3 = re.search(r"Create failed:\s*([^\n\r]+)", s, flags=re.I)
        if m3:
            reason = m3.group(1).strip()

    out = ""
    if code:
        out += f"HTTP {code}"
    if reason:
        out += f" {reason}"
    if out.strip():
        return out.strip()

    # 兜底：第一行
    return s.splitlines()[0][:160].strip()


def _safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def _norm_maybe_url(p: str) -> str:
    """URL 不动，普通路径做 norm_path"""
    if not isinstance(p, str):
        return ""
    s = p.strip()
    if s.lower().startswith(("http://", "https://")):
        return s
    return norm_path(s)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1380x820")

        Path(LOG_DIR).mkdir(exist_ok=True, parents=True)

        # state
        self.executor = ThreadPoolExecutor(max_workers=6)
        self.tasks: dict[str, TaskItem] = {}
        self.task_counter = 1
        self.current_config = {}
        self.billing = BillingManager()

        # UI vars
        self.passphrase_var = tk.StringVar(value="")
        self.key_loaded_var = tk.BooleanVar(value=False)
        self.pause_new_tasks_var = tk.BooleanVar(value=False)

        self.provider_var = tk.StringVar(value="xintian")
        self.base_url_var = tk.StringVar(value=XINTIAN_DEFAULT_BASE)
        self.model_var = tk.StringVar(value=XINTIAN_MODELS[0])
        self.api_key_var = tk.StringVar(value="")

        self.mission_root_var = tk.StringVar(value=str(MISSION_ROOT_DEFAULT))
        self.download_root_var = tk.StringVar(value=str(DOWNLOAD_ROOT_DEFAULT))

        self.prompt_var = tk.StringVar(value="")
        self.image_path_var = tk.StringVar(value="")

        self.note_var = tk.StringVar(value="")
        self.group_var = tk.StringVar(value="")
        self.mission_n_each_var = tk.IntVar(value=0)  # 0=全部（不限）
        self.repeat_add_var = tk.IntVar(value=1)

        # filters
        self.filter_status_var = tk.StringVar(value="ALL")
        self.filter_provider_var = tk.StringVar(value="ALL")
        self.search_var = tk.StringVar(value="")

        # auto retry
        self.auto_retry_enabled_var = tk.BooleanVar(value=AUTO_RETRY_ENABLED)
        self.auto_retry_include_done_no_url_var = tk.BooleanVar(value=AUTO_RETRY_INCLUDE_DONE_NO_URL)

        # gate
        self.gate_enabled_var = tk.BooleanVar(value=False)
        self.gate_remote_limit_var = tk.StringVar(value="0")
        self.gate_link_limit_var = tk.StringVar(value="0")
        self.gate = GateController(self.gate_enabled_var, self.gate_remote_limit_var, self.gate_link_limit_var)

        # download options
        self.dl_workers_var = tk.StringVar(value="3")
        self.dl_retries_var = tk.StringVar(value="2")
        self.dl_skip_dup_var = tk.BooleanVar(value=True)
        self.download_folder_var = tk.StringVar(value="")

        # API Key 明文显示一次 marker
        self._key_marker_file = Path(".key_saved.marker")

        # ✅ BatchGate state
        self.batch_counter = 1
        self.active_batch_id: str | None = None

        # ✅ BatchGate 去重记忆：必须在 __init__ 初始化，绝对不能在运行中重置
        self._bg_last_active_batch_logged: str | None = None
        self._bg_last_release_logged: str | None = None
        self._bg_last_started_logged: tuple[str, int] | None = None

        self._build_ui()
        self._bind_events()

        # restore tasks
        self._restore_tasks()

        # startup resume remote_id tasks
        self.root.after(800, self._resume_remote_id_tasks_on_startup)

        # timers
        self.root.after(1200, self._tick_autosave)
        self.root.after(1200, self._tick_auto_retry)
        self.root.after(1500, self._tick_gate)

        # ✅ autorun queue
        self.root.after(1200, self._tick_autorun_queue)

    # ---------- helpers ----------
    def _new_batch_id(self) -> str:
        bid = f"B{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{self.batch_counter:03d}"
        self.batch_counter += 1
        return bid

    def _task_batch_id(self, t: TaskItem) -> str:
        return (getattr(t, "batch_id", None) or "LEGACY")

    def _sanitize_folder_name(self, name: str) -> str:
        s = (name or "").strip()
        if not s:
            return ""
        for ch in r'\/:*?"<>|':
            s = s.replace(ch, "_")
        return s.strip(" .")

    def _get_prompt_text(self) -> str:
        try:
            return self.prompt_text.get("1.0", "end-1c").strip()
        except Exception:
            return (self.prompt_var.get() or "").strip()

    def _set_prompt_text(self, text: str):
        value = text or ""
        self.prompt_var.set(value)
        try:
            self.prompt_text.delete("1.0", "end")
            self.prompt_text.insert("1.0", value)
        except Exception:
            pass

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=8)

        ttk.Label(top, text="Passphrase（用于解密/保存Key）:").pack(side="left")
        ttk.Entry(top, textvariable=self.passphrase_var, width=22, show="•").pack(side="left", padx=6)
        ttk.Button(top, text="加载Key", command=self.load_keys).pack(side="left")
        ttk.Button(top, text="保存Key", command=self.save_keys).pack(side="left", padx=6)
        self.key_state = ttk.Label(top, text="Key 未加载", foreground="red")
        self.key_state.pack(side="left", padx=8)

        ttk.Separator(self.root).pack(fill="x", padx=10, pady=6)

        cfg = ttk.Frame(self.root)
        cfg.pack(fill="x", padx=10)

        ttk.Label(cfg, text="Provider:").grid(row=0, column=0, sticky="w")
        self.provider_cb = ttk.Combobox(
            cfg, textvariable=self.provider_var,
            values=[p[1] for p in PROVIDERS], width=14, state="readonly"
        )
        self.provider_cb.grid(row=0, column=1, padx=6, sticky="w")

        ttk.Label(cfg, text="Base URL:").grid(row=0, column=2, sticky="w")
        ttk.Entry(cfg, textvariable=self.base_url_var, width=36).grid(row=0, column=3, padx=6, sticky="w")

        ttk.Label(cfg, text="Model:").grid(row=0, column=4, sticky="w")
        self.model_cb = ttk.Combobox(cfg, textvariable=self.model_var, values=XINTIAN_MODELS, width=26, state="readonly")
        self.model_cb.grid(row=0, column=5, padx=6, sticky="w")

        ttk.Label(cfg, text="API Key:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.api_key_entry = ttk.Entry(cfg, textvariable=self.api_key_var, width=55, show="•")
        self.api_key_entry.grid(row=1, column=1, columnspan=3, padx=6, sticky="w", pady=(6, 0))

        ttk.Label(cfg, text="Mission Root:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(cfg, textvariable=self.mission_root_var, width=55).grid(
            row=2, column=1, columnspan=3, padx=6, sticky="w", pady=(6, 0)
        )
        ttk.Button(cfg, text="选择", command=self.pick_mission_root).grid(row=2, column=4, sticky="w", pady=(6, 0))

        ttk.Label(cfg, text="Download Root:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(cfg, textvariable=self.download_root_var, width=55).grid(
            row=3, column=1, columnspan=3, padx=6, sticky="w", pady=(6, 0)
        )
        ttk.Button(cfg, text="选择", command=self.pick_download_root).grid(row=3, column=4, sticky="w", pady=(6, 0))

        io = ttk.Frame(self.root)
        io.pack(fill="x", padx=10, pady=8)

        ttk.Label(io, text="Prompt:").grid(row=0, column=0, sticky="w")
        self.prompt_text = tk.Text(io, height=4, width=96, wrap="word")
        self.prompt_text.grid(row=0, column=1, rowspan=2, padx=6, sticky="we")
        ttk.Button(io, text="读取最新(不入队)", command=self.load_from_mission_latest).grid(row=0, column=2, padx=6, sticky="n")
        ttk.Button(io, text="扫描Mission批量入队", command=self.import_mission_all).grid(row=0, column=3, padx=6, sticky="n")
        ttk.Label(io, text="每子目录最多:").grid(row=0, column=4, sticky="ne")
        ttk.Spinbox(io, from_=0, to=9999, width=6, textvariable=self.mission_n_each_var).grid(
            row=0, column=5, sticky="nw", padx=(2, 6)
        )
        ttk.Label(io, text="(0=不限)").grid(row=0, column=6, sticky="nw")

        io.grid_columnconfigure(1, weight=1)

        ttk.Label(io, text="Image:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(io, textvariable=self.image_path_var, width=110).grid(
            row=2, column=1, padx=6, sticky="we", pady=(6, 0)
        )
        ttk.Button(io, text="选择图片", command=self.pick_image).grid(row=2, column=2, padx=6, pady=(6, 0), sticky="w")

        ttk.Label(io, text="Note:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(io, textvariable=self.note_var, width=44).grid(row=3, column=1, padx=6, sticky="w", pady=(6, 0))
        ttk.Label(io, text="Group:").grid(row=3, column=2, sticky="e", pady=(6, 0))
        ttk.Entry(io, textvariable=self.group_var, width=26).grid(row=3, column=3, padx=6, sticky="w", pady=(6, 0))
        ttk.Label(io, text="Repeat:").grid(row=3, column=4, sticky="e", pady=(6, 0))
        ttk.Spinbox(io, from_=1, to=999, width=6, textvariable=self.repeat_add_var).grid(
            row=3, column=5, sticky="w", padx=(2, 6), pady=(6, 0)
        )
        ttk.Label(io, text="(扫描/添加复制次数)").grid(row=3, column=6, sticky="w", pady=(6, 0))

        act = ttk.Frame(self.root)
        act.pack(fill="x", padx=10, pady=6)

        ttk.Button(act, text="➕ Add Task", command=self.add_task).pack(side="left")
        ttk.Button(act, text="▶ Start Queued", command=self.start_all_queued).pack(side="left", padx=6)
        ttk.Button(act, text="⏸ Pause New", command=self.pause_new_tasks).pack(side="left", padx=6)
        ttk.Button(act, text="▶ Resume New", command=self.resume_new_tasks).pack(side="left", padx=6)
        ttk.Button(act, text="⛔ Stop Selected", command=self.stop_selected).pack(side="left", padx=6)
        ttk.Button(act, text="♻ Retry Selected", command=self.retry_selected).pack(side="left", padx=6)
        ttk.Button(act, text="🔎 Poll RemoteID", command=self.poll_selected_remote_id).pack(side="left", padx=6)
        ttk.Button(act, text="🗑 Delete Selected", command=self.delete_selected).pack(side="left", padx=6)
        ttk.Button(act, text="📤 Export tasks.json", command=self.export_tasks_ui).pack(side="left", padx=16)
        ttk.Button(act, text="📥 Import tasks.json", command=self.import_tasks_ui).pack(side="left", padx=6)

        # download dropdown
        self.download_mode_var = tk.StringVar(value="selected")
        ttk.Label(act, text="Batch Download:").pack(side="left", padx=(16, 6))
        self.dl_combo = ttk.Combobox(
            act, textvariable=self.download_mode_var, state="readonly", width=26,
            values=["selected", "filtered_success", "filtered_success_or_done_no_url"]
        )
        self.dl_combo.pack(side="left")
        ttk.Button(act, text="⬇ Download", command=self.batch_download_ui).pack(side="left", padx=6)
        ttk.Label(act, text="归档目录:").pack(side="left", padx=(12, 4))
        ttk.Entry(act, textvariable=self.download_folder_var, width=18).pack(side="left")

        filt = ttk.Frame(self.root)
        filt.pack(fill="x", padx=10, pady=4)

        ttk.Label(filt, text="Filter Status:").pack(side="left")
        self.status_cb = ttk.Combobox(
            filt, textvariable=self.filter_status_var, state="readonly", width=16,
            values=["ALL", "Queued", "Running", "Pending(Check)", "Success", "Done(No URL)", "Failed", "Stopped"]
        )
        self.status_cb.pack(side="left", padx=6)

        ttk.Label(filt, text="Filter Provider:").pack(side="left")
        self.provider_filter_cb = ttk.Combobox(
            filt, textvariable=self.filter_provider_var, state="readonly", width=12,
            # ✅ 修复：补齐 provider
            values=["ALL", "auto", "apiyi", "xintian", "lingke", "toapis"]
        )
        self.provider_filter_cb.pack(side="left", padx=6)

        ttk.Label(filt, text="Search:").pack(side="left")
        ttk.Entry(filt, textvariable=self.search_var, width=34).pack(side="left", padx=6)
        ttk.Button(filt, text="Apply", command=self.refresh_table).pack(side="left")
        ttk.Button(filt, text="Clear", command=self.clear_filters).pack(side="left", padx=6)

        opt = ttk.Frame(self.root)
        opt.pack(fill="x", padx=10, pady=6)

        ttk.Checkbutton(opt, text="Auto Retry", variable=self.auto_retry_enabled_var).pack(side="left")
        ttk.Checkbutton(opt, text="Include Done(No URL)", variable=self.auto_retry_include_done_no_url_var).pack(
            side="left", padx=6
        )

        ttk.Separator(opt, orient="vertical").pack(side="left", fill="y", padx=10)

        ttk.Checkbutton(opt, text="Queue Gate", variable=self.gate_enabled_var).pack(side="left")
        ttk.Label(opt, text="RemoteID limit:").pack(side="left", padx=(8, 2))
        ttk.Entry(opt, textvariable=self.gate_remote_limit_var, width=6).pack(side="left")
        ttk.Label(opt, text="Link limit:").pack(side="left", padx=(8, 2))
        ttk.Entry(opt, textvariable=self.gate_link_limit_var, width=6).pack(side="left")

        ttk.Separator(self.root).pack(fill="x", padx=10, pady=6)

        mid = ttk.Frame(self.root)
        mid.pack(fill="both", expand=True, padx=10, pady=6)

        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)

        self._cols = ("task_id", "status", "provider", "model", "status_msg", "progress", "note", "group", "created_at", "remote_id", "video_url")
        self.tree = ttk.Treeview(left, columns=self._cols, show="tree headings", height=18)
        self.tree.heading("#0", text="组/任务")
        self.tree.column("#0", width=220, anchor="w")

        col_titles = {
            "task_id": "任务ID",
            "status": "状态",
            "provider": "平台",
            "model": "模型",
            "status_msg": "状态信息",
            "progress": "进度",
            "note": "备注",
            "group": "分组",
            "created_at": "创建时间",
            "remote_id": "远端ID",
            "video_url": "视频链接",
        }

        widths = {
            "task_id": 90,
            "status": 140,
            "provider": 80,
            "model": 180,
            "status_msg": 240,
            "progress": 90,
            "note": 260,
            "group": 160,
            "created_at": 150,
            "remote_id": 160,
            "video_url": 260,
        }

        for c in self._cols:
            self.tree.heading(c, text=col_titles.get(c, c))
            self.tree.column(c, width=widths.get(c, 120), anchor="w")

        ysb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        right = ttk.Frame(mid)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        self.log = tk.Text(right, height=18, wrap="word")
        self.log.pack(fill="both", expand=True)

        foot = ttk.Frame(self.root)
        self.pause_label = ttk.Label(foot, text="")
        self.pause_label.pack(side="left", padx=12)
        foot.pack(fill="x", padx=10, pady=6)
        self.bill_label = ttk.Label(foot, text="Billing: -")
        self.bill_label.pack(side="left")
        ttk.Button(foot, text="Open Selected URL", command=self.open_selected_url).pack(side="right")

    def _bind_events(self):
        self.provider_cb.bind("<<ComboboxSelected>>", lambda e: self._sync_provider_ui())
        self.status_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_table())
        self.provider_filter_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_table())
        self.search_var.trace_add("write", lambda *args: self._debounced_refresh())
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select_fill_form)

    def _debounced_refresh(self):
        if hasattr(self, "_refresh_timer"):
            self.root.after_cancel(self._refresh_timer)
        self._refresh_timer = self.root.after(250, self.refresh_table)

    # ---------- table row helpers ----------
    @staticmethod
    def _is_group_iid(iid: str) -> bool:
        return isinstance(iid, str) and iid.startswith("group_")

    def _row_values(self, t: TaskItem) -> tuple:
        """✅ 统一 Treeview values 顺序，避免错位"""
        progress = float(getattr(t, "progress", 0.0) or 0.0)
        return (
            t.task_id,
            self._display_status(t),
            (t.provider or ""),
            (t.model or ""),
            (getattr(t, "status_msg", "") or ""),
            f"{progress:.1f}%",
            (t.note or ""),
            (t.group or ""),
            (t.created_at or ""),
            (getattr(t, "remote_id", "") or ""),
            (getattr(t, "video_url", "") or ""),
        )

    # ---------- double click ----------
    def _on_tree_double_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if self._is_group_iid(item):
            cur = bool(self.tree.item(item, "open"))
            self.tree.item(item, open=(not cur))
            return
        try:
            vals = self.tree.item(item, "values")
            if not vals:
                return
            url = vals[-1]
            if isinstance(url, str) and url.strip():
                webbrowser.open(url.strip())
        except Exception:
            return

    # ---------- pause/resume ----------
    def pause_new_tasks(self):
        self.pause_new_tasks_var.set(True)
        self.log_line("\n⏸ 已暂停：不会再启动新的 Queued 任务（自动/手动/重试均不启动）\n")
        self.refresh_table()

    def resume_new_tasks(self):
        self.pause_new_tasks_var.set(False)
        self.log_line("\n▶ 已恢复：将启动所有未执行的 Queued 任务\n")
        self.refresh_table()
        self.start_all_queued()

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

        self.provider_var.set(t.provider if t.provider else "xintian")
        self.base_url_var.set(_norm_maybe_url(getattr(t, "base_url", "") or ""))
        self.model_var.set(getattr(t, "model", "") or "")
        self._set_prompt_text(getattr(t, "prompt", "") or "")
        self.image_path_var.set(norm_path(getattr(t, "image_path", "") or ""))
        self.note_var.set(getattr(t, "note", "") or "")
        self.group_var.set(getattr(t, "group", "") or "")
        self._sync_provider_ui()

    # ---------- logging ----------
    def log_line(self, s: str):
        try:
            self.log.insert("end", s)
            self.log.see("end")
        except Exception:
            pass

    # ---------- restore/autosave ----------
    def _restore_tasks(self):
        tasks, next_counter = load_tasks_store(self.log_line)
        self.tasks = tasks
        self.task_counter = next_counter
        self._normalize_all_paths()
        self.refresh_table()
        self.log_line(f"✅ 已恢复历史任务：{len(self.tasks)} 条（来自 {TASKS_STORE_FILE}）\n")

    def _tick_autosave(self):
        try:
            if self.pause_new_tasks_var.get():
                self.pause_label.configure(text="⏸ Paused: new tasks will NOT start", foreground="red")
            else:
                self.pause_label.configure(text="▶ Running: new tasks can start", foreground="green")
        except Exception:
            pass
        try:
            save_tasks_store(self.tasks)
        except Exception:
            pass
        self._update_billing_label()
        self.root.after(3500, self._tick_autosave)

    def _tick_gate(self):
        if self.gate.reached(self.tasks):
            if not self.gate.queue_frozen:
                self.gate.queue_frozen = True
                self.log_line("\n🧊 Queue Gate 已触发：将暂停启动新任务\n")
        else:
            self.gate.queue_frozen = False
        self.root.after(1200, self._tick_gate)

    # ---------- autorun queue ----------
    def _running_count(self) -> int:
        n = 0
        for t in self.tasks.values():
            if t.status in ("Running", "Pending(Check)"):
                n += 1
            else:
                try:
                    if getattr(t, "future", None) is not None and t.future and not t.future.done():
                        n += 1
                except Exception:
                    pass
        return n

    def _maybe_release_batch_gate_on_remote_id(self, t: TaskItem):
        try:
            if not self.active_batch_id:
                return
            if self._task_batch_id(t) != self.active_batch_id:
                return

            active_tasks = [x for x in self.tasks.values() if self._task_batch_id(x) == self.active_batch_id]
            if not active_tasks:
                return

            all_have_rid = all((x.remote_id or "").strip() for x in active_tasks)
            if all_have_rid:
                bid = self.active_batch_id
                self.active_batch_id = None
                # ✅ 不要重置 dedupe 变量
                self.log_line(f"✅ BatchGate: batch {bid} 已全员获取 RemoteID（即时放行）\n")
                self.root.after(100, self._tick_autorun_queue)
        except Exception:
            pass

    def _tick_autorun_queue(self):
        try:
            if self.pause_new_tasks_var.get() or self.gate.queue_frozen:
                self.root.after(1000, self._tick_autorun_queue)
                return

            # 1) choose active_batch
            if not self.active_batch_id:
                candidates = [
                    t for t in self.tasks.values()
                    if t.status in ("Queued", "Failed", "Done(No URL)", "Stopped")
                    and not (getattr(t, "remote_id", "") or "").strip()
                ]
                if candidates:
                    candidates.sort(key=lambda x: (x.created_at or "", x.task_id))
                    self.active_batch_id = self._task_batch_id(candidates[0])

                    if self.active_batch_id != self._bg_last_active_batch_logged:
                        self._bg_last_active_batch_logged = self.active_batch_id
                        self.log_line(f"🧱 BatchGate: active_batch = {self.active_batch_id}\n")

            if not self.active_batch_id:
                self.root.after(1000, self._tick_autorun_queue)
                return

            # 2) release if all have remote_id
            active_tasks = [t for t in self.tasks.values() if self._task_batch_id(t) == self.active_batch_id]
            if active_tasks:
                all_have_rid = all((getattr(t, "remote_id", "") or "").strip() for t in active_tasks)
                if all_have_rid:
                    bid = self.active_batch_id
                    if bid and bid != self._bg_last_release_logged:
                        self._bg_last_release_logged = bid
                        self.log_line(f"✅ BatchGate: batch {bid} 已全员获取 RemoteID，放行下一批\n")
                    self.active_batch_id = None
                    self.root.after(200, self._tick_autorun_queue)
                    return

            # 3) capacity
            max_workers = getattr(self.executor, "_max_workers", 6) or 6
            running = self._running_count()
            capacity = max(0, int(max_workers) - int(running))
            if capacity <= 0:
                self.root.after(1000, self._tick_autorun_queue)
                return

            q1 = [t for t in active_tasks if t.status == "Queued" and not (getattr(t, "remote_id", "") or "").strip()]
            q2 = [t for t in active_tasks if t.status == "Queued" and (getattr(t, "remote_id", "") or "").strip()]

            started = 0
            for t in (q1 + q2):
                if started >= capacity:
                    break
                self._start_task(t)
                started += 1

            if started:
                key = (self.active_batch_id or "", int(started))
                if key != self._bg_last_started_logged:
                    self._bg_last_started_logged = key
                    self.log_line(f"🤖 BatchGate: started={started} | active_batch={self.active_batch_id}\n")

        except Exception:
            pass

        self.root.after(1000, self._tick_autorun_queue)

    # ---------- auto retry ----------
    def _tick_auto_retry(self):
        if self.pause_new_tasks_var.get() or (not self.auto_retry_enabled_var.get()):
            self.root.after(max(1000, int(AUTO_RETRY_TICK_SEC * 1000)), self._tick_auto_retry)
            return

        include_done = bool(self.auto_retry_include_done_no_url_var.get())
        now = datetime.datetime.now()

        for t in self.tasks.values():
            if self.active_batch_id and self._task_batch_id(t) != self.active_batch_id:
                continue

            if not (t.status == "Failed" or (include_done and t.status == "Done(No URL)")):
                continue

            attempts = getattr(t, "attempts", 0) or 0
            max_retries = int(AUTO_RETRY_MAX_RETRIES) if AUTO_RETRY_MAX_RETRIES else 50
            if attempts >= max_retries:
                continue

            nra = getattr(t, "next_retry_at", None)
            if isinstance(nra, str) and nra.strip():
                try:
                    dt = datetime.datetime.strptime(nra.strip(), "%Y-%m-%d %H:%M:%S")
                    if now < dt:
                        continue
                except Exception:
                    pass

            has_remote_id = bool((getattr(t, "remote_id", "") or "").strip())

            if t.status == "Failed" and has_remote_id:
                delay = float(AUTO_RETRY_BASE_DELAY_SEC) * (2 ** max(0, attempts))
                delay = min(delay, float(AUTO_RETRY_MAX_DELAY_SEC))
                next_dt = now + datetime.timedelta(seconds=delay)
                setattr(t, "next_retry_at", next_dt.strftime("%Y-%m-%d %H:%M:%S"))
                setattr(t, "attempts", attempts + 1)
                self._append_task_log(t, f"\n🔄 自动轮询 RemoteID | attempts={attempts + 1} | next={t.next_retry_at}\n")
                self._resume_task_by_remote_id(t)
                continue

            # no remote_id -> requeue
            t.status = "Queued"
            t.progress = 0.0
            setattr(t, "attempts", attempts + 1)
            delay = float(AUTO_RETRY_BASE_DELAY_SEC) * (2 ** max(0, attempts))
            delay = min(delay, 30.0)
            next_dt = now + datetime.timedelta(seconds=delay)
            setattr(t, "next_retry_at", next_dt.strftime("%Y-%m-%d %H:%M:%S"))
            self._append_task_log(t, f"\n♻ 自动重试入队 attempts={attempts + 1} | next={t.next_retry_at}\n")

        self.root.after(max(1000, int(AUTO_RETRY_TICK_SEC * 1000)), self._tick_auto_retry)

    # ---------- billing label ----------
    def _update_billing_label(self):
        try:
            st = self.billing.today_stats()
            self.bill_label.configure(
                text=f"Billing {st['date']}: net={st['net_amount']} | charge={st['charge_count']} | actual={st['actual_paid_count']}"
            )
        except Exception:
            self.bill_label.configure(text="Billing: -")

    # ---------- provider sync ----------
    def _sync_provider_ui(self):
        pv = self.provider_var.get()
        if pv == "xintian":
            self.base_url_var.set(XINTIAN_DEFAULT_BASE)
            self.model_cb["values"] = XINTIAN_MODELS
            if self.model_var.get() not in XINTIAN_MODELS:
                self.model_var.set(XINTIAN_MODELS[0])
        elif pv == "lingke":
            self.base_url_var.set(LINGKE_DEFAULT_BASE)
            self.model_cb["values"] = LINGKE_MODELS
            if self.model_var.get() not in LINGKE_MODELS:
                self.model_var.set(LINGKE_MODELS[0])
        elif pv == "toapis":
            self.base_url_var.set(TOAPIS_DEFAULT_BASE)
            self.model_cb["values"] = TOAPIS_MODELS
            if self.model_var.get() not in TOAPIS_MODELS:
                self.model_var.set(TOAPIS_MODELS[0])
        else:
            self.base_url_var.set(APIYI_DEFAULT_BASE)
            self.model_cb["values"] = APIYI_MODELS
            if self.model_var.get() not in APIYI_MODELS:
                self.model_var.set(APIYI_MODELS[0])

    # ---------- key persistence ----------
    def load_keys(self):
        passphrase = self.passphrase_var.get().strip()
        if not passphrase:
            messagebox.showwarning("需要Passphrase", "请输入 passphrase 再加载。")
            return
        try:
            if CRYPTO_OK:
                cfg = load_encrypted_config(passphrase)
            else:
                p = Path(PLAIN_CONFIG_FILE)
                cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

            if not cfg:
                messagebox.showinfo("无配置", "未找到已保存的配置。")
                return

            self.current_config = cfg
            self.api_key_var.set(cfg.get("api_key", ""))
            self.key_loaded_var.set(True)
            self.key_state.configure(text="Key 已加载", foreground="green")
            self.log_line("✅ 已加载 Key 配置\n")

            try:
                self.api_key_entry.configure(show="•")
            except Exception:
                pass

        except Exception as e:
            messagebox.showerror("加载失败", str(e))

    def save_keys(self):
        passphrase = self.passphrase_var.get().strip()
        if not passphrase:
            messagebox.showwarning("需要Passphrase", "请输入 passphrase 再保存。")
            return
        cfg = {"api_key": (self.api_key_var.get() or "").strip()}
        try:
            if CRYPTO_OK:
                save_encrypted_config(passphrase, cfg)
            else:
                Path(PLAIN_CONFIG_FILE).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

            self.key_loaded_var.set(True)
            self.key_state.configure(text="Key 已保存", foreground="green")
            self.log_line("✅ 已保存 Key 配置\n")

            first_time = not self._key_marker_file.exists()
            try:
                self._key_marker_file.write_text("saved", encoding="utf-8")
            except Exception:
                pass

            if first_time:
                try:
                    self.api_key_entry.configure(show="")
                    self.root.after(10000, lambda: self.api_key_entry.configure(show="•"))
                except Exception:
                    pass
            else:
                try:
                    self.api_key_entry.configure(show="•")
                except Exception:
                    pass

        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    # ---------- mission IO ----------
    def pick_mission_root(self):
        p = filedialog.askdirectory(title="选择 Mission Root")
        if p:
            self.mission_root_var.set(norm_path(p))

    def pick_download_root(self):
        p = filedialog.askdirectory(title="选择 Download Root")
        if p:
            self.download_root_var.set(norm_path(p))

    def pick_image(self):
        p = filedialog.askopenfilename(
            title="选择图片",
            filetypes=[("Image", "*.png;*.jpg;*.jpeg;*.webp"), ("All", "*.*")]
        )
        if p:
            self.image_path_var.set(norm_path(p))

    def load_from_mission_latest(self):
        root = (self.mission_root_var.get() or "").strip()
        if not root or not os.path.isdir(root):
            self.log_line("❌ Mission Root 无效，请先选择 Mission Root\n")
            return

        rootp = Path(root)
        subdirs = [p for p in rootp.iterdir() if p.is_dir()]
        if not subdirs:
            self.log_line("❌ Mission Root 下没有子目录\n")
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
            txts = sorted(latest_dir.glob("*.txt"))
            if txts:
                try:
                    prompt_text = txts[0].read_text(encoding="utf-8", errors="ignore").strip()
                except Exception:
                    prompt_text = ""

        imgs = []
        for pat in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.PNG", "*.JPG", "*.JPEG", "*.WEBP"):
            imgs.extend(list(latest_dir.glob(pat)))
        img_path = str(max(imgs, key=lambda p: p.stat().st_mtime)) if imgs else ""

        self._set_prompt_text(prompt_text)
        self.image_path_var.set(norm_path(img_path))
        self.group_var.set(latest_dir.name)
        if not (self.note_var.get() or "").strip():
            self.note_var.set(latest_dir.name)
        self._normalize_all_paths()

        self.log_line(f"✅ 已从最新目录读取（不入队）：{latest_dir}\n")
        if not prompt_text:
            self.log_line("⚠️ 未找到可读取的 prompt 文本（prompt.txt 或 *.txt）\n")
        if not img_path:
            self.log_line("⚠️ 未找到图片（png/jpg/jpeg/webp）\n")

    # ---------- table ----------
    def refresh_table(self):
        try:
            y_view = self.tree.yview()
        except Exception:
            y_view = None

        for iid in self.tree.get_children():
            self.tree.delete(iid)

        grouped: dict[str, list[TaskItem]] = {}
        for t in self._filtered_tasks():
            g_name = (t.group or "Ungrouped").strip() or "Ungrouped"
            grouped.setdefault(g_name, []).append(t)

        for g_name in grouped:
            grouped[g_name].sort(key=lambda x: x.created_at or "")

        for g_name, task_list in grouped.items():
            gid = f"group_{hashlib.md5(g_name.encode('utf-8')).hexdigest()[:10]}"
            # ✅ 修复：group 行 values 长度必须等于列数
            self.tree.insert("", "end", iid=gid, text=f"📂 {g_name}（{len(task_list)}）", values=("",) * len(cols),
                             open=True)
            for t in task_list:
                self.tree.insert(gid, "end", iid=t.task_id, text=t.task_id, values=self._row_values(t))

        self._update_billing_label()

        if y_view is not None:
            try:
                self.tree.yview_moveto(y_view[0])
            except Exception:
                pass

    def clear_filters(self):
        self.filter_status_var.set("ALL")
        self.filter_provider_var.set("ALL")
        self.search_var.set("")
        self.refresh_table()

    def _filtered_tasks(self) -> list[TaskItem]:
        status_filter = self.filter_status_var.get()
        provider_filter = self.filter_provider_var.get()
        search_query = (self.search_var.get() or "").strip().lower()

        out: list[TaskItem] = []
        for t in self.tasks.values():
            if status_filter != "ALL" and t.status != status_filter:
                continue
            if provider_filter != "ALL" and (t.provider or "") != provider_filter:
                continue
            if search_query:
                haystack = f"{t.task_id} {t.note} {t.group} {t.model} {t.prompt}".lower()
                if search_query not in haystack:
                    continue
            out.append(t)

        out.sort(key=lambda x: x.created_at or "")
        return out

    def selected_task_ids(self) -> list[str]:
        out: list[str] = []
        for iid in self.tree.selection():
            if self._is_group_iid(iid):
                out.extend(list(self.tree.get_children(iid)))
            else:
                out.append(iid)
        seen = set()
        return [x for x in out if not (x in seen or seen.add(x))]

    def _display_status(self, t: TaskItem) -> str:
        st = t.status or ""
        nra = getattr(t, "next_retry_at", None)
        if isinstance(nra, str) and nra.strip():
            return f"{st} | retry@{nra}"
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

    def _get_next_tid(self) -> str:
        while True:
            tid = f"T{self.task_counter:05d}"
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
    ) -> TaskItem:
        tid = self._get_next_tid()
        log_file = norm_path(str(Path(LOG_DIR) / f"{tid}.log.txt"))

        t = TaskItem(
            task_id=tid,
            provider=(provider or "").strip() or "xintian",
            base_url=_norm_maybe_url(base_url),
            model=(model or "").strip(),
            prompt=(prompt or "").strip(),
            image_path=norm_path(image_path),
            note=(note or "").strip(),
            group=(group or "").strip() or "Manual",
            log_file=log_file,
            created_at=now_str(),
            batch_id=(batch_id or "LEGACY"),
        )
        self._init_task_fields(t)
        t.status = "Queued"
        t.progress = 0.0
        t.next_retry_at = None
        self.tasks[tid] = t
        return t

    # ---------- import/export ----------
    def export_tasks_ui(self):
        p = filedialog.asksaveasfilename(
            title="导出 tasks.json",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")]
        )
        if not p:
            return
        export_tasks_to(p, self.tasks)
        self.log_line(f"📤 任务已成功导出至：{p}\n")

    def import_tasks_ui(self):
        p = filedialog.askopenfilename(
            title="导入 tasks.json",
            filetypes=[("JSON", "*.json")]
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
            added += 1

        self.log_line(f"📥 已成功导入 {added} 个任务。\n")
        self._normalize_all_paths()
        self.refresh_table()

    # ---------- batch download ----------
    def batch_download_ui(self):
        mode = self.download_mode_var.get()
        selected = [self.tasks[tid] for tid in self.selected_task_ids() if tid in self.tasks]
        filtered = self._filtered_tasks()

        tasks_to_download: list[TaskItem] = []
        if mode == "selected":
            tasks_to_download = [t for t in selected if t.status == "Success" and (t.video_url or "").strip()]
        elif mode == "filtered_success":
            tasks_to_download = [t for t in filtered if t.status == "Success" and (t.video_url or "").strip()]
        else:
            tasks_to_download = [
                t for t in filtered
                if (t.status == "Success" and (t.video_url or "").strip()) or (t.status == "Done(No URL)")
            ]

        if not tasks_to_download:
            messagebox.showinfo("无可下载", "未找到符合条件的视频链接。")
            return

        self._run_batch_download_logic(tasks_to_download)

    def _run_batch_download_logic(self, tasks: list[TaskItem]):
        out_root = Path((self.download_root_var.get() or "").strip())
        folder_name = self._sanitize_folder_name(self.download_folder_var.get())
        if folder_name:
            out_root = out_root / folder_name
        out_root.mkdir(parents=True, exist_ok=True)
        index_path = out_root / DOWNLOAD_INDEX_FILE

        stop_flag = threading.Event()

        dlg = tk.Toplevel(self.root)
        dlg.title("批量归档下载")
        dlg.geometry("520x220")
        dlg.transient(self.root)
        dlg.grab_set()

        folder_tip = f"\n归档目录：{folder_name}" if folder_name else "\n归档目录：默认根目录"
        ttk.Label(dlg, text=f"准备下载：{len(tasks)} 个任务（自动去重）{folder_tip}").pack(pady=10)

        overall = ttk.Progressbar(dlg, length=440, mode="determinate")
        overall.pack(pady=6)
        current_lbl = ttk.Label(dlg, text="等待启动...")
        current_lbl.pack(pady=6)

        btns = ttk.Frame(dlg)
        btns.pack(pady=10)
        ttk.Button(btns, text="取消下载", command=lambda: stop_flag.set()).pack()

        def ui_update_overall(done, total):
            self.root.after(0, lambda: [
                overall.configure(maximum=total, value=done),
                current_lbl.configure(text=f"进度: {done} / {total}")
            ])

        def ui_done(ok, skipped, failed):
            def _():
                try:
                    dlg.destroy()
                except Exception:
                    pass
                messagebox.showinfo("下载任务结束", f"✅ 成功: {ok}\n⏭️ 跳过: {skipped}\n❌ 失败: {failed}")
            self.root.after(0, _)

        def log_cb(msg: str):
            self.root.after(0, lambda: self.log_line(msg))

        dl_workers = _safe_int(self.dl_workers_var.get(), 3)
        retries = _safe_int(self.dl_retries_var.get(), 2)
        skip = bool(self.dl_skip_dup_var.get())

        thread = threading.Thread(
            target=run_batch_download,
            args=(self.root, tasks, out_root, index_path, retries, skip, dl_workers, stop_flag, log_cb,
                  ui_update_overall, None, ui_done),
            daemon=True
        )
        thread.start()

    # ---------- add/import mission (你原逻辑较长，这里保留你的函数名，建议继续用你原来的实现) ----------
    def import_mission_all(self):
        """
        扫描 Mission Root：遍历所有子目录（深度），凡是目录内存在 txt + 图片，即生成任务入队。
        - 每子目录最多：0=不限；>0=最多生成 N 条（按 txt 文件顺序）
        - group：使用相对路径（避免同名子目录冲突）
        - 去重：跨历史去重（同 group + prompt + image_path）
          ✅ 新增：如果检测到重复，会弹窗问是否继续，并提示“重复但参数修改”改了什么
          ✅ 修复：同时去重本次扫描内部重复
          ✅ 修复：未加载Key时，默认“只入队不启动”（自动暂停）
        """
        root_dir = (self.mission_root_var.get() or "").strip()
        root_dir = norm_path(root_dir)
        if not root_dir or not os.path.isdir(root_dir):
            messagebox.showerror("路径错误", "Mission Root 目录不存在，请先选择。")
            return

        # ✅ 未加载 Key：允许扫描入队，但自动暂停启动（避免 autorun 直接全失败）
        if (not self.key_loaded_var.get()) and (not (self.api_key_var.get() or "").strip()):
            self.pause_new_tasks_var.set(True)
            self.log_line("⚠️ 当前未加载/未填写 API Key：将仅扫描入队，不会自动启动任务。\n")
            self.log_line("👉 请先加载 Key，然后点击 Resume New 再开始跑。\n")

        limit_each = self.mission_n_each_var.get()
        limit_each = int(limit_each) if isinstance(limit_each, int) else 0
        if limit_each < 0:
            limit_each = 0  # 0=不限

        repeat = _safe_int(self.repeat_add_var.get(), 1)
        if repeat < 1:
            repeat = 1

        provider = (self.provider_var.get() or "").strip()
        base_url = (self.base_url_var.get() or "").strip()  # 交给 _create_task_and_add 归一化
        model = (self.model_var.get() or "").strip()

        # 读取 txt 兼容
        try:
            from .utils import read_text_safely  # type: ignore
        except Exception:
            def read_text_safely(p: Path) -> str:
                return p.read_text(encoding="utf-8", errors="ignore")

        root_path = Path(root_dir)
        self.log_line(f"🔍 正在深度扫描: {root_path}\n")
        self.log_line(f"🧩 Repeat={repeat} | 每子目录最多={limit_each if limit_each != 0 else '不限'}\n")

        # all_dirs = 所有“包含文件”的目录（深度）
        all_dirs = sorted({p.parent for p in root_path.rglob("*") if p.is_file()})
        batch_id = self._new_batch_id()

        candidates: list[dict] = []
        for sub in all_dirs:
            txt_files = sorted(sub.glob("*.txt"))
            if not txt_files:
                continue

            img_files: list[Path] = []
            for pat in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.PNG", "*.JPG", "*.JPEG", "*.WEBP"):
                img_files.extend(list(sub.glob(pat)))
            if not img_files:
                continue

            img_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            default_img = norm_path(str(img_files[0]))
            if not default_img or not Path(default_img).exists():
                continue

            # ✅ 用相对路径做 group，避免同名目录冲突
            try:
                group_name = norm_path(str(sub.relative_to(root_path)))
            except Exception:
                group_name = sub.name

            produce_txts = txt_files if limit_each == 0 else txt_files[:limit_each]

            for txtp in produce_txts:
                try:
                    prompt_content = read_text_safely(txtp).strip()
                except Exception:
                    prompt_content = ""

                if not prompt_content:
                    continue

                for i in range(repeat):
                    note_i = txtp.stem
                    if repeat > 1:
                        note_i = f"{txtp.stem} #{i + 1}/{repeat}"

                    candidates.append({
                        "provider": provider,
                        "base_url": base_url,
                        "model": model,
                        "prompt": prompt_content,
                        "image_path": default_img,
                        "note": note_i,
                        "group": group_name,
                        "batch_id": batch_id,
                    })

        if not candidates:
            self.log_line("ℹ️ 未扫描到可入队的 txt+图片组合\n")
            return

        # ✅ 新增：重复检测弹窗
        proceed, allow_dups = self._check_dups_before_bulk_add(candidates)
        if not proceed:
            self.log_line("⛔ 已取消本次批量扫描入队\n")
            return

        # 历史索引用于跳过重复（当 allow_dups=False）
        hist_sig = set()
        for t in self.tasks.values():
            hist_sig.add(self._task_signature_key(t.group or "", t.prompt or "", t.image_path or ""))

        created_count = 0
        skipped_dup = 0

        for c in candidates:
            sig = self._task_signature_key(c["group"], c["prompt"], c["image_path"])

            # ✅ 修复：同时去重“本次扫描内部重复”
            if (not allow_dups) and (sig in hist_sig):
                skipped_dup += 1
                continue

            self._create_task_and_add(
                provider=c["provider"],
                base_url=c["base_url"],
                model=c["model"],
                prompt=c["prompt"],
                image_path=c["image_path"],
                note=c["note"],
                group=c["group"],
                batch_id=c["batch_id"],
            )
            created_count += 1

            # ✅ 修复：把本次新增也加入 sig 集合，防止 candidates 内部重复继续加入
            hist_sig.add(sig)

        self.refresh_table()
        self.log_line(f"✅ 扫描完成！新增 {created_count} 条任务；跳过重复 {skipped_dup} 条。\n")
        if self.pause_new_tasks_var.get():
            self.log_line("⏸ 当前处于暂停状态：已入队但不会自动启动。加载Key后点 Resume New。\n")
        else:
            self.log_line("🤖 队列将自动启动（无需点 Start Queued）\n")

    def add_task(self):
        if not self.key_loaded_var.get() and not (self.api_key_var.get() or "").strip():
            messagebox.showwarning("未加载Key", "请先加载或填写 API Key。")
            return

        prompt = self._get_prompt_text()
        img = norm_path((self.image_path_var.get() or "").strip())

        if not prompt or not img or not Path(img).exists():
            messagebox.showwarning("输入错误", "请检查 Prompt 和图片路径是否有效。")
            return

        provider = (self.provider_var.get() or "").strip()
        base_url = (self.base_url_var.get() or "").strip()
        model = (self.model_var.get() or "").strip()
        note = (self.note_var.get() or "").strip()
        group = (self.group_var.get() or "").strip() or "Manual"

        repeat = _safe_int(self.repeat_add_var.get(), 1)
        if repeat < 1:
            repeat = 1

        batch_id = self._new_batch_id()

        created = 0
        for i in range(repeat):
            note_i = note
            if repeat > 1:
                note_i = f"{note} #{i + 1}/{repeat}".strip()

            self._create_task_and_add(
                provider=provider,
                base_url=base_url,
                model=model,
                prompt=prompt,
                image_path=img,
                note=note_i,
                group=group,
                batch_id=batch_id,
            )
            created += 1

        self.log_line(f"➕ 已添加任务 {created} 个 | batch={batch_id} | {provider} | {model} | group={group}\n")
        self.refresh_table()

    # ---------- delete/stop/retry/open ----------
    def delete_selected(self):
        tids = self.selected_task_ids()
        if not tids:
            return

        if not messagebox.askyesno("确认删除", f"确定要删除选中的 {len(tids)} 个任务吗？"):
            return

        for tid in tids:
            t = self.tasks.get(tid)
            if t:
                try:
                    if getattr(t, "stop_event", None):
                        t.stop_event.set()
                except Exception:
                    pass
                self.tasks.pop(tid, None)

        self.refresh_table()
        self.log_line(f"🗑 已从列表中移除 {len(tids)} 个任务\n")

    def open_selected_url(self):
        tids = self.selected_task_ids()
        if not tids:
            return
        t = self.tasks.get(tids[0])
        if not t:
            return
        url = (getattr(t, "video_url", "") or "").strip()
        if url:
            webbrowser.open(url)
        else:
            messagebox.showinfo("提示", "该任务尚未生成有效的视频链接。")

    def stop_selected(self):
        tids = self.selected_task_ids()
        for tid in tids:
            t = self.tasks.get(tid)
            if not t:
                continue
            if getattr(t, "stop_event", None):
                t.stop_event.set()
            if t.status in ("Running", "Pending(Check)"):
                t.status = "Stopped"
                self.billing.append({"task_id": t.task_id, "event": EV_MANUAL_CANCEL, "amount": 0, "provider": t.provider})
                self._append_task_log(t, "\n⛔ 手动停止\n")
                self._ui_update_task_row(t)
        self.refresh_table()

    def retry_selected(self):
        tids = self.selected_task_ids()
        for tid in tids:
            t = self.tasks.get(tid)
            if not t:
                continue
            if t.status in ("Failed", "Done(No URL)", "Stopped"):
                t.status = "Queued"
                t.progress = 0.0
                t.next_retry_at = None
                self.billing.append({"task_id": t.task_id, "event": EV_RETRY_STARTED, "amount": 0, "provider": t.provider})
                self._append_task_log(t, "\n♻ 已手动入队重试\n")
        self.refresh_table()

    # ---------- start queued ----------
    def start_all_queued(self):
        if self.pause_new_tasks_var.get():
            self.log_line("⏸ 当前处于暂停状态：不会启动新的 Queued 任务。\n")
            return
        if self.gate.queue_frozen:
            self.log_line("🧊 Queue Gate 冻结中：已阻止启动新任务。\n")
            return

        queued_count = 0
        for t in self.tasks.values():
            if t.status == "Queued":
                self._start_task(t)
                queued_count += 1
        if queued_count > 0:
            self.log_line(f"🚀 已批量启动 {queued_count} 个任务\n")
        self.refresh_table()

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
            Path(t.log_file).parent.mkdir(exist_ok=True, parents=True)
            with open(t.log_file, "a", encoding="utf-8") as f:
                f.write(msg)
        except Exception:
            pass
        self.root.after(0, lambda: self.log_line(f"[{t.task_id}] {msg}"))

    def _ui_update_task_row(self, t: TaskItem):
        def _():
            if self.tree.exists(t.task_id):
                # columns = ("task_id","status","provider","model","status_msg","progress","note","group","created_at","remote_id","video_url")
                self.tree.item(t.task_id, values=(
                    t.task_id,
                    self._display_status(t),
                    t.provider,
                    t.model,
                    getattr(t, "status_msg", "") or "",
                    f"{float(getattr(t, 'progress', 0.0) or 0.0):.1f}%",
                    t.note,
                    t.group,
                    t.created_at,
                    getattr(t, "remote_id", "") or "",
                    getattr(t, "video_url", "") or ""
                ))
            self._update_billing_label()

        self.root.after(0, _)

    # ---------- remote_id polling ----------
    def poll_selected_remote_id(self):
        tids = self.selected_task_ids()
        if not tids:
            return

        count = 0
        for tid in tids:
            t = self.tasks.get(tid)
            if not t:
                continue
            if not (getattr(t, "remote_id", "") or "").strip():
                continue
            try:
                if getattr(t, "future", None) and t.future and not t.future.done():
                    continue
            except Exception:
                pass
            self._resume_task_by_remote_id(t)
            count += 1

        self.log_line(f"🔎 手动轮询 RemoteID：{count} 个任务\n")

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
            self.log_line(f"🔄 启动后自动续跑 remote_id 任务：{resumed} 个\n")

    def _resume_task_by_remote_id(self, t: TaskItem):
        if self.pause_new_tasks_var.get() or self.gate.queue_frozen:
            return
        try:
            if getattr(t, "future", None) and t.future and not t.future.done():
                return
        except Exception:
            pass

        api_key = (self.api_key_var.get() or "").strip()
        provider = (t.provider or "").strip()
        if provider == "auto":
            provider = "xintian" if "sora-2" in (t.model or "") else "apiyi"
        if not (getattr(t, "remote_id", "") or "").strip():
            return

        t.stop_event = threading.Event()
        t.status = "Pending(Check)"
        self._ui_update_task_row(t)
        self._append_task_log(t, f"\n🔄 续跑(remote_id)轮询中 | {now_str()}\n")

        def on_text(s: str):
            self._append_task_log(t, s)
            p = parse_progress_percent(s)
            if p is not None:
                try:
                    t.progress = float(p)
                except Exception:
                    pass
                self._ui_update_task_row(t)

        def on_done(video_url: str | None):
            if video_url:
                t.video_url = video_url
                t.status = "Success"
                t.progress = 100.0
                self.billing.mark_actual_cost_once(t, provider, t.model, t.remote_id or "", video_url)
            else:
                t.status = "Done(No URL)"
            self._ui_update_task_row(t)
            self.refresh_table()

        def on_error(err: str):
            err = (err or "").strip()
            t.last_error = err
            t.status = "Failed"
            t.status_msg = compact_status_msg(err) or "Poll Failed"
            self._ui_update_task_row(t)
            self.refresh_table()

        def run():
            try:
                if provider == "xintian":
                    cfg = XintianConfig(api_key=api_key, base_url=t.base_url, model=t.model, prompt=t.prompt, image_path=t.image_path)
                    run_xintian_poll_existing(cfg, t.remote_id, on_text, on_done, on_error, t.stop_event)
                elif provider == "lingke":
                    from .providers.lingke import run_lingke_poll_existing
                    cfg = LingkeConfig(api_key=api_key, base_url=t.base_url, preset=t.model, prompt=t.prompt, image_path=t.image_path, verify_ssl=True, poll_interval_sec=3)
                    run_lingke_poll_existing(cfg, t.remote_id, on_text, on_done, on_error, t.stop_event)
                else:
                    on_error("该 provider 暂不支持按 remote_id 续跑。")
            except Exception as e:
                on_error(str(e))

        t.future = self.executor.submit(run)

    # ---------- core runner ----------
    # ---------------- core runner ----------------
    def _start_task(self, t: TaskItem):
        # ✅ 暂停：不启动
        if self.pause_new_tasks_var.get():
            return

        # ✅ Gate 冻结：不启动
        if self.gate.queue_frozen:
            return

        # ✅ 已在跑：不重复启动
        if getattr(t, "future", None) is not None:
            try:
                if t.future and not t.future.done():
                    return
            except Exception:
                pass

        # ✅ 防御：Key 为空时，autorun 也可能触发启动
        api_key = (self.api_key_var.get() or "").strip()
        if not api_key:
            t.status = "Failed"
            t.last_error = "missing_api_key"
            t.status_msg = "Missing API Key"
            self._append_task_log(t, "\n❌ Missing API Key：请先加载/填写 Key，再重试。\n")
            self._ui_update_task_row(t)
            self.root.after(0, self.refresh_table)
            return

        # ✅ reset runtime fields
        t.stop_event = threading.Event()
        t.status = "Running"
        t.progress = 0.0
        t.last_error = ""
        t.status_msg = ""  # ✅ 每次启动清空一次状态信息
        t.run_attempt = (getattr(t, "run_attempt", 0) or 0) + 1

        self._append_task_log(t, f"\n▶ 开始运行 | 尝试次数={t.run_attempt} | {now_str()}\n")

        provider = (t.provider or "").strip() or "xintian"
        if provider == "auto":
            provider = "xintian" if ("sora-2" in (t.model or "")) else "apiyi"

        # ✅ refresh_table 防抖：避免并发时频繁重绘
        def _debounced_refresh():
            try:
                if hasattr(self, "_refresh_timer"):
                    self.root.after_cancel(self._refresh_timer)
            except Exception:
                pass
            self._refresh_timer = self.root.after(120, self.refresh_table)

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
            t.remote_id = rid
            try:
                self.billing.charge_remote_id_once(t, provider, t.model, rid)
            except Exception:
                pass

            self._ui_update_task_row(t)

            # ✅ 立刻检查 batch gate 是否可以放行
            self._maybe_release_batch_gate_on_remote_id(t)

            # ✅ 分组计数/筛选刷新（防抖）
            self.root.after(0, _debounced_refresh)

        def on_done(video_url: str | None):
            if (video_url or "").strip():
                t.video_url = (video_url or "").strip()
                t.status = "Success"
                t.progress = 100.0
                try:
                    self.billing.mark_actual_cost_once(t, provider, t.model, (t.remote_id or ""), t.video_url)
                except Exception:
                    pass
            else:
                t.status = "Done(No URL)"
                t.progress = float(getattr(t, "progress", 0.0) or 0.0)

            self._ui_update_task_row(t)
            self.root.after(0, _debounced_refresh)

        def on_error(err: str):
            err = (err or "").strip()
            t.last_error = err
            t.status = "Failed"

            # ✅ 状态信息栏：只显示精简信息（你要的 “HTTP 401 无效的令牌”）
            t.status_msg = compact_status_msg(err) or "Failed"

            # ✅ 退款：仅当 remote_id 有值才触发
            if (t.remote_id or "").strip():
                try:
                    self.billing.refund_failed_once(t, provider, t.model, t.remote_id, "api_error")
                except Exception:
                    pass

            self._ui_update_task_row(t)
            self.root.after(0, _debounced_refresh)

        def run():
            try:
                if provider == "xintian":
                    cfg = XintianConfig(
                        api_key=api_key,
                        base_url=t.base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                    )
                    run_xintian_upload_and_poll(cfg, on_text, on_remote_id, on_done, on_error, t.stop_event)

                elif provider == "lingke":
                    cfg = LingkeConfig(
                        api_key=api_key,
                        base_url=t.base_url,
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
                        base_url=t.base_url,
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

                else:
                    cfg = ApiyiConfig(
                        api_key=api_key,
                        base_url=t.base_url,
                        model=t.model,
                        prompt=t.prompt,
                        image_path=t.image_path,
                    )
                    run_apiyi_sse(cfg, on_text, on_done, on_error, t.stop_event)

            except Exception as e:
                on_error(str(e))

        t.future = self.executor.submit(run)
        self._ui_update_task_row(t)
        self.root.after(0, _debounced_refresh)

def run_app():
    root = tk.Tk()
    App(root)
    root.mainloop()
