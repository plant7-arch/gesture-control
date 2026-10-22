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


# -- the phone dropping out while the app runs (v0.19.0) -------------------------------------

def _phone_app(monkeypatch, tmp_path, plan, local=""):
    """The app with --phone: a fake scrcpy following `plan` (one mode per launch, see
    test_phone.FAKE) and a camera that, like v4l2loopback, allows only one reader and
    has frames only while scrcpy runs. Stops after 15 frames following the return."""
    import test_phone

    from gesture_control import phone as ph

    exe, args = test_phone._fake(tmp_path, mode=f"plan:{plan}")
    monkeypatch.setattr(ph, "FIRST_FRAME_S", 0.0)
    made = {"phones": [], "cameras": 0}

    class Phone(ph.PhoneCamera):
        def __init__(self, pc, verbose=False):
            super().__init__(pc, verbose, exe=exe, node_exists=lambda p: True,
                             probe=lambda p: True)
            made["phones"].append(self)

    class QuickReconnector(ph.Reconnector):
        def __init__(self, cam):
            super().__init__(cam, retry_s=0.05, start_timeout=3.0)

    class OneReader:
        def __init__(self, cfg):
            self.index, self.actual = int(cfg["index"]), (640, 480, 30.0)
            made["cameras"] += 1
            self.served = self.after = 0
            self.dropped = False

        def start(self):
            if made["cameras"] > 1:
                raise RuntimeError("can't open camera by index (a reader is open)")

        def read(self, seq, timeout=1.0):
            phone = made["phones"][0]
            if not phone.running:
                self.dropped = True
                time.sleep(0.05)          # (the real read waits up to 1 s)
                return None
            if self.dropped:
                self.after += 1
                if self.after > 15:
                    raise KeyboardInterrupt
            self.served += 1
            time.sleep(0.01)
            return np.zeros((480, 640, 3), np.uint8), time.monotonic(), seq + 1

        def stop(self):
            pass

    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    if local:
        (tmp_path / "local.yaml").write_text(local)
    monkeypatch.setattr(model, "ensure_model", lambda *a, **k: None)
    monkeypatch.setattr(display, "primary_display", lambda: None)
    monkeypatch.setattr(tpm, "TouchpadSettings", FakeSettings)
    monkeypatch.setattr(tracker, "HandTracker", FakeTracker)
    monkeypatch.setattr(capture, "Camera", OneReader)
    monkeypatch.setattr(ph, "PhoneCamera", Phone)
    monkeypatch.setattr(ph, "Reconnector", QuickReconnector)

    def run():
        app.main(["--config", str(tmp_path / "gestures.yaml"), "--dry-run",
                  "--no-preview", "--phone"])
    return run, made, args


def test_unplugged_phone_comes_back_without_restarting_the_app(monkeypatch, tmp_path,
                                                               capsys):
    """Unplug (scrcpy quits: Device disconnected), 3 failed tries, plug back in: the
    app carries on with the same camera reader (a second one can't be opened)."""
    run, made, args = _phone_app(monkeypatch, tmp_path,
                                 "quits-later,no-device,no-device,no-device,ok")
    run()
    out = capsys.readouterr()
    assert made["cameras"] == 1                                  # never a second reader
    assert out.out.count("Phone disconnected (scrcpy: Device disconnected).") == 1
    assert out.out.count("Phone camera back after") == 1
    assert "No frames from the phone" not in out.out             # no repeated stall lines
    assert "Stopped." in out.out and "Traceback" not in out.err
    assert int(args.with_name(args.name + ".runs").read_text()) == 5
    phone = made["phones"][0]
    assert not phone.running                                     # scrcpy stopped at exit


def test_with_reconnect_off_the_app_stops_when_the_phone_drops_out(monkeypatch, tmp_path,
                                                                   capsys):
    run, made, args = _phone_app(monkeypatch, tmp_path, "quits-later,ok",
                                 local="phone:\n  reconnect: false\n")
    run()
    out = capsys.readouterr()
    assert "The phone camera stopped (scrcpy quit)" in out.err
    assert "Stopping, as phone.reconnect is false" in out.err
    assert "Stopped." in out.out
    assert int(args.with_name(args.name + ".runs").read_text()) == 1   # no restart


def test_a_failure_retrying_cant_fix_stops_the_app_with_the_reason(monkeypatch, tmp_path,
                                                                    capsys):
    run, made, _ = _phone_app(monkeypatch, tmp_path, "quits-later,bad-size")
    run()
    out = capsys.readouterr()
    assert "The phone camera can't reconnect: phone.size isn't a size" in out.err
    assert "Stopped." in out.out and not made["phones"][0].running


# -- quitting ---------------------------------------------------------------------------------

def test_forced_quit_runs_the_last_minute_stops_first():
    """The third Ctrl+C exits at once, but first kills scrcpy, which runs in its own
    process group and would otherwise keep the phone's camera."""
    order = []
    guard = app.ShutdownGuard(exit_now=lambda code: order.append(("exit", code)),
                              say=lambda s: None)
    guard.on_force_quit.append(lambda: order.append("kill scrcpy"))
    guard.on_force_quit.append(lambda: 1 / 0)          # one failing doesn't block exit
    guard.cleaning()
    guard._handle(2, None)
    guard._handle(2, None)
    assert order == ["kill scrcpy", ("exit", 130)]


def test_still_stopping_says_when_it_waits_for_the_hand_tracker():
    said = []
    guard = app.ShutdownGuard(exit_now=lambda c: None, say=said.append)
    guard.cleaning()
    guard.doing = guard.TRACKER_LOADING
    guard._handle(2, None)
    assert said == ["Still stopping (waiting for the hand tracker to finish loading, a few "
                    "seconds)... press Ctrl+C again to quit at once."]
