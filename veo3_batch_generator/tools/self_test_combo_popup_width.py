from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui_combo import bounded_popup_width


def test_popup_width_expands_for_long_options() -> None:
    width = bounded_popup_width(
        option_texts=[
            "Veo 3.1 Fast",
            "Veo 3.1 Portrait FL HD",
            "Veo 3.1 Landscape FL HD - long provider display name",
        ],
        current_width=120,
        screen_width=1000,
        char_px=8,
    )
    assert width > 120
    assert width >= 480


def test_popup_width_is_clamped_to_screen() -> None:
    width = bounded_popup_width(
        option_texts=["X" * 300],
        current_width=180,
        screen_width=800,
        char_px=9,
    )
    assert width <= 620
    assert width <= int(800 * 0.82)


if __name__ == "__main__":
    test_popup_width_expands_for_long_options()
    test_popup_width_is_clamped_to_screen()
    print("combo popup width self-test passed")
