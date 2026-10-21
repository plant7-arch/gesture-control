"""Gesture control: a hand-gesture mouse for Linux (Wayland) using MediaPipe + uinput."""

import os

# OpenCV's preview window uses Qt. On GNOME Wayland, Qt falls back to XWayland
# anyway and prints a warning; asking for xcb up front keeps the console quiet.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")


def _version() -> str:
    """The version from pyproject.toml (the only place it's written), as installed."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("gesture-control")
    except PackageNotFoundError:  # running from a source tree without installing
        return "0+unknown"


__version__ = _version()


def main() -> None:
    """Entry point for `uv run gesture-control`."""
    from .app import main as _main

    _main()
