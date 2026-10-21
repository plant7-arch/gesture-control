"""Threaded camera reader that always hands out the newest frame (no lag build-up)."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from typing import Any

import cv2
import numpy as np


class Camera:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.index = int(cfg["index"])
        self._cap: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._ts = 0.0
        self._seq = 0
        self.actual: tuple[int, int, float] = (0, 0, 0.0)
        self._saved_dynamic_fps: int | None = None

    def start(self) -> None:
        if self.cfg.get("lock_framerate", True):
            self._lock_framerate()
        cap = cv2.VideoCapture(self.index, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open camera /dev/video{self.index}")
        fourcc = str(self.cfg.get("fourcc", "MJPG"))
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.cfg["width"]))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.cfg["height"]))
        cap.set(cv2.CAP_PROP_FPS, float(self.cfg["fps"]))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.actual = (
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            float(cap.get(cv2.CAP_PROP_FPS)),
        )
        self._cap = cap
        self._running = True
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    CONTROL = "exposure_dynamic_framerate"

    def _v4l2(self, *args: str) -> str | None:
        if not shutil.which("v4l2-ctl"):
            return None
        try:
            out = subprocess.run(["v4l2-ctl", "-d", f"/dev/video{self.index}", *args],
                                 capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout if out.returncode == 0 else None

    def _lock_framerate(self) -> None:
        """Stop auto-exposure from lowering the frame rate in dim light (best effort).
        The camera's previous setting is remembered and put back by stop(), so other
        apps (video calls) aren't left with it changed."""
        out = self._v4l2("-C", self.CONTROL)  # e.g. "exposure_dynamic_framerate: 1"
        if out is None or ":" not in out:
            return  # no v4l2-ctl, or this camera doesn't have the control
        try:
            self._saved_dynamic_fps = int(out.split(":", 1)[1].strip())
        except ValueError:
            return
        if self._saved_dynamic_fps != 0:
            self._v4l2("-c", f"{self.CONTROL}=0")

    def _default_value(self) -> int | None:
        """The camera's own default for the control, from `v4l2-ctl --list-ctrls`:
        "exposure_dynamic_framerate 0x009a0903 (bool) : default=1 value=0"."""
        out = self._v4l2("--list-ctrls") or ""
        for line in out.splitlines():
            if line.strip().startswith(self.CONTROL) and "default=" in line:
                try:
                    return int(line.split("default=", 1)[1].split()[0])
                except (ValueError, IndexError):
                    return None
        return None

    def _restore_framerate(self) -> None:
        saved, self._saved_dynamic_fps = self._saved_dynamic_fps, None
        if saved is None:
            return
        if saved == 0:
            # Already 0 at startup: most likely left that way by an earlier version of
            # this app, so put back the camera's own default instead.
            saved = self._default_value()
        if saved not in (None, 0):
            self._v4l2("-c", f"{self.CONTROL}={saved}")

    def _run(self) -> None:
        assert self._cap is not None
        failures = 0
        while self._running:
            ok, frame = self._cap.read()
            ts = time.monotonic()
            if not ok or frame is None:
                failures += 1
                time.sleep(0.01 if failures < 100 else 0.2)
                continue
            failures = 0
            with self._cond:
                self._frame, self._ts = frame, ts
                self._seq += 1
                self._cond.notify_all()

    def read(self, last_seq: int, timeout: float = 1.0) -> tuple[np.ndarray, float, int] | None:
        """Wait for a frame newer than last_seq. Returns (frame, capture_time, seq)."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq > last_seq or not self._running, timeout):
                return None
            if self._frame is None:
                return None
            return self._frame, self._ts, self._seq

    def stop(self) -> None:
        self._running = False
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self._restore_framerate()
