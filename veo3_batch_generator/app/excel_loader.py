from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.models.task import TaskItem


# Header aliases — every logical field can be matched against any of these
# Chinese / English / variant column names. Order does not matter; matching is
# case-insensitive and whitespace-insensitive. To support a new column header,
# just add it to the appropriate list.
PID_HEADERS = ["PID", "pid", "产品ID", "商品PID", "商品ID"]
TASK_NAME_HEADERS = ["任务名称", "任务名", "任务标题", "商品名称", "产品名称", "task_name"]
OWNER_HEADERS = ["负责人", "负责人名称", "执行人", "分配人", "负责"]
NETDISK_PATH_HEADERS = [
    "网盘路径", "网盘目录", "网盘地址", "网盘文件夹",
    "产品网盘路径", "产品网盘目录", "素材路径", "素材目录",
]
# Legacy single-stage prompts (still supported in fallback mode).
IMAGE_PROMPT_HEADERS = ["图片提示词", "图生图提示词", "首帧提示词", "图片Prompt", "image_prompt"]
VIDEO_PROMPT_HEADERS = ["视频提示词", "图生视频提示词", "视频Prompt", "video_prompt"]
# New 4-stage prompts. If ANY of these are present we use the new workflow.
PROMPT_STAGE_1_HEADERS = [
    "提示词【阶段1】", "提示词[阶段1]", "提示词(阶段1)",
    "阶段1提示词", "阶段一提示词", "提示词阶段1", "prompt_stage_1",
]
PROMPT_STAGE_2_HEADERS = [
    "提示词【阶段2】", "提示词[阶段2]", "提示词(阶段2)",
    "阶段2提示词", "阶段二提示词", "提示词阶段2", "prompt_stage_2",
]
PROMPT_STAGE_3_HEADERS = [
    "提示词【阶段3】", "提示词[阶段3]", "提示词(阶段3)",
    "阶段3提示词", "阶段三提示词", "提示词阶段3", "prompt_stage_3",
]
PROMPT_STAGE_4_HEADERS = [
    "提示词【阶段4】", "提示词[阶段4]", "提示词(阶段4)",
    "阶段4提示词", "阶段四提示词", "提示词阶段4", "prompt_stage_4",
]
PRODUCT_IMAGE_URL_HEADERS = ["产品白底图URL", "白底图URL", "产品图URL", "图片URL", "产品白底图链接"]
PRODUCT_IMAGE_FILENAME_HEADERS = [
    "商品白底图文件名",
    "产品白底图文件名",
    "白底图文件名",
    "白底图名称",
    "产品图文件名",
    "商品图文件名",
    "图片文件名",
    "product_image_filename",
    "product_image_name",
]


def _clean_value(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _normalize(header: str) -> str:
    return str(header or "").strip().lower().replace(" ", "")


def _build_header_index(columns) -> dict[str, str]:
    """Map normalized header → original column name as it appears in the DataFrame."""
    index: dict[str, str] = {}
    for col in columns:
        key = _normalize(col)
        if key and key not in index:
            index[key] = col
    return index


def _resolve(index: dict[str, str], aliases: list[str]) -> str | None:
    """Return the actual DataFrame column name matching any of the given aliases."""
    for alias in aliases:
        col = index.get(_normalize(alias))
        if col is not None:
            return col
    return None


def _get_cell(row, column: str | None) -> str:
    if not column:
        return ""
    return _clean_value(row.get(column))


def load_tasks_from_excel(excel_path: str | Path) -> list[TaskItem]:
    path = Path(excel_path)
    if not path.exists():
        raise FileNotFoundError(f"Excel 文件不存在：{path}")

    try:
        df = pd.read_excel(path, dtype=str, engine="openpyxl")
    except PermissionError as exc:
        raise PermissionError(f"Excel 文件可能被占用：{path}") from exc

    if df.empty:
        return []

    header_index = _build_header_index(df.columns)

    pid_col = _resolve(header_index, PID_HEADERS)
    task_name_col = _resolve(header_index, TASK_NAME_HEADERS)
    netdisk_col = _resolve(header_index, NETDISK_PATH_HEADERS)
    owner_col = _resolve(header_index, OWNER_HEADERS)
    product_url_col = _resolve(header_index, PRODUCT_IMAGE_URL_HEADERS)
    product_filename_col = _resolve(header_index, PRODUCT_IMAGE_FILENAME_HEADERS)

    # Stage columns (new format) and legacy fallback columns.
    stage_1_col = _resolve(header_index, PROMPT_STAGE_1_HEADERS)
    stage_2_col = _resolve(header_index, PROMPT_STAGE_2_HEADERS)
    stage_3_col = _resolve(header_index, PROMPT_STAGE_3_HEADERS)
    stage_4_col = _resolve(header_index, PROMPT_STAGE_4_HEADERS)
    legacy_image_prompt_col = _resolve(header_index, IMAGE_PROMPT_HEADERS)
    legacy_video_prompt_col = _resolve(header_index, VIDEO_PROMPT_HEADERS)

    # New workflow mode is engaged if at least one new stage column is present.
    use_new_stage_format = any([stage_1_col, stage_2_col, stage_3_col, stage_4_col])
    # Legacy fallback: if no new stage columns at all, but old columns exist, we
    # map 图片提示词→阶段1 and 视频提示词→阶段2.
    legacy_fallback_mode = (
        not use_new_stage_format
        and (legacy_image_prompt_col or legacy_video_prompt_col)
    )

    missing: list[str] = []
    if not pid_col:
        missing.append("PID（任意别名：" + " / ".join(PID_HEADERS) + "）")
    if not netdisk_col:
        missing.append("网盘路径（任意别名：" + " / ".join(NETDISK_PATH_HEADERS) + "）")
    if not (use_new_stage_format or legacy_fallback_mode):
        missing.append(
            "提示词（任意别名：" + " / ".join(PROMPT_STAGE_1_HEADERS[:3])
            + " 或 旧字段 " + " / ".join(IMAGE_PROMPT_HEADERS[:2]) + "）"
        )
    if missing:
        raise ValueError(
            "Excel 表头缺失以下必需字段，请检查首行表头：\n  - " + "\n  - ".join(missing)
            + f"\n实际表头：{list(df.columns)}"
        )

    tasks: list[TaskItem] = []
    for idx, row in df.iterrows():
        netdisk_path = _get_cell(row, netdisk_col)
        product_image_url = _get_cell(row, product_url_col)
        product_image_filename = _get_cell(row, product_filename_col)

        if use_new_stage_format:
            prompt_stage_1 = _get_cell(row, stage_1_col)
            prompt_stage_2 = _get_cell(row, stage_2_col)
            prompt_stage_3 = _get_cell(row, stage_3_col)
            prompt_stage_4 = _get_cell(row, stage_4_col)
            # For backward compatibility (downstream code may still read these):
            # mirror stage1→image_prompt, stage2→video_prompt.
            image_prompt = prompt_stage_1 or _get_cell(row, legacy_image_prompt_col)
            video_prompt = prompt_stage_2 or _get_cell(row, legacy_video_prompt_col)
            legacy_compat = False
        else:
            # Legacy fallback: only stage1 + stage2 derived from old columns;
            # stage3 / stage4 are empty (the new image_stage_2 / video_stage_2
            # nodes will surface as WAITING_INPUT).
            image_prompt = _get_cell(row, legacy_image_prompt_col)
            video_prompt = _get_cell(row, legacy_video_prompt_col)
            prompt_stage_1 = image_prompt
            prompt_stage_2 = video_prompt
            prompt_stage_3 = ""
            prompt_stage_4 = ""
            legacy_compat = True

        tasks.append(
            TaskItem(
                row_index=int(idx) + 2,  # +1 for header row, +1 for 1-based Excel row numbering
                task_name=_get_cell(row, task_name_col),
                pid=_get_cell(row, pid_col),
                owner=_get_cell(row, owner_col),
                netdisk_path=netdisk_path,
                netdisk_original_path=netdisk_path,
                product_image_filename=product_image_filename,
                product_image_url=product_image_url or None,
                product_image_source_type="excel_url" if product_image_url else "",
                image_prompt=image_prompt,
                video_prompt=video_prompt,
                prompt_stage_1=prompt_stage_1 or None,
                prompt_stage_2=prompt_stage_2 or None,
                prompt_stage_3=prompt_stage_3 or None,
                prompt_stage_4=prompt_stage_4 or None,
                legacy_prompt_compat_mode=legacy_compat,
            )
        )
    return [
        task for task in tasks
        if task.pid
        or task.netdisk_path
        or task.image_prompt
        or task.video_prompt
        or task.prompt_stage_1
        or task.prompt_stage_2
        or task.prompt_stage_3
        or task.prompt_stage_4
    ]
