from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import requests

from app.api.upload_api import upload_image_for_public_url
from app.api.provider_registry import IMAGE_PROVIDERS, get_image_provider
from light_image_tool.log_manager import redact_secrets


class LightImageApiAdapter:
    def __init__(self) -> None:
        self._app_config = None
        self._cache_lock = threading.Lock()
        self._url_access_cache: dict[str, bool] = {}
        self._uploaded_url_cache: dict[str, str] = {}

    def provider_options(self) -> list[tuple[str, str]]:
        return [(key, provider.provider_name) for key, provider in IMAGE_PROVIDERS.items()]

    def model_options(self, provider_key: str) -> list[tuple[str, str]]:
        provider = get_image_provider(provider_key)
        return [(item["logical_key"], item["display_name"]) for item in provider.get_model_options()]

    def default_provider_model(self) -> tuple[str | None, str | None]:
        config = self._load_app_config_safely()
        if config is not None:
            provider = str(getattr(config, "image_provider", "") or "")
            model = str(getattr(config, "image_model_logical_key", "") or "")
            if provider == "hellobabygo_image" and model in {"", "gpt_image_2"}:
                model = "auto_image"
            return provider, model
        provider = next(iter(IMAGE_PROVIDERS.keys()), None)
        model = self.model_options(provider)[0][0] if provider else None
        return provider, model

    def masked_api_key_status(self, provider_key: str | None = None) -> str:
        api_key = self._resolve_api_key(provider_key)
        if not api_key:
            return "未配置"
        text = api_key.strip()
        if len(text) <= 8:
            return "已配置"
        return f"{text[:3]}****{text[-4:]}"

    def generate_image(
        self,
        input_image_path: str,
        prompt: str,
        provider: str | None = None,
        model: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        provider_key = provider or self.default_provider_model()[0] or ""
        model_key = model or self.default_provider_model()[1] or ""
        image_provider = get_image_provider(provider_key)
        api_key = self._resolve_api_key(provider_key)
        if not api_key:
            return self._failure("图生图 API Key 未配置")
        if not str(prompt or "").strip():
            return self._failure("提示词为空")

        params = self._default_extra_params(provider_key)
        params.update(extra_params or {})
        references, public_urls, error_message = self._prepare_reference_params(input_image_path, params, provider_key)
        request_summary = self._build_request_summary(
            provider_key=provider_key,
            model_key=model_key,
            image_provider=image_provider,
            params=params,
            references=references,
            public_urls=public_urls,
            prompt=prompt,
        )
        if error_message:
            return self._failure(error_message, request_summary)
        params["input_images"] = references
        params["input_image_urls"] = public_urls
        params.setdefault("requires_reference_image", True)
        if (
            str(provider_key) == "hellobabygo_image"
            and bool(params.get("requires_reference_image"))
            and not public_urls
        ):
            return self._failure("HelloBabyGo 参考图必须是公网可访问图片 URL；请检查 Http 路径转换或网盘映射配置", request_summary)
        try:
            result = image_provider.generate_image(
                product_image_path=references[0] if references else input_image_path,
                prompt=prompt,
                model_logical_key=model_key,
                api_key=api_key,
                extra_params=params,
            )
        except Exception as exc:
            return self._failure(f"图生图 API 调用异常：{exc}", request_summary)
        return {
            "success": bool(result.success),
            "image_url": result.image_url,
            "image_bytes": None,
            "image_path": result.image_path,
            "task_id": result.task_id,
            "status": result.status,
            "error_message": result.error_message,
            "raw_response": self._safe_raw(result.raw_response),
            "request_summary": request_summary,
        }

    def _load_app_config_safely(self) -> Any:
        if self._app_config is False:
            return None
        if self._app_config is not None:
            return self._app_config
        try:
            from app.config import apply_api_profile_to_config, load_config

            config = load_config()
            if getattr(config, "image_provider", ""):
                apply_api_profile_to_config(config, "image", config.image_provider)
            self._app_config = config
            return config
        except Exception:
            self._app_config = False
            return None

    def _resolve_api_key(self, provider_key: str | None) -> str:
        config = self._load_app_config_safely()
        if config is None:
            return ""
        provider_key = provider_key or getattr(config, "image_provider", "")
        try:
            from app.config import get_api_profile

            profile = get_api_profile(config, "image", provider_key)
            key = str(profile.get("api_key") or "")
            if key:
                return key
        except Exception:
            pass
        return str(getattr(config, "image_api_key", "") or "")

    def _default_extra_params(self, provider_key: str) -> dict[str, Any]:
        config = self._load_app_config_safely()
        if config is None:
            return {}
        base_url = str(getattr(config, "image_api_base_url", "") or "")
        try:
            from app.config import get_api_profile

            profile = get_api_profile(config, "image", provider_key)
            base_url = str(profile.get("base_url") or base_url)
            extra = profile.get("extra_params") if isinstance(profile.get("extra_params"), dict) else {}
        except Exception:
            extra = {}
        image_size = str(getattr(config, "image_size", "") or "1024x1024")
        if provider_key == "hellobabygo_image":
            image_size = "1024x1024"
        params = {
            "base_url": base_url,
            "size": image_size,
            "image_size": image_size,
            "retry_count": int(getattr(config, "retry_count", 3) or 3),
            "retry_interval_seconds": int(getattr(config, "retry_interval_seconds", 5) or 5),
            "timeout": int(getattr(config, "request_timeout_seconds", 180) or 180),
            "poll_interval_seconds": int(getattr(config, "poll_interval_seconds", 5) or 5),
            "max_poll_count": int(getattr(config, "max_poll_count", 120) or 120),
            "image_upload_api_url": str(getattr(config, "image_upload_api_url", "") or ""),
            "image_upload_api_key": str(getattr(config, "image_upload_api_key", "") or ""),
            "image_upload_file_field": str(getattr(config, "image_upload_file_field", "file") or "file"),
            "reference_url_check_timeout": min(10, int(getattr(config, "request_timeout_seconds", 180) or 180)),
        }
        params.update(extra)
        return params

    def _prepare_reference_params(self, input_image_path: str, params: dict[str, Any], provider_key: str = "") -> tuple[list[str], list[str], str | None]:
        references: list[str] = []
        public_urls: list[str] = []
        needs_accessible_urls = str(provider_key) == "hellobabygo_image"

        def add_reference(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in references:
                references.append(text)

        for value in _as_list(params.get("input_images")):
            add_reference(value)
        add_reference(input_image_path)

        if not references:
            return [], [], "没有可用参考图"

        for value in references:
            if _is_http_url(value):
                if needs_accessible_urls:
                    resolved_url, resolve_error = self._accessible_or_uploaded_reference_url(value, value, params)
                    if resolve_error:
                        return references, public_urls, resolve_error
                    if resolved_url and resolved_url not in public_urls:
                        public_urls.append(resolved_url)
                elif value not in public_urls:
                    public_urls.append(value)
                continue
            exists, exists_error = self._path_exists(value)
            if exists_error:
                return references, public_urls, exists_error
            if not exists:
                return references, public_urls, f"源图片不存在：{value}"
            converted = self._to_public_url(value)
            if needs_accessible_urls:
                resolved_url, resolve_error = self._accessible_or_uploaded_reference_url(value, converted, params)
                if resolve_error:
                    return references, public_urls, resolve_error
                if resolved_url and resolved_url not in public_urls:
                    public_urls.append(resolved_url)
            elif converted and converted not in public_urls:
                public_urls.append(converted)

        for value in _as_list(params.get("input_image_urls")):
            converted = self._to_public_url(str(value or "").strip())
            if needs_accessible_urls and converted and not self._url_is_accessible(converted, self._reference_url_check_timeout(params)):
                return references, public_urls, f"参考图公网 URL 不可访问：{converted}"
            if converted and converted not in public_urls:
                public_urls.append(converted)

        return references, public_urls, None

    def _accessible_or_uploaded_reference_url(self, source_path: str, candidate_url: str, params: dict[str, Any]) -> tuple[str, str | None]:
        timeout = self._reference_url_check_timeout(params)
        if candidate_url and self._url_is_accessible(candidate_url, timeout):
            return candidate_url, None
        if _is_http_url(source_path):
            return "", f"参考图公网 URL 不可访问：{candidate_url or source_path}；无法上传非本地参考图"
        uploaded_url, upload_error = self._upload_reference_image(source_path, params)
        if upload_error:
            prefix = f"参考图公网 URL 不可访问：{candidate_url}" if candidate_url else "参考图未能转换为可访问公网 URL"
            return "", f"{prefix}；{upload_error}"
        if not self._url_is_accessible(uploaded_url, timeout):
            return "", f"上传后的参考图 URL 仍不可访问：{uploaded_url}"
        return uploaded_url, None

    def _upload_reference_image(self, source_path: str, params: dict[str, Any]) -> tuple[str, str | None]:
        upload_api_url = str(params.get("image_upload_api_url") or "").strip()
        if not upload_api_url:
            return "", "未配置图片上传接口 IMAGE_UPLOAD_API_URL"
        cache_key = str(source_path)
        with self._cache_lock:
            cached = self._uploaded_url_cache.get(cache_key)
        if cached:
            return cached, None
        upload_url, _raw, error = upload_image_for_public_url(
            image_path=source_path,
            upload_api_url=upload_api_url,
            api_key=str(params.get("image_upload_api_key") or ""),
            file_field=str(params.get("image_upload_file_field") or "file"),
            timeout=int(params.get("timeout") or 120),
        )
        if error:
            return "", error
        if not upload_url or not _is_http_url(upload_url):
            return "", "图片上传接口没有返回公网 URL"
        with self._cache_lock:
            self._uploaded_url_cache[cache_key] = upload_url
        return upload_url, None

    def _url_is_accessible(self, url: str, timeout: int = 10) -> bool:
        text = str(url or "").strip()
        if not _is_http_url(text):
            return False
        with self._cache_lock:
            cached = self._url_access_cache.get(text)
        if cached is not None:
            return cached
        ok = self._probe_image_url(text, timeout)
        if ok:
            with self._cache_lock:
                self._url_access_cache[text] = ok
        return ok

    @staticmethod
    def _probe_image_url(url: str, timeout: int) -> bool:
        headers = {"Accept": "image/*,*/*;q=0.8"}
        try:
            response = requests.head(url, headers=headers, allow_redirects=True, timeout=timeout)
            try:
                if _response_looks_accessible(response):
                    return True
            finally:
                response.close()
        except requests.RequestException:
            pass
        try:
            with requests.get(url, headers=headers, allow_redirects=True, timeout=timeout, stream=True) as response:
                return _response_looks_accessible(response)
        except requests.RequestException:
            return False

    @staticmethod
    def _reference_url_check_timeout(params: dict[str, Any]) -> int:
        try:
            return max(1, int(params.get("reference_url_check_timeout") or 10))
        except (TypeError, ValueError):
            return 10

    @staticmethod
    def _path_exists(value: str) -> tuple[bool, str | None]:
        try:
            return Path(value).exists(), None
        except OSError as exc:
            return False, f"源图片无法访问：{value}；{exc}"

    def _to_public_url(self, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if _is_http_url(text):
            return text
        config = self._load_app_config_safely()
        if config is None:
            return ""
        try:
            from app.file_utils import path_to_http_url

            return path_to_http_url(text, config)
        except Exception:
            return ""

    @staticmethod
    def _failure(message: str, request_summary: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "success": False,
            "image_url": None,
            "image_bytes": None,
            "image_path": None,
            "error_message": message,
            "raw_response": None,
            "request_summary": request_summary,
        }

    @staticmethod
    def _build_request_summary(
        *,
        provider_key: str,
        model_key: str,
        image_provider: Any,
        params: dict[str, Any],
        references: list[str],
        public_urls: list[str],
        prompt: str,
    ) -> dict[str, Any]:
        provider_model = model_key
        try:
            option = image_provider.get_model_option(model_key)
            if isinstance(option, dict):
                provider_model = str(option.get("provider_value") or model_key)
        except Exception:
            provider_model = model_key
        return {
            "provider": provider_key,
            "model": model_key,
            "provider_model": provider_model,
            "response_format": "url",
            "size": str(params.get("size") or params.get("image_size") or ""),
            "reference_count": len(references),
            "url_count": len(public_urls),
            "urls": list(public_urls),
            "prompt_chars": len(str(prompt or "").strip()),
        }

    @staticmethod
    def _safe_raw(raw_response: Any) -> Any:
        if raw_response is None:
            return None
        text = redact_secrets(raw_response)
        return text[:2000] + "..." if len(text) > 2000 else text


def _is_http_url(value: str) -> bool:
    return str(value or "").strip().lower().startswith(("http://", "https://"))


def _response_looks_accessible(response: requests.Response) -> bool:
    if response.status_code < 200 or response.status_code >= 400:
        return False
    content_type = str(response.headers.get("Content-Type") or "").strip().lower()
    if content_type.startswith("text/html"):
        return False
    return True


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]
