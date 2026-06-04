from __future__ import annotations

import re
from pathlib import Path

from light_image_tool.image_selector import parse_custom_names, select_images, supported_image_files
from light_image_tool.models import PIDScanResult


SCAN_STATUS_TEXT = {
    "found": "已找到",
    "not_found": "未找到 PID 目录",
    "ambiguous": "匹配歧义",
    "product_info_missing": "缺少 01.product_info",
    "no_images": "没有图片",
    "root_unavailable": "根目录不可访问",
}


def parse_pid_input(text: str) -> list[str]:
    parts = re.split(r"[\n,，;；\t ]+", str(text or ""))
    seen: set[str] = set()
    pids: list[str] = []
    for part in parts:
        pid = part.strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        pids.append(pid)
    return pids


class PIDScanner:
    def __init__(
        self,
        product_root_dir: str,
        product_info_subdir: str,
        output_subdir: str,
        pid_match_mode: str = "exact",
    ) -> None:
        self.product_root_dir = product_root_dir
        self.product_info_subdir = product_info_subdir
        self.output_subdir = output_subdir
        self.pid_match_mode = pid_match_mode if pid_match_mode in {"exact", "case_insensitive", "contains"} else "exact"

    def scan_many(
        self,
        pids: list[str],
        image_select_rule: str,
        image_select_count: int = 10,
        custom_image_names: list[str] | str | None = None,
        range_start: int | None = None,
        range_end: int | None = None,
    ) -> list[PIDScanResult]:
        return [
            self.scan_one(pid, image_select_rule, image_select_count, custom_image_names, range_start, range_end)
            for pid in pids
        ]

    def scan_one(
        self,
        pid: str,
        image_select_rule: str,
        image_select_count: int = 10,
        custom_image_names: list[str] | str | None = None,
        range_start: int | None = None,
        range_end: int | None = None,
    ) -> PIDScanResult:
        pid = str(pid or "").strip()
        root = Path(self.product_root_dir)
        try:
            if not root.exists() or not root.is_dir():
                return self._result(pid, "root_unavailable", f"产品资料根目录不可访问：{root}")
        except OSError as exc:
            return self._result(pid, "root_unavailable", f"产品资料根目录不可访问：{exc}")

        matches = self._find_pid_dirs(root, pid)
        if not matches:
            return self._result(pid, "not_found", f"未找到 PID 目录：{pid}")
        if len(matches) > 1:
            return PIDScanResult(
                pid=pid,
                status="ambiguous",
                pid_dir=None,
                product_info_dir=None,
                output_dir=None,
                selected_images=[],
                error_message="PID 匹配到多个候选目录，已跳过",
                candidate_dirs=[str(path) for path in matches],
            )

        pid_dir = matches[0]
        product_info_dir = pid_dir / self.product_info_subdir
        try:
            if not product_info_dir.exists() or not product_info_dir.is_dir():
                return self._result(
                    pid,
                    "product_info_missing",
                    f"缺少 {self.product_info_subdir} 子目录",
                    pid_dir=pid_dir,
                    product_info_dir=product_info_dir,
                    output_dir=pid_dir / self.output_subdir,
                )
        except OSError as exc:
            return self._result(
                pid,
                "product_info_missing",
                f"{self.product_info_subdir} 无法访问：{exc}",
                pid_dir=pid_dir,
                product_info_dir=product_info_dir,
                output_dir=pid_dir / self.output_subdir,
            )

        images = supported_image_files(product_info_dir)
        if not images:
            return self._result(
                pid,
                "no_images",
                f"{self.product_info_subdir} 中没有支持的图片文件",
                pid_dir=pid_dir,
                product_info_dir=product_info_dir,
                output_dir=pid_dir / self.output_subdir,
            )
        selection = select_images(
            images,
            image_select_rule,
            image_select_count,
            parse_custom_names(custom_image_names),
            range_start=range_start,
            range_end=range_end,
        )
        if not selection.selected:
            return PIDScanResult(
                pid=pid,
                status="no_images",
                pid_dir=str(pid_dir),
                product_info_dir=str(product_info_dir),
                output_dir=str(pid_dir / self.output_subdir),
                selected_images=[],
                error_message="图片选择规则没有选中任何可用图片",
                missing_image_names=selection.missing_names,
            )
        message = None
        if selection.missing_names:
            message = "指定文件不存在，已跳过：" + "，".join(selection.missing_names[:10])
        return PIDScanResult(
            pid=pid,
            status="found",
            pid_dir=str(pid_dir),
            product_info_dir=str(product_info_dir),
            output_dir=str(pid_dir / self.output_subdir),
            selected_images=[str(path) for path in selection.selected],
            error_message=message,
            missing_image_names=selection.missing_names,
        )

    def _find_pid_dirs(self, root: Path, pid: str) -> list[Path]:
        exact = root / pid
        try:
            if exact.exists() and exact.is_dir():
                return [exact]
        except OSError:
            return []
        if self.pid_match_mode == "exact":
            return []
        try:
            children = [path for path in root.iterdir() if path.is_dir()]
        except OSError:
            return []
        normalized_pid = pid.strip()
        if self.pid_match_mode == "case_insensitive":
            return [path for path in children if path.name.strip().lower() == normalized_pid.lower()]
        return [path for path in children if normalized_pid.lower() in path.name.strip().lower()]

    @staticmethod
    def _result(
        pid: str,
        status: str,
        error_message: str,
        *,
        pid_dir: Path | None = None,
        product_info_dir: Path | None = None,
        output_dir: Path | None = None,
    ) -> PIDScanResult:
        return PIDScanResult(
            pid=pid,
            status=status,
            pid_dir=str(pid_dir) if pid_dir else None,
            product_info_dir=str(product_info_dir) if product_info_dir else None,
            output_dir=str(output_dir) if output_dir else None,
            selected_images=[],
            error_message=error_message,
        )
