# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import shutil
import threading
import queue
import time
import hashlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# =========================
# Core filesystem helpers
# =========================
def walk_all_dirs(root: str) -> List[str]:
    """Return all subdirectories under root (excluding root itself)."""
    dirs = []
    for cur, subdirs, _files in os.walk(root):
        for d in subdirs:
            dirs.append(os.path.join(cur, d))
    return dirs


def iter_files_recursive(folder: str):
    for cur, _subdirs, files in os.walk(folder):
        for fn in files:
            yield os.path.join(cur, fn)


def safe_mkdir(path: str):
    os.makedirs(path, exist_ok=True)


def delete_dir_if_empty_tree(path: str):
    """Delete empty directories bottom-up."""
    for cur, subdirs, files in os.walk(path, topdown=False):
        if not subdirs and not files:
            try:
                os.rmdir(cur)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def unique_path(path: str) -> str:
    """If path exists, add suffix ' (n)' before extension."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{base} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def build_name_map(root: str) -> Dict[str, List[str]]:
    all_dirs = walk_all_dirs(root)
    name_map: Dict[str, List[str]] = defaultdict(list)
    for d in all_dirs:
        name_map[os.path.basename(d)].append(d)
    return name_map


def count_files_recursive(folder: str) -> int:
    total = 0
    for _ in iter_files_recursive(folder):
        total += 1
    return total


def move_file_preserve_rel_dedupe(
    src_file: str,
    src_base_dir: str,
    dst_base_dir: str,
    dedupe_same_name_same_path: bool = True
) -> Tuple[str, str, str]:
    """
    Move src_file into dst_base_dir preserving relative structure (relative to src_base_dir).

    If destination file already exists:
      - if same size and same sha256 => treat as duplicate, delete src (keep one)
      - else rename destination with (n) and move

    Returns: (src, final_dst, action)
      action in {"moved", "dedup_deleted_src", "renamed_moved"}
    """
    rel = os.path.relpath(src_file, src_base_dir)
    dst_file = os.path.join(dst_base_dir, rel)
    safe_mkdir(os.path.dirname(dst_file))

    if os.path.exists(dst_file) and os.path.isfile(dst_file):
        if dedupe_same_name_same_path:
            try:
                if os.path.getsize(src_file) == os.path.getsize(dst_file):
                    if sha256_file(src_file) == sha256_file(dst_file):
                        os.remove(src_file)
                        return src_file, dst_file, "dedup_deleted_src"
            except Exception:
                # fall back to rename strategy
                pass

        final_dst = unique_path(dst_file)
        shutil.move(src_file, final_dst)
        return src_file, final_dst, "renamed_moved"

    shutil.move(src_file, dst_file)
    return src_file, dst_file, "moved"


def copy_file_preserve_rel(src_file: str, src_base_dir: str, dst_base_dir: str) -> Tuple[str, str]:
    rel = os.path.relpath(src_file, src_base_dir)
    dst_file = os.path.join(dst_base_dir, rel)
    safe_mkdir(os.path.dirname(dst_file))
    final_dst = unique_path(dst_file)
    shutil.copy2(src_file, final_dst)
    return src_file, final_dst


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def list_direct_files(folder: str) -> List[str]:
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    files = []
    for name in names:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            files.append(path)
    return files


def classify_pair_split_files(folder: str) -> Tuple[List[str], List[str]]:
    images: List[str] = []
    txts: List[str] = []
    for path in list_direct_files(folder):
        ext = os.path.splitext(path)[1].lower()
        if ext in IMAGE_EXTS:
            images.append(path)
        elif ext == ".txt":
            txts.append(path)
    images.sort(key=lambda p: os.path.basename(p).lower())
    txts.sort(key=lambda p: os.path.basename(p).lower())
    return images, txts


# =========================
# File scan & split
# =========================
@dataclass
class FileScanResult:
    root: str
    total_files_in_subdirs: int
    matched: int
    not_matched: int
    keyword: str


@dataclass
class PairSplitPlan:
    src_dir: str
    rule: str
    images: List[str]
    txts: List[str]
    target_count: int


@dataclass
class PairSplitPreviewResult:
    root: str
    scanned_dirs: int
    planned_dirs: int
    planned_new_dirs: int
    skipped_noop: int
    skipped_unsupported: int
    plans: List[PairSplitPlan]
    unsupported_examples: List[str]


def scan_files_in_subdirs(
    root: str,
    keyword: str,
    case_insensitive: bool,
    log_cb,
    progress_cb,
    stop_flag: threading.Event
) -> FileScanResult:
    """Count files inside subdirectories of root and optional keyword match counts."""
    root = os.path.abspath(root)
    kw = (keyword or "").strip()
    kw_cmp = kw.lower() if case_insensitive else kw

    mode_text = "不区分大小写" if case_insensitive else "区分大小写"
    log_cb(f"开始扫描（只统计子目录内文件，不含 root 直接文件）: {root}")
    log_cb(f"关键字: {kw!r}（{mode_text}）")

    all_files: List[str] = []
    for cur, _subdirs, files in os.walk(root):
        if stop_flag.is_set():
            raise RuntimeError("已取消")
        if os.path.abspath(cur) == root:
            continue
        for fn in files:
            all_files.append(os.path.join(cur, fn))

    total = max(1, len(all_files))
    progress_cb(0, total)

    total_files = 0
    matched = 0
    for i, p in enumerate(all_files, 1):
        if stop_flag.is_set():
            raise RuntimeError("已取消")
        total_files += 1
        if kw_cmp:
            fn = os.path.basename(p)
            name_cmp = fn.lower() if case_insensitive else fn
            if kw_cmp in name_cmp:
                matched += 1
        if i % 200 == 0 or i == total:
            progress_cb(i, total)

    not_matched = total_files - matched if kw_cmp else total_files
    log_cb(f"扫描完成，子目录内文件总数: {total_files}")
    if kw_cmp:
        log_cb(f"包含关键字的文件数: {matched}")
        log_cb(f"不包含关键字的文件数: {not_matched}")
    else:
        log_cb("未输入关键字：只输出子目录内文件总数。")

    return FileScanResult(
        root=root,
        total_files_in_subdirs=total_files,
        matched=matched if kw_cmp else 0,
        not_matched=not_matched,
        keyword=kw,
    )


def build_pair_split_plan_for_dir(folder: str) -> Tuple[Optional[PairSplitPlan], str]:
    images, txts = classify_pair_split_files(folder)
    img_n = len(images)
    txt_n = len(txts)

    if img_n == 1 and txt_n == 1:
        return None, "already_ok"
    if img_n == 0 or txt_n == 0:
        return None, "missing_side"
    if img_n > 1 and txt_n == 1:
        return PairSplitPlan(folder, "multi_image_single_txt", images, txts, img_n), "planned"
    if img_n == 1 and txt_n > 1:
        return PairSplitPlan(folder, "single_image_multi_txt", images, txts, txt_n), "planned"
    if img_n > 1 and txt_n > 1 and img_n == txt_n:
        return PairSplitPlan(folder, "equal_pairs", images, txts, img_n), "planned"
    return None, "unsupported_combo"


def preview_pair_split_subdirs(
    root: str,
    log_cb,
    progress_cb,
    stop_flag: threading.Event,
) -> PairSplitPreviewResult:
    root = os.path.abspath(root)
    all_dirs = walk_all_dirs(root)
    total = max(1, len(all_dirs))
    progress_cb(0, total)
    log_cb(f"开始预演图文拆分: {root}")

    plans: List[PairSplitPlan] = []
    skipped_noop = 0
    skipped_unsupported = 0
    unsupported_examples: List[str] = []

    for i, folder in enumerate(sorted(all_dirs), 1):
        if stop_flag.is_set():
            raise RuntimeError("已取消")
        plan, status = build_pair_split_plan_for_dir(folder)
        if status == "planned" and plan:
            plans.append(plan)
            log_cb(
                f"[可拆分] {folder} | rule={plan.rule} | images={len(plan.images)} | "
                f"txt={len(plan.txts)} | new_dirs={plan.target_count}"
            )
        elif status == "unsupported_combo":
            skipped_unsupported += 1
            images, txts = classify_pair_split_files(folder)
            detail = f"{folder} | images={len(images)} | txt={len(txts)}"
            if len(unsupported_examples) < 20:
                unsupported_examples.append(detail)
            log_cb(f"[跳过] 数量组合暂不支持: {detail}")
        else:
            skipped_noop += 1
        if i % 100 == 0 or i == total:
            progress_cb(i, total)

    planned_new_dirs = sum(p.target_count for p in plans)
    log_cb(
        f"图文拆分预演完成：扫描目录={len(all_dirs)} | 可拆分目录={len(plans)} | "
        f"将创建子目录={planned_new_dirs} | 跳过={skipped_noop} | 不支持={skipped_unsupported}"
    )
    return PairSplitPreviewResult(
        root=root,
        scanned_dirs=len(all_dirs),
        planned_dirs=len(plans),
        planned_new_dirs=planned_new_dirs,
        skipped_noop=skipped_noop,
        skipped_unsupported=skipped_unsupported,
        plans=plans,
        unsupported_examples=unsupported_examples,
    )


def copy_file_to_dir(src_file: str, dst_dir: str) -> str:
    safe_mkdir(dst_dir)
    dst_file = unique_path(os.path.join(dst_dir, os.path.basename(src_file)))
    shutil.copy2(src_file, dst_file)
    return dst_file


def move_file_to_dir(src_file: str, dst_dir: str) -> str:
    safe_mkdir(dst_dir)
    dst_file = unique_path(os.path.join(dst_dir, os.path.basename(src_file)))
    shutil.move(src_file, dst_file)
    return dst_file


def prepare_pair_split_target_dirs(src_dir: str, count: int) -> List[str]:
    parent = os.path.dirname(src_dir)
    base = os.path.basename(src_dir)
    targets = [os.path.join(parent, f"{base}_{i}") for i in range(1, count + 1)]
    conflicts = [p for p in targets if os.path.exists(p)]
    if conflicts:
        raise RuntimeError(
            f"目标目录已存在，跳过以避免覆盖：{src_dir} -> {', '.join(conflicts[:3])}"
            + (" ..." if len(conflicts) > 3 else "")
        )
    for dst in targets:
        safe_mkdir(dst)
    return targets


def execute_pair_split_plan(plan: PairSplitPlan, log_cb, stop_flag: threading.Event) -> int:
    if stop_flag.is_set():
        raise RuntimeError("已取消")
    targets = prepare_pair_split_target_dirs(plan.src_dir, plan.target_count)
    created = len(targets)
    log_cb(f"开始拆分: {plan.src_dir} | rule={plan.rule} | targets={created}")

    if plan.rule == "multi_image_single_txt":
        shared_txt = plan.txts[0]
        for dst, image in zip(targets, plan.images):
            if stop_flag.is_set():
                raise RuntimeError("已取消")
            move_file_to_dir(image, dst)
            copy_file_to_dir(shared_txt, dst)
        os.remove(shared_txt)
    elif plan.rule == "single_image_multi_txt":
        shared_image = plan.images[0]
        for dst, txt in zip(targets, plan.txts):
            if stop_flag.is_set():
                raise RuntimeError("已取消")
            copy_file_to_dir(shared_image, dst)
            move_file_to_dir(txt, dst)
        os.remove(shared_image)
    elif plan.rule == "equal_pairs":
        for dst, image, txt in zip(targets, plan.images, plan.txts):
            if stop_flag.is_set():
                raise RuntimeError("已取消")
            move_file_to_dir(image, dst)
            move_file_to_dir(txt, dst)
    else:
        raise ValueError(f"未知拆分规则: {plan.rule}")

    delete_dir_if_empty_tree(plan.src_dir)
    log_cb(f"拆分完成: {plan.src_dir} -> {created} 个子目录")
    return created


def run_pair_split_subdirs(
    root: str,
    log_cb,
    progress_cb,
    stop_flag: threading.Event,
) -> Tuple[int, int, int]:
    preview = preview_pair_split_subdirs(root, log_cb, progress_cb, stop_flag)
    total = max(1, len(preview.plans))
    progress_cb(0, total)
    done_dirs = 0
    created_dirs = 0

    for i, plan in enumerate(preview.plans, 1):
        if stop_flag.is_set():
            raise RuntimeError("已取消")
        created_dirs += execute_pair_split_plan(plan, log_cb, stop_flag)
        done_dirs += 1
        progress_cb(i, total)

    log_cb(f"图文拆分执行完成：已拆分目录={done_dirs} | 新建目录={created_dirs}")
    return done_dirs, created_dirs, preview.skipped_unsupported


def split_files_by_keyword_to_folders(
    root: str,
    keyword: str,
    case_insensitive: bool,
    output_dir: str,
    move_files: bool,
    log_cb,
    progress_cb,
    stop_flag: threading.Event,
) -> Tuple[int, int]:
    """Split files into Matched/NotMatched while preserving relative paths."""
    root = os.path.abspath(root)
    output_dir = os.path.abspath(output_dir)

    kw = (keyword or "").strip()
    if not kw:
        raise ValueError("关键字不能为空。")
    kw_cmp = kw.lower() if case_insensitive else kw

    matched_root = os.path.join(output_dir, "Matched")
    not_root = os.path.join(output_dir, "NotMatched")
    safe_mkdir(matched_root)
    safe_mkdir(not_root)

    log_cb(f"开始分拣输出: {output_dir}")
    log_cb(f"模式：{'移动' if move_files else '复制'}")
    log_cb(f"Matched => {matched_root}")
    log_cb(f"NotMatched => {not_root}")

    all_files = []
    for cur, _subdirs, files in os.walk(root):
        if stop_flag.is_set():
            raise RuntimeError("已取消")
        if os.path.abspath(cur) == root:
            continue
        for fn in files:
            all_files.append(os.path.join(cur, fn))

    total = max(1, len(all_files))
    progress_cb(0, total)

    matched = 0
    not_matched = 0

    for i, src in enumerate(all_files, 1):
        if stop_flag.is_set():
            raise RuntimeError("已取消")

        fn = os.path.basename(src)
        name_cmp = fn.lower() if case_insensitive else fn
        is_match = kw_cmp in name_cmp

        dst_base = matched_root if is_match else not_root

        if move_files:
            rel = os.path.relpath(src, root)
            dst = os.path.join(dst_base, rel)
            safe_mkdir(os.path.dirname(dst))
            dst = unique_path(dst)
            shutil.move(src, dst)
        else:
            copy_file_preserve_rel(src, root, dst_base)

        if is_match:
            matched += 1
        else:
            not_matched += 1

        if i % 200 == 0 or i == total:
            progress_cb(i, total)

    log_cb(f"分拣完成: matched={matched} | not_matched={not_matched}")
    return matched, not_matched


def split_files_by_count_to_batches(
    root: str,
    output_dir: str,
    files_per_batch: int,
    log_cb,
    progress_cb,
    stop_flag: threading.Event,
) -> Tuple[int, int]:
    """
    Move files under root's subdirectories into batch folders by fixed file count.
    Keep original relative structure (relative to root) inside each batch folder.
    """
    root = os.path.abspath(root)
    output_dir = os.path.abspath(output_dir)
    n = int(files_per_batch)
    if n <= 0:
        raise ValueError("files_per_batch must be > 0")

    all_files: List[str] = []
    for cur, _subdirs, files in os.walk(root):
        if stop_flag.is_set():
            raise RuntimeError("已取消")
        if os.path.abspath(cur) == root:
            continue
        for fn in files:
            all_files.append(os.path.join(cur, fn))

    all_files.sort()
    total_files = len(all_files)
    if total_files == 0:
        log_cb("没有可拆分的文件（只处理 root 子目录内文件）。")
        progress_cb(1, 1)
        return 0, 0

    total_batches = (total_files + n - 1) // n
    log_cb(f"开始按数量拆分：total_files={total_files}, files_per_batch={n}, total_batches={total_batches}")
    progress_cb(0, total_files)

    for i, src in enumerate(all_files, 1):
        if stop_flag.is_set():
            raise RuntimeError("已取消")

        batch_idx = (i - 1) // n + 1
        batch_root = os.path.join(output_dir, f"batch_{batch_idx:03d}")
        rel = os.path.relpath(src, root)
        dst = os.path.join(batch_root, rel)
        safe_mkdir(os.path.dirname(dst))
        dst = unique_path(dst)
        shutil.move(src, dst)

        if i % 200 == 0 or i == total_files:
            progress_cb(i, total_files)

    log_cb(f"按数量拆分完成：total_files={total_files} | batches={total_batches} | output={output_dir}")
    return total_files, total_batches


def split_files_by_parts_across_subdirs(
    root: str,
    output_dir: str,
    total_parts: int,
    log_cb,
    progress_cb,
    stop_flag: threading.Event,
) -> Tuple[int, int, int]:
    """
    Split files into N parts by subdirectory-wise balancing:
    - For each direct child directory of root, split its files into N balanced chunks.
    - Compose each final part from chunk-i of every subdirectory.
    - Move files and preserve original relative structure under root.
    Returns: (moved_files, total_parts, non_empty_parts)
    """
    root = os.path.abspath(root)
    output_dir = os.path.abspath(output_dir)
    n = int(total_parts)
    if n <= 0:
        raise ValueError("total_parts must be > 0")

    direct_subdirs: List[str] = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.isdir(p):
            direct_subdirs.append(p)

    if not direct_subdirs:
        log_cb("没有可拆分的子目录（root 下不存在子目录）。")
        progress_cb(1, 1)
        return 0, n, 0

    files_by_subdir: Dict[str, List[str]] = {}
    total_files = 0
    for sub in direct_subdirs:
        if stop_flag.is_set():
            raise RuntimeError("已取消")
        flist: List[str] = []
        for cur, _subdirs, files in os.walk(sub):
            if stop_flag.is_set():
                raise RuntimeError("已取消")
            for fn in files:
                flist.append(os.path.join(cur, fn))
        flist.sort()
        if flist:
            files_by_subdir[sub] = flist
            total_files += len(flist)

    if total_files == 0:
        log_cb("没有可拆分的文件（只处理 root 子目录内文件）。")
        progress_cb(1, 1)
        return 0, n, 0

    log_cb(
        f"开始按份数拆分：total_files={total_files}, total_parts={n}, "
        f"subdirs_with_files={len(files_by_subdir)}"
    )
    progress_cb(0, total_files)

    moved = 0
    non_empty_parts: set[int] = set()

    for sub, flist in files_by_subdir.items():
        if stop_flag.is_set():
            raise RuntimeError("已取消")
        m = len(flist)
        sub_rel = os.path.relpath(sub, root)

        # Balanced chunking for current subdir: each part gets floor/ceil(m/n) files.
        for part_idx in range(n):
            start = (part_idx * m) // n
            end = ((part_idx + 1) * m) // n
            if start >= end:
                continue
            part_no = part_idx + 1
            part_root = os.path.join(output_dir, f"part_{part_no:03d}")
            non_empty_parts.add(part_no)

            for src in flist[start:end]:
                if stop_flag.is_set():
                    raise RuntimeError("已取消")
                rel_in_sub = os.path.relpath(src, sub)
                dst = os.path.join(part_root, sub_rel, rel_in_sub)
                safe_mkdir(os.path.dirname(dst))
                dst = unique_path(dst)
                shutil.move(src, dst)
                moved += 1
                if moved % 200 == 0 or moved == total_files:
                    progress_cb(moved, total_files)

    log_cb(
        f"按份数拆分完成：total_files={total_files} | total_parts={n} | "
        f"non_empty_parts={len(non_empty_parts)} | output={output_dir}"
    )
    return moved, n, len(non_empty_parts)


# =========================
# Preview + Merge models
# =========================
@dataclass
class PreviewResult:
    root: str
    target_parent: str
    initial_subdir_count: int
    unique_name_count: int
    total_files_before: int
    total_files_expected_after: int
    per_name_files_expected: Dict[str, int]


def run_preview(root: str, target_parent: str, log_cb, progress_cb, stop_flag: threading.Event) -> PreviewResult:
    root = os.path.abspath(root)
    target_parent = os.path.abspath(target_parent)

    log_cb(f"Root: {root}")
    log_cb(f"Target parent: {target_parent}")
    log_cb("开始扫描子目录...")

    all_dirs = walk_all_dirs(root)
    initial_subdir_count = len(all_dirs)
    name_map = build_name_map(root)
    unique_name_count = len(name_map)

    log_cb(f"初始子目录总数（递归）: {initial_subdir_count}")
    log_cb(f"唯一子目录名数量（同名合并后数量）: {unique_name_count}")

    # Count total files before and per-name expected
    total_files_before = 0
    per_name_files_expected: Dict[str, int] = {}

    total_steps = max(1, len(all_dirs))
    done_steps = 0
    progress_cb(0, total_steps)

    # NOTE: This counts per directory subtree and will overcount if nested overlap.
    # But your structure (PID directories) likely not deeply nested. We'll keep as-is for preview.
    for d in all_dirs:
        if stop_flag.is_set():
            raise RuntimeError("已取消")
        total_files_before += count_files_recursive(d)
        done_steps += 1
        if done_steps % 30 == 0 or done_steps == total_steps:
            progress_cb(done_steps, total_steps)

    log_cb(f"合并前（估算）：所有子目录文件总数 = {total_files_before}")

    log_cb("统计预计合并后文件数（按同名目录聚合）...")
    group_names = list(name_map.keys())
    total_groups = max(1, len(group_names))
    done_groups = 0
    progress_cb(0, total_groups)

    for name in group_names:
        if stop_flag.is_set():
            raise RuntimeError("已取消")
        cnt = 0
        for src_dir in name_map[name]:
            cnt += count_files_recursive(src_dir)
        per_name_files_expected[name] = cnt
        done_groups += 1
        if done_groups % 20 == 0 or done_groups == total_groups:
            progress_cb(done_groups, total_groups)

    total_files_expected_after = sum(per_name_files_expected.values())
    log_cb(f"预计合并后（估算）：所有子目录文件总数 = {total_files_expected_after}")
    log_cb("预演完成")

    return PreviewResult(
        root=root,
        target_parent=target_parent,
        initial_subdir_count=initial_subdir_count,
        unique_name_count=unique_name_count,
        total_files_before=total_files_before,
        total_files_expected_after=total_files_expected_after,
        per_name_files_expected=per_name_files_expected
    )


def run_merge(
    root: str,
    target_parent: str,
    delete_sources: bool,
    log_cb,
    progress_cb,
    stop_flag: threading.Event
):
    root = os.path.abspath(root)
    target_parent = os.path.abspath(target_parent)

    safe_mkdir(target_parent)

    name_map = build_name_map(root)
    all_names = sorted(name_map.keys())
    total_names = max(1, len(all_names))
    done_names = 0

    log_cb("开始执行合并（Apply）...")
    log_cb(f"合并输出目录: {target_parent}")
    log_cb(f"合并后删除源目录空壳: {delete_sources}")

    progress_cb(0, total_names)

    moved_files = 0
    renamed_due_to_conflict = 0
    dedup_deleted = 0

    for name in all_names:
        if stop_flag.is_set():
            raise RuntimeError("已取消")

        dir_list = name_map[name]
        if not dir_list:
            done_names += 1
            progress_cb(done_names, total_names)
            continue

        target_dir = os.path.join(target_parent, name)
        safe_mkdir(target_dir)

        for src_dir in dir_list:
            if stop_flag.is_set():
                raise RuntimeError("已取消")

            src_real = os.path.realpath(src_dir)
            tgt_real = os.path.realpath(target_dir)
            if src_real == tgt_real or src_real.startswith(tgt_real + os.sep):
                continue

            for src_file in list(iter_files_recursive(src_dir)):
                if stop_flag.is_set():
                    raise RuntimeError("已取消")

                _src, _dst, action = move_file_preserve_rel_dedupe(src_file, src_dir, target_dir)
                if action == "moved":
                    moved_files += 1
                elif action == "renamed_moved":
                    moved_files += 1
                    renamed_due_to_conflict += 1
                elif action == "dedup_deleted_src":
                    dedup_deleted += 1

            if delete_sources:
                delete_dir_if_empty_tree(src_dir)

        done_names += 1
        if done_names % 5 == 0 or done_names == total_names:
            log_cb(
                f"进度：{done_names}/{total_names} 组完成；已移动 {moved_files}；"
                f"改名 {renamed_due_to_conflict}；去重删除 {dedup_deleted}"
            )
        progress_cb(done_names, total_names)

    log_cb("合并完成")
    log_cb(f"总移动文件数: {moved_files}")
    log_cb(f"改名保留（同名但内容不同）次数: {renamed_due_to_conflict}")
    log_cb(f"内容相同去重（删除源重复）次数: {dedup_deleted}")

    # Count actual files after
    log_cb("统计合并后真实文件数...")
    total_after = 0
    for name in all_names:
        merged_dir = os.path.join(target_parent, name)
        if os.path.isdir(merged_dir):
            total_after += count_files_recursive(merged_dir)
    log_cb(f"合并后：所有子目录文件总数 = {total_after}")


# =========================
# PID copy helpers
# =========================
def extract_pid_from_dirname(name: str) -> str:
    name = (name or "").strip()
    m = re.match(r"^(\d+)", name)
    if m:
        return m.group(1)
    digits = re.findall(r"\d+", name)
    return "".join(digits) if digits else ""


# =========================
# GUI
# =========================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("同名子目录合并工具（统计 + 一键合并 + 文件搜索/分拣 + 复制PID）")
        self.geometry("1180x780")
        self.minsize(1020, 680)

        self.log_q = queue.Queue()
        self.worker_thread = None
        self.stop_flag = threading.Event()

        self.preview_result: Optional[PreviewResult] = None
        self.file_scan_result: Optional[FileScanResult] = None
        self.pair_split_preview_result: Optional[PairSplitPreviewResult] = None

        self._sort_key = "count"
        self._sort_desc = True

        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=10)

        # Root
        ttk.Label(top, text="主目录 Root:").grid(row=0, column=0, sticky="w")
        self.root_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.root_var, width=92).grid(row=0, column=1, padx=8, sticky="we")
        ttk.Button(top, text="选择...", command=self.choose_root).grid(row=0, column=2, sticky="e")

        # Target parent
        ttk.Label(top, text="合并输出目录:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.target_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.target_var, width=92).grid(row=1, column=1, padx=8, sticky="we", pady=(8, 0))
        ttk.Button(top, text="默认 .merged", command=self.set_default_target).grid(row=1, column=2, sticky="e", pady=(8, 0))

        # Options
        opt = ttk.Frame(top)
        opt.grid(row=2, column=1, sticky="w", pady=(10, 0))
        self.keep_sources_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="合并后不删除源目录（保留原目录结构）", variable=self.keep_sources_var).pack(side="left")

        # Buttons
        btns = ttk.Frame(top)
        btns.grid(row=3, column=1, sticky="w", pady=(12, 0))
        self.preview_btn = ttk.Button(btns, text="预演统计（Dry-Run）", command=self.on_preview)
        self.preview_btn.pack(side="left")

        self.merge_btn = ttk.Button(btns, text="一键合并（Apply）", command=self.on_merge, state="disabled")
        self.merge_btn.pack(side="left", padx=10)

        self.cancel_btn = ttk.Button(btns, text="取消当前任务", command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="left")

        # Progress
        prog = ttk.Frame(self)
        prog.pack(fill="x", padx=12)
        self.progress = ttk.Progressbar(prog, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", pady=(4, 10))
        self.progress_label = ttk.Label(prog, text="就绪")
        self.progress_label.pack(anchor="w")

        # Middle split
        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=12, pady=6)

        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)

        right = ttk.LabelFrame(mid, text="日志")
        right.pack(side="right", fill="both", expand=False, padx=(10, 0))

        self.log_text = tk.Text(right, width=48, height=30, wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_text.configure(state="disabled")

        # ===== Summary for merge preview =====
        summary = ttk.LabelFrame(left, text="合并统计汇总（预演/合并后）")
        summary.pack(fill="x", pady=(0, 10))

        self.sum_initial_dirs = tk.StringVar(value="-")
        self.sum_unique_names = tk.StringVar(value="-")
        self.sum_files_before = tk.StringVar(value="-")
        self.sum_files_after = tk.StringVar(value="-")

        r = 0
        ttk.Label(summary, text="初始子目录总数（递归）:").grid(row=r, column=0, sticky="w", padx=10, pady=6)
        ttk.Label(summary, textvariable=self.sum_initial_dirs, anchor="center", justify="center") \
            .grid(row=r, column=1, sticky="ew", padx=10, pady=6)
        r += 1
        ttk.Label(summary, text="同名合并后保留的子目录数量（唯一目录名）:").grid(row=r, column=0, sticky="w", padx=10, pady=6)
        ttk.Label(summary, textvariable=self.sum_unique_names, anchor="center", justify="center") \
            .grid(row=r, column=1, sticky="ew", padx=10, pady=6)
        r += 1
        ttk.Label(summary, text="合并前：所有子目录文件总数（估算）:").grid(row=r, column=0, sticky="w", padx=10, pady=6)
        ttk.Label(summary, textvariable=self.sum_files_before, anchor="center", justify="center") \
            .grid(row=r, column=1, sticky="ew", padx=10, pady=6)
        r += 1
        ttk.Label(summary, text="预计/合并后：所有子目录文件总数（估算）:").grid(row=r, column=0, sticky="w", padx=10, pady=6)
        ttk.Label(summary, textvariable=self.sum_files_after, anchor="center", justify="center") \
            .grid(row=r, column=1, sticky="ew", padx=10, pady=6)

        summary.columnconfigure(1, weight=1)

        # ===== File scan panel =====
        scan_panel = ttk.LabelFrame(left, text="文件统计与关键字搜索（只统计子目录内文件，不含 root 直接文件）")
        scan_panel.pack(fill="x", pady=(0, 10))

        ttk.Label(scan_panel, text="扫描目录:").grid(row=0, column=0, sticky="w", padx=10, pady=6)
        self.scan_root_var = tk.StringVar()
        ttk.Entry(scan_panel, textvariable=self.scan_root_var, width=78).grid(row=0, column=1, padx=8, sticky="we", pady=6)
        ttk.Button(scan_panel, text="使用 Root", command=self.use_root_as_scan).grid(row=0, column=2, sticky="e", padx=(0, 8))
        ttk.Button(scan_panel, text="选择...", command=self.choose_scan_root).grid(row=0, column=3, sticky="e", padx=(0, 8))

        ttk.Label(scan_panel, text="文件名关键字:").grid(row=1, column=0, sticky="w", padx=10, pady=6)
        self.keyword_var = tk.StringVar()
        ttk.Entry(scan_panel, textvariable=self.keyword_var, width=30).grid(row=1, column=1, padx=8, sticky="w", pady=6)

        self.case_insensitive_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(scan_panel, text="不区分大小写", variable=self.case_insensitive_var).grid(row=1, column=1, sticky="e", padx=(0, 10))

        self.scan_btn = ttk.Button(scan_panel, text="扫描统计", command=self.on_scan_files)
        self.scan_btn.grid(row=1, column=2, sticky="e", padx=(0, 8), pady=6)

        # split output controls
        ttk.Label(scan_panel, text="分拣输出目录:").grid(row=2, column=0, sticky="w", padx=10, pady=6)
        self.split_out_var = tk.StringVar()
        ttk.Entry(scan_panel, textvariable=self.split_out_var, width=78).grid(row=2, column=1, padx=8, sticky="we", pady=6)

        ttk.Button(scan_panel, text="默认 .scan_split", command=self.set_default_split_out).grid(row=2, column=2, sticky="e", padx=(0, 8))
        ttk.Button(scan_panel, text="选择...", command=self.choose_split_out).grid(row=2, column=3, sticky="e", padx=(0, 8))

        self.move_split_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(scan_panel, text="移动文件（默认复制更安全）", variable=self.move_split_var).grid(
            row=3, column=1, sticky="w", padx=8, pady=6
        )

        self.split_btn = ttk.Button(
            scan_panel, text="分拣输出（Matched / NotMatched）", command=self.on_split_files, state="disabled"
        )
        self.split_btn.grid(row=3, column=2, sticky="e", padx=(0, 8), pady=6)
        ttk.Label(scan_panel, text="按数量拆分（每份文件数）:").grid(row=4, column=0, sticky="w", padx=10, pady=6)
        self.custom_split_count_var = tk.StringVar(value="100")
        ttk.Entry(scan_panel, textvariable=self.custom_split_count_var, width=12).grid(
            row=4, column=1, sticky="w", padx=(8, 8), pady=6
        )
        self.custom_split_btn = ttk.Button(
            scan_panel,
            text="按数量剪切拆分",
            command=self.on_custom_split_files,
        )
        self.custom_split_btn.grid(row=4, column=2, sticky="e", padx=(0, 8), pady=6)

        ttk.Label(scan_panel, text="按份数拆分（总份数）:").grid(row=5, column=0, sticky="w", padx=10, pady=6)
        self.custom_split_parts_var = tk.StringVar(value="5")
        ttk.Entry(scan_panel, textvariable=self.custom_split_parts_var, width=12).grid(
            row=5, column=1, sticky="w", padx=(8, 8), pady=6
        )
        self.custom_split_parts_btn = ttk.Button(
            scan_panel,
            text="按份数剪切拆分",
            command=self.on_custom_split_parts_files,
        )
        self.custom_split_parts_btn.grid(row=5, column=2, sticky="e", padx=(0, 8), pady=6)

        # scan result labels (center)
        self.scan_total_files = tk.StringVar(value="-")
        self.scan_matched = tk.StringVar(value="-")
        self.scan_not_matched = tk.StringVar(value="-")

        ttk.Label(scan_panel, text="子目录内文件总数:").grid(row=6, column=0, sticky="w", padx=10, pady=6)
        ttk.Label(scan_panel, textvariable=self.scan_total_files, anchor="center", justify="center") \
            .grid(row=6, column=1, sticky="ew", padx=8, pady=6)

        ttk.Label(scan_panel, text="包含关键字的文件数:").grid(row=7, column=0, sticky="w", padx=10, pady=6)
        ttk.Label(scan_panel, textvariable=self.scan_matched, anchor="center", justify="center") \
            .grid(row=7, column=1, sticky="ew", padx=8, pady=6)

        ttk.Label(scan_panel, text="不包含关键字的文件数:").grid(row=8, column=0, sticky="w", padx=10, pady=6)
        ttk.Label(scan_panel, textvariable=self.scan_not_matched, anchor="center", justify="center") \
            .grid(row=8, column=1, sticky="ew", padx=8, pady=6)

        ttk.Separator(scan_panel, orient="horizontal").grid(row=9, column=0, columnspan=4, sticky="ew", padx=8, pady=(8, 6))

        ttk.Label(scan_panel, text="图文拆分工具:").grid(row=10, column=0, sticky="w", padx=10, pady=6)
        ttk.Label(
            scan_panel,
            text="遍历所有子目录，按 1图+1txt 拆分；支持 多图单txt / 单图多txt / 图数=txt数",
        ).grid(row=10, column=1, columnspan=3, sticky="w", padx=8, pady=6)

        self.pair_split_preview_btn = ttk.Button(scan_panel, text="预演图文拆分", command=self.on_pair_split_preview)
        self.pair_split_preview_btn.grid(row=11, column=2, sticky="e", padx=(0, 8), pady=6)

        self.pair_split_apply_btn = ttk.Button(
            scan_panel,
            text="执行图文拆分",
            command=self.on_pair_split_apply,
            state="disabled",
        )
        self.pair_split_apply_btn.grid(row=11, column=3, sticky="e", padx=(0, 8), pady=6)

        scan_panel.columnconfigure(1, weight=1)

        # ===== Table: per name file counts =====
        table_frame = ttk.LabelFrame(left, text="每个唯一目录名的文件数（预演统计）")
        table_frame.pack(fill="both", expand=True)

        search_bar = ttk.Frame(table_frame)
        search_bar.pack(fill="x", padx=8, pady=6)

        ttk.Label(search_bar, text="搜索目录名:").pack(side="left")
        self.search_var = tk.StringVar()
        ttk.Entry(search_bar, textvariable=self.search_var, width=40).pack(side="left", padx=8)
        ttk.Button(search_bar, text="筛选", command=self.refresh_table).pack(side="left")
        ttk.Button(search_bar, text="清空", command=self.clear_search).pack(side="left", padx=6)
        ttk.Button(search_bar, text="复制PID", command=self.copy_selected_pids).pack(side="left", padx=6)

        # 璁?table_frame 鐢?grid 绠＄悊鍐呴儴缁勪欢锛圱reeview + Scrollbar锛?
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(1, weight=1)

        # search_bar 浠嶇劧 pack 涔熻锛屼絾鏇寸ǔ鏄?grid锛涜繖閲屼繚鎸佷綘鐨勭粨鏋勪篃鍙互
        # 濡傛灉浣犳効鎰忎篃鍙互鎶?search_bar 鏀规垚 grid

        # Treeview + Scrollbar 鐢?grid
        tree_container = ttk.Frame(table_frame)
        tree_container.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        tree_container.columnconfigure(0, weight=1)
        tree_container.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            tree_container,
            columns=("name", "count"),
            show="headings",
            height=16,
            selectmode="extended"
        )

        # 琛ㄥご灞呬腑锛堜綘涔嬪墠瑕佸眳涓樉绀猴級
        self.tree.heading("name", text="目录名（basename）", anchor="center", command=lambda: self.sort_table("name"))
        self.tree.heading("count", text="文件数", anchor="center", command=lambda: self.sort_table("count"))

        # 关键：固定 count 列最小宽度，防止被压缩到 0
        self.tree.column("name", width=680, minwidth=300, anchor="w", stretch=True)
        self.tree.column("count", width=120, minwidth=80, anchor="center", stretch=False)

        self.tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns")

        top.columnconfigure(1, weight=1)

    # ---------- Root/Target actions ----------
    def choose_root(self):
        folder = filedialog.askdirectory(title="选择主目录 Root")
        if folder:
            self.root_var.set(folder)
            if not self.target_var.get().strip():
                self.set_default_target()
            if not self.scan_root_var.get().strip():
                self.scan_root_var.set(folder)
            if not self.split_out_var.get().strip():
                self.split_out_var.set(os.path.join(folder, ".scan_split"))
            self._log(f"已选择 Root: {folder}")

    def set_default_target(self):
        root = self.root_var.get().strip()
        if not root:
            messagebox.showwarning("提示", "请先选择 Root 目录")
            return
        self.target_var.set(os.path.join(root, ".merged"))

    def use_root_as_scan(self):
        root = self.root_var.get().strip()
        if not root:
            messagebox.showwarning("提示", "请先选择 Root 目录")
            return
        self.scan_root_var.set(root)

    def choose_scan_root(self):
        folder = filedialog.askdirectory(title="选择要扫描统计的目录")
        if folder:
            self.scan_root_var.set(folder)
            if not self.split_out_var.get().strip():
                self.split_out_var.set(os.path.join(folder, ".scan_split"))
            self._log(f"已选择扫描目录: {folder}")

    def set_default_split_out(self):
        base = (self.scan_root_var.get().strip() or self.root_var.get().strip())
        if not base:
            messagebox.showwarning("提示", "请先选择扫描目录或 Root")
            return
        self.split_out_var.set(os.path.join(base, ".scan_split"))

    def choose_split_out(self):
        folder = filedialog.askdirectory(title="选择分拣输出目录")
        if folder:
            self.split_out_var.set(folder)
            self._log(f"已选择分拣输出目录: {folder}")

    # ---------- table ----------
    def clear_search(self):
        self.search_var.set("")
        self.refresh_table()

    def sort_table(self, key: str):
        if self._sort_key == key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key = key
            self._sort_desc = True
        self.refresh_table()

    def refresh_table(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        if not self.preview_result:
            return

        q = self.search_var.get().strip().lower()
        items = list(self.preview_result.per_name_files_expected.items())

        if q:
            items = [(n, c) for (n, c) in items if q in n.lower()]

        if self._sort_key == "name":
            items.sort(key=lambda x: x[0], reverse=self._sort_desc)
        else:
            items.sort(key=lambda x: x[1], reverse=self._sort_desc)

        for name, cnt in items:
            self.tree.insert("", "end", values=(name, cnt))

    def copy_selected_pids(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先在表格中选择一个或多个目录名。")
            return

        pids = []
        for item_id in selected:
            vals = self.tree.item(item_id, "values")
            if not vals:
                continue
            dirname = str(vals[0])
            pid = extract_pid_from_dirname(dirname)
            if pid:
                pids.append(pid)

        if not pids:
            messagebox.showwarning("提示", "选中的目录名里没有检测到 PID（数字）。")
            return

        text = "\n".join(pids)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self._log(f"已复制 PID {len(pids)} 个到剪贴板")
        messagebox.showinfo("完成", f"已复制 {len(pids)} 个 PID 到剪贴板（每行一个）。")

    # ---------- Buttons ----------
    def on_preview(self):
        root = self.root_var.get().strip()
        target = self.target_var.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showerror("错误", "Root 目录无效，请重新选择。")
            return
        if not target:
            self.target_var.set(os.path.join(root, ".merged"))
            target = self.target_var.get().strip()
        self._start_worker(task="preview")

    def on_merge(self):
        if not self.preview_result:
            messagebox.showwarning("提示", "请先进行预演统计（Dry-Run）。")
            return

        root = self.root_var.get().strip()
        target = self.target_var.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showerror("错误", "Root 目录无效，请重新选择。")
            return
        if not target:
            messagebox.showerror("错误", "合并输出目录为空。")
            return

        keep_sources = self.keep_sources_var.get()
        delete_sources = not keep_sources

        msg = (
            "你即将开始执行【一键合并】（会移动文件）。\n\n"
            f"Root:\n{os.path.abspath(root)}\n\n"
            f"输出目录:\n{os.path.abspath(target)}\n\n"
            f"合并后删除源目录空壳: {delete_sources}\n\n"
            "确认继续？"
        )
        if not messagebox.askyesno("确认执行", msg):
            return

        if os.path.abspath(target) == os.path.abspath(root):
            if not messagebox.askyesno("风险提示", "输出目录与 Root 相同，建议使用 Root/.merged。仍要继续吗？"):
                return

        self._start_worker(task="merge")

    def on_scan_files(self):
        scan_root = self.scan_root_var.get().strip()
        if not scan_root or not os.path.isdir(scan_root):
            messagebox.showerror("错误", "扫描目录无效，请重新选择。")
            return
        self._start_worker(task="scan")

    def on_split_files(self):
        scan_root = self.scan_root_var.get().strip()
        keyword = self.keyword_var.get().strip()
        if not scan_root or not os.path.isdir(scan_root):
            messagebox.showerror("错误", "扫描目录无效，请重新选择。")
            return
        if not keyword:
            messagebox.showwarning("提示", "请输入关键字后再分拣。")
            return

        out_dir = self.split_out_var.get().strip()
        if not out_dir:
            out_dir = os.path.join(scan_root, ".scan_split")
            self.split_out_var.set(out_dir)

        msg = (
            "你即将执行【分拣输出】。\n\n"
            f"扫描目录:\n{os.path.abspath(scan_root)}\n\n"
            f"输出目录:\n{os.path.abspath(out_dir)}\n\n"
            f"模式：{'移动源文件' if self.move_split_var.get() else '复制源文件（推荐）'}\n\n"
            "确认继续？"
        )
        if not messagebox.askyesno("确认执行", msg):
            return

        self._start_worker(task="split")

    def on_custom_split_files(self):
        scan_root = self.scan_root_var.get().strip()
        if not scan_root or not os.path.isdir(scan_root):
            messagebox.showerror("错误", "扫描目录无效，请重新选择。")
            return

        raw_n = (self.custom_split_count_var.get() or "").strip()
        try:
            n = int(raw_n)
        except Exception:
            messagebox.showerror("错误", "请输入有效的整数文件数。")
            return
        if n <= 0:
            messagebox.showerror("错误", "每份文件数必须大于 0。")
            return

        out_dir = self.split_out_var.get().strip()
        if not out_dir:
            out_dir = os.path.join(scan_root, ".scan_split")
            self.split_out_var.set(out_dir)

        msg = (
            "你即将执行【按数量剪切拆分】。\n\n"
            f"扫描目录:\n{os.path.abspath(scan_root)}\n\n"
            f"输出目录:\n{os.path.abspath(out_dir)}\n\n"
            f"每份文件数: {n}\n\n"
            "说明：会把 root 子目录内文件剪切到 output/batch_xxx 下，并保留原相对目录结构。\n\n"
            "确认继续？"
        )
        if not messagebox.askyesno("确认执行", msg):
            return

        self._start_worker(task="split_custom")

    def on_custom_split_parts_files(self):
        scan_root = self.scan_root_var.get().strip()
        if not scan_root or not os.path.isdir(scan_root):
            messagebox.showerror("错误", "扫描目录无效，请重新选择。")
            return

        raw_n = (self.custom_split_parts_var.get() or "").strip()
        try:
            n = int(raw_n)
        except Exception:
            messagebox.showerror("错误", "请输入有效的整数份数。")
            return
        if n <= 0:
            messagebox.showerror("错误", "总份数必须大于 0。")
            return

        out_dir = self.split_out_var.get().strip()
        if not out_dir:
            out_dir = os.path.join(scan_root, ".scan_split")
            self.split_out_var.set(out_dir)

        msg = (
            "你即将执行【按份数剪切拆分】。\n\n"
            f"扫描目录:\n{os.path.abspath(scan_root)}\n\n"
            f"输出目录:\n{os.path.abspath(out_dir)}\n\n"
            f"总份数: {n}\n\n"
            "说明：每个子目录会先平均分成 N 份，再将各子目录第 i 份组合为 part_i，并保留原相对目录结构。\n\n"
            "确认继续？"
        )
        if not messagebox.askyesno("确认执行", msg):
            return

        self._start_worker(task="split_parts")

    def on_pair_split_preview(self):
        scan_root = self.scan_root_var.get().strip()
        if not scan_root or not os.path.isdir(scan_root):
            messagebox.showerror("错误", "扫描目录无效，请重新选择。")
            return
        self._start_worker(task="pair_split_preview")

    def on_pair_split_apply(self):
        scan_root = self.scan_root_var.get().strip()
        if not scan_root or not os.path.isdir(scan_root):
            messagebox.showerror("错误", "扫描目录无效，请重新选择。")
            return

        msg = (
            "你即将执行【图文拆分】。\n\n"
            f"扫描目录:\n{os.path.abspath(scan_root)}\n\n"
            "规则：\n"
            "1. 多图 + 单txt => 按图片数拆分，txt 复制到每份\n"
            "2. 单图 + 多txt => 按txt数拆分，图片复制到每份\n"
            "3. 图数 = txt数 => 按排序后一一配对拆分\n\n"
            "说明：会在原目录同级生成 原目录_1 / 原目录_2 ...，并清理已处理完的原目录空壳。\n\n"
            "确认继续？"
        )
        if not messagebox.askyesno("确认执行", msg):
            return

        self._start_worker(task="pair_split_apply")

    def on_cancel(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.stop_flag.set()
            self._log("已请求取消任务（将尽快停止）")
            self.cancel_btn.config(state="disabled")

    # ---------- Worker orchestration ----------
    def _start_worker(self, task: str):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("提示", "已有任务在运行，请先取消或等待完成。")
            return

        self.stop_flag.clear()

        self.preview_btn.config(state="disabled")
        self.merge_btn.config(state="disabled")
        self.scan_btn.config(state="disabled")
        self.split_btn.config(state="disabled")
        self.custom_split_btn.config(state="disabled")
        self.custom_split_parts_btn.config(state="disabled")
        self.pair_split_preview_btn.config(state="disabled")
        self.pair_split_apply_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self._set_progress(0, 1, "开始任务...")

        root = self.root_var.get().strip()
        target = self.target_var.get().strip()
        scan_root = self.scan_root_var.get().strip()
        keyword = self.keyword_var.get().strip()
        case_ins = self.case_insensitive_var.get()

        keep_sources = self.keep_sources_var.get()
        delete_sources = not keep_sources

        def log_cb(msg: str):
            self.log_q.put(("log", msg))

        def progress_cb(done: int, total: int):
            self.log_q.put(("progress", done, total))

        def worker():
            try:
                if task == "preview":
                    res = run_preview(root, target, log_cb, progress_cb, self.stop_flag)
                    self.log_q.put(("preview_done", res))
                elif task == "merge":
                    run_merge(root, target, delete_sources, log_cb, progress_cb, self.stop_flag)
                    self.log_q.put(("merge_done",))
                elif task == "scan":
                    res = scan_files_in_subdirs(scan_root, keyword, case_ins, log_cb, progress_cb, self.stop_flag)
                    self.log_q.put(("scan_done", res))
                elif task == "split":
                    out_dir = self.split_out_var.get().strip()
                    if not out_dir:
                        out_dir = os.path.join(scan_root, ".scan_split")
                    matched_cnt, not_cnt = split_files_by_keyword_to_folders(
                        root=scan_root,
                        keyword=keyword,
                        case_insensitive=case_ins,
                        output_dir=out_dir,
                        move_files=self.move_split_var.get(),
                        log_cb=log_cb,
                        progress_cb=progress_cb,
                        stop_flag=self.stop_flag
                    )
                    self.log_q.put(("split_done", matched_cnt, not_cnt, out_dir))
                elif task == "split_custom":
                    out_dir = self.split_out_var.get().strip()
                    if not out_dir:
                        out_dir = os.path.join(scan_root, ".scan_split")
                    files_per_batch = int((self.custom_split_count_var.get() or "0").strip())
                    moved_cnt, batch_cnt = split_files_by_count_to_batches(
                        root=scan_root,
                        output_dir=out_dir,
                        files_per_batch=files_per_batch,
                        log_cb=log_cb,
                        progress_cb=progress_cb,
                        stop_flag=self.stop_flag,
                    )
                    self.log_q.put(("split_custom_done", moved_cnt, batch_cnt, out_dir, files_per_batch))
                elif task == "split_parts":
                    out_dir = self.split_out_var.get().strip()
                    if not out_dir:
                        out_dir = os.path.join(scan_root, ".scan_split")
                    total_parts = int((self.custom_split_parts_var.get() or "0").strip())
                    moved_cnt, part_cnt, non_empty_cnt = split_files_by_parts_across_subdirs(
                        root=scan_root,
                        output_dir=out_dir,
                        total_parts=total_parts,
                        log_cb=log_cb,
                        progress_cb=progress_cb,
                        stop_flag=self.stop_flag,
                    )
                    self.log_q.put(("split_parts_done", moved_cnt, part_cnt, non_empty_cnt, out_dir))
                elif task == "pair_split_preview":
                    res = preview_pair_split_subdirs(scan_root, log_cb, progress_cb, self.stop_flag)
                    self.log_q.put(("pair_split_preview_done", res))
                elif task == "pair_split_apply":
                    done_dirs, created_dirs, skipped_unsupported = run_pair_split_subdirs(
                        scan_root, log_cb, progress_cb, self.stop_flag
                    )
                    self.log_q.put(("pair_split_apply_done", done_dirs, created_dirs, skipped_unsupported, scan_root))
                else:
                    raise ValueError("Unknown task")
            except Exception as e:
                self.log_q.put(("error", str(e)))
            finally:
                self.log_q.put(("worker_finished",))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def _poll_log_queue(self):
        try:
            while True:
                item = self.log_q.get_nowait()
                kind = item[0]

                if kind == "log":
                    self._log(item[1])

                elif kind == "progress":
                    done, total = item[1], item[2]
                    self._set_progress(done, total, f"进度：{done}/{total}")

                elif kind == "preview_done":
                    res: PreviewResult = item[1]
                    self.preview_result = res
                    self._apply_preview_to_ui(res)
                    self._log("预演统计已更新到界面。确认无误后可执行一键合并。")
                    self.merge_btn.config(state="normal")

                elif kind == "scan_done":
                    res: FileScanResult = item[1]
                    self.file_scan_result = res
                    self._apply_scan_to_ui(res)
                    if self.keyword_var.get().strip():
                        self.split_btn.config(state="normal")
                    messagebox.showinfo("扫描完成", "文件统计与关键字搜索已完成。")

                elif kind == "split_done":
                    matched_cnt, not_cnt, out_dir = item[1], item[2], item[3]
                    self._log(f"分拣完成：Matched={matched_cnt} | NotMatched={not_cnt}")
                    messagebox.showinfo("完成", f"分拣完成\n输出目录：{out_dir}\nMatched={matched_cnt}\nNotMatched={not_cnt}")
                elif kind == "split_custom_done":
                    moved_cnt, batch_cnt, out_dir, n = item[1], item[2], item[3], item[4]
                    self._log(f"按数量拆分完成：moved={moved_cnt} | batches={batch_cnt} | files_per_batch={n}")
                    messagebox.showinfo(
                        "完成",
                        f"按数量剪切拆分完成 ✅\n输出目录：{out_dir}\n每份文件数：{n}\n总文件数：{moved_cnt}\n批次数：{batch_cnt}",
                    )
                elif kind == "split_parts_done":
                    moved_cnt, part_cnt, non_empty_cnt, out_dir = item[1], item[2], item[3], item[4]
                    self._log(
                        f"按份数拆分完成：moved={moved_cnt} | total_parts={part_cnt} | "
                        f"non_empty_parts={non_empty_cnt}"
                    )
                    messagebox.showinfo(
                        "完成",
                        f"按份数剪切拆分完成 ✅\n输出目录：{out_dir}\n总份数：{part_cnt}\n"
                        f"非空份数：{non_empty_cnt}\n总文件数：{moved_cnt}",
                    )
                elif kind == "pair_split_preview_done":
                    res: PairSplitPreviewResult = item[1]
                    self.pair_split_preview_result = res
                    if res.planned_dirs > 0:
                        self.pair_split_apply_btn.config(state="normal")
                    self._log(
                        f"图文拆分预演完成：可拆分目录={res.planned_dirs} | "
                        f"将创建子目录={res.planned_new_dirs} | 不支持={res.skipped_unsupported}"
                    )
                    messagebox.showinfo(
                        "预演完成",
                        f"图文拆分预演完成 ✅\n扫描目录数：{res.scanned_dirs}\n"
                        f"可拆分目录：{res.planned_dirs}\n将创建子目录：{res.planned_new_dirs}\n"
                        f"暂不支持目录：{res.skipped_unsupported}",
                    )
                elif kind == "pair_split_apply_done":
                    done_dirs, created_dirs, skipped_unsupported, scan_root = item[1], item[2], item[3], item[4]
                    self._log(
                        f"图文拆分执行完成：已拆分目录={done_dirs} | 新建目录={created_dirs} | "
                        f"跳过不支持={skipped_unsupported}"
                    )
                    messagebox.showinfo(
                        "完成",
                        f"图文拆分完成 ✅\n扫描目录：{scan_root}\n已拆分目录：{done_dirs}\n"
                        f"新建目录：{created_dirs}\n跳过不支持：{skipped_unsupported}",
                    )
                elif kind == "merge_done":
                    self._log("合并任务完成。")
                    messagebox.showinfo("完成", "合并已完成。\n请到输出目录查看结果。")

                elif kind == "error":
                    self._log(f"错误：{item[1]}")
                    messagebox.showerror("错误", item[1])

                elif kind == "worker_finished":
                    self.preview_btn.config(state="normal")
                    self.scan_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")

                    if self.preview_result:
                        self.merge_btn.config(state="normal")
                    else:
                        self.merge_btn.config(state="disabled")

                    if self.file_scan_result and self.keyword_var.get().strip():
                        self.split_btn.config(state="normal")
                    else:
                        self.split_btn.config(state="disabled")
                    self.custom_split_btn.config(state="normal")
                    self.custom_split_parts_btn.config(state="normal")
                    self.pair_split_preview_btn.config(state="normal")
                    if self.pair_split_preview_result and self.pair_split_preview_result.planned_dirs > 0:
                        self.pair_split_apply_btn.config(state="normal")
                    else:
                        self.pair_split_apply_btn.config(state="disabled")

                    self._set_progress(0, 1, "灏辩华")

        except queue.Empty:
            pass

        self.after(120, self._poll_log_queue)

    # ---------- UI updates ----------
    def _apply_preview_to_ui(self, res: PreviewResult):
        self.sum_initial_dirs.set(str(res.initial_subdir_count))
        self.sum_unique_names.set(str(res.unique_name_count))
        self.sum_files_before.set(str(res.total_files_before))
        self.sum_files_after.set(str(res.total_files_expected_after))
        self.refresh_table()

    def _apply_scan_to_ui(self, res: FileScanResult):
        self.scan_total_files.set(str(res.total_files_in_subdirs))
        self.scan_matched.set(str(res.matched))
        self.scan_not_matched.set(str(res.not_matched))

    def _set_progress(self, done: int, total: int, text: str):
        total = max(1, total)
        done = max(0, min(done, total))
        self.progress.configure(maximum=total, value=done)
        self.progress_label.config(text=text)

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


if __name__ == "__main__":
    app = App()
    app.mainloop()
