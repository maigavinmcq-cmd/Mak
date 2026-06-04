#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_TITLE = "麦组工作表公式生成器"
APP_SIZE = "840x660"

DEFAULTS = {
    "sheet": "重点运营账号数据",
    "col": "G",

    # 👉 首块总区间（例如 G3:G13）
    "start_first": 3,   # 区间起始行（首块）
    "start_last": 13,   # 区间结束行（首块）

    # 👉 第二块起始行（例如下一块是 G16:G26，则 next_first = 16）
    # 程序会自动计算 step = next_first - first
    "next_first": 16,

    # 最后一块的结束行上限
    "end_last": 409,

    # 默认分段：示例 2 段（你可以 GUI 里继续添加更多段）
    "segments": [
        {"len": 6, "rule": "直接求和"},                    # 比如 G3:G8
        {"len": 5, "rule": "求和后除以除数", "divisor": 20},  # 比如 G9:G13
    ],

    "default_divisor": 20,  # 新增分段时默认除数
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(APP_SIZE)
        self.minsize(800, 600)

        # 原有参数
        self.var_sheet = tk.StringVar(value=DEFAULTS["sheet"])
        self.var_col = tk.StringVar(value=DEFAULTS["col"])
        self.var_start_first = tk.StringVar(value=str(DEFAULTS["start_first"]))
        self.var_start_last  = tk.StringVar(value=str(DEFAULTS["start_last"]))
        self.var_next_first  = tk.StringVar(value=str(DEFAULTS["next_first"]))
        self.var_end_last    = tk.StringVar(value=str(DEFAULTS["end_last"]))

        # 输出模式：single=合并为一条公式，multi=分段输出（每段全部区块统一输出）
        self.var_mode = tk.StringVar(value="single")

        # 分段规则
        self.rule_choices = ("直接求和", "求和后除以除数")
        self.segments = []  # 每个元素：{"frame", "label", "len_var", "rule_var", "div_var", "div_label", "div_entry"}

        self._build_ui()

    # ===================== UI =====================

    def _build_ui(self):
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True, padx=10, pady=10)

        # ---------- 参数设置 ----------
        frm = ttk.LabelFrame(root, text="参数设置")
        frm.pack(fill="x", pady=(0, 10))

        # 行1：工作表名、列
        r1 = ttk.Frame(frm); r1.pack(fill="x", padx=10, pady=4)
        ttk.Label(r1, text="工作表名（Sheet）：").pack(side="left")
        ttk.Entry(r1, width=24, textvariable=self.var_sheet).pack(side="left", padx=(6, 20))
        ttk.Label(r1, text="公式计算列（如 G）：").pack(side="left")
        ttk.Entry(r1, width=8, textvariable=self.var_col).pack(side="left", padx=(6, 0))

        # 行2：首块 first, last
        r2 = ttk.Frame(frm); r2.pack(fill="x", padx=10, pady=4)
        ttk.Label(r2, text="首块区间起始行（first）：").pack(side="left")
        ttk.Entry(r2, width=10, textvariable=self.var_start_first).pack(side="left", padx=(6, 20))
        ttk.Label(r2, text="首块区间结束行（last）：").pack(side="left")
        ttk.Entry(r2, width=10, textvariable=self.var_start_last).pack(side="left", padx=(6, 0))

        # 行3：下一个区块起始行 + end_last（自动算 step）
        r3 = ttk.Frame(frm); r3.pack(fill="x", padx=10, pady=4)
        ttk.Label(r3, text="下一个区块起始行（next_first）：").pack(side="left")
        ttk.Entry(r3, width=10, textvariable=self.var_next_first).pack(side="left", padx=(6, 20))
        ttk.Label(r3, text="最后一块的结束行上限（end_last）：").pack(side="left")
        ttk.Entry(r3, width=10, textvariable=self.var_end_last).pack(side="left", padx=(6, 0))

        # 行4：输出模式
        r4 = ttk.Frame(frm); r4.pack(fill="x", padx=10, pady=4)
        ttk.Label(r4, text="输出模式：").pack(side="left")
        ttk.Radiobutton(
            r4, text="合并为一条公式",
            variable=self.var_mode, value="single"
        ).pack(side="left", padx=(6, 10))
        ttk.Radiobutton(
            r4, text="分段输出",
            variable=self.var_mode, value="multi"
        ).pack(side="left")

        # ---------- 分段规则 ----------
        seg_group = ttk.LabelFrame(root, text="分段规则设置（自上而下依次）")
        seg_group.pack(fill="x", pady=(0, 10))

        header = ttk.Frame(seg_group)
        header.pack(fill="x", padx=10, pady=(4, 0))
        ttk.Label(header, text="段次", width=8).grid(row=0, column=0, padx=3)
        ttk.Label(header, text="段长度（行数）", width=16).grid(row=0, column=1, padx=3)
        ttk.Label(header, text="计算规则", width=24).grid(row=0, column=2, padx=3)
        ttk.Label(header, text="除数（若使用）", width=16).grid(row=0, column=3, padx=3)

        self.seg_rows_container = ttk.Frame(seg_group)
        self.seg_rows_container.pack(fill="x", padx=10, pady=4)

        btn_row = ttk.Frame(seg_group)
        btn_row.pack(fill="x", padx=10, pady=(2, 6))
        ttk.Button(btn_row, text="添加分段", command=self.add_segment).pack(side="left")
        ttk.Button(btn_row, text="删除最后一段", command=self.remove_last_segment).pack(side="left", padx=8)

        # 初始化默认分段
        self._init_default_segments()

        # ---------- 按钮行 ----------
        rbtn = ttk.Frame(root); rbtn.pack(fill="x", pady=(0, 8))
        ttk.Button(rbtn, text="生成公式", command=self.generate).pack(side="left")
        ttk.Button(rbtn, text="复制全部", command=self.copy_all).pack(side="left", padx=(8,0))
        ttk.Button(rbtn, text="保存为文件…", command=self.save_file).pack(side="left", padx=(8,0))
        ttk.Button(rbtn, text="重置默认", command=self.reset_defaults).pack(side="right")

        # ---------- 输出区 ----------
        outfrm = ttk.LabelFrame(root, text="输出结果（每行一条 Excel 公式）")
        outfrm.pack(fill="both", expand=True, pady=(0, 0))

        self.txt = tk.Text(outfrm, wrap="none", undo=True)
        self.txt.pack(fill="both", expand=True, padx=8, pady=8)

        yscroll = ttk.Scrollbar(self.txt.master, orient="vertical", command=self.txt.yview)
        xscroll = ttk.Scrollbar(self.txt.master, orient="horizontal", command=self.txt.xview)
        self.txt.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        yscroll.pack(side="right", fill="y")
        xscroll.pack(side="bottom", fill="x")

        # 快捷键：Ctrl+A 全选
        self.txt.bind("<Control-a>", self._select_all)

    # ============ 分段行管理 ============

    def _init_default_segments(self):
        # 清空原有
        for seg in self.segments:
            seg["frame"].destroy()
        self.segments.clear()

        for seg_def in DEFAULTS["segments"]:
            self._create_segment_row(
                length=str(seg_def.get("len", "")),
                rule=seg_def.get("rule", self.rule_choices[0]),
                divisor=str(seg_def.get("divisor", DEFAULTS["default_divisor"]))
            )

    def _on_rule_change(self, rule_var, div_label, div_entry):
        """当规则切换时，控制除数输入框是否显示"""
        rule = rule_var.get().strip()
        if rule == "求和后除以除数":
            if not div_label.winfo_ismapped():
                div_label.pack(side="left", padx=(6, 2))
                div_entry.pack(side="left", padx=(0, 0))
        else:
            # 隐藏除数输入框
            if div_label.winfo_ismapped():
                div_label.pack_forget()
            if div_entry.winfo_ismapped():
                div_entry.pack_forget()

    def _create_segment_row(self, length: str = "", rule: str = None, divisor: str = None):
        idx = len(self.segments)
        row = ttk.Frame(self.seg_rows_container)
        row.pack(fill="x", pady=2)

        lbl = ttk.Label(row, text=f"第 {idx+1} 段", width=8)
        lbl.pack(side="left")

        len_var = tk.StringVar(value=length)
        ttk.Entry(row, width=12, textvariable=len_var).pack(side="left", padx=(4, 12))

        rule_var = tk.StringVar(value=rule or self.rule_choices[0])
        cb = ttk.Combobox(
            row,
            values=self.rule_choices,
            textvariable=rule_var,
            width=22,
            state="readonly"
        )
        cb.pack(side="left")

        div_label = ttk.Label(row, text="除数：")
        div_var = tk.StringVar(value=divisor or str(DEFAULTS["default_divisor"]))
        div_entry = ttk.Entry(row, width=10, textvariable=div_var)

        # 规则变化时，动态显示/隐藏除数输入框
        cb.bind(
            "<<ComboboxSelected>>",
            lambda e, rv=rule_var, dl=div_label, de=div_entry: self._on_rule_change(rv, dl, de)
        )

        # 初始化时也执行一次逻辑
        self._on_rule_change(rule_var, div_label, div_entry)

        self.segments.append({
            "frame": row,
            "label": lbl,
            "len_var": len_var,
            "rule_var": rule_var,
            "div_var": div_var,
            "div_label": div_label,
            "div_entry": div_entry,
        })

    def add_segment(self):
        self._create_segment_row()

    def remove_last_segment(self):
        if not self.segments:
            return
        seg = self.segments.pop()
        seg["frame"].destroy()

    # ============ 小工具 ============

    def _select_all(self, event):
        self.txt.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _read_int(self, var, name):
        v = var.get().strip()
        if not v:
            raise ValueError(f"{name} 不能为空")
        try:
            iv = int(v)
        except Exception:
            raise ValueError(f"{name} 必须是整数")
        return iv

    def _read_number(self, var, name):
        v = var.get().strip()
        if not v:
            raise ValueError(f"{name} 不能为空")
        try:
            nv = float(v)
        except Exception:
            raise ValueError(f"{name} 必须是数字")
        return nv

    # ============ 核心生成逻辑 ============

    def generate(self):
        try:
            sheet = self.var_sheet.get().strip()
            col   = self.var_col.get().strip().upper()
            if not sheet:
                raise ValueError("工作表名不能为空")
            if not col or not col.isalpha():
                raise ValueError("列名必须是字母，如 G 或 AA")

            first = self._read_int(self.var_start_first, "首块区间起始行（first）")
            last  = self._read_int(self.var_start_last,  "首块区间结束行（last）")
            next_first = self._read_int(self.var_next_first, "下一个区块起始行（next_first）")
            end_last = self._read_int(self.var_end_last, "最后一块的结束行上限（end_last）")

            if first <= 0 or last <= 0 or next_first <= 0 or end_last <= 0:
                raise ValueError("first / last / next_first / end_last 必须为正整数")
            if last < first:
                raise ValueError("首块结束行（last）必须 ≥ 起始行（first）")
            if next_first <= first:
                raise ValueError("下一个区块起始行（next_first）必须大于首块起始行（first）")

            block_len = last - first + 1
            step = next_first - first  # ✅ 自动计算每块向下移动行数

            # 读取分段配置（长度 + 规则 + 每段除数）
            if not self.segments:
                raise ValueError("请至少添加一段分段规则。")

            seg_lengths = []
            seg_rules = []
            seg_divisors = []  # 每段的除数字符串（若不需要除则为 None）
            for i, seg in enumerate(self.segments, start=1):
                length = self._read_int(seg["len_var"], f"第{i}段长度")
                if length <= 0:
                    raise ValueError(f"第{i}段长度必须为正整数")
                rule = seg["rule_var"].get().strip()
                if rule not in self.rule_choices:
                    raise ValueError(f"第{i}段的规则不合法")

                seg_lengths.append(length)
                seg_rules.append(rule)

                if rule == "求和后除以除数":
                    div_value = self._read_number(seg["div_var"], f"第{i}段除数")
                    if div_value == 0:
                        raise ValueError(f"第{i}段的除数不能为 0")
                    if isinstance(div_value, float) and div_value.is_integer():
                        div_str = str(int(div_value))
                    else:
                        div_str = str(div_value)
                    seg_divisors.append(div_str)
                else:
                    seg_divisors.append(None)

            if sum(seg_lengths) != block_len:
                raise ValueError(
                    f"所有段长度之和必须等于区间总行数：\n"
                    f"当前区间总行数 = {block_len}（{first}~{last}），"
                    f"但所有段长度之和 = {sum(seg_lengths)}"
                )

            mode = self.var_mode.get()  # "single" / "multi"

            formulas = []

            if mode == "single":
                # ===== 模式1：每个区块一条公式 =====
                cur_first, cur_last = first, last
                while cur_last <= end_last:
                    block_formulas = self._build_block_formulas_single(
                        sheet=sheet,
                        col=col,
                        first=cur_first,
                        last=cur_last,
                        seg_lengths=seg_lengths,
                        seg_rules=seg_rules,
                        seg_divisors=seg_divisors,
                    )
                    formulas.extend(block_formulas)
                    cur_first += step
                    cur_last  += step

            else:
                # ===== 模式2：分段输出，按段分组，并在公式里加 N("第x段") 注释 =====
                # 先算出所有区块的 seg_infos
                all_blocks_seg_infos = []  # [ [seg_info1, seg_info2, ...], ... ]
                cur_first, cur_last = first, last
                while cur_last <= end_last:
                    seg_infos = self._calc_seg_infos(
                        sheet=sheet,
                        col=col,
                        first=cur_first,
                        last=cur_last,
                        seg_lengths=seg_lengths,
                        seg_rules=seg_rules,
                        seg_divisors=seg_divisors,
                    )
                    all_blocks_seg_infos.append(seg_infos)
                    cur_first += step
                    cur_last  += step

                num_blocks = len(all_blocks_seg_infos)
                num_segments = len(seg_lengths)

                # 按段分组输出：
                # 先输出：所有区块的第1段公式（都带 +N("第1段")）
                # 再输出：所有区块的第2段公式（都带 +N("第2段")）
                for seg_idx in range(num_segments):
                    for block_idx in range(num_blocks):
                        seg = all_blocks_seg_infos[block_idx][seg_idx]
                        rng = seg["range"]
                        expr = seg["expr"]
                        comment = f'N("第{seg_idx+1}段")'
                        fml = (
                            f'=IF(SUM({rng})=0,'
                            f'IF(COUNTIF({rng}, "<>")>0, "/", ""),'
                            f'{expr}+{comment})'
                        )
                        formulas.append(fml)

            # 输出：每行一条公式，无额外文本
            self.txt.delete("1.0", "end")
            self.txt.insert("1.0", "\n".join(formulas).rstrip("\n"))
            messagebox.showinfo("完成", f"已生成 {len(formulas)} 条公式。")

        except Exception as e:
            messagebox.showerror("错误", str(e))

    def _calc_seg_infos(
        self,
        sheet: str,
        col: str,
        first: int,
        last: int,
        seg_lengths,
        seg_rules,
        seg_divisors,
    ):
        """
        仅计算某一块下，每段的范围 + 表达式信息，不直接生成公式字符串。
        返回列表：[{first, last, range, expr}, ...]
        """
        seg_infos = []
        cur_row = first
        for seg_len, rule, div_str in zip(seg_lengths, seg_rules, seg_divisors):
            seg_first = cur_row
            seg_last  = cur_row + seg_len - 1
            rng = f"'{sheet}'!{col}{seg_first}:{col}{seg_last}"

            if rule == "直接求和":
                expr = f"SUM({rng})"
            elif rule == "求和后除以除数" and div_str is not None:
                expr = f"SUM({rng})/{div_str}"
            else:
                expr = f"SUM({rng})"  # 兜底

            seg_infos.append({
                "first": seg_first,
                "last": seg_last,
                "range": rng,
                "expr": expr,
            })

            cur_row = seg_last + 1

        return seg_infos

    def _build_block_formulas_single(
        self,
        sheet: str,
        col: str,
        first: int,
        last: int,
        seg_lengths,
        seg_rules,
        seg_divisors,
    ):
        """
        单块合并公式（single 模式）：返回 [公式字符串]
        """
        total_range = f"'{sheet}'!{col}{first}:{col}{last}"
        seg_infos = self._calc_seg_infos(
            sheet=sheet,
            col=col,
            first=first,
            last=last,
            seg_lengths=seg_lengths,
            seg_rules=seg_rules,
            seg_divisors=seg_divisors,
        )
        combined_expr = " + ".join(seg["expr"] for seg in seg_infos)
        formula = (
            f'=IF(SUM({total_range})=0,'
            f'IF(COUNTIF({total_range}, "<>")>0, "/", ""),'
            f'{combined_expr})'
        )
        return [formula]

    # ============ 复制 / 保存 / 重置 ============

    def copy_all(self):
        data = self.txt.get("1.0", "end-1c")
        if not data.strip():
            messagebox.showinfo("提示", "没有可复制的内容。请先生成。")
            return
        self.clipboard_clear()
        self.clipboard_append(data)
        messagebox.showinfo("已复制", "输出内容已复制到剪贴板。")

    def save_file(self):
        data = self.txt.get("1.0", "end-1c").strip()
        if not data:
            messagebox.showinfo("提示", "没有可保存的内容。请先生成。")
            return
        path = filedialog.asksaveasfilename(
            title="保存输出为文本文件",
            defaultextension=".txt",
            filetypes=[("Text files","*.txt"), ("All files","*.*")]
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(data + "\n")
            messagebox.showinfo("已保存", f"文件已保存：\n{path}")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败：{e}")

    def reset_defaults(self):
        self.var_sheet.set(DEFAULTS["sheet"])
        self.var_col.set(DEFAULTS["col"])
        self.var_start_first.set(str(DEFAULTS["start_first"]))
        self.var_start_last.set(str(DEFAULTS["start_last"]))
        self.var_next_first.set(str(DEFAULTS["next_first"]))
        self.var_end_last.set(str(DEFAULTS["end_last"]))
        self.var_mode.set("single")
        self._init_default_segments()
        self.txt.delete("1.0", "end")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
