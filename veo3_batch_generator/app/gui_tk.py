from __future__ import annotations

import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from app.api.image_api import generate_image_from_product_image
from app.api.video_api import create_video_task, poll_video_task
from app.config import load_config
from app.excel_loader import load_tasks_from_excel
from app.file_utils import (
    download_video_to_path,
    ensure_output_dirs,
    existing_grouped_video_path,
    existing_video_path,
    find_product_image,
    grouped_video_output_path,
    image_output_path,
    netdisk_image_output_path,
    netdisk_video_output_path,
    open_path,
    video_output_path,
)
from app.logger import setup_logger
from app.models.task import TaskStatus
from app.task_manager import TaskManager


class TkApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Veo3 视频批量生成工具")
        self.geometry("1450x850")
        self.config_data = load_config()
        ensure_output_dirs(self.config_data.output_dir)
        self.logger, _ = setup_logger(self.config_data.output_dir)
        self.manager = TaskManager(self.config_data.state_path)
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.worker_thread: threading.Thread | None = None
        self._build()
        self._restore_state()

    def _build(self) -> None:
        config_frame = ttk.LabelFrame(self, text="配置")
        config_frame.pack(fill="x", padx=8, pady=6)
        self.excel_var = tk.StringVar(value=str(self.config_data.excel_path))
        self.output_var = tk.StringVar(value=str(self.config_data.output_dir))
        self.concurrency_var = tk.IntVar(value=1)
        self.retry_var = tk.IntVar(value=self.config_data.retry_count)
        self.poll_var = tk.IntVar(value=self.config_data.poll_interval_seconds)
        self.max_poll_var = tk.IntVar(value=self.config_data.max_poll_count)
        self.regenerate_image_var = tk.BooleanVar(value=False)
        self.table_text_color_var = tk.StringVar(value=self.config_data.table_text_color)

        ttk.Label(config_frame, text="任务 Excel 文件路径").grid(row=0, column=0, sticky="w")
        ttk.Entry(config_frame, textvariable=self.excel_var, width=110).grid(row=0, column=1, sticky="ew")
        ttk.Button(config_frame, text="选择 Excel", command=self.choose_excel).grid(row=0, column=2)
        ttk.Label(config_frame, text="输出目录").grid(row=1, column=0, sticky="w")
        ttk.Entry(config_frame, textvariable=self.output_var, width=110).grid(row=1, column=1, sticky="ew")
        ttk.Button(config_frame, text="选择输出目录", command=self.choose_output).grid(row=1, column=2)
        ttk.Label(config_frame, text="并发数量").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(config_frame, from_=1, to=50, textvariable=self.concurrency_var, width=8).grid(row=2, column=1, sticky="w")
        ttk.Label(config_frame, text="失败重试次数").grid(row=2, column=1, sticky="w", padx=120)
        ttk.Spinbox(config_frame, from_=0, to=10, textvariable=self.retry_var, width=8).grid(row=2, column=1, sticky="w", padx=220)
        ttk.Label(config_frame, text="轮询间隔秒数").grid(row=2, column=1, sticky="w", padx=340)
        ttk.Spinbox(config_frame, from_=1, to=60, textvariable=self.poll_var, width=8).grid(row=2, column=1, sticky="w", padx=450)
        ttk.Label(config_frame, text="最大轮询次数").grid(row=2, column=1, sticky="w", padx=560)
        ttk.Spinbox(config_frame, from_=1, to=1000, textvariable=self.max_poll_var, width=8).grid(row=2, column=1, sticky="w", padx=660)
        ttk.Checkbutton(config_frame, text="重新生成已有图片", variable=self.regenerate_image_var).grid(row=3, column=0, sticky="w", pady=4)
        ttk.Label(config_frame, text="表格字体颜色").grid(row=3, column=1, sticky="w")
        ttk.Entry(config_frame, textvariable=self.table_text_color_var, width=12).grid(row=3, column=1, sticky="w", padx=100)
        ttk.Button(config_frame, text="应用颜色", command=self.apply_table_text_color).grid(row=3, column=1, sticky="w", padx=210)
        ttk.Button(config_frame, text="加载任务", command=self.load_tasks).grid(row=4, column=0, pady=4)
        ttk.Button(config_frame, text="检查任务", command=self.check_tasks).grid(row=4, column=1, sticky="w", pady=4)
        config_frame.columnconfigure(1, weight=1)

        stats_frame = ttk.LabelFrame(self, text="任务统计")
        stats_frame.pack(fill="x", padx=8, pady=4)
        self.stats_var = tk.StringVar(value="总任务数: 0  待提交: 0  图片生成中: 0  视频已提交: 0  视频轮询中: 0  已完成: 0  失败: 0  超时: 0  已跳过: 0")
        ttk.Label(stats_frame, textvariable=self.stats_var).pack(anchor="w", padx=8, pady=4)

        columns = ("任务名称", "PID", "网盘路径", "产品图状态", "图片提示词", "视频提示词", "任务状态", "生成图片路径", "视频任务ID", "视频提交时间", "轮询次数", "视频链接", "错误信息")
        self.table = ttk.Treeview(self, columns=columns, show="headings", height=18)
        self.table_style = ttk.Style(self)
        self.apply_table_text_color()
        for col in columns:
            self.table.heading(col, text=col)
            self.table.column(col, width=140)
        self.table.column("网盘路径", width=260)
        self.table.column("图片提示词", width=220)
        self.table.column("视频提示词", width=220)
        self.table.pack(fill="both", expand=True, padx=8, pady=4)
        self.table.bind("<Double-1>", self.open_selected_cell)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=8, pady=4)
        ttk.Button(btn_frame, text="开始执行", command=lambda: self.start(False)).pack(side="left", padx=3)
        ttk.Button(btn_frame, text="暂停", command=self.pause).pack(side="left", padx=3)
        ttk.Button(btn_frame, text="继续", command=self.resume).pack(side="left", padx=3)
        ttk.Button(btn_frame, text="停止", command=self.stop).pack(side="left", padx=3)
        ttk.Button(btn_frame, text="重新执行失败任务", command=lambda: self.start(True)).pack(side="left", padx=3)
        ttk.Button(btn_frame, text="导出结果 Excel", command=self.export_results).pack(side="left", padx=3)
        ttk.Button(btn_frame, text="下载选中视频", command=self.download_selected_video).pack(side="left", padx=3)
        ttk.Button(btn_frame, text="批量下载视频", command=self.batch_download_videos).pack(side="left", padx=3)
        ttk.Button(btn_frame, text="打开输出目录", command=lambda: open_path(self.output_var.get())).pack(side="left", padx=3)

        log_frame = ttk.LabelFrame(self, text="日志")
        log_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.log_text = tk.Text(log_frame, height=9)
        self.log_text.pack(fill="both", expand=True)

    def _restore_state(self) -> None:
        if self.manager.has_state() and messagebox.askyesno("恢复任务", "检测到上次未完成任务，是否恢复？"):
            self.manager.load_state()
            self.refresh_table()
            self.update_stats()

    def choose_excel(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Excel 文件", "*.xlsx *.xls")])
        if path:
            self.excel_var.set(path)

    def choose_output(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.output_var.set(path)

    def sync_config(self) -> None:
        self.config_data.excel_path = Path(self.excel_var.get())
        self.config_data.output_dir = Path(self.output_var.get())
        self.config_data.concurrency = int(self.concurrency_var.get())
        self.config_data.retry_count = int(self.retry_var.get())
        self.config_data.poll_interval_seconds = int(self.poll_var.get())
        self.config_data.max_poll_count = int(self.max_poll_var.get())
        self.config_data.regenerate_existing_images = bool(self.regenerate_image_var.get())
        self.config_data.table_text_color = self.table_text_color_var.get().strip() or "#111111"
        ensure_output_dirs(self.config_data.output_dir)

    def apply_table_text_color(self) -> None:
        color = self.table_text_color_var.get().strip() or "#111111"
        self.table_style.configure("Treeview", foreground=color)

    def load_tasks(self) -> None:
        self.sync_config()
        try:
            if self.manager.has_state() and not self.manager.tasks:
                self.manager.load_state()
            tasks = load_tasks_from_excel(self.config_data.excel_path)
            self.manager.set_tasks(tasks)
            self.refresh_table()
            self.update_stats()
            self.log("INFO", f"加载任务成功，共 {len(tasks)} 条")
        except Exception as exc:
            self.log("ERROR", f"加载任务失败：{exc}")
            messagebox.showerror("加载失败", str(exc))

    def check_tasks(self) -> None:
        for task in self.manager.tasks:
            image, status, error = find_product_image(task.netdisk_path)
            task.product_image_path = image
            if status:
                task.set_status(status, error)
        self.manager.save_state()
        self.refresh_table()
        self.update_stats()
        self.log("INFO", "检查任务完成")

    def start(self, failed_only: bool) -> None:
        self.sync_config()
        if not self.config_data.image_api_key or not self.config_data.video_api_key:
            messagebox.showwarning("API Key 为空", "请先在 .env 文件中配置 IMAGE_API_KEY 和 VIDEO_API_KEY")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("正在执行", "任务正在执行中")
            return
        self.stop_event.clear()
        self.pause_event.set()
        self.worker_thread = threading.Thread(target=self.run_tasks, args=(failed_only,), daemon=True)
        self.worker_thread.start()

    def run_tasks(self, failed_only: bool) -> None:
        tasks = [t for t in self.manager.tasks if TaskStatus.is_failed_or_skipped(t.status)] if failed_only else [t for t in self.manager.tasks if not TaskStatus.is_done(t.status)]
        for task in tasks:
            if self.stop_event.is_set():
                break
            while not self.pause_event.is_set() and not self.stop_event.is_set():
                time.sleep(0.2)
            self.run_one(task)
        self.after(0, self.log, "INFO", "任务执行结束")

    def run_one(self, task) -> None:
        start = time.time()
        if task.status == TaskStatus.FAILED_VIDEO_API:
            task.video_task_id = None
            task.video_url = None
            task.video_file_path = None
            self.manager.save_state()
        task.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        task.error_message = None
        try:
            self.set_task(task, TaskStatus.CHECKING_PRODUCT_IMAGE)
            image, status, error = find_product_image(task.netdisk_path)
            if status:
                self.finish_task(task, status, error, start)
                return
            task.product_image_path = image
            if not task.image_prompt.strip() or not task.video_prompt.strip():
                self.finish_task(task, TaskStatus.SKIPPED_EMPTY_PROMPT, "图片提示词或视频提示词为空", start)
                return
            existing_image_ok = bool(task.generated_image_path and Path(task.generated_image_path).exists())
            existing_image_url_ok = bool(task.generated_image_url and task.generated_image_url.startswith(("http://", "https://", "data:image")))
            if (existing_image_ok or existing_image_url_ok) and not self.config_data.regenerate_existing_images:
                self.set_task(task, TaskStatus.IMAGE_DONE)
                self.after(0, self.log, "INFO", f"PID={task.pid} row={task.row_index} 复用已生成图片，跳过图生图")
            else:
                self.set_task(task, TaskStatus.GENERATING_IMAGE)
                self.after(0, self.log, "INFO", f"PID={task.pid} row={task.row_index} 开始图生图，model={self.config_data.image_model}，format=json_image_array")
                image_result = generate_image_from_product_image(
                    image or "",
                    task.image_prompt,
                    self.config_data.image_api_key,
                    str(self.image_save_path(task)),
                    self.config_data.image_api_base_url,
                    self.config_data.image_model,
                    self.config_data.image_size,
                    self.config_data.retry_count,
                )
                if not image_result.success:
                    self.finish_task(task, TaskStatus.FAILED_IMAGE_API, image_result.error_message, start)
                    return
                task.generated_image_path = image_result.image_path
                task.generated_image_url = image_result.image_url
                task.image_raw_response = image_result.raw_response
            self.set_task(task, TaskStatus.GENERATING_VIDEO)
            try:
                video_save_path = netdisk_video_output_path(task.netdisk_path, task.pid, task.row_index)
            except Exception:
                video_save_path = video_output_path(self.config_data.output_dir, task.pid, task.row_index)
            if task.video_task_id and not task.video_url:
                self.after(0, self.log, "INFO", f"PID={task.pid} row={task.row_index} 继续轮询已有视频任务：{task.video_task_id}")
                video_result = poll_video_task(
                    task.video_task_id,
                    self.config_data.video_api_key,
                    str(video_save_path),
                    self.config_data.video_api_base_url,
                    self.config_data.poll_interval_seconds,
                    self.config_data.max_poll_count,
                )
            else:
                video_result = self.submit_and_poll_video_with_recreate(task, str(video_save_path))
            if not video_result.success:
                task.video_task_id = video_result.task_id or task.video_task_id
                task.video_raw_response = video_result.raw_response
                self.finish_task(task, TaskStatus.FAILED_VIDEO_API, video_result.error_message, start)
                return
            task.video_task_id = video_result.task_id or task.video_task_id
            task.video_raw_response = video_result.raw_response
            task.video_url = video_result.video_url
            task.video_file_path = video_result.video_file_path
            self.finish_task(task, TaskStatus.VIDEO_DOWNLOADED if task.video_file_path else TaskStatus.VIDEO_DOWNLOAD_PENDING, None, start)
        except Exception as exc:
            self.finish_task(task, TaskStatus.FAILED_UNKNOWN, str(exc), start)

    def submit_and_poll_video_with_recreate(self, task, video_save_path: str):
        attempts = max(1, self.config_data.retry_count)
        last_result = None
        for attempt in range(1, attempts + 1):
            create_result = create_video_task(
                task.generated_image_path or task.generated_image_url or "",
                task.video_prompt,
                self.config_data.video_api_key,
                self.config_data.video_api_base_url,
                self.config_data.video_model,
                self.config_data.video_orientation,
                self.config_data.video_resolution,
                self.config_data.retry_count,
            )
            task.video_task_id = create_result.task_id
            task.video_raw_response = create_result.raw_response
            self.manager.save_state()
            if task.video_task_id:
                self.after(0, self.log, "INFO", f"PID={task.pid} row={task.row_index} 视频任务已提交：{task.video_task_id}")
            if not create_result.success:
                return create_result
            result = poll_video_task(
                task.video_task_id or "",
                self.config_data.video_api_key,
                video_save_path,
                self.config_data.video_api_base_url,
                self.config_data.poll_interval_seconds,
                self.config_data.max_poll_count,
            )
            last_result = result
            if result.success:
                return result
            if not result.retryable_failure or attempt >= attempts:
                return result
            self.after(0, self.log, "ERROR", f"PID={task.pid} row={task.row_index} 视频任务失败且建议重提，准备第 {attempt + 1}/{attempts} 次重新提交：{result.error_message}")
            task.video_task_id = None
            task.video_raw_response = result.raw_response
            task.video_url = None
            task.video_file_path = None
            self.manager.save_state()
        return last_result

    def image_save_path(self, task):
        try:
            return netdisk_image_output_path(task.netdisk_path, task.pid, task.row_index)
        except Exception:
            return image_output_path(self.config_data.output_dir, task.pid, task.row_index)

    def set_task(self, task, status: str, error: str | None = None) -> None:
        task.set_status(status, error)
        self.manager.save_state()
        self.after(0, self.refresh_table)
        self.after(0, self.update_stats)

    def finish_task(self, task, status: str, error: str | None, start: float) -> None:
        task.ended_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        task.elapsed_seconds = round(time.time() - start, 2)
        self.set_task(task, status, error)
        level = "ERROR" if status.startswith("FAILED") else "INFO"
        self.after(0, self.log, level, f"PID={task.pid} row={task.row_index} {status} {error or ''}")

    def pause(self) -> None:
        self.pause_event.clear()
        self.log("INFO", "已请求暂停，当前 API 请求结束后暂停")

    def resume(self) -> None:
        self.pause_event.set()
        self.log("INFO", "继续执行")

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.set()
        self.log("INFO", "已请求停止")

    def export_results(self) -> None:
        self.sync_config()
        try:
            path = self.manager.export_excel(self.config_data.output_dir)
            self.log("INFO", f"结果已导出：{path}")
            messagebox.showinfo("导出完成", path)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def download_selected_video(self) -> None:
        selected = self.table.focus()
        if not selected:
            messagebox.showwarning("未选择任务", "请先选择一条已有视频链接的任务")
            return
        index = self.table.index(selected)
        if index < 0 or index >= len(self.manager.tasks):
            return
        task = self.manager.tasks[index]
        if not task.video_url:
            messagebox.showwarning("没有视频链接", "当前任务还没有视频链接")
            return
        try:
            path, downloaded = self.download_task_video(task)
            self.manager.save_state()
            self.refresh_table()
            self.log("INFO", f"PID={task.pid} row={task.row_index} {'视频已下载' if downloaded else '视频已存在，跳过下载'}：{path}")
            messagebox.showinfo("下载完成", path)
        except Exception as exc:
            self.log("ERROR", f"下载视频失败：{exc}")
            messagebox.showerror("下载失败", str(exc))

    def batch_download_videos(self) -> None:
        base_dir = None
        if messagebox.askyesno("批量下载路径", "是否选择一个统一下载目录？选择后会按 PID 创建子目录。"):
            chosen = filedialog.askdirectory()
            if chosen:
                base_dir = chosen
        ok = 0
        skipped = 0
        failed = 0
        for task in self.manager.tasks:
            if not task.video_url:
                continue
            try:
                path, downloaded = self.download_task_video(task, base_dir=base_dir)
                if downloaded:
                    ok += 1
                else:
                    skipped += 1
                self.log("INFO", f"PID={task.pid} row={task.row_index} {'视频已下载' if downloaded else '视频已存在，跳过下载'}：{path}")
            except Exception as exc:
                failed += 1
                task.error_message = f"视频下载失败：{exc}"
                self.log("ERROR", f"PID={task.pid} row={task.row_index} 下载视频失败：{exc}")
        self.manager.save_state()
        self.refresh_table()
        self.update_stats()
        messagebox.showinfo("批量下载完成", f"新下载 {ok} 个，已存在跳过 {skipped} 个，失败 {failed} 个")

    def download_task_video(self, task, base_dir: str | None = None) -> tuple[str, bool]:
        if base_dir:
            existed = existing_grouped_video_path(base_dir, task.pid, task.row_index, task.video_url, task.video_task_id)
        else:
            existed = existing_video_path(task.netdisk_path, task.pid, task.row_index, task.video_url, task.video_task_id)
        if existed:
            task.video_file_path = str(existed)
            task.set_status(TaskStatus.VIDEO_DOWNLOADED)
            return str(existed), False
        save_path = grouped_video_output_path(base_dir, task.pid, task.row_index, task.video_url, task.video_task_id) if base_dir else netdisk_video_output_path(task.netdisk_path, task.pid, task.row_index, task.video_url, task.video_task_id)
        path, downloaded = download_video_to_path(task.video_url or "", save_path, timeout=180)
        task.video_file_path = path
        task.set_status(TaskStatus.VIDEO_DOWNLOADED)
        return path, downloaded

    def refresh_table(self) -> None:
        self.table.delete(*self.table.get_children())
        for idx, task in enumerate(self.manager.tasks, start=1):
            self.table.insert("", "end", values=(getattr(task, "task_name", "") or "", task.pid, task.netdisk_path, "已找到" if task.product_image_path else "", self.summary(task.image_prompt), self.summary(task.video_prompt), task.status, task.generated_image_path or "", task.video_task_id or "", task.video_submit_time or "", task.video_poll_count, task.video_url or "", task.error_message or ""))

    def update_stats(self) -> None:
        s = self.manager.stats()
        self.stats_var.set(f"总任务数: {s['total']}  待提交: {s['pending']}  图片生成中: {s['image_running']}  视频已提交: {s['submitted']}  视频轮询中: {s['polling']}  已完成: {s['completed']}  失败: {s['failed']}  超时: {s['timeout']}  已跳过: {s['skipped']}")

    def open_selected_cell(self, _event) -> None:
        item = self.table.focus()
        values = self.table.item(item, "values")
        if values:
            for value in (values[7], values[12]):
                if value:
                    open_path(value)
                    break

    def log(self, level: str, message: str) -> None:
        getattr(self.logger, level.lower(), self.logger.info)(message)
        self.log_text.insert("end", f"[{level}] {message}\n")
        self.log_text.see("end")

    @staticmethod
    def summary(text: str) -> str:
        return text if len(text) <= 80 else text[:80] + "..."


def main() -> None:
    app = TkApp()
    app.mainloop()
