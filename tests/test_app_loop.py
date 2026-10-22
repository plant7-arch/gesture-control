"""The app's main loop and startup, run for real with stand-ins for the camera, the
hand tracker, GNOME and the model download (--dry-run, no preview window)."""

from __future__ import annotations

import shutil
import time

import numpy as np
import pytest

from gesture_control import app, capture, display, model, tracker
from gesture_control import controller as ctl_mod
from gesture_control import touchpad as tpm
from gesture_control.config import DEFAULT_CONFIG_PATH


class FakeSettings:
    """GNOME's touchpad settings, without gsettings."""
    state, natural, two_finger = "enabled", True, True
    usable = scroll_usable = True

    def __init__(self, *a, **k):
        pass

    @classmethod
    def read(cls):
        return "enabled"

    def stop(self):
        pass

    def message(self):
        return ""

    def scroll_message(self):
        return ""


def fake_camera(frames, start_error=None):
    """A Camera that serves `frames` capture times (None = a 1 s stall), then quits."""
    class FakeCamera:
        def __init__(self, cfg):
            self.index = int(cfg["index"])
            self.actual = (640, 480, 30.0)
            self.stopped = False
            self._todo = list(frames)

        def start(self):
            if start_error is not None:
                raise start_error

        def read(self, seq, timeout=1.0):
            if not self._todo:
                raise KeyboardInterrupt      # like Ctrl+C: the app stops cleanly
            ts = self._todo.pop(0)
            if ts is None:
                return None
            return np.zeros((480, 640, 3), np.uint8), ts, seq + 1

        def stop(self):
            self.stopped = True

    return FakeCamera


class FakeTracker:
    def __init__(self, cfg):
        pass

    def warm_up(self, *a):
        pass

    def process_all(self, frame, ts_ms):
        time.sleep(0.002)  # some tracking time
        return []

    def close(self):
        pass


@pytest.fixture
def run_app(monkeypatch, tmp_path):
    """Run the app on the shipped settings only (never the user's local.yaml)."""
    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    monkeypatch.setattr(model, "ensure_model", lambda *a, **k: None)
    monkeypatch.setattr(display, "primary_display", lambda: None)
    monkeypatch.setattr(tpm, "TouchpadSettings", FakeSettings)
    monkeypatch.setattr(tracker, "HandTracker", FakeTracker)

    def run(frames=(), *args, start_error=None):
        monkeypatch.setattr(capture, "Camera", fake_camera(frames, start_error))
        app.main(["--config", str(tmp_path / "gestures.yaml"), "--dry-run",
                  "--no-preview", *args])
    return run


# -- startup errors ----------------------------------------------------------------------

def test_a_camera_that_fails_to_start_is_one_line_not_a_traceback(run_app, capsys):
    """v0.18.1 bug: an error other than RuntimeError (here a bad camera.fourcc made
    OpenCV raise TypeError) escaped as a traceback. It is now one line, and the app
    still cleans up."""
    run_app(start_error=TypeError("VideoWriter_fourcc() takes at most 4 arguments"))
    out = capsys.readouterr()
    assert ("Error: The camera couldn't start: TypeError: VideoWriter_fourcc() "
            "takes at most 4 arguments") in out.err
    assert "Traceback" not in out.err
    assert "Stopped." in out.out


def test_verbose_adds_the_traceback_for_a_bug_report(run_app, capsys):
    run_app((), "-v", start_error=TypeError("boom"))
    err = capsys.readouterr().err
    assert "Traceback" in err and "Error: The camera couldn't start: TypeError: boom" in err


# -- the clock the controller runs on ------------------------------------------------------

def test_the_controller_gets_each_frames_capture_time(run_app, monkeypatch):
    """v0.18.1: the controller used the time after hand tracking, which varied 7.7 to
    62.7 ms between frames captured 31-37 ms apart (measured). Speeds are now worked
    out from the capture times."""
    seen = []
    real = ctl_mod.GestureController.update

    def update(self, feats, anchor, t, *a, **k):
        seen.append(t)
        return real(self, feats, anchor, t, *a, **k)

    monkeypatch.setattr(ctl_mod.GestureController, "update", update)
    frames = [5000.0 + i / 30 for i in range(10)]
    run_app(frames)
    assert seen == frames


# -- the phone camera ----------------------------------------------------------------------

def test_camera_flag_that_would_ignore_the_phone_is_refused(run_app):
    """v0.18.1 bug: --phone --camera 0 started scrcpy into /dev/video10 but read
    /dev/video0, so the phone was never used."""
    with pytest.raises(SystemExit) as exc:
        run_app((), "--phone", "--camera", "0")
    msg = str(exc.value.code)
    assert "--camera 0 can't be used with the phone camera, which is /dev/video10" in msg
    assert "--no-phone --camera 0" in msg


def test_camera_flag_naming_the_phones_device_is_fine(monkeypatch, tmp_path):
    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    seen = {}

    def stop_after_config(cfg, *a, **k):
        seen["index"] = cfg["camera"]["index"]
        raise SystemExit(0)

    monkeypatch.setattr(app, "_resolve_output", stop_after_config)
    with pytest.raises(SystemExit):
        app.main(["--config", str(tmp_path / "gestures.yaml"), "--pointer-test",
                  "--dry-run", "--phone", "--camera", "10"])
    assert seen["index"] == 10


def test_stall_messages_name_the_phone_when_it_is_the_camera():
    assert app.stall_message(6.2, phone=False) == (
        "No frames from the camera for 6 s - is another app using it?")
    msg = app.stall_message(6.2, phone=True)
    assert msg.startswith("No frames from the phone for 6 s") and "connected" in msg
    assert "another app" not in msg
