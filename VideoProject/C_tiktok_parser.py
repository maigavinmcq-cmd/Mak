#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
工程 C · TikTok 单视频解析器（浏览器预览版 · 修正黑屏问题）
依赖：
    pip install yt-dlp requests
"""

import os
import sys
import threading
import time
import webbrowser

import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from yt_dlp import YoutubeDL


# ========= 基础工具 =========

def get_ffmpeg_path():
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "ffmpeg", "ffmpeg.exe")
    else:
        return r"C:\ProgramApply\ffmpeg-7.1.1-essentials_build\bin\ffmpeg.exe"


FFMPEG_PATH = get_ffmpeg_path()


def is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in (url or "").lower()


def extract_tiktok_info(url: str) -> dict:
    """
    使用 yt-dlp 解析 TikTok 视频信息（不下载）
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extractor_args": {
            "tiktok": {
                "comments": ["all"],
                "comment_sort": ["time"],
            }
        },
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info


def download_tiktok_video(url: str, outdir: str) -> str | None:
    """
    稳定版：使用 yt-dlp 下载 TikTok 视频（强制输出 mp4）
    避免：下载成功但找不到文件
    """

    os.makedirs(outdir, exist_ok=True)

    # 先获取 metadata，拿到视频 VID
    info = extract_tiktok_info(url)
    vid = info.get("id") or "tiktok_video"

    # 临时输出模板：先不要写扩展名
    base_path = os.path.join(outdir, vid)

    ydl_opts = {
        "outtmpl": base_path + ".%(ext)s",
        "format": "bv*+ba/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": os.path.dirname(FFMPEG_PATH) if FFMPEG_PATH else None,

        # ffmpeg 转换器：强制最终 mp4
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
    }

    final_file = os.path.join(outdir, f"{vid}.mp4")

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        return f"yt-dlp 错误：{e}"

    # =============== 文件存在检查（核心修复点） ===============
    # 最终 ffmpeg 会合并并输出 base.mp4
    if os.path.exists(final_file):
        return final_file

    # 如果扩展名不同（如 .mkv/.mov），兜底找出文件
    for fname in os.listdir(outdir):
        if fname.startswith(vid + "."):
            return os.path.join(outdir, fname)

    return "下载完成，但未找到最终输出文件，请检查 ffmpeg 是否正常工作"




# ========= GUI 主类 =========

class TikTokParserApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("TikTok 单视频解析器（工程 C · 浏览器预览版）")
        self.geometry("1100x650")
        self.minsize(1000, 620)

        self.download_path = os.path.join(os.path.expanduser("~"), "Downloads")
        self.last_parsed_url: str | None = None
        self.last_parsed_info: dict | None = None
        self.preview_url: str | None = None   # 预览用视频直链

        self._build_ui()

    # ====== UI 搭建 ======

    def _build_ui(self):
        main = ttk.Frame(self, padding=10)
        main.pack(fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")

        path_row = ttk.Frame(top)
        path_row.pack(fill="x", pady=(0, 4))

        ttk.Label(path_row, text="保存路径：").pack(side="left")
        self.path_label = ttk.Label(path_row, text=self.download_path, foreground="#444")
        self.path_label.pack(side="left", padx=(4, 8), fill="x", expand=True)
        ttk.Button(path_row, text="选择保存路径…", command=self.select_download_path, width=18).pack(side="right")

        self.status_var = tk.StringVar(value="状态：就绪，粘贴一个 TikTok 链接试试")
        status_label = ttk.Label(top, textvariable=self.status_var, foreground="#006699")
        status_label.pack(fill="x", pady=(0, 8))

        mid = ttk.Frame(main)
        mid.pack(fill="both", expand=True)

        # 左列：链接输入 + 控制按钮
        left = ttk.LabelFrame(mid, text="操作区")
        left.pack(side="left", fill="y", padx=(0, 6), pady=2)

        input_frame = ttk.LabelFrame(left, text="输入链接")
        input_frame.pack(fill="x", padx=5, pady=(5, 3))

        ttk.Label(input_frame, text="TikTok 视频链接：").pack(side="left", padx=(4, 2))
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(input_frame, textvariable=self.url_var, width=60)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(2, 4))
        self.url_entry.focus()

        self.parse_btn = ttk.Button(input_frame, text="解析信息", command=self.on_parse_clicked, width=14)
        self.parse_btn.pack(side="right", padx=(0, 4))

        btn_frame = ttk.LabelFrame(left, text="视频操作")
        btn_frame.pack(fill="x", padx=5, pady=(3, 5))

        self.open_in_browser_btn = ttk.Button(
            btn_frame, text="在浏览器打开 TikTok 页", command=self.open_in_browser,
            state="disabled", width=22
        )
        self.open_in_browser_btn.pack(fill="x", padx=4, pady=3)

        self.preview_btn = ttk.Button(
            btn_frame, text="预览播放（浏览器直链）", command=self.play_preview,
            state="disabled", width=22
        )
        self.preview_btn.pack(fill="x", padx=4, pady=3)

        self.download_btn = ttk.Button(
            btn_frame, text="下载视频到保存路径", command=self.on_download_clicked,
            state="disabled", width=22
        )
        self.download_btn.pack(fill="x", padx=4, pady=6)

        # 右列：信息 + 评论
        right = ttk.LabelFrame(mid, text="视频信息 & 评论")
        right.pack(side="right", fill="both", expand=True, padx=(6, 0), pady=2)

        info_frame = ttk.LabelFrame(right, text="基本信息")
        info_frame.pack(fill="x", padx=5, pady=(5, 3))

        self.info_text = tk.Text(info_frame, height=12, wrap="word", bg="#111111", fg="#f2f2f2")
        self.info_text.pack(side="left", fill="both", expand=True, padx=3, pady=3)
        self.info_text.configure(state="disabled")

        info_scroll = ttk.Scrollbar(info_frame, orient="vertical", command=self.info_text.yview)
        info_scroll.pack(side="right", fill="y")
        self.info_text.configure(yscrollcommand=info_scroll.set)

        comment_frame = ttk.LabelFrame(right, text="评论（最多展示前 50 条）")
        comment_frame.pack(fill="both", expand=True, padx=5, pady=(3, 5))

        self.comment_text = tk.Text(comment_frame, wrap="word", bg="#181818", fg="#e0e0e0")
        self.comment_text.pack(side="left", fill="both", expand=True, padx=3, pady=3)
        self.comment_text.configure(state="disabled")

        comment_scroll = ttk.Scrollbar(comment_frame, orient="vertical", command=self.comment_text.yview)
        comment_scroll.pack(side="right", fill="y")
        self.comment_text.configure(yscrollcommand=comment_scroll.set)

        bottom = ttk.Label(
            main,
            text="说明：本解析器抓取单条 TikTok 视频的信息和评论；预览播放使用浏览器打开真实视频地址，如果仍黑屏可直接点“在浏览器打开 TikTok 页”。",
            foreground="#777",
            wraplength=1050,
            justify="left",
        )
        bottom.pack(fill="x", pady=(6, 0))

    # ====== 工具 ======

    def set_status(self, text: str):
        self.status_var.set(f"状态：{text}")

    def select_download_path(self):
        new_path = filedialog.askdirectory(
            title="选择保存路径",
            initialdir=self.download_path
        )
        if new_path:
            self.download_path = new_path
            self.path_label.config(text=self.download_path)

    def _write_to_text(self, widget: tk.Text, content: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    # ====== 核心：从 info 中挑一个「有画面」的直链 ======

    def _pick_video_url(self, info: dict) -> str | None:
        """
        从 yt-dlp 的 info 里挑一个“有画面”的播放直链：
          1）如果 info 本身有 vcodec 且不是 none，就用 info["url"]
          2）否则从 formats 里筛选 vcodec != none 的格式，按分辨率/码率选一个最优
          3）如果连一个有画面的都没有，返回 None（说明当前环境只能拿到音频流）
        """
        # 1. info 本身带的视频 url
        if info.get("url") and info.get("vcodec") not in (None, "none"):
            return info["url"]

        fmts = info.get("formats") or []
        if not fmts:
            return None

        # 2. 过滤出“有画面”的格式（vcodec 不是 none）
        video_fmts = [f for f in fmts if f.get("vcodec") not in (None, "none")]
        if not video_fmts:
            # 当前环境真的没有视频流（可能区域/风控），那就老老实实返回 None
            return None

        # 3. 选一个比较好的（按高度+码率排序）
        def fmt_score(f):
            height = f.get("height") or 0
            tbr = f.get("tbr") or 0  # total bitrate
            return (height, tbr)

        best = max(video_fmts, key=fmt_score)
        return best.get("url")

    # ====== 解析入口 ======

    def on_parse_clicked(self):
        url = (self.url_var.get() or "").strip()
        if not url:
            messagebox.showwarning("提示", "请先输入 TikTok 视频链接。")
            return
        if not is_tiktok_url(url):
            messagebox.showwarning("提示", "当前解析器仅支持 TikTok 链接。")
            return

        self.parse_btn.config(state="disabled")
        self.open_in_browser_btn.config(state="disabled")
        self.preview_btn.config(state="disabled")
        self.download_btn.config(state="disabled")

        self.set_status("正在解析视频信息，请稍候……")
        self._write_to_text(self.info_text, "正在解析视频信息，请稍候……")
        self._write_to_text(self.comment_text, "")

        t = threading.Thread(target=self._parse_worker, args=(url,), daemon=True)
        t.start()

    def _parse_worker(self, url: str):
        try:
            info = extract_tiktok_info(url)

            # ✅ 使用新函数挑选「有画面」的视频 URL
            media_url = self._pick_video_url(info)

            self.last_parsed_url = url
            self.last_parsed_info = info
            self.preview_url = media_url

            info_text = self._format_info(info)
            comment_text = self._format_comments(info)

            self.after(0, lambda: self._on_parse_success(info_text, comment_text, bool(media_url)))
        except Exception as e:
            self.after(0, lambda: self._on_parse_fail(str(e)))

    def _on_parse_success(self, info_text: str, comment_text: str, has_preview: bool):
        self._write_to_text(self.info_text, info_text)
        self._write_to_text(self.comment_text, comment_text or "暂无评论或评论未抓取。")

        self.parse_btn.config(state="normal")
        self.open_in_browser_btn.config(state="normal")
        self.download_btn.config(state="normal")
        if has_preview:
            self.preview_btn.config(state="normal")
        else:
            self.preview_btn.config(state="disabled")

        self.set_status("解析成功，可以查看信息 / 评论，并选择播放或下载。")

    def _on_parse_fail(self, err: str):
        self._write_to_text(self.info_text, f"解析失败：{err}")
        self._write_to_text(self.comment_text, "")

        self.parse_btn.config(state="normal")
        self.open_in_browser_btn.config(state="disabled")
        self.download_btn.config(state="disabled")
        self.preview_btn.config(state="disabled")

        self.set_status("解析失败，请检查链接或网络。")
        messagebox.showerror("解析失败", f"解析失败：{err}")

    # ====== 信息格式化 ======

    def _format_info(self, info: dict) -> str:
        title = info.get("title") or ""
        desc = info.get("description") or ""
        uploader = info.get("uploader") or info.get("uploader_id") or ""
        vid = info.get("id") or ""
        like_count = info.get("like_count")
        comment_count = info.get("comment_count")
        share_count = info.get("share_count") or info.get("repost_count")
        view_count = info.get("view_count")
        tags = info.get("tags") or []
        upload_date = info.get("upload_date")

        lines = []
        lines.append("【平台】TikTok")
        if vid:
            lines.append(f"【视频ID】{vid}")
        if uploader:
            lines.append(f"【创作者】{uploader}")
        if upload_date:
            lines.append(f"【发布时间】{upload_date}")
        if title:
            lines.append(f"【标题】{title}")
        if desc and desc != title:
            lines.append(f"【文案】{desc}")
        if tags:
            lines.append("【标签】" + "，".join(tags))

        stat_parts = []
        if view_count is not None:
            stat_parts.append(f"播放量 {view_count}")
        if like_count is not None:
            stat_parts.append(f"点赞 {like_count}")
        if comment_count is not None:
            stat_parts.append(f"评论 {comment_count}")
        if share_count is not None:
            stat_parts.append(f"转发/分享 {share_count}")
        if stat_parts:
            lines.append("【数据】" + " · ".join(stat_parts))

        return "\n".join(lines)

    def _format_comments(self, info: dict, max_comments: int = 50) -> str:
        comments = info.get("comments") or []
        if not comments:
            return ""

        lines = []
        for i, c in enumerate(comments[:max_comments], start=1):
            author = c.get("author") or ""
            text = c.get("text") or ""
            likes = c.get("like_count")
            ts = c.get("timestamp")

            header = f"#{i} {author}" if author else f"#{i}"
            if likes is not None:
                header += f"  ❤️{likes}"
            lines.append(header)
            lines.append(text)
            if ts:
                lines.append(f"时间戳：{ts}")
            lines.append("-" * 40)

        return "\n".join(lines)

    # ====== 按钮：浏览器 / 预览 / 下载 ======

    def open_in_browser(self):
        if self.last_parsed_url:
            webbrowser.open(self.last_parsed_url)

    def play_preview(self):
        if not self.preview_url:
            messagebox.showwarning("提示", "当前没有可预览的视频直链，请先解析成功一次。")
            return
        try:
            webbrowser.open(self.preview_url)
        except Exception as e:
            messagebox.showerror("预览失败", f"无法在浏览器中打开视频：{e}")

    def on_download_clicked(self):
        if not self.last_parsed_url:
            messagebox.showwarning("提示", "请先解析一个视频。")
            return

        url = self.last_parsed_url
        self.set_status("正在下载视频，请稍候……")
        self.download_btn.config(state="disabled")
        self.parse_btn.config(state="disabled")

        t = threading.Thread(target=self._download_worker, args=(url,), daemon=True)
        t.start()

    def _download_worker(self, url: str):
        try:
            path = download_tiktok_video(url, self.download_path)
            if path:
                self.after(0, lambda: self._download_success(path))
            else:
                self.after(0, lambda: self._download_fail("下载完成，但未找到输出文件。"))
        except Exception as e:
            self.after(0, lambda: self._download_fail(str(e)))

    def _download_success(self, path: str):
        self.set_status("下载完成。")
        self.download_btn.config(state="normal")
        self.parse_btn.config(state="normal")
        messagebox.showinfo("完成", f"视频已下载到：\n{path}")

    def _download_fail(self, err: str):
        self.set_status("下载失败。")
        self.download_btn.config(state="normal")
        self.parse_btn.config(state="normal")
        messagebox.showerror("下载失败", f"下载失败：{err}")


def main():
    app = TikTokParserApp()
    app.mainloop()


if __name__ == "__main__":
    main()
