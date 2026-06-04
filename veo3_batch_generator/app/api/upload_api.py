from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import requests

from app.api.response_parser import extract_image_url
from app.file_utils import mime_type_for


def upload_image_for_public_url(
    image_path: str,
    upload_api_url: str,
    api_key: str = "",
    file_field: str = "file",
    timeout: int = 120,
) -> tuple[Optional[str], Any, Optional[str]]:
    if not upload_api_url:
        return None, None, "未配置 IMAGE_UPLOAD_API_URL"
    path = Path(image_path)
    if not path.exists():
        return None, None, f"待上传图片不存在：{path}"
    if _is_local_oss_upload_url(upload_api_url):
        return _upload_image_with_sora2_oss(path)

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        with path.open("rb") as fh:
            files = {file_field: (path.name, fh, mime_type_for(path))}
            response = requests.post(upload_api_url, headers=headers, files=files, timeout=timeout)
        try:
            raw = response.json()
        except ValueError:
            raw = response.text
        if not response.ok:
            return None, raw, f"图片上传失败：HTTP {response.status_code} {raw}"
        if not isinstance(raw, dict):
            return None, raw, "图片上传接口返回非 JSON"
        url = extract_image_url(raw)
        if not url:
            return None, raw, "图片上传接口返回字段缺失：未找到 url/image_url"
        if url.startswith("data:image"):
            return None, raw, "图片上传接口返回的是 data URL，不是公网 URL"
        return url, raw, None
    except Exception as exc:
        return None, None, f"图片上传异常：{exc}"


def _is_local_oss_upload_url(value: str) -> bool:
    return str(value or "").strip().lower() in {"oss://sora2-mission", "local-oss://sora2-mission"}


def _upload_image_with_sora2_oss(path: Path) -> tuple[Optional[str], Any, Optional[str]]:
    uploader_path = _find_sora2_oss_uploader()
    if uploader_path is None:
        return None, None, "未找到 Sora2-mission OSS 上传模块"
    sora_root = uploader_path.parents[2]
    added_paths: list[str] = []
    for candidate in [str(sora_root), str(uploader_path.parent)]:
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
            added_paths.append(candidate)
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    upload_path = path
    normalized = False
    try:
        upload_path, temp_dir, normalized = _normalize_image_for_external_reference(path)
        spec = importlib.util.spec_from_file_location("_sora2_mission_oss_uploader", uploader_path)
        if spec is None or spec.loader is None:
            return None, None, f"OSS 上传模块无法加载：{uploader_path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        uploader = getattr(module, "upload_file_and_sign_url", None)
        if not callable(uploader):
            return None, None, "OSS 上传模块缺少 upload_file_and_sign_url"
        url = str(uploader(str(upload_path)) or "").strip()
        if not url.startswith(("http://", "https://")):
            return None, {"url": url}, "OSS 上传未返回公网 URL"
        url = _prefer_https_url(url)
        return url, {"mode": "local_oss", "uploader": str(uploader_path), "normalized": normalized}, None
    except Exception as exc:
        return None, None, f"OSS 图片上传异常：{exc}"
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()
        for candidate in added_paths:
            try:
                sys.path.remove(candidate)
            except ValueError:
                pass


def _find_sora2_oss_uploader() -> Path | None:
    env_root = str(os.getenv("SORA2_MISSION_ROOT") or "").strip()
    roots = []
    if env_root:
        roots.append(Path(env_root))
    workspace_root = Path(__file__).resolve().parents[3]
    roots.append(workspace_root / "Sora2-mission")
    for root in roots:
        candidate = root / "xv_gui" / "providers" / "oss_uploader.py"
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _prefer_https_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme.lower() == "http":
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return str(url or "").strip()


def _normalize_image_for_external_reference(path: Path) -> tuple[Path, tempfile.TemporaryDirectory[str] | None, bool]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            fmt = str(image.format or "").upper()
            suffix = path.suffix.lower()
            if (fmt == "JPEG" and suffix in {".jpg", ".jpeg"}) or (fmt == "PNG" and suffix == ".png"):
                return path, None, False
            temp_dir = tempfile.TemporaryDirectory(prefix="veo3_ref_upload_")
            normalized_path = Path(temp_dir.name) / f"{path.stem}.png"
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            image.save(normalized_path, format="PNG")
            return normalized_path, temp_dir, True
    except Exception:
        return path, None, False
