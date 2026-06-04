from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import (
    AppConfig,
    apply_api_profile_to_config,
    get_api_profile,
    load_config,
    reconcile_api_key_with_provider_profile,
    save_config,
    update_api_profile,
)


def main() -> None:
    cfg = AppConfig()
    cfg.image_provider = "xibapi_gpt_image2"
    cfg.image_model_logical_key = "gpt_image_2"
    cfg.image_api_key = "sk-image-old"
    cfg.video_provider = "xibapi_veo"
    cfg.video_model_logical_key = "veo_3_1_fast"
    cfg.video_api_key = "sk-video-old"

    update_api_profile(
        cfg,
        "image",
        "xibapi_gpt_image2",
        api_key="sk-image-new",
        last_model_logical_key="gpt_image_1",
        base_url="https://image.example",
        display_name="Xibapi GPT Image",
    )
    update_api_profile(
        cfg,
        "video",
        "xibapi_veo",
        api_key="sk-video-new",
        last_model_logical_key="veo_3_1",
        base_url="https://video.example",
        display_name="Xibapi VEO",
    )

    assert get_api_profile(cfg, "image", "xibapi_gpt_image2")["api_key"] == "sk-image-new"
    apply_api_profile_to_config(cfg, "image", "xibapi_gpt_image2")
    apply_api_profile_to_config(cfg, "video", "xibapi_veo")
    assert cfg.image_api_key == "sk-image-new"
    assert cfg.image_model_logical_key == "gpt_image_1"
    assert cfg.image_api_base_url == "https://image.example"
    assert cfg.video_api_key == "sk-video-new"
    assert cfg.video_model_logical_key == "veo_3_1"
    assert cfg.video_api_base_url == "https://video.example"

    cfg.video_provider = "xibapi_veo"
    cfg.video_model_logical_key = "veo_3_1_hd_fl"
    cfg.video_api_key = "sk-other-provider"
    cfg.video_api_base_url = "https://xibapi.com"
    update_api_profile(
        cfg,
        "video",
        "hellobabygo_veo",
        api_key="sk-other-provider",
        last_model_logical_key="veo_3_1_fast_portrait_fl_hd",
        base_url="https://api.hellobabygo.com",
    )
    changed = reconcile_api_key_with_provider_profile(cfg, "video")
    assert changed is True
    assert cfg.video_provider == "xibapi_veo"
    assert cfg.video_model_logical_key == "veo_3_1_hd_fl"
    assert cfg.video_api_key == "sk-video-new"
    assert cfg.video_api_base_url == "https://video.example"

    cfg.video_api_key = "sk-previous-xibapi-key"
    cfg.video_api_base_url = "https://old-xibapi.example"
    changed = reconcile_api_key_with_provider_profile(cfg, "video", force_profile=True)
    assert changed is True
    assert cfg.video_api_key == "sk-video-new"
    assert cfg.video_api_base_url == "https://video.example"

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "app_config.json"
        save_config(cfg, path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["api_profiles"]["image"]["xibapi_gpt_image2"]["api_key"] == "sk-image-new"

    print("self_test_api_profiles passed")


if __name__ == "__main__":
    main()
