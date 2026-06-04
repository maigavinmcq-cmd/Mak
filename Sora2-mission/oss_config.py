# -*- coding: utf-8 -*-
"""
OSS config for Jimmy provider fallback image upload.
Fill these values or set equivalent environment variables.
"""
from dotenv import load_dotenv
import os

load_dotenv()  # 自动读取项目根目录的 .env 文件
# Switch
OSS_ENABLED = os.getenv("OSS_ENABLED", "0").strip() in ("1", "true", "True", "YES", "yes")

# Credentials
OSS_ACCESS_KEY_ID = os.getenv("OSS_ACCESS_KEY_ID", "").strip()
OSS_ACCESS_KEY_SECRET = os.getenv("OSS_ACCESS_KEY_SECRET", "").strip()
OSS_BUCKET_NAME = os.getenv("OSS_BUCKET_NAME", "").strip()
OSS_ENDPOINT = os.getenv("OSS_ENDPOINT", "").strip()  # e.g. oss-cn-hangzhou.aliyuncs.com

# Signed URL expiration in seconds
OSS_SIGN_EXPIRE = int(os.getenv("OSS_SIGN_EXPIRE", "3600").strip() or "3600")

# Optional object key prefix in bucket
OSS_OBJECT_PREFIX = os.getenv("OSS_OBJECT_PREFIX", "jimmy-images").strip() or "jimmy-images"
