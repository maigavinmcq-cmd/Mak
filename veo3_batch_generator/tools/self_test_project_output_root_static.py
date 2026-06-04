from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    config_source = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert "project_output_root" in config_source
    assert "def project_outputs_dir" in config_source
    assert '"project_output_root"' in config_source

    gui_source = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
    assert "runtime_events_dir(self.config" in gui_source
    assert "UIDiagnostics(PROJECT_ROOT, runtime_events_dir(self.config" in gui_source
    assert 'project_outputs_dir(self.config, "runtime_events"' not in gui_source
    assert 'PROJECT_ROOT / "outputs" / "runtime_events"' not in gui_source

    diagnostics_source = (ROOT / "app" / "ui_diagnostics.py").read_text(encoding="utf-8")
    assert "output_root" in diagnostics_source
    assert 'self.project_root / "outputs" / "ui_lag_diagnostics"' not in diagnostics_source

    task_manager_source = (ROOT / "app" / "task_manager.py").read_text(encoding="utf-8")
    assert 'parent.parent / "outputs" / "state_save_fallback"' not in task_manager_source
    assert '"state_save_fallback"' in task_manager_source

    print("project output root static self-test passed")


if __name__ == "__main__":
    main()
