"""The camera reader's shutdown and its frame-rate setting, with a fake camera
(no /dev/video* or v4l2-ctl needed)."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from gesture_control import capture
from gesture_control.capture import Camera

CFG = {"index": 0, "width": 640, "height": 480, "fps": 30, "fourcc": "MJPG",
       "lock_framerate": True}


class FakeCap:
    """Stands in for cv2.VideoCapture. With block=True, read() hangs until .unblock
    is set, like a camera that stops responding."""

    def __init__(self, block: bool = False):
        self.block = block
        self.unblock = threading.Event()
        self.reading = threading.Event()
        self.released = 0
        self.read_running = False
        self.release_during_read = False

    def isOpened(self):
        return True

    def set(self, *_):
        return True

    def get(self, prop):
        return {capture.cv2.CAP_PROP_FRAME_WIDTH: 640,
                capture.cv2.CAP_PROP_FRAME_HEIGHT: 480,
                capture.cv2.CAP_PROP_FPS: 30}.get(prop, 0)

    def read(self):
        self.read_running = True
        self.reading.set()
        try:
            if self.block:
                self.unblock.wait(5)
            else:
                time.sleep(0.005)
            return True, np.zeros((480, 640, 3), np.uint8)
        finally:
            self.read_running = False

    def release(self):
        if self.read_running:
            self.release_during_read = True
        self.released += 1


@pytest.fixture
def v4l2(monkeypatch):
    """A fake v4l2-ctl: holds the control's value and records every set."""
    state = {"value": 1, "sets": []}

    def fake(self, *args):
        if args[0] == "-C":
            return f"{Camera.CONTROL}: {state['value']}\n"
        if args[0] == "-c":
            state["value"] = int(args[1].split("=")[1])
            state["sets"].append(state["value"])
            return ""
        if args[0] == "--list-ctrls":   # the camera's own default is 1
            return (f"{Camera.CONTROL} 0x009a0903 (bool)   : default=1 "
                    f"value={state['value']}\n")
        return None

    monkeypatch.setattr(Camera, "_v4l2", fake)
    return state


def _camera(monkeypatch, cap: FakeCap) -> Camera:
    monkeypatch.setattr(capture.cv2, "VideoCapture", lambda *a: cap)
    cam = Camera(dict(CFG))
    cam.start()
    return cam


def test_frames_arrive_and_stop_releases_once(monkeypatch, v4l2):
    cap = FakeCap()
    cam = _camera(monkeypatch, cap)
    assert cam.read(0, timeout=1.0) is not None
    cam.stop()
    assert cap.released == 1 and not cap.release_during_read


def test_stop_never_releases_the_camera_under_a_stuck_read(monkeypatch, v4l2):
    """A read() that hangs (camera unplugged or stuck): stop() returns after its
    timeout without releasing the capture under it; the reader thread releases it
    once read() returns. Releasing it mid-read can crash OpenCV."""
    monkeypatch.setattr(Camera, "STOP_TIMEOUT_S", 0.1)
    cap = FakeCap(block=True)
    cam = _camera(monkeypatch, cap)
    assert cap.reading.wait(1)
    t = time.monotonic()
    cam.stop()
    assert time.monotonic() - t < 1.0          # didn't hang the app's shutdown
    assert cap.released == 0                    # read() still running: left alone
    cap.unblock.set()                           # the camera finally answers
    cam._thread.join(2)
    assert cap.released == 1 and not cap.release_during_read


def test_framerate_control_is_put_back_when_the_app_changed_it(monkeypatch, v4l2):
    cam = _camera(monkeypatch, FakeCap())
    assert v4l2["value"] == 0                   # locked while running
    cam.stop()
    assert v4l2["value"] == 1 and v4l2["sets"] == [0, 1]


def test_framerate_control_already_off_is_left_alone(monkeypatch, v4l2):
    """Someone set exposure_dynamic_framerate=0 on purpose: the app must not turn it
    back on at exit (earlier versions put back the camera's default, 1)."""
    v4l2["value"] = 0
    cam = _camera(monkeypatch, FakeCap())
    cam.stop()
    assert v4l2["value"] == 0 and v4l2["sets"] == []


def test_failed_start_still_restores_the_control(monkeypatch, v4l2):
    """Camera can't open after the control was changed: stop() (called by the app
    on a failed start) puts it back and has no capture to release."""
    class Closed(FakeCap):
        def isOpened(self):
            return False

    monkeypatch.setattr(capture.cv2, "VideoCapture", lambda *a: Closed())
    cam = Camera(dict(CFG))
    with pytest.raises(RuntimeError, match="Could not open camera"):
        cam.start()
    cam.stop()
    assert v4l2["value"] == 1
