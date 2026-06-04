import os
import re
import threading
import time
import sys
import requests
from http.client import IncompleteRead
from urllib.parse import urlparse, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import (
    Tk,
    filedialog,
    Text,
    Button,
    messagebox,
    Scrollbar,
    Label,
    Frame,
    LabelFrame,
    Checkbutton,
    BooleanVar,
    StringVar,
)
from tkinter.ttk import Progressbar, Combobox

from yt_dlp import YoutubeDL  # ✅ 用于 TikTok 真正下载

import json
import hmac
import hashlib
import uuid
import platform
import getpass
from datetime import datetime, timezone
from tkinter import Toplevel, simpledialog
import hashlib

def url_tag(url: str) -> str:
    """
    返回：不包含任何域名/路径/参数的标识。
    用于日志：只展示类型 + hash短指纹
    """
    u = (url or "").strip()
    if not u:
        return "EMPTY#00000000"

    h = hashlib.sha1(u.encode("utf-8")).hexdigest()[:8]
    low = u.lower()

    if "tiktok.com/" in low:
        kind = "TIKTOK"
    elif "/p/s_" in low and ("sora.chatgpt.com" in low or "mcq.com" in low):
        kind = "SORA_SHORT"
    elif "dyysy.com" in low:
        kind = "SORA_REDIRECT"
    else:
        # 只要是 mp4 直链（不管你内部域名是什么），统一标为 MP4
        kind = "MP4"

    return f"{kind}#{h}"


# ====== 你们自己改一下这个密钥（务必改，且不要外泄）======
# 建议：在你们私有仓库里存，别发给外部；打包时编进 exe
_LICENSE_HMAC_SECRET = "CHANGE_ME_TO_A_LONG_RANDOM_SECRET"

def _app_dir() -> str:
    # exe 所在目录
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def _license_path() -> str:
    return os.path.join(_app_dir(), "license.json")

def _get_machine_fingerprint() -> str:
    """
    机器指纹：使用 MAC + OS 信息做 hash（跨重装可能变化，够用于内部授权）
    你也可以替换成更稳定的 Windows MachineGuid（需要读注册表，见下方注释）。
    """
    mac = uuid.getnode()
    base = f"{mac}|{platform.system()}|{platform.release()}|{platform.machine()}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]

def _canonical_license_payload(lic: dict) -> str:
    """
    把 license 的核心字段按固定顺序序列化，用于签名/验签
    """
    core = {
        "app": lic.get("app", ""),
        "expires_utc": lic.get("expires_utc", ""),
        "allowed_machine_ids": lic.get("allowed_machine_ids", []),
        "allowed_users": lic.get("allowed_users", []),
    }
    return json.dumps(core, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

def _sign_license_payload(payload: str) -> str:
    sig = hmac.new(
        _LICENSE_HMAC_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return sig

def _parse_utc(dt_str: str) -> datetime:
    # 期望格式: "2026-02-01T00:00:00Z"
    if not dt_str:
        raise ValueError("expires_utc missing")
    dt_str = dt_str.replace("Z", "+00:00")
    return datetime.fromisoformat(dt_str).astimezone(timezone.utc)

def enforce_license_or_exit(app_name: str = "SoraTikTokDownloader"):
    """
    校验失败 -> 弹窗并退出
    """
    lic_file = _license_path()
    if not os.path.exists(lic_file):
        messagebox.showerror(
            "授权缺失",
            "未检测到 license.json（授权文件）。\n请联系项目负责人获取授权。"
        )
        raise SystemExit(1)

    try:
        with open(lic_file, "r", encoding="utf-8") as f:
            lic = json.load(f)
    except Exception as e:
        messagebox.showerror("授权错误", f"license.json 读取失败：{e}")
        raise SystemExit(1)

    # 1) app 名称匹配（防止 license 被别的项目复用）
    if lic.get("app") != app_name:
        messagebox.showerror("授权错误", "授权文件不匹配当前应用。")
        raise SystemExit(1)

    # 2) 验签（防篡改）
    payload = _canonical_license_payload(lic)
    expected_sig = _sign_license_payload(payload)
    if lic.get("signature") != expected_sig:
        messagebox.showerror("授权错误", "授权文件签名校验失败（可能被篡改）。")
        raise SystemExit(1)

    # 3) 过期检查
    try:
        exp = _parse_utc(lic.get("expires_utc", ""))
    except Exception as e:
        messagebox.showerror("授权错误", f"授权到期时间格式错误：{e}")
        raise SystemExit(1)

    now = datetime.now(timezone.utc)
    if now > exp:
        messagebox.showerror("授权到期", f"授权已过期（到期时间：{exp.isoformat()}）。")
        raise SystemExit(1)

    # 4) 用户白名单（可选）
    current_user = getpass.getuser()
    allowed_users = lic.get("allowed_users") or []
    if allowed_users and current_user not in allowed_users:
        messagebox.showerror(
            "无权限",
            f"当前用户：{current_user}\n不在授权用户列表内。"
        )
        raise SystemExit(1)

    # 5) 机器白名单（核心）
    machine_id = _get_machine_fingerprint()
    allowed_machines = lic.get("allowed_machine_ids") or []
    if allowed_machines and machine_id not in allowed_machines:
        messagebox.showerror(
            "无权限",
            "此设备未被授权使用该工具。\n"
            f"Machine ID:\n{machine_id}\n\n"
            "请联系项目负责人登记授权。"
        )
        raise SystemExit(1)

    # 通过
    return True



def ensure_playwright_browsers_path():
    import os, sys

    # dist/YourApp/SoraVideoDownloader.exe 所在目录
    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(sys.executable)
    else:
        app_dir = os.path.dirname(os.path.abspath(__file__))

    # ✅ PyInstaller onedir 常见位置：_internal\ms-playwright
    cand1 = os.path.join(app_dir, "_internal", "ms-playwright")
    # ✅ 兼容你之前的结构：ms-playwright
    cand2 = os.path.join(app_dir, "ms-playwright")

    bundled = cand1 if os.path.isdir(cand1) else cand2

    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bundled
    os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"

    # 检查目录是否存在
    if not os.path.isdir(bundled):
        return bundled, False, "ms-playwright 目录不存在（打包时未携带浏览器）"

    # 检查是否包含 chromium
    has_chromium = False
    try:
        for name in os.listdir(bundled):
            if name.startswith("chromium"):
                has_chromium = True
                break
    except Exception:
        pass

    if not has_chromium:
        return bundled, False, "ms-playwright 内未发现 chromium（可能被杀软删除/打包不完整）"

    return bundled, True, "OK"





def get_ffmpeg_path():
    if hasattr(sys, "_MEIPASS"):
        # PyInstaller 文件夹版
        return os.path.join(sys._MEIPASS, "ffmpeg", "ffmpeg.exe")
    else:
        # 你电脑本地路径
        return r"C:\ProgramApply\ffmpeg-7.1.1-essentials_build\bin\ffmpeg.exe"


ffmpeg_path = get_ffmpeg_path()

# 支持 sora.chatgpt.com 和 mcq.com 两种短链
SHORT_URL_PATTERN = re.compile(
    r'https?://(?:sora\.chatgpt\.com|mcq\.com)/p/(s_[a-zA-Z0-9]+)'
)

# 正确的直链前缀（必须包含 s_）
REAL_VIDEO_PREFIX = "https://oscdn2.dyysy.com/MP4/"
FINAL_VIDEO_HOST_HINTS = ("ss2.life",)
FINAL_VIDEO_PATH_HINTS = ("/raw?", "%2fraw?", "raw?")

# ✅ 默认重定向页前缀（按你要求固定）
REDIRECT_PREFIX_DEFAULT = "https://dyysy.com/"




# TikTok 链接判断
def is_tiktok_url(url: str) -> bool:
    return "tiktok.com/" in url.lower()


def safe_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = os.path.basename(parsed.path)
    if not name:
        name = "video.mp4"
    elif name == "raw":
        parts = [seg for seg in parsed.path.split("/") if seg]
        name = f"{parts[-2]}.mp4" if len(parts) >= 2 else "video.mp4"
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name


def check_url_accessible(url: str, timeout: int = 10):
    try:
        resp = requests.get(url, stream=True, timeout=timeout)
        status = resp.status_code
        resp.close()
        if status == 200:
            return True, None
        else:
            return False, f"HTTP {status}"
    except Exception as e:
        return False, str(e)


def build_redirect_link(original_url: str, prefix: str = REDIRECT_PREFIX_DEFAULT) -> str:
    """
    Link1 -> https://dyysy.com/?url=<encoded_link1>
    prefix 默认固定 https://dyysy.com/
    """
    u = original_url.strip()
    if not u:
        return ""
    param = "?url=" + quote(u, safe="")
    # prefix 一般是以 / 结尾的域名根；直接拼接即可
    # 也兼容 prefix 自带 query 的情况
    if "?" in prefix:
        return prefix + "&" + param.lstrip("?")
    return prefix + param


def process_short_link(url: str) -> tuple[str, str | None]:
    """
    新版：短链处理返回 (original_or_direct_url, redirect_url_or_None)
    - 如果是短链（sora/mcq）:
        real_url = 原始 sora 链接（保留完整 query）
        redirect_url = https://dyysy.com/?url=<encoded original>
    - 否则:
        real_url = 原样
        redirect_url = None
    """
    clean = url.strip()
    m = SHORT_URL_PATTERN.search(clean)
    if m:
        redirect = build_redirect_link(clean, REDIRECT_PREFIX_DEFAULT)
        return clean, redirect
    return clean, None


def is_final_video_url(url: str) -> bool:
    low = (url or "").lower()
    if not low.startswith("http"):
        return False
    if not any(host in low for host in FINAL_VIDEO_HOST_HINTS):
        return False
    if "thumbnail" in low or "/drvs/" in low or "%2fdrvs%2f" in low:
        return False
    return any(hint in low for hint in FINAL_VIDEO_PATH_HINTS)


def extract_candidate_video_urls(page_url: str, dom_urls: list[str], response_urls: list[str]) -> list[str]:
    ordered = []
    seen = set()
    for candidate in [page_url, *dom_urls, *response_urls]:
        candidate = (candidate or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if is_final_video_url(candidate):
            ordered.append(candidate)
    return ordered


def download_video_http(
    url: str,
    outdir: str,
    progress_callback,
    index: int | None = None,
    max_retries: int = 8
):
    os.makedirs(outdir, exist_ok=True)

    base_name = safe_filename_from_url(url)
    if index is not None:
        filename = f"{index:03d}_{base_name}"
    else:
        filename = base_name

    file_path = os.path.join(outdir, filename)
    temp_path = file_path + ".part"

    base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }

    last_err = None
    session = requests.Session()

    for attempt in range(1, max_retries + 1):
        try:
            existing_size = 0
            if os.path.exists(temp_path):
                existing_size = os.path.getsize(temp_path)

            headers = base_headers.copy()
            if existing_size > 0:
                headers["Range"] = f"bytes={existing_size}-"

            print(
                f"开始下载（第 {attempt}/{max_retries} 次尝试，"
                f"已存在 {existing_size} 字节）：{os.path.basename(file_path)} [{url_tag(url)}]"
            )

            resp = session.get(
                url,
                headers=headers,
                stream=True,
                timeout=(10, 120)
            )
            resp.raise_for_status()

            total_size = None
            content_length = resp.headers.get("content-length")
            content_range = resp.headers.get("Content-Range")

            if content_range:
                try:
                    total_size = int(content_range.split("/")[-1])
                except Exception:
                    total_size = None
            elif content_length:
                try:
                    remaining = int(content_length)
                    total_size = existing_size + remaining
                except Exception:
                    total_size = None

            mode = "ab" if existing_size > 0 else "wb"
            downloaded = existing_size

            with open(temp_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)

                    if total_size:
                        pct = int(downloaded * 100 / total_size)
                        pct = max(0, min(100, pct))
                        progress_callback(pct)

            if total_size and downloaded < total_size:
                raise IncompleteRead(downloaded, total_size - downloaded)

            progress_callback(100)

            if os.path.exists(file_path):
                os.remove(file_path)
            os.rename(temp_path, file_path)

            print(f"下载完成：{file_path}")
            return None

        except (IncompleteRead,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_err = e
            print(
                f"连接中断或网络异常，准备重试 "
                f"({attempt}/{max_retries})：{e}"
            )
            time.sleep(2 * attempt)
            continue
        except Exception as e:
            last_err = e
            print(f"下载异常（第 {attempt} 次）：{e}")
            time.sleep(2 * attempt)
            continue

    if os.path.exists(temp_path):
        try:
            os.remove(temp_path)
        except OSError:
            pass

    err = f"下载失败：[{url_tag(url)}]，错误：{last_err}"
    print(err)
    return err


def extract_tiktok_vid(url: str) -> str:
    m = re.search(r'/video/(\d+)', url)
    if m:
        return m.group(1)
    return "tiktok_video"


def download_tiktok(url: str, outdir: str, progress_callback, index: int | None = None):
    os.makedirs(outdir, exist_ok=True)

    vid = extract_tiktok_vid(url)

    if index is not None:
        filename = f"{index:03d}_{vid}.mp4"
    else:
        filename = f"{vid}.mp4"

    outtmpl = os.path.join(outdir, filename)

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "mp4/best",
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "ffmpeg_location": os.path.dirname(ffmpeg_path) if ffmpeg_path else None,
    }

    try:
        progress_callback(5)
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        progress_callback(100)
        return None
    except Exception as e:
        err = f"TikTok 下载失败：{url}，错误：{e}"
        print(err)
        return err


class VideoDownloaderApp(Tk):

    def _open_admin_panel(self, event=None):
        # 可选：简单口令
        pwd = simpledialog.askstring("管理员入口", "请输入管理员口令：", show="*")
        if pwd != "YOUR_ADMIN_PASSWORD":
            messagebox.showerror("拒绝", "口令错误。")
            return

        mid = _get_machine_fingerprint()
        win = Toplevel(self)
        win.title("管理员工具")
        win.geometry("420x180")

        Label(win, text="当前设备 Machine ID：").pack(pady=(10, 5))
        txt = Text(win, height=2, width=45)
        txt.insert("1.0", mid)
        txt.config(state="disabled")
        txt.pack(pady=5)

        def copy_mid():
            self.clipboard_clear()
            self.clipboard_append(mid)
            messagebox.showinfo("已复制", "Machine ID 已复制到剪贴板")

        Button(win, text="复制 Machine ID", command=copy_mid).pack(pady=8)

    def __init__(self):
        super().__init__()


        # ✅ 启动即校验（失败会弹窗并退出）
        enforce_license_or_exit(app_name="SoraTikTokDownloader")

        self.download_path = os.path.join(os.path.expanduser("~"), "Downloads")
        self.total_videos = 0
        self.completed_videos = 0
        self.successful_downloads = 0
        self.failed_downloads = 0

        self.enable_precheck_var = BooleanVar(value=False)

        self.bind_all("<Control-Shift-A>", self._open_admin_panel)

        # ✅ 并发下载数配置
        self.workers_var = StringVar(value="3")

        # ✅ 新增：Headless 激活开关 + 参数（尽量不动原UI，只加一小块）
        self.enable_headless_var = BooleanVar(value=True)
        self.headless_wait_ms_var = StringVar(value="3000")
        self.headless_timeout_ms_var = StringVar(value="20000")
        self.headless_delay_var = StringVar(value="0.35")

        self.title("Sora / TikTok 批量视频下载工具")
        self.geometry("900x760")  # 轻微加高一点点，避免挤压
        self.minsize(900, 720)

        self.create_widgets()



    # ===== 日志 & 状态 =====

    def log(self, msg: str, url: str | None = None):
        ts = time.strftime("[%H:%M:%S] ")
        if url:
            msg = f"{msg} [{url_tag(url)}]"
        full = ts + msg
        print(full)
        self.after(0, self._append_log, full)

    def _append_log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_status(self, text: str):
        self.after(0, lambda: self.status_label.config(text=f"状态：{text}"))

    # ===== UI 构建 =====

    def create_widgets(self):
        root_frame = Frame(self, padx=10, pady=10)
        root_frame.pack(fill="both", expand=True)

        guide_label = Label(
            root_frame,
            text="使用步骤：① 选择保存位置 -> 粘贴或导入链接（② 可选） -> ③ 点击【开始下载】",
            fg="#0052cc",
            font=("Microsoft YaHei", 11, "bold"),
            anchor="w",
            justify="left",
        )
        guide_label.pack(fill="x", pady=(0, 8))

        top_frame = Frame(root_frame)
        top_frame.pack(fill="x", pady=(0, 10))

        path_frame = Frame(top_frame)
        path_frame.pack(fill="x", pady=3)

        self.path_button = Button(
            path_frame,
            text="① 选择保存位置",
            command=self.select_download_path,
            width=16,
            bg="#f0f0f0",
        )
        self.path_button.pack(side="left")

        self.download_path_label = Label(
            path_frame,
            text=f"当前保存位置： {self.download_path}",
            anchor="w",
            wraplength=650,
            justify="left",
        )
        self.download_path_label.pack(side="left", padx=10, fill="x", expand=True)

        btn_row = Frame(top_frame)
        btn_row.pack(fill="x", pady=3)

        self.import_button = Button(
            btn_row,
            text="② 从TXT导入链接（可选）",
            command=self.import_links,
            width=22,
        )
        self.import_button.pack(side="left", padx=3)

        self.export_button = Button(
            btn_row,
            text="导出当前链接到TXT",
            command=self.export_links,
            width=18,
        )
        self.export_button.pack(side="left", padx=3)

        self.start_button = Button(
            btn_row,
            text="③ 开始下载（自动去重）",
            command=self.start_download,
            width=26,
            bg="#e0ffe0",
        )
        self.start_button.pack(side="left", padx=8)

        self.clear_button = Button(
            btn_row,
            text="清空链接",
            command=self.clear_links,
            width=10,
            bg="#ffe0e0",
        )
        self.clear_button.pack(side="left", padx=3)

        self.precheck_checkbox = Checkbutton(
            top_frame,
            text="启用下载前检查（可能变慢）",
            variable=self.enable_precheck_var
        )
        self.precheck_checkbox.pack(anchor="w", pady=(2, 3))

        # ✅ 新增一小块：Headless 参数（尽量不破坏原布局）
        headless_frame = Frame(top_frame)
        headless_frame.pack(fill="x", pady=(0, 4))

        self.headless_checkbox = Checkbutton(
            headless_frame,
            text="启用 Headless 激活（Sora短链必需）",
            variable=self.enable_headless_var
        )
        self.headless_checkbox.pack(side="left")

        Label(headless_frame, text="等待(ms)：").pack(side="left", padx=(10, 0))
        self.wait_entry = Text(headless_frame, height=1, width=6)
        self.wait_entry.insert("1.0", self.headless_wait_ms_var.get())
        self.wait_entry.pack(side="left", padx=3)

        Label(headless_frame, text="超时(ms)：").pack(side="left", padx=(10, 0))
        self.timeout_entry = Text(headless_frame, height=1, width=7)
        self.timeout_entry.insert("1.0", self.headless_timeout_ms_var.get())
        self.timeout_entry.pack(side="left", padx=3)

        Label(headless_frame, text="间隔(s)：").pack(side="left", padx=(10, 0))
        self.delay_entry = Text(headless_frame, height=1, width=6)
        self.delay_entry.insert("1.0", self.headless_delay_var.get())
        self.delay_entry.pack(side="left", padx=3)

        Label(headless_frame, text="重定向前缀：<hidden>").pack(side="left", padx=(12, 0))

        meta_frame = Frame(root_frame)
        meta_frame.pack(fill="x", pady=(5, 5))

        self.link_count_label = Label(meta_frame, text="当前链接数：0")
        self.link_count_label.pack(side="left")

        self.total_progress_label = Label(meta_frame, text="   总进度：0 / 0")
        self.total_progress_label.pack(side="left", padx=20)

        Label(meta_frame, text="并发下载数：").pack(side="left")
        self.worker_combo = Combobox(
            meta_frame,
            width=4,
            textvariable=self.workers_var,
            state="readonly",
            values=("1", "2", "3", "4", "8"),
        )
        self.worker_combo.pack(side="left", padx=5)

        progress_frame = Frame(root_frame)
        progress_frame.pack(fill="x", pady=(0, 10))

        Label(progress_frame, text="整体进度：").pack(anchor="w")
        self.total_progress = Progressbar(progress_frame, length=800, mode="determinate")
        self.total_progress.pack(fill="x", pady=3)

        Label(progress_frame, text="当前正在下载的这一个：").pack(anchor="w", pady=(8, 0))
        self.current_progress = Progressbar(progress_frame, length=800, mode="determinate")
        self.current_progress.pack(fill="x", pady=3)

        link_frame = LabelFrame(root_frame, text="②链接列表区域")
        link_frame.pack(fill="both", expand=True, pady=(5, 10))

        tips_label = Label(
            link_frame,
            text="小提示：\n- 直接粘贴链接到下面框里，一行一个\n- 不用手动回车，粘贴后软件会自动帮你换行\n- 支持：TikTok 链接、sora 短链、mp4 直链混合",
            justify="left",
            anchor="w",
        )
        tips_label.pack(fill="x", padx=5, pady=(3, 0))

        link_inner = Frame(link_frame)
        link_inner.pack(fill="both", expand=True, padx=2, pady=3)

        self.url_input = Text(link_inner, height=10, width=110)
        self.url_input.pack(side="left", fill="both", expand=True)

        scroll = Scrollbar(link_inner, command=self.url_input.yview)
        scroll.pack(side="right", fill="y")
        self.url_input.config(yscrollcommand=scroll.set)

        self.url_input.bind("<KeyRelease>", self.update_link_count)
        self.url_input.bind("<<Paste>>", self.on_paste)

        log_frame = LabelFrame(root_frame, text="下载过程")
        log_frame.pack(fill="both", expand=False, pady=(0, 5))

        log_inner = Frame(log_frame)
        log_inner.pack(fill="both", expand=True)

        self.log_text = Text(log_inner, height=8, width=110, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)

        log_scroll = Scrollbar(log_inner, command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=log_scroll.set)

        status_frame = Frame(self, bd=1, relief="sunken")
        status_frame.pack(side="bottom", fill="x")
        self.status_label = Label(status_frame, text="状态：就绪，可以开始粘贴链接啦", anchor="w", padx=5)
        self.status_label.pack(fill="x")

    # ===== 工具方法 =====

    def get_current_links(self):
        text = self.url_input.get("1.0", "end-1c").strip()
        if not text:
            return []
        return [line.strip() for line in text.splitlines() if line.strip()]

    def set_links(self, links):
        self.url_input.delete("1.0", "end")
        if links:
            self.url_input.insert("1.0", "\n".join(links) + "\n")
        self.update_link_count()

    def dedupe_links(self):
        links = self.get_current_links()
        seen = set()
        deduped = []
        for url in links:
            if url not in seen:
                seen.add(url)
                deduped.append(url)
        if len(deduped) != len(links):
            self.log(f"已自动去重：原来 {len(links)} 条，去重后 {len(deduped)} 条")
        self.set_links(deduped)
        return deduped

    # ===== 文本框事件 =====

    def on_paste(self, event=None):
        self.after(1, self._post_paste_fix)

    def _post_paste_fix(self):
        text = self.url_input.get("1.0", "end-1c")
        if text and not text.endswith("\n"):
            self.url_input.insert("end", "\n")
        self.update_link_count()

    def clear_links(self):
        self.url_input.delete("1.0", "end")
        self.total_videos = 0
        self.completed_videos = 0
        self.successful_downloads = 0
        self.failed_downloads = 0

        self.link_count_label.config(text="当前链接数：0")
        self.total_progress_label.config(text="总进度：0 / 0")
        self.total_progress["value"] = 0
        self.total_progress["maximum"] = 0
        self.current_progress["value"] = 0
        self.log("已清空所有链接。")
        self.set_status("就绪，可以开始粘贴链接啦")

    def update_link_count(self, event=None):
        links = self.get_current_links()
        count = len(links)
        self.total_videos = count
        self.link_count_label.config(text=f"当前链接数：{count}")
        self.total_progress_label.config(text=f"总进度：0 / {self.total_videos}")

    # ===== 路径 & 导入导出 =====

    def select_download_path(self):
        new_path = filedialog.askdirectory(title="选择保存位置", initialdir=self.download_path)
        if new_path:
            self.download_path = new_path
            self.download_path_label.config(text=f"当前保存位置： {self.download_path}")
            self.log(f"已选择保存位置：{self.download_path}")
            self.set_status("已选择保存位置，可以继续粘贴链接")

    def import_links(self):
        file_path = filedialog.askopenfilename(
            title="从TXT导入链接",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if not file_path:
            return
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f.readlines()]
            links = [ln for ln in lines if ln]
            self.set_links(links)
            self.log(f"已从 TXT 导入链接：{file_path} （{len(links)} 条）")
            messagebox.showinfo("导入成功", f"成功导入 {len(links)} 条链接。")
            self.set_status("链接已导入，可以直接开始下载")
        except Exception as e:
            self.log(f"导入失败：{e}")
            messagebox.showerror("导入失败", f"导入失败：{e}")

    def export_links(self):
        links = self.get_current_links()
        if not links:
            messagebox.showwarning("提示", "当前没有可导出的链接。")
            return
        file_path = filedialog.asksaveasfilename(
            title="导出链接到TXT",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if not file_path:
            return
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("\n".join(links))
            self.log(f"已导出链接到：{file_path} （{len(links)} 条）")
            messagebox.showinfo("导出成功", f"已导出 {len(links)} 条链接到：\n{file_path}")
        except Exception as e:
            self.log(f"导出失败：{e}")
            messagebox.showerror("导出失败", f"导出失败：{e}")

    # ===== ✅ 新增：Headless 批量激活（在下载前做一次）=====

    def _read_headless_params(self):
        def read_textbox(tb: Text, fallback: str) -> str:
            try:
                v = tb.get("1.0", "end-1c").strip()
                return v if v else fallback
            except Exception:
                return fallback

        wait_ms = read_textbox(self.wait_entry, "2500")
        timeout_ms = read_textbox(self.timeout_entry, "20000")
        delay_s = read_textbox(self.delay_entry, "0.35")

        try:
            wait_ms_i = int(float(wait_ms))
        except Exception:
            wait_ms_i = 2500

        try:
            timeout_ms_i = int(float(timeout_ms))
        except Exception:
            timeout_ms_i = 20000

        try:
            delay_f = float(delay_s)
        except Exception:
            delay_f = 0.35

        # 合理范围
        wait_ms_i = max(0, min(wait_ms_i, 60000))
        timeout_ms_i = max(5000, min(timeout_ms_i, 120000))
        delay_f = max(0.0, min(delay_f, 5.0))

        return wait_ms_i, timeout_ms_i, delay_f



    def headless_activate_redirects(self, redirect_urls: list[str], concurrency: int = 3):
        """
        并发激活并解析最终下载链接。
        返回：
          resolved_map: {redirect_url: final_video_url}
          failed_redirects: [redirect_url, ...]
        """
        bundled_path, ok, why = ensure_playwright_browsers_path()
        self.log(f"Playwright browsers path = {bundled_path} ({why})")
        if not ok:
            self.log("❌ Headless 无法启动：浏览器未就绪。请确认打包包含 ms-playwright。")
            return {}, redirect_urls[:]

        self.log(f"Playwright browsers path = {bundled_path}")
        if not redirect_urls:
            return {}, []

        try:
            from playwright.async_api import async_playwright
            import asyncio
        except Exception:
            self.log("❌ 缺少 playwright.async_api 或 asyncio 不可用。请确认已安装 playwright。")
            return {}, redirect_urls[:]

        wait_ms, timeout_ms, delay_f = self._read_headless_params()

        try:
            concurrency = int(concurrency)
        except Exception:
            concurrency = 3
        concurrency = max(1, min(concurrency, 6))

        self.log(
            f"✅ Headless 并发激活(Async)：concurrency={concurrency}, wait={wait_ms}ms, timeout={timeout_ms}ms, delay={delay_f}s")
        self.set_status("Headless 并发激活中（Async，无多线程）...")

        async def safe_launch(p):
            return await asyncio.wait_for(
                p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-gpu",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-dev-shm-usage",
                        "--disable-features=site-per-process",
                    ],
                ),
                timeout=20
            )

        async def run():
            resolved_map = {}
            failed = []
            sem = asyncio.Semaphore(concurrency)

            async with async_playwright() as p:
                browser = await safe_launch(p)

                async def activate_one(url_item: str, idx: int):
                    async with sem:
                        context = None
                        try:
                            context = await browser.new_context(
                                user_agent=(
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/123.0.0.0 Safari/537.36"
                                ),
                                viewport={"width": 1280, "height": 720},
                            )
                            page = await context.new_page()
                            page.set_default_timeout(timeout_ms)
                            response_urls = []

                            def handle_response(resp):
                                try:
                                    resp_url = resp.url
                                except Exception:
                                    return
                                if is_final_video_url(resp_url):
                                    response_urls.append(resp_url)

                            page.on("response", handle_response)

                            self.log(f"[Headless激活 {idx}/{len(redirect_urls)}] ", url=url_item)
                            await page.goto(url_item, wait_until="domcontentloaded")
                            await page.wait_for_timeout(wait_ms)

                            player = page.locator("#videoPlayer")
                            try:
                                await player.wait_for(state="attached", timeout=timeout_ms)
                            except Exception:
                                pass

                            try:
                                await page.wait_for_function(
                                    """() => {
                                        const el = document.querySelector('#videoPlayer');
                                        const src = el?.getAttribute('src') || el?.src || '';
                                        return src.includes('ss2.life') && src.includes('/raw?');
                                    }""",
                                    timeout=timeout_ms,
                                )
                            except Exception:
                                pass

                            await page.wait_for_timeout(min(wait_ms, 1500))

                            player_src = await player.get_attribute("src")
                            player_poster = await player.get_attribute("poster")
                            dom_urls = await page.eval_on_selector_all(
                                "video, source, a[href]",
                                """elements => elements
                                    .map(el => el.currentSrc || el.src || el.href || "")
                                    .filter(Boolean)"""
                            )
                            candidates = extract_candidate_video_urls(
                                page.url,
                                [player_src, player_poster, *dom_urls],
                                response_urls
                            )
                            if not candidates:
                                video_count = await page.locator("video").count()
                                raise RuntimeError(
                                    f"未解析到最终视频链接 | page={page.url} | video_count={video_count} | "
                                    f"player_src={player_src or '<empty>'} | player_poster={player_poster or '<empty>'} | "
                                    f"dom_hits={len(dom_urls)} | resp_hits={len(response_urls)}"
                                )

                            final_url = candidates[0]
                            resolved_map[url_item] = final_url
                            self.log("  -> FINAL: <hidden>")
                            await asyncio.sleep(delay_f)
                            return True
                        except Exception as e:
                            self.log(f"❌ [Headless FAIL] ERROR: {e}", url=url_item)
                            failed.append(url_item)
                            return False
                        finally:
                            if context:
                                await context.close()

                tasks = [activate_one(u, i + 1) for i, u in enumerate(redirect_urls)]
                await asyncio.gather(*tasks)
                await browser.close()

            return resolved_map, failed

        try:
            resolved_map, failed_redirects = asyncio.run(run())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            resolved_map, failed_redirects = loop.run_until_complete(run())
            loop.close()

        self.set_status("Headless 激活完成，开始下载...")
        return resolved_map, failed_redirects

    # ===== 下载主流程 =====

    def start_download(self):
        deduped_links = self.dedupe_links()

        if not deduped_links:
            messagebox.showwarning("提示", "请先粘贴至少一个视频链接。")
            return

        self.completed_videos = 0
        self.successful_downloads = 0
        self.failed_downloads = 0

        self.total_progress["value"] = 0
        self.total_progress["maximum"] = len(deduped_links)
        self.total_progress_label.config(text=f"总进度：0 / {len(deduped_links)}")
        self.current_progress["value"] = 0

        self.start_button.config(state="disabled")

        try:
            workers = int(self.workers_var.get() or "3")
        except ValueError:
            workers = 3
        self.log(f"本次下载并发数设置为：{workers}")

        # ✅ 下载线程（保持原结构）
        if self.enable_precheck_var.get():
            self.set_status("准备中：将先激活短链，再检查可用性...")
            t = threading.Thread(
                target=self.run_precheck_and_download,
                args=(deduped_links,),
                daemon=True
            )
            t.start()
        else:
            self.set_status("准备中：将先激活短链，再开始下载...")
            t = threading.Thread(
                target=self.run_downloads_with_headless_prepare,
                args=(deduped_links, False),
                daemon=True
            )
            t.start()

    def _prepare_urls_with_headless_if_needed(self, orig_urls: list[str]) -> tuple[list[str], set[str]]:
        """
        核心升级点：
        - 把 orig_urls 转成可下载的 real_urls（短链 -> Headless 解析最终下载链接；其他原样）
        - 如果启用 Headless，则先批量激活所有短链对应的 redirect_url 并提取最终视频链接
        返回：
          real_urls, failed_real_urls_set（激活失败导致不可下载的 original_url）
        """
        real_urls = []
        redirect_urls = []
        mapping_original_from_redirect = {}  # redirect -> original sora url

        for u in orig_urls:
            # TikTok 不需要激活
            if is_tiktok_url(u):
                real_urls.append(u)
                continue

            real, redirect = process_short_link(u)
            if redirect:
                redirect_urls.append(redirect)
                mapping_original_from_redirect[redirect] = real
            else:
                real_urls.append(real)

        failed_real = set()

        if self.enable_headless_var.get():
            # 只对短链激活
            redirect_urls = list(dict.fromkeys(redirect_urls))  # 去重保序
            if redirect_urls:
                self.log(f"检测到短链 {len(redirect_urls)} 条，需要 Headless 激活后再下载。")
                try:
                    c = int(self.workers_var.get() or "3")
                except:
                    c = 3
                c = max(1, min(c, 3))
                resolved_map, failed_redirects = self.headless_activate_redirects(redirect_urls, concurrency=c)

                for redirect in redirect_urls:
                    final_url = resolved_map.get(redirect)
                    original = mapping_original_from_redirect.get(redirect, redirect)
                    if final_url:
                        real_urls.append(final_url)
                    else:
                        real_urls.append(original)
                        failed_real.add(original)

                failed_real = {x for x in failed_real if x}

                if failed_real:
                    self.log(f"❌ Headless 激活失败 {len(failed_real)} 条，这些将标记为失败并跳过下载。")
            else:
                self.log("未检测到需要 Headless 激活的短链。")
        else:
            # 用户关闭 headless：短链大概率会失败，但尊重开关
            if redirect_urls:
                self.log("⚠️ 你关闭了 Headless 激活，但输入包含 Sora 短链；这些链接可能无法下载。")
                for redirect in redirect_urls:
                    original = mapping_original_from_redirect.get(redirect)
                    if original:
                        real_urls.append(original)
                        failed_real.add(original)

        return real_urls, failed_real

    def run_precheck_and_download(self, orig_urls):
        # ✅ 先准备（激活短链）
        self.log(f"准备阶段：解析链接 & Headless 激活（如需要），共 {len(orig_urls)} 条...")
        real_urls, failed_real = self._prepare_urls_with_headless_if_needed(orig_urls)

        self.log(f"开始检查链接是否可用，共 {len(real_urls)} 条...")
        valid_urls = []

        for idx, real in enumerate(real_urls, start=1):
            # 如果激活失败，直接算失败
            if real in failed_real:
                self.failed_downloads += 1
                self.log(f"[不可用] {real} - Headless 激活失败")
                continue

            # TikTok 不做 HTTP precheck（可选：你原来也没做专门处理，这里保持简单）
            if is_tiktok_url(real):
                valid_urls.append(real)
                self.log(f"[跳过检查 TikTok] {real}")
                continue

            ok, err = check_url_accessible(real)
            if ok:
                self.log("[可用]", url=real)
                valid_urls.append(real)
            else:
                self.failed_downloads += 1
                self.log(f"[不可用] - {err}", url=real)

        if not valid_urls:
            self.after(0, self._precheck_all_failed)
            return

        self.after(0, lambda: self._setup_progress_for_valid(len(valid_urls)))
        self.log(f"检查完成：可下载 {len(valid_urls)} 条，失效 {self.failed_downloads} 条，开始下载视频...")
        self.set_status("检查完成，正在下载视频...")

        # ✅ valid_urls 都是“最终可下载URL”（可能是 TikTok 原链接 或 mp4 直链）
        self.run_downloads(valid_urls, already_real=True)

    def run_downloads_with_headless_prepare(self, orig_urls, already_real: bool = False):
        """
        不启用 precheck 的情况下：
        - 也要先做 Headless 激活
        - 然后直接 run_downloads
        """
        self.log(f"准备阶段：解析链接 & Headless 激活（如需要），共 {len(orig_urls)} 条...")
        real_urls, failed_real = self._prepare_urls_with_headless_if_needed(orig_urls)

        # 激活失败的直接计失败并从队列剔除
        filtered = []
        for r in real_urls:
            if r in failed_real:
                self.failed_downloads += 1
                self.log(f"[跳过下载] {r} - Headless 激活失败")
            else:
                filtered.append(r)

        if not filtered:
            self.after(0, self._precheck_all_failed)
            return

        # 这里 already_real=True，因为我们已经把短链转换成 mp4 直链
        self.run_downloads(filtered, already_real=True)

    def _precheck_all_failed(self):
        self.total_videos = 0
        self.total_progress["maximum"] = 0
        self.total_progress["value"] = 0
        self.total_progress_label.config(text="总进度：0 / 0")
        self.current_progress["value"] = 0
        self.start_button.config(state="normal")
        self.set_status("所有链接都失效，请检查后重新粘贴")
        messagebox.showerror("全部失效", "检查结果：所有链接都不可用，已终止下载。")

    def _setup_progress_for_valid(self, n: int):
        self.total_videos = n
        self.completed_videos = 0
        self.total_progress["maximum"] = n
        self.total_progress["value"] = 0
        self.total_progress_label.config(text=f"总进度：0 / {n}")

    def run_downloads(self, urls, already_real: bool = False):
        try:
            workers = int(self.workers_var.get() or "3")
        except ValueError:
            workers = 3
        if workers < 1:
            workers = 1
        if workers > 16:
            workers = 16

        self.log(f"使用线程池并发数量：{workers}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = []
            for idx, url in enumerate(urls, start=1):
                futures.append(pool.submit(self.download_one, url, idx, already_real))

            for _ in as_completed(futures):
                self.completed_videos += 1
                self.after(0, self.update_total_progress)

        self.after(0, self.on_all_done)

    def download_one(self, url: str, index: int, already_real: bool = False):
        # ✅ 这里保持你的原逻辑：real_url 已经在上游准备好（mp4 or tiktok or other）
        real_url = url if already_real else url

        if is_tiktok_url(real_url):
            self.log(f"[开始下载 TikTok 第 {index:03d} 条]", url=real_url)
            err = download_tiktok(
                real_url,
                self.download_path,
                self.update_current_progress_threadsafe,
                index=index,
            )
        else:
            self.log(f"[开始下载 第 {index:03d} 条]", url=real_url)
            err = download_video_http(
                real_url,
                self.download_path,
                self.update_current_progress_threadsafe,
                index=index,
            )

        if err:
            self.failed_downloads += 1
            self.log(f"[下载失败 第 {index:03d} 条] - {err}", url=real_url)
        else:
            self.successful_downloads += 1
            self.log(f"[下载成功 第 {index:03d} 条]", url=real_url)
        return err

    # ===== GUI 更新 =====

    def update_current_progress_threadsafe(self, pct: int):
        self.after(0, lambda: self._update_current_progress(pct))

    def _update_current_progress(self, pct: int):
        self.current_progress["value"] = pct

    def update_total_progress(self):
        self.total_progress["value"] = self.completed_videos
        self.total_progress_label.config(
            text=f"总进度：{self.completed_videos} / {self.total_videos}"
        )

    def on_all_done(self):
        self.start_button.config(state="normal")
        msg = f"全部完成！成功：{self.successful_downloads}，失败：{self.failed_downloads}"
        self.log(msg)
        self.set_status("已完成，可以查看保存目录")
        messagebox.showinfo("完成", msg)


if __name__ == "__main__":
    app = VideoDownloaderApp()
    app.mainloop()
