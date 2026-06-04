import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
import yt_dlp
import threading
import os
import datetime
import re
import requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

class TikTokDownloaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("批量下载 TikTok 视频")
        self.root.geometry("620x520")

        self.filename_counter = defaultdict(int)
        self.total_links = 0
        self.completed_count = 0

        # 链接输入框
        self.links_label = tk.Label(root, text="请输入多个 TikTok 视频链接（每行一个）:")
        self.links_label.pack(pady=5)

        self.text_area = scrolledtext.ScrolledText(root, height=8)
        self.text_area.pack(fill="both", padx=10, pady=5, expand=True)

        # 链接统计
        self.link_stats_label = tk.Label(root, text="共输入：0 条，去重后：0 条", fg="blue")
        self.link_stats_label.pack(pady=2)

        self.count_label = tk.Label(root, text="准备下载视频数: 0 / 已完成: 0", fg="green")
        self.count_label.pack(pady=2)

        # 保存路径选择
        self.dir_label = tk.Label(root, text="保存路径：")
        self.dir_label.pack()

        self.dir_var = tk.StringVar()
        self.dir_entry = tk.Entry(root, textvariable=self.dir_var, width=50)
        self.dir_entry.pack(pady=2)

        self.browse_button = tk.Button(root, text="选择文件夹", command=self.select_directory)
        self.browse_button.pack(pady=5)

        # 日志窗口
        '''self.log_label = tk.Label(root, text="下载日志：")
        self.log_label.pack()

        self.log_text = scrolledtext.ScrolledText(root, height=8, state='disabled', bg="#f0f0f0")
        self.log_text.pack(fill="both", padx=10, pady=5, expand=True)'''

        self.download_button = tk.Button(root, text="开始下载", command=self.start_download_thread)
        self.download_button.pack(pady=10)

    '''def log_to_ui(self, message):
        self.log_text.config(state='normal')
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')'''

    def select_directory(self):
        folder_selected = filedialog.askdirectory()
        if folder_selected:
            self.dir_var.set(folder_selected)

    def start_download_thread(self):
        thread = threading.Thread(target=self.download_videos)
        thread.start()

    def update_count_label(self):
        self.count_label.config(text=f"准备下载视频数: {self.total_links} / 已完成: {self.completed_count}")

    def sanitize_filename(self, name):
        return re.sub(r'[\\/:*?"<>|]', "_", name).strip()

    def resolve_short_link(self, url):
        try:
            response = requests.head(url, allow_redirects=True, timeout=10)
            final_url = response.url
            if 'tiktok.com/@' in final_url and '/video/' in final_url:
                return final_url
            else:
                return url
        except Exception as e:
            self.log_to_ui(f"短链解析失败：{url} 错误：{e}")
            return url

    def resolve_short_links_async(self, links):
        resolved_links = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(self.resolve_short_link, link): link for link in links}
            for future in as_completed(futures):
                try:
                    resolved_links.append(future.result())
                except Exception as e:
                    link = futures[future]
                    self.log_to_ui(f"短链解析失败：{link} 错误：{e}")
                    resolved_links.append(link)
        return resolved_links

    def generate_unique_filename(self, save_path):
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        base = f"tiktok_{timestamp}"
        filename = base
        counter = 1
        while os.path.exists(os.path.join(save_path, filename + ".mp4")):
            filename = f"{base}_{counter}"
            counter += 1
        return filename + ".%(ext)s"

    def download_single_video(self, url, save_path):
        filename = self.generate_unique_filename(save_path)
        ydl_opts = {
            'outtmpl': os.path.join(save_path, filename),
            'quiet': True,
            'noplaylist': True,
            'format': 'mp4',
            'retries': 3,  # ✅ 重试机制
            'concurrent_fragment_downloads': 3,  # ✅ 提升下载稳定性
            'fragment_retries': 3,
            'nocheckcertificate': True,  # 有时 TikTok SSL 会报错
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    def download_videos(self):
        raw_links = self.text_area.get("1.0", "end").strip().splitlines()
        links = [link.strip() for link in raw_links if link.strip()]
        short_links = [l for l in links if "tiktok.com/t/" in l]
        normal_links = [l for l in links if "tiktok.com/@/" in l or "tiktok.com/video/" in l]

        print("正在解析短链，请稍候...")
        resolved_short_links = self.resolve_short_links_async(short_links)
        all_links = normal_links + resolved_short_links

        # 去重
        seen = set()
        unique_links = []
        for link in all_links:
            if link not in seen:
                seen.add(link)
                unique_links.append(link)

        self.link_stats_label.config(text=f"共输入：{len(links)} 条，去重后：{len(unique_links)} 条")

        save_path = self.dir_var.get()
        if not unique_links or not save_path:
            messagebox.showwarning("提示", "请填写链接并选择保存路径")
            return

        self.total_links = len(unique_links)
        self.completed_count = 0
        self.update_count_label()

        for i, url in enumerate(unique_links, 1):
            try:
                self.download_single_video(url, save_path)
                self.completed_count += 1
                self.update_count_label()
                print(f"[{i}] 下载完成：{url}")
            except Exception as e:
                print(f"[{i}] 下载失败：{url} 错误：{e}")

        messagebox.showinfo("完成", f"共处理 {self.total_links} 个链接，成功下载 {self.completed_count} 个视频。")

if __name__ == "__main__":
    root = tk.Tk()
    app = TikTokDownloaderApp(root)
    root.mainloop()
