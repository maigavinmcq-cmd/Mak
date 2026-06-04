from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from app.ui_layout import ensure_ui_config


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _runtime_root()
CONFIG_PATH = PROJECT_ROOT / "config" / "app_config.json"

DEFAULT_EXCEL_PATH = Path(r"C:\Users\22892\Downloads\Veo3视频生成任务.xlsx")
DEFAULT_IMAGE_DOC_PATH = Path(r"C:\Users\22892\Downloads\xibapi_gpt-image2_sop.md")
DEFAULT_VIDEO_DOC_PATH = Path(r"C:\Users\22892\Downloads\jimmyai_veo_frames_api_config_sop.md")
DEFAULT_VIDEO_DOWNLOAD_ROOT = Path(r"\\192.168.1.6\004.短视频运营中心\麦超群\01.Veo3下载视频")
DEFAULT_IMAGE_ASSETS_ROOT = Path(r"\\192.168.1.6\004.短视频运营中心\麦超群\01.Veo3任务资料")
DEFAULT_SOFTWARE_LOG_ROOT = Path(r"\\192.168.1.6\004.短视频运营中心\麦超群\01.Veo3软件日志")
DEFAULT_NETDISK_LOCAL_PREFIX = r"\\192.168.1.6\004.短视频运营中心\01.产品信息\\"
OLD_NETDISK_HTTP_PREFIX = "http://media.pennitech.top:48080"
DEFAULT_NETDISK_HTTP_PREFIX = "https://media.pennitech.top:48443"
DEFAULT_VISIBLE_COLUMNS = [
    "任务名称",
    "PID",
    "负责人",
    "网盘路径",
    "产品白底图URL",
    "指定白底图文件名",
    "图片提示词",
    "视频提示词",
    "生成图片路径",
    "video_task_id",
    "视频轮询次数",
    "视频链接",
    "视频本地路径",
    "任务状态",
    "错误信息",
]


@dataclass
class AppConfig:
    excel_path: Path = DEFAULT_EXCEL_PATH
    image_doc_path: Path = DEFAULT_IMAGE_DOC_PATH
    video_doc_path: Path = DEFAULT_VIDEO_DOC_PATH
    output_dir: Path = DEFAULT_SOFTWARE_LOG_ROOT
    state_path: Path = DEFAULT_SOFTWARE_LOG_ROOT / "state" / "task_state.json"

    image_provider: str = "xibapi_gpt_image2"
    image_model_logical_key: str = "gpt_image_2"
    video_provider: str = "xibapi_veo"
    video_model_logical_key: str = "veo_3_1_fast"

    video_download_root: Path = DEFAULT_VIDEO_DOWNLOAD_ROOT
    image_assets_root: Path = DEFAULT_IMAGE_ASSETS_ROOT
    software_log_root: Path = DEFAULT_SOFTWARE_LOG_ROOT
    project_output_root: Path | str = ""

    concurrency: int = 5
    image_concurrency: int = 5
    video_submit_concurrency: int = 5
    poll_concurrency: int = 20
    download_concurrency: int = 20
    retry_count: int = 3
    retry_interval_seconds: int = 5
    poll_interval_seconds: int = 5
    max_poll_count: int = 120
    request_timeout_seconds: int = 120

    auto_download_video: bool = True
    auto_save_image_assets: bool = True
    group_by_owner: bool = True
    restore_last_tasks_on_startup: bool = True
    regenerate_existing_images: bool = False
    table_text_color: str = "#111111"

    image_api_key: str = ""
    video_api_key: str = ""
    image_api_base_url: str = "https://YOUR_API_HOST"
    video_api_base_url: str = "https://xibapi.com"
    image_model: str = "gpt-image-2"
    image_size: str = "1024x1024"
    image_upload_api_url: str = ""
    image_upload_api_key: str = ""
    image_upload_file_field: str = "file"
    video_model: str = "veo_3_1-fast-fl"
    video_orientation: str = "portrait"
    video_resolution: str = "1080x1920"

    enable_multi_filter: bool = True
    active_filters: dict[str, list[str]] | dict[str, Any] = None
    enable_group_view: bool = False
    group_by_fields: list[str] = None

    enable_netdisk_http_mapping: bool = True
    netdisk_local_prefix: str = DEFAULT_NETDISK_LOCAL_PREFIX
    netdisk_http_prefix: str = DEFAULT_NETDISK_HTTP_PREFIX

    # --- Workflow engine v2 (4-stage node-based DAG) -----------------------
    # When True, BatchWorker delegates execution to WorkflowExecutor and walks
    # the per-batch workflow_definition node graph. When False, fall back to
    # the legacy 2-stage image→video pipeline (kept for emergency rollback).
    enable_stage_based_workflow: bool = True
    workflow_engine_version: str = "v2_default_4_stage"
    # UI feedback toggles. The backend already honors these where applicable;
    # the GUI integration lands in the next upgrade round.
    show_stage_progress_in_task_table: bool = True
    show_task_detail_panel: bool = True
    ui_feedback_level: str = "balanced"  # minimal | balanced | verbose
    enable_toast_feedback: bool = True
    enable_statusbar_feedback: bool = True
    enable_empty_state_guidance: bool = True
    enable_node_level_manual_control: bool = True
    auto_retry_failed_workflow_enabled: bool = False
    auto_retry_image_nodes: bool = False
    auto_retry_video_nodes: bool = False
    auto_retry_video_download: bool = False
    auto_stop_on_consecutive_failures: bool = False
    max_consecutive_failures_before_pause: int = 20
    pause_on_auth_error: bool = True
    pause_on_rate_limit: bool = False
    retry_base_delay_seconds: int = 3
    retry_max_delay_seconds: int = 60
    rate_limit_backoff_seconds: int = 30
    file_io_concurrency: int = 5
    max_inflight_video_tasks: int = 100
    max_inflight_image_tasks: int = 20
    ui_refresh_interval_ms: int = 1000
    log_flush_interval_ms: int = 500
    batch_card_refresh_interval_ms: int = 2000

    visible_columns: list[str] = None
    export_only_visible_columns: bool = False
    show_api_doc_paths_in_main_settings: bool = False
    last_selected_batch_id: str = ""
    auto_load_last_batch_on_startup: bool = True
    batch_root_dir_name: str = "batches"
    table_horizontal_scrollbar_always_on: bool = True
    table_column_widths: dict[str, int] = None
    batch_list_view_enabled: bool = True
    last_view_batch_id: str = ""
    active_running_batch_id: str = ""
    show_batch_config_dialog_on_import: bool = True
    save_batch_dialog_settings_as_global_default: bool = False
    ui_button_feedback_enabled: bool = True
    ui_loading_indicator_enabled: bool = True
    batch_list_refresh_interval_seconds: int = 2
    enable_manual_poll_button: bool = True
    manual_poll_ignore_max_count: bool = True
    manual_poll_include_timeout_tasks: bool = True
    manual_poll_include_failed_tasks: bool = True
    api_profiles: dict[str, dict[str, Any]] = None
    remember_api_key_by_provider: bool = True
    mask_api_key_in_ui: bool = True
    enable_task_log_popup: bool = True
    enable_excel_drag_drop_import: bool = True
    show_import_loading_dialog: bool = True
    remember_batch_dialog_api_key: bool = True
    ui_layout: dict[str, Any] = None
    ui_theme: dict[str, Any] = None
    task_table_columns: dict[str, Any] = None
    log_panel: dict[str, Any] = None

    def __post_init__(self) -> None:
        if self.active_filters is None:
            self.active_filters = {}
        if self.group_by_fields is None:
            self.group_by_fields = []
        if self.visible_columns is None:
            self.visible_columns = list(DEFAULT_VISIBLE_COLUMNS)
        if self.table_column_widths is None:
            self.table_column_widths = {}
        ensure_api_profiles(self)
        ensure_ui_config(self)


PATH_FIELDS = {
    "excel_path",
    "image_doc_path",
    "video_doc_path",
    "output_dir",
    "state_path",
    "video_download_root",
    "image_assets_root",
    "software_log_root",
    "project_output_root",
}

INT_FIELDS = {
    "concurrency",
    "image_concurrency",
    "video_submit_concurrency",
    "poll_concurrency",
    "download_concurrency",
    "retry_count",
    "retry_interval_seconds",
    "poll_interval_seconds",
    "max_poll_count",
    "request_timeout_seconds",
    "batch_list_refresh_interval_seconds",
    "max_consecutive_failures_before_pause",
    "retry_base_delay_seconds",
    "retry_max_delay_seconds",
    "rate_limit_backoff_seconds",
    "file_io_concurrency",
    "max_inflight_video_tasks",
    "max_inflight_image_tasks",
    "ui_refresh_interval_ms",
    "log_flush_interval_ms",
    "batch_card_refresh_interval_ms",
}

BOOL_FIELDS = {
    "auto_download_video",
    "auto_save_image_assets",
    "group_by_owner",
    "restore_last_tasks_on_startup",
    "regenerate_existing_images",
    "enable_multi_filter",
    "enable_group_view",
    "enable_netdisk_http_mapping",
    "export_only_visible_columns",
    "show_api_doc_paths_in_main_settings",
    "auto_load_last_batch_on_startup",
    "table_horizontal_scrollbar_always_on",
    "batch_list_view_enabled",
    "show_batch_config_dialog_on_import",
    "save_batch_dialog_settings_as_global_default",
    "ui_button_feedback_enabled",
    "ui_loading_indicator_enabled",
    "enable_manual_poll_button",
    "manual_poll_ignore_max_count",
    "manual_poll_include_timeout_tasks",
    "enable_stage_based_workflow",
    "show_stage_progress_in_task_table",
    "show_task_detail_panel",
    "enable_toast_feedback",
    "enable_statusbar_feedback",
    "enable_empty_state_guidance",
    "enable_node_level_manual_control",
    "auto_retry_failed_workflow_enabled",
    "auto_retry_image_nodes",
    "auto_retry_video_nodes",
    "auto_retry_video_download",
    "auto_stop_on_consecutive_failures",
    "pause_on_auth_error",
    "pause_on_rate_limit",
    "manual_poll_include_failed_tasks",
    "remember_api_key_by_provider",
    "mask_api_key_in_ui",
    "enable_task_log_popup",
    "enable_excel_drag_drop_import",
    "show_import_loading_dialog",
    "remember_batch_dialog_api_key",
}

DICT_FIELDS = {"active_filters", "table_column_widths", "api_profiles", "ui_layout", "ui_theme", "task_table_columns", "log_panel"}
LIST_FIELDS = {"group_by_fields", "visible_columns"}

JSON_KEY_ALIASES = {
    "image_api_doc_path": "image_doc_path",
    "video_api_doc_path": "video_doc_path",
    "submit_concurrency": "concurrency",
}


def today_text() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def new_batch_id() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def software_day_dir(config: AppConfig, date_text: str | None = None) -> Path:
    return Path(config.software_log_root) / (date_text or today_text())


def logs_dir(config: AppConfig, date_text: str | None = None) -> Path:
    return software_day_dir(config, date_text) / "logs"


def states_dir(config: AppConfig, date_text: str | None = None) -> Path:
    return software_day_dir(config, date_text) / "states"


def exports_dir(config: AppConfig, date_text: str | None = None) -> Path:
    return software_day_dir(config, date_text) / "exports"


def configs_dir(config: AppConfig, date_text: str | None = None) -> Path:
    return software_day_dir(config, date_text) / "configs"


def project_outputs_dir(config: AppConfig, *parts: str) -> Path:
    root = Path(config.project_output_root) if config.project_output_root else Path(config.software_log_root) / "project_outputs"
    for part in parts:
        root = root / str(part)
    return root


def runtime_events_dir(config: AppConfig, *parts: str) -> Path:
    root_text = str(os.environ.get("VEO3_RUNTIME_EVENT_ROOT") or "").strip()
    root = Path(root_text) if root_text else Path(tempfile.gettempdir()) / "veo3_batch_generator" / "runtime_events"
    for part in parts:
        root = root / str(part)
    return root


def state_path_for_batch(config: AppConfig, batch_id: str | None = None) -> Path:
    suffix = batch_id or "latest"
    return states_dir(config) / f"task_state_{suffix}.json"


def refresh_runtime_paths(config: AppConfig, batch_id: str | None = None) -> None:
    if not config.project_output_root:
        config.project_output_root = Path(config.software_log_root) / "project_outputs"
    day_dir = software_day_dir(config)
    config.output_dir = day_dir
    config.state_path = state_path_for_batch(config, batch_id)


def ensure_runtime_dirs(config: AppConfig) -> None:
    for path in [
        Path(config.video_download_root),
        Path(config.image_assets_root),
        logs_dir(config),
        states_dir(config),
        exports_dir(config),
        configs_dir(config),
        project_outputs_dir(config),
        CONFIG_PATH.parent,
    ]:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Network roots may be unavailable at startup. Individual save/download
            # operations will surface the concrete path error when that feature is used.
            pass


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on", "是", "启用"}


def normalize_netdisk_http_prefix(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if text.lower() == OLD_NETDISK_HTTP_PREFIX.lower():
        return DEFAULT_NETDISK_HTTP_PREFIX
    return text


def _apply_value(config: AppConfig, key: str, value: Any) -> None:
    field = JSON_KEY_ALIASES.get(key, key)
    if not hasattr(config, field) or value is None:
        return
    if field in PATH_FIELDS:
        setattr(config, field, Path(str(value)))
    elif field in INT_FIELDS:
        try:
            setattr(config, field, int(value))
        except (TypeError, ValueError):
            return
    elif field in BOOL_FIELDS:
        setattr(config, field, _coerce_bool(value))
    elif field in DICT_FIELDS:
        setattr(config, field, value if isinstance(value, dict) else {})
    elif field in LIST_FIELDS:
        setattr(config, field, [str(item) for item in value] if isinstance(value, list) else [])
    elif field == "netdisk_http_prefix":
        setattr(config, field, normalize_netdisk_http_prefix(value))
    else:
        setattr(config, field, str(value).strip() if isinstance(value, str) else value)


def ensure_api_profiles(config: AppConfig) -> dict[str, dict[str, Any]]:
    profiles = getattr(config, "api_profiles", None)
    if not isinstance(profiles, dict):
        profiles = {}
    image = profiles.get("image")
    video = profiles.get("video")
    if not isinstance(image, dict):
        image = {}
    if not isinstance(video, dict):
        video = {}
    profiles["image"] = image
    profiles["video"] = video
    config.api_profiles = profiles
    return profiles


def get_api_profile(config: AppConfig, kind: str, provider: str) -> dict[str, Any]:
    profiles = ensure_api_profiles(config)
    group = profiles.setdefault(str(kind or "").lower(), {})
    provider_key = str(provider or "").strip()
    profile = group.get(provider_key)
    if not isinstance(profile, dict):
        profile = {
            "display_name": provider_key,
            "api_key": "",
            "last_model_logical_key": "",
            "base_url": "",
            "extra_params": {},
            "updated_at": "",
        }
        group[provider_key] = profile
    profile.setdefault("display_name", provider_key)
    profile.setdefault("api_key", "")
    profile.setdefault("last_model_logical_key", "")
    profile.setdefault("base_url", "")
    profile.setdefault("extra_params", {})
    profile.setdefault("updated_at", "")
    return profile


def update_api_profile(
    config: AppConfig,
    kind: str,
    provider: str,
    *,
    api_key: str | None = None,
    last_model_logical_key: str | None = None,
    base_url: str | None = None,
    display_name: str | None = None,
    extra_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = get_api_profile(config, kind, provider)
    if api_key is not None:
        profile["api_key"] = str(api_key).strip()
    if last_model_logical_key is not None:
        profile["last_model_logical_key"] = str(last_model_logical_key).strip()
    if base_url is not None:
        profile["base_url"] = str(base_url).strip().rstrip("/")
    if display_name:
        profile["display_name"] = str(display_name)
    if extra_params is not None:
        profile["extra_params"] = extra_params if isinstance(extra_params, dict) else {}
    profile["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return profile


def apply_api_profile_to_config(config: AppConfig, kind: str, provider: str) -> None:
    profile = get_api_profile(config, kind, provider)
    if kind == "image":
        config.image_provider = provider
        if profile.get("api_key"):
            config.image_api_key = str(profile.get("api_key") or "")
            if not config.image_upload_api_key:
                config.image_upload_api_key = config.image_api_key
        if profile.get("last_model_logical_key"):
            config.image_model_logical_key = str(profile.get("last_model_logical_key") or "")
        if profile.get("base_url"):
            config.image_api_base_url = str(profile.get("base_url") or "").rstrip("/")
    elif kind == "video":
        config.video_provider = provider
        if profile.get("api_key"):
            config.video_api_key = str(profile.get("api_key") or "")
        if profile.get("last_model_logical_key"):
            config.video_model_logical_key = str(profile.get("last_model_logical_key") or "")
        if profile.get("base_url"):
            config.video_api_base_url = str(profile.get("base_url") or "").rstrip("/")


def _api_key_matches_other_profile(config: AppConfig, kind: str, provider: str, api_key: str) -> bool:
    key_text = str(api_key or "").strip()
    if not key_text:
        return False
    profiles = ensure_api_profiles(config)
    group = profiles.get(str(kind or "").lower())
    if not isinstance(group, dict):
        return False
    provider_text = str(provider or "").strip()
    for profile_provider, profile in group.items():
        if str(profile_provider or "").strip() == provider_text:
            continue
        if not isinstance(profile, dict):
            continue
        if str(profile.get("api_key") or "").strip() == key_text:
            return True
    return False


def reconcile_api_key_with_provider_profile(config: AppConfig, kind: str, *, force_profile: bool = False) -> bool:
    kind_text = str(kind or "").lower().strip()
    if kind_text == "image":
        provider = str(config.image_provider or "").strip()
        current_key = str(config.image_api_key or "").strip()
    elif kind_text == "video":
        provider = str(config.video_provider or "").strip()
        current_key = str(config.video_api_key or "").strip()
    else:
        return False

    profile = get_api_profile(config, kind_text, provider)
    profile_key = str(profile.get("api_key") or "").strip()
    if not profile_key:
        return False

    should_apply = force_profile or not current_key or _api_key_matches_other_profile(config, kind_text, provider, current_key)
    if not should_apply:
        return False

    profile_base = str(profile.get("base_url") or "").strip().rstrip("/")
    profile_model = str(profile.get("last_model_logical_key") or "").strip()
    changed = False
    if kind_text == "image":
        old_key = config.image_api_key
        if config.image_api_key != profile_key:
            config.image_api_key = profile_key
            changed = True
        if force_profile and profile_model and config.image_model_logical_key != profile_model:
            config.image_model_logical_key = profile_model
            changed = True
        if profile_base and config.image_api_base_url != profile_base:
            config.image_api_base_url = profile_base
            changed = True
        if not config.image_upload_api_key or config.image_upload_api_key == old_key:
            config.image_upload_api_key = profile_key
            changed = True
    else:
        if config.video_api_key != profile_key:
            config.video_api_key = profile_key
            changed = True
        if force_profile and profile_model and config.video_model_logical_key != profile_model:
            config.video_model_logical_key = profile_model
            changed = True
        if profile_base and config.video_api_base_url != profile_base:
            config.video_api_base_url = profile_base
            changed = True
    return changed


def migrate_api_profiles(config: AppConfig) -> None:
    ensure_api_profiles(config)
    image_profile = get_api_profile(config, "image", config.image_provider)
    video_profile = get_api_profile(config, "video", config.video_provider)
    if config.image_api_key and not image_profile.get("api_key"):
        update_api_profile(
            config,
            "image",
            config.image_provider,
            api_key=config.image_api_key,
            last_model_logical_key=config.image_model_logical_key,
            base_url=config.image_api_base_url,
        )
    if config.video_api_key and not video_profile.get("api_key"):
        update_api_profile(
            config,
            "video",
            config.video_provider,
            api_key=config.video_api_key,
            last_model_logical_key=config.video_model_logical_key,
            base_url=config.video_api_base_url,
        )
    apply_api_profile_to_config(config, "image", config.image_provider)
    apply_api_profile_to_config(config, "video", config.video_provider)


def _read_json_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _env_override(config: AppConfig) -> None:
    env_map = {
        "IMAGE_API_KEY": "image_api_key",
        "VIDEO_API_KEY": "video_api_key",
        "IMAGE_API_BASE_URL": "image_api_base_url",
        "VIDEO_API_BASE_URL": "video_api_base_url",
        "IMAGE_MODEL": "image_model",
        "IMAGE_SIZE": "image_size",
        "IMAGE_UPLOAD_API_URL": "image_upload_api_url",
        "IMAGE_UPLOAD_API_KEY": "image_upload_api_key",
        "IMAGE_UPLOAD_FILE_FIELD": "image_upload_file_field",
        "VIDEO_MODEL": "video_model",
        "VIDEO_ORIENTATION": "video_orientation",
        "VIDEO_SIZE": "video_resolution",
        "VIDEO_RESOLUTION": "video_resolution",
        "BATCH_CONCURRENCY": "concurrency",
        "IMAGE_CONCURRENCY": "image_concurrency",
        "VIDEO_SUBMIT_CONCURRENCY": "video_submit_concurrency",
        "POLL_CONCURRENCY": "poll_concurrency",
        "DOWNLOAD_CONCURRENCY": "download_concurrency",
    }
    for env_key, field in env_map.items():
        value = os.getenv(env_key)
        if value is not None and str(value).strip():
            _apply_value(config, field, value)


def load_config() -> AppConfig:
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()

    config = AppConfig()
    for key, value in _read_json_config().items():
        _apply_value(config, key, value)
    _env_override(config)

    if not config.image_upload_api_key:
        config.image_upload_api_key = config.image_api_key
    migrate_api_profiles(config)
    ensure_ui_config(config)
    config.netdisk_http_prefix = normalize_netdisk_http_prefix(config.netdisk_http_prefix)
    refresh_runtime_paths(config)
    return config


def _jsonable_config(config: AppConfig) -> dict[str, Any]:
    data = asdict(config)
    for key, value in list(data.items()):
        if isinstance(value, Path):
            data[key] = str(value)

    data["image_api_doc_path"] = data.pop("image_doc_path")
    data["video_api_doc_path"] = data.pop("video_doc_path")
    data["submit_concurrency"] = data.get("concurrency", 1)
    data["image_model_logical_key"] = config.image_model_logical_key
    data["video_model_logical_key"] = config.video_model_logical_key
    return data


def save_config(config: AppConfig, path: Path = CONFIG_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable_config(config), ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        snapshot_dir = configs_dir(config)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / f"app_config_snapshot_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.json"
        snapshot_path.write_text(json.dumps(_jsonable_config(config), ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return path


def mask_api_key(api_key: str) -> str:
    text = (api_key or "").strip()
    if len(text) <= 8:
        return "****" if text else ""
    return f"{text[:6]}****{text[-4:]}"
