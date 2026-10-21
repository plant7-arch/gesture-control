"""Gesture control: a hand-gesture mouse for Linux (Wayland) using MediaPipe + uinput."""

import os

# OpenCV's preview window uses Qt. On GNOME Wayland, Qt falls back to XWayland
# anyway and prints a warning; asking for xcb up front keeps the console quiet.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

__version__ = "0.10.1"


def main() -> None:
    """Entry point for `uv run gesture-control`."""
    from .app import main as _main

    _main()
