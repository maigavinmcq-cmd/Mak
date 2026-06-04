from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import AppConfig, update_api_profile
from app.gui import MainWindow
from app.models.batch import TaskBatch


class _BatchManagerStub:
    def batch_dir(self, batch_id: str) -> Path:
        return Path("C:/tmp") / batch_id

    def task_state_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "task_state.json"


def main() -> None:
    cfg = AppConfig()
    cfg.video_provider = "hellobabygo_veo"
    cfg.video_api_key = "sk-hello"
    cfg.video_api_base_url = "https://api.hellobabygo.com"
    update_api_profile(
        cfg,
        "video",
        "hellobabygo_veo",
        api_key="sk-hello",
        last_model_logical_key="veo_3_1_fast_portrait_fl_hd",
        base_url="https://api.hellobabygo.com",
    )
    update_api_profile(
        cfg,
        "video",
        "xibapi_veo",
        api_key="sk-xibapi",
        last_model_logical_key="veo_3_1_hd_fl",
        base_url="https://xibapi.com",
    )
    batch = TaskBatch(
        batch_id="test_batch",
        batch_name="test_batch",
        source_excel_path="",
        imported_at="",
        batch_config={
            "video_provider": "xibapi_veo",
            "video_model_logical_key": "veo_3_1_hd_fl",
            "video_api_key": "sk-hello",
            "video_api_base_url": "https://xibapi.com",
        },
    )
    dummy = SimpleNamespace(config=cfg, batch_manager=_BatchManagerStub())

    runtime_cfg = MainWindow.config_from_batch(dummy, batch)

    assert runtime_cfg.video_provider == "xibapi_veo"
    assert runtime_cfg.video_model_logical_key == "veo_3_1_hd_fl"
    assert runtime_cfg.video_api_key == "sk-xibapi"
    assert runtime_cfg.video_api_base_url == "https://xibapi.com"

    print("self_test_batch_profile_reconcile passed")


if __name__ == "__main__":
    main()
