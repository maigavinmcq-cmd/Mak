import re
import tkinter as tk
from tkinter import messagebox, scrolledtext

def parse_text(text):
    # 固定输出顺序
    keys = ['SPU', 'US', 'UK', 'DE', 'MX', 'JP', 'FR']
    data = {k: '' for k in keys}

    # ✅ 改进后的正则表达式（支持各种写法：ID、PID、全角冒号、空格等）
    pattern = re.compile(
        r'\b(SPU|US|UK|DE|MX|JP|FR)\b(?:\s*(?:-|:|：)?\s*(?:ID|PID)?)?\s*(?:-|:|：)\s*([A-Za-z0-9]+)',
        re.IGNORECASE
    )

    for key, value in pattern.findall(text):
        k = key.upper()
        if k in data:
            data[k] = value.strip()

    # 按固定顺序输出
    return '\t'.join([data[k] for k in keys])

def process_input():
    input_text = text_box.get("1.0", tk.END).strip()
    if not input_text:
        messagebox.showwarning("提示", "请输入要解析的文本！")
        return

    result = parse_text(input_text)
    output_box.delete("1.0", tk.END)
    output_box.insert(tk.END, result)

def copy_to_clipboard():
    result = output_box.get("1.0", tk.END).strip()
    if not result:
        messagebox.showwarning("提示", "没有可复制的内容！")
        return
    root.clipboard_clear()
    root.clipboard_append(result)
    messagebox.showinfo("成功", "已复制到剪贴板！")

# 创建 UI
root = tk.Tk()
root.title("SPU & PID 自动提取工具 v2")
root.geometry("650x420")

tk.Label(root, text="输入原始文本：").pack(anchor="w", padx=10, pady=5)
text_box = scrolledtext.ScrolledText(root, height=10)
text_box.pack(fill="both", expand=True, padx=10)

frame = tk.Frame(root)
frame.pack(pady=5)
tk.Button(frame, text="提取并输出", command=process_input, bg="#4CAF50", fg="white").pack(side="left", padx=5)
tk.Button(frame, text="复制结果", command=copy_to_clipboard, bg="#FF9800", fg="white").pack(side="left", padx=5)

tk.Label(root, text="输出结果（Tab 分隔，可直接粘贴到 Excel）：").pack(anchor="w", padx=10, pady=5)
output_box = scrolledtext.ScrolledText(root, height=5)
output_box.pack(fill="both", expand=True, padx=10, pady=(0,10))

root.mainloop()
