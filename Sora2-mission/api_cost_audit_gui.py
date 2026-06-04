import os
import json
import csv
import threading
import queue
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# =========================
# Cost rules (customizable)
# =========================
DEFAULT_CHARGE_AMOUNT = 0.4
DEFAULT_REFUND_AMOUNT = 0.4

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def parse_dt(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, TIME_FMT)
    except Exception:
        return None


def is_nonempty(s: Any) -> bool:
    return isinstance(s, str) and s.strip() != ""


@dataclass
class TaskRow:
    source_file: str
    task_id: str
    provider: str
    model: str
    remote_id: str  # ✅ 新增
    status: str
    created_at: str
    charged_at: str
    refunded_at: str
    video_url: str

    charged_flag: bool
    refunded_flag: bool

    charge_delta: float   # negative for spend
    refund_delta: float   # positive for refund
    net_delta: float      # charge + refund (negative = net spend)

    in_time_range: bool
    has_asset: bool


@dataclass
class Summary:
    files_loaded: int
    tasks_total_raw: int
    tasks_after_dedup: int

    charged_events: int
    refunded_events: int

    total_charged_amount: float   # positive number for "spent"
    total_refunded_amount: float  # positive number for "refund"
    net_spend: float              # positive number = spent - refunded

    assets_count: int

    initial_balance: float
    adjustments: float
    ending_balance: float

    time_field: str
    start_time: Optional[datetime]
    end_time: Optional[datetime]

    anomalies: int


# =========================
# Load / normalize JSON
# =========================
def load_tasks_from_json(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    tasks = data.get("tasks", [])
    if not isinstance(tasks, list):
        return []
    return tasks


def collect_json_files(paths: List[str]) -> List[str]:
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for fn in files:
                    if fn.lower().endswith(".json"):
                        out.append(os.path.join(root, fn))
        elif os.path.isfile(p) and p.lower().endswith(".json"):
            out.append(p)
    # dedup
    out = sorted(list(dict.fromkeys(out)))
    return out


def task_time_for_filter(task: Dict[str, Any], field: str) -> Optional[datetime]:
    return parse_dt(str(task.get(field, "") or ""))


def in_range(dt: Optional[datetime], start: Optional[datetime], end: Optional[datetime]) -> bool:
    if dt is None:
        return False
    if start and dt < start:
        return False
    if end and dt > end:
        return False
    return True


def compute_row(
    task: Dict[str, Any],
    source_file: str,
    time_field: str,
    start: Optional[datetime],
    end: Optional[datetime],
    charge_amount: float,
    refund_amount: float,
) -> TaskRow:
    task_id = str(task.get("task_id", "") or "")
    remote_id = str(task.get("remote_id", "") or "")
    provider = str(task.get("provider", "") or "")
    model = str(task.get("model", "") or "")
    status = str(task.get("status", "") or "")
    created_at = str(task.get("created_at", "") or "")
    charged_at = str(task.get("charged_at", "") or "")
    refunded_at = str(task.get("refunded_at", "") or "")
    video_url = task.get("video_url", None)
    video_url = "" if video_url is None else str(video_url)

    charged_flag = is_nonempty(charged_at)
    refunded_flag = is_nonempty(refunded_at)

    # Your rules:
    # charged_at not empty => -0.4
    # refunded_at not empty => +0.4
    charge_delta = (-charge_amount) if charged_flag else 0.0
    refund_delta = (refund_amount) if refunded_flag else 0.0
    net_delta = charge_delta + refund_delta

    dt = task_time_for_filter(task, time_field)
    in_time = in_range(dt, start, end) if (start or end) else True

    has_asset = bool(video_url.strip())

    return TaskRow(
        source_file=os.path.basename(source_file),
        task_id=task_id,
        remote_id=remote_id,  # ✅ 新增
        provider=provider,
        model=model,
        status=status,
        created_at=created_at,
        charged_at=charged_at,
        refunded_at=refunded_at,
        video_url=video_url,
        charged_flag=charged_flag,
        refunded_flag=refunded_flag,
        charge_delta=charge_delta,
        refund_delta=refund_delta,
        net_delta=net_delta,
        in_time_range=in_time,
        has_asset=has_asset,
    )


def dedup_by_remote_id(rows: List[TaskRow], prefer: str) -> List[TaskRow]:
    """
    prefer:
      - "created_at": keep latest created_at
      - "charged_at": keep latest charged_at (fallback created_at)
    Dedup key: remote_id
    """
    best: Dict[str, TaskRow] = {}

    def key_dt(r: TaskRow) -> datetime:
        if prefer == "charged_at":
            dt = parse_dt(r.charged_at) or parse_dt(r.created_at) or datetime.min
        else:
            dt = parse_dt(r.created_at) or datetime.min
        return dt

    for r in rows:
        # remote_id 为空：不去重（全部保留）
        if not r.remote_id:
            unique_key = f"__NOREMOTE__::{id(r)}"
            best[unique_key] = r
            continue

        if r.remote_id not in best:
            best[r.remote_id] = r
        else:
            if key_dt(r) >= key_dt(best[r.remote_id]):
                best[r.remote_id] = r

    return list(best.values())



def summarize(
    rows_all: List[TaskRow],
    rows_used: List[TaskRow],
    initial_balance: float,
    adjustments: float,
    time_field: str,
    start: Optional[datetime],
    end: Optional[datetime],
) -> Summary:
    # filter by time
    used_in_range = [r for r in rows_used if r.in_time_range]

    charged_events = sum(1 for r in used_in_range if r.charged_flag)
    refunded_events = sum(1 for r in used_in_range if r.refunded_flag)

    # "spent" is positive
    total_charged_amount = sum((-r.charge_delta) for r in used_in_range if r.charge_delta < 0)
    total_refunded_amount = sum((r.refund_delta) for r in used_in_range if r.refund_delta > 0)
    net_spend = total_charged_amount - total_refunded_amount

    assets_count = sum(1 for r in used_in_range if r.has_asset)

    # anomalies (basic)
    anomalies = 0
    for r in used_in_range:
        # Detect internal flag mismatch if fields exist in your JSON
        # (not required for cost, but useful for audit)
        # We can't see charged_once/refunded_once here, since TaskRow doesn't store them.
        # We'll detect anomalies by "refunded but not charged" etc.
        if r.refunded_flag and not r.charged_flag:
            anomalies += 1

    ending_balance = initial_balance + adjustments - net_spend

    return Summary(
        files_loaded=len(set(r.source_file for r in rows_all)),
        tasks_total_raw=len(rows_all),
        tasks_after_dedup=len(rows_used),

        charged_events=charged_events,
        refunded_events=refunded_events,

        total_charged_amount=round(total_charged_amount, 6),
        total_refunded_amount=round(total_refunded_amount, 6),
        net_spend=round(net_spend, 6),

        assets_count=assets_count,

        initial_balance=round(initial_balance, 6),
        adjustments=round(adjustments, 6),
        ending_balance=round(ending_balance, 6),

        time_field=time_field,
        start_time=start,
        end_time=end,

        anomalies=anomalies,
    )


def group_by(rows: List[TaskRow], key_fn) -> Dict[str, List[TaskRow]]:
    d: Dict[str, List[TaskRow]] = {}
    for r in rows:
        k = key_fn(r) or ""
        d.setdefault(k, []).append(r)
    return d


# =========================
# GUI
# =========================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("API 支出审计工具（charged_at/refunded_at 0.4 规则）")
        self.geometry("1280x820")
        self.minsize(1100, 720)

        self.log_q = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.stop_flag = threading.Event()

        self.loaded_files: List[str] = []
        self.rows_all: List[TaskRow] = []
        self.rows_used: List[TaskRow] = []
        self.summary: Optional[Summary] = None

        self._build_ui()
        self._poll()

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=10)

        # Import
        ttk.Button(top, text="📥 导入 JSON 文件（多选）", command=self.import_files).grid(row=0, column=0, sticky="w")
        ttk.Button(top, text="📂 选择目录（批量读取 *.json）", command=self.import_folder).grid(row=0, column=1, padx=8, sticky="w")
        ttk.Button(top, text="🧹 清空", command=self.clear_all).grid(row=0, column=2, padx=8, sticky="w")

        self.files_label = ttk.Label(top, text="未导入文件")
        self.files_label.grid(row=0, column=3, padx=10, sticky="w")

        # Params
        params = ttk.LabelFrame(self, text="计算参数")
        params.pack(fill="x", padx=12, pady=(0, 8))

        # amounts
        ttk.Label(params, text="charged 扣费额度:").grid(row=0, column=0, padx=10, pady=8, sticky="w")
        self.charge_amount_var = tk.StringVar(value=str(DEFAULT_CHARGE_AMOUNT))
        ttk.Entry(params, textvariable=self.charge_amount_var, width=10).grid(row=0, column=1, sticky="w")

        ttk.Label(params, text="refunded 退款额度:").grid(row=0, column=2, padx=10, pady=8, sticky="w")
        self.refund_amount_var = tk.StringVar(value=str(DEFAULT_REFUND_AMOUNT))
        ttk.Entry(params, textvariable=self.refund_amount_var, width=10).grid(row=0, column=3, sticky="w")

        # balance
        ttk.Label(params, text="初始余额:").grid(row=0, column=4, padx=10, pady=8, sticky="w")
        self.initial_balance_var = tk.StringVar(value="0")
        ttk.Entry(params, textvariable=self.initial_balance_var, width=12).grid(row=0, column=5, sticky="w")

        ttk.Label(params, text="手动调整(可正可负):").grid(row=0, column=6, padx=10, pady=8, sticky="w")
        self.adjust_var = tk.StringVar(value="0")
        ttk.Entry(params, textvariable=self.adjust_var, width=12).grid(row=0, column=7, sticky="w")

        # time filter
        ttk.Label(params, text="时间字段:").grid(row=1, column=0, padx=10, pady=8, sticky="w")
        self.time_field_var = tk.StringVar(value="charged_at")
        ttk.Combobox(params, textvariable=self.time_field_var, values=["created_at", "charged_at", "refunded_at"], width=12, state="readonly") \
            .grid(row=1, column=1, sticky="w")

        ttk.Label(params, text="开始(YYYY-MM-DD HH:MM:SS):").grid(row=1, column=2, padx=10, pady=8, sticky="w")
        self.start_var = tk.StringVar(value="")
        ttk.Entry(params, textvariable=self.start_var, width=22).grid(row=1, column=3, sticky="w")

        ttk.Label(params, text="结束(YYYY-MM-DD HH:MM:SS):").grid(row=1, column=4, padx=10, pady=8, sticky="w")
        self.end_var = tk.StringVar(value="")
        ttk.Entry(params, textvariable=self.end_var, width=22).grid(row=1, column=5, sticky="w")

        # dedup
        ttk.Label(params, text="task_id 去重策略:").grid(row=1, column=6, padx=10, pady=8, sticky="w")
        self.dedup_var = tk.StringVar(value="created_at")
        ttk.Combobox(params, textvariable=self.dedup_var, values=["created_at", "charged_at"], width=12, state="readonly") \
            .grid(row=1, column=7, sticky="w")

        # run
        actions = ttk.Frame(self)
        actions.pack(fill="x", padx=12, pady=(0, 8))
        self.run_btn = ttk.Button(actions, text="🧮 计算支出", command=self.run_calc, state="disabled")
        self.run_btn.pack(side="left")
        self.export_btn = ttk.Button(actions, text="📤 导出 CSV 报表", command=self.export_csv, state="disabled")
        self.export_btn.pack(side="left", padx=8)

        # Summary panel
        summary = ttk.LabelFrame(self, text="汇总结果")
        summary.pack(fill="x", padx=12, pady=(0, 8))

        self.sum_text = tk.StringVar(value="导入 JSON 后点击【计算支出】")
        ttk.Label(summary, textvariable=self.sum_text, justify="left").pack(anchor="w", padx=10, pady=8)

        # Main split
        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=12, pady=6)

        left = ttk.LabelFrame(mid, text="任务明细（用于对账）")
        left.pack(side="left", fill="both", expand=True)

        right = ttk.LabelFrame(mid, text="日志")
        right.pack(side="right", fill="both", expand=False, padx=(10, 0))

        # table
        cols = ("file", "task_id", "remote_id", "provider", "model", "status",
                "created_at", "charged_at", "refunded_at", "asset", "net_delta")

        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=18)
        for c in cols:
            self.tree.heading(c, text=c, anchor="center")
            self.tree.column(c, width=120, anchor="center")

        self.tree.column("file", width=130, anchor="center")
        self.tree.column("task_id", width=120, anchor="center")
        self.tree.column("remote_id", width=160, anchor="center")
        self.tree.column("model", width=220, anchor="center")
        self.tree.column("created_at", width=170, anchor="center")
        self.tree.column("charged_at", width=170, anchor="center")
        self.tree.column("refunded_at", width=170, anchor="center")
        self.tree.column("asset", width=80, anchor="center")
        self.tree.column("net_delta", width=110, anchor="center")

        self.tree.pack(fill="both", expand=True, padx=8, pady=8)

        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")

        # log
        self.log_text = tk.Text(right, width=50, wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_text.configure(state="disabled")

    # ---------- UI helpers ----------
    def log(self, msg: str):
        self.log_q.put(msg)

    def _poll(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def clear_all(self):
        self.loaded_files = []
        self.rows_all = []
        self.rows_used = []
        self.summary = None
        self.files_label.config(text="未导入文件")
        self.sum_text.set("导入 JSON 后点击【计算支出】")
        self.run_btn.config(state="disabled")
        self.export_btn.config(state="disabled")
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.log("已清空。")

    # ---------- import ----------
    def import_files(self):
        paths = filedialog.askopenfilenames(title="选择 JSON 文件", filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")])
        if not paths:
            return
        self._set_files(list(paths))

    def import_folder(self):
        folder = filedialog.askdirectory(title="选择目录（批量读取 *.json）")
        if not folder:
            return
        self._set_files([folder])

    def _set_files(self, paths: List[str]):
        files = collect_json_files(paths)
        if not files:
            messagebox.showwarning("提示", "未找到任何 JSON 文件。")
            return
        self.loaded_files = files
        self.files_label.config(text=f"已导入 {len(files)} 个 JSON")
        self.run_btn.config(state="normal")
        self.export_btn.config(state="disabled")
        self.log(f"已载入文件数: {len(files)}")
        for f in files[:10]:
            self.log(f" - {f}")
        if len(files) > 10:
            self.log(f" ... 还有 {len(files) - 10} 个文件")

    # ---------- calculation ----------
    def run_calc(self):
        if not self.loaded_files:
            return

        try:
            charge_amount = float(self.charge_amount_var.get().strip())
            refund_amount = float(self.refund_amount_var.get().strip())
            initial_balance = float(self.initial_balance_var.get().strip() or "0")
            adjustments = float(self.adjust_var.get().strip() or "0")
        except Exception:
            messagebox.showerror("错误", "额度/余额参数必须是数字。")
            return

        time_field = self.time_field_var.get().strip()
        start = parse_dt(self.start_var.get().strip())
        end = parse_dt(self.end_var.get().strip())
        dedup_mode = self.dedup_var.get().strip()

        def worker():
            try:
                self.log("开始解析 JSON...")
                rows_all: List[TaskRow] = []
                for fp in self.loaded_files:
                    try:
                        tasks = load_tasks_from_json(fp)
                        for t in tasks:
                            r = compute_row(
                                t, fp, time_field, start, end,
                                charge_amount=charge_amount,
                                refund_amount=refund_amount
                            )
                            rows_all.append(r)
                    except Exception as e:
                        self.log(f"❌ 读取失败: {fp} | {e}")

                self.log(f"Raw tasks 行数: {len(rows_all)}")

                rows_used = dedup_by_remote_id(rows_all, prefer=dedup_mode)

                self.log(f"去重后 tasks 行数: {len(rows_used)}（按 remote_id，策略: {dedup_mode}）")

                summ = summarize(
                    rows_all=rows_all,
                    rows_used=rows_used,
                    initial_balance=initial_balance,
                    adjustments=adjustments,
                    time_field=time_field,
                    start=start,
                    end=end
                )

                self.rows_all = rows_all
                self.rows_used = rows_used
                self.summary = summ

                self._refresh_table()
                self._refresh_summary()

                self.export_btn.config(state="normal")
                self.log("✅ 计算完成。")
            except Exception as e:
                self.log(f"❌ 计算错误: {e}")
                messagebox.showerror("错误", str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_table(self):
        for i in self.tree.get_children():
            self.tree.delete(i)

        if not self.rows_used:
            return

        # show only in-time-range to match your "时间范围" requirement
        rows = [r for r in self.rows_used if r.in_time_range]

        # sort by charged_at/created_at
        def sort_key(r: TaskRow):
            return parse_dt(r.charged_at) or parse_dt(r.created_at) or datetime.min

        rows.sort(key=sort_key)

        for r in rows:
            asset = "Y" if r.has_asset else ""
            self.tree.insert(
                "", "end",
                values=(
                    r.source_file, r.task_id, r.remote_id,  # ✅ 新增 remote_id
                    r.source_file, r.task_id, r.provider, r.model, r.status,
                    r.created_at, r.charged_at, r.refunded_at,
                    asset,
                    f"{r.net_delta:.2f}"
                )
            )

    def _refresh_summary(self):
        s = self.summary
        if not s:
            return

        start_s = s.start_time.strftime(TIME_FMT) if s.start_time else "-"
        end_s = s.end_time.strftime(TIME_FMT) if s.end_time else "-"

        txt = (
            f"文件数: {s.files_loaded}\n"
            f"任务数（Raw / 去重后）: {s.tasks_total_raw} / {s.tasks_after_dedup}\n"
            f"时间范围（字段: {s.time_field}）: {start_s}  ~  {end_s}\n\n"
            f"charged 事件数: {s.charged_events}  |  refunded 事件数: {s.refunded_events}\n"
            f"总扣费(支出): {s.total_charged_amount:.2f}\n"
            f"总退款(返还): {s.total_refunded_amount:.2f}\n"
            f"净支出: {s.net_spend:.2f}\n\n"
            f"实际素材数(video_url 非空): {s.assets_count}\n"
            f"异常(退款但无扣费等): {s.anomalies}\n\n"
            f"初始余额: {s.initial_balance:.2f}\n"
            f"手动调整: {s.adjustments:.2f}\n"
            f"期末余额: {s.ending_balance:.2f}"
        )
        self.sum_text.set(txt)

    # ---------- export ----------
    def export_csv(self):
        if not self.summary or not self.rows_used:
            return
        path = filedialog.asksaveasfilename(
            title="保存 CSV 报表",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")]
        )
        if not path:
            return

        rows = [r for r in self.rows_used if r.in_time_range]
        s = self.summary

        # Extra: provider/model aggregation
        by_provider = group_by(rows, lambda r: r.provider)
        by_model = group_by(rows, lambda r: r.model)

        def agg(group: List[TaskRow]) -> Tuple[int, int, float, float, float, int]:
            charged = sum(1 for r in group if r.charged_flag)
            refunded = sum(1 for r in group if r.refunded_flag)
            spent = sum((-r.charge_delta) for r in group if r.charge_delta < 0)
            back = sum((r.refund_delta) for r in group if r.refund_delta > 0)
            net = spent - back
            assets = sum(1 for r in group if r.has_asset)
            return charged, refunded, spent, back, net, assets

        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)

                # Summary sheet (top)
                w.writerow(["=== SUMMARY ==="])
                w.writerow(["files_loaded", s.files_loaded])
                w.writerow(["tasks_total_raw", s.tasks_total_raw])
                w.writerow(["tasks_after_dedup", s.tasks_after_dedup])
                w.writerow(["time_field", s.time_field])
                w.writerow(["start_time", s.start_time.strftime(TIME_FMT) if s.start_time else ""])
                w.writerow(["end_time", s.end_time.strftime(TIME_FMT) if s.end_time else ""])
                w.writerow(["charged_events", s.charged_events])
                w.writerow(["refunded_events", s.refunded_events])
                w.writerow(["total_spent", f"{s.total_charged_amount:.2f}"])
                w.writerow(["total_refunded", f"{s.total_refunded_amount:.2f}"])
                w.writerow(["net_spend", f"{s.net_spend:.2f}"])
                w.writerow(["assets_count(video_url not empty)", s.assets_count])
                w.writerow(["initial_balance", f"{s.initial_balance:.2f}"])
                w.writerow(["adjustments", f"{s.adjustments:.2f}"])
                w.writerow(["ending_balance", f"{s.ending_balance:.2f}"])
                w.writerow([])

                # Provider aggregation
                w.writerow(["=== BY PROVIDER ==="])
                w.writerow(["provider", "charged_events", "refunded_events", "spent", "refunded", "net_spend", "assets"])
                for k, group in sorted(by_provider.items(), key=lambda x: x[0]):
                    c, r, spent, back, net, assets = agg(group)
                    w.writerow([k, c, r, f"{spent:.2f}", f"{back:.2f}", f"{net:.2f}", assets])
                w.writerow([])

                # Model aggregation
                w.writerow(["=== BY MODEL ==="])
                w.writerow(["model", "charged_events", "refunded_events", "spent", "refunded", "net_spend", "assets"])
                for k, group in sorted(by_model.items(), key=lambda x: x[0]):
                    c, r, spent, back, net, assets = agg(group)
                    w.writerow([k, c, r, f"{spent:.2f}", f"{back:.2f}", f"{net:.2f}", assets])
                w.writerow([])

                # Task details
                w.writerow(["=== TASK DETAILS (IN RANGE) ==="])
                w.writerow(["source_file", "task_id", "provider", "model", "status", "created_at", "charged_at", "refunded_at",
                            "has_asset(video_url)", "charge_delta", "refund_delta", "net_delta", "video_url"])
                for r in rows:
                    w.writerow([
                        r.source_file, r.task_id, r.provider, r.model, r.status,
                        r.created_at, r.charged_at, r.refunded_at,
                        "Y" if r.has_asset else "",
                        f"{r.charge_delta:.2f}", f"{r.refund_delta:.2f}", f"{r.net_delta:.2f}",
                        r.video_url
                    ])

            self.log(f"✅ 已导出 CSV: {path}")
            messagebox.showinfo("完成", f"CSV 已导出：\n{path}")
        except Exception as e:
            messagebox.showerror("错误", str(e))


if __name__ == "__main__":
    app = App()
    app.mainloop()
