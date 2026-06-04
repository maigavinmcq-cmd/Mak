from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = (ROOT / "app" / "api" / "video_providers" / "hellobabygo_veo_provider.py").read_text(encoding="utf-8")
    submit = source[source.index("    def _submit_image_video") : source.index("    def _submit_text_video")]
    assert "data = {" in submit, "multipart submit should send scalar fields through data="
    assert '"duration": str(duration)' not in submit, "duration as a normal multipart field is parsed as a string by the API"
    assert '"alias": json.dumps({"duration": duration}' in submit, "duration must be sent as an alias JSON object so it remains an int"
    assert "('duration'," not in submit and '("duration",' not in submit, "duration must not be sent as a files-part"
    assert "files=files" in submit, "image references should still be sent as multipart files"
    print("hellobabygo duration alias static check passed")


if __name__ == "__main__":
    main()
