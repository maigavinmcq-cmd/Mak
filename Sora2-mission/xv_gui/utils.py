# -*- coding: utf-8 -*-
from __future__ import annotations
import base64, json, os, re, uuid, datetime, hashlib
from pathlib import Path
from urllib.parse import urlparse

SUPPORTED_MIMES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}
import os
import hashlib
import re
import re

_NON_TERMINAL_HINTS = [
    "heavy_load", "queue", "queued", "running", "processing",
    "排队", "处理中", "生成中", "等待",
    "503", "502", "429"
]

POLL_TERMINAL_PATTERNS = [
    # 内容/政策/合规（终态）
    r"内容政策", r"违反.*政策", r"违规", r"合规", r"审核不通过", r"被拒绝", r"敏感",
    r"官方负载", r"请求故障", r"任务失败",
    r"policy", r"violat", r"rejected", r"content",

    # prompt / 参数终态
    r"invalid prompt", r"prompt.*error", r"提示词.*错误", r"输入.*不合法",

    # 模型/能力终态
    r"model not found", r"模型不存在", r"unsupported model", r"not supported",

    # 鉴权/权限（除非换 key，否则继续轮询没意义）
    r"unauthorized", r"\b401\b", r"\b403\b", r"invalid api key", r"no permission",
]

def is_poll_terminal_failure(msg: str) -> bool:
    """remote_id 已有，但该错误属于终态：继续轮询无意义"""
    s = (msg or "").strip()
    if not s:
        return False
    low = s.lower()

    # ✅ 明确非终态：继续 poll / 或走 retry
    if any(k in low for k in _NON_TERMINAL_HINTS):
        return False

    for pat in POLL_TERMINAL_PATTERNS:
        try:
            if re.search(pat, s, flags=re.IGNORECASE):
                return True
        except re.error:
            if pat.lower() in low:
                return True
    return False


def norm_path(p: str) -> str:
    """统一路径：去空格、展开 ~、转绝对、Windows 下强制 \ 分隔"""
    if not isinstance(p, str):
        return ""
    p = p.strip().strip('"').strip("'")
    if not p:
        return ""
    try:
        p = os.path.expanduser(p)
        # 注意：不做 resolve()，避免网络盘/不存在路径导致慢或报错
        p = os.path.abspath(p)
        p = os.path.normpath(p)  # Windows 下会把 / 变成 \
    except Exception:
        pass
    return p

def _norm_maybe_url(p: str) -> str:
    """如果是 http/https 就不动；否则按路径规范化"""
    if not isinstance(p, str):
        return ""
    s = p.strip()
    if s.lower().startswith(("http://", "https://")):
        return s
    return _norm_path(s)

def guess_mime(path: str) -> str:
    suf = Path(path).suffix.lower().lstrip(".")
    return SUPPORTED_MIMES.get(suf, "application/octet-stream")

def image_to_data_url(image_path: str) -> str:
    mime = guess_mime(image_path)
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"

def normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")

def build_chat_completions_url(base_url: str) -> str:
    base = normalize_base_url(base_url)
    return f"{base}/chat/completions"

def build_xintian_videos_url(base_url: str) -> str:
    base = normalize_base_url(base_url)
    low = base.lower()
    if low.endswith("/v1/videos"):
        return base
    if low.endswith("/v1"):
        return f"{base}/videos"
    return f"{base}/v1/videos"

def build_xintian_video_status_url(base_url: str, video_id: str) -> str:
    base = build_xintian_videos_url(base_url)
    rid = (video_id or "").strip().strip("/")
    if "/v1/videos/" in rid:
        rid = rid.split("/v1/videos/", 1)[-1].strip("/")
    return f"{base}/{rid}"

def extract_video_url(text: str) -> str | None:
    m = re.search(r"\((https?://[^)]+)\)", text)
    if m:
        return m.group(1)
    m = re.search(r"(https?://\S+)", text)
    if m:
        return m.group(1)
    return None

def calc_sha256(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def safe_json_loads(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None

def find_first_key(obj, keys):
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k]:
                return obj[k]
        for v in obj.values():
            r = find_first_key(v, keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for it in obj:
            r = find_first_key(it, keys)
            if r is not None:
                return r
    return None

def parse_progress_percent(text: str) -> float | None:
    patterns = [
        r"进度[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*%",
        r"progress[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*%",
        r"percent[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*%",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1))
                if 0 <= v <= 100:
                    return v
            except Exception:
                pass
    return None

def safe_filename(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "Unnamed"
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:160] if len(s) > 160 else s

# utils.py
def read_text_safely(p: Path) -> str:
    """解决 Windows 环境下提示词文件编码不一的问题"""
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            return p.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return p.read_text(encoding="utf-8", errors="ignore")

def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def today_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")

def machine_fingerprint() -> str:
    raw = f"{uuid.getnode()}|{os.name}|{os.getenv('COMPUTERNAME','')}|{os.getenv('HOSTNAME','')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def allowlist_contains(fp: str, allowlist_file: str) -> bool:
    p = Path(allowlist_file)
    if not p.exists():
        return False
    allowed = {line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()}
    return fp in allowed

def is_timeout_error(err: str) -> bool:
    low = (err or "").lower()
    return ("timeout" in low) or ("read timed out" in low) or ("timed out" in low)

def guess_ext_from_url(url: str) -> str:
    try:
        path = urlparse(url).path
        name = Path(path).name
        if "." in name:
            ext = "." + name.split(".")[-1].lower()
            if ext in [".mp4", ".mov", ".m4v", ".webm"]:
                return ext
    except Exception:
        pass
    return ".mp4"
import os

def normalize_win_path_for_ui(p: str) -> str:
    """
    用于 UI 展示：强制 Windows 风格路径
    """
    try:
        return os.path.normpath(p)
    except Exception:
        return p
