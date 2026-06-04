#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
并发批量视频下载 GUI（yt-dlp）
- 并发线程池
- 队列保存/加载（JSON）
- 去重链接（默认开，修复了 sv= 被误判为 YouTube v= 的问题）
- 快捷开关：仅音频 / 仅字幕 / 首选无水印（TikTok 尝试）
- 总体进度条 + 当前任务进度条
"""

import os
import re
import sys
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote   # 修复去重用

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

try:
    from yt_dlp import YoutubeDL
except Exception as e:
    raise SystemExit("未安装 yt-dlp，请先：pip install -U yt-dlp") from e

APP_TITLE = "麦组批量视频下载器 Pro"
APP_GEOMETRY = "1060x780"


# --- 让打包版/源码版都能找到 ffmpeg（按你的路径） ---
def _patch_ffmpeg_path():
    base = getattr(sys, "_MEIPASS", os.path.abspath("."))
    ffdir = os.path.join(base, "ffmpeg")
    if os.path.isdir(ffdir):
        os.environ["PATH"] = ffdir + os.pathsep + os.environ.get("PATH", "")

    # 你的本机路径作为 fallback
    if os.name == "nt":
        fallback = r"C:\ProgramApply\ffmpeg-7.1.1-essentials_build\bin"
        if os.path.isdir(fallback):
            os.environ["PATH"] = fallback + os.pathsep + os.environ.get("PATH", "")

_patch_ffmpeg_path()
# ------------------------------------------------------


def parse_urls_from_text(text: str):
    lines = [ln.strip() for ln in text.splitlines()]
    urls = []
    for ln in lines:
        if not ln:
            continue
        if re.match(r"^(https?://|www\.)", ln, re.I):
            if ln.lower().startswith("www."):
                ln = "https://" + ln
            urls.append(ln)
    return urls


def canonical_key(url: str) -> str:
    """
    生成可去重的“规范键”
    - 仅在对应域名下才匹配各平台 ID（避免把 ?sv= 误判成 YouTube v=）
    - 对 videos.openai.com 的 Azure 路径，提取 files/<ID>%2Fraw 里的 <ID>
    - 其余域名：返回 scheme://host/path?排序后的查询串 作为兜底键
    """
    u = url.strip()
    u = re.sub(r"#.+$", "", u)  # 去掉哈希
    p = urlparse(u)
    host = p.netloc.lower().lstrip("www.")
    path = p.path
    qs = parse_qs(p.query, keep_blank_values=True)

    # --- YouTube / youtu.be（仅在对应域名下判断）---
    if "youtube.com" in host:
        v = qs.get("v", [None])[0]
        if v:
            return f"yt:{v}"
        m = re.match(r"/shorts/([A-Za-z0-9_-]{6,})", path)
        if m:
            return f"yt:{m.group(1)}"
        m = re.match(r"/embed/([A-Za-z0-9_-]{6,})", path)
        if m:
            return f"yt:{m.group(1)}"
    if "youtu.be" in host:
        m = re.match(r"/([A-Za-z0-9_-]{6,})", path)
        if m:
            return f"yt:{m.group(1)}"

    # --- TikTok（含 vm 短链）---
    if "tiktok.com" in host:
        m = re.search(r"/video/(\d+)", path)
        if m:
            return f"tt:{m.group(1)}"
        if "vm.tiktok.com" in host:
            code = path.strip("/")
            if code:
                return f"ttvm:{code}"

    # --- Instagram ---
    if "instagram.com" in host:
        m = re.match(r"/(?:reel|p|tv)/([A-Za-z0-9\-_]+)/?", path)
        if m:
            return f"ig:{m.group(1)}"

    # --- Twitter/X ---
    if host.endswith("twitter.com") or host == "x.com":
        m = re.match(r"/[^/]+/status/(\d+)", path)
        if m:
            return f"x:{m.group(1)}"

    # --- videos.openai.com Azure 文件直链（按 files/<ID> 去重）---
    if host == "videos.openai.com" and "/az/files/" in path:
        seg = path.split("/az/files/")[-1]  # 0000-...%2Fraw
        seg = seg.split("/")[0]
        seg = unquote(seg)                 # 还原 %2F
        file_id = seg.split("/")[0]        # 取 <ID>
        if file_id:
            return f"voaif:{file_id}"

    # --- 兜底：host+path + 排序后的 query ---
    base = f"{p.scheme}://{host}{path}"
    if p.query:
        parts = []
        for k, vals in sorted(qs.items()):
            for v in vals:
                parts.append(f"{k}={v}")
        return base + "?" + "&".join(parts)
    return base


class DownloaderGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(APP_GEOMETRY)
        self.minsize(980, 660)

        # 状态
        self.executor = None
        self.stop_event = threading.Event()
        self.downloading = False

        # 绑定变量
        self.var_outdir = tk.StringVar(value=os.path.join(os.getcwd(), "downloads"))
        self.var_proxy = tk.StringVar(value="")
        self.var_cookies = tk.StringVar(value="")
        self.var_limit = tk.StringVar(value="")
        self.var_retries = tk.IntVar(value=5)
        self.var_workers = tk.IntVar(value=3)

        self.var_audio_only = tk.BooleanVar(value=False)
        self.var_sub_only = tk.BooleanVar(value=False)
        self.var_prefer_no_wm = tk.BooleanVar(value=True)
        self.var_dedup = tk.BooleanVar(value=True)

        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        frm_top = ttk.LabelFrame(self, text="下载参数")
        frm_top.pack(fill="x", padx=10, pady=(10, 6))

        r1 = ttk.Frame(frm_top); r1.pack(fill="x", padx=8, pady=6)
        ttk.Label(r1, text="输出目录：").pack(side="left")
        ttk.Entry(r1, textvariable=self.var_outdir).pack(side="left", fill="x", expand=True, padx=(6, 6))
        ttk.Button(r1, text="选择…", command=self.choose_outdir).pack(side="left")

        r2 = ttk.Frame(frm_top); r2.pack(fill="x", padx=8, pady=6)
        ttk.Label(r2, text="代理：").pack(side="left")
        ttk.Entry(r2, textvariable=self.var_proxy, width=22).pack(side="left", padx=(6, 12))
        ttk.Label(r2, text="Cookies：").pack(side="left")
        ttk.Entry(r2, textvariable=self.var_cookies, width=30).pack(side="left", padx=(6, 6))
        ttk.Button(r2, text="选择…", command=self.choose_cookies).pack(side="left", padx=(0, 12))
        ttk.Label(r2, text="限速：").pack(side="left")
        ttk.Entry(r2, textvariable=self.var_limit, width=10).pack(side="left", padx=(6, 12))
        ttk.Label(r2, text="重试：").pack(side="left")
        ttk.Spinbox(r2, from_=0, to=20, textvariable=self.var_retries, width=5).pack(side="left", padx=(6, 20))
        ttk.Label(r2, text="并发线程：").pack(side="left")
        ttk.Spinbox(r2, from_=1, to=12, textvariable=self.var_workers, width=5).pack(side="left", padx=(6, 0))

        r3 = ttk.Frame(frm_top); r3.pack(fill="x", padx=8, pady=(0,6))
        ttk.Checkbutton(r3, text="仅音频（提取 mp3）", variable=self.var_audio_only,
                        command=self._mutual_exclude_audio_sub).pack(side="left")
        ttk.Checkbutton(r3, text="仅字幕（只下载字幕）", variable=self.var_sub_only,
                        command=self._mutual_exclude_audio_sub).pack(side="left", padx=(18,0))
        ttk.Checkbutton(r3, text="首选无水印（TikTok 尝试）", variable=self.var_prefer_no_wm).pack(side="left", padx=(18,0))
        ttk.Checkbutton(r3, text="自动去重链接（默认开）", variable=self.var_dedup).pack(side="left", padx=(18,0))

        # 链接区
        frm_urls = ttk.LabelFrame(self, text="视频链接（每行一个）")
        frm_urls.pack(fill="both", expand=False, padx=10, pady=(0, 6))
        self.txt_urls = ScrolledText(frm_urls, height=10, wrap="none")
        self.txt_urls.pack(fill="both", expand=True, padx=8, pady=8)

        # 操作按钮
        row_btn = ttk.Frame(self); row_btn.pack(fill="x", padx=10, pady=(0,6))
        ttk.Button(row_btn, text="导入链接文件…", command=self.import_urls_file).pack(side="left")
        ttk.Button(row_btn, text="去重（预处理）", command=self.dedup_preview).pack(side="left", padx=(8,0))
        ttk.Button(row_btn, text="保存队列…", command=self.save_queue).pack(side="left", padx=(8,0))
        ttk.Button(row_btn, text="加载队列…", command=self.load_queue).pack(side="left", padx=(8,0))
        ttk.Button(row_btn, text="清空链接", command=self.clear_urls).pack(side="left", padx=(8,0))
        ttk.Button(row_btn, text="开始下载", command=self.start_download).pack(side="right")
        ttk.Button(row_btn, text="停止（不再提交新任务）", command=self.stop_download).pack(side="right", padx=(0,8))

        # 进度区：总进度 + 当前任务进度
        frm_prog = ttk.LabelFrame(self, text="进度")
        frm_prog.pack(fill="x", padx=10, pady=(0,6))

        rp1 = ttk.Frame(frm_prog); rp1.pack(fill="x", padx=8, pady=4)
        ttk.Label(rp1, text="总体进度：").pack(side="left")
        self.pb_total = ttk.Progressbar(rp1, orient="horizontal", length=420,
                                        mode="determinate", maximum=100)
        self.pb_total.pack(side="left", padx=(6, 12))
        self.lbl_total = ttk.Label(rp1, text="0 / 0")
        self.lbl_total.pack(side="left")

        rp2 = ttk.Frame(frm_prog); rp2.pack(fill="x", padx=8, pady=4)
        ttk.Label(rp2, text="当前任务：").pack(side="left")
        self.pb_current = ttk.Progressbar(rp2, orient="horizontal", length=420,
                                          mode="determinate", maximum=100)
        self.pb_current.pack(side="left", padx=(6, 12))
        self.lbl_current = ttk.Label(rp2, text="等待开始…")
        self.lbl_current.pack(side="left")

        # 日志
        frm_log = ttk.LabelFrame(self, text="日志")
        frm_log.pack(fill="both", expand=True, padx=10, pady=(0,10))
        self.txt_log = ScrolledText(frm_log, wrap="word", height=14)
        self.txt_log.pack(fill="both", expand=True, padx=8, pady=8)
        self._log("准备就绪。")

    # ---------- 互斥：仅音频 vs 仅字幕 ----------
    def _mutual_exclude_audio_sub(self):
        if self.var_audio_only.get() and self.var_sub_only.get():
            # 简单策略：如果两者都被选中，就关掉“仅字幕”
            self.var_sub_only.set(False)

    # ---------- 辅助 UI ----------
    def choose_outdir(self):
        d = filedialog.askdirectory(title="选择输出目录",
                                    initialdir=self.var_outdir.get() or os.getcwd())
        if d:
            self.var_outdir.set(d)

    def choose_cookies(self):
        p = filedialog.askopenfilename(title="选择 cookies.txt",
                                       filetypes=[("Text files","*.txt"),("All files","*.*")])
        if p:
            self.var_cookies.set(p)

    def import_urls_file(self):
        p = filedialog.askopenfilename(title="选择包含链接的文本文件",
                                       filetypes=[("Text files","*.txt"),("All files","*.*")])
        if not p:
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = f.read()
            self.txt_urls.insert(
                "end",
                ("\n" if self.txt_urls.get("1.0","end").strip() else "") + data.strip() + "\n"
            )
            self._log(f"已导入链接文件：{p}")
        except Exception as e:
            messagebox.showerror("错误", f"读取失败：{e}")

    def clear_urls(self):
        self.txt_urls.delete("1.0", "end")

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_log.insert("end", f"[{ts}] {msg}\n")
        self.txt_log.see("end")

    # ---------- 队列持久化 ----------
    def save_queue(self):
        urls = parse_urls_from_text(self.txt_urls.get("1.0", "end"))
        if not urls:
            messagebox.showinfo("提示", "没有可保存的链接。")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("JSON","*.json"),("All files","*.*")],
                                            title="保存队列为 JSON")
        if not path:
            return
        data = {"urls": urls}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._log(f"已保存队列：{path}")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败：{e}")

    def load_queue(self):
        path = filedialog.askopenfilename(title="加载队列 JSON",
                                          filetypes=[("JSON","*.json"),("All files","*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            urls = data.get("urls", [])
            if not urls:
                messagebox.showinfo("提示", "该文件未包含 urls 列表。")
                return
            block = "\n".join(urls)
            self.txt_urls.insert(
                "end",
                ("\n" if self.txt_urls.get("1.0","end").strip() else "") + block + "\n"
            )
            self._log(f"已加载队列：{path}（{len(urls)} 条）")
        except Exception as e:
            messagebox.showerror("错误", f"读取失败：{e}")

    # ---------- 去重预览按钮 ----------
    def dedup_preview(self):
        raw = parse_urls_from_text(self.txt_urls.get("1.0", "end"))
        if not raw:
            messagebox.showinfo("提示", "没有可去重的链接。")
            return
        keys = set()
        uniq = []
        for u in raw:
            k = canonical_key(u)
            if k not in keys:
                keys.add(k)
                uniq.append(u)
        removed = len(raw) - len(uniq)
        self.txt_urls.delete("1.0", "end")
        self.txt_urls.insert("1.0", "\n".join(uniq) + "\n")
        self._log(f"去重完成：移除 {removed} 条，剩余 {len(uniq)} 条。")

    # ---------- 下载 ----------
    def start_download(self):
        if self.downloading:
            messagebox.showinfo("提示", "任务进行中。")
            return

        urls = parse_urls_from_text(self.txt_urls.get("1.0", "end"))
        if not urls:
            messagebox.showwarning("提示", "请粘贴或导入链接（每行一个）。")
            return

        if self.var_dedup.get():
            keys = set()
            uniq = []
            for u in urls:
                k = canonical_key(u)
                if k not in keys:
                    keys.add(k)
                    uniq.append(u)
            if len(uniq) != len(urls):
                self._log(f"自动去重：移除 {len(urls)-len(uniq)} 条重复。")
            urls = uniq

        outdir = self.var_outdir.get().strip()
        if not outdir:
            messagebox.showwarning("提示", "请选择输出目录。")
            return
        os.makedirs(outdir, exist_ok=True)

        self.stop_event.clear()
        self.downloading = True

        # 日志文件
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.success_log = os.path.join(outdir, f"_download_success_{ts}.log")
        self.fail_log = os.path.join(outdir, f"_download_fail_{ts}.log")
        open(self.success_log, "a", encoding="utf-8").close()
        open(self.fail_log, "a", encoding="utf-8").close()

        # 统计
        self.total = len(urls)
        self.done = 0
        self.succ = 0
        self.fail = 0
        self.pb_total["value"] = 0
        self.lbl_total.config(text=f"0 / {self.total}")
        self.pb_current["value"] = 0
        self.lbl_current.config(text="等待开始…")

        workers = max(1, int(self.var_workers.get() or 1))
        self._log(f"开始下载：{self.total} 条链接，并发 {workers}。")
        self.executor = ThreadPoolExecutor(max_workers=workers)

        self.futures = []
        for u in urls:
            if self.stop_event.is_set():
                break
            fut = self.executor.submit(self._download_one, u, outdir)
            fut.add_done_callback(self._on_one_done)
            self.futures.append(fut)

        threading.Thread(target=self._watch_executor, daemon=True).start()

    def stop_download(self):
        if not self.downloading:
            self._log("当前没有下载任务。")
            return
        self.stop_event.set()
        self._log("已请求停止：不会再提交新的任务，正在等待已提交的任务结束…")

    def _append_line(self, path: str, line: str):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _on_one_done(self, fut):
        try:
            ok, url, err = fut.result()
        except Exception as e:
            ok, url, err = False, "未知", repr(e)

        self.done += 1
        if ok:
            self.succ += 1
            self._append_line(self.success_log, url)
            self._log(f"✓ 成功：{url}")
        else:
            self.fail += 1
            self._append_line(self.fail_log, f"{url}\t{err}")
            self._log(f"✗ 失败：{url} | {err}")

        pct = int(self.done * 100 / self.total) if self.total else 0
        self.after(0, lambda: (
            self.pb_total.configure(value=pct),
            self.lbl_total.config(text=f"{self.done} / {self.total}")
        ))

    def _watch_executor(self):
        for fut in as_completed(self.futures):
            pass
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.downloading = False
        self._log(f"任务结束：成功 {self.succ}，失败 {self.fail}")
        self.after(0, lambda: self.pb_total.configure(value=100))
        self.after(0, lambda: self.lbl_total.config(text=f"{self.total} / {self.total}"))
        self.after(0, lambda: self.lbl_current.config(text="就绪"))
        self.after(0, lambda: self.pb_current.configure(value=0))

    # ---------- 单个下载任务 + 进度 hook ----------
    def _download_one(self, url: str, outdir: str):
        try:
            ydl_opts = self._build_ydl_opts(outdir, url)
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            return True, url, None
        except Exception as e:
            return False, url, repr(e)

    def _build_ydl_opts(self, outdir: str, url: str):
        outtmpl = os.path.join(
            outdir,
            "%(upload_date>%Y-%m-%d)s_%(extractor_key)s_%(uploader|creator|channel)s_%(title).120B_%(id)s_%(height>0)sp.%(ext)s"
        )

        audio_only = self.var_audio_only.get()
        sub_only = self.var_sub_only.get()

        opts = {
            "outtmpl": outtmpl,
            "paths": {"home": outdir},
            "continuedl": True,
            "retries": int(self.var_retries.get() or 0),
            "fragment_retries": int(self.var_retries.get() or 0),
            "concurrent_fragment_downloads": 5,
            "restrictfilenames": False,
            "windowsfilenames": True,
            "trim_file_name": 220,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "overwrites": False,
        }

        proxy = self.var_proxy.get().strip()
        if proxy:
            opts["proxy"] = proxy
        cookies = self.var_cookies.get().strip()
        if cookies:
            opts["cookiefile"] = cookies
        limit_rate = self.var_limit.get().strip()
        if limit_rate:
            opts["ratelimit"] = limit_rate

        if sub_only:
            opts.update({
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["all"],
                "subtitlesformat": "srt/best",
            })
        elif audio_only:
            opts.update({
                "format": "bestaudio/b",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "0",
                }],
                "merge_output_format": None,
            })
        else:
            opts.update({
                "format": "bv*+ba/b",
                "merge_output_format": "mp4",
            })

        if self.var_prefer_no_wm.get():
            opts.setdefault("extractor_args", {})
            tta = opts["extractor_args"].setdefault("tiktok", {})
            tta["download_addr"] = ["1"]

        # 进度 hook（当前任务进度条）
        def hook(d, _url=url):
            status = d.get("status")
            if status == "downloading":
                total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                pct = int(downloaded * 100 / total_bytes) if total_bytes else 0
                speed = d.get("speed") or 0
                eta = d.get("eta")
                self._update_current_progress(_url, pct, speed, eta)
            elif status == "finished":
                self._update_current_progress(_url, 100, None, None, extra="处理中…")

        opts["progress_hooks"] = [hook]

        return opts

    def _update_current_progress(self, url: str, pct: int, speed, eta, extra: str = ""):
        # 只展示 URL 末尾 30 个字符，防止太长
        short = url[-40:] if len(url) > 40 else url
        txt = f"{pct}% ({short})"
        if speed:
            spd = f"{speed/1024/1024:.2f} MB/s"
            txt += f" | {spd}"
        if eta:
            txt += f" | 剩余约 {eta}s"
        if extra:
            txt += f" | {extra}"

        pct = max(0, min(100, pct))
        self.after(0, lambda: (
            self.pb_current.configure(value=pct),
            self.lbl_current.config(text=txt)
        ))


def main():
    app = DownloaderGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
