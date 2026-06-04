import json
import csv
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone, date

import pytz
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from playwright.sync_api import sync_playwright


# -----------------------------
# Helpers
# -----------------------------
def normalize_profile_input(line: str) -> str | None:
    """
    Accept:
    - full url: https://www.tiktok.com/@username
    - username: @username or username
    Return normalized profile url.
    """
    s = (line or "").strip()
    if not s:
        return None

    if s.startswith("http"):
        # basic validation
        if "tiktok.com/@" in s:
            # strip query/fragment
            s = s.split("?")[0].split("#")[0]
            return s.rstrip("/")
        return None

    # username form
    if s.startswith("@"):
        s = s[1:]
    # keep only until first space/slash
    s = s.split()[0].split("/")[0]
    if not s:
        return None
    return f"https://www.tiktok.com/@{s}"


def safe_json(response):
    try:
        return response.json()
    except Exception:
        try:
            return json.loads(response.text())
        except Exception:
            return None


def extract_items(payload: dict):
    if not isinstance(payload, dict):
        return [], None
    items = payload.get("itemList") or payload.get("item_list") or []
    has_more = payload.get("hasMore")
    if has_more is None:
        has_more = payload.get("has_more")
    return items, has_more


def epoch_to_local_day(epoch_seconds: int, tz_name: str) -> date:
    tz = pytz.timezone(tz_name)
    dt_utc = datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc)
    return dt_utc.astimezone(tz).date()


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


# -----------------------------
# Core scraping per account
# -----------------------------
def scrape_one_account_daily_counts(
    profile_url: str,
    tz_name: str,
    start_day: date,
    end_day: date,
    max_scrolls: int,
    scroll_pause_s: float,
    headless: bool,
    cookies_path: str | None,
    log_fn,
    progress_fn,
):
    """
    Returns:
      daily_counts: dict[YYYY-MM-DD] = count
      total_seen_video_ids: int
      total_matched: int
    """
    collected_video_ids = set()
    daily_counts = defaultdict(int)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = p.chromium.launch(headless=headless).new_context()  # safeguard? (not used)
        browser.close()

        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 900, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )

        if cookies_path:
            try:
                with open(cookies_path, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                context.add_cookies(cookies)
                log_fn(f"[INFO] Cookies loaded: {cookies_path}")
            except Exception as e:
                log_fn(f"[WARN] Cookies load failed: {e}")

        page = context.new_page()

        def on_response(resp):
            url = resp.url
            if "/api/post/item_list/" not in url:
                return

            payload = safe_json(resp)
            items, _ = extract_items(payload)

            added_any = False
            for it in items:
                vid = it.get("id") or it.get("itemId") or it.get("item_id")
                ct = it.get("createTime") or it.get("create_time")
                if not vid or not ct:
                    continue
                if vid in collected_video_ids:
                    continue
                collected_video_ids.add(vid)

                day = epoch_to_local_day(int(ct), tz_name)
                if day < start_day or day > end_day:
                    continue

                daily_counts[day.strftime("%Y-%m-%d")] += 1
                added_any = True

            if added_any:
                progress_fn(len(collected_video_ids), sum(daily_counts.values()))

        page.on("response", on_response)

        log_fn(f"[INFO] Open: {profile_url}")
        page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1800)

        last_total = 0
        stagnant = 0

        for i in range(max_scrolls):
            page.mouse.wheel(0, 2400)
            page.wait_for_timeout(int(scroll_pause_s * 1000))

            total = len(collected_video_ids)
            log_fn(f"[SCROLL {i+1}/{max_scrolls}] seen_video_ids={total}")

            if total == last_total:
                stagnant += 1
            else:
                stagnant = 0
                last_total = total

            if stagnant >= 5:
                log_fn("[INFO] No new videos for a while, stop scrolling.")
                break

        browser.close()

    total_seen = len(collected_video_ids)
    total_matched = sum(daily_counts.values())
    return dict(daily_counts), total_seen, total_matched


# -----------------------------
# GUI
# -----------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TikTok 批量账号 | 每日发布数量统计 (Playwright)")
        self.geometry("1180x760")

        # settings
        self.cookies_path = tk.StringVar(value="")
        self.headless = tk.BooleanVar(value=False)
        self.max_scrolls = tk.IntVar(value=80)
        self.pause = tk.DoubleVar(value=1.2)

        # timezone dropdown (default Singapore)
        self.tz_var = tk.StringVar(value="Asia/Singapore")
        self.tz_options = [
            "Asia/Singapore",
            "America/Los_Angeles",
            "America/New_York",
            "Asia/Shanghai",
            "Asia/Kuala_Lumpur",
            "Asia/Jakarta",
            "Asia/Manila",
            "Europe/London",
            "Europe/Paris",
        ]

        # date dropdowns: last 365 days
        self.date_options = self._make_date_options(days_back=365)
        self.start_date_var = tk.StringVar(value=self.date_options[-30])  # default ~30 days ago
        self.end_date_var = tk.StringVar(value=self.date_options[-1])     # today

        # results storage
        self.rows_daily = []   # (profile_url, date, count)
        self.rows_summary = [] # (profile_url, matched_total, days_with_posts, seen_video_ids)

        self._build_ui()

    def _make_date_options(self, days_back: int):
        today = datetime.now().date()
        options = []
        for i in range(days_back, -1, -1):
            d = today - timedelta(days=i)
            options.append(d.strftime("%Y-%m-%d"))
        return options

    def _build_ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        # Inputs
        box = ttk.LabelFrame(root, text="批量输入与筛选", padding=10)
        box.pack(fill="x")

        ttk.Label(box, text="批量主页链接（每行一个，支持 @username / username / 完整链接）:").grid(
            row=0, column=0, sticky="w"
        )
        self.url_text = tk.Text(box, height=6, wrap="word")
        self.url_text.grid(row=1, column=0, columnspan=8, sticky="we", pady=6)
        self.url_text.insert("end", "https://www.tiktok.com/@hoangquangphone\n")

        ttk.Label(box, text="开始日期:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.start_cb = ttk.Combobox(
            box, textvariable=self.start_date_var, values=self.date_options, width=14, state="readonly"
        )
        self.start_cb.grid(row=2, column=1, sticky="w", padx=(6, 18), pady=(6, 0))

        ttk.Label(box, text="结束日期:").grid(row=2, column=2, sticky="w", pady=(6, 0))
        self.end_cb = ttk.Combobox(
            box, textvariable=self.end_date_var, values=self.date_options, width=14, state="readonly"
        )
        self.end_cb.grid(row=2, column=3, sticky="w", padx=(6, 18), pady=(6, 0))

        ttk.Label(box, text="时区:").grid(row=2, column=4, sticky="w", pady=(6, 0))
        self.tz_cb = ttk.Combobox(
            box, textvariable=self.tz_var, values=self.tz_options, width=22, state="readonly"
        )
        self.tz_cb.grid(row=2, column=5, sticky="w", padx=(6, 18), pady=(6, 0))

        ttk.Checkbutton(box, text="Headless(无界面)运行", variable=self.headless).grid(
            row=2, column=6, sticky="w", pady=(6, 0)
        )

        ttk.Label(box, text="最大滚动:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(box, from_=10, to=500, textvariable=self.max_scrolls, width=8).grid(
            row=3, column=1, sticky="w", padx=(6, 18), pady=(6, 0)
        )
        ttk.Label(box, text="滚动间隔(s):").grid(row=3, column=2, sticky="w", pady=(6, 0))
        ttk.Spinbox(box, from_=0.5, to=5.0, increment=0.1, textvariable=self.pause, width=8).grid(
            row=3, column=3, sticky="w", padx=(6, 18), pady=(6, 0)
        )

        ttk.Label(box, text="Cookies JSON(可选):").grid(row=3, column=4, sticky="w", pady=(6, 0))
        ttk.Entry(box, textvariable=self.cookies_path, width=36).grid(
            row=3, column=5, sticky="w", padx=(6, 6), pady=(6, 0)
        )
        ttk.Button(box, text="选择文件", command=self.pick_cookies).grid(
            row=3, column=6, sticky="w", pady=(6, 0)
        )

        box.columnconfigure(5, weight=1)

        # Buttons
        btns = ttk.Frame(root)
        btns.pack(fill="x", pady=10)

        self.run_btn = ttk.Button(btns, text="开始批量抓取", command=self.run_batch)
        self.run_btn.pack(side="left")

        self.export_daily_btn = ttk.Button(btns, text="导出 Daily CSV", command=self.export_daily, state="disabled")
        self.export_daily_btn.pack(side="left", padx=8)

        self.export_sum_btn = ttk.Button(btns, text="导出 Summary CSV", command=self.export_summary, state="disabled")
        self.export_sum_btn.pack(side="left", padx=8)

        ttk.Button(btns, text="清空结果", command=self.clear_results).pack(side="left", padx=8)

        # Status
        status = ttk.Frame(root)
        status.pack(fill="x", pady=(0, 8))
        self.status_var = tk.StringVar(value="就绪（默认时区：Asia/Singapore）")
        ttk.Label(status, textvariable=self.status_var).pack(side="left")

        # Panes: results and logs
        panes = ttk.PanedWindow(root, orient="vertical")
        panes.pack(fill="both", expand=True)

        # Results upper: two tables (daily, summary)
        results = ttk.PanedWindow(panes, orient="horizontal")
        panes.add(results, weight=3)

        daily_frame = ttk.LabelFrame(results, text="Daily 明细（账号-日期-数量）", padding=8)
        results.add(daily_frame, weight=3)

        self.daily_tree = ttk.Treeview(daily_frame, columns=("url", "date", "count"), show="headings", height=12)
        for col, w in [("url", 520), ("date", 130), ("count", 90)]:
            self.daily_tree.heading(col, text=col)
            self.daily_tree.column(col, width=w, anchor="w" if col == "url" else "center")
        self.daily_tree.pack(fill="both", expand=True)

        summary_frame = ttk.LabelFrame(results, text="Summary 汇总（每账号）", padding=8)
        results.add(summary_frame, weight=2)

        self.sum_tree = ttk.Treeview(
            summary_frame,
            columns=("url", "matched_total", "days_with_posts", "seen_video_ids"),
            show="headings",
            height=12
        )
        headers = {
            "url": ("url", 420, "w"),
            "matched_total": ("matched_total", 110, "center"),
            "days_with_posts": ("days_with_posts", 120, "center"),
            "seen_video_ids": ("seen_video_ids", 120, "center"),
        }
        for col, (txt, w, anc) in headers.items():
            self.sum_tree.heading(col, text=txt)
            self.sum_tree.column(col, width=w, anchor=anc)
        self.sum_tree.pack(fill="both", expand=True)

        # Logs lower
        log_frame = ttk.LabelFrame(panes, text="运行日志", padding=8)
        panes.add(log_frame, weight=2)

        self.log_text = tk.Text(log_frame, height=10, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log("提示：抓不到/很少数据时：取消 headless + 提供 cookies.json 通常更稳。")

    def pick_cookies(self):
        path = filedialog.askopenfilename(
            title="选择 cookies.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if path:
            self.cookies_path.set(path)

    def log(self, msg: str):
        def _append():
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
        self.after(0, _append)

    def set_status(self, msg: str):
        self.after(0, lambda: self.status_var.set(msg))

    def clear_results(self):
        for t in (self.daily_tree, self.sum_tree):
            for item in t.get_children():
                t.delete(item)
        self.rows_daily = []
        self.rows_summary = []
        self.export_daily_btn.config(state="disabled")
        self.export_sum_btn.config(state="disabled")
        self.set_status("就绪")

    def _get_urls(self):
        raw = self.url_text.get("1.0", "end").strip().splitlines()
        urls = []
        bad = []
        for line in raw:
            u = normalize_profile_input(line)
            if u:
                urls.append(u)
            elif line.strip():
                bad.append(line.strip())
        # de-dup while preserving order
        seen = set()
        unique = []
        for u in urls:
            if u not in seen:
                unique.append(u)
                seen.add(u)
        return unique, bad

    def run_batch(self):
        urls, bad = self._get_urls()
        if not urls:
            messagebox.showerror("输入错误", "请至少输入一个有效账号链接或用户名。")
            return
        if bad:
            self.log(f"[WARN] 无效输入将被忽略：{bad[:5]}{' ...' if len(bad)>5 else ''}")

        # validate dates
        try:
            start_day = parse_date(self.start_date_var.get())
            end_day = parse_date(self.end_date_var.get())
            if start_day > end_day:
                messagebox.showerror("日期错误", "开始日期不能晚于结束日期。")
                return
        except Exception:
            messagebox.showerror("日期错误", "日期下拉值异常，请重新选择。")
            return

        self.run_btn.config(state="disabled")
        self.export_daily_btn.config(state="disabled")
        self.export_sum_btn.config(state="disabled")
        self.clear_results()

        self.set_status(f"开始批量抓取：{len(urls)} 个账号 | {self.start_date_var.get()} ~ {self.end_date_var.get()} | {self.tz_var.get()}")
        self.log(f"[INFO] Batch start. accounts={len(urls)}")

        def worker():
            try:
                tz_name = self.tz_var.get()
                max_scrolls = int(self.max_scrolls.get())
                pause = float(self.pause.get())
                headless = bool(self.headless.get())
                cookies = self.cookies_path.get().strip() or None
                start_day = parse_date(self.start_date_var.get())
                end_day = parse_date(self.end_date_var.get())

                all_daily_rows = []
                all_summary_rows = []

                for idx, url in enumerate(urls, start=1):
                    self.set_status(f"抓取中 {idx}/{len(urls)}：{url}")
                    self.log(f"\n[ACCOUNT {idx}/{len(urls)}] {url}")

                    def progress_fn(seen_ids, matched):
                        self.set_status(f"{idx}/{len(urls)} {url} | seen_ids={seen_ids} | matched={matched}")

                    daily_counts, seen_ids, matched_total = scrape_one_account_daily_counts(
                        profile_url=url,
                        tz_name=tz_name,
                        start_day=start_day,
                        end_day=end_day,
                        max_scrolls=max_scrolls,
                        scroll_pause_s=pause,
                        headless=headless,
                        cookies_path=cookies,
                        log_fn=self.log,
                        progress_fn=progress_fn,
                    )

                    # fill empty days? (optional) — we keep only days with posts
                    days_with_posts = len(daily_counts)
                    all_summary_rows.append((url, matched_total, days_with_posts, seen_ids))
                    for d, c in sorted(daily_counts.items()):
                        all_daily_rows.append((url, d, c))

                self.rows_daily = all_daily_rows
                self.rows_summary = all_summary_rows
                self.after(0, self._render_results)

            except Exception as e:
                self.log(f"[ERROR] {e}")
                self.after(0, lambda: messagebox.showerror("运行失败", str(e)))
            finally:
                self.after(0, lambda: self.run_btn.config(state="normal"))
                self.set_status("完成")

        threading.Thread(target=worker, daemon=True).start()

    def _render_results(self):
        # daily table
        for item in self.daily_tree.get_children():
            self.daily_tree.delete(item)
        for r in self.rows_daily:
            self.daily_tree.insert("", "end", values=r)

        # summary table
        for item in self.sum_tree.get_children():
            self.sum_tree.delete(item)
        for r in self.rows_summary:
            self.sum_tree.insert("", "end", values=r)

        self.export_daily_btn.config(state="normal" if self.rows_daily else "disabled")
        self.export_sum_btn.config(state="normal" if self.rows_summary else "disabled")

        self.log(f"\n[INFO] Batch done. daily_rows={len(self.rows_daily)}, summary_rows={len(self.rows_summary)}")

    def export_daily(self):
        if not self.rows_daily:
            messagebox.showinfo("无数据", "没有 Daily 明细可导出。")
            return
        path = filedialog.asksaveasfilename(
            title="保存 Daily CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")]
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["profile_url", "date", "post_count"])
            for r in self.rows_daily:
                w.writerow(r)
        messagebox.showinfo("导出成功", f"已保存：{path}")

    def export_summary(self):
        if not self.rows_summary:
            messagebox.showinfo("无数据", "没有 Summary 汇总可导出。")
            return
        path = filedialog.asksaveasfilename(
            title="保存 Summary CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")]
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["profile_url", "matched_total_in_date_range", "days_with_posts", "seen_unique_video_ids"])
            for r in self.rows_summary:
                w.writerow(r)
        messagebox.showinfo("导出成功", f"已保存：{path}")


if __name__ == "__main__":
    app = App()
    app.mainloop()
