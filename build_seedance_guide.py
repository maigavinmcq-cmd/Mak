from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


OUT_PATH = "Seedance_2.0_影视场景提示词指南_整理版.docx"

LATIN_FONT = "Calibri"
CN_FONT = "Microsoft YaHei"
ACCENT = RGBColor(0x2E, 0x74, 0xB5)
DARK_ACCENT = RGBColor(0x1F, 0x4D, 0x78)
MUTED = RGBColor(0x55, 0x55, 0x55)
HEADER_FILL = "E8EEF5"
LIGHT_FILL = "F4F6F9"
NOTE_FILL = "FFF8E6"
BORDER = "B8C2CC"


def set_run_font(run, size=None, bold=None, color=None, italic=None):
    run.font.name = LATIN_FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), CN_FONT)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if color is not None:
        run.font.color.rgb = color


def set_style_font(style, size=None, bold=None, color=None):
    style.font.name = LATIN_FONT
    style._element.rPr.rFonts.set(qn("w:eastAsia"), CN_FONT)
    if size is not None:
        style.font.size = Pt(size)
    if bold is not None:
        style.font.bold = bold
    if color is not None:
        style.font.color.rgb = color


def set_p_spacing(p, before=0, after=6, line=1.25):
    pf = p.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing = line


def set_cell_shading(cell, fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_margins(cell, top=80, start=120, bottom=80, end=120):
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for m, v in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{m}"))
        if node is None:
            node = OxmlElement(f"w:{m}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(v))
        node.set(qn("w:type"), "dxa")


def set_cell_border(cell, color=BORDER, size="6"):
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = tc_pr.first_child_found_in("w:tcBorders")
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        tc_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        tag = f"w:{edge}"
        elem = borders.find(qn(tag))
        if elem is None:
            elem = OxmlElement(tag)
            borders.append(elem)
        elem.set(qn("w:val"), "single")
        elem.set(qn("w:sz"), size)
        elem.set(qn("w:space"), "0")
        elem.set(qn("w:color"), color)


def set_table_geometry(table, widths_in):
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    total_dxa = int(sum(widths_in) * 1440)
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(total_dxa))
    tbl_w.set(qn("w:type"), "dxa")
    tbl_ind = tbl_pr.find(qn("w:tblInd"))
    if tbl_ind is None:
        tbl_ind = OxmlElement("w:tblInd")
        tbl_pr.append(tbl_ind)
    tbl_ind.set(qn("w:w"), "120")
    tbl_ind.set(qn("w:type"), "dxa")

    grid = table._tbl.tblGrid
    if grid is None:
        grid = OxmlElement("w:tblGrid")
        table._tbl.insert(1, grid)
    for child in list(grid):
        grid.remove(child)
    widths_dxa = [int(w * 1440) for w in widths_in]
    for width in widths_dxa:
        col = OxmlElement("w:gridCol")
        col.set(qn("w:w"), str(width))
        grid.append(col)

    for row in table.rows:
        for idx, cell in enumerate(row.cells):
            cell.width = Inches(widths_in[idx])
            tc_pr = cell._tc.get_or_add_tcPr()
            tc_w = tc_pr.find(qn("w:tcW"))
            if tc_w is None:
                tc_w = OxmlElement("w:tcW")
                tc_pr.append(tc_w)
            tc_w.set(qn("w:w"), str(widths_dxa[idx]))
            tc_w.set(qn("w:type"), "dxa")
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            set_cell_margins(cell)
            set_cell_border(cell)


def repeat_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def add_table(doc, headers, rows, widths_in):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    hdr = table.rows[0]
    repeat_header(hdr)
    for i, text in enumerate(headers):
        cell = hdr.cells[i]
        set_cell_shading(cell, HEADER_FILL)
        p = cell.paragraphs[0]
        set_p_spacing(p, 0, 0, 1.15)
        r = p.add_run(text)
        set_run_font(r, 10, True, RGBColor(0x00, 0x00, 0x00))
    for row in rows:
        cells = table.add_row().cells
        for i, text in enumerate(row):
            cell = cells[i]
            p = cell.paragraphs[0]
            set_p_spacing(p, 0, 0, 1.15)
            for part_idx, part in enumerate(str(text).split("\n")):
                if part_idx:
                    p.add_run().add_break()
                r = p.add_run(part)
                set_run_font(r, 9.5, False, RGBColor(0x00, 0x00, 0x00))
    set_table_geometry(table, widths_in)
    after = doc.add_paragraph()
    set_p_spacing(after, 0, 6, 1.0)
    return table


def add_heading(doc, text, level=1):
    p = doc.add_paragraph(style=f"Heading {level}")
    r = p.add_run(text)
    set_run_font(r, {1: 16, 2: 13, 3: 12}[level], True, ACCENT if level < 3 else DARK_ACCENT)
    set_p_spacing(p, {1: 18, 2: 14, 3: 10}[level], {1: 10, 2: 7, 3: 5}[level], 1.15)
    return p


def add_para(doc, text="", bold_prefix=None, after=6):
    p = doc.add_paragraph()
    set_p_spacing(p, 0, after, 1.25)
    if bold_prefix and text.startswith(bold_prefix):
        r = p.add_run(bold_prefix)
        set_run_font(r, 11, True, None)
        rest = text[len(bold_prefix):]
        if rest:
            r = p.add_run(rest)
            set_run_font(r, 11)
    else:
        r = p.add_run(text)
        set_run_font(r, 11)
    return p


def add_bullets(doc, items):
    for item in items:
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.left_indent = Inches(0.375)
        p.paragraph_format.first_line_indent = Inches(-0.188)
        set_p_spacing(p, 0, 4, 1.25)
        r = p.add_run(item)
        set_run_font(r, 10.5)


def new_decimal_num_id(doc):
    numbering = doc.part.numbering_part.numbering_definitions._numbering
    num = numbering.add_num(7)  # built-in abstract numbering for List Number
    return int(num.numId)


def apply_numbering(paragraph, num_id):
    p_pr = paragraph._p.get_or_add_pPr()
    existing = p_pr.find(qn("w:numPr"))
    if existing is not None:
        p_pr.remove(existing)
    num_pr = OxmlElement("w:numPr")
    ilvl = OxmlElement("w:ilvl")
    ilvl.set(qn("w:val"), "0")
    num_id_el = OxmlElement("w:numId")
    num_id_el.set(qn("w:val"), str(num_id))
    num_pr.append(ilvl)
    num_pr.append(num_id_el)
    p_pr.append(num_pr)


def add_numbers(doc, items):
    for idx, item in enumerate(items, start=1):
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.32)
        p.paragraph_format.first_line_indent = Inches(-0.32)
        set_p_spacing(p, 0, 4, 1.25)
        r = p.add_run(f"{idx}. {item}")
        set_run_font(r, 10.5)


def add_callout(doc, title, body, fill=NOTE_FILL):
    table = doc.add_table(rows=1, cols=1)
    table.style = "Table Grid"
    cell = table.cell(0, 0)
    set_cell_shading(cell, fill)
    p = cell.paragraphs[0]
    set_p_spacing(p, 0, 2, 1.2)
    r = p.add_run(title)
    set_run_font(r, 10.5, True, DARK_ACCENT)
    for part in body.split("\n"):
        p = cell.add_paragraph()
        set_p_spacing(p, 0, 2, 1.2)
        r = p.add_run(part)
        set_run_font(r, 10.5)
    set_table_geometry(table, [6.5])
    spacer = doc.add_paragraph()
    set_p_spacing(spacer, 0, 6, 1.0)


def add_code_block(doc, lines):
    table = doc.add_table(rows=1, cols=1)
    table.style = "Table Grid"
    cell = table.cell(0, 0)
    set_cell_shading(cell, "F7F7F7")
    p = cell.paragraphs[0]
    set_p_spacing(p, 0, 0, 1.1)
    for idx, line in enumerate(lines):
        if idx:
            p.add_run().add_break()
        r = p.add_run(line)
        r.font.name = "Consolas"
        r._element.rPr.rFonts.set(qn("w:eastAsia"), "Consolas")
        r.font.size = Pt(9)
    set_table_geometry(table, [6.5])


def add_page_number(paragraph):
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    fld_sep = OxmlElement("w:fldChar")
    fld_sep.set(qn("w:fldCharType"), "separate")
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_sep)
    run._r.append(fld_end)


def build_doc():
    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin = Inches(1)
    section.right_margin = Inches(1)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)

    styles = doc.styles
    set_style_font(styles["Normal"], 11, False, RGBColor(0x00, 0x00, 0x00))
    styles["Normal"].paragraph_format.space_after = Pt(6)
    styles["Normal"].paragraph_format.line_spacing = 1.25
    for level, size, color in ((1, 16, ACCENT), (2, 13, ACCENT), (3, 12, DARK_ACCENT)):
        style = styles[f"Heading {level}"]
        set_style_font(style, size, True, color)
    set_style_font(styles["List Bullet"], 10.5, False, RGBColor(0x00, 0x00, 0x00))
    set_style_font(styles["List Number"], 10.5, False, RGBColor(0x00, 0x00, 0x00))

    header_p = section.header.paragraphs[0]
    header_p.text = ""
    r = header_p.add_run("Seedance 2.0 影视场景提示词指南（图片文字整理版）")
    set_run_font(r, 9, False, MUTED)
    footer_p = section.footer.paragraphs[0]
    add_page_number(footer_p)

    title = doc.add_paragraph()
    set_p_spacing(title, 0, 3, 1.15)
    r = title.add_run("Seedance 2.0 影视场景提示词指南")
    set_run_font(r, 24, True, ACCENT)
    sub = doc.add_paragraph()
    set_p_spacing(sub, 0, 12, 1.15)
    r = sub.add_run("根据 19 张截图文字整理，按原文上下文重排为完整参考文档")
    set_run_font(r, 11, False, MUTED)

    add_callout(
        doc,
        "整理说明",
        "本文将上传截图中的分散文字按主题串联，形成“方法论—多参考—视频编辑—实战示例—电影感词库—常见问题—VLM 反推”的完整结构。内容保留原始提示词写法与术语，并对明显散落的上下文做了章节化整理。",
        LIGHT_FILL,
    )

    add_heading(doc, "一、为什么 Seedance 2.0 的提示词要“工程化”", 1)
    add_para(
        doc,
        "Seedance 2.0 本质上是一个多模态 AI 导演：它同时读你的文字、图片、视频、音频，在内部拆成“空间层”（画面里有什么）和“时间层”（事情如何随时间变化）两个维度来理解和生成画面。",
    )
    add_para(
        doc,
        "因此，好的提示词不是单纯的“文案型形容”，而是“工程型指令”：谁、在什么场景、做什么动作、镜头怎么运动、按怎样的时间顺序发生，分别喂给空间层和时间层。",
    )
    add_para(
        doc,
        "配合多参考能力，可以用多张图片锁定角色和场景，用视频参考直接复刻运镜与动作节奏，用音频控制节奏和情绪。这对短剧这种高度依赖角色一致性和分镜节奏的内容形态尤其关键。",
    )

    add_heading(doc, "二、Seedance 2.0 提示词方法论", 1)
    add_heading(doc, "1. 万能结构与八个核心要素", 2)
    add_para(
        doc,
        "经过大量实测，收敛出一套在 Seedance 2.0 上效果较稳的“万能公式”，建议撰写时尽量保持这些元素：",
    )
    add_callout(
        doc,
        "万能公式",
        "精准主体 + 动作细节 + 场景环境 + 光影色调 + 镜头运镜 + 视觉风格 + 画质参数 + 约束条件",
        LIGHT_FILL,
    )
    add_para(
        doc,
        "按这些元素写，模型对提示词的解析率显著提升，画面稳定性和一次成功率都比堆砌形容词高得多。简单来说就是：先锁定“谁”和“在干什么”，再交代“在哪、什么氛围”，然后告诉模型“怎么拍”，最后用风格、画质和约束把结果收紧。",
    )

    add_heading(doc, "2. 按时间线组织成“分镜脚本”——分镜时序", 2)
    add_para(
        doc,
        "模型的内部建模是空间-时间解耦的。因此，一个电影感视频的提示词，最理想的形态是“时间轴化分镜”：把视频拆成几个时间段，每一段都写清楚“谁 + 在哪 + 做什么 + 镜头怎么动”。",
    )
    add_bullets(
        doc,
        [
            "反面案例：“男人在街头紧张地奔跑，画面很有电影感。”",
            "正面案例（15 秒追逐戏）：镜头 1 街巷侧拍，男人缓慢起跑，带有急促的呼吸感；镜头 2 男人撞翻水果摊，镜头快速摇动并给到惊恐特写；镜头 3 男人翻过矮墙消失，镜头缓慢拉远定格在空荡的街道。",
            "实操建议：对每一段视频都写一个简单的“镜头 1 / 镜头 2 / 镜头 3”分镜，再翻译成完整句子的提示词，而不是一口气写一大段无结构的剧情描述。",
        ],
    )

    add_heading(doc, "3. 动作与运镜的写法", 2)
    add_bullets(
        doc,
        [
            "优先写缓慢、连续的小动作，例如缓慢行走、轻轻抬手、微微低头、顺势坐下，尽量避免“狂奔、大跳、剧烈翻滚”等高强度动作。",
            "写出动作间的过渡，例如借着刚才转身的惯性、从停顿自然过渡到举手，这有助于模型判断连贯性。",
            "情绪外化：用“嘴角颤抖、眼眶渐红、肩膀微微抖动”这类身体信号，替代“很悲伤”“非常愤怒”这类抽象情绪词。",
        ],
    )
    add_table(
        doc,
        ["抽象情绪", "外化为动作与细节"],
        [
            ["悲伤", "低头、肩膀微微颤抖、眼眶泛红、手指无意识地攥紧衣角、泪水在眼眶里打转但没有落下"],
            ["喜悦", "嘴角抑制不住地上扬、眉眼舒展、脚步变得轻快、下意识地哼起小曲、忍不住原地转个圈"],
            ["紧张/焦虑", "频繁地看手表、手指不停敲击桌面、呼吸急促、眼神闪躲、无意识地啃咬指甲"],
            ["愤怒", "双拳紧握、下颌线紧绷、胸口剧烈起伏、眼神如刀般锐利、从牙缝里挤出话语"],
            ["释然", "长长地舒了一口气、紧绷的肩膀完全放松下来、脸上露出久违的淡淡微笑、抬头望向远方"],
        ],
        [1.2, 5.3],
    )
    add_para(
        doc,
        "运镜方面，Seedance 对中文运镜词理解力很强，直接写“中景、特写、全景、缓慢推镜、平稳横移、固定镜头”即可。注意：一个镜头里尽量只指定 1 种运镜方式，不要同时要求推拉摇移，否则会增加画面的不稳定性。",
    )

    add_heading(doc, "4. 构图", 2)
    add_para(doc, "构图是画面的骨架，引导观众的视线，传递潜在的秩序感或混乱感。")
    add_table(
        doc,
        ["术语", "中文解释", "典型用法", "示例提示片段"],
        [
            ["三分法构图", "将画面分割成九宫格，主体置于交叉点上。", "最常用、最稳妥的构图，自然和谐。", "采用三分法构图，人物位于画面右侧三分之一处。"],
            ["居中对称构图", "主体位于画面正中，左右对称。", "营造庄重、稳定、正式或强迫症般的秩序感。", "韦斯安德森风格，严格的居中对称构图。"],
            ["引导线构图", "利用画面中的线条（路、河、栏杆）引向主体。", "自然地将观众视线聚焦到重点。", "道路作为引导线，消失在远方的主体建筑。"],
            ["框架构图", "利用门、窗、树枝等作为前景，框住主体。", "增加画面层次感和窥视感。", "通过窗户形成的框架构图，拍摄屋内的女孩。"],
            ["荷兰角/倾斜构图", "画面刻意倾斜。", "表现人物内心的不安、紧张、疯狂或世界的失衡。", "使用荷兰角构图，画面倾斜，暗示主角内心的混乱。"],
        ],
        [1.1, 1.65, 1.7, 2.05],
    )

    add_heading(doc, "5. 光影与色调", 2)
    add_para(doc, "光影塑造氛围，色调定义情绪。")
    add_table(
        doc,
        ["维度", "分类", "常用词汇", "示例提示片段"],
        [
            ["光线质感", "硬光", "正午阳光、聚光灯、闪光灯", "硬光下，人物面部轮廓分明，营造戏剧冲突感。"],
            ["光线质感", "柔光", "阴天散射光、窗边自然光、柔光箱", "柔和的窗边光线，画面干净治愈。"],
            ["光线质感", "伦勃朗光", "伦勃朗光、三角光", "经典的伦勃朗光，人物脸颊一侧有三角形光斑。"],
            ["光线方向", "顺光", "正面光", "顺光拍摄，色彩鲜明，细节清晰。"],
            ["光线方向", "侧光", "45 度侧光、90 度侧光", "强烈的侧光，人物一半脸在明，一半脸在暗。"],
            ["光线方向", "逆光", "剪影、轮廓光、发丝光", "黄昏逆光，勾勒出人物金色的轮廓。"],
            ["光线方向", "顶光/底光", "顶光、底光、蝴蝶光", "从下往上打的底光，制造恐怖悬疑氛围。"],
            ["色调风格", "饱和度", "高饱和、低饱和、去饱和（黑白）", "低饱和度色调，画面情绪内敛，带有忧郁感。"],
            ["色调风格", "色温", "暖色调（黄/橙）、冷色调（蓝/青）", "整体为赛博朋克风格的冷蓝与品红色调。"],
            ["色调风格", "特殊风格", "复古胶片色、莫兰迪色系、阿宝色", "带有 80 年代港风电影的复古胶片色调。"],
        ],
        [0.9, 0.95, 2.0, 2.65],
    )

    add_heading(doc, "6. 画质、风格与约束词", 2)
    add_para(
        doc,
        "画质与风格建议放在句末收紧效果，例如“高清，细节丰富，电影质感，色彩自然，光影柔和”等。风格词（如赛博朋克冷蓝紫色调、复古胶片、日系清新）能让关键帧向特定美学靠拢。",
    )
    add_callout(
        doc,
        "极其重要的约束词",
        "尤其对人像和角色类短剧/影视场景，建议结尾固定加上：“面部稳定不变形、五官清晰、人体结构正常、动作自然流畅、不僵硬、画面无卡顿、无闪烁”。这能显著降低变脸、掉脸和抖动的概率。",
        NOTE_FILL,
    )

    add_heading(doc, "三、多参考能力：如何用好图片 / 视频 / 音频", 1)
    add_para(doc, "Seedance 2.0 的全能参考模式允许用户同时上传多种素材，并用 @ 语法给每个素材指定职责。")
    add_heading(doc, "1. 素材角色划分与文件数策略", 2)
    add_para(doc, "通常把素材分成四种“功能角色”：")
    add_numbers(
        doc,
        [
            "角色锚定：锁定角色外观。",
            "场景定调：锁定环境与风格。",
            "运镜参考：锁定镜头语言与动作节奏。",
            "节奏氛围：用音频控制情绪、音色。",
        ],
    )
    add_callout(
        doc,
        "素材数量建议",
        "注意不要用满上限。过多的素材会让模型难以判断优先级。\n稳妥的配置是：角色图 1-2 张（正脸/全身）+ 场景图 1 张 + 运镜视频 1 段 + 音频 1 段（总计 4-5 个素材）。",
        NOTE_FILL,
    )
    add_heading(doc, "2. @ 语法写法：基础与进阶", 2)
    add_para(doc, "多参考模式下，@ 语法是你和模型的“导演指令”。如果不写，模型只能靠猜，成功率极低。")
    add_bullets(
        doc,
        [
            "基础写法（声明用途）：“@图片1 作为角色参考，@图片2 作为场景参考，@视频1 参考运镜和动作节奏，@音频1 作为背景音乐。接下来……”",
            "进阶写法（绑定分镜时序）：“@图片1 为角色外观。镜头1：角色背对镜头站立，镜头缓慢环绕；镜头2：角色转身，参考 @视频1 的运镜方式；镜头3：镜头快速拉远展现全景，配合 @音频1 的鼓点。”",
            "避坑点：当有多个人物或视频时，必须明确对应关系。例如：“@图片1 的女生为女主，@图片2 的男生为男二，@视频1 仅用于学习镜头运动轨迹，不要复刻其中的人物和场景”。",
        ],
    )
    add_heading(doc, "3. 多镜头角色一致性与跨段衔接", 2)
    add_bullets(
        doc,
        [
            "单段内：只要每次生成都引用同一张角色参考图，并在文字里保持主体描述一致，角色一致性就能得到保证。建议加上“基于 @图片1 保持角色外观、服装、发型一致”。",
            "视频延长：把上一段生成的视频作为 @视频1 输入，并写“将 @视频1 向后延长 N 秒，保持角色外观、背景和光线与前段完全一致”。适合 30-45 秒内的连续场景。",
            "分段拼接：把整段剧情拆分成多个 15 秒内的小片段独立生成，但统一使用同一套角色图、场景图与提示词模板。最后在剪辑软件里拼接。",
        ],
    )

    add_heading(doc, "四、视频编辑能力的提示词方法", 1)
    add_heading(doc, "1. 视频延长与连续镜头衔接", 2)
    add_para(
        doc,
        "实施方法：将已经生成好的 15s 视频作为新的参考素材，例如“@视频1 输入”，然后在提示词中明确指示：“将 @视频1 的内容向后平滑延长 10 秒，保持角色、场景、光影风格与前段完全一致。”",
    )
    add_bullets(
        doc,
        [
            "推荐句式：延长<视频N>，生成……",
            "推荐句式：向前延长<视频N>，生成……",
            "连续长镜头：当一个场景内的核心动作或对话需要一镜到底时，优先使用“视频延长”。例如，角色从房间一头走到另一头的完整过程。",
            "场景切换/大跨度动作：当剧情发生转折，或需要表现快速、复杂的动作时，“分段拼接”依然是最佳选择。先独立生成各个小片段，再用剪辑软件组合。",
        ],
    )
    add_callout(
        doc,
        "用法策略：何时延长，何时拼接？",
        "延长：适用于“文戏”，如长对话、情绪的缓慢积累、单一路径的移动等，追求的是沉浸感和连贯性。\n拼接：适用于“武戏”，如追逐、打斗、快速蒙太奇等，追求的是节奏感和视觉冲击力。\n在实际制作中，通常是两种方法的结合。例如，先用“延长”做完一段 30 秒的室内对话，再“拼接”一个 5 秒的窗外风景空镜。\n注意：在编辑和延长任务中，提到视频时，不要使用“参考视频N”这样的字样，直接使用<视频N>，避免被认为是参考任务。",
        NOTE_FILL,
    )
    add_heading(doc, "2. 局部修改与内容重绘", 2)
    add_bullets(
        doc,
        [
            "角色更替：在保持场景和其他演员不变的情况下，将 A 角色替换为 B 角色。",
            "道具/背景微调：为角色手上的杯子换个颜色，或擦除背景中一个穿帮的路人。",
            "瑕疵修复：修正生成视频中偶尔出现的轻微面部抖动、肢体变形或光影跳跃。",
            "提示词写法：关键在于“绑定时间轴与空间层”。你需要精确告诉模型在哪个时间段、哪个区域进行修改。",
        ],
    )
    add_callout(
        doc,
        "提示词片段示例：局部替换",
        "场景：一段已生成的 10 秒视频（@视频1），其中女主穿着蓝色外套。现在需要将其换成红色。\n提示词：“基于 @视频1 进行编辑。在 0-10 秒的整个时间轴内，将画面中主角身上的蓝色外套替换为红色外套。保持人物的面部、发型、动作以及背景环境、光线完全不变。最终成片需确保色彩过渡自然，无闪烁或边缘破绽。”",
        NOTE_FILL,
    )
    add_heading(doc, "3. 首尾帧控制与多参考结合", 2)
    add_para(
        doc,
        "精准控制视频的开场和收尾，是提升短剧“电影感”的关键。通过“首帧/尾帧约束”，可以强制视频的开始或结束画面与指定的参考图保持高度一致，从而实现稳定的风格、统一的主体和紧凑的节奏。",
    )
    add_bullets(
        doc,
        [
            "首帧约束：使用 @图片1 作为首帧，确保视频的开场画面在构图、角色姿态和场景氛围上与参考图对齐。这对于系列短剧的风格统一至关重要。",
            "尾帧约束：使用 @图片2 作为尾帧，可以让镜头的落点精准收在一个设计好的画面上，常用于制造悬念或完成一个抒情段落，实现节奏收紧。",
            "@角色图 + @场景图 + @首帧图：保证开篇的角色、环境和构图稳定。",
            "@运镜视频 + @音频 + @尾帧图：控制过程的动态节奏和结尾的静态构图。",
        ],
    )

    add_heading(doc, "五、三段论写法实战示例", 1)
    add_para(doc, "你可以将以下模板抽象成“整体设定 + 分镜时序 + 风格画质约束”的结构。")
    add_heading(doc, "示例一：都市情感短剧 - 宿舍场景（偏文戏/对话）", 2)
    add_para(doc, "素材准备：")
    add_bullets(
        doc,
        [
            "@图片1：女主半身照",
            "@图片2：宿舍场景参考图",
            "@视频1：室内对话运镜参考（中景推拉或轻微摇移）",
            "@音频1：室内环境声或轻音乐",
        ],
    )
    add_callout(
        doc,
        "提示词",
        "@图片1 中的女孩作为主角，@图片2 作为宿舍场景风格参考，参考 @视频1 的运镜方式。\n镜头1：傍晚时分，女孩 @图片1 脚步轻快地走到宿舍门口 @图片2，镜头中景平稳跟拍，暖黄色日光从窗外洒进走廊，她在门口停顿一下，深呼吸，表情略带紧张。\n镜头2：她 @图片1 推开门走进宿舍，镜头切到室内中景，舍友们一边整理书本一边抬头看向她，其中一人笑着问“考得怎么样呀，过了吗？”，镜头在几人之间缓慢切换半身特写。\n镜头3：女孩 @图片1 先低头露出落寞表情，镜头给到她的近景，随后她抬头憋不住笑意，哈哈大笑说“骗你们的”，舍友们追着打闹起来，镜头缓慢拉远，定格在宿舍内一片欢声笑语的全景画面。\n全程画面高清电影纪实风，色调温暖，光影柔和；人物面部稳定不变形，动作自然流畅，无卡顿无闪烁；环境音效与 @音频1 自然融合。",
        LIGHT_FILL,
    )
    add_heading(doc, "示例二：古风短剧 - 悬崖对手戏（偏动作/氛围）", 2)
    add_para(doc, "素材准备：")
    add_bullets(
        doc,
        [
            "@图片1：红衣女主",
            "@图片2：黑衣刺客（对手）",
            "@图片3：悬崖竹林场景图",
            "@视频1：武打对决运镜参考",
            "@音频1：紧凑的鼓点或打斗音效",
        ],
    )
    add_callout(
        doc,
        "提示词",
        "@图片1 作为女主，@图片2 的黑衣女子作为对手，场景参考 @图片3 的悬崖竹林环境，整体运镜和动作节奏参考 @视频1，背景音效与 @音频1 同步。\n镜头1：傍晚，镜头从红衣女子 @图片1 侧面中景缓慢推进，她站在悬崖边拿起酒壶喝酒，衣袂在山风中轻轻摆动，镜头环绕她半圈，从正面推到背影，远处隐约可见竹林中的黑衣人影。\n镜头2：镜头变焦斜切到远景，无人机视角俯瞰整片悬崖和竹林，两人分立山崖两端，山风卷起衣摆和尘土，节奏随鼓点略微加快。\n镜头3：镜头切回地面近景，一人缓慢拔剑对峙，红衣女子神情从漫不经心转为冷冽，黑衣女子目光坚毅，剑尖微微颤动，镜头平稳跟随两人绕圈移动，最后定格在两剑相交前一瞬间的特写。\n整体画面烟雨江湖电影感，冷调低饱和，电影胶片质感，光影层次丰富；人物面部和身体比例稳定不变形，动作连贯自然，不僵硬，无穿模无卡顿。",
        LIGHT_FILL,
    )

    add_heading(doc, "六、分类词库：电影感弹药库", 1)
    add_heading(doc, "1. 镜头语言与景别", 2)
    add_para(doc, "景别决定了观众与主体的距离，直接影响情感代入的深度。")
    add_table(
        doc,
        ["术语", "定义", "核心功能与情绪指向", "示例提示片段"],
        [
            ["大特写 (ECU)", "只展现人物面部的局部，如眼睛、嘴唇。", "强调微表情，放大极致情绪（爱、恨、惊恐）。", "眼部大特写，瞳孔因惊恐而放大。"],
            ["特写 (CU)", "展现人物从头到肩的部分。", "聚焦面部表情，传递强烈情绪，拉近心理距离。", "给到主角一个悲伤的特写，眼泪正从脸颊滑落。"],
            ["近景 (MCU)", "展现人物胸部以上。", "既能看清表情，又能兼顾部分肢体语言。", "近景展现他无奈地摊开双手。"],
            ["中景 (MS)", "展现人物膝盖或腰部以上。", "常用于对话场景，展现人物间的互动关系。", "两人在吧台前的中景对话。"],
            ["全景 (FS)", "完整展现人物全身及其周围小范围环境。", "交代人物的完整动态和与环境的直接互动。", "全景镜头下，她独自站在空旷的站台上。"],
            ["远景 (LS)", "人物在画面中占比较小，环境成为主体。", "强调环境氛围，表现人物的渺小、孤独或宏大叙事。", "远景中，他的身影消失在连绵的雪山里。"],
            ["大远景 (ELS)", "人物在画面中几乎成为一个点。", "极致地渲染环境，常用于史诗感开场或结尾。", "大远景，一艘小船在暴风雨的大海中飘摇。"],
            ["过肩镜头 (OTS)", "从一个角色的肩膀后方拍摄另一个角色。", "增强对话的代入感，让观众感觉身临其境。", "从男主角的过肩镜头看去，女主角正在微笑。"],
            ["主观视角 (POV)", "模拟角色眼睛看到的画面。", "提供第一人称的沉浸式体验。", "第一人称主观视角，他颤抖地举起手中的信。"],
        ],
        [1.15, 1.55, 2.0, 1.8],
    )
    add_heading(doc, "2. 导演级运镜语言", 2)
    add_heading(doc, "第一类：基础运动运镜（控制叙事节奏与焦点）", 3)
    add_para(doc, "这些运镜是叙事的基础，用于控制观众看什么、怎么看。")
    add_table(
        doc,
        ["运镜术语", "导演意图与适用场景", "Seedance 提示词公式"],
        [
            ["推镜 (Push-in)", "意图：聚焦细节、强调重要性、拉近心理距离、制造紧张感。\n场景：角色做出关键决定前的面部表情；发现重要线索的物体。", "镜头从[起始景别]缓慢/快速地向[主体]推进，最终停在[结束景别]上。\n例：镜头从中景缓慢向主角的脸部推进，最终停在眼神的特写上。"],
            ["拉镜 (Pull-out)", "意图：揭示环境、展现人物与场景的关系，营造疏离感或史诗感。\n场景：从主角特写拉远，展现他身处战场的孤独；故事结尾，镜头拉远升空出主题。", "镜头从[主体]的[起始景别]平滑地向后拉远，逐渐展现出[所在的宏大环境]。\n例：镜头从主角的特写向后拉远，逐渐展现出他身后广阔无垠的沙漠。"],
            ["摇镜 (Pan/Tilt)", "意图：水平 (Pan) 或垂直 (Tilt) 巡视场景，跟随主体移动，或展示广阔景色。\n场景：从左到右扫视宴会上的宾客；镜头从高楼底部向上摇到楼顶。", "镜头从[起点]开始，平稳地向[左/右/上/下]摇摄，最终停在[终点]上。\n例：镜头从窗外开始，平稳地向右摇摄，展现房间内的全貌。"],
            ["移镜 (Dolly/Track)", "意图：摄影机本身在轨道上平移，创造出比摇镜更强烈的空间感和沉浸感。\n场景：侧向跟拍两个并排行走的角色；向前移动穿过人群。", "轨道镜头，[水平/向前/向后]平移，平稳地跟随[主体]移动。\n例：轨道镜头，水平向右平移，平稳地跟随两位主角在公园里散步。"],
            ["升降镜 (Crane/Boom)", "意图：镜头在垂直方向做大幅度升降，常用于改变视角、营造宏伟或压抑感。\n场景：从地面升至高空，展现城市全景；从高处降下，聚焦于人群中的主角。", "摇臂镜头，从[低角度/高角度]开始，平滑地[上升/下降]，展现[...]的视角变化。\n例：摇臂镜头，从地面平滑地上升至高空，展现整个庆典广场的鸟瞰视角。"],
        ],
        [1.25, 2.65, 2.6],
    )
    add_heading(doc, "第二类：情绪表达运镜（外化人物内心世界）", 3)
    add_para(doc, "这类运镜直接服务于情绪的表达，让观众“看见”角色的感受。")
    add_table(
        doc,
        ["运镜术语", "导演意图与适用场景", "Seedance 提示词公式"],
        [
            ["环绕运镜 (Arc Shot)", "意图：围绕主体做弧形运动，产生戏剧张力，或全方位展示主体。\n场景：两人对峙时，镜头环绕他们，暗示紧张关系；展示一件关键物品的 360 度细节。", "镜头围绕[主体]进行[缓慢/快速]的半圆形/360 度环绕拍摄。\n例：镜头围绕对峙的二人进行缓慢的 360 度环绕拍摄，加剧紧张氛围。"],
            ["手持感运镜 (Handheld)", "意图：模拟人手持摄影机的轻微、自然的晃动，增强纪实感、主观感和紧迫感。\n场景：纪录片风格的访谈；主角在混乱人群中寻找某人；激烈的追逐或打斗。", "手持镜头风格，带有轻微真实的呼吸感晃动，跟随[主体]。\n例：手持镜头风格，带有轻微晃动，紧跟主角在拥挤的市集中穿行。"],
            ["希区柯克变焦 (Dolly Zoom)", "意图：推拉镜头的同时进行反向变焦，主体大小不变而背景急剧变化，产生强烈的眩晕和心理压迫感。\n场景：主角得知惊天秘密时的震惊瞬间；角色面临巨大危险时的恐惧。", "希区柯克变焦效果，镜头向前推进的同时镜头向后变焦，背景产生扭曲感。\n例：对准主角的面部，施以希区柯克变焦，表现他内心的极度震惊。"],
            ["呼吸感镜头 (Lens Breathing)", "意图：模拟人眼在凝视时焦点的微弱变化，产生画面好像在“呼吸”的生命感。\n场景：长时间凝视某物的静态镜头；情绪的微妙积累。", "带有轻微的镜头呼吸效应，焦距发生微弱的周期性变化，画面仿佛在呼吸。\n例：固定机位特写，带有镜头呼吸效应，静静地注视着熟睡婴儿的脸庞。"],
        ],
        [1.35, 2.55, 2.6],
    )
    add_heading(doc, "第三类：动作与冲击力运镜（武戏场景）", 3)
    add_para(doc, "专为打斗、追逐等高能量场景设计，核心是速度、力量和节奏感。")
    add_table(
        doc,
        ["运镜术语", "导演意图与适用场景", "Seedance 提示词公式"],
        [
            ["甩镜转场 (Whip Pan)", "意图：极快速地摇动镜头，使画面模糊，用于创造快速、生硬的转场，连接两个高速场景。\n场景：从一个角色的视线快速甩到另一个角色；追逐戏中视角的快速切换。", "快速甩动镜头，画面产生强烈的动态模糊，无缝转场到下一个场景。"],
            ["格挡震动 (Impact Shake)", "意图：在撞击、爆炸或重物落地的瞬间，让画面产生一次剧烈、短暂的震动。\n场景：拳头击中面部；刀剑碰撞；爆炸瞬间。", "在[事件，如“两剑相交”]的瞬间，画面产生一次剧烈、猛烈的震动。"],
            ["子弹时间 (Bullet Time)", "意图：主体动作极度放慢甚至静止，而镜头围绕其高速运动，全方位展示关键瞬间。\n场景：主角躲避子弹的经典慢动作；展示高难度动作的决定性瞬间。", "子弹时间效果，主体在半空中保持静止，而镜头围绕他进行高速环绕拍摄。"],
            ["冲刺跟拍 (Rush Follow)", "意图：以极快的速度紧跟奔跑的主体，画面充满动态模糊和颠簸感，营造强烈的速度与激情。\n场景：巷战追逐；赛车比赛。", "侧面高速平移跟拍，紧随奔跑的角色，画面带有强烈的动态模糊和颠簸感。"],
        ],
        [1.35, 2.65, 2.5],
    )
    add_heading(doc, "3. 热门美学风格", 2)
    add_para(doc, "直接使用这些风格词，可以快速为你的视频定下整体基调。")
    add_table(
        doc,
        ["风格", "关键词"],
        [
            ["治愈清新", "自然光、柔和色调、干净治愈、日系空气感、温暖舒服"],
            ["复古胶片", "轻微颗粒感、复古色调、胶片质感、漏光效果、怀旧氛围"],
            ["赛博朋克", "霓虹灯光、蓝紫对比色、科技未来感、潮湿的街道、暗调高级"],
            ["国风意境", "水墨晕染、留白构图、古典雅致、汉服飘逸、烟雨朦胧"],
            ["高级暗调", "低亮度、高对比度、极简构图、光影层次丰富、情绪内敛"],
            ["纪实感", "原生自然光、手持镜头感、生活化场景、记录感、不完美的真实"],
        ],
        [1.3, 5.2],
    )

    add_heading(doc, "七、善用 VLM 模型反推提示词", 1)
    add_heading(doc, "1. 挑战与目标", 2)
    add_para(
        doc,
        "围绕正面多人物与镜头移动的人脸替换场景，视频中不同的人都有自己的神态、表情、动作，甚至台词，很多变化是瞬时的。如果让模型直接基于参考视频姿势 + 单一正面静帧 + 强编辑指令生成，视频容易出现某人物跳脸、人物细节变化、一致性保持不好等问题；同时再伴有音频参考时难度更高。",
    )
    add_heading(doc, "2. 方法", 2)
    add_bullets(
        doc,
        [
            "运用 VLM 视觉理解模型（建议 seed-2.0-pro 260215，思考深度：低）对视频/图片进行理解。若需反推视频提示词，在 prompt 中要求模型描述空间内人物的位置关系以及每个人的关键特征，并告知模型：“若要生成这样一个视频，请协助反推出提示词，需明确写出全局基础设定、分镜时序、约束条件”。",
            "静帧参考图准备：借助 seedream 5.0 Lite，将几张正面静帧替换为外国籍人士的面部以及物品细节、环境细节等内容。",
            "姿势动作参考视频准备。",
        ],
    )
    add_callout(
        doc,
        "体验中心使用补充",
        "体验中心上传图片的话，都会默认给它打上图一、图二、图三、图四并且插入到 prompt 里。这里用户看着没有写图1、图2、图3也可以使用；但实际上体验中心会帮他把 prompt 写成“[图1][图2][图3]一起跳舞”。",
        LIGHT_FILL,
    )
    add_heading(doc, "3. PE 示例", 2)
    add_para(
        doc,
        "输入视频给 seed-2.0-pro 260215，文字 prompt：如果我要生成这样一个视频，帮我反推出提示词，需明确写出全局基础设定、分镜时序、约束条件。",
    )
    add_heading(doc, "一、全局基础设定", 3)
    add_numbers(
        doc,
        [
            "场景属性：国际航天联合岗前培训场地，背景为教室背景，严格参考 @图片x，前方设投影幕布与教学黑板，受训人员统一坐在塑料折叠椅上，整体为冷调工业风。",
            "人物设定：讲师欧洲男性，穿灰蓝色作训服、蓝色贝雷帽，头戴通讯耳麦；学员为多国航天受训者，统一穿着浅白色工装，佩戴白色工作帽与通讯耳麦，部分学员手臂印有所属国国旗臂章，全员背对镜头朝向投影方向。",
            "视觉风格：硬核科幻现实主义，低饱和冷青灰调色，光线偏柔和偏暗，整体氛围严肃正式，符合职业培训的质感。",
            "核心主题：深空探索安全警示教育课堂上，学员举手示意后离场，讲师回应后继续讲解事故案例。",
        ],
    )
    add_heading(doc, "二、分镜时序", 3)
    add_numbers(
        doc,
        [
            "镜头1：镜头采用浅景深，焦点锁定前景学员举起的手臂，手臂外清晰显示巴西国旗臂章，背景的其他学员、讲师、投影幕均处于虚化状态；学员保持举手姿态数秒后放下手臂，起身离开座位。",
            "镜头2：镜头焦点顺滑后移，整个培训教室清晰呈现，举手学员已离开画面，此时投影幕为白屏状态，讲师坐在学员侧前方的折叠椅上，转头看向学员离场的方向。",
            "镜头3：投影切换为面部灼伤的遇难宇航员黑白遗像，讲师点头示意后转头看向投影，学员们保持注视投影的状态。",
            "镜头4：讲师从椅子上起身，走到投影幕旁站立，此时投影内容切换为宇航服内受伤宇航员的特写画面，讲师面向投影准备继续讲解。",
        ],
    )
    add_heading(doc, "三、约束条件", 3)
    add_numbers(
        doc,
        [
            "运镜要求：全程固定机位，仅做焦点前后切换，焦点过渡自然顺滑，符合真实相机浅景深对焦逻辑，不能拉摇移等额外运镜。",
            "风格统一：全程保持低饱和冷调，投影内容均为黑白纪实风格，无高饱和暖色元素，画面质感与同系列培训场景完全一致。",
            "动作逻辑：学员举手、放下、离场的动作符合课堂申请离场的正常流程，讲师的回应、起身、走到投影旁的动作自然流畅，无夸张表演痕迹。",
            "细节一致性：所有人物服装、配饰无穿帮，人物臂章清晰准确，投影内容符合深空事故伤情特征，背景绿幕保持纯色无杂色，光影统一来自投影光源，无额外奇怪光源。",
        ],
    )

    add_heading(doc, "注意：高频 Badcase 和优化", 1)
    add_heading(doc, "1. 未正确定义 Asset ID 和输入参考关系", 2)
    add_para(
        doc,
        "从模型视角看，Asset ID 是一个风控准入的身份 ID，不是一个从参考调用视角的映射关系。使用 Asset 时，模型不会把 Asset 和输入的参考内容直接对应上，需要用户自己从图1、视频1等写清楚两者对应关系。",
    )
    add_code_block(
        doc,
        [
            "错误示意：",
            '17    "role": "reference_image"',
            "18  },",
            "19  {",
            '20    "type": "image_url",',
            '21    "image_url": {',
            '22      "url": "asset-20260319080559-4wb6j"',
            "23    },",
            '24    "role": "reference_image"',
            "25  },",
        ],
    )
    add_para(
        doc,
        "上述内容中，模型其实不知道图 1 是 asset-20260324135118-mksq2、图 2 是 asset-20260319080559-4wb6j；用户也没写，模型就会自己去猜人名和图片 N 的对应关系，最终导致效果混乱。正确做法是在提示词里显式声明“图 N / 视频 N / Asset ID”分别对应什么角色、场景或动作参考。",
    )
    add_heading(doc, "2. 多人场景人物数量出错", 2)
    add_callout(
        doc,
        "复杂性提醒",
        "多人场景属于复杂生成任务，模型需要同时满足多项常识与结构条件，生成难度更高，可能需要多次生成才能得到符合要求的多人场景。下面的方法有助于降低抽卡次数。",
        NOTE_FILL,
    )
    add_bullets(
        doc,
        [
            "检查提示词是否出现歧义：若存在歧义，会导致视频生成效果不佳。例如“@图片3是美女3……美女3位于画面左下后景”容易被误解为编号或数量，应改成“@图片3是美女C……美女C位于画面左下后景”。",
            "清晰描述人物在不同图中的对应关系：尽可能在提示词中描述清楚人物的背景信息、多人场景参考图和单人参考图之间同一人物的对应关系。",
            "优化前示例：“@图片3场景里，@图片2生气地看向@图片3中的@图片1（疑惑）说：里墙，你这是干什么？”问题在于没有说明图片1的人物和图片3的对应关系。",
            "优化后示例：“在@图片3的多人场景中，@图片1站在最右边，穿着灰色的衣服。@图片2的女生生气地看向@图片3中的@图片1，并疑惑地问道：‘你这是干什么？’”",
        ],
    )
    add_heading(doc, "3. 九宫格作为参考图效果不佳", 2)
    add_bullets(
        doc,
        [
            "拆分参考图像：输入的九宫格图片融合了太多小图，会导致模型对某些小图参考不到位。尝试将九宫格拆分成若干参考图片再作引用。",
            "提示词明确关联对应分镜的参考图：不建议把所有分镜混成一大段提示词，而是在适当时候引用合适的参考图片。",
        ],
    )
    add_heading(doc, "优化前：九宫格混合引用", 3)
    add_para(
        doc,
        "将@图1按照从左到右、从上到下的顺序3X3，演绎3d国漫短片。一句话不要同景别停留太久，对话太长的话当给出反打镜头，只需要有对话和剧情演绎，不要有旁白。氛围光感按电影标准，不要出现字幕，音频只要环境音、特效音和人声，不要背景音乐。剧情是暴雨夜，乌云翻滚，雷电交织，一颗巨大燃烧火球从天空急速坠落，就在火球即将砸落的瞬间，一道身影突然悬停在半空中，黑色雨衣被狂风卷起，衣摆剧烈翻飞，人物神情冷峻、镇定，带有强烈东方道术师气质。镜头迅速推近，他猛然抬手掐诀，动作符合道家正常掐诀手法，手指变换极快、精准、利落、专业，诀印连续切换，带有明显法术节奏感，不是随意比划。掐诀瞬间，指尖迸发金色灵光，细小电弧与灵流光丝，双手周围浮现微型法阵纹理。雨滴被无形法力震开，形成旋转气场，仪式感强烈，神秘庄严。随后一张黄色符箓从指间飞出，在暴雨中高速前行并被金色道火点燃，朱砂符文逐渐发光，古老金色符文从符纸中浮现，像活物一样游动、缠绕、盘旋。符文不断扩散并组成八卦轮廓与道家阵纹，天空中的能量迅速凝聚。最终符箓在高空轰然炸开，爆出一圈金色冲击波，巨大的风水阵法瞬间铺满天空，层层圆环、八卦图、道家符号、古老咒文高速展开，形成恢弘的立体玄门法阵，锁定火球，暴雨、雷电、火焰与金色阵光激烈碰撞，史诗级东方玄学电影感，超写实，电影级光影，强烈仪式感，体积云，粒子特效，慢动作与高速镜头结合。",
    )
    add_heading(doc, "优化后：按分镜逐一引用参考图", 3)
    add_para(
        doc,
        "演绎3d国漫短片，一句话不要同景别停留太久，对话太长的话当给出反打镜头，只需要有对话和剧情演绎，不要有旁白，氛围光感按电影标准，不要出现字幕，音频只要环境音、特效音和人声，不要背景音乐。剧情是暴雨夜，乌云翻滚，雷电交织，@图1 一颗巨大燃烧火球从天空急速坠落，就在火球即将砸落的瞬间，@图2 的一道身影突然悬停在半空中，黑色雨衣被狂风卷起，衣摆剧烈翻飞，人物神情冷峻、镇定，带有强烈东方道术师气质。镜头迅速推近，@图3 的道术师猛然抬手掐诀，动作符合道家正常掐诀手法，手指变换极快、精准、利落、专业，诀印连续切换，带有明显法术节奏感，不是随意比划。掐诀瞬间，指尖迸发 @图4 的金色灵光，细小电弧与灵流光丝，双手周围浮现微型法阵纹理。雨滴被无形法力震开，形成旋转气场，仪式感强烈，神秘庄严。随后 @图5 的黄色符箓从指间飞出，在暴雨中高速前行并被金色道火点燃，朱砂符文逐渐发光，古老金色符文从符纸中浮现，像活物一样游动、缠绕、盘旋。符文如 @图6 不断扩散并组成八卦轮廓与道家阵纹，天空中的能量迅速凝聚。最终符箓在高空如 @图7 轰然炸开，爆出一圈金色冲击波，巨大的风水阵法瞬间铺满天空，@图8 的层层圆环、八卦图、道家符号、古老咒文高速展开，形成恢弘的立体玄门法阵，锁定火球，暴雨、雷电、火焰与金色阵光如 @图9 激烈碰撞，史诗级东方玄学电影感，超写实，电影级光影，强烈仪式感，体积云，粒子特效，慢动作与高速镜头结合。",
    )
    add_para(
        doc,
        "该提示词还可以按照本文介绍的“三段式”方法，在原有基础上进一步优化，例如定义分镜的时序等。",
    )

    # Keep tables and callouts from breaking awkwardly where Word allows it.
    for p in doc.paragraphs:
        for run in p.runs:
            if run.font.name is None:
                set_run_font(run)

    doc.save(OUT_PATH)


if __name__ == "__main__":
    build_doc()
