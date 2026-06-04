from __future__ import annotations

import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api.image_providers import xibapi_gpt_image2_provider as provider_module


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.ok = 200 <= status_code < 300
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.gets: list[dict] = []
        self.next_post = FakeResponse(
            200,
            {
                "id": "task_1776831820897",
                "object": "image",
                "model": "gpt-image-2",
                "status": "queued",
                "progress": 0,
            },
        )
        self.next_get = FakeResponse(
            200,
            {
                "id": "task_1776831820897",
                "object": "image",
                "model": "gpt-image-2",
                "status": "completed",
                "progress": 100,
                "video_url": "https://example.com/generated-image.png",
            },
        )

    def post(self, url, headers=None, json=None, timeout=None, **kwargs):
        self.posts.append(
            {
                "url": url,
                "headers": headers or {},
                "json": json or {},
                "timeout": timeout,
                "kwargs": kwargs,
            }
        )
        return self.next_post

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.gets.append(
            {
                "url": url,
                "headers": headers or {},
                "timeout": timeout,
                "kwargs": kwargs,
            }
        )
        return self.next_get


def main() -> None:
    provider = provider_module.XibapiGptImage2Provider()
    fake_session = FakeSession()
    original_download = provider_module.download_file
    original_session = provider._session
    original_upload = provider_module.upload_image_for_public_url

    downloaded: list[dict] = []
    uploads: list[dict] = []

    def fake_download(url, output_path, timeout=180, **kwargs):
        downloaded.append(
            {
                "url": url,
                "output_path": str(output_path),
                "timeout": timeout,
                "kwargs": kwargs,
            }
        )
        Path(output_path).write_bytes(b"generated-image")
        return str(output_path)

    try:
        provider_module.download_file = fake_download
        provider_module.upload_image_for_public_url = lambda image_path, upload_api_url, api_key="", file_field="file", timeout=120: (
            uploads.append(
                {
                    "image_path": str(image_path),
                    "upload_api_url": upload_api_url,
                    "api_key": api_key,
                    "file_field": file_field,
                    "timeout": timeout,
                }
            )
            or ("https://oss.example/signed-product.png?sig=1", {"mode": "local_oss"}, None)
        )
        provider._session = lambda: fake_session

        assert getattr(provider, "supports_async_image_tasks", False)

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "product.png"
            source.write_bytes(b"fake-image")
            output = Path(tmp) / "out.png"
            result = provider.submit_image_task(
                product_image_path=str(source),
                prompt="product ad",
                model_logical_key="gpt_image_2",
                api_key="test-key",
                extra_params={
                    "base_url": "https://xibapi.example",
                    "aspect_ratio": "16:9",
                    "input_image_urls": ["https://media.example/source.png"],
                    "output_path": str(output),
                    "timeout": 123,
                },
            )
        assert not result.success
        assert result.task_id == "task_1776831820897"
        assert result.status == "queued"
        assert fake_session.posts[0]["url"] == "https://xibapi.example/v1/videos"
        assert fake_session.posts[0]["headers"]["Authorization"] == "Bearer test-key"
        assert fake_session.posts[0]["headers"]["Content-Type"] == "application/json"
        assert fake_session.posts[0]["json"] == {
            "model": "gpt-image-2",
            "prompt": "product ad",
            "metadata": {
                "aspect_ratio": "16:9",
                "urls": ["https://media.example/source.png"],
            },
        }
        assert "files" not in fake_session.posts[0]["kwargs"]

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "polled.png"
            result = provider.poll_image_task_once(
                "task_1776831820897",
                "test-key",
                extra_params={
                    "base_url": "https://xibapi.example",
                    "output_path": str(output),
                    "timeout": 77,
                },
            )
            assert output.read_bytes() == b"generated-image"
        assert result.success
        assert result.image_url == "https://example.com/generated-image.png"
        assert result.image_path == str(output)
        assert result.task_id == "task_1776831820897"
        assert fake_session.gets[0]["url"] == "https://xibapi.example/v1/videos/task_1776831820897"
        assert downloaded == [
            {
                "url": "https://example.com/generated-image.png",
                "output_path": str(output),
                "timeout": 77,
                "kwargs": {"retry_count": 3, "retry_interval_seconds": 5},
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "product.jpg"
            source.write_bytes(b"local-image")
            result = provider.submit_image_task(
                product_image_path=str(source),
                prompt="local image fallback",
                model_logical_key="gpt_image_2_4k",
                api_key="test-key",
                extra_params={
                    "base_url": "https://xibapi.example",
                    "aspect_ratio": "9:16",
                },
            )
        assert result.task_id == "task_1776831820897"
        fallback_payload = fake_session.posts[-1]["json"]
        assert fallback_payload["model"] == "gpt-image-2-4K"
        assert fallback_payload["metadata"]["aspect_ratio"] == "9:16"
        assert fallback_payload["metadata"]["urls"][0].startswith("data:image/jpeg;base64,")

        uploads.clear()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "product.png"
            source.write_bytes(b"local-for-oss")
            result = provider.submit_image_task(
                product_image_path=str(source),
                prompt="oss fallback reference",
                model_logical_key="gpt_image_2",
                api_key="test-key",
                extra_params={
                    "base_url": "https://xibapi.example",
                    "input_images": [str(source)],
                    "input_image_urls": ["https://media.example:48443/not-public.png"],
                    "image_upload_api_url": "oss://sora2-mission",
                    "image_upload_api_key": "upload-key",
                    "image_upload_file_field": "image",
                    "timeout": 66,
                },
            )
        assert result.task_id == "task_1776831820897"
        assert uploads == [
            {
                "image_path": str(source),
                "upload_api_url": "oss://sora2-mission",
                "api_key": "upload-key",
                "file_field": "image",
                "timeout": 66,
            }
        ]
        assert fake_session.posts[-1]["json"]["metadata"]["urls"] == ["https://oss.example/signed-product.png?sig=1"]
    finally:
        provider_module.download_file = original_download
        provider_module.upload_image_for_public_url = original_upload
        provider._session = original_session

    print("self_test_xibapi_gpt_image2_async_provider: PASS")


if __name__ == "__main__":
    main()
