from __future__ import annotations

from collections.abc import Iterable, Sequence


def bounded_popup_width(
    option_texts: Iterable[str],
    current_width: int,
    screen_width: int,
    char_px: int = 8,
    text_pixel_widths: Sequence[int] | None = None,
    horizontal_padding: int = 74,
    min_width: int = 220,
    max_width: int = 620,
    screen_ratio: float = 0.82,
) -> int:
    """Return a practical popup width for combo boxes with long option text."""

    current = max(0, int(current_width or 0))
    screen = max(320, int(screen_width or 0))
    if text_pixel_widths:
        longest = max(int(value or 0) for value in text_pixel_widths)
    else:
        longest = max((len(str(text or "")) for text in option_texts), default=0) * max(4, int(char_px or 8))
    desired = max(current, min_width, longest + horizontal_padding)
    hard_cap = min(max_width, max(min_width, int(screen * screen_ratio)))
    return max(current, min(desired, hard_cap))
