"""Pick the right OS adapter."""

from __future__ import annotations

import sys

from .base import AccessDenied, FileInfo, Platform, PlatformError, ProcessGone  # noqa: F401


def get_platform() -> Platform:
    if sys.platform == "win32":
        from .windows import WindowsPlatform

        return WindowsPlatform()
    from .generic import GenericPlatform

    return GenericPlatform()
