#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import csv
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText
from collections import defaultdict, OrderedDict

APP_TITLE = "文本物品统计 (会话累计 + 并列TopN + 计分可配 + 汇总/截图模式)"
APP_GEOMETRY = "1400x940"

# ====== 配置：可按需修改 ======
DEFAULT_UNITS = "个|单|组|份|只|条|袋|盒|件|对|枚|本|朵|颗|支|张|把|顶|块|瓶|双|套|杯|台|辆|根|片|幅|款"
DEFAULT_IGNORE_ITEMS = "单|次日|组|practicalpicks|米|Wohlf|Mia|百k"   # 黑名单（完全匹配）
DEFAULT_MERGE_RULES = (
    "猩猩 = 猩猩冰箱贴\n"
    "转笔 = 转转笔\n"
    "圣诞树 = 圣诞树冰箱贴\n"
    "小熊 = 蜥蜴小熊\n"
    "小蝾螈 = 小蝾螈宝宝\n"
    "小蝾螈 = 蝾螈宝宝\n"
    "黄鹤楼 = 武汉冰箱贴\n"
)
# 计分默认：Top3 档，分值 15/10/5
DEFAULT_TOP_N_LEVELS = 3
DEFAULT_POINTS_TEXT = "15,10,5"   # 逗号分隔

# “总计/合计”行 & 名称清洗规则
STOP_PREFIX = ("共", "大约", "约", "大概")
STOP_INFIX  = ("视频出",)
STOP_SUFFIX = ("左右", "约", "大概")

# ====== 中文数字：简单 0~99 转换 ======
CN_NUM = {"零":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9}
def cn_to_int(s: str) -> int:
    s = s.strip()
    if not s:
        return 0
    if re.fullmatch(r"\d+", s):
        return int(s)
    if s == "十":
        return 10
    m = re.fullmatch(r"([一二两三四五六七八九])?十([一二两三四五六七八九])?", s)
    if m:
        tens = CN_NUM.get(m.group(1), 1)
        ones = CN_NUM.get(m.group(2), 0)
        return tens * 10 + ones
    if s in CN_NUM:
        return CN_NUM[s]
    digits = "".join(str(CN_NUM.get(ch, "")) for ch in s)
    return int(digits) if digits else 0

# 更稳的 URL 清理（遇到中文就停止，不吞“圣诞树2”之类）
def strip_urls(text: str) -> str:
    return re.sub(r"https?://[^\s\u4e00-\u9fa5]+", " ", text)

# 去掉 “TikTok · 英文名 …” 前缀，并把英文昵称从中文物品前移除
def drop_tiktok_prefix(name: str) -> str:
    name = name.strip()
    name = re.sub(r"^\s*TikTok\s*[·.\-]?\s*", "", name, flags=re.I)  # 去掉 "TikTok ·"
    name = re.sub(r"^[A-Za-z0-9_ \-]+(?=[\u4e00-\u9fa5])", "", name)  # 丢弃前导英文
    name = re.sub(r"\s+", "", name)
    return name

def clean_name(name: str) -> str:
    name = drop_tiktok_prefix(name)
    for p in STOP_INFIX:
        name = name.replace(p, "")
    for sp in STOP_PREFIX:
        if name.startswith(sp):
            name = name[len(sp):]
    for ss in STOP_SUFFIX:
        if name.endswith(ss):
            name = name[:-len(ss)]
    # 仅保留常见字符（避免把“圣诞树冰箱贴”等拆坏）
    name = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9·\-—_贴冰箱树蜥猩蝾转笔熊蛙]", "", name)
    # 再兜底一次：如果前面仍有英文昵称而后面是中文，去掉前导英文
    name = re.sub(r"^[A-Za-z0-9_·\-]+(?=[\u4e00-\u9fa5])", "", name)
    return name

def is_summary_line(s: str) -> bool:
    # 忽略 “总：”/“总计/合计/小计 …” 行
    return bool(re.match(r"^\s*(总|总计|合计|小计)\s*[:：]", s)) or ("总计" in s or "合计" in s or "小计" in s)

# —— 黑名单过滤
def filter_counts(counts: dict, ignore_items_text: str) -> dict:
    ignore = {s.strip() for s in re.split(r"[|,，\s]+", ignore_items_text or "") if s.strip()}
    if not ignore:
        return counts
    return {k: v for k, v in counts.items() if k not in ignore}

# —— 合并规则解析与应用
def parse_merge_rules(text: str) -> dict:
    """
    解析多行规则：
      规范名 = 别名1 | 别名2 | ...
    或单行： 规范名 | 别名1 | 别名2
    返回 alias_map: {任一别名或规范名: 规范名}
    """
    alias_map = {}
    if not text:
        return alias_map
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        if "=" in ln:
            left, right = ln.split("=", 1)
            canon = clean_name(left)
            aliases = [clean_name(x) for x in re.split(r"[|,，]+", right) if x.strip()]
            for a in [canon] + aliases:
                if a:
                    alias_map[a] = canon
        elif ":" in ln:
            left, right = ln.split(":", 1)
            canon = clean_name(left)
            aliases = [clean_name(x) for x in re.split(r"[|,，]+", right) if x.strip()]
            for a in [canon] + aliases:
                if a:
                    alias_map[a] = canon
        else:
            parts = [clean_name(p) for p in re.split(r"[|,，]+", ln) if p.strip()]
            if not parts:
                continue
            canon = parts[0]
            for a in parts:
                alias_map[a] = canon
    return alias_map

def merge_counts(counts: dict, alias_map: dict) -> dict:
    if not alias_map:
        return counts
    merged = defaultdict(int)
    for k, v in counts.items():
        canon = alias_map.get(k, k)
        merged[canon] += v
    return dict(merged)

# 支持“总共/一共/出”等插入词的提取
def extract_counts(text: str, units_regex: str, ignore_summary=True):
    counts = defaultdict(int)
    text = strip_urls(text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    UNIT_RE = f"(?:{units_regex})?"  # 单位可有可无
    FILL_RE = r"(?:总共|一共|共|合计)?\s*(?:视频出|出|共出|合计出)?"

    # ① 物品 + (可选插入词) + 数字(+单位)
    pat1 = re.compile(
        rf"(?P<name>[\u4e00-\u9fa5A-Za-z·.\- ]{{1,40}}?)\s*{FILL_RE}\s*(?P<num>\d+|[一二两三四五六七八九十]+)\s*{UNIT_RE}(?!\w)"
    )
    # ② 数字(+单位) + 物品
    pat2 = re.compile(
        rf"(?P<num>\d+|[一二两三四五六七八九十]+)\s*{UNIT_RE}\s*(?P<name>[\u4e00-\u9fa5A-Za-z·.\- ]{{1,40}})"
    )

    for ln in lines:
        if ignore_summary and is_summary_line(ln):
            continue

        for m in pat1.finditer(ln):
            name = clean_name(m.group("name"))
            num = cn_to_int(m.group("num"))
            if name and num > 0:
                counts[name] += num

        for m in pat2.finditer(ln):
            name = clean_name(m.group("name"))
            num = cn_to_int(m.group("num"))
            if name and num > 0:
                counts[name] += num

    counts.pop("", None)
    return dict(counts)

# ================ GUI ================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(APP_GEOMETRY)
        self.minsize(1240, 860)

        # 运行时选项
        self.var_units = tk.StringVar(value=DEFAULT_UNITS)
        self.var_ignore_summary = tk.BooleanVar(value=True)
        self.var_ignore_items = tk.StringVar(value=DEFAULT_IGNORE_ITEMS)
        self.var_use_first_line = tk.BooleanVar(value=True)  # 用首行作为段名
        self.var_presentation = tk.BooleanVar(value=True)     # 截图模式（可读性）
        self.var_top_levels = tk.IntVar(value=DEFAULT_TOP_N_LEVELS)
        self.var_points_text = tk.StringVar(value=DEFAULT_POINTS_TEXT)

        # 会话：保存多段文本（只存原文与名称，统计时按当前规则重算）
        # 每项：{"name": "自定义名/文本N", "text": 原文字符串}
        self.session = []

        self._build_ui()
        self._apply_style()  # 默认应用“截图模式”样式

    # ---------- 样式 ----------
    def _apply_style(self):
        style = ttk.Style(self)
        style.theme_use("default")

        # 行高 & 字体
        base_font = ("Microsoft YaHei UI", 10)
        big_font  = ("Microsoft YaHei UI", 12, "bold")
        head_font = ("Microsoft YaHei UI", 12, "bold") if self.var_presentation.get() else ("Microsoft YaHei UI", 10, "bold")
        row_h = 28 if self.var_presentation.get() else 22

        style.configure("Treeview",
                        font=base_font,
                        rowheight=row_h)
        style.configure("Treeview.Heading",
                        font=head_font,
                        background="#1f2937" if self.var_presentation.get() else "#e5e7eb",
                        foreground="#ffffff" if self.var_presentation.get() else "#111827",
                        padding=6 if self.var_presentation.get() else 4)
        style.map("Treeview.Heading",
                  background=[("active", "#111827")] if self.var_presentation.get() else [("active", "#d1d5db")])

        # 斑马纹（通过 tag）
        self.row_bg_even = "#f8fafc" if self.var_presentation.get() else "#ffffff"
        self.row_bg_odd  = "#eef2ff" if self.var_presentation.get() else "#f9fafb"

    def _zebra_fill(self, tree: ttk.Treeview):
        # 重新上色
        for idx, iid in enumerate(tree.get_children()):
            tree.item(iid, tags=("even" if idx % 2 == 0 else "odd",))
        tree.tag_configure("even", background=self.row_bg_even)
        tree.tag_configure("odd",  background=self.row_bg_odd)

    # ---------- UI ----------
    def _build_ui(self):
        container = ttk.PanedWindow(self, orient="horizontal")
        container.pack(fill="both", expand=True)

        # ---------- 左侧 ----------
        left = ttk.Frame(container); container.add(left, weight=3)

        frm_top = ttk.LabelFrame(left, text="输入文本（本段）")
        frm_top.pack(fill="x", padx=10, pady=(10, 6))
        self.txt = ScrolledText(frm_top, height=8, wrap="word")
        self.txt.pack(fill="x", padx=8, pady=8)

        frm_opts = ttk.LabelFrame(left, text="识别与规则")
        frm_opts.pack(fill="x", padx=10, pady=6)

        row = ttk.Frame(frm_opts); row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="量词（| 分隔）：").pack(side="left")
        ttk.Entry(row, textvariable=self.var_units).pack(side="left", fill="x", expand=True, padx=(6, 0))

        row2 = ttk.Frame(frm_opts); row2.pack(fill="x", padx=8, pady=6)
        ttk.Checkbutton(row2, text="忽略含“总：/总计/合计/小计”的整行", variable=self.var_ignore_summary).pack(side="left")
        ttk.Label(row2, text="忽略物品（黑名单，| 分隔）：").pack(side="left", padx=(12,0))
        ttk.Entry(row2, textvariable=self.var_ignore_items, width=28).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(row2, text="用首行作为段名", variable=self.var_use_first_line).pack(side="left", padx=(12,0))
        ttk.Checkbutton(row2, text="截图模式（高可读）", variable=self.var_presentation, command=self._on_toggle_presentation).pack(side="left", padx=(12,0))

        # 计分配置
        frm_score = ttk.LabelFrame(left, text="计分设置（并列同档同分）")
        frm_score.pack(fill="x", padx=10, pady=6)
        row3 = ttk.Frame(frm_score); row3.pack(fill="x", padx=8, pady=6)
        ttk.Label(row3, text="档位数量 TopN：").pack(side="left")
        ttk.Spinbox(row3, from_=1, to=10, textvariable=self.var_top_levels, width=5).pack(side="left", padx=(6, 12))
        ttk.Label(row3, text="各档分值（逗号分隔，例：15,10,5）：").pack(side="left")
        ttk.Entry(row3, textvariable=self.var_points_text, width=20).pack(side="left", padx=(6, 0))

        # 合并规则
        frm_merge = ttk.LabelFrame(left, text="合并规则（每行：规范名 = 别名1 | 别名2 | ...）")
        frm_merge.pack(fill="x", padx=10, pady=6)
        self.txt_merge = ScrolledText(frm_merge, height=4, wrap="word")
        self.txt_merge.pack(fill="x", padx=8, pady=8)
        self.txt_merge.insert("1.0", DEFAULT_MERGE_RULES)

        frm_btns = ttk.Frame(left); frm_btns.pack(fill="x", padx=10, pady=(0,6))
        ttk.Button(frm_btns, text="加入会话（保存本段）", command=self.add_batch).pack(side="left")
        ttk.Button(frm_btns, text="统计全部（会话内，按当前规则）", command=self.analyze_session).pack(side="left", padx=8)
        ttk.Button(frm_btns, text="生成汇总文本（截图转发）", command=self.generate_summary_text).pack(side="left", padx=8)
        ttk.Button(frm_btns, text="清空输入框", command=self.clear_input).pack(side="left", padx=8)

        # 选项卡
        nb = ttk.Notebook(left); nb.pack(fill="both", expand=True, padx=10, pady=(6,10))

        # 表1：分段统计
        frm_tbl1 = ttk.Frame(nb); nb.add(frm_tbl1, text="表1：分段统计")
        cols1 = ("group","item","count")
        self.tree_counts = ttk.Treeview(frm_tbl1, columns=cols1, show="headings", height=12)
        for c, w, anc, txt in [
            ("group", 320, "w", "段名"),
            ("item",  520, "w", "物品"),
            ("count", 120, "center", "数量"),
        ]:
            self.tree_counts.heading(c, text=txt)
            self.tree_counts.column(c, width=w, anchor=anc)
        self.tree_counts.pack(fill="both", expand=True, side="left", padx=(8,0), pady=8)
        vsb1 = ttk.Scrollbar(frm_tbl1, orient="vertical", command=self.tree_counts.yview)
        hsb1 = ttk.Scrollbar(frm_tbl1, orient="horizontal", command=self.tree_counts.xview)
        self.tree_counts.configure(yscrollcommand=vsb1.set, xscrollcommand=hsb1.set)
        vsb1.pack(side="right", fill="y", padx=(0,8), pady=8)
        hsb1.pack(side="bottom", fill="x", padx=8, pady=(0,8))

        # 表2：每个物品TopN（并列合并）
        frm_tbl2 = ttk.Frame(nb); nb.add(frm_tbl2, text="表2：每个物品TopN（并列合并）")
        cols2 = ("item","lvl1","lvl2","lvl3","more")
        self.tree_topn = ttk.Treeview(frm_tbl2, columns=cols2, show="headings", height=12)
        for c, w, anc, txt in [
            ("item",  260, "w", "物品"),
            ("lvl1",  300, "w", "第1档 段：数量"),
            ("lvl2",  300, "w", "第2档 段：数量"),
            ("lvl3",  300, "w", "第3档 段：数量"),
            ("more",  160, "w", "更多档位…"),
        ]:
            self.tree_topn.heading(c, text=txt)
            self.tree_topn.column(c, width=w, anchor=anc)
        self.tree_topn.pack(fill="both", expand=True, side="left", padx=(8,0), pady=8)
        vsb2 = ttk.Scrollbar(frm_tbl2, orient="vertical", command=self.tree_topn.yview)
        hsb2 = ttk.Scrollbar(frm_tbl2, orient="horizontal", command=self.tree_topn.xview)
        self.tree_topn.configure(yscrollcommand=vsb2.set, xscrollcommand=hsb2.set)
        vsb2.pack(side="right", fill="y", padx=(0,8), pady=8)
        hsb2.pack(side="bottom", fill="x", padx=8, pady=(0,8))

        # 表3：段分数
        frm_tbl3 = ttk.Frame(nb); nb.add(frm_tbl3, text="表3：段分数（TopN计分）")
        cols3 = ("group","score","detail")
        self.tree_scores = ttk.Treeview(frm_tbl3, columns=cols3, show="headings", height=12)
        for c, w, anc, txt in [
            ("group", 320, "w", "段名"),
            ("score", 120, "center", "总分"),
            ("detail", 700, "w", "明细（物品=分值…）"),
        ]:
            self.tree_scores.heading(c, text=txt)
            self.tree_scores.column(c, width=w, anchor=anc)
        self.tree_scores.pack(fill="both", expand=True, side="left", padx=(8,0), pady=8)
        vsb3 = ttk.Scrollbar(frm_tbl3, orient="vertical", command=self.tree_scores.yview)
        hsb3 = ttk.Scrollbar(frm_tbl3, orient="horizontal", command=self.tree_scores.xview)
        self.tree_scores.configure(yscrollcommand=vsb3.set, xscrollcommand=hsb3.set)
        vsb3.pack(side="right", fill="y", padx=(0,8), pady=8)
        hsb3.pack(side="bottom", fill="x", padx=8, pady=(0,8))

        # 简洁汇总文本（截图友好）
        frm_sum = ttk.Frame(nb); nb.add(frm_sum, text="汇总（截图友好）")
        self.txt_summary = ScrolledText(frm_sum, height=20, wrap="word",
                                        font=("Microsoft YaHei UI", 12))
        self.txt_summary.pack(fill="both", expand=True, padx=8, pady=8)

        # 表格操作按钮
        frm_tbl_btns = ttk.Frame(left); frm_tbl_btns.pack(fill="x", padx=10, pady=(0,6))
        ttk.Button(frm_tbl_btns, text="复制表1", command=self.copy_table_counts).pack(side="left")
        ttk.Button(frm_tbl_btns, text="复制表2", command=self.copy_table_topn).pack(side="left", padx=8)
        ttk.Button(frm_tbl_btns, text="复制分数", command=self.copy_table_scores).pack(side="left", padx=8)
        ttk.Button(frm_tbl_btns, text="导出两表 CSV", command=self.export_both_csv).pack(side="left", padx=8)
        ttk.Button(frm_tbl_btns, text="导出分数 CSV", command=self.export_scores_csv).pack(side="left", padx=8)
        ttk.Button(frm_tbl_btns, text="导出汇总 TXT", command=self.export_summary_txt).pack(side="left", padx=8)

        # ---------- 右侧：会话管理 ----------
        right = ttk.Frame(container); container.add(right, weight=2)
        frm_sess = ttk.LabelFrame(right, text="会话（已加入的段）")
        frm_sess.pack(fill="both", expand=True, padx=10, pady=(10, 10))
        self.listbox = tk.Listbox(frm_sess, height=22, selectmode="extended")  # 支持多选
        self.listbox.pack(fill="both", expand=True, padx=8, pady=(8,4))
        btns = ttk.Frame(frm_sess); btns.pack(fill="x", padx=8, pady=(0,8))
        ttk.Button(btns, text="全选", command=self.select_all).pack(side="left")
        ttk.Button(btns, text="删除选中", command=self.remove_selected).pack(side="left", padx=8)
        ttk.Button(btns, text="撤回上一次", command=self.undo_last).pack(side="left", padx=8)
        ttk.Button(btns, text="清空会话", command=self.clear_session).pack(side="left", padx=8)

        self.status = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=12, pady=(0,10))

    # ---------- 会话操作 ----------
    def add_batch(self):
        text = self.txt.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("提示", "请先在左侧输入框粘贴一段文本。")
            return

        # 默认名
        idx = len(self.session) + 1
        name = f"文本{idx}"

        # 用首行作为段名（可选）
        if self.var_use_first_line.get():
            first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            first_line = re.sub(r"https?://[^\s\u4e00-\u9fa5]+", "", first_line).strip()
            first_line = re.sub(r"\s+", " ", first_line)
            if first_line:
                name = first_line[:40]

        # 保证名称唯一
        existing = {rec["name"] for rec in self.session}
        if name in existing:
            k = 2
            while f"{name}#{k}" in existing:
                k += 1
            name = f"{name}#{k}"

        # 保存
        self.session.append({"name": name, "text": text})
        self.listbox.insert("end", f"{name} | （已加入）")
        self.txt.delete("1.0", "end")
        self.status.set(f"已加入会话：{name}")

    def select_all(self):
        self.listbox.select_set(0, "end")

    def remove_selected(self):
        sel = list(self.listbox.curselection())
        if not sel:
            messagebox.showinfo("提示", "请先在右侧列表选择要删除的段（可多选）。")
            return
        # 从后往前删，避免下标位移
        for i in sorted(sel, reverse=True):
            del self.session[i]
            self.listbox.delete(i)
        self.status.set(f"已删除 {len(sel)} 条。")

    def undo_last(self):
        if not self.session:
            messagebox.showinfo("提示", "会话为空。")
            return
        self.session.pop()
        self.listbox.delete("end")
        self.status.set("已撤回上一次加入的段。")

    def clear_session(self):
        self.session.clear()
        self.listbox.delete(0, "end")
        self.status.set("会话已清空。")

    def clear_input(self):
        self.txt.delete("1.0", "end")
        self.status.set("输入框已清空。")

    # ---------- 统计与展示 ----------
    def _parse_points(self):
        # 解析分值文本
        txt = self.var_points_text.get().strip()
        pts = []
        for p in re.split(r"[,\s，]+", txt):
            if p.strip().isdigit():
                pts.append(int(p.strip()))
        # 保证长度>=TopN，短了就用0填充；长了截断
        topn = max(1, int(self.var_top_levels.get()))
        if len(pts) < topn:
            pts += [0] * (topn - len(pts))
        else:
            pts = pts[:topn]
        return topn, pts

    def analyze_session(self):
        for tv in (self.tree_counts, self.tree_topn, self.tree_scores):
            for iid in tv.get_children():
                tv.delete(iid)
        self.txt_summary.delete("1.0", "end")

        if not self.session:
            messagebox.showinfo("提示", "会话里还没有任何段。先点击“加入会话”。")
            return

        # 当前规则
        units = self.var_units.get().strip() or DEFAULT_UNITS
        ignore_summary = self.var_ignore_summary.get()
        ignore_items_text = self.var_ignore_items.get()
        alias_map = parse_merge_rules(self.txt_merge.get("1.0", "end"))

        # 解析计分参数
        TOPN, POINTS = self._parse_points()

        # 表1：分段统计
        per_group = OrderedDict()  # 段 -> {item:count}
        for rec in self.session:
            counts = extract_counts(rec["text"], units_regex=units, ignore_summary=ignore_summary)
            counts = filter_counts(counts, ignore_items_text)
            counts = merge_counts(counts, alias_map)
            per_group[rec["name"]] = counts

        rows1 = 0
        for gname, d in per_group.items():
            if not d:
                iid = self.tree_counts.insert("", "end", values=(gname, "（未识别到记录）", ""))
            else:
                for item, cnt in sorted(d.items(), key=lambda kv: (-kv[1], kv[0])):
                    iid = self.tree_counts.insert("", "end", values=(gname, item, cnt))
                    rows1 += 1
        self._zebra_fill(self.tree_counts)

        # 表2：每个物品TopN（并列合并） & 计分
        item_buckets = defaultdict(list)  # item -> [(group, count)]
        for gname, d in per_group.items():
            for item, cnt in d.items():
                item_buckets[item].append((gname, cnt))

        # 排序规则：按最高档数量降序，再按物品名
        ranked_items = sorted(
            item_buckets.items(),
            key=lambda kv: (-max((c for _, c in kv[1]), default=0), kv[0])
        )

        group_scores = defaultdict(int)
        group_details = defaultdict(list)

        # 构建“汇总文本”缓冲
        summary_lines = []
        summary_lines.append("【总览】")
        summary_lines.append(f"段数：{len(self.session)} ｜ 物品种类：{len(ranked_items)} ｜ 计分TopN：{TOPN}（分值：{','.join(map(str,POINTS))}）")
        summary_lines.append("")

        summary_lines.append("【每个物品TopN（并列合并）】")
        for item, pairs in ranked_items:
            pairs.sort(key=lambda x: (-x[1], x[0]))
            # 聚合为 count -> [groups...]
            level = OrderedDict()
            for g, c in pairs:
                level.setdefault(c, []).append(g)
            # 取前 TOPN 个不同数量层级
            levels = list(level.items())[:TOPN]

            def fmt(entry):
                c, gs = entry
                return f"{' / '.join(gs)}：{c}"

            # Treeview 显示：前三档占三列，其余合并到“更多”
            col_vals = [item]
            more_texts = []
            for i in range(TOPN):
                if i < 3:
                    col_vals.append(fmt(levels[i]) if i < len(levels) else "")
                else:
                    if i < len(levels):
                        more_texts.append(fmt(levels[i]))
            # 填够列
            while len(col_vals) < 4:
                col_vals.append("")
            more_join = "；".join(more_texts)
            col_vals.append(more_join)
            self.tree_topn.insert("", "end", values=tuple(col_vals))

            # 汇总文本
            line = f"● {item}："
            parts = []
            for i, e in enumerate(levels, 1):
                parts.append(f"第{i}档[{fmt(e)}]")
            summary_lines.append(line + "； ".join(parts))

            # 计分
            for idx, (_, groups) in enumerate(levels):
                pts = POINTS[idx]
                if pts <= 0:
                    continue
                for g in groups:
                    group_scores[g] += pts
                    group_details[g].append(f"{item}={pts}")

        self._zebra_fill(self.tree_topn)

        # 表3：段分数（按总分降序显示更直观）
        ranked_groups = sorted(per_group.keys(), key=lambda g: (-group_scores.get(g, 0), g))
        summary_lines.append("")
        summary_lines.append("【段分数】（按总分降序）")
        for gname in ranked_groups:
            score = group_scores.get(gname, 0)
            detail = "，".join(group_details.get(gname, []))
            self.tree_scores.insert("", "end", values=(gname, score, detail))
            summary_lines.append(f"{gname}：{score} 分  （{detail}）")
        self._zebra_fill(self.tree_scores)

        # 汇总文本放入文本框
        self.txt_summary.delete("1.0", "end")
        self.txt_summary.insert("1.0", "\n".join(summary_lines))

        self.status.set(
            f"统计完成：{len(self.session)} 段 | 表1 {rows1} 行 | 物品 {len(ranked_items)} 种 | 已计算分数并生成汇总"
        )

    # ---------- 汇总导出 ----------
    def generate_summary_text(self):
        # 单独生成（不清表）
        if not self.session:
            messagebox.showinfo("提示", "会话为空，先加入几段再生成汇总。")
            return
        self.analyze_session()  # 直接复用现成流程，保证一致

    # ---------- 表格复制/导出 ----------
    def copy_table_counts(self):
        self._copy_tree(self.tree_counts, header=["段名","物品","数量"])

    def copy_table_topn(self):
        self._copy_tree(self.tree_topn, header=["物品","第1档 段：数量","第2档 段：数量","第3档 段：数量","更多档位…"])

    def copy_table_scores(self):
        self._copy_tree(self.tree_scores, header=["段名","总分","明细（物品=分值…）"])

    def export_both_csv(self):
        if not self.tree_counts.get_children() and not self.tree_topn.get_children():
            messagebox.showinfo("提示", "请先点击“统计全部（会话内，按当前规则）”。")
            return
        path1 = filedialog.asksaveasfilename(defaultextension=".csv",
                    filetypes=[("CSV files","*.csv"),("All files","*.*")],
                    title="保存 表1 CSV（分段统计）")
        if not path1: return
        path2 = filedialog.asksaveasfilename(defaultextension=".csv",
                    filetypes=[("CSV files","*.csv"),("All files","*.*")],
                    title="保存 表2 CSV（TopN 并列合并）")
        if not path2: return
        # 导出表1
        rows1 = [self.tree_counts.item(iid, "values") for iid in self.tree_counts.get_children()]
        with open(path1, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["group","item","count"])
            for r in rows1: w.writerow(r)
        # 导出表2
        rows2 = [self.tree_topn.item(iid, "values") for iid in self.tree_topn.get_children()]
        with open(path2, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["item","lvl1","lvl2","lvl3","more"])
            for r in rows2: w.writerow(r)
        self.status.set(f"已导出：{path1}  与  {path2}")

    def export_scores_csv(self):
        if not self.tree_scores.get_children():
            messagebox.showinfo("提示", "请先点击“统计全部（会话内，按当前规则）”。")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                    filetypes=[("CSV files","*.csv"),("All files","*.*")],
                    title="保存 表3 CSV（段分数）")
        if not path: return
        rows = [self.tree_scores.item(iid, "values") for iid in self.tree_scores.get_children()]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["group","score","detail"])
            for r in rows: w.writerow(r)
        self.status.set(f"已导出：{path}")

    def export_summary_txt(self):
        if not self.txt_summary.get("1.0","end").strip():
            messagebox.showinfo("提示", "请先生成汇总文本。")
            return
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                    filetypes=[("Text files","*.txt"),("All files","*.*")],
                    title="保存 汇总 TXT")
        if not path: return
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.txt_summary.get("1.0","end").strip())
        self.status.set(f"已导出：{path}")

    def _copy_tree(self, tree: ttk.Treeview, header=None):
        rows = [tree.item(iid, "values") for iid in tree.get_children()]
        if not rows:
            messagebox.showinfo("提示", "当前表暂无数据可复制。")
            return
        lines = []
        if header:
            lines.append(",".join(header))
        for r in rows:
            out = []
            for cell in r:
                s = str(cell)
                if "," in s or '"' in s:
                    s = '"' + s.replace('"','""') + '"'
                out.append(s)
            lines.append(",".join(out))
        text = "\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.set("已复制到剪贴板")

    def _on_toggle_presentation(self):
        self._apply_style()
        # 重新画斑马纹
        self._zebra_fill(self.tree_counts)
        self._zebra_fill(self.tree_topn)
        self._zebra_fill(self.tree_scores)

def main():
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
