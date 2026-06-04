from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.file_utils as file_utils


MP4_LIKE = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + (b"\0" * 1200) + b"mdat" + (b"\0" * 64) + b"moov"
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.headers = headers or {"Content-Length": str(len(body))}
        self.ok = True
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 1024):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index : index + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        product_dir = file_utils.product_image_folder(root)
        product_dir.mkdir(parents=True)
        (product_dir / "a.png").write_bytes(PNG_1X1)
        (product_dir / "target.JPG").write_bytes(PNG_1X1)

        found, status, error = file_utils.find_product_image_by_filename(root, "target.JPG")
        assert status is None, error
        assert Path(found or "").name == "target.JPG"

        found_case, status, error = file_utils.find_product_image_by_filename(root, "TARGET.jpg")
        assert status is None, error
        assert Path(found_case or "").name.lower() == "target.jpg"

        missing, status, error = file_utils.find_product_image_by_filename(root, "../missing.png")
        assert missing is None
        assert status == "SKIPPED_NO_PRODUCT_IMAGE", error

        video_path = root / "video.mp4"
        video_path.write_bytes(b"broken")

        original_get: Any = file_utils.requests.get

        def fake_get(url: str, **kwargs) -> FakeResponse:
            del url, kwargs
            return FakeResponse(MP4_LIKE)

        file_utils.requests.get = fake_get
        try:
            saved, downloaded = file_utils.download_video_to_path("https://example.com/video.mp4", video_path)
            assert downloaded is True
            assert Path(saved).read_bytes() == MP4_LIKE
        finally:
            file_utils.requests.get = original_get

    print("product image filename and download validation self-test passed")


if __name__ == "__main__":
    main()
