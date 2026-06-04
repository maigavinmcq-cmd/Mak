from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import AppConfig, update_api_profile
from app.models.batch import TaskBatch
from app.runtime.worker_process import config_from_batch


def main() -> None:
    cfg = AppConfig()
    update_api_profile(
        cfg,
        "video",
        "xibapi_veo",
        api_key="sk-xibapi",
        last_model_logical_key="veo_3_1_hd_fl",
        base_url="https://xibapi.com",
    )
    update_api_profile(
        cfg,
        "video",
        "hellobabygo_veo",
        api_key="sk-hello",
        last_model_logical_key="veo_3_1_fast_portrait_fl_hd",
        base_url="https://api.hellobabygo.com",
    )
    cfg.video_provider = "hellobabygo_veo"
    cfg.video_api_key = "sk-hello"
    cfg.video_api_base_url = "https://api.hellobabygo.com"

    batch = TaskBatch(
        batch_id="batch_worker_process_reconcile",
        batch_name="batch_worker_process_reconcile",
        source_excel_path="",
        imported_at="",
        batch_config={
            "video_provider": "xibapi_veo",
            "video_model_logical_key": "stale_wrong_video_model",
            "video_api_key": "sk-hello",
            "video_api_base_url": "https://xibapi.com",
        },
    )

    runtime_cfg = config_from_batch(cfg, batch)

    assert runtime_cfg.video_provider == "xibapi_veo"
    assert runtime_cfg.video_model_logical_key == "veo_3_1_hd_fl"
    assert runtime_cfg.video_api_key == "sk-xibapi"
    assert runtime_cfg.video_api_base_url == "https://xibapi.com"

    print("worker process profile reconcile self-test passed")


if __name__ == "__main__":
    main()
