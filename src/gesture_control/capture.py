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
        self._release_lock = threading.Lock()

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
        apps (video calls) aren't left with it changed. A camera already at 0 isn't
        touched, now or at stop()."""
        out = self._v4l2("-C", self.CONTROL)  # e.g. "exposure_dynamic_framerate: 1"
        if out is None or ":" not in out:
            return  # no v4l2-ctl, or this camera doesn't have the control
        try:
            self._saved_dynamic_fps = int(out.split(":", 1)[1].strip())
        except ValueError:
            return
        if self._saved_dynamic_fps != 0:
            self._v4l2("-c", f"{self.CONTROL}=0")

    def _restore_framerate(self) -> None:
        """Put the control back, but only if this app changed it. A camera that was
        already at 0 when the app started stays at 0: that's someone's own setting."""
        saved, self._saved_dynamic_fps = self._saved_dynamic_fps, None
        if saved not in (None, 0):
            self._v4l2("-c", f"{self.CONTROL}={saved}")

    def _release_cap(self) -> None:
        """Release the capture exactly once, from whichever side gets here first."""
        with self._release_lock:
            cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()

    def _run(self) -> None:
        cap = self._cap
        assert cap is not None
        failures = 0
        try:
            while self._running:
                ok, frame = cap.read()
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
        finally:
            # The reader thread releases the capture itself, after its last read():
            # releasing it from another thread while read() is still running can
            # crash OpenCV (e.g. a camera that hangs or is unplugged).
            self._release_cap()

    def read(self, last_seq: int, timeout: float = 1.0) -> tuple[np.ndarray, float, int] | None:
        """Wait for a frame newer than last_seq. Returns (frame, capture_time, seq)."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq > last_seq or not self._running, timeout):
                return None
            if self._frame is None:
                return None
            return self._frame, self._ts, self._seq

    STOP_TIMEOUT_S = 2.0  # how long stop() waits for a read() in progress

    def stop(self) -> None:
        self._running = False
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self.STOP_TIMEOUT_S)
        if thread is None or not thread.is_alive():
            self._release_cap()  # never started, or already done (then a no-op)
        # else: read() is stuck; the thread releases the capture when it returns,
        # and as a daemon thread it doesn't keep the app from exiting.
        self._restore_framerate()
