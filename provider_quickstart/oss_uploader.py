# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import time
from pathlib import Path


def _load_oss_config():
    try:
        from oss_config import (
            OSS_ENABLED,
            OSS_ACCESS_KEY_ID,
            OSS_ACCESS_KEY_SECRET,
            OSS_BUCKET_NAME,
            OSS_ENDPOINT,
            OSS_SIGN_EXPIRE,
            OSS_OBJECT_PREFIX,
        )
    except Exception as e:
        raise RuntimeError(f"oss_config_import_error:{e}")

    return {
        "enabled": bool(OSS_ENABLED),
        "ak": (OSS_ACCESS_KEY_ID or "").strip(),
        "sk": (OSS_ACCESS_KEY_SECRET or "").strip(),
        "bucket": (OSS_BUCKET_NAME or "").strip(),
        "endpoint": (OSS_ENDPOINT or "").strip(),
        "expire": int(OSS_SIGN_EXPIRE or 3600),
        "prefix": (OSS_OBJECT_PREFIX or "jimmy-images").strip().strip("/"),
    }


def _sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _build_object_key(local_file: Path, prefix: str) -> str:
    ts = time.strftime("%Y%m%d")
    digest = _sha1_file(local_file)[:10]
    safe_name = local_file.name.replace("\\", "_").replace("/", "_").replace(" ", "_")
    if prefix:
        return f"{prefix}/{ts}/{digest}_{safe_name}"
    return f"{ts}/{digest}_{safe_name}"


def upload_file_and_sign_url(local_image_path: str) -> str:
    cfg = _load_oss_config()
    if not cfg["enabled"]:
        raise RuntimeError("oss_not_enabled")
    if not (cfg["ak"] and cfg["sk"] and cfg["bucket"] and cfg["endpoint"]):
        raise RuntimeError("oss_missing_config")

    p = Path((local_image_path or "").strip())
    if not p.exists() or not p.is_file():
        raise RuntimeError(f"oss_local_file_not_found:{local_image_path}")

    try:
        import oss2
    except Exception:
        raise RuntimeError("oss2_not_installed")

    auth = oss2.Auth(cfg["ak"], cfg["sk"])
    bucket = oss2.Bucket(auth, cfg["endpoint"], cfg["bucket"])
    object_key = _build_object_key(p, cfg["prefix"])

    bucket.put_object_from_file(object_key, str(p))
    return bucket.sign_url("GET", object_key, int(cfg["expire"]))
