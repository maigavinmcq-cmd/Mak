# -*- coding: utf-8 -*-
"""
Global settings & constants.
Keep this file small and stable so other modules don't become tightly coupled.
"""
import json
from pathlib import Path
from . import __version__ as APP_VERSION

APP_NAME = "Sora2-视频批量生成"
APP_TITLE = f"{APP_NAME} {APP_VERSION}"

# Files / folders
CONFIG_FILE = "config.enc"
SALT_FILE = "config.salt"
LOG_DIR = "logs"
TASKS_STORE_FILE = "tasks.json"
TASKS_STORE_MAX = 5000
TASKS_STORE_DIR = "tasks_store"
TASKS_STORE_SHARD_SIZE = 400
TASK_HISTORY_FILE = "task_history.json"
TASK_HISTORY_DIR = "task_history_store"
DOWNLOAD_INDEX_FILE = "download_index.json"  # stored under download root

# Gate (queue freeze)
QUEUE_GATE_ENABLED_DEFAULT = False
QUEUE_GATE_REMOTE_ID_LIMIT_DEFAULT = 0  # 0=disabled
QUEUE_GATE_LINK_LIMIT_DEFAULT = 0       # 0=disabled
QUEUE_GATE_POPUP_ONCE = True

# Auto Retry
AUTO_RETRY_ENABLED = True
AUTO_RETRY_TICK_SEC = 20
AUTO_RETRY_MAX_RETRIES = 50
AUTO_RETRY_BASE_DELAY_SEC = 30
AUTO_RETRY_MAX_DELAY_SEC = 300
AUTO_RETRY_INCLUDE_DONE_NO_URL = True

# Optional machine binding
ENABLE_MACHINE_BINDING = False
ALLOWLIST_FILE = "machine.allow"

# Billing
COST_PER_REQUEST = 0.4
EV_CHARGE_REMOTE_ID = "charge_remote_id"
EV_REFUND_FAILED = "refund_failed"
EV_REFUND_FAILED_BY_REFRESH = "refund_failed_by_refresh"
EV_FINAL_ACTUAL_COST = "final_actual_cost"
EV_LINK_FOUND = "link_found"
EV_PENDING_TIMEOUT = "pending_timeout"
EV_MANUAL_CANCEL = "manual_cancel"
EV_RETRY_STARTED = "retry_started"

BILLING_LEDGER_FILE = Path(LOG_DIR) / "billing_ledger.jsonl"  # JSONL ledger

# Providers
PROVIDER_MODELS_FILE = Path(__file__).with_name("provider_models.json")

_DEFAULT_PROVIDER_CATALOG = {
    "xintian": {
        "label": "XINTIAN(v1/videos 上传+轮询)",
        "default_base": "https://api.xintianwengai.com",
        "default_model": "sora-2-landscape-10s",
        "models": [
            "sora-2-landscape-10s",
            "sora-2-portrait-10s",
            "sora-2-landscape-15s",
            "sora-2-portrait-15s",
            "sora-2-pro-landscape-25s",
            "sora-2-pro-portrait-25s",
            "sora-2-pro-landscape-hd-10s",
            "sora-2-pro-portrait-hd-10s",
            "sora-2-pro-landscape-hd-15s",
            "sora-2-pro-portrait-hd-15s",
        ],
    },
    "lingke": {
        "label": "LINGKE(v1/video/create + /v1/video/query)",
        "default_base": "https://lingkeapi.com",
        "default_model": "sora-2-portrait-15s",
        "models": [
            "sora-2-portrait-15s",
        ],
    },
    "apiyi": {
        "label": "APIYI(chat/completions 流式)",
        "default_base": "https://api.apiyi.com/v1",
        "default_model": "sora_video2-15s",
        "models": [
            "sora_video2-15s",
            "sora-2-pro",
        ],
    },
    "toapis": {
        "label": "TOAPIS(upload+generations+poll)",
        "default_base": "https://toapis.com",
        "default_model": "sora-2-portrait-15s",
        "models": [
            "sora-2-portrait-15s",
            "sora-2-portrait-10s",
            "sora-2-landscape-15s",
            "sora-2-landscape-10s",
        ],
    },
    "baoyouhuyu": {
        "label": "BAOYOUHUYU(v1/videos create+poll)",
        "default_base": "https://api.baoyouhuyu.com",
        "default_model": "sora-2-vip",
        "models": [
            "sora-2-vip",
        ],
    },
    "jimmy": {
        "label": "JIMMY(open-api videos create+poll)",
        "default_base": "https://www.jimmyai.cn",
        "default_model": "sora2Stable",
        "models": [
            "sora2Stable",
        ],
    },
    "dyuapi": {
        "label": "HELLOBABYGO(v1/videos 图生视频 create+poll)",
        "default_base": "https://api.hellobabygo.com",
        "default_model": "sora-2-teshu",
        "models": [
            "sora-2-teshu",
        ],
    },
}


def _load_provider_catalog() -> dict[str, dict]:
    catalog = {k: dict(v) for k, v in _DEFAULT_PROVIDER_CATALOG.items()}
    try:
        if PROVIDER_MODELS_FILE.exists():
            raw = json.loads(PROVIDER_MODELS_FILE.read_text(encoding="utf-8"))
            providers = raw.get("providers", raw) if isinstance(raw, dict) else {}
            if isinstance(providers, dict):
                for key, default_cfg in _DEFAULT_PROVIDER_CATALOG.items():
                    candidate = providers.get(key, {})
                    if not isinstance(candidate, dict):
                        continue
                    label = str(candidate.get("label", default_cfg["label"]) or default_cfg["label"]).strip()
                    default_base = str(candidate.get("default_base", default_cfg["default_base"]) or default_cfg["default_base"]).strip()
                    models = candidate.get("models", default_cfg["models"])
                    if not isinstance(models, list):
                        models = default_cfg["models"]
                    cleaned_models = [str(m).strip() for m in models if str(m).strip()]
                    default_model = str(candidate.get("default_model", default_cfg.get("default_model", "")) or "").strip()
                    if default_model not in cleaned_models:
                        default_model = cleaned_models[0] if cleaned_models else str(default_cfg.get("default_model", "") or "").strip()
                    catalog[key] = {
                        "label": label,
                        "default_base": default_base or default_cfg["default_base"],
                        "default_model": default_model,
                        "models": cleaned_models or list(default_cfg["models"]),
                    }
    except Exception:
        pass
    for key, cfg in catalog.items():
        models = list(cfg.get("models", []) or [])
        default_model = str(cfg.get("default_model", "") or "").strip()
        if default_model not in models:
            cfg["default_model"] = models[0] if models else ""
    return catalog


_PROVIDER_CATALOG = _load_provider_catalog()

APIYI_DEFAULT_BASE = _PROVIDER_CATALOG["apiyi"]["default_base"]
APIYI_MODELS = list(_PROVIDER_CATALOG["apiyi"]["models"])

XINTIAN_DEFAULT_BASE = _PROVIDER_CATALOG["xintian"]["default_base"]
XINTIAN_MODELS = list(_PROVIDER_CATALOG["xintian"]["models"])

LINGKE_DEFAULT_BASE = _PROVIDER_CATALOG["lingke"]["default_base"]
LINGKE_MODELS = list(_PROVIDER_CATALOG["lingke"]["models"])

TOAPIS_DEFAULT_BASE = _PROVIDER_CATALOG["toapis"]["default_base"]
TOAPIS_MODELS = list(_PROVIDER_CATALOG["toapis"]["models"])

BAOYOUHUYU_DEFAULT_BASE = _PROVIDER_CATALOG["baoyouhuyu"]["default_base"]
BAOYOUHUYU_MODELS = list(_PROVIDER_CATALOG["baoyouhuyu"]["models"])

JIMMY_DEFAULT_BASE = _PROVIDER_CATALOG["jimmy"]["default_base"]
JIMMY_MODELS = list(_PROVIDER_CATALOG["jimmy"]["models"])

DYUAPI_DEFAULT_BASE = _PROVIDER_CATALOG["dyuapi"]["default_base"]
DYUAPI_MODELS = list(_PROVIDER_CATALOG["dyuapi"]["models"])

PROVIDER_DEFAULT_MODELS = {key: str(cfg.get("default_model", "") or "").strip() for key, cfg in _PROVIDER_CATALOG.items()}

PROVIDERS = [
    (_PROVIDER_CATALOG["xintian"]["label"], "xintian"),
    (_PROVIDER_CATALOG["lingke"]["label"], "lingke"),
    (_PROVIDER_CATALOG["apiyi"]["label"], "apiyi"),
    (_PROVIDER_CATALOG["toapis"]["label"], "toapis"),
    (_PROVIDER_CATALOG["baoyouhuyu"]["label"], "baoyouhuyu"),
    (_PROVIDER_CATALOG["jimmy"]["label"], "jimmy"),
    (_PROVIDER_CATALOG["dyuapi"]["label"], "dyuapi"),
]

# Mission defaults (can be changed in UI)
MISSION_ROOT_DEFAULT = Path(r"C:\Users\22892\Desktop\RPA-file\All_Mission")
DOWNLOAD_ROOT_DEFAULT = Path(r"C:\Users\22892\Desktop\RPA-file\Download")

SUPPORTED_IMAGE_EXTS = [".webp", ".png", ".jpg", ".jpeg"]
