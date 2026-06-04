from __future__ import annotations

__all__ = ["launch"]


def launch(*args, **kwargs):
    from light_image_tool.app import launch as _launch

    return _launch(*args, **kwargs)
