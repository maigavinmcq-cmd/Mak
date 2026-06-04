#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import tkinter as tk
from tkinter import ttk, messagebox

def launch_batch_downloader(root):
    """
    启动工程 B：Sora/TikTok 批量下载器
    """
    try:
        # 延迟 import，避免一启动就加载所有东西
        from B_sora2_video_downloader import VideoDownloaderApp
    except ImportError as e:
        messagebox.showerror("错误", f"导入批量下载器失败：{e}\n请确认 B_sora2_video_downloader.py 是否和本文件在同一目录。")
        return

    # 关闭当前入口窗口，再开启 B 的主窗口
    root.destroy()
    app = VideoDownloaderApp()
    app.mainloop()


def launch_tiktok_parser(root):
    """
    启动工程 C：TikTok 单视频解析器
    """
    try:
        from C_tiktok_parser import TikTokParserApp
    except ImportError as e:
        messagebox.showerror("错误", f"导入 TikTok 解析器失败：{e}\n请确认 C_tiktok_parser.py 是否和本文件在同一目录。")
        return

    root.destroy()
    app = TikTokParserApp()
    app.mainloop()


def main():
    root = tk.Tk()
    root.title("视频工具合集 · 主入口")
    root.geometry("420x260")
    root.resizable(False, False)

    # 外层
    main_frame = ttk.Frame(root, padding=20)
    main_frame.pack(fill="both", expand=True)

    title_label = ttk.Label(
        main_frame,
        text="请选择要使用的工具",
        font=("Microsoft YaHei", 14, "bold"),
        anchor="center"
    )
    title_label.pack(fill="x", pady=(0, 10))

    desc_label = ttk.Label(
        main_frame,
        text=(
            "主入口\n\n"
            "· 1：Sora / TikTok 视频批量下载工具\n"
            "· 2：TikTok 单视频解析（预览/播放/下载）"
        ),
        justify="left"
    )
    desc_label.pack(fill="x", pady=(0, 15))

    # 按钮区
    btn_frame = ttk.Frame(main_frame)
    btn_frame.pack(fill="x", pady=(0, 10))

    btn_batch = ttk.Button(
        btn_frame,
        text="打开 Sora / TikTok 批量下载器",
        command=lambda: launch_batch_downloader(root),
        width=40
    )
    btn_batch.pack(pady=5)

    btn_parser = ttk.Button(
        btn_frame,
        text="打开 TikTok 单视频解析器",
        command=lambda: launch_tiktok_parser(root),
        width=40
    )
    btn_parser.pack(pady=5)

    note_label = ttk.Label(
        main_frame,
        text="提示：一次启动选择其中一个工具即可使用。\n如需切换工具，关闭后重新运行本入口程序。",
        foreground="#666",
        justify="center"
    )
    note_label.pack(fill="x", pady=(10, 0))

    root.mainloop()


if __name__ == "__main__":
    main()
