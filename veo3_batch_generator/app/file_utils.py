from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote, unquote, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_DATA_URL_CACHE: dict[tuple[str, int, int], str] = {}
_DATA_URL_CACHE_LOCK = threading.Lock()
_DATA_URL_CACHE_MAX_ITEMS = 256
_HTTP_THREAD_LOCAL = threading.local()


def _http_session() -> requests.Session:
    session = getattr(_HTTP_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _HTTP_THREAD_LOCAL.session = session
    return session


def ensure_output_dirs(output_dir: str | Path) -> None:
    root = Path(output_dir)
    for relative in ["images", "videos", "logs", "result_excel"]:
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root.parent / "state").mkdir(parents=True, exist_ok=True)


def safe_pid(pid: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(pid).strip())
    return cleaned or "unknown_pid"


def safe_path_part(value: str, fallback: str = "未分配") -> str:
    text = str(value or "").strip()
    if not text:
        text = fallback
    cleaned = "".join(ch if ch not in '<>:"/\\|?*' else "_" for ch in text)
    return cleaned.strip(" .") or fallback


def task_date(task) -> str:
    return str(getattr(task, "batch_date", "") or getattr(task, "task_added_date", "") or datetime.now().strftime("%Y-%m-%d"))


def task_batch_part(task) -> str:
    return safe_path_part(str(getattr(task, "batch_id", "") or getattr(task, "batch_name", "") or "unbatched"), "unbatched")


def task_name_part(task) -> str:
    name = str(getattr(task, "task_name", "") or "").strip()
    if name:
        return safe_path_part(name, "task")
    return f"row_{int(getattr(task, 'row_index', 0) or 0)}"


def image_assets_dir(root: str | Path, task) -> Path:
    folder = Path(root) / task_date(task) / task_batch_part(task) / safe_pid(getattr(task, "pid", ""))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def stage_assets_dir(root: str | Path, task) -> Path:
    """Per-task per-row folder for stage-aware outputs.

    Layout: <root>/<date>/<batch_id>/<pid>/row_<row_index>/
    Used by the v2 workflow engine to store source_image_1, image_stage_X_output,
    prompt_stage_X.txt and per-node metadata side by side.
    """
    row_part = f"row_{int(getattr(task, 'row_index', 0) or 0)}"
    folder = image_assets_dir(root, task) / row_part
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def stage_source_image_path(root: str | Path, task, suffix: str = ".png") -> Path:
    suffix = (suffix or "").lower()
    if suffix not in IMAGE_EXTENSIONS:
        suffix = ".png"
    return stage_assets_dir(root, task) / f"source_image_1{suffix}"


def stage_image_output_path(root: str | Path, task, node_id: str) -> Path:
    safe_node = safe_path_part(str(node_id or "node"), "node")
    return stage_assets_dir(root, task) / f"{safe_node}_output.png"


def stage_video_metadata_path(root: str | Path, task, node_id: str) -> Path:
    safe_node = safe_path_part(str(node_id or "node"), "node")
    return stage_assets_dir(root, task) / f"{safe_node}_metadata.json"


def stage_prompt_txt_path(root: str | Path, task, prompt_field: str) -> Path:
    """Persist the prompt used by a node alongside its outputs for traceability."""
    label = str(prompt_field or "").strip()
    label = label.replace("【", "_").replace("】", "").replace("[", "_").replace("]", "")
    label = label.replace("(", "_").replace(")", "")
    label = safe_path_part(label or "prompt", "prompt")
    return stage_assets_dir(root, task) / f"{label}.txt"


def stage_video_dir(root: str | Path, task, group_by_owner: bool = True) -> Path:
    """Per-task folder for v2 video downloads.

    Layout: <root>[/<owner>]/<date>/<batch_id>/<pid>/<task_name>/
    """
    base = video_download_dir(root, task, group_by_owner)
    folder = base / task_name_part(task)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def stage_video_output_path(
    root: str | Path,
    task,
    node_id: str,
    group_by_owner: bool = True,
    video_url: str | None = None,
    task_id: str | None = None,
) -> Path:
    safe_node = safe_path_part(str(node_id or "node"), "node")
    ident = video_identifier(video_url, task_id)
    return stage_video_dir(root, task, group_by_owner) / f"{safe_node}_output_{ident}.mp4"


def video_download_dir_candidates(root: str | Path, task, group_by_owner: bool = True) -> list[Path]:
    """Return possible per-task video folders without creating directories.

    The project has used both owner-grouped and non-owner layouts across
    versions. Repair/resume code should be able to discover already archived
    videos in either layout, but it must not create thousands of empty folders
    while scanning a large batch.
    """

    root_path = Path(root)
    date_part = task_date(task)
    batch_part = task_batch_part(task)
    pid_part = safe_pid(getattr(task, "pid", ""))
    owner = str(getattr(task, "owner", "") or "").strip()
    candidates: list[Path] = []
    if group_by_owner and owner:
        candidates.append(root_path / safe_path_part(owner) / date_part / batch_part / pid_part)
    candidates.append(root_path / date_part / batch_part / pid_part)
    # Some old manual-download paths were owner grouped even when the current
    # config no longer is; keep this as a read-only fallback.
    if owner:
        owner_path = root_path / safe_path_part(owner) / date_part / batch_part / pid_part
        if owner_path not in candidates:
            candidates.append(owner_path)
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _valid_video_candidate(path: Path) -> Optional[Path]:
    try:
        if path.is_file() and path.stat().st_size > 0:
            ok, _message = validate_downloaded_file(path, expected_kind="video")
            if ok:
                return path
    except OSError:
        return None
    return None


def video_archive_lookup_keys(
    task,
    node_id: str,
    video_url: str | None = None,
    task_id: str | None = None,
) -> list[str]:
    safe_node = safe_path_part(str(node_id or "node"), "node")
    ident = video_identifier(video_url, task_id)
    row = int(getattr(task, "row_index", 0) or 0)
    pid = safe_pid(getattr(task, "pid", ""))
    task_folder = task_name_part(task)
    return [
        f"pid-task-node-ident::{pid}::{task_folder}::{safe_node}::{ident}",
        f"pid-row-ident::{pid}::{row}::{ident}",
        f"ident::{ident}",
        f"pid-row::{pid}::{row}",
    ]


def _extract_video_ident_from_name(name: str) -> str:
    stem = Path(name).stem
    parts = stem.split("_")
    return parts[-1] if parts else ""


def build_existing_video_archive_index(root: str | Path, tasks, group_by_owner: bool = True) -> dict[str, str]:
    """Build a batch-scoped index of archived mp4 files.

    Scans each unique <root>[/owner]/date/batch directory once instead of
    probing the network share per task. Values are paths as strings so the index
    remains JSON-serializable and cheap to pass around.
    """

    root_path = Path(root)
    batch_dirs: set[Path] = set()
    for task in list(tasks or []):
        date_part = task_date(task)
        batch_part = task_batch_part(task)
        owner = str(getattr(task, "owner", "") or "").strip()
        if group_by_owner and owner:
            batch_dirs.add(root_path / safe_path_part(owner) / date_part / batch_part)
        batch_dirs.add(root_path / date_part / batch_part)
        if owner:
            batch_dirs.add(root_path / safe_path_part(owner) / date_part / batch_part)

    index: dict[str, str] = {}
    for batch_dir in sorted(batch_dirs, key=lambda p: str(p).lower()):
        try:
            if not batch_dir.exists():
                continue
            files = sorted(batch_dir.rglob("*.mp4"))
        except OSError:
            continue
        for path in files:
            try:
                if not path.is_file() or path.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            rel_parts = path.relative_to(batch_dir).parts
            if not rel_parts:
                continue
            pid = safe_pid(rel_parts[0])
            task_folder = rel_parts[1] if len(rel_parts) >= 3 else ""
            name = path.name
            stem = path.stem
            ident = _extract_video_ident_from_name(name)
            if ident:
                index.setdefault(f"ident::{ident}", str(path))
            stage_match = re.match(r"^(video_stage_\d+)_output_([A-Za-z0-9]+)$", stem)
            if stage_match and task_folder:
                node_id, stage_ident = stage_match.groups()
                index.setdefault(
                    f"pid-task-node-ident::{pid}::{safe_path_part(task_folder, 'task')}::{safe_path_part(node_id, 'node')}::{stage_ident}",
                    str(path),
                )
            row_match = re.search(r"_row(\d+)_([A-Za-z0-9]+)$", stem)
            if row_match:
                row, row_ident = row_match.groups()
                index.setdefault(f"pid-row-ident::{pid}::{int(row)}::{row_ident}", str(path))
                index.setdefault(f"pid-row::{pid}::{int(row)}", str(path))
    return index


def find_existing_stage_video_path(
    root: str | Path,
    task,
    node_id: str,
    group_by_owner: bool = True,
    video_url: str | None = None,
    task_id: str | None = None,
    archive_index: dict[str, str] | None = None,
    allow_glob_fallback: bool = False,
) -> Optional[Path]:
    """Find an already archived v2 video for a task/node.

    This is used when a state file preserved the video URL but missed the local
    path after a crash/restart or a failed network-share write. It recognizes
    both the current stage-aware layout and the older PID_row layout.
    """

    safe_node = safe_path_part(str(node_id or "node"), "node")
    ident = video_identifier(video_url, task_id)
    row = int(getattr(task, "row_index", 0) or 0)
    pid = safe_pid(getattr(task, "pid", ""))
    task_folder = task_name_part(task)
    if archive_index:
        for key in video_archive_lookup_keys(task, node_id, video_url, task_id):
            value = archive_index.get(key)
            if not value:
                continue
            found = _valid_video_candidate(Path(value))
            if found:
                return found
    for base in video_download_dir_candidates(root, task, group_by_owner):
        if not base.exists():
            continue
        exact_candidates = [
            base / task_folder / f"{safe_node}_output_{ident}.mp4",
            base / f"{pid}_row{row}_{ident}.mp4",
        ]
        for candidate in exact_candidates:
            found = _valid_video_candidate(candidate)
            if found:
                return found

        if not allow_glob_fallback:
            continue
        # Optional bounded fallback: scan only this task's PID folder, never the
        # whole download root. Disabled by default because UNC rglob can be very
        # slow on large batches.
        patterns = [
            f"**/*{ident}*.mp4",
            f"**/{safe_node}_output_*.mp4",
            f"**/*_row{row}_*.mp4",
        ]
        checked: set[str] = set()
        for pattern in patterns:
            try:
                matches = sorted(base.glob(pattern))
            except OSError:
                continue
            for match in matches:
                key = str(match).lower()
                if key in checked:
                    continue
                checked.add(key)
                found = _valid_video_candidate(match)
                if found:
                    return found
    return None


def generated_image_asset_path(root: str | Path, task) -> Path:
    return image_assets_dir(root, task) / f"{safe_pid(getattr(task, 'pid', ''))}_row{getattr(task, 'row_index', 0)}_generated_image.png"


def product_image_asset_path(root: str | Path, task, source: str = "") -> Path:
    parsed = urlparse(source or "")
    suffix = Path(unquote(parsed.path)).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        suffix = ".png"
    return image_assets_dir(root, task) / f"{safe_pid(getattr(task, 'pid', ''))}_row{getattr(task, 'row_index', 0)}_product_image{suffix}"


def video_download_dir(root: str | Path, task, group_by_owner: bool = True) -> Path:
    owner = str(getattr(task, "owner", "") or "").strip()
    pid = safe_pid(getattr(task, "pid", ""))
    if group_by_owner and owner:
        folder = Path(root) / safe_path_part(owner) / task_date(task) / task_batch_part(task) / pid
    else:
        folder = Path(root) / task_date(task) / task_batch_part(task) / pid
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def video_download_output_path(root: str | Path, task, group_by_owner: bool = True) -> Path:
    return video_download_dir(root, task, group_by_owner) / video_filename(
        getattr(task, "pid", ""),
        int(getattr(task, "row_index", 0) or 0),
        getattr(task, "video_url", None),
        getattr(task, "video_task_id", None),
    )


def selected_task_dir(root: str | Path, task, group_by_owner: bool = False) -> Path:
    owner = str(getattr(task, "owner", "") or "").strip()
    pid = safe_pid(getattr(task, "pid", ""))
    folder = Path(root) / safe_path_part(owner) / pid if group_by_owner and owner else Path(root) / pid
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def selected_video_output_path(root: str | Path, task, group_by_owner: bool = False) -> Path:
    return selected_task_dir(root, task, group_by_owner) / video_filename(
        getattr(task, "pid", ""),
        int(getattr(task, "row_index", 0) or 0),
        getattr(task, "video_url", None),
        getattr(task, "video_task_id", None),
    )


def selected_image_assets_dir(root: str | Path, task, group_by_owner: bool = False) -> Path:
    return selected_task_dir(root, task, group_by_owner)


def task_metadata(task) -> dict:
    return {
        "pid": getattr(task, "pid", ""),
        "row_index": getattr(task, "row_index", ""),
        "task_uid": getattr(task, "task_uid", ""),
        "batch_id": getattr(task, "batch_id", ""),
        "batch_name": getattr(task, "batch_name", ""),
        "owner": getattr(task, "owner", ""),
        "netdisk_path": getattr(task, "netdisk_path", ""),
        "netdisk_original_path": getattr(task, "netdisk_original_path", ""),
        "netdisk_http_path": getattr(task, "netdisk_http_path", ""),
        "product_image_filename": getattr(task, "product_image_filename", ""),
        "product_image_path": getattr(task, "product_image_path", ""),
        "product_image_url": getattr(task, "product_image_url", ""),
        "product_image_source_type": getattr(task, "product_image_source_type", ""),
        "generated_image_path": getattr(task, "generated_image_path", ""),
        "image_task_id": getattr(task, "image_task_id", ""),
        "image_prompt": getattr(task, "image_prompt", ""),
        "video_prompt": getattr(task, "video_prompt", ""),
        "image_provider": getattr(task, "image_provider", ""),
        "image_model": getattr(task, "image_model_logical_key", ""),
        "video_provider": getattr(task, "video_provider", ""),
        "video_model": getattr(task, "video_model_logical_key", ""),
        "video_task_id": getattr(task, "video_task_id", ""),
        "video_url": getattr(task, "video_url", ""),
        "video_file_path": getattr(task, "video_file_path", ""),
        "video_download_status": getattr(task, "video_download_status", ""),
        "video_download_attempt_count": getattr(task, "video_download_attempt_count", 0),
        "last_video_download_time": getattr(task, "last_video_download_time", ""),
        "last_video_download_error": getattr(task, "last_video_download_error", ""),
        "manual_poll_count": getattr(task, "manual_poll_count", 0),
        "last_manual_poll_time": getattr(task, "last_manual_poll_time", ""),
        "last_manual_poll_result": getattr(task, "last_manual_poll_result", ""),
        "auto_retry_count": getattr(task, "auto_retry_count", 0),
        "last_auto_retry_time": getattr(task, "last_auto_retry_time", ""),
        "last_auto_retry_stage": getattr(task, "last_auto_retry_stage", ""),
        "last_auto_retry_reason": getattr(task, "last_auto_retry_reason", ""),
        "status": getattr(task, "status", ""),
        "created_at": getattr(task, "created_at", ""),
        "updated_at": getattr(task, "updated_at", ""),
    }


def save_image_task_assets(task, assets_root: str | Path) -> None:
    folder = image_assets_dir(assets_root, task)
    pid = safe_pid(getattr(task, "pid", ""))
    row = getattr(task, "row_index", 0)

    product_path = Path(str(getattr(task, "product_image_path", "") or ""))
    if product_path.exists() and product_path.is_file():
        suffix = product_path.suffix if product_path.suffix else ".png"
        product_copy = folder / f"{pid}_row{row}_product_image{suffix}"
        if not product_copy.exists():
            shutil.copy2(product_path, product_copy)

    (folder / f"{pid}_row{row}_image_prompt.txt").write_text(str(getattr(task, "image_prompt", "") or ""), encoding="utf-8")
    (folder / f"{pid}_row{row}_video_prompt.txt").write_text(str(getattr(task, "video_prompt", "") or ""), encoding="utf-8")
    (folder / f"{pid}_row{row}_metadata.json").write_text(json.dumps(task_metadata(task), ensure_ascii=False, indent=2), encoding="utf-8")


def save_video_task_metadata(task, video_root: str | Path, group_by_owner: bool = True) -> None:
    folder = video_download_dir(video_root, task, group_by_owner)
    (folder / "task_metadata.json").write_text(json.dumps(task_metadata(task), ensure_ascii=False, indent=2), encoding="utf-8")


def product_image_folder(netdisk_path: str | Path) -> Path:
    return Path(str(netdisk_path).strip()) / "01.产品白底图"


def _slash_path(value: str) -> str:
    return re.sub(r"/+", "/", str(value or "").replace("\\", "/")).strip()


def _safe_url_path(path: str) -> str:
    return "/".join(quote(unquote(part), safe=":@") for part in path.split("/") if part)


def convert_netdisk_path_to_http(path: str, config) -> str:
    raw = str(path or "").strip()
    if _is_http_url(raw):
        old_prefix = "http://media.pennitech.top:48080"
        http_prefix = str(getattr(config, "netdisk_http_prefix", "") or "").strip().rstrip("/")
        if raw.lower().startswith(old_prefix.lower()) and http_prefix:
            return f"{http_prefix}{raw[len(old_prefix):]}"
        return raw
    if not raw or not bool(getattr(config, "enable_netdisk_http_mapping", False)):
        return raw
    local_prefix = _slash_path(str(getattr(config, "netdisk_local_prefix", "") or "")).rstrip("/")
    http_prefix = str(getattr(config, "netdisk_http_prefix", "") or "").strip().rstrip("/")
    normalized = _slash_path(raw)
    if not http_prefix:
        return raw
    if local_prefix and normalized.lower().startswith(local_prefix.lower()):
        relative = normalized[len(local_prefix) :].lstrip("/")
        encoded = _safe_url_path(relative)
        return f"{http_prefix}/{encoded}" if encoded else http_prefix

    common_prefix = _common_netdisk_prefix(local_prefix)
    if not common_prefix or not normalized.lower().startswith(common_prefix.lower()):
        return raw
    relative = normalized[len(common_prefix) :].lstrip("/")
    encoded = _safe_url_path(relative)
    return f"{http_prefix}/{encoded}" if encoded else http_prefix


def _common_netdisk_prefix(local_prefix: str) -> str:
    normalized = _slash_path(local_prefix).rstrip("/")
    marker = "/01.产品信息"
    if marker in normalized:
        return normalized.split(marker, 1)[0].rstrip("/")
    return normalized


def path_to_http_url(path: str | Path | None, config) -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    converted = convert_netdisk_path_to_http(value, config)
    return converted if _is_http_url(converted) else ""


def sync_task_urls_from_paths(task, config, prefer_local_generated: bool = True, prefer_local_video: bool = True) -> None:
    existing_product_url = path_to_http_url(getattr(task, "product_image_url", ""), config)
    if existing_product_url:
        task.product_image_url = existing_product_url
    product_url = path_to_http_url(getattr(task, "product_image_path", ""), config)
    if product_url and not getattr(task, "product_image_url", None):
        task.product_image_url = product_url

    existing_generated_url = path_to_http_url(getattr(task, "generated_image_url", ""), config)
    if existing_generated_url:
        task.generated_image_url = existing_generated_url
    generated_url = path_to_http_url(getattr(task, "generated_image_path", ""), config)
    if generated_url and (prefer_local_generated or not getattr(task, "generated_image_url", None)):
        task.generated_image_url = generated_url

    existing_video_url = path_to_http_url(getattr(task, "video_url", ""), config)
    if existing_video_url:
        task.video_url = existing_video_url
    video_url = path_to_http_url(getattr(task, "video_file_path", ""), config)
    if video_url and (prefer_local_video or not getattr(task, "video_url", None)):
        task.video_url = video_url


def _is_http_url(value: str) -> bool:
    return str(value or "").strip().lower().startswith(("http://", "https://"))


def _http_product_dir(http_netdisk_path: str) -> str:
    return f"{http_netdisk_path.rstrip('/')}/01.%E4%BA%A7%E5%93%81%E7%99%BD%E5%BA%95%E5%9B%BE/"


def _extract_image_links_from_html(html: str, base_url: str) -> list[str]:
    links = re.findall(r"""href=["']([^"']+)["']""", html or "", flags=re.IGNORECASE)
    image_urls = []
    for link in links:
        absolute = urljoin(base_url, link)
        suffix = Path(unquote(urlparse(absolute).path)).suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            image_urls.append(absolute)
    return sorted(set(image_urls), key=lambda value: value.lower())


def _download_product_image(url: str, task, config) -> str:
    save_path = product_image_asset_path(getattr(config, "image_assets_root", Path.cwd()), task, url)
    return download_file(url, save_path, timeout=int(getattr(config, "request_timeout_seconds", 120) or 120))


def _specified_product_image_filename(task) -> str:
    text = str(getattr(task, "product_image_filename", "") or "").strip().replace("\\", "/")
    if not text:
        return ""
    # Treat the Excel value as a file name, not as an arbitrary path. This keeps
    # task rows from accidentally escaping the 01.产品白底图 directory.
    return Path(text).name.strip()


def _http_product_file_url(http_netdisk_path: str, filename: str) -> str:
    encoded_name = _safe_url_path(Path(filename).name)
    return f"{_http_product_dir(http_netdisk_path)}{encoded_name}"


def find_product_image_by_filename(netdisk_path: str | Path, filename: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    name = Path(str(filename or "").strip().replace("\\", "/")).name
    if not name:
        return None, None, None
    if Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
        return None, "SKIPPED_NO_PRODUCT_IMAGE", f"指定白底图文件名不是支持的图片格式：{name}"
    if not str(netdisk_path).strip():
        return None, "SKIPPED_NO_PRODUCT_IMAGE_FOLDER", "网盘路径为空，无法按指定文件名查找产品白底图"
    folder = product_image_folder(netdisk_path)
    try:
        if not folder.exists() or not folder.is_dir():
            return None, "SKIPPED_NO_PRODUCT_IMAGE_FOLDER", "未找到子目录：01.产品白底图"
        exact = folder / name
        if exact.exists() and exact.is_file():
            ok, message = validate_downloaded_file(exact, expected_kind="image")
            if not ok:
                return None, "SKIPPED_NO_PRODUCT_IMAGE", f"指定白底图文件损坏或不是有效图片：{name}；{message}"
            return str(exact), None, None
        lower_name = name.lower()
        for candidate in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
            if candidate.is_file() and candidate.suffix.lower() in IMAGE_EXTENSIONS and candidate.name.lower() == lower_name:
                ok, message = validate_downloaded_file(candidate, expected_kind="image")
                if not ok:
                    return None, "SKIPPED_NO_PRODUCT_IMAGE", f"指定白底图文件损坏或不是有效图片：{candidate.name}；{message}"
                return str(candidate), None, None
        return None, "SKIPPED_NO_PRODUCT_IMAGE", f"01.产品白底图目录下未找到指定文件：{name}"
    except OSError as exc:
        return None, "SKIPPED_NO_PRODUCT_IMAGE_FOLDER", f"网盘路径无法访问：{exc}"


def find_product_image_for_task(task, config) -> tuple[Optional[str], Optional[str], Optional[str]]:
    task.netdisk_original_path = task.netdisk_original_path or task.netdisk_path
    # Best-effort: try to map to HTTP path for display/metadata purposes. Failure here
    # MUST NOT block task execution — local netdisk path access is the primary flow.
    task.netdisk_http_path = convert_netdisk_path_to_http(task.netdisk_original_path or task.netdisk_path, config)
    sync_task_urls_from_paths(task, config, prefer_local_generated=False, prefer_local_video=False)

    # 1) If an explicit product image URL was provided in the Excel, honor it first.
    if task.product_image_url and _is_http_url(task.product_image_url):
        try:
            task.product_image_path = _download_product_image(task.product_image_url, task, config)
            task.product_image_source_type = task.product_image_source_type or "excel_url"
            return task.product_image_path, None, None
        except Exception as exc:
            # Fall through to local/HTTP discovery rather than killing the task.
            url_error = f"产品白底图URL下载失败：{exc}"
    else:
        url_error = None

    specified_filename = _specified_product_image_filename(task)
    if specified_filename:
        task.product_image_filename = specified_filename
        specified_image, specified_status, specified_error = find_product_image_by_filename(task.netdisk_path, specified_filename)
        if specified_image:
            task.product_image_path = specified_image
            task.product_image_source_type = "local_specified_filename"
            return specified_image, None, None
        if bool(getattr(config, "enable_netdisk_http_mapping", False)) and _is_http_url(task.netdisk_http_path):
            task.product_image_url = _http_product_file_url(task.netdisk_http_path, specified_filename)
            task.product_image_source_type = "http_specified_filename"
            try:
                task.product_image_path = _download_product_image(task.product_image_url, task, config)
                return task.product_image_path, None, None
            except Exception as exc:
                return None, "SKIPPED_NO_PRODUCT_IMAGE_URL", f"指定产品白底图下载失败：{task.product_image_url}；{exc}"
        return None, specified_status, specified_error

    # 2) Prefer reading the product image directly from the netdisk path on the
    #    local filesystem. This avoids any URL conversion and works as long as
    #    the network share is mounted.
    local_image, local_status, local_error = find_product_image(task.netdisk_path)
    if local_image:
        task.product_image_path = local_image
        task.product_image_source_type = "local"
        return local_image, None, None

    # 3) Optional fallback: if HTTP mapping is enabled AND the conversion produced a
    #    real HTTP URL, try discovering the product image via the HTTP directory.
    if bool(getattr(config, "enable_netdisk_http_mapping", False)) and _is_http_url(task.netdisk_http_path):
        directory_url = _http_product_dir(task.netdisk_http_path)
        try:
            response = _http_session().get(directory_url, timeout=int(getattr(config, "request_timeout_seconds", 120) or 120))
            response.raise_for_status()
            image_urls = _extract_image_links_from_html(response.text, directory_url)
        except Exception:
            image_urls = []
        if image_urls:
            task.product_image_url = image_urls[0]
            task.product_image_source_type = "http_directory_parse"
            try:
                task.product_image_path = _download_product_image(task.product_image_url, task, config)
                return task.product_image_path, None, None
            except Exception as exc:
                return None, "SKIPPED_NO_PRODUCT_IMAGE_URL", f"Http 产品白底图下载失败：{exc}"

    # 4) Nothing worked — surface the most specific local-side error we have.
    if url_error:
        return None, "SKIPPED_NO_PRODUCT_IMAGE_URL", url_error
    return None, local_status, local_error


def find_product_image(netdisk_path: str | Path) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not str(netdisk_path).strip():
        return None, "SKIPPED_NO_PRODUCT_IMAGE_FOLDER", "网盘路径为空"
    root = Path(str(netdisk_path).strip())
    try:
        root_exists = root.exists()
    except OSError as exc:
        return None, "SKIPPED_NO_PRODUCT_IMAGE_FOLDER", f"网盘路径无法访问：{exc}"
    if not root_exists:
        return None, "SKIPPED_NO_PRODUCT_IMAGE_FOLDER", f"网盘路径不存在：{root}"
    folder = product_image_folder(root)
    if not folder.exists() or not folder.is_dir():
        return None, "SKIPPED_NO_PRODUCT_IMAGE_FOLDER", "未找到子目录：01.产品白底图"
    images = sorted(
        [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS],
        key=lambda p: p.name.lower(),
    )
    valid_images = []
    invalid_errors = []
    for image in images:
        ok, message = validate_downloaded_file(image, expected_kind="image")
        if ok:
            valid_images.append(image)
        else:
            invalid_errors.append(f"{image.name}: {message}")
    if not valid_images:
        if invalid_errors:
            return None, "SKIPPED_NO_PRODUCT_IMAGE", "01.产品白底图目录下图片文件均无法通过校验：" + "；".join(invalid_errors[:3])
        return None, "SKIPPED_NO_PRODUCT_IMAGE", "01.产品白底图目录下未找到图片文件"
    return str(valid_images[0]), None, None


def image_output_path(output_dir: str | Path, pid: str, row_index: int) -> Path:
    return Path(output_dir) / "images" / safe_pid(pid) / f"{safe_pid(pid)}_row{row_index}_generated.png"


def netdisk_image_dir(netdisk_path: str | Path) -> Path:
    folder = Path(str(netdisk_path).strip()) / "03.VEO首帧图片"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def netdisk_image_output_path(netdisk_path: str | Path, pid: str, row_index: int) -> Path:
    return netdisk_image_dir(netdisk_path) / f"{safe_pid(pid)}_row{row_index}_generated.png"


def video_identifier(video_url: str | None = None, task_id: str | None = None) -> str:
    source = (video_url or task_id or "unknown").strip()
    return hashlib.sha1(source.encode("utf-8", errors="ignore")).hexdigest()[:12]


def video_filename(pid: str, row_index: int, video_url: str | None = None, task_id: str | None = None) -> str:
    return f"{safe_pid(pid)}_row{row_index}_{video_identifier(video_url, task_id)}.mp4"


def video_output_path(output_dir: str | Path, pid: str, row_index: int, video_url: str | None = None, task_id: str | None = None) -> Path:
    return Path(output_dir) / "videos" / safe_pid(pid) / video_filename(pid, row_index, video_url, task_id)


def netdisk_video_dir(netdisk_path: str | Path) -> Path:
    folder = Path(str(netdisk_path).strip()) / "03.Veo3视频"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def netdisk_video_output_path(
    netdisk_path: str | Path,
    pid: str,
    row_index: int,
    video_url: str | None = None,
    task_id: str | None = None,
) -> Path:
    return netdisk_video_dir(netdisk_path) / video_filename(pid, row_index, video_url, task_id)


def grouped_video_output_path(
    base_dir: str | Path,
    pid: str,
    row_index: int,
    video_url: str | None = None,
    task_id: str | None = None,
) -> Path:
    folder = Path(base_dir) / safe_pid(pid)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / video_filename(pid, row_index, video_url, task_id)


def existing_grouped_video_path(
    base_dir: str | Path,
    pid: str,
    row_index: int,
    video_url: str | None = None,
    task_id: str | None = None,
) -> Optional[Path]:
    folder = Path(base_dir) / safe_pid(pid)
    folder.mkdir(parents=True, exist_ok=True)
    ident = video_identifier(video_url, task_id)
    exact = folder / video_filename(pid, row_index, video_url, task_id)
    if exact.exists() and exact.stat().st_size > 0 and validate_downloaded_file(exact, expected_kind="video")[0]:
        return exact
    for match in sorted(folder.glob(f"{safe_pid(pid)}_row{row_index}_{ident}.mp4")):
        if match.is_file() and match.stat().st_size > 0 and validate_downloaded_file(match, expected_kind="video")[0]:
            return match
    return None


def existing_video_path(
    netdisk_path: str | Path,
    pid: str,
    row_index: int,
    video_url: str | None = None,
    task_id: str | None = None,
) -> Optional[Path]:
    folder = netdisk_video_dir(netdisk_path)
    ident = video_identifier(video_url, task_id)
    exact = folder / video_filename(pid, row_index, video_url, task_id)
    if exact.exists() and exact.stat().st_size > 0 and validate_downloaded_file(exact, expected_kind="video")[0]:
        return exact
    matches = sorted(folder.glob(f"{safe_pid(pid)}_row{row_index}_{ident}.mp4"))
    for match in matches:
        if match.is_file() and match.stat().st_size > 0 and validate_downloaded_file(match, expected_kind="video")[0]:
            return match
    return None


def save_base64_image(b64_string: str, save_path: str | Path) -> str:
    if b64_string.startswith("data:image"):
        b64_string = b64_string.split(",", 1)[1]
    image_bytes = base64.b64decode(b64_string)
    path = Path(save_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image_bytes)
    ok, message = validate_downloaded_file(path, expected_kind="image")
    if not ok:
        try:
            path.unlink()
        except OSError:
            pass
        raise IOError(f"invalid image file: {message}")
    return str(path)


def file_to_data_url(path: str | Path) -> str:
    file_path = Path(path)
    stat = file_path.stat()
    cache_key = (str(file_path.resolve()), stat.st_size, stat.st_mtime_ns)
    with _DATA_URL_CACHE_LOCK:
        cached = _DATA_URL_CACHE.get(cache_key)
        if cached is not None:
            return cached
    mime = mime_type_for(file_path)
    b64 = base64.b64encode(file_path.read_bytes()).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    with _DATA_URL_CACHE_LOCK:
        if len(_DATA_URL_CACHE) >= _DATA_URL_CACHE_MAX_ITEMS:
            _DATA_URL_CACHE.pop(next(iter(_DATA_URL_CACHE)))
        _DATA_URL_CACHE[cache_key] = data_url
    return data_url


def validate_downloaded_file(path: str | Path, expected_kind: str | None = None) -> tuple[bool, str]:
    file_path = Path(path)
    try:
        if not file_path.exists() or not file_path.is_file():
            return False, "file does not exist"
        size = file_path.stat().st_size
    except OSError as exc:
        return False, f"file is not accessible: {exc}"
    if size <= 0:
        return False, "file is empty"

    kind = (expected_kind or _kind_from_suffix(file_path)).lower()
    if kind == "image":
        return _validate_image_file(file_path)
    if kind == "video":
        return _validate_video_file(file_path)
    return True, "ok"


def _kind_from_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in {".mp4", ".mov", ".m4v", ".webm"}:
        return "video"
    return ""


def _validate_image_file(path: Path) -> tuple[bool, str]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        return True, "ok"
    except ImportError:
        try:
            head = path.read_bytes()[:16]
        except OSError as exc:
            return False, str(exc)
        signatures = (
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff",
            b"GIF87a",
            b"GIF89a",
            b"RIFF",
            b"BM",
        )
        return (True, "ok") if any(head.startswith(sig) for sig in signatures) else (False, "image header is not recognized")
    except Exception as exc:
        return False, str(exc)


def _validate_video_file(path: Path) -> tuple[bool, str]:
    try:
        size = path.stat().st_size
        if size < 1024:
            return False, f"video file is too small: {size} bytes"
        with path.open("rb") as handle:
            head = handle.read(64)
    except OSError as exc:
        return False, str(exc)

    suffix = path.suffix.lower()
    if suffix == ".webm":
        return (True, "ok") if head.startswith(b"\x1a\x45\xdf\xa3") else (False, "webm header is not recognized")
    if suffix in {".mov", ".m4v", ".mp4"}:
        if b"ftyp" not in head[:32]:
            return False, "mp4 ftyp box is missing"
        has_mdat = False
        has_index = False
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    if b"mdat" in chunk:
                        has_mdat = True
                    if b"moov" in chunk or b"moof" in chunk:
                        has_index = True
                    if has_mdat and has_index:
                        return True, "ok"
        except OSError as exc:
            return False, str(exc)
        if not has_mdat:
            return False, "mp4 media data box is missing"
        if not has_index:
            return False, "mp4 index box is missing"
    return True, "ok"


def _validator_for_path(path: Path) -> Callable[[Path], tuple[bool, str]] | None:
    kind = _kind_from_suffix(path)
    if kind == "image":
        return lambda p: validate_downloaded_file(p, expected_kind="image")
    if kind == "video":
        return lambda p: validate_downloaded_file(p, expected_kind="video")
    return None


def download_file(
    url: str,
    save_path: str | Path,
    timeout: int = 120,
    retry_count: int = 1,
    retry_interval_seconds: int = 2,
) -> str:
    path = Path(save_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    validator = _validator_for_path(path)
    if path.exists() and path.stat().st_size > 0:
        if validator is None:
            return str(path)
        ok, message = validator(path)
        if ok:
            return str(path)
        try:
            path.unlink()
        except OSError:
            pass
    retry_count = max(1, int(retry_count or 1))
    last_error: Exception | None = None
    tmp_path = path.with_suffix(path.suffix + ".part")
    for attempt in range(1, retry_count + 1):
        try:
            if tmp_path.exists():
                tmp_path.unlink()
            with _http_session().get(url, timeout=timeout, stream=True) as response:
                response.raise_for_status()
                expected_size = int(response.headers.get("Content-Length") or 0)
                content_encoding = str(response.headers.get("Content-Encoding") or "").strip()
                written = 0
                with tmp_path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                            written += len(chunk)
                if expected_size and not content_encoding and written != expected_size:
                    raise IOError(f"incomplete download: expected {expected_size} bytes, got {written} bytes")
            if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
                raise IOError("downloaded file is empty")
            tmp_path.replace(path)
            if validator is not None:
                ok, message = validator(path)
                if not ok:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    raise IOError(f"downloaded media validation failed: {message}")
            return str(path)
        except Exception as exc:
            last_error = exc
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            if attempt < retry_count:
                time.sleep(max(0, int(retry_interval_seconds or 0)) * attempt)
    if last_error:
        raise last_error
    raise RuntimeError("download failed")


def download_video_to_path(
    video_url: str,
    save_path: str | Path,
    timeout: int = 180,
    retry_count: int = 1,
    retry_interval_seconds: int = 2,
) -> tuple[str, bool]:
    path = Path(save_path)
    if path.exists() and path.stat().st_size > 0:
        ok, _message = validate_downloaded_file(path, expected_kind="video")
        if ok:
            return str(path), False
        try:
            path.unlink()
        except OSError:
            pass
    return download_file(
        video_url,
        path,
        timeout=timeout,
        retry_count=retry_count,
        retry_interval_seconds=retry_interval_seconds,
    ), True


def mime_type_for(path: str | Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def open_path(path_or_url: str) -> None:
    if not path_or_url:
        return
    os.startfile(path_or_url)  # type: ignore[attr-defined]
