from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI_PATH = ROOT / "app" / "gui.py"
DIAG_PATH = ROOT / "app" / "ui_diagnostics.py"


def main() -> None:
    assert DIAG_PATH.exists(), "app/ui_diagnostics.py should provide local UI lag diagnostics"
    diag_source = DIAG_PATH.read_text(encoding="utf-8")
    gui_source = GUI_PATH.read_text(encoding="utf-8")

    for token in [
        "class UIDiagnostics",
        "record_heartbeat",
        "measure",
        "ui_diagnostics",
        "ui_lag_diagnostics",
    ]:
        assert token in diag_source, f"missing diagnostic token: {token}"

    for token in [
        "self.ui_heartbeat_timer",
        "check_ui_heartbeat",
        "self.ui_diag.measure(\"refresh_table\"",
        "self.ui_diag.measure(\"flush_deferred_ui_updates\"",
        "self.ui_diag.measure(\"poll_process_worker_events\"",
        "events_count",
        "threading.active_count()",
    ]:
        assert token in gui_source, f"GUI should include diagnostic hook: {token}"


if __name__ == "__main__":
    main()
