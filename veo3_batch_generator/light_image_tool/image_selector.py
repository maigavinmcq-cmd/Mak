from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


@dataclass
class ImageSelectionResult:
    selected: list[Path]
    missing_names: list[str] = field(default_factory=list)
    error_message: str | None = None


def is_supported_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def natural_sort_key(path: str | Path) -> list[object]:
    name = Path(path).name.lower()
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def supported_image_files(folder: str | Path) -> list[Path]:
    root = Path(folder)
    try:
        files = [path for path in root.iterdir() if path.is_file() and is_supported_image(path)]
    except OSError:
        return []
    return sorted(files, key=natural_sort_key)


def parse_custom_names(text_or_names: str | Iterable[str] | None) -> list[str]:
    if text_or_names is None:
        return []
    if isinstance(text_or_names, str):
        raw_parts = re.split(r"[\n,，;；]+", text_or_names)
    else:
        raw_parts = list(text_or_names)
    return [Path(str(item).strip().replace("\\", "/")).name for item in raw_parts if str(item).strip()]


def select_images(
    images: list[Path],
    rule: str,
    count: int = 10,
    custom_names: str | Iterable[str] | None = None,
    range_start: int | None = None,
    range_end: int | None = None,
) -> ImageSelectionResult:
    sorted_images = sorted(images, key=natural_sort_key)
    count = max(1, int(count or 1))
    if rule == "all":
        return ImageSelectionResult(sorted_images)
    if rule == "last_n":
        return ImageSelectionResult(sorted_images[-count:])
    if rule == "custom_names":
        names = parse_custom_names(custom_names)
        by_name = {path.name.lower(): path for path in sorted_images}
        selected: list[Path] = []
        missing: list[str] = []
        for name in names:
            match = by_name.get(name.lower())
            if match is None:
                missing.append(name)
            elif match not in selected:
                selected.append(match)
        return ImageSelectionResult(selected, missing)
    if rule == "custom_range":
        start = max(1, int(range_start or 1))
        end = max(start, int(range_end or start))
        return ImageSelectionResult(sorted_images[start - 1 : end])
    return ImageSelectionResult(sorted_images[:count])
