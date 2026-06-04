from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from light_image_tool.models import LightImageToolConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]


DEFAULT_WHITE_BG_PROMPT = (
    "请基于产品图片生成一张干净专业的白底产品图。"
    "保留产品主体真实外观、材质、颜色和结构，去除杂乱背景，背景为纯白色，"
    "产品居中展示，边缘清晰，适合电商商品展示。PID：{pid}，图片：{image_name}"
)

DEFAULT_UI_LAYOUT = {
    "layout_version": "light_image_tool_ui_v1",
    "compact_breakpoint": 1500,
    "detail_panel_width": 360,
    "detail_panel_collapsed_in_compact": True,
    "log_panel_height": 150,
    "log_panel_collapsed_in_compact": True,
    "task_table_row_height": 36,
    "log_max_visible_lines": 100,
}

LIGHT_IMAGE_MAX_CONCURRENCY = 100

CONFIG_PATH = PROJECT_ROOT / "config" / "light_image_tool_config.json"


def _default_config() -> LightImageToolConfig:
    cfg = LightImageToolConfig()
    cfg.white_bg_prompt_template = DEFAULT_WHITE_BG_PROMPT
    cfg.ui_layout = dict(DEFAULT_UI_LAYOUT)
    return cfg


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "是", "启用"}
    return default


def _coerce_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _merge_ui_layout(value: Any) -> dict[str, Any]:
    merged = dict(DEFAULT_UI_LAYOUT)
    if isinstance(value, dict):
        merged.update(value)
    return merged


def _from_dict(data: dict[str, Any]) -> LightImageToolConfig:
    cfg = _default_config()
    field_names = {field.name for field in fields(LightImageToolConfig)}
    for key, value in data.items():
        if key not in field_names:
            continue
        default = getattr(cfg, key)
        if key in {
            "image_select_count",
            "generation_count",
            "concurrency",
            "max_concurrency",
            "manual_generation_count",
            "pid_generation_count",
            "custom_range_start",
            "custom_range_end",
            "api_task_failed_retry_count",
            "api_task_failed_retry_interval_seconds",
            "last_mode_index",
        }:
            minimum = 1
            maximum = None
            if key == "api_task_failed_retry_count":
                minimum = 0
                maximum = 10
            if key == "api_task_failed_retry_interval_seconds":
                maximum = 300
            if key in {"generation_count", "manual_generation_count", "pid_generation_count"}:
                maximum = 50
            if key == "last_mode_index":
                minimum = 0
            setattr(cfg, key, _coerce_int(value, int(default), minimum, maximum))
        elif key in {
            "auto_open_output_dir",
            "reuse_veo3_image_api_profile",
            "restore_last_session",
            "detail_panel_collapsed",
            "log_panel_collapsed",
        }:
            setattr(cfg, key, _coerce_bool(value, bool(default)))
        elif key in {"custom_image_names", "manual_image_paths"}:
            setattr(cfg, key, [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else [])
        elif key == "ui_layout":
            setattr(cfg, key, _merge_ui_layout(value))
        elif key in {"selected_image_provider", "selected_image_model"}:
            setattr(cfg, key, str(value).strip() if value not in {None, ""} else None)
        else:
            setattr(cfg, key, str(value) if isinstance(default, str) else value)
    if not str(cfg.white_bg_prompt_template or "").strip():
        cfg.white_bg_prompt_template = DEFAULT_WHITE_BG_PROMPT
    cfg.pid_match_mode = cfg.pid_match_mode if cfg.pid_match_mode in {"exact", "case_insensitive", "contains"} else "exact"
    cfg.image_select_rule = cfg.image_select_rule if cfg.image_select_rule in {"first_n", "last_n", "all", "custom_names", "custom_range"} else "first_n"
    cfg.max_concurrency = LIGHT_IMAGE_MAX_CONCURRENCY
    cfg.concurrency = min(max(1, cfg.concurrency), cfg.max_concurrency)
    cfg.ui_layout = _merge_ui_layout(cfg.ui_layout)
    return cfg


def load_light_image_tool_config(path: str | Path = CONFIG_PATH) -> LightImageToolConfig:
    cfg_path = Path(path)
    data: dict[str, Any] = {}
    if cfg_path.exists() and cfg_path.stat().st_size > 0:
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            data = {}
    cfg = _from_dict(data)
    save_light_image_tool_config(cfg, cfg_path)
    return cfg


def save_light_image_tool_config(config: LightImageToolConfig, path: str | Path = CONFIG_PATH) -> Path:
    cfg_path = Path(path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(config)
    data["ui_layout"] = _merge_ui_layout(data.get("ui_layout"))
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path
