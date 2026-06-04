import os
import re

# 中文字符范围
CHINESE_PATTERN = re.compile(r'[\u4e00-\u9fa5]')

# 提取前导数字
NUMBER_PATTERN = re.compile(r'^(\d+)')


def extract_number_before_chinese(filename: str):
    """
    在文件名中查找第一个中文字符，
    并提取其前面的连续数字字符串（前导数字）。
    例："00123小熊玩具.jpg" -> "00123"
    """
    match_ch = CHINESE_PATTERN.search(filename)
    if not match_ch:
        return None  # 没有中文字符，忽略

    idx = match_ch.start()
    prefix = filename[:idx]  # 中文前面的部分

    # 从 prefix 开头提取连续数字
    match_num = NUMBER_PATTERN.match(prefix.strip())
    if match_num:
        return match_num.group(1)
    return None


def process_directory(directory: str):
    results = []  # (文件名, 数字字符串)
    for fname in os.listdir(directory):
        full_path = os.path.join(directory, fname)
        if not os.path.isfile(full_path):
            continue  # 跳过子文件夹

        num = extract_number_before_chinese(fname)
        if num:
            results.append((fname, num))
    return results


if __name__ == "__main__":
    # TODO：改成你自己的目录路径
    folder = r"C:\Users\22892\Desktop\AI生成图素材\1"

    if not os.path.isdir(folder):
        print(f"目录不存在：{folder}")
        raise SystemExit

    records = process_directory(folder)

    # 生成 Excel 形式文本（制表符分隔：文件名\t数字）
    # 你可以直接复制 print 出来的内容，粘贴到 Excel
    lines = []
    header = "文件名\t数字"
    lines.append(header)
    for fname, num in records:
        lines.append(f"{fname}\t{num}")

    excel_like_text = "\n".join(lines)

    print("=========== 可直接粘贴到 Excel 的文本 ===========")
    print(excel_like_text)

    # 同时输出为一个文件（UTF-8 BOM，Excel 打开不会乱码）
    out_path = os.path.join(folder, "文件名_数字提取结果.tsv")
    with open(out_path, "w", encoding="utf-8-sig") as f:
        f.write(excel_like_text)

    print("\n已生成结果文件：", out_path)
    print("打开方式：在 Excel 中直接双击打开，或复制上面的文本粘贴到 Excel。")
