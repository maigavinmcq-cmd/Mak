from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import requests


INVALID_WINDOWS_CHARS = r'\/:*?"<>|'


def sanitize_filename(value: str, fallback: str = "output") -> str:
    text = str(value or "").strip()
    if not text:
        text = fallback
    cleaned = "".join("_" if char in INVALID_WINDOWS_CHARS else char for char in text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix or ".png"
    match = re.match(r"^(.*)_(\d{3})$", stem)
    base_stem = match.group(1) if match else stem
    index = int(match.group(2)) + 1 if match else 2
    while True:
        candidate = path.with_name(f"{base_stem}_{index:03d}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def make_output_path(
    *,
    output_dir: str | Path,
    pid: str | None,
    source_image_name: str,
    task_type: str,
    generation_index: int = 1,
    suffix: str = ".png",
) -> Path:
    output_root = Path(output_dir)
    source_stem = sanitize_filename(Path(source_image_name).stem, "source").rstrip("_ .") or "source"
    ext = suffix if suffix.startswith(".") else f".{suffix}"
    if task_type == "pid_white_bg":
        pid_part = sanitize_filename(pid or "unknown_pid", "unknown_pid")
        base_name = f"{pid_part}_{source_stem}_white_bg_{max(1, generation_index):03d}{ext}"
    else:
        base_name = f"{source_stem}_image_gen_{max(1, generation_index):03d}{ext}"
    return _unique_path(output_root / base_name)


def write_image_bytes(image_bytes: bytes, output_path: str | Path) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image_bytes)
    return str(path)


def download_image_url(url: str, output_path: str | Path, timeout: int = 180) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, timeout=timeout, stream=True) as response:
        response.raise_for_status()
        tmp = path.with_suffix(path.suffix + ".part")
        if tmp.exists():
            tmp.unlink()
        with tmp.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
        tmp.replace(path)
    return str(path)


def suffix_from_url(url: str, default: str = ".png") -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"} else default
