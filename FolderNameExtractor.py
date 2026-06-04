"""文件夹名称提取工具 - 提取目录下所有子文件夹名称"""
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


class FolderNameExtractor:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("文件夹名称提取工具")
        self.root.geometry("600x500")
        self.root.minsize(500, 400)
        self._setup_ui()

    def _setup_ui(self):
        # 顶部控制区
        top_frame = tk.Frame(self.root, padx=10, pady=10)
        top_frame.pack(fill=tk.X)

        tk.Button(top_frame, text="选择目录", command=self._select_folder, width=12).pack(side=tk.LEFT)

        self.path_var = tk.StringVar()
        path_entry = tk.Entry(top_frame, textvariable=self.path_var, state="readonly")
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0))

        # 选项区
        option_frame = tk.Frame(self.root, padx=10)
        option_frame.pack(fill=tk.X)

        self.recursive_var = tk.BooleanVar(value=False)
        tk.Checkbutton(option_frame, text="递归子目录", variable=self.recursive_var).pack(side=tk.LEFT)

        self.fullpath_var = tk.BooleanVar(value=False)
        tk.Checkbutton(option_frame, text="显示完整路径", variable=self.fullpath_var).pack(side=tk.LEFT, padx=(20, 0))

        tk.Button(option_frame, text="刷新", command=self._refresh, width=8).pack(side=tk.RIGHT)

        # 统计标签
        self.count_label = tk.Label(self.root, text="共 0 个文件夹", padx=10, anchor="w")
        self.count_label.pack(fill=tk.X)

        # 结果列表
        list_frame = tk.Frame(self.root, padx=10, pady=5)
        list_frame.pack(fill=tk.BOTH, expand=True)

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, font=("Consolas", 10))
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.listbox.yview)

        # 底部按钮区
        bottom_frame = tk.Frame(self.root, padx=10, pady=10)
        bottom_frame.pack(fill=tk.X)

        tk.Button(bottom_frame, text="复制全部", command=self._copy_all, width=12).pack(side=tk.LEFT)
        tk.Button(bottom_frame, text="导出到文件", command=self._export, width=12).pack(side=tk.LEFT, padx=(10, 0))
        tk.Button(bottom_frame, text="复制选中", command=self._copy_selected, width=12).pack(side=tk.RIGHT)

    def _select_folder(self):
        folder = filedialog.askdirectory(title="选择要扫描的目录")
        if folder:
            self.path_var.set(folder)
            self._scan_folders(folder)

    def _refresh(self):
        folder = self.path_var.get()
        if folder and os.path.isdir(folder):
            self._scan_folders(folder)

    def _scan_folders(self, root_path):
        self.listbox.delete(0, tk.END)
        folders = []

        if self.recursive_var.get():
            for dirpath, dirnames, _ in os.walk(root_path):
                for dirname in dirnames:
                    if self.fullpath_var.get():
                        folders.append(os.path.join(dirpath, dirname))
                    else:
                        rel_path = os.path.relpath(os.path.join(dirpath, dirname), root_path)
                        folders.append(rel_path)
        else:
            try:
                for item in os.listdir(root_path):
                    item_path = os.path.join(root_path, item)
                    if os.path.isdir(item_path):
                        if self.fullpath_var.get():
                            folders.append(item_path)
                        else:
                            folders.append(item)
            except PermissionError:
                messagebox.showerror("错误", "没有权限访问该目录")
                return

        folders.sort(key=str.lower)
        for folder in folders:
            self.listbox.insert(tk.END, folder)

        self.count_label.config(text=f"共 {len(folders)} 个文件夹")

    def _get_all_items(self):
        return [self.listbox.get(i) for i in range(self.listbox.size())]

    def _copy_all(self):
        items = self._get_all_items()
        if not items:
            messagebox.showinfo("提示", "没有数据可复制")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(items))
        messagebox.showinfo("成功", f"已复制 {len(items)} 个文件夹名称到剪贴板")

    def _copy_selected(self):
        selected = self.listbox.curselection()
        if not selected:
            messagebox.showinfo("提示", "请先选择要复制的项目")
            return
        items = [self.listbox.get(i) for i in selected]
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(items))
        messagebox.showinfo("成功", f"已复制 {len(items)} 个文件夹名称到剪贴板")

    def _export(self):
        items = self._get_all_items()
        if not items:
            messagebox.showinfo("提示", "没有数据可导出")
            return
        file_path = filedialog.asksaveasfilename(
            title="导出文件夹名称",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if file_path:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("\n".join(items))
            messagebox.showinfo("成功", f"已导出到 {file_path}")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = FolderNameExtractor()
    app.run()
