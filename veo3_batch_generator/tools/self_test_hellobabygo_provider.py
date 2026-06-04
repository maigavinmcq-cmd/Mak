from __future__ import annotations

import tempfile
import json
import sys
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.provider_registry import IMAGE_PROVIDERS, VIDEO_PROVIDERS

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)
MP4_LIKE = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + (b"\0" * 1200) + b"mdat" + (b"\0" * 64) + b"moov"


class FakeResponse:
    def __init__(self, status_code: int, body: Any = None, content: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self.content = content
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300
        self.text = str(body)

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 1024):
        del chunk_size
        yield self.content or b"x"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class FakeSession:
    def post(self, url: str, **kwargs) -> FakeResponse:
        return requests.post(url, **kwargs)

    def get(self, url: str, **kwargs) -> FakeResponse:
        return requests.get(url, **kwargs)


def main() -> None:
    image_provider = IMAGE_PROVIDERS["hellobabygo_image"]
    video_provider = VIDEO_PROVIDERS["hellobabygo_veo"]

    original_post = requests.post
    original_get = requests.get
    original_video_session = video_provider._session
    expected_image_urls = [f"https://cdn.example/source-{index}.png" for index in range(1, 11)]

    def fake_post(url: str, **kwargs) -> FakeResponse:
        if url.endswith("/v1/images/generations"):
            assert kwargs.get("params") == {"async": "true"}
            payload = kwargs.get("json") or {}
            assert payload["model"] == "gpt-image-2"
            assert payload["urls"] == expected_image_urls
            return FakeResponse(200, {"id": "img_task_1", "status": "queued"})
        if url.endswith("/v1/videos"):
            payload = kwargs.get("data") if "files" in kwargs else kwargs.get("json")
            assert payload is not None
            assert payload.get("model") == "veo_3_1-fast-portrait-fl-hd"
            if "files" in kwargs:
                # Multipart form text fields are encoded as strings. Live API
                # validation rejects data={"duration": "8"} with
                # Alias.duration type errors, so duration has to be carried in
                # the alias JSON object where it remains an int.
                assert "duration" not in payload, payload
                assert json.loads(payload["alias"]) == {"duration": 8}, payload
                duration_parts = [part for part in kwargs["files"] if part[0] == "duration"]
                assert duration_parts == [], kwargs["files"]
                return FakeResponse(200, {"id": "vid_task_1", "status": "queued"})
            assert isinstance(payload.get("duration"), int), payload
            return FakeResponse(200, {"id": "vid_task_text_1", "status": "queued"})
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url: str, **kwargs) -> FakeResponse:
        del kwargs
        if url.endswith("/v1/images/img_task_1"):
            # The real image endpoint may return the finished data payload
            # without a top-level status field. Presence of data[0].url means
            # the image is available and must stop polling.
            return FakeResponse(200, {"created": 1770000000, "data": [{"url": "https://cdn.example/out.png"}], "usage": {"total_tokens": 1}})
        if url == "https://cdn.example/out.png":
            return FakeResponse(200, {"ignored": True}, content=PNG_1X1)
        if url.endswith("/v1/videos/vid_task_1"):
            return FakeResponse(200, {"id": "vid_task_1", "status": "completed"})
        if url.endswith("/v1/videos/vid_task_1/content"):
            return FakeResponse(200, {"ignored": True}, content=MP4_LIKE)
        raise AssertionError(f"unexpected GET {url}")

    requests.post = fake_post
    requests.get = fake_get
    video_provider._session = lambda: FakeSession()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            image_result = image_provider.generate_image(
                product_image_path="",
                prompt="make image",
                model_logical_key="gpt_image_2",
                api_key="test-key",
                extra_params={
                    "input_image_urls": expected_image_urls,
                    "output_path": "",
                    "poll_interval_seconds": 1,
                    "max_poll_count": 1,
                },
            )
            assert image_result.success, image_result.error_message
            assert image_result.task_id == "img_task_1"
            assert image_result.image_url == "https://cdn.example/out.png"

            local_source = tmp_dir / "local_source.png"
            local_source.write_bytes(PNG_1X1)
            missing_ref_result = image_provider.generate_image(
                product_image_path=str(local_source),
                prompt="must use product image",
                model_logical_key="gpt_image_2",
                api_key="test-key",
                extra_params={
                    "requires_reference_image": True,
                    "input_image_urls": [],
                    "output_path": str(tmp_dir / "should_not_exist.png"),
                    "poll_interval_seconds": 1,
                    "max_poll_count": 1,
                },
            )
            assert not missing_ref_result.success
            assert "public reference image URL" in (missing_ref_result.error_message or "")

            image_file = tmp_dir / "image.png"
            image_file.write_bytes(PNG_1X1)
            submit_result = video_provider.submit_video_task(
                image_source=str(image_file),
                prompt="make video",
                model_logical_key="veo_3_1_fast_portrait_fl_hd",
                api_key="test-key",
            )
            assert submit_result.success, submit_result.error_message
            assert submit_result.task_id == "vid_task_1"

            submit_with_bad_companion_url = video_provider.submit_video_task(
                image_source=str(image_file),
                prompt="make video from local image even when mapped URL is down",
                model_logical_key="veo_3_1_fast_portrait_fl_hd",
                api_key="test-key",
                extra_params={
                    "input_image_urls": ["https://media.pennitech.top:48443/down/image.png"],
                    "input_images": [str(image_file)],
                },
            )
            assert submit_with_bad_companion_url.success, submit_with_bad_companion_url.error_message

            text_submit_result = video_provider.submit_video_task(
                image_source="",
                prompt="make video from text",
                model_logical_key="veo_3_1_fast_portrait_fl_hd",
                api_key="test-key",
                extra_params={"duration": "8"},
            )
            assert text_submit_result.success, text_submit_result.error_message
            assert text_submit_result.task_id == "vid_task_text_1"

            poll_result = video_provider.poll_video_task("vid_task_1", "test-key")
            assert poll_result.finished, poll_result.error_message
            assert poll_result.video_url == "https://api.hellobabygo.com/v1/videos/vid_task_1/content"

            video_path = video_provider.download_video_content("vid_task_1", "test-key", tmp_dir / "video.mp4")
            assert Path(video_path).read_bytes() == MP4_LIKE
    finally:
        requests.post = original_post
        requests.get = original_get
        video_provider._session = original_video_session

    print("hellobabygo provider self-test passed")


if __name__ == "__main__":
    main()
