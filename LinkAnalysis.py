#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TikTok 链接批量解析器 (修复版-无下划线方法)
- 自动解析短链，再提取 product_id / video_id
- “提取 product_id（必要时自动解析）”
- 复制 PID(选中) / 复制全部 PID
"""

import re, csv, time, threading, webbrowser, random
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText

try:
    import requests
except Exception:
    requests = None

APP_TITLE = "TikTok 链接批量解析器 (含 product_id 提取)"
APP_GEOMETRY = "1120x780"

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

COLUMNS = ("#","short_url","final_url","video_id","product_id","status")

VIDEO_ID_RE   = re.compile(r"/video/(\d+)")
PRODUCT_ID_RE = re.compile(r"/product/(\d+)")
VM_HOST_RE    = re.compile(r"(?:^|\.)vm\.tiktok\.com$", re.I)

def extract_video_id(s: str) -> str:
    m = VIDEO_ID_RE.search(s or "")
    return m.group(1) if m else ""

def extract_product_id(s: str) -> str:
    m = PRODUCT_ID_RE.search(s or "")
    return m.group(1) if m else ""

def need_resolve_first(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        if VM_HOST_RE.search(host):
            return True
    except Exception:
        pass
    return not ("/product/" in url or "/video/" in url)

def robust_resolve(session: "requests.Session", url: str, timeout: float):
    """
    解析跳转：优先 GET(跟随)，失败回退 HEAD。
    还会尝试从 HTML 的 canonical/og:url 兜底。
    返回 (final_url, status_text)
    """
    try:
        r = session.get(url, allow_redirects=True, timeout=timeout)
        final = r.url
        status = f"{r.status_code}"
        try:
            if ("/product/" not in final and "/video/" not in final) and r.text:
                m1 = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', r.text, re.I)
                m2 = re.search(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', r.text, re.I)
                cand = (m1.group(1) if m1 else (m2.group(1) if m2 else ""))
                if cand:
                    final = cand
        except Exception:
            pass
        return final, status
    except Exception:
        try:
            r = session.head(url, allow_redirects=True, timeout=timeout)
            return r.url, f"{r.status_code}"
        except Exception as e_head:
            return url, f"ERR:{type(e_head).__name__}"

class ResolverGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(APP_GEOMETRY)
        self.minsize(980, 660)

        self.var_timeout = tk.StringVar(value="12")
        self.var_delay = tk.StringVar(value="0.3")
        self.var_ua = tk.StringVar(value=DEFAULT_UA)
        self.var_concurrency = tk.StringVar(value="4")
        self.var_retries = tk.StringVar(value="2")
        self.var_running = False

        self.build_ui()

    def build_ui(self):
        top = ttk.Frame(self); top.pack(fill="x", padx=12, pady=(12,8))
        ttk.Label(top, text="粘贴链接（每行一条）：").grid(row=0, column=0, sticky="w")
        self.input_box = ScrolledText(top, height=8, wrap="none")
        self.input_box.grid(row=1, column=0, columnspan=12, sticky="nsew", pady=(4,8))
        top.columnconfigure(11, weight=1)

        ttk.Label(top, text="超时(s)").grid(row=2, column=0, sticky="e")
        ttk.Entry(top, width=6, textvariable=self.var_timeout).grid(row=2, column=1, sticky="w", padx=(4,12))
        ttk.Label(top, text="请求间隔(s)").grid(row=2, column=2, sticky="e")
        ttk.Entry(top, width=6, textvariable=self.var_delay).grid(row=2, column=3, sticky="w", padx=(4,12))
        ttk.Label(top, text="并发").grid(row=2, column=4, sticky="e")
        ttk.Entry(top, width=4, textvariable=self.var_concurrency).grid(row=2, column=5, sticky="w", padx=(4,12))
        ttk.Label(top, text="失败重试").grid(row=2, column=6, sticky="e")
        ttk.Entry(top, width=4, textvariable=self.var_retries).grid(row=2, column=7, sticky="w", padx=(4,12))

        ttk.Button(top, text="批量解析（跟随跳转）", command=self.start_resolve).grid(row=2, column=8, sticky="e", padx=(0,8))
        ttk.Button(top, text="提取 product_id（必要时自动解析）", command=self.extract_pid_auto).grid(row=2, column=9, sticky="w")
        ttk.Button(top, text="清空", command=self.clear_all).grid(row=2, column=10, sticky="w")

        ttk.Label(top, text="User-Agent").grid(row=3, column=0, sticky="e", pady=(6,0))
        ua_entry = ttk.Entry(top, textvariable=self.var_ua)
        ua_entry.grid(row=3, column=1, columnspan=11, sticky="ew", padx=(4,0), pady=(6,0))

        mid = ttk.Frame(self); mid.pack(fill="both", expand=True, padx=12, pady=8)
        self.tree = ttk.Treeview(mid, columns=COLUMNS, show="headings", height=16)
        for c in COLUMNS: self.tree.heading(c, text=c)
        self.tree.column("#", width=44, anchor="center")
        self.tree.column("short_url", width=310, anchor="w")
        self.tree.column("final_url", width=520, anchor="w")
        self.tree.column("video_id", width=120, anchor="center")
        self.tree.column("product_id", width=180, anchor="center")
        self.tree.column("status", width=120, anchor="center")
        self.tree.pack(fill="both", expand=True, side="left")
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y"); hsb.pack(side="bottom", fill="x")

        bottom = ttk.Frame(self); bottom.pack(fill="x", padx=12, pady=(0,12))
        ttk.Button(bottom, text="导出 CSV", command=self.export_csv).pack(side="left")
        ttk.Button(bottom, text="复制最终链接(选中)", command=self.copy_selected_final).pack(side="left", padx=8)
        ttk.Button(bottom, text="在浏览器打开(选中)", command=self.open_selected).pack(side="left", padx=8)
        ttk.Button(bottom, text="复制整表 CSV", command=self.copy_table_csv).pack(side="left", padx=8)
        ttk.Button(bottom, text="复制 PID(选中)", command=self.copy_selected_pid).pack(side="left", padx=8)
        ttk.Button(bottom, text="复制全部 PID", command=self.copy_all_pid).pack(side="left", padx=8)

        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(side="right", fill="x", expand=True)

        self.status = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=12, pady=(0,10))

    # -------------------- 启动入口 --------------------

    def start_resolve(self):
        if requests is None:
            messagebox.showerror("缺少依赖", "需要安装 requests：\n\npip install requests"); return
        if self.var_running:
            messagebox.showinfo("提示", "正在解析中…"); return

        urls = self.gather_urls()
        if not urls:
            messagebox.showwarning("提示", "请粘贴至少一条链接"); return

        timeout, delay, concurrency, retries = self.read_runtime_params()
        self.reset_table_with_urls(urls)
        self.progress["value"] = 0; self.progress["maximum"] = len(urls)
        self.status.set(f"准备解析 {len(urls)} 条…"); self.var_running = True

        threading.Thread(
            target=self.resolve_worker,
            args=(urls, timeout, delay, concurrency, retries, self.var_ua.get(), True),
            daemon=True
        ).start()

    def extract_pid_auto(self):
        if requests is None:
            messagebox.showerror("缺少依赖", "需要安装 requests：\n\npip install requests"); return
        if self.var_running:
            messagebox.showinfo("提示", "正在解析中…"); return

        urls = self.gather_urls()
        if not urls:
            messagebox.showwarning("提示", "请粘贴至少一条链接"); return

        timeout, delay, concurrency, retries = self.read_runtime_params()
        self.reset_table_with_urls(urls)
        self.progress["value"] = 0; self.progress["maximum"] = len(urls)
        self.status.set(f"准备提取 product_id（必要时解析） 共 {len(urls)} 条…"); self.var_running = True

        threading.Thread(
            target=self.resolve_worker,
            args=(urls, timeout, delay, concurrency, retries, self.var_ua.get(), False),
            daemon=True
        ).start()

    # -------------------- 核心解析 --------------------

    def read_runtime_params(self):
        def _num(var, default, cast):
            try: return cast(var.get())
            except Exception: var.set(str(default)); return default
        timeout = _num(self.var_timeout, 12.0, float)
        delay   = _num(self.var_delay, 0.3, float)
        conc    = max(1, _num(self.var_concurrency, 4, int))
        retry   = max(0, _num(self.var_retries, 2, int))
        return timeout, delay, conc, retry

    def reset_table_with_urls(self, urls):
        for i in self.tree.get_children(): self.tree.delete(i)
        for idx, u in enumerate(urls, 1):
            self.tree.insert("", "end", iid=str(idx), values=(idx, u, "", "", "", "待解析"))

    def gather_urls(self):
        raw = self.input_box.get("1.0", "end").strip()
        return [l.strip() for l in raw.splitlines() if l.strip() and not l.strip().startswith("#")]

    def resolve_worker(self, urls, timeout, delay, concurrency, retries, ua, show_all_cols: bool):
        session = requests.Session()
        session.headers.update({"User-Agent": ua, "Accept": "text/html,*/*;q=0.8"})
        lock = threading.Lock(); total = len(urls); done = 0

        def resolve_once(u: str):
            final, status = robust_resolve(session, u, timeout)
            for _ in range(retries):
                if ("/product/" in final) or ("/video/" in final): break
                time.sleep(0.25 + random.random() * 0.35)
                final, status = robust_resolve(session, u, timeout)
            return final, status

        def one(url, idx):
            nonlocal done
            try:
                if need_resolve_first(url):
                    final, status = resolve_once(url)
                    if not final: final, status = url, status or "ERR:resolve"
                else:
                    final, status = url, "no-resolve"

                vid = extract_video_id(final) or extract_video_id(url)
                pid = extract_product_id(final) or extract_product_id(url)

            except Exception as e:
                final, status = url, f"ERR:{type(e).__name__}"
                vid = extract_video_id(url) or ""
                pid = extract_product_id(url) or ""

            # 这里调用我们“无下划线”的方法
            self.update_row(idx, url, final if show_all_cols else "", vid if show_all_cols else "", pid, status)

            with lock:
                done += 1
                self.set_progress(done, total)
            time.sleep(delay)

        queue = list(enumerate(urls, start=1)); threads = []
        while queue or any(t.is_alive() for t in threads):
            threads = [t for t in threads if t.is_alive()]
            while queue and len(threads) < concurrency:
                idx, url = queue.pop(0)
                t = threading.Thread(target=one, args=(url, idx), daemon=True)
                t.start(); threads.append(t)
            time.sleep(0.05)

        self.var_running = False
        self.status.set("完成")

    # -------------------- UI 更新（主线程） --------------------

    def update_row(self, idx, short_url, final_url, vid, pid, status):
        def _do():
            if self.tree.exists(str(idx)):
                self.tree.item(str(idx), values=(idx, short_url, final_url, vid, pid, status))
        self.after(0, _do)

    def set_progress(self, done, total):
        def _do():
            self.progress["value"] = done
            self.status.set(f"解析中… {done}/{total}")
        self.after(0, _do)

    # -------------------- 操作按钮 --------------------

    def clear_all(self):
        self.input_box.delete("1.0", "end")
        for i in self.tree.get_children(): self.tree.delete(i)
        self.progress["value"] = 0
        self.status.set("已清空")

    def export_csv(self):
        rows = self.collect_table_rows()
        if not rows: messagebox.showinfo("提示", "没有可导出的数据。"); return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV files","*.csv"), ("All files","*.*")],
                                            title="保存为 CSV")
        if not path: return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(COLUMNS)
            for r in rows: w.writerow(r)
        self.status.set(f"已保存：{path}")

    def copy_selected_final(self):
        sel = self.tree.selection()
        finals = [self.tree.item(iid, "values")[2] for iid in sel if self.tree.item(iid, "values")[2]]
        if not finals: messagebox.showinfo("提示", "未选中或选中项无 final_url。"); return
        self.clipboard_clear(); self.clipboard_append("\n".join(finals))
        self.status.set(f"已复制 {len(finals)} 条最终链接")

    def open_selected(self):
        sel = self.tree.selection(); opened = 0
        for iid in sel:
            final_url = self.tree.item(iid, "values")[2]
            if final_url:
                try: webbrowser.open(final_url); opened += 1
                except Exception: pass
        self.status.set(f"尝试在浏览器打开 {opened} 条")

    def copy_table_csv(self):
        rows = self.collect_table_rows()
        if not rows: messagebox.showinfo("提示", "表中暂无数据。"); return
        lines = [",".join(COLUMNS)]
        for r in rows:
            out = []
            for cell in r:
                s = str(cell)
                if "," in s or '"' in s: s = '"' + s.replace('"','""') + '"'
                out.append(s)
            lines.append(",".join(out))
        self.clipboard_clear(); self.clipboard_append("\n".join(lines))
        self.status.set("整表 CSV 已复制到剪贴板")

    def copy_selected_pid(self):
        sel = self.tree.selection()
        pids = [self.tree.item(iid, "values")[4] for iid in sel if self.tree.item(iid, "values")[4]]
        if not pids: messagebox.showinfo("提示", "未选中或选中项无 product_id。"); return
        self.clipboard_clear(); self.clipboard_append("\n".join(pids))
        self.status.set(f"已复制 {len(pids)} 个 PID")

    def copy_all_pid(self):
        pids = []
        for iid in self.tree.get_children():
            pid = self.tree.item(iid, "values")[4]
            if pid: pids.append(pid)
        if not pids: messagebox.showinfo("提示", "表中暂无可复制的 product_id。"); return
        self.clipboard_clear(); self.clipboard_append("\n".join(pids))
        self.status.set(f"已复制全部 {len(pids)} 个 PID")

    def collect_table_rows(self):
        return [self.tree.item(iid, "values") for iid in self.tree.get_children()]

def main():
    app = ResolverGUI()
    app.mainloop()

if __name__ == "__main__":
    main()
