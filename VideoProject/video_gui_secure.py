# -*- coding: utf-8 -*-
import base64
import json
import os
import re
import threading
import webbrowser
import hashlib
import uuid
import datetime
import itertools
from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, Future
from urllib.parse import urlparse
import time
import shutil

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import requests
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.fernet import Fernet
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# =========================
# ✅ 固定配置
# =========================
APP_TITLE = "图生视频（AUTO/双API·多任务并发·任务记忆·Mission批量·导入导出·筛选·归档下载·加密配置·带日志）"
CONFIG_FILE = "config.enc"
SALT_FILE = "config.salt"
LOG_DIR = "logs"
DOWNLOAD_INDEX_FILE = "download_index.json"  # 放在归档根目录下

TASKS_STORE_FILE = "tasks.json"
TASKS_STORE_MAX = 5000

ENABLE_MACHINE_BINDING = False
ALLOWLIST_FILE = "machine.allow"

# Provider 1: apiyi SSE chat-completions
APIYI_DEFAULT_BASE = "https://api.apiyi.com/v1"
APIYI_MODELS = [
    "sora_video2",
    "sora_video2-landscape",
    "sora_video2-15s",
    "sora_video2-landscape-15s",
    "sora-2-pro",
]

# Provider 2: xintian upload + polling
XINTIAN_DEFAULT_BASE = "https://api.xintianwengai.com"
XINTIAN_MODELS = [
    "sora-2-landscape-10s",
    "sora-2-portrait-10s",
    "sora-2-landscape-15s",
    "sora-2-portrait-15s",
]

# ✅ Provider UI：新增 AUTO
PROVIDERS = [
    ("AUTO（自动选择可用渠道）", "auto"),
    ("APIYI（/chat/completions 流式）", "apiyi"),
    ("XINTIAN（/v1/videos 上传+轮询）", "xintian"),
]

# ✅ Mission 默认路径（可在 UI 改）
MISSION_ROOT_DEFAULT = Path(r"C:\Users\22892\Desktop\RPA-file\Mission")
DOWNLOAD_ROOT_DEFAULT = Path(r"C:\Users\22892\Desktop\RPA-file\Download")

SUPPORTED_IMAGE_EXTS = [".webp", ".png", ".jpg", ".jpeg"]


# =========================
# Helpers
# =========================
def guess_mime(path: str) -> str:
    suf = Path(path).suffix.lower().lstrip(".")
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(suf, "application/octet-stream")


def image_to_data_url(image_path: str) -> str:
    mime = guess_mime(image_path)
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def build_chat_completions_url(base_url: str) -> str:
    base = normalize_base_url(base_url)
    return f"{base}/chat/completions"


def build_xintian_videos_url(base_url: str) -> str:
    base = normalize_base_url(base_url)
    return f"{base}/v1/videos"


def build_xintian_video_status_url(base_url: str, video_id: str) -> str:
    base = normalize_base_url(base_url)
    return f"{base}/v1/videos/{video_id}"


def extract_video_url(text: str) -> str | None:
    m = re.search(r"\((https?://[^)]+)\)", text)
    if m:
        return m.group(1)
    m = re.search(r"(https?://\S+)", text)
    if m:
        return m.group(1)
    return None


def safe_json_loads(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def find_first_key(obj, keys):
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k]:
                return obj[k]
        for v in obj.values():
            r = find_first_key(v, keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for it in obj:
            r = find_first_key(it, keys)
            if r is not None:
                return r
    return None


def parse_progress_percent(text: str) -> float | None:
    patterns = [
        r"进度[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*%",
        r"progress[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*%",
        r"percent[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*%",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1))
                if 0 <= v <= 100:
                    return v
            except Exception:
                pass
    return None


def safe_filename(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "Unnamed"
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:160] if len(s) > 160 else s


def read_text_safely(p: Path) -> str:
    # 兼容未知编码
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin1"):
        try:
            return p.read_text(encoding=enc, errors="strict")
        except Exception:
            continue
    return p.read_text(encoding="utf-8", errors="ignore")


# =========================
# Machine binding (optional)
# =========================
def machine_fingerprint() -> str:
    raw = f"{uuid.getnode()}|{os.name}|{os.getenv('COMPUTERNAME','')}|{os.getenv('HOSTNAME','')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_machine_allowed() -> bool:
    if not ENABLE_MACHINE_BINDING:
        return True
    fp = machine_fingerprint()
    allow_path = Path(ALLOWLIST_FILE)
    if not allow_path.exists():
        return False
    allowed = {line.strip() for line in allow_path.read_text(encoding="utf-8").splitlines() if line.strip()}
    return fp in allowed


# =========================
# Encryption / Decryption
# =========================
def get_or_create_salt() -> bytes:
    salt_path = Path(SALT_FILE)
    if salt_path.exists():
        return salt_path.read_bytes()
    salt = os.urandom(16)
    salt_path.write_bytes(salt)
    return salt


def derive_fernet_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=200_000,
    )
    key = kdf.derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(key)


def encrypt_config(passphrase: str, config_obj: dict) -> bytes:
    salt = get_or_create_salt()
    key = derive_fernet_key(passphrase, salt)
    f = Fernet(key)
    plaintext = json.dumps(config_obj, ensure_ascii=False).encode("utf-8")
    return f.encrypt(plaintext)


def decrypt_config(passphrase: str, token: bytes) -> dict:
    salt = get_or_create_salt()
    key = derive_fernet_key(passphrase, salt)
    f = Fernet(key)
    plaintext = f.decrypt(token)
    return json.loads(plaintext.decode("utf-8"))


def load_encrypted_config(passphrase: str) -> dict | None:
    p = Path(CONFIG_FILE)
    if not p.exists():
        return None
    token = p.read_bytes()
    return decrypt_config(passphrase, token)


def save_encrypted_config(passphrase: str, config_obj: dict):
    token = encrypt_config(passphrase, config_obj)
    Path(CONFIG_FILE).write_bytes(token)


# =========================
# Provider 1: APIYI SSE
# =========================
@dataclass
class ApiyiConfig:
    api_key: str
    base_url: str
    model: str
    prompt: str
    image_path: str
    connect_timeout: int = 30
    read_timeout: int = 900
    verify_ssl: bool = True


def run_apiyi_sse(cfg: ApiyiConfig, on_text, on_done, on_error, stop_flag: threading.Event):
    try:
        url = build_chat_completions_url(cfg.base_url)
        headers = {
            "Authorization": f"Bearer {cfg.api_key.strip()}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        image_url = image_to_data_url(cfg.image_path)
        payload = {
            "model": cfg.model.strip(),
            "stream": True,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": cfg.prompt.strip()},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        }

        timeout = (cfg.connect_timeout, cfg.read_timeout)
        final_video_url = None
        all_text = []

        on_text(f"📡 Request URL: {url}\n")
        on_text("🧠 Provider: APIYI SSE\n")
        on_text(f"🧠 Model: {cfg.model}\n")
        on_text("🚀 开始生成（流式）...\n\n")

        with requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout, verify=cfg.verify_ssl) as r:
            if r.status_code != 200:
                on_text(f"\n❌ HTTP {r.status_code}\n")
                try:
                    r.encoding = "utf-8"
                    on_text(f"❌ Response: {r.text}\n")
                except Exception:
                    pass
                r.raise_for_status()

            r.encoding = "utf-8"

            for raw_line in r.iter_lines(decode_unicode=False):
                if stop_flag.is_set():
                    on_text("\n⛔ 已停止（用户取消）\n")
                    on_done(final_video_url)
                    return
                if not raw_line:
                    continue

                line = raw_line.decode("utf-8", errors="replace")
                if not line.startswith("data:"):
                    continue

                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break

                obj = safe_json_loads(data_str)
                if not obj:
                    continue

                content = None
                try:
                    content = obj["choices"][0]["delta"].get("content")
                except Exception:
                    content = find_first_key(obj, ["content"])

                if not content:
                    continue

                on_text(content)
                all_text.append(content)

                maybe = extract_video_url(content)
                if maybe:
                    final_video_url = maybe

        if not final_video_url:
            final_video_url = extract_video_url("".join(all_text))

        on_done(final_video_url)

    except Exception as e:
        on_error(str(e))


# =========================
# Provider 2: XINTIAN upload + polling
# =========================
@dataclass
class XintianConfig:
    api_key: str
    base_url: str
    model: str
    prompt: str
    image_path: str
    connect_timeout: int = 30
    read_timeout: int = 900
    verify_ssl: bool = True
    poll_interval_sec: int = 3


def run_xintian_upload_and_poll(cfg: XintianConfig, on_text, on_done, on_error, stop_flag: threading.Event):
    try:
        post_url = build_xintian_videos_url(cfg.base_url)
        headers = {"Authorization": f"Bearer {cfg.api_key.strip()}"}

        on_text(f"📡 Request URL: {post_url}\n")
        on_text("🧠 Provider: XINTIAN upload+poll\n")
        on_text(f"🧠 Model: {cfg.model}\n")
        on_text("🚀 开始创建任务（上传图片）...\n\n")

        files = {
            "input_reference": (Path(cfg.image_path).name, open(cfg.image_path, "rb"), guess_mime(cfg.image_path))
        }
        data = {"prompt": cfg.prompt.strip(), "model": cfg.model.strip()}

        timeout = (cfg.connect_timeout, cfg.read_timeout)

        try:
            r = requests.post(post_url, headers=headers, files=files, data=data, timeout=timeout, verify=cfg.verify_ssl)
        finally:
            try:
                files["input_reference"][1].close()
            except Exception:
                pass

        if r.status_code not in (200, 201):
            on_text(f"\n❌ HTTP {r.status_code}\n")
            try:
                r.encoding = "utf-8"
                on_text(f"❌ Response: {r.text}\n")
            except Exception:
                pass
            r.raise_for_status()

        r.encoding = "utf-8"
        resp_text = r.text
        on_text("✅ 任务已创建，开始查询进度...\n\n")
        on_text(f"📦 Create Response: {resp_text}\n\n")

        resp_obj = safe_json_loads(resp_text) or {}
        video_id = find_first_key(resp_obj, ["id", "video_id", "task_id", "job_id", "uuid"])
        if not video_id:
            m = re.search(r"(video_[0-9a-fA-F\-]{8,})", resp_text)
            if m:
                video_id = m.group(1)

        if not video_id or not isinstance(video_id, str):
            on_error("无法从创建响应中解析 video_id/task_id（请把日志发服务商确认返回字段）")
            return

        on_text(f"🆔 video_id: {video_id}\n\n")

        status_url = build_xintian_video_status_url(cfg.base_url, video_id)
        final_video_url = None

        done_keywords = {"succeeded", "success", "completed", "done", "finished"}
        fail_keywords = {"failed", "error", "rejected", "canceled", "cancelled"}

        last_status = None
        last_progress = None

        while True:
            if stop_flag.is_set():
                on_text("\n⛔ 已停止（用户取消）\n")
                on_done(final_video_url)
                return

            gr = requests.get(status_url, headers=headers, timeout=timeout, verify=cfg.verify_ssl)
            gr.encoding = "utf-8"
            if gr.status_code != 200:
                on_text(f"\n❌ HTTP {gr.status_code} (poll)\n")
                on_text(f"❌ Response: {gr.text}\n")
                gr.raise_for_status()

            obj = safe_json_loads(gr.text) or {}

            status = (find_first_key(obj, ["status", "state"]) or "").strip()
            progress = find_first_key(obj, ["progress", "percent", "percentage"])
            url = find_first_key(obj, ["video_url", "url", "result_url", "download_url", "mp4_url"])

            pval = None
            if isinstance(progress, (int, float)):
                pval = float(progress)
                if 0 <= pval <= 1.0:
                    pval *= 100.0
            elif isinstance(progress, str):
                m = re.search(r"([0-9]+(?:\.[0-9]+)?)", progress)
                if m:
                    try:
                        pval = float(m.group(1))
                    except Exception:
                        pval = None

            if pval is None:
                pval = parse_progress_percent(gr.text)

            if status and status != last_status:
                on_text(f"> 📌 状态：{status}\n")
                last_status = status

            if pval is not None:
                if last_progress is None or abs(pval - last_progress) >= 0.1:
                    on_text(f"> 🏃 进度：{pval:.1f}%\n")
                    last_progress = pval

            if url and isinstance(url, str):
                final_video_url = url

            status_l = status.lower() if status else ""

            if any(k in status_l for k in done_keywords) or final_video_url:
                on_text("\n> ✅ 生成成功\n")
                on_done(final_video_url)
                return

            if any(k in status_l for k in fail_keywords):
                on_error(f"任务失败：{status} | raw={gr.text}")
                return

            # sleep 可中断
            for _ in range(max(1, int(cfg.poll_interval_sec))):
                if stop_flag.is_set():
                    on_text("\n⛔ 已停止（用户取消）\n")
                    on_done(final_video_url)
                    return
                threading.Event().wait(1)

    except Exception as e:
        on_error(str(e))


# =========================
# Task model
# =========================
@dataclass
class TaskItem:
    task_id: str
    provider: str  # auto/apiyi/xintian
    base_url: str
    model: str
    prompt: str
    image_path: str
    note: str
    group: str  # ✅ 用于下载归档的目录名（Mission子目录名）
    log_file: str
    created_at: str

    status: str = "Queued"
    progress: float = 0.0
    video_url: str | None = None

    # runtime only
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
            "status": self.status,
            "progress": float(self.progress),
            "video_url": self.video_url,
        }

    @staticmethod
    def from_persist_dict(d: dict):
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
            status=d.get("status", "Queued"),
            progress=float(d.get("progress", 0.0) or 0.0),
            video_url=d.get("video_url"),
        )
        t.stop_event = threading.Event()
        t.future = None
        return t


# =========================
# GUI App
# =========================
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1540x1060")

        # keys in memory only
        self.apiyi_key = ""
        self.xintian_key = ""

        Path(LOG_DIR).mkdir(exist_ok=True)

        self.executor: ThreadPoolExecutor | None = None
        self.max_workers_var = tk.IntVar(value=3)

        self.tasks: dict[str, TaskItem] = {}
        self.task_counter = itertools.count(1)

        self.provider_label_to_key = {name: key for name, key in PROVIDERS}

        # ✅ 落盘/刷新节流
        self._dirty = False
        self._save_timer = None
        self._refresh_timer = None

        if ENABLE_MACHINE_BINDING and not is_machine_allowed():
            fp = machine_fingerprint()
            messagebox.showerror(
                "未授权机器",
                f"此电脑未在授权列表内。\n\n机器指纹：{fp}\n\n请将该指纹加入 {ALLOWLIST_FILE} 后再运行。"
            )
            root.destroy()
            return

        # ======= 顶部说明
        guide = tk.LabelFrame(root, text="✅ 使用说明（可普及版·批量 Mission 跑数）")
        guide.pack(fill="x", padx=12, pady=10)
        guide_text = (
            "① 程序启动会自动恢复 tasks.json 中的历史任务（无需重新添加）\n"
            "② 仍需【加载配置】才能执行（API Key 不会写进 tasks.json）\n"
            "③ 支持 AUTO Provider：自动选可用渠道\n"
            "④ 支持 Mission 扫描：按子目录(备注/标签)自动生成 N 任务并一键开跑\n"
            "⑤ 批量下载会按 group(子目录名)自动归档到 Download 根目录\n"
        )
        tk.Label(guide, text=guide_text, justify="left").pack(anchor="w", padx=10, pady=8)

        # ======= 安全区
        sec = tk.LabelFrame(root, text="🔐 安全区（内部口令 + 加密配置）")
        sec.pack(fill="x", padx=12, pady=6)

        tk.Label(sec, text="内部口令").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.passphrase_var = tk.StringVar(value="")
        tk.Entry(sec, textvariable=self.passphrase_var, width=34, show="*").grid(row=0, column=1, sticky="w", padx=8)
        tk.Button(sec, text="加载配置", command=self.load_config, width=12).grid(row=0, column=2, padx=6)
        tk.Button(sec, text="保存加密配置（管理员）", command=self.save_config, width=20).grid(row=0, column=3, padx=6)

        tk.Label(sec, text="机器指纹（可选）").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.fp_var = tk.StringVar(value=machine_fingerprint())
        tk.Entry(sec, textvariable=self.fp_var, width=78, state="readonly").grid(row=1, column=1, columnspan=3, sticky="w", padx=8)
        tk.Button(sec, text="复制指纹", command=lambda: self.copy_to_clipboard(self.fp_var.get()), width=10).grid(row=1, column=4, padx=6)

        # ======= 批量 Mission 区
        mission = tk.LabelFrame(root, text="📦 Mission 批量任务源（自动遍历子目录 -> 图片+txt -> 生成任务）")
        mission.pack(fill="x", padx=12, pady=6)

        tk.Label(mission, text="Mission 根目录").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.mission_root_var = tk.StringVar(value=str(MISSION_ROOT_DEFAULT))
        tk.Entry(mission, textvariable=self.mission_root_var, width=86).grid(row=0, column=1, sticky="we", padx=8)
        tk.Button(mission, text="选择…", command=self.pick_mission_root, width=10).grid(row=0, column=2, padx=6)

        tk.Label(mission, text="Download 归档根目录").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.download_root_var = tk.StringVar(value=str(DOWNLOAD_ROOT_DEFAULT))
        tk.Entry(mission, textvariable=self.download_root_var, width=86).grid(row=1, column=1, sticky="we", padx=8)
        tk.Button(mission, text="选择…", command=self.pick_download_root, width=10).grid(row=1, column=2, padx=6)

        tk.Label(mission, text="每个子目录生成任务数").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        self.mission_n_var = tk.IntVar(value=5)
        tk.Spinbox(mission, from_=1, to=20, width=6, textvariable=self.mission_n_var).grid(row=2, column=1, sticky="w", padx=8)

        tk.Button(mission, text="扫描 Mission 并生成任务", command=self.scan_mission_and_create_tasks, width=26)\
            .grid(row=2, column=2, padx=6)

        mission.grid_columnconfigure(1, weight=1)

        # ======= 参数区
        form = tk.LabelFrame(root, text="⚙️ 单任务参数（点击任务可回填）")
        form.pack(fill="x", padx=12, pady=6)

        tk.Label(form, text="Provider").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.provider_var = tk.StringVar(value=PROVIDERS[0][0])
        self.provider_box = ttk.Combobox(form, textvariable=self.provider_var, values=[p[0] for p in PROVIDERS],
                                         state="readonly", width=30)
        self.provider_box.grid(row=0, column=1, sticky="w", padx=8)
        self.provider_box.bind("<<ComboboxSelected>>", lambda e: self.on_provider_change())

        tk.Label(form, text="端点 Base URL").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.base_url_var = tk.StringVar(value=APIYI_DEFAULT_BASE)
        tk.Entry(form, textvariable=self.base_url_var, width=88).grid(row=1, column=1, sticky="we", padx=8)

        tk.Label(form, text="模型").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        self.model_var = tk.StringVar(value=APIYI_MODELS[0])
        self.model_box = ttk.Combobox(form, textvariable=self.model_var, values=APIYI_MODELS, state="readonly", width=36)
        self.model_box.grid(row=2, column=1, sticky="w", padx=8)

        tk.Label(form, text="备注/标签").grid(row=3, column=0, sticky="w", padx=8, pady=6)
        self.note_var = tk.StringVar(value="")
        tk.Entry(form, textvariable=self.note_var, width=88).grid(row=3, column=1, sticky="we", padx=8)

        tk.Label(form, text="提示词").grid(row=4, column=0, sticky="nw", padx=8, pady=6)
        self.prompt_text = tk.Text(form, height=4, width=88, wrap="word")
        self.prompt_text.grid(row=4, column=1, sticky="we", padx=8)
        self.prompt_text.insert("1.0", "让这个场景动起来，添加生动的细节")

        tk.Label(form, text="图片").grid(row=5, column=0, sticky="w", padx=8, pady=6)
        self.image_path_var = tk.StringVar(value="")
        tk.Entry(form, textvariable=self.image_path_var, width=88).grid(row=5, column=1, sticky="we", padx=8)
        tk.Button(form, text="选择图片", command=self.pick_image, width=12).grid(row=5, column=2, sticky="w")

        tk.Label(form, text="APIYI Key（管理员保存用）").grid(row=6, column=0, sticky="w", padx=8, pady=6)
        self.apiyi_key_var = tk.StringVar(value="")
        tk.Entry(form, textvariable=self.apiyi_key_var, width=88, show="*").grid(row=6, column=1, sticky="we", padx=8)

        tk.Label(form, text="XINTIAN Key（管理员保存用）").grid(row=7, column=0, sticky="w", padx=8, pady=6)
        self.xintian_key_var = tk.StringVar(value="")
        tk.Entry(form, textvariable=self.xintian_key_var, width=88, show="*").grid(row=7, column=1, sticky="we", padx=8)

        form.grid_columnconfigure(1, weight=1)

        # ======= 控制区（按钮 + 并发）
        ctrl = tk.Frame(root)
        ctrl.pack(fill="x", padx=12, pady=8)

        tk.Label(ctrl, text="并发数").pack(side="left")
        tk.Spinbox(ctrl, from_=1, to=10, width=4, textvariable=self.max_workers_var).pack(side="left", padx=6)
        tk.Button(ctrl, text="应用并发数", command=self.apply_concurrency, width=10).pack(side="left", padx=6)

        tk.Button(ctrl, text="添加任务并开始", command=self.add_task_and_start, height=2, width=16).pack(side="left", padx=10)
        tk.Button(ctrl, text="重复执行选中任务", command=self.repeat_selected_task, height=2, width=16).pack(side="left", padx=6)
        tk.Button(ctrl, text="停止选中任务", command=self.stop_selected_task, height=2, width=14).pack(side="left", padx=6)
        tk.Button(ctrl, text="停止全部任务", command=self.stop_all_tasks, height=2, width=14).pack(side="left", padx=6)

        self.open_btn = tk.Button(ctrl, text="打开选中链接", command=self.open_selected_video, height=2, width=14, state="disabled")
        self.open_btn.pack(side="left", padx=6)

        tk.Button(ctrl, text="打开日志文件夹", command=self.open_log_dir, height=2, width=14).pack(side="left", padx=6)

        tk.Button(ctrl, text="导出任务…", command=self.export_tasks, height=2, width=12).pack(side="left", padx=6)
        tk.Button(ctrl, text="导入任务…", command=self.import_tasks, height=2, width=12).pack(side="left", padx=6)

        tk.Button(ctrl, text="批量下载并归档(按group)", command=self.open_download_dialog, height=2, width=20).pack(side="left", padx=6)
        tk.Button(ctrl, text="重试全部失败(筛选范围)", command=self.retry_failed_in_filtered, height=2, width=18).pack(side="left", padx=6)

        tk.Button(ctrl, text="清空任务记忆（慎用）", command=self.clear_task_memory, height=2, width=16).pack(side="left", padx=6)

        self.status_var = tk.StringVar(value="状态：未开始（请先加载配置）")
        tk.Label(ctrl, textvariable=self.status_var, fg="#555").pack(side="right")

        # ======= 任务列表（含筛选/搜索）
        table_frame = tk.LabelFrame(root, text="📋 任务列表（自动恢复｜搜索/筛选｜颜色标记｜group=归档目录）")
        table_frame.pack(fill="both", expand=False, padx=12, pady=8)

        # ✅ 筛选栏
        filter_bar = ttk.Frame(table_frame)
        filter_bar.pack(fill="x", padx=8, pady=(6, 2))

        ttk.Label(filter_bar, text="搜索").pack(side="left")
        self.search_var = tk.StringVar(value="")
        search_entry = ttk.Entry(filter_bar, textvariable=self.search_var, width=28)
        search_entry.pack(side="left", padx=6)
        search_entry.bind("<KeyRelease>", lambda e: self.apply_filter())

        ttk.Label(filter_bar, text="Provider").pack(side="left", padx=(12, 0))
        self.filter_provider_var = tk.StringVar(value="全部")
        provider_box = ttk.Combobox(filter_bar, textvariable=self.filter_provider_var,
                                    values=["全部", "AUTO", "APIYI", "XINTIAN"], state="readonly", width=10)
        provider_box.pack(side="left", padx=6)
        provider_box.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())

        ttk.Label(filter_bar, text="状态").pack(side="left", padx=(12, 0))
        self.filter_status_var = tk.StringVar(value="全部")
        status_box = ttk.Combobox(filter_bar, textvariable=self.filter_status_var,
                                  values=["全部", "Queued", "Running", "Success", "Failed", "Stopped", "Done(No URL)"],
                                  state="readonly", width=12)
        status_box.pack(side="left", padx=6)
        status_box.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())

        ttk.Button(filter_bar, text="清空筛选", command=self.clear_filter).pack(side="right")

        cols = ("task_id", "provider", "created_at", "group", "note", "status", "progress", "model", "image", "video_url")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=10)
        self.tree.pack(fill="both", expand=True, padx=8, pady=6)

        headings = [
            ("task_id", "Task ID", 90),
            ("provider", "Provider", 110),
            ("created_at", "创建时间", 150),
            ("group", "Group(归档目录)", 150),
            ("note", "备注/标签", 200),
            ("status", "状态", 120),
            ("progress", "进度%", 80),
            ("model", "模型", 210),
            ("image", "图片文件", 150),
            ("video_url", "视频链接（如有）", 360),
        ]
        for c, name, w in headings:
            self.tree.heading(c, text=name)
            self.tree.column(c, width=w, anchor="w")

        self.tree.tag_configure("success", background="#e8f7ee")
        self.tree.tag_configure("failed",  background="#fde8e8")
        self.tree.tag_configure("running", background="#fff6d6")
        self.tree.tag_configure("stopped", background="#eeeeee")
        self.tree.tag_configure("queued",  background="#eef5ff")

        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_select_task())

        # 输出区
        out_frame = tk.LabelFrame(root, text="🧾 输出（并发日志会带 [Txxxx] 前缀）")
        out_frame.pack(fill="both", expand=True, padx=12, pady=10)

        self.output = tk.Text(out_frame, wrap="word")
        self.output.pack(fill="both", expand=True)
        scroll = tk.Scrollbar(self.output, command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

        # 右键菜单
        self.task_menu = tk.Menu(root, tearoff=0)
        self.task_menu.add_command(label="回填任务参数", command=self.on_select_task)
        self.task_menu.add_command(label="重复执行", command=self.repeat_selected_task)
        self.task_menu.add_command(label="停止任务", command=self.stop_selected_task)
        self.task_menu.add_separator()
        self.task_menu.add_command(label="打开链接", command=self.open_selected_video)
        self.task_menu.add_command(label="打开日志文件夹", command=self.open_log_dir)
        self.task_menu.add_separator()
        self.task_menu.add_command(label="批量下载并归档(按group)", command=self.open_download_dialog)
        self.task_menu.add_command(label="重试全部失败(筛选范围)", command=self.retry_failed_in_filtered)

        def popup(e):
            try:
                self.task_menu.tk_popup(e.x_root, e.y_root)
            finally:
                self.task_menu.grab_release()

        self.tree.bind("<Button-3>", popup)

        self.apply_concurrency()
        self.on_provider_change()

        # ✅ 启动自动恢复任务
        self.load_tasks_store()
        self.apply_filter()

        if Path(CONFIG_FILE).exists():
            self.log_global("ℹ️ 检测到 config.enc：输入口令点击【加载配置】即可执行任务。\n\n")
        else:
            self.log_global("ℹ️ 未检测到 config.enc：管理员先【保存加密配置】。\n\n")

    # =========================
    # Status -> tree tag
    # =========================
    def status_to_tag(self, status: str) -> str:
        s = (status or "").lower()
        if "success" in s:
            return "success"
        if "fail" in s:
            return "failed"
        if "running" in s:
            return "running"
        if "stop" in s:
            return "stopped"
        if "queue" in s:
            return "queued"
        return ""

    # =========================
    # Task memory (persist) - throttled
    # =========================
    def mark_dirty(self):
        self._dirty = True
        if self._save_timer is None:
            self._save_timer = self.root.after(800, self._flush_save)

    def _flush_save(self):
        self._save_timer = None
        if not self._dirty:
            return
        self._dirty = False
        self.save_tasks_store()

    def request_refresh(self):
        if self._refresh_timer is None:
            self._refresh_timer = self.root.after(200, self._flush_refresh)

    def _flush_refresh(self):
        self._refresh_timer = None
        self.apply_filter()

    def load_tasks_store(self):
        p = Path(TASKS_STORE_FILE)
        if not p.exists():
            self.log_global("ℹ️ 未发现 tasks.json（首次使用正常）。\n")
            return

        try:
            obj = safe_json_loads(p.read_text(encoding="utf-8")) or {}
            items = obj.get("tasks", [])
            if not isinstance(items, list):
                items = []

            restored = 0
            max_id_num = 0

            for d in items:
                try:
                    t = TaskItem.from_persist_dict(d)
                    if not t.task_id:
                        continue
                    m = re.match(r"T(\d+)", t.task_id)
                    if m:
                        max_id_num = max(max_id_num, int(m.group(1)))
                    # Running 恢复时：统一变为 Queued（避免假Running）
                    if t.status == "Running":
                        t.status = "Queued"
                        t.progress = 0.0
                    self.tasks[t.task_id] = t
                    restored += 1
                except Exception:
                    continue

            self.task_counter = itertools.count(max_id_num + 1)

            self.log_global(f"✅ 已恢复历史任务：{restored} 条（来自 tasks.json）\n\n")
            self.set_status(f"已恢复 {restored} 条历史任务")
        except Exception as e:
            self.log_global(f"⚠️ 读取 tasks.json 失败：{e}\n")

    def save_tasks_store(self):
        try:
            tasks_list = [t.to_persist_dict() for t in self.tasks.values()]
            if len(tasks_list) > TASKS_STORE_MAX:
                tasks_list = tasks_list[-TASKS_STORE_MAX:]

            payload = {
                "version": 2,
                "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "tasks": tasks_list,
            }
            Path(TASKS_STORE_FILE).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def clear_task_memory(self):
        if messagebox.askyesno("确认", "确定要清空任务记忆吗？\n这会删除 tasks.json，并清空任务列表（不可恢复）。"):
            try:
                Path(TASKS_STORE_FILE).unlink(missing_ok=True)
            except Exception:
                pass
            self.tasks.clear()
            self.apply_filter()
            self.log_global("🗑️ 已清空任务记忆（tasks.json）与当前任务列表。\n")
            self.set_status("已清空任务记忆")

    # =========================
    # Export / Import tasks
    # =========================
    def export_tasks(self):
        default_name = f"tasks_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path = filedialog.asksaveasfilename(
            title="导出任务",
            defaultextension=".json",
            initialfile=default_name,
            filetypes=[("JSON", "*.json")]
        )
        if not path:
            return
        try:
            tasks_list = [t.to_persist_dict() for t in self.tasks.values()]
            payload = {
                "version": 2,
                "exported_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "tasks": tasks_list,
            }
            Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            messagebox.showinfo("成功", f"已导出 {len(tasks_list)} 条任务：\n{path}")
        except Exception as e:
            messagebox.showerror("错误", f"导出失败：{e}")

    def import_tasks(self):
        path = filedialog.askopenfilename(
            title="导入任务",
            filetypes=[("JSON", "*.json"), ("All Files", "*.*")]
        )
        if not path:
            return
        try:
            obj = safe_json_loads(Path(path).read_text(encoding="utf-8")) or {}
            items = obj.get("tasks", [])
            if not isinstance(items, list) or not items:
                messagebox.showwarning("提示", "该文件里没有 tasks 列表或为空。")
                return

            overwrite = messagebox.askyesno("导入选项", "遇到相同 Task ID，是否覆盖原任务？\n是=覆盖 / 否=跳过")

            imported = 0
            skipped = 0
            max_id_num = 0

            for d in items:
                t = TaskItem.from_persist_dict(d)
                if not t.task_id:
                    continue

                m = re.match(r"T(\d+)", t.task_id)
                if m:
                    max_id_num = max(max_id_num, int(m.group(1)))

                if t.task_id in self.tasks and not overwrite:
                    skipped += 1
                    continue

                t.stop_event = threading.Event()
                t.future = None
                self.tasks[t.task_id] = t
                imported += 1

            # 更新 counter
            for tid in self.tasks.keys():
                m = re.match(r"T(\d+)", tid)
                if m:
                    max_id_num = max(max_id_num, int(m.group(1)))
            self.task_counter = itertools.count(max_id_num + 1)

            self.mark_dirty()
            self.apply_filter()
            messagebox.showinfo("完成", f"导入成功：{imported} 条\n跳过：{skipped} 条")
        except Exception as e:
            messagebox.showerror("错误", f"导入失败：{e}")

    # =========================
    # Concurrency (safe executor lifecycle)
    # =========================
    def apply_concurrency(self):
        n = int(self.max_workers_var.get())
        old = self.executor
        if old is not None:
            try:
                old.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        self.executor = ThreadPoolExecutor(max_workers=n)
        self.set_status(f"并发数已设置为 {n}")

    # =========================
    # Provider change
    # =========================
    def on_provider_change(self):
        pkey = self.provider_label_to_key.get(self.provider_var.get(), "auto")
        if pkey == "apiyi":
            self.base_url_var.set(APIYI_DEFAULT_BASE)
            self.model_box["values"] = APIYI_MODELS
            if self.model_var.get() not in APIYI_MODELS:
                self.model_var.set(APIYI_MODELS[0])
        elif pkey == "xintian":
            self.base_url_var.set(XINTIAN_DEFAULT_BASE)
            self.model_box["values"] = XINTIAN_MODELS
            if self.model_var.get() not in XINTIAN_MODELS:
                self.model_var.set(XINTIAN_MODELS[0])
        else:
            # AUTO：允许选任意模型（两边合并）
            self.base_url_var.set(XINTIAN_DEFAULT_BASE)  # 默认显示 xintian base，实际会自动选择
            merged = sorted(set(APIYI_MODELS + XINTIAN_MODELS))
            self.model_box["values"] = merged
            if self.model_var.get() not in merged:
                self.model_var.set("sora-2-portrait-15s" if "sora-2-portrait-15s" in merged else merged[0])

    # =========================
    # Logging
    # =========================
    def log_global(self, text: str):
        def _append():
            self.output.insert("end", text)
            self.output.see("end")
        self.root.after(0, _append)

    def log_task(self, task_id: str, text: str):
        prefix = f"[{task_id}] "
        out = (("\n" + prefix + text.lstrip("\n")) if text.startswith("\n") else (prefix + text))
        self.log_global(out)

        t = self.tasks.get(task_id)
        if t and t.log_file:
            try:
                with open(t.log_file, "a", encoding="utf-8") as f:
                    f.write(out)
            except Exception:
                pass

    # =========================
    # UI helpers
    # =========================
    def set_status(self, text: str):
        self.root.after(0, lambda: self.status_var.set(f"状态：{text}"))

    def copy_to_clipboard(self, text: str):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.set_status("已复制机器指纹")
        except Exception:
            pass

    def open_log_dir(self):
        try:
            os.startfile(str(Path(LOG_DIR).resolve()))
        except Exception:
            messagebox.showinfo("提示", f"日志目录：{Path(LOG_DIR).resolve()}")

    def pick_image(self):
        path = filedialog.askopenfilename(
            title="选择图片",
            filetypes=[("Image Files", "*.jpg *.jpeg *.png *.webp"), ("All Files", "*.*")],
        )
        if path:
            self.image_path_var.set(path)
            self.set_status("已选择图片")

    def pick_mission_root(self):
        folder = filedialog.askdirectory(title="选择 Mission 根目录")
        if folder:
            self.mission_root_var.set(folder)
            self.set_status("已选择 Mission 根目录")

    def pick_download_root(self):
        folder = filedialog.askdirectory(title="选择 Download 归档根目录")
        if folder:
            self.download_root_var.set(folder)
            self.set_status("已选择 Download 根目录")

    def provider_name(self, provider: str) -> str:
        if provider == "apiyi":
            return "APIYI"
        if provider == "xintian":
            return "XINTIAN"
        return "AUTO"

    def get_selected_task_id(self) -> str | None:
        items = self.tree.selection()
        if not items:
            return None
        return self.tree.item(items[0], "values")[0]

    # =========================
    # Filter/Search
    # =========================
    def clear_filter(self):
        self.search_var.set("")
        self.filter_provider_var.set("全部")
        self.filter_status_var.set("全部")
        self.apply_filter()

    def apply_filter(self):
        kw = (self.search_var.get() or "").strip().lower()
        pfilter = self.filter_provider_var.get()
        sfilter = self.filter_status_var.get()

        for iid in self.tree.get_children():
            self.tree.delete(iid)

        def provider_disp(t: TaskItem) -> str:
            return self.provider_name(t.provider)

        def sort_key(t: TaskItem):
            return (t.created_at or "", t.task_id or "")

        for t in sorted(self.tasks.values(), key=sort_key):
            if pfilter != "全部":
                if provider_disp(t) != pfilter:
                    continue

            if sfilter != "全部":
                if (t.status or "") != sfilter:
                    continue

            if kw:
                hay = " ".join([
                    t.task_id or "",
                    provider_disp(t),
                    t.status or "",
                    t.group or "",
                    t.note or "",
                    t.model or "",
                    Path(t.image_path).name if t.image_path else "",
                    t.video_url or "",
                ]).lower()
                if kw not in hay:
                    continue

            self.add_tree_row(t)

    def add_tree_row(self, task: TaskItem):
        tag = self.status_to_tag(task.status)
        self.tree.insert(
            "",
            "end",
            values=(
                task.task_id,
                self.provider_name(task.provider),
                task.created_at,
                task.group,
                task.note,
                task.status,
                f"{task.progress:.1f}",
                task.model,
                Path(task.image_path).name if task.image_path else "",
                task.video_url or "",
            ),
            tags=(tag,) if tag else ()
        )

    # =========================
    # Selection -> autofill
    # =========================
    def on_select_task(self):
        task_id = self.get_selected_task_id()
        if not task_id:
            self.open_btn.config(state="disabled")
            return
        t = self.tasks.get(task_id)
        if not t:
            self.open_btn.config(state="disabled")
            return

        # 回填 provider
        if t.provider == "apiyi":
            self.provider_var.set(PROVIDERS[1][0])
        elif t.provider == "xintian":
            self.provider_var.set(PROVIDERS[2][0])
        else:
            self.provider_var.set(PROVIDERS[0][0])
        self.on_provider_change()

        # base_url 展示：按当前 provider 显示；AUTO 时仍显示 XINTIAN 默认（不影响真实选择）
        self.base_url_var.set(t.base_url)
        self.model_var.set(t.model)
        self.image_path_var.set(t.image_path)
        self.note_var.set(t.note)
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", t.prompt)

        self.open_btn.config(state="normal" if t.video_url else "disabled")
        self.set_status(f"已回填 {task_id} 参数")

    # =========================
    # Config
    # =========================
    def save_config(self):
        passphrase = self.passphrase_var.get().strip()
        if not passphrase:
            messagebox.showerror("错误", "内部口令不能为空（用于加密/解密）")
            return

        apiyi_key = self.apiyi_key_var.get().strip()
        xintian_key = self.xintian_key_var.get().strip()

        if not apiyi_key and not xintian_key:
            messagebox.showerror("错误", "请至少填写一套 API Key（APIYI 或 XINTIAN）")
            return

        try:
            cfg = {
                "apiyi_key": apiyi_key,
                "apiyi_base": APIYI_DEFAULT_BASE,
                "xintian_key": xintian_key,
                "xintian_base": XINTIAN_DEFAULT_BASE,
            }
            save_encrypted_config(passphrase, cfg)

            self.apiyi_key = apiyi_key
            self.xintian_key = xintian_key
            self.apiyi_key_var.set("")
            self.xintian_key_var.set("")

            self.set_status("已保存加密配置")
            self.log_global("✅ 已保存加密配置到 config.enc（两套 Key 都会加密保存）\n\n")
            messagebox.showinfo("成功", "已保存加密配置（config.enc）。")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败：{e}")

    def load_config(self):
        passphrase = self.passphrase_var.get().strip()
        if not passphrase:
            messagebox.showerror("错误", "请输入内部口令（用于解密 config.enc）")
            return
        try:
            cfg = load_encrypted_config(passphrase)
            if not cfg:
                messagebox.showerror("错误", "未找到 config.enc，请让管理员先保存加密配置")
                return

            self.apiyi_key = (cfg.get("apiyi_key") or "").strip()
            self.xintian_key = (cfg.get("xintian_key") or "").strip()

            if not self.apiyi_key and not self.xintian_key:
                messagebox.showerror("错误", "配置解密成功但两套 API Key 都为空")
                return

            self.apiyi_key_var.set("")
            self.xintian_key_var.set("")
            self.set_status("配置加载成功")
            self.log_global("✅ 配置加载成功（API Key 已解密到内存，界面不显示）\n\n")
            messagebox.showinfo("成功", "配置加载成功！现在可以执行/批量执行任务。")
        except Exception:
            messagebox.showerror("错误", "口令错误或配置文件损坏，无法解密。")

    # =========================
    # Mission scan -> create tasks
    # =========================
    def scan_mission_and_create_tasks(self):
        root_dir = Path(self.mission_root_var.get().strip())
        if not root_dir.exists() or not root_dir.is_dir():
            messagebox.showerror("错误", "Mission 根目录无效，请重新选择。")
            return

        n_each = int(self.mission_n_var.get())
        total_dirs = 0
        created = 0
        skipped = 0

        self.log_global(f"\n🔎 开始扫描 Mission：{root_dir}\n")

        # 子目录名：数字为主，但也允许非数字（你举例是数字）
        subdirs = [p for p in root_dir.iterdir() if p.is_dir()]
        subdirs.sort(key=lambda p: p.name)

        for sd in subdirs:
            total_dirs += 1
            group_name = sd.name.strip()
            # 找 txt
            txts = list(sd.glob("*.txt"))
            if not txts:
                self.log_global(f"⚠️ 跳过 {group_name}：未找到 txt 提示词\n")
                skipped += 1
                continue
            txt_path = txts[0]
            prompt = read_text_safely(txt_path).strip()
            if not prompt:
                self.log_global(f"⚠️ 跳过 {group_name}：txt 为空\n")
                skipped += 1
                continue

            # 找图片（多格式）
            img_path = None
            for ext in SUPPORTED_IMAGE_EXTS:
                imgs = list(sd.glob(f"*{ext}"))
                if imgs:
                    img_path = imgs[0]
                    break
            if img_path is None:
                # 再兜底：目录里找任意图片后缀
                all_files = list(sd.iterdir())
                for f in all_files:
                    if f.is_file() and f.suffix.lower() in SUPPORTED_IMAGE_EXTS:
                        img_path = f
                        break
            if img_path is None or not img_path.exists():
                self.log_global(f"⚠️ 跳过 {group_name}：未找到可用图片（支持 {SUPPORTED_IMAGE_EXTS}）\n")
                skipped += 1
                continue

            # 生成 n_each 任务
            for i in range(1, n_each + 1):
                note = group_name  # 子目录名作为备注/标签
                # 为了避免 5 个任务完全一致导致生成重复，给 prompt 加轻微扰动（不影响主语义）
                varied_prompt = prompt + f"\n\n[variation:{i}] keep content consistent, vary camera angle and micro-actions only."
                provider = "auto"  # ✅ 你要求自动
                model = (self.model_var.get() or "").strip()
                # base_url 只是展示字段；实际 AUTO 会在执行时按 provider 决定
                base_url = self.base_url_var.get().strip() or XINTIAN_DEFAULT_BASE

                t = self._create_task_from_params(
                    provider=provider,
                    base_url=base_url,
                    model=model,
                    prompt=varied_prompt,
                    image_path=str(img_path),
                    note=note,
                    group=group_name
                )
                self._submit_task(t, autostart=False)
                created += 1

        self.mark_dirty()
        self.request_refresh()
        self.set_status(f"Mission 扫描完成：目录 {total_dirs} | 创建任务 {created} | 跳过 {skipped}")
        self.log_global(f"✅ Mission 扫描完成：目录 {total_dirs} | 创建任务 {created} | 跳过 {skipped}\n\n")

        # 是否立刻开始？
        if created > 0 and messagebox.askyesno("开始执行？", f"已生成 {created} 个任务，是否立刻开始队列？"):
            self.start_all_queued()

    # =========================
    # Create & run tasks
    # =========================
    def _create_task_from_params(self, provider: str, base_url: str, model: str, prompt: str, image_path: str, note: str, group: str) -> TaskItem:
        idx = next(self.task_counter)
        task_id = f"T{idx:04d}"
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        log_file = str(Path(LOG_DIR) / f"{ts}_{task_id}.log")
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("=== 图生视频任务日志 ===\n")
            f.write(f"task_id: {task_id}\n")
            f.write(f"created_at: {created_at}\n")
            f.write(f"provider: {provider}\n")
            f.write(f"group: {group}\n")
            f.write(f"note: {note}\n")
            f.write(f"base_url: {base_url}\n")
            f.write(f"model: {model}\n")
            f.write(f"image: {image_path}\n")
            f.write("=" * 70 + "\n\n")

        t = TaskItem(
            task_id=task_id,
            provider=provider,
            base_url=base_url,
            model=model,
            prompt=prompt,
            image_path=image_path,
            note=note.strip(),
            group=group.strip() or (note.strip() or "Ungrouped"),
            log_file=log_file,
            created_at=created_at,
            status="Queued",
            progress=0.0,
            video_url=None,
        )
        t.stop_event = threading.Event()
        return t

    def _submit_task(self, task: TaskItem, autostart: bool = True):
        self.tasks[task.task_id] = task
        self.mark_dirty()
        self.request_refresh()

        if autostart:
            self._run_task_async(task)

    def _run_task_async(self, task: TaskItem):
        def run_one():
            task.status = "Running"
            task.progress = 0.0
            self.mark_dirty()
            self.request_refresh()

            self.log_task(task.task_id, f"📁 group：{task.group}\n")
            self.log_task(task.task_id, f"🏷️ 备注：{task.note}\n" if task.note else "")
            self.log_task(task.task_id, f"🔧 Provider: {task.provider}\n")

            def on_text(t: str):
                self.log_task(task.task_id, t)
                p = parse_progress_percent(t)
                if p is not None and task.status == "Running":
                    task.progress = p
                    self.mark_dirty()
                    self.request_refresh()

            def on_done(video_url: str | None):
                task.video_url = video_url
                if task.stop_event and task.stop_event.is_set():
                    task.status = "Stopped"
                else:
                    task.status = "Success" if video_url else "Done(No URL)"
                if task.status in ("Success", "Done(No URL)"):
                    task.progress = max(task.progress, 100.0)

                self.log_task(task.task_id, f"\n🧾 本次日志已保存：{task.log_file}\n")
                self.mark_dirty()
                self.request_refresh()
                self.root.after(0, self.on_select_task)

            def friendly_provider_error(raw: str) -> str:
                # 更“人话”
                low = (raw or "").lower()
                if "model_not_found" in low or "distributor" in low:
                    return "模型当前无可用渠道（服务商侧问题）。建议换模型/稍后重试，或用 AUTO 让程序自动切换渠道。"
                if "401" in low or "unauthorized" in low:
                    return "鉴权失败：API Key 无效或过期。"
                if "403" in low:
                    return "权限不足：该 Key 无权访问该模型/接口。"
                if "429" in low:
                    return "触发限流：请降低并发/稍后重试。"
                if "503" in low:
                    return "服务不可用(503)：服务商渠道波动，建议稍后重试或用 AUTO 切换渠道。"
                if "timeout" in low:
                    return "请求超时：网络波动或服务端慢。建议稍后重试。"
                return raw

            def on_error(err: str):
                task.status = "Failed"
                self.log_task(task.task_id, f"\n❌ 出错：{friendly_provider_error(err)}\n")
                self.log_task(task.task_id, f"🧾 本次日志已保存：{task.log_file}\n")
                self.mark_dirty()
                self.request_refresh()
                self.root.after(0, self.on_select_task)

            # ✅ AUTO Provider：自动选择可用渠道
            chosen_provider, chosen_model, chosen_base = self.resolve_auto_provider(task)

            # 执行
            if chosen_provider == "apiyi":
                if not self.apiyi_key:
                    on_error("未加载 APIYI Key（请加载配置或让管理员保存 APIYI Key）")
                    return
                cfg = ApiyiConfig(
                    api_key=self.apiyi_key,
                    base_url=chosen_base,
                    model=chosen_model,
                    prompt=task.prompt,
                    image_path=task.image_path,
                )
                run_apiyi_sse(cfg, on_text, on_done, on_error, task.stop_event or threading.Event())
            else:
                if not self.xintian_key:
                    on_error("未加载 XINTIAN Key（请加载配置或让管理员保存 XINTIAN Key）")
                    return
                cfg = XintianConfig(
                    api_key=self.xintian_key,
                    base_url=chosen_base,
                    model=chosen_model,
                    prompt=task.prompt,
                    image_path=task.image_path,
                    poll_interval_sec=3,
                )
                run_xintian_upload_and_poll(cfg, on_text, on_done, on_error, task.stop_event or threading.Event())

        if not self.executor:
            self.apply_concurrency()
        task.future = self.executor.submit(run_one)

    def resolve_auto_provider(self, task: TaskItem) -> tuple[str, str, str]:
        """
        AUTO 策略：
        1) 如果 task.provider 明确是 apiyi/xintian，则照做
        2) 如果是 auto：
           - 如果 model 属于某 provider 的模型列表，并且对应 key 存在，则优先该 provider
           - 否则：优先选择“有 key 的 provider”，并选一个最接近的模型
        """
        # base url：按 provider 走各自 default（避免用户填错）
        def base_for(p: str) -> str:
            return APIYI_DEFAULT_BASE if p == "apiyi" else XINTIAN_DEFAULT_BASE

        if task.provider in ("apiyi", "xintian"):
            p = task.provider
            return p, task.model, normalize_base_url(task.base_url) or base_for(p)

        model = (task.model or "").strip()

        apiyi_ok = bool(self.apiyi_key)
        xintian_ok = bool(self.xintian_key)

        # model 归属判断
        model_is_apiyi = model in APIYI_MODELS
        model_is_xintian = model in XINTIAN_MODELS

        if model_is_xintian and xintian_ok:
            return "xintian", model, base_for("xintian")
        if model_is_apiyi and apiyi_ok:
            return "apiyi", model, base_for("apiyi")

        # fallback：优先有 key 的 provider
        if xintian_ok:
            # 若用户填了 api模型，则给 xintian 找一个“最接近”的默认
            fallback_model = "sora-2-portrait-15s" if "portrait" in model else "sora-2-landscape-15s"
            if fallback_model not in XINTIAN_MODELS:
                fallback_model = XINTIAN_MODELS[0]
            return "xintian", fallback_model, base_for("xintian")

        # 再退：apiyi
        if apiyi_ok:
            # 若用户填了 xintian 模型，则选 api 的 15s 或 pro
            fallback_model = "sora-2-pro" if "sora-2-pro" in APIYI_MODELS else APIYI_MODELS[0]
            return "apiyi", fallback_model, base_for("apiyi")

        # 都没有 key：保持原样（后面会提示）
        return "xintian", model, base_for("xintian")

    # =========================
    # Actions
    # =========================
    def add_task_and_start(self):
        if not self.apiyi_key and not self.xintian_key:
            messagebox.showwarning("提示", "请先加载配置（至少包含一套 API Key）。")
            return

        provider = self.provider_label_to_key.get(self.provider_var.get(), "auto")
        base_url = self.base_url_var.get().strip()
        model = self.model_var.get().strip()
        note = self.note_var.get().strip()
        prompt = self.prompt_text.get("1.0", "end").strip()
        image_path = self.image_path_var.get().strip()

        if not image_path or not Path(image_path).exists():
            messagebox.showwarning("提示", "请选择有效图片文件。")
            return
        if not prompt:
            messagebox.showwarning("提示", "请填写提示词。")
            return

        # provider 校验：AUTO 允许任何模型；明确 provider 则要求匹配列表
        if provider == "apiyi":
            if model not in APIYI_MODELS:
                messagebox.showwarning("提示", "APIYI Provider 请选择其模型列表中的模型。")
                return
            if not self.apiyi_key:
                messagebox.showwarning("提示", "未加载 APIYI Key。")
                return
        elif provider == "xintian":
            if model not in XINTIAN_MODELS:
                messagebox.showwarning("提示", "XINTIAN Provider 请选择其模型列表中的模型。")
                return
            if not self.xintian_key:
                messagebox.showwarning("提示", "未加载 XINTIAN Key。")
                return
        else:
            if not (self.apiyi_key or self.xintian_key):
                messagebox.showwarning("提示", "AUTO 需要至少加载一套 Key。")
                return

        group = note or "Ungrouped"
        task = self._create_task_from_params(provider, base_url, model, prompt, image_path, note, group)
        self._submit_task(task, autostart=True)
        self.set_status(f"已添加并开始：{task.task_id}")

    def start_all_queued(self):
        # 将所有 Queued 启动
        started = 0
        for t in self.tasks.values():
            if t.status == "Queued":
                self._run_task_async(t)
                started += 1
        self.set_status(f"已启动队列：{started} 个任务")
        self.log_global(f"\n🚀 已启动队列：{started} 个 Queued 任务\n\n")

    def repeat_selected_task(self):
        sel = self.get_selected_task_id()
        if not sel:
            messagebox.showinfo("提示", "请先选中一条任务。")
            return
        t = self.tasks.get(sel)
        if not t:
            return
        if not t.image_path or not Path(t.image_path).exists():
            messagebox.showerror("错误", "原任务图片路径不存在（可能被移动/删除），请重新选择图片。")
            return

        new_task = self._create_task_from_params(t.provider, t.base_url, t.model, t.prompt, t.image_path, t.note, t.group)
        self._submit_task(new_task, autostart=True)
        self.set_status(f"重复执行 {sel} → 新任务 {new_task.task_id}")

    def stop_selected_task(self):
        sel = self.get_selected_task_id()
        if not sel:
            messagebox.showinfo("提示", "请先选中一条任务。")
            return
        t = self.tasks.get(sel)
        if not t:
            return
        if not t.stop_event:
            t.stop_event = threading.Event()
        t.stop_event.set()
        t.status = "Stopped"
        self.log_task(sel, "\n🟨 已请求停止该任务...\n")
        self.mark_dirty()
        self.request_refresh()
        self.set_status(f"已请求停止 {sel}")

    def stop_all_tasks(self):
        for t in self.tasks.values():
            if not t.stop_event:
                t.stop_event = threading.Event()
            t.stop_event.set()
            if t.status == "Running":
                t.status = "Stopped"
        self.log_global("\n🟨 已请求停止全部任务...\n")
        self.mark_dirty()
        self.request_refresh()
        self.set_status("已请求停止全部任务")

    def open_selected_video(self):
        sel = self.get_selected_task_id()
        if not sel:
            messagebox.showinfo("提示", "请先选中一条任务。")
            return
        t = self.tasks.get(sel)
        if not t or not t.video_url:
            messagebox.showinfo("提示", "该任务还没有可打开的视频链接。")
            return
        webbrowser.open(t.video_url)

    # =========================
    # Batch retry failed in filtered scope
    # =========================
    def _get_task_ids_from_tree_filtered(self) -> list[str]:
        ids = []
        for iid in self.tree.get_children():
            vals = self.tree.item(iid, "values")
            if vals and vals[0]:
                ids.append(vals[0])
        return ids

    def retry_failed_in_filtered(self):
        ids = self._get_task_ids_from_tree_filtered()
        targets = []
        for tid in ids:
            t = self.tasks.get(tid)
            if t and t.status == "Failed":
                targets.append(t)
        if not targets:
            messagebox.showinfo("提示", "当前筛选范围内没有 Failed 任务。")
            return
        if not messagebox.askyesno("确认", f"将重试 {len(targets)} 个 Failed 任务，继续？"):
            return
        for t in targets:
            t.status = "Queued"
            t.progress = 0.0
            t.stop_event = threading.Event()
        self.mark_dirty()
        self.request_refresh()
        self.start_all_queued()

    # =========================
    # Download (archive by group)
    # =========================
    def _ensure_download_root(self) -> Path:
        p = Path(self.download_root_var.get().strip())
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _download_index_path(self) -> Path:
        root = self._ensure_download_root()
        return root / DOWNLOAD_INDEX_FILE

    def _load_download_index(self) -> dict:
        p = self._download_index_path()
        if not p.exists():
            return {"by_url": {}, "by_sha256": {}}
        try:
            obj = safe_json_loads(p.read_text(encoding="utf-8")) or {}
            obj.setdefault("by_url", {})
            obj.setdefault("by_sha256", {})
            return obj
        except Exception:
            return {"by_url": {}, "by_sha256": {}}

    def _save_download_index(self, index: dict):
        p = self._download_index_path()
        p.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    def _guess_ext_from_url(self, url: str) -> str:
        try:
            path = urlparse(url).path
            name = Path(path).name
            if "." in name:
                ext = "." + name.split(".")[-1].lower()
                if ext in [".mp4", ".mov", ".m4v", ".webm"]:
                    return ext
        except Exception:
            pass
        return ".mp4"

    def _filename_from_task(self, t: TaskItem, ext: str) -> str:
        ts = (t.created_at or "").replace(":", "-").replace(" ", "_")
        note = safe_filename(t.note) if t.note else ""
        model = safe_filename(t.model) if t.model else ""
        parts = [ts, t.task_id, model]
        if note:
            parts.append(note)
        base = "_".join([p for p in parts if p])
        return safe_filename(base) + ext

    def _download_one_with_progress(self, url: str, target_path: Path, progress_cb, stop_cb) -> tuple[bool, str, str]:
        tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
        h = hashlib.sha256()

        with requests.get(url, stream=True, timeout=(20, 600)) as r:
            r.raise_for_status()
            total = r.headers.get("Content-Length")
            total_bytes = int(total) if total and total.isdigit() else None

            downloaded = 0
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if stop_cb():
                        raise RuntimeError("用户已取消下载")
                    if not chunk:
                        continue
                    f.write(chunk)
                    h.update(chunk)
                    downloaded += len(chunk)
                    progress_cb(downloaded, total_bytes)

        sha = h.hexdigest()
        tmp_path.replace(target_path)
        return True, sha, str(target_path)

    def _download_with_retries(self, url: str, target_path: Path, retries: int, progress_cb, stop_cb, log_cb) -> tuple[bool, str, str]:
        last_err = None
        for attempt in range(1, retries + 2):
            try:
                log_cb(f"   🔁 尝试 {attempt}/{retries + 1}\n")
                return self._download_one_with_progress(url, target_path, progress_cb, stop_cb)
            except Exception as e:
                last_err = e
                log_cb(f"   ⚠️ 本次失败：{e}\n")
                try:
                    tmp = target_path.with_suffix(target_path.suffix + ".tmp")
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
                if attempt < retries + 1:
                    time.sleep(1.2)
                    continue
                break
        raise last_err

    def _ensure_in_group_dir(self, existing_file: Path, target_file: Path):
        """
        SHA 去重时：保证文件出现在目标 group 目录。
        优先硬链接（快、省空间），失败则复制。
        """
        target_file.parent.mkdir(parents=True, exist_ok=True)
        if target_file.exists():
            return
        try:
            os.link(str(existing_file), str(target_file))
        except Exception:
            try:
                shutil.copy2(str(existing_file), str(target_file))
            except Exception:
                pass

    def open_download_dialog(self):
        dl_workers_var = tk.IntVar(value=3)
        win = tk.Toplevel(self.root)
        win.title("批量下载并归档（按 group 子目录）")
        win.geometry("660x300")
        win.resizable(False, False)

        mode_var = tk.StringVar(value="filtered")  # filtered / selected
        retry_var = tk.IntVar(value=2)

        ttk.Label(win, text="下载范围：").pack(anchor="w", padx=14, pady=(12, 2))

        frm_mode = ttk.Frame(win)
        frm_mode.pack(fill="x", padx=14)

        ttk.Radiobutton(frm_mode, text="只下载筛选结果（当前列表展示）", value="filtered", variable=mode_var).pack(anchor="w")
        ttk.Radiobutton(frm_mode, text="只下载选中任务（需先在列表选中）", value="selected", variable=mode_var).pack(anchor="w")

        frm_retry = ttk.Frame(win)
        frm_retry.pack(fill="x", padx=14, pady=(8, 2))
        ttk.Label(frm_retry, text="失败自动重试次数：").pack(side="left")
        ttk.Spinbox(frm_retry, from_=0, to=5, width=4, textvariable=retry_var).pack(side="left", padx=6)
        ttk.Label(frm_retry, text="（0=不重试，2=推荐）").pack(side="left")

        frm_workers = ttk.Frame(win)
        frm_workers.pack(fill="x", padx=14, pady=(4, 2))
        ttk.Label(frm_workers, text="下载并发数：").pack(side="left")
        ttk.Spinbox(frm_workers, from_=1, to=10, width=4, textvariable=dl_workers_var).pack(side="left", padx=6)
        ttk.Label(frm_workers, text="（越大越快，但会更吃带宽/CPU）").pack(side="left")

        sep = ttk.Separator(win)
        sep.pack(fill="x", padx=14, pady=10)

        overall_label = ttk.Label(win, text="总体进度：0/0")
        overall_label.pack(anchor="w", padx=14)

        overall_bar = ttk.Progressbar(win, length=600, mode="determinate", maximum=100)
        overall_bar.pack(padx=14, pady=(2, 8))

        current_label = ttk.Label(win, text="当前文件：-")
        current_label.pack(anchor="w", padx=14)

        current_bar = ttk.Progressbar(win, length=600, mode="determinate", maximum=100)
        current_bar.pack(padx=14, pady=(2, 10))

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=14, pady=8)

        stop_flag = threading.Event()

        def stop_now():
            stop_flag.set()

        cancel_btn = ttk.Button(btns, text="取消下载", command=stop_now, state="disabled")
        cancel_btn.pack(side="right")

        def start_download():
            download_root = self._ensure_download_root()


            if not download_root.exists():
                messagebox.showerror("错误", "Download 根目录无效，请在上方设置。")
                return

            if mode_var.get() == "selected":
                task_ids = []
                for iid in self.tree.selection():
                    vals = self.tree.item(iid, "values")
                    if vals and vals[0]:
                        task_ids.append(vals[0])
                if not task_ids:
                    messagebox.showinfo("提示", "你选择了“只下载选中任务”，但当前没有选中任何任务。")
                    return
            else:
                task_ids = self._get_task_ids_from_tree_filtered()
                if not task_ids:
                    messagebox.showinfo("提示", "当前筛选结果为空，没有可下载任务。")
                    return

            tasks = []
            for tid in task_ids:
                t = self.tasks.get(tid)
                if not t:
                    continue
                if t.status == "Success" and t.video_url:
                    tasks.append(t)

            if not tasks:
                messagebox.showinfo("提示", "选定范围内没有可下载的视频（必须状态 Success 且有链接）。")
                return

            stop_flag.clear()
            cancel_btn.config(state="normal")

            th = threading.Thread(
                target=self._batch_download_worker,
                args=(tasks, download_root, int(retry_var.get()), int(dl_workers_var.get()),stop_flag,
                      overall_label, overall_bar, current_label, current_bar, cancel_btn),
                daemon=True
            )
            th.start()


        start_btn = ttk.Button(btns, text="开始下载", command=start_download)
        start_btn.pack(side="right", padx=(0, 10))

        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="left")



    def _batch_download_worker(self, tasks: list, out_dir: Path, retries: int, dl_workers: int,
                               stop_flag: threading.Event,
                               overall_label, overall_bar, current_label, current_bar, cancel_btn):
        def ui(fn):
            self.root.after(0, fn)

        def log(msg: str):
            ui(lambda: self.log_global(msg))

        def stop_cb() -> bool:
            return stop_flag.is_set()

        index_lock = threading.Lock()  # ✅ 并发写 index 必须加锁

        index = self._load_download_index()
        by_url = index["by_url"]
        by_sha = index["by_sha256"]

        # URL 去重（同批次）
        unique = {}
        for t in tasks:
            u = t.video_url.strip()
            if u not in unique:
                unique[u] = t

        urls = list(unique.items())
        total = len(urls)

        ui(lambda: overall_label.config(text=f"总体进度：0/{total}"))
        ui(lambda: overall_bar.config(value=0, maximum=100))
        ui(lambda: current_label.config(text=f"当前：并发下载中（{dl_workers} workers）"))
        ui(lambda: current_bar.config(value=0, maximum=100, mode="determinate"))

        ok = 0
        skipped = 0
        failed = 0
        done_count = 0

        log(f"\n📥 并发批量下载开始：{total} 个不同链接 | workers={dl_workers} | retries={retries}\n")

        def prepare_target(url: str, t: TaskItem) -> tuple[Path, str]:
            ext = self._guess_ext_from_url(url)
            filename = self._filename_from_task(t, ext)
            return out_dir / filename, ext

        def job(url: str, t: TaskItem):
            if stop_cb():
                return ("cancel", url, t.task_id, "", "")

            # 1) 先用锁检查 URL 记忆（避免并发重复下载同 URL）
            with index_lock:
                if url in by_url and by_url[url].get("file") and Path(by_url[url]["file"]).exists():
                    return ("skip_url", url, t.task_id, "", by_url[url]["file"])

            target, _ = prepare_target(url, t)

            # 2) 若目标文件已存在（用户手动拷贝/重复跑），直接写入索引并跳过
            if target.exists():
                with index_lock:
                    by_url[url] = {"file": str(target), "sha256": by_url.get(url, {}).get("sha256", ""),
                                   "task_id": t.task_id}
                    self._save_download_index(index)
                return ("skip_exists", url, t.task_id, "", str(target))

            # 3) 真下载（带重试）
            def progress_cb(downloaded, total_bytes):
                # 并发下不做单文件进度条，避免 UI 抖动；只更新总体即可
                return

            def log_cb(m: str):
                log(f"[DL] {t.task_id} {m}")

            try:
                success, sha, final_path = self._download_with_retries(
                    url=url,
                    target_path=target,
                    retries=retries,
                    progress_cb=progress_cb,
                    stop_cb=stop_cb,
                    log_cb=log_cb
                )

                if stop_cb():
                    return ("cancel", url, t.task_id, "", "")

                # 4) 下载完成后，用锁做 sha 去重 + 写索引
                with index_lock:
                    # sha 内容去重
                    if sha in by_sha and by_sha[sha].get("file") and Path(by_sha[sha]["file"]).exists():
                        # 删除重复文件，复用旧文件
                        try:
                            Path(final_path).unlink(missing_ok=True)
                        except Exception:
                            pass
                        existing = by_sha[sha]["file"]
                        by_url[url] = {"file": existing, "sha256": sha, "task_id": t.task_id}
                        self._save_download_index(index)
                        return ("skip_sha", url, t.task_id, sha, existing)

                    # 记录新文件
                    by_sha[sha] = {"file": final_path, "task_id": t.task_id, "url": url}
                    by_url[url] = {"file": final_path, "sha256": sha, "task_id": t.task_id}
                    self._save_download_index(index)

                return ("ok", url, t.task_id, sha, final_path)

            except Exception as e:
                # 清理 tmp
                try:
                    tmp = target.with_suffix(target.suffix + ".tmp")
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
                return ("fail", url, t.task_id, "", str(e))

        # 并发执行
        with ThreadPoolExecutor(max_workers=max(1, int(dl_workers))) as pool:
            futures = [pool.submit(job, url, t) for (url, t) in urls]

            for fut in as_completed(futures):
                if stop_cb():
                    break

                typ, url, tid, sha, info = fut.result()

                done_count += 1
                pct = done_count / total * 100.0
                ui(lambda pct=pct: overall_bar.config(value=pct))
                ui(lambda dc=done_count: overall_label.config(text=f"总体进度：{dc}/{total}"))
                ui(lambda: current_label.config(text=f"当前：并发下载中（{dl_workers} workers）"))

                if typ == "ok":
                    ok += 1
                    log(f"✅ [DL] {tid} 下载完成：{info}\n")
                elif typ in ("skip_url", "skip_exists", "skip_sha"):
                    skipped += 1
                    if typ == "skip_sha":
                        log(f"⏭️ [DL] {tid} 内容重复(sha256) 复用：{info}\n")
                    else:
                        log(f"⏭️ [DL] {tid} 跳过：{info}\n")
                elif typ == "cancel":
                    log("\n⛔ 已取消批量下载。\n")
                else:
                    failed += 1
                    log(f"❌ [DL] {tid} 下载失败：{info}\n")

        ui(lambda: cancel_btn.config(state="disabled"))
        log(f"\n📥 并发批量下载结束：成功 {ok} | 跳过 {skipped} | 失败 {failed}\n")
        ui(lambda: messagebox.showinfo("完成",
                                       f"并发批量下载结束：\n成功 {ok}\n跳过 {skipped}\n失败 {failed}\n\n下载目录：{out_dir}"))

    # =========================
    # Remaining helpers
    # =========================
    def open_selected_video(self):
        sel = self.get_selected_task_id()
        if not sel:
            messagebox.showinfo("提示", "请先选中一条任务。")
            return
        t = self.tasks.get(sel)
        if not t or not t.video_url:
            messagebox.showinfo("提示", "该任务还没有可打开的视频链接。")
            return
        webbrowser.open(t.video_url)


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
