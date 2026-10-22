"""The phone camera (phone.enabled / --phone): the scrcpy command, starting and stopping
scrcpy, and its errors. A fake scrcpy (a small script) stands in for the real one, so
no phone, adb or v4l2loopback is needed."""

from __future__ import annotations

import os
import stat
import sys
import time

import pytest
from conftest import make_config

from gesture_control import phone as ph
from gesture_control.phone import PhoneCamera, PhoneError, apply_settings, scrcpy_command

FAKE = r'''#!{python}
import signal, sys, time
mode = {mode!r}
if "--version" in sys.argv:
    time.sleep({version_delay})
    print("scrcpy {version} <https://github.com/Genymobile/scrcpy>")
    sys.exit(0)
with open({args_file!r}, "w") as f:
    f.write("\n".join(sys.argv[1:]))
if mode.startswith("plan:"):
    # One mode per launch, like a phone unplugged and plugged back in:
    # "plan:quits-later,no-device,ok" = first run, second run, third and later runs.
    steps = mode[5:].split(",")
    runs = {args_file!r} + ".runs"
    try:
        n = int(open(runs).read())
    except (OSError, ValueError):
        n = 0
    with open(runs, "w") as f:
        f.write(str(n + 1))
    mode = steps[min(n, len(steps) - 1)]
if mode == "unauthorized":
    print("ERROR: Device is unauthorized: accept the prompt on the device", flush=True)
    sys.exit(1)
print("INFO: ADB device found:", flush=True)
if mode == "no-device":
    print("ERROR: Could not find any ADB device", flush=True)
    sys.exit(1)
if mode == "bad-size":
    print("ERROR: Could not find camera size 999x999 (--camera-size)", flush=True)
    sys.exit(1)
def bye(*_):
    print("WARN: Killing the server...", flush=True)
    with open({args_file!r} + ".stopped", "w") as f:
        f.write("SIGINT")
    sys.exit(0)
signal.signal(signal.SIGINT, bye)
if mode == "ok":
    print("[server] INFO: Using camera '0'", flush=True)
    print("INFO: v4l2 sink started to device: /dev/video10", flush=True)
    for _ in range(3):
        print("ERROR: [FFmpeg] Application provided invalid, non monotonically "
              "increasing dts to muxer in stream 0: 1 >= 1", flush=True)
if mode == "quits-later":
    print("INFO: v4l2 sink started to device: /dev/video10", flush=True)
    time.sleep(0.3)
    print("ERROR: Device disconnected", flush=True)
    sys.exit(1)
if mode == "stdio":
    # Like a C program: messages are line-buffered in a terminal, held back otherwise
    # (whatever PYTHONUNBUFFERED says).
    import io, os
    sys.stdout = io.TextIOWrapper(open(1, "wb", buffering=1 << 16, closefd=False),
                                  line_buffering=os.isatty(1))
    print("INFO: ADB device found:")
    print("INFO: v4l2 sink started to device: /dev/video10")
if mode == "holds-back":
    # Like the real scrcpy without a terminal, whatever it's connected to: nothing
    # comes out until it exits.
    held = ["scrcpy 3.3.4", "INFO: v4l2 sink started to device: /dev/video10"]
    def release(*_):
        import os
        os.write(1, ("\n".join(held) + "\n").encode())
        sys.exit(0)
    signal.signal(signal.SIGINT, release)
if mode == "ignores-sigint":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    print("INFO: v4l2 sink started to device: /dev/video10", flush=True)
while True:
    time.sleep(0.05)
'''


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(ph, "FIRST_FRAME_S", 0.0)


def _fake(tmp_path, mode="ok", version="3.3.4", version_delay=0.0):
    exe = tmp_path / "scrcpy"
    args_file = str(tmp_path / "args.txt")
    exe.write_text(FAKE.format(python=sys.executable, mode=mode, version=version,
                               args_file=args_file, version_delay=version_delay))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe), tmp_path / "args.txt"


def _pc(**kw):
    return make_config(phone={"enabled": True, **kw})["phone"]


def _camera(tmp_path, mode="ok", version="3.3.4", verbose=False, probe=None, **kw):
    exe, args = _fake(tmp_path, mode, version)
    cam = PhoneCamera(_pc(**kw), verbose=verbose, exe=exe, node_exists=lambda p: True,
                      probe=probe or (lambda p: True))
    return cam, args


# -- the command --------------------------------------------------------------------------

def test_scrcpy_command_from_the_settings():
    cmd = scrcpy_command(_pc())
    assert cmd == ["scrcpy", "--video-source=camera", "--camera-facing=back",
                   "--camera-size=640x480", "--camera-fps=30", "--video-bit-rate=8M",
                   "--no-audio", "--v4l2-sink=/dev/video10", "--no-window"]


def test_wifi_address_serial_and_extra_options():
    cmd = scrcpy_command(_pc(connect="192.168.1.42:41234", facing="front",
                             video_device=12, extra_args=["--camera-ar=4:3"]))
    assert cmd[1] == "--tcpip=192.168.1.42:41234"
    assert "--camera-facing=front" in cmd and "--v4l2-sink=/dev/video12" in cmd
    assert cmd[-1] == "--camera-ar=4:3"
    assert scrcpy_command(_pc(serial="10BE4D1NXT000AZ"))[1] == "--serial=10BE4D1NXT000AZ"


@pytest.mark.parametrize("text, version", [
    ("scrcpy 3.3.4 <https://github.com/Genymobile/scrcpy>\n", (3, 3, 4)),
    ("scrcpy 2.4\n", (2, 4)),
    ("something else\n", None),
])
def test_version_is_read(text, version):
    assert ph.parse_version(text) == version


def test_the_app_reads_the_virtual_camera_at_the_phones_size():
    cfg = make_config(phone={"enabled": True, "size": "1280x720", "video_device": 11})
    apply_settings(cfg)
    cam = cfg["camera"]
    assert (cam["index"], cam["width"], cam["height"], cam["fps"]) == (11, 1280, 720, 30)
    assert cam["lock_framerate"] is False


# -- starting and stopping ------------------------------------------------------------

def test_start_waits_for_the_picture_and_stop_sends_ctrl_c(tmp_path):
    cam, args = _camera(tmp_path)
    cam.start(timeout=5)
    assert cam.running
    assert "--v4l2-sink=/dev/video10" in args.read_text().splitlines()
    cam.stop()
    assert not cam.running
    assert (tmp_path / "args.txt.stopped").read_text() == "SIGINT"  # a clean quit
    assert cam.stopped_message() == ""                               # we stopped it


def test_scrcpy_runs_in_its_own_process_group(tmp_path):
    """So Ctrl+C in the terminal reaches only the app, which stops scrcpy last."""
    cam, _ = _camera(tmp_path)
    cam.start(timeout=5)
    try:
        assert os.getpgid(cam.proc.pid) != os.getpgid(0)
    finally:
        cam.stop()


def test_scrcpy_that_ignores_ctrl_c_is_killed(tmp_path, monkeypatch):
    cam, _ = _camera(tmp_path, mode="ignores-sigint")
    cam.start(timeout=5)
    proc = cam.proc
    t = time.monotonic()
    cam.stop()
    assert proc.poll() is not None and time.monotonic() - t < 6


def test_verbose_shows_scrcpy_output_without_the_repeated_ffmpeg_lines(tmp_path, capsys):
    cam, _ = _camera(tmp_path, verbose=True)
    cam.start(timeout=5)
    time.sleep(0.2)
    cam.stop()
    out = capsys.readouterr().out
    assert out.startswith("Phone camera: ") and "--video-source=camera" in out
    assert "[scrcpy] INFO: v4l2 sink started" in out
    assert "non monotonically" not in out


# -- waiting for the first frames --------------------------------------------------------

def test_start_waits_until_frames_reach_the_virtual_camera(tmp_path):
    """scrcpy says "v4l2 sink started" before the first frame; opening the virtual
    camera before that fails ("can't open camera by index"), so start() waits until
    the camera presents itself as one."""
    checks = []

    def probe(path):
        checks.append(time.monotonic())
        return len(checks) >= 6            # frames arrive after a few checks

    cam, _ = _camera(tmp_path, probe=probe)
    cam.start(timeout=5)
    try:
        assert len(checks) == 6 and cam.running
    finally:
        cam.stop()


def test_frames_that_never_arrive_time_out(tmp_path):
    cam, _ = _camera(tmp_path, probe=lambda p: False)
    with pytest.raises(PhoneError, match="didn't start within 1 s"):
        cam.start(timeout=1)
    assert not cam.running


def test_scrcpy_quitting_while_waiting_for_frames_says_why(tmp_path):
    cam, _ = _camera(tmp_path, mode="quits-later", probe=lambda p: False)
    with pytest.raises(PhoneError, match="(?s)stopped before the camera started.*"
                                         "Device disconnected"):
        cam.start(timeout=5)


def test_when_the_camera_cant_be_checked_it_waits_a_fixed_time(tmp_path, monkeypatch):
    monkeypatch.setattr(ph, "NO_PROBE_WAIT_S", 0.4)
    cam, _ = _camera(tmp_path, probe=lambda p: None)
    t = time.monotonic()
    cam.start(timeout=5)
    try:
        assert 0.35 <= time.monotonic() - t < 3
    finally:
        cam.stop()


def test_messages_held_back_until_exit_dont_stop_the_start(tmp_path):
    """The bug: without a terminal, the real scrcpy holds "v4l2 sink started" back
    until it exits, so waiting for it timed out while the phone was streaming. Frames
    arriving in the virtual camera are what count."""
    calls = []

    def probe(path):
        calls.append(1)
        return len(calls) > 4                  # frames arrive after a moment

    cam, _ = _camera(tmp_path, mode="holds-back", probe=probe)
    t = time.monotonic()
    cam.start(timeout=5)
    try:
        assert time.monotonic() - t < 2 and cam.running
    finally:
        cam.stop()


def test_messages_come_through_live(tmp_path, capsys):
    """scrcpy runs in a pseudo-terminal, so its messages (line-buffered there, held
    back in a pipe) show live with -v, and the device can't be checked here: the
    start relies on the message."""
    cam, _ = _camera(tmp_path, mode="stdio", verbose=True, probe=lambda p: None)
    t = time.monotonic()
    cam.start(timeout=5)
    try:
        assert time.monotonic() - t < 4        # NO_PROBE_WAIT_S after the message
        assert "[scrcpy] INFO: v4l2 sink started" in capsys.readouterr().out
    finally:
        cam.stop()


def test_device_already_offering_a_picture_waits_for_scrcpy(tmp_path):
    """Without exclusive_caps the device offers capture before any frame: then the
    start waits for scrcpy's message instead of opening an empty camera."""
    cam, _ = _camera(tmp_path, mode="hang", probe=lambda p: True)
    with pytest.raises(PhoneError, match="didn't start within 1 s"):
        cam.start(timeout=1)


@pytest.mark.parametrize("caps, device_caps, ready", [
    (0x84200001, 0x04200001, True),     # capture (device caps in use): frames flowing
    (0x84200002, 0x04200002, False),    # output only: scrcpy hasn't written a frame
    (0x00000001, 0x00000000, True),     # old driver without device caps
    (0x00000002, 0x00000000, False),
])
def test_capture_ready_reads_the_cameras_flags(monkeypatch, caps, device_caps, ready):
    import struct

    def fake_ioctl(fd, request, buf):
        assert request == ph.VIDIOC_QUERYCAP and len(buf) == 104
        struct.pack_into("<II", buf, 84, caps, device_caps)

    monkeypatch.setattr(ph.os, "open", lambda *a: 99)
    monkeypatch.setattr(ph.os, "close", lambda fd: None)
    monkeypatch.setattr(ph.fcntl, "ioctl", fake_ioctl)
    assert ph.capture_ready("/dev/video10") is ready


def test_capture_ready_is_unknown_without_the_device():
    assert ph.capture_ready("/dev/does-not-exist") is None


# -- failures --------------------------------------------------------------------------

def test_no_phone_connected_says_so(tmp_path):
    cam, _ = _camera(tmp_path, mode="no-device")
    with pytest.raises(PhoneError) as err:
        cam.start(timeout=5)
    msg = str(err.value)
    assert "scrcpy stopped before the camera started" in msg
    assert "Could not find any ADB device" in msg and "USB debugging" in msg


def test_a_size_the_camera_doesnt_offer_points_to_the_list(tmp_path):
    cam, _ = _camera(tmp_path, mode="bad-size", size="999x999")
    with pytest.raises(PhoneError, match="--list-camera-sizes"):
        cam.start(timeout=5)


def test_picture_that_never_starts_times_out_and_stops_scrcpy(tmp_path):
    cam, _ = _camera(tmp_path, mode="hang")
    t = time.monotonic()
    with pytest.raises(PhoneError, match="didn't start within 1 s"):
        cam.start(timeout=1)
    assert time.monotonic() - t < 5 and not cam.running


def test_scrcpy_quitting_while_running_is_reported_once(tmp_path):
    cam, _ = _camera(tmp_path, mode="quits-later")
    cam.start(timeout=5)
    time.sleep(1.0)                        # it quits 0.3 s after the picture started
    msg = cam.stopped_message()
    assert "The phone camera stopped" in msg and "Device disconnected" in msg
    assert cam.stopped_message() == ""


def test_missing_scrcpy(monkeypatch):
    monkeypatch.setattr(ph.shutil, "which", lambda _: None)
    cam = PhoneCamera(_pc(), node_exists=lambda p: True)
    with pytest.raises(PhoneError, match="(?s)needs scrcpy.*sudo apt install scrcpy"):
        cam.start()


def test_missing_virtual_camera(tmp_path):
    exe, _ = _fake(tmp_path)
    cam = PhoneCamera(_pc(video_device=12), exe=exe, node_exists=lambda p: False)
    with pytest.raises(PhoneError, match="(?s)/dev/video12 doesn't exist.*video_nr=12"):
        cam.start()


def test_old_scrcpy(tmp_path):
    cam, _ = _camera(tmp_path, version="2.4")
    with pytest.raises(PhoneError, match="scrcpy 2.4 is too old"):
        cam.start()


def test_stopping_during_start_ends_the_wait_at_once(tmp_path):
    """Quitting the app while the phone starts must not wait for the timeout."""
    import threading

    cam, _ = _camera(tmp_path, mode="hang")
    errors = []

    def run():
        try:
            cam.start(timeout=30)
        except PhoneError as exc:
            errors.append(exc)

    th = threading.Thread(target=run)
    th.start()
    time.sleep(0.5)
    t = time.monotonic()
    cam.stop()
    th.join(5)
    assert not th.is_alive() and errors and time.monotonic() - t < 5


# -- settings ------------------------------------------------------------------------------

@pytest.mark.parametrize("text, message", [
    ("phone:\n  facing: side\n", "phone.facing"),
    ("phone:\n  size: big\n", "phone.size"),
    ("phone:\n  bit_rate: fast\n", "phone.bit_rate"),
    ("phone:\n  connect: 192.168.1.42\n", "phone.connect"),
    ("phone:\n  extra_args: --camera-ar=4:3\n", "phone.extra_args"),
    ("phone:\n  fps: 0\n", "phone.fps"),
    ("phone:\n  video_device: -1\n", "phone.video_device"),
])
def test_bad_phone_settings_are_refused(tmp_path, text, message):
    import shutil

    from gesture_control.config import DEFAULT_CONFIG_PATH, load_config

    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    (tmp_path / "local.yaml").write_text(text)
    with pytest.raises(ValueError, match=message):
        load_config(tmp_path / "gestures.yaml")


def test_empty_connect_line_means_usb(tmp_path):
    import shutil

    from gesture_control.config import DEFAULT_CONFIG_PATH, load_config

    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    (tmp_path / "local.yaml").write_text("phone:\n  enabled: true\n  connect:\n")
    assert load_config(tmp_path / "gestures.yaml")["phone"]["connect"] == ""


# -- the app ------------------------------------------------------------------------------

def test_phone_flags_switch_the_camera(monkeypatch):
    """--phone points the app at the virtual camera; --no-phone wins over the config."""
    from gesture_control import app

    seen = {}

    def stop_after_config(cfg, *a, **k):
        seen["camera"] = dict(cfg["camera"])
        seen["phone"] = cfg["phone"]["enabled"]
        raise SystemExit(0)

    monkeypatch.setattr(app, "_resolve_output", stop_after_config)
    with pytest.raises(SystemExit):
        app.main(["--pointer-test", "--dry-run", "--phone"])
    assert seen["phone"] and seen["camera"]["index"] == 10
    with pytest.raises(SystemExit):
        app.main(["--pointer-test", "--dry-run", "--no-phone"])
    assert not seen["phone"] and seen["camera"]["index"] == 0


def test_phone_and_no_phone_together_are_refused(capsys):
    from gesture_control.app import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--phone", "--no-phone"])
    assert "not allowed with" in capsys.readouterr().err


def test_quitting_before_scrcpy_launches_doesnt_wait_for_the_timeout(tmp_path):
    """v0.18.1 bug: stop() during start()'s checks found no process to stop, so
    start() launched scrcpy anyway and waited the whole start_timeout_s (the app took
    22 s to quit after Ctrl+C at 0.3 s). Now start() gives up and never launches."""
    import threading

    exe, args = _fake(tmp_path, mode="hang", version_delay=1.0)  # slow `scrcpy --version`
    cam = PhoneCamera(_pc(), exe=exe, node_exists=lambda p: True, probe=lambda p: False)
    errors = []

    def run():
        try:
            cam.start(timeout=20)
        except PhoneError as exc:
            errors.append(exc)

    th = threading.Thread(target=run)
    t0 = time.monotonic()
    th.start()
    time.sleep(0.3)
    cam.stop()                               # like Ctrl+C at 0.3 s
    th.join(10)
    assert not th.is_alive() and time.monotonic() - t0 < 3
    assert errors and "Stopped before the phone camera started" in str(errors[0])
    assert not args.exists()                 # scrcpy itself was never launched
    assert not cam.running


@pytest.mark.parametrize("value, sent", [(8000000, "8000000"), ("8M", "8M"),
                                         ("4000K", "4000K")])
def test_bit_rate_can_be_a_plain_number(tmp_path, value, sent):
    """v0.18.1: phone.bit_rate: 8000000 was refused as 'must be a word or text'."""
    import shutil

    import yaml

    from gesture_control.config import DEFAULT_CONFIG_PATH, load_config

    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    (tmp_path / "local.yaml").write_text(yaml.safe_dump({"phone": {"bit_rate": value}}))
    pc = load_config(tmp_path / "gestures.yaml")["phone"]
    assert f"--video-bit-rate={sent}" in scrcpy_command(pc)


# -- reconnecting while the app runs (v0.19.0) -----------------------------------------------

def _wait(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _runs(args) -> int:
    try:
        return int(args.with_name(args.name + ".runs").read_text())
    except OSError:
        return 0


def _dropped(tmp_path, plan):
    """A phone camera that started, then dropped out ("Device disconnected"); its
    later launches follow `plan` (comma-separated modes)."""
    cam, args = _camera(tmp_path, mode=f"plan:quits-later,{plan}")
    cam.start()
    assert _wait(lambda: not cam.running)
    return cam, args


def _reconnector(cam, clock=time.monotonic):
    return ph.Reconnector(cam, retry_s=0.05, frames_within_s=5.0, start_timeout=3.0,
                          clock=clock)


def test_reconnects_once_the_phone_is_back(tmp_path):
    """Unplugged for 3 tries, then plugged back in: scrcpy runs again by itself."""
    cam, args = _dropped(tmp_path, "no-device,no-device,no-device,ok")
    rc = _reconnector(cam)
    try:
        rc.begin(time.monotonic())
        assert rc.active
        assert _wait(lambda: rc.state == "started")
        assert cam.running and _runs(args) == 5        # first start, 3 failures, success
        assert rc.frames_back(time.monotonic()) and rc.state == "idle"
        msgs = rc.take_messages()
        assert msgs[0].startswith("Phone disconnected (scrcpy: Device disconnected). "
                                  "Reconnecting every 0.05 s")
        assert msgs[-1].startswith("Phone camera back after")
        assert not rc.frames_back(time.monotonic())    # said once
    finally:
        cam.stop()
        rc.stop()


def test_debugging_prompt_is_mentioned_once_while_retrying(tmp_path):
    cam, _ = _dropped(tmp_path, "unauthorized,unauthorized,unauthorized,ok")
    rc = _reconnector(cam)
    try:
        rc.begin(time.monotonic())
        assert _wait(lambda: rc.state == "started")
        msgs = rc.take_messages()
        assert sum("Allow USB debugging?" in m for m in msgs) == 1
    finally:
        cam.stop()
        rc.stop()


def test_a_failure_retrying_cant_fix_stops_the_reconnect(tmp_path):
    cam, _ = _dropped(tmp_path, "no-device,bad-size")
    rc = _reconnector(cam)
    try:
        rc.begin(time.monotonic())
        assert _wait(lambda: rc.state == "failed")
        assert "can't reconnect: phone.size isn't a size this camera offers" in rc.error
        assert "Could not find camera size" in rc.error           # scrcpy's own words
        assert not rc.active
    finally:
        cam.stop()
        rc.stop()


def test_scrcpy_that_runs_but_sends_no_frames_is_restarted(tmp_path):
    """The safety net: running again isn't enough, frames must arrive within 5 s."""
    clock = type("Clock", (), {"t": 0.0})()
    cam, args = _dropped(tmp_path, "ok")
    rc = _reconnector(cam, clock=lambda: clock.t)
    try:
        rc.begin(0.0)
        assert _wait(lambda: rc.state == "started")
        first = cam.proc
        rc.check(4.0)
        assert rc.state == "started"                   # still within 5 s
        clock.t = 0.0
        rc.check(5.5)
        assert any("no frames arrived within 5 s" in m for m in rc.take_messages())
        assert first.poll() is not None                # that run was stopped...
        assert _wait(lambda: rc.state == "started") and cam.running   # ...and another started
        assert _runs(args) == 3
    finally:
        cam.stop()
        rc.stop()


@pytest.mark.parametrize("plan", ["no-device", "hang"])   # between tries / during one
def test_quitting_while_reconnecting_is_quick_and_leaves_no_scrcpy(tmp_path, plan):
    cam, _ = _dropped(tmp_path, plan)
    rc = ph.Reconnector(cam, retry_s=2.0, start_timeout=20.0)
    rc.begin(time.monotonic())
    time.sleep(0.4)
    t = time.monotonic()
    cam.stop()
    rc.stop()
    assert time.monotonic() - t < 1.5
    assert not cam.running and (cam.proc is None or cam.proc.poll() is not None)
    assert rc._thread is None or not rc._thread.is_alive()


def test_forced_quit_kills_scrcpy_at_once(tmp_path):
    cam, args = _camera(tmp_path, mode="ignores-sigint")   # even one that ignores Ctrl+C
    cam.start()
    proc = cam.proc
    t = time.monotonic()
    cam.kill_now()
    assert _wait(lambda: proc.poll() is not None, timeout=2.0)
    assert time.monotonic() - t < 1.0


@pytest.mark.parametrize("line, retry, hint", [
    ("ERROR: Could not find any ADB device", True, ""),
    ("WARN: Device disconnected", True, ""),
    ("ERROR: Device is unauthorized", True, "Allow USB debugging?"),
    ("ERROR: Could not find camera size 999x999 (--camera-size)", False, "phone.size"),
])
def test_which_failures_a_retry_can_fix(tmp_path, line, retry, hint):
    cam, _ = _camera(tmp_path)
    cam.lines.append(line)
    can_retry, text = cam.failure_kind()
    assert can_retry == retry and hint in text


def test_wifi_that_cant_be_reached_suggests_the_new_port(tmp_path):
    cam, _ = _camera(tmp_path, connect="192.168.1.42:41234")
    cam.lines.append("ERROR: Could not connect to 192.168.1.42:41234")
    can_retry, text = cam.failure_kind()
    assert can_retry and "port changed" in text and "--connect" in text


def test_opencv_warnings_are_hidden_only_while_the_phone_is_away():
    import cv2

    log = cv2.utils.logging
    before = log.getLogLevel()
    ph.quiet_opencv(True)
    ph.quiet_opencv(True)                      # twice: still restores the original
    assert log.getLogLevel() == log.LOG_LEVEL_ERROR
    ph.quiet_opencv(False)
    assert log.getLogLevel() == before


def test_forced_quit_kills_scrcpy_even_while_a_normal_stop_waits_for_it(tmp_path):
    """Ctrl+C starts a normal stop, which gives scrcpy 3 s to quit; a forced quit
    (third Ctrl+C) during that wait must still find scrcpy and kill it."""
    import threading

    cam, _ = _camera(tmp_path, mode="ignores-sigint")
    cam.start()
    proc = cam.proc
    stopper = threading.Thread(target=cam.stop)
    stopper.start()
    time.sleep(0.3)                          # the normal stop is waiting now
    t = time.monotonic()
    cam.kill_now()
    assert _wait(lambda: proc.poll() is not None, timeout=2.0)
    assert time.monotonic() - t < 1.0
    stopper.join(5)



# -- --connect: the Wi-Fi address on the command line (v0.19.1) ---------------------------

def test_connect_flag_uses_the_phone_over_wifi_at_that_address(monkeypatch, tmp_path):
    """The Wi-Fi address changes each time Wireless debugging is turned on, so it can be
    given per run: --connect implies --phone and overrides phone.connect."""
    import shutil

    from gesture_control import app
    from gesture_control.config import DEFAULT_CONFIG_PATH

    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    (tmp_path / "local.yaml").write_text("phone:\n  connect: 10.0.0.5:5555\n")
    seen = {}

    def stop_after_config(cfg, *a, **k):
        seen.update(phone=dict(cfg["phone"]), index=cfg["camera"]["index"])
        raise SystemExit(0)

    monkeypatch.setattr(app, "_resolve_output", stop_after_config)
    with pytest.raises(SystemExit):
        app.main(["--config", str(tmp_path / "gestures.yaml"), "--pointer-test",
                  "--dry-run", "--connect", "192.168.1.42:41234"])
    assert seen["phone"]["enabled"] and seen["index"] == 10
    assert seen["phone"]["connect"] == "192.168.1.42:41234"
    assert scrcpy_command(seen["phone"])[1] == "--tcpip=192.168.1.42:41234"


@pytest.mark.parametrize("argv, message", [
    (["--connect", "192.168.1.42"], "isn't an address like 192.168.1.42:41234"),
    (["--connect", "192.168.1.42:37123 x"], "isn't an address"),
    (["--no-phone", "--connect", "192.168.1.42:41234"], "not allowed with argument --no-phone"),
])
def test_bad_connect_flags_are_refused(capsys, argv, message):
    from gesture_control.app import parse_args

    with pytest.raises(SystemExit):
        parse_args(argv)
    assert message in capsys.readouterr().err


def test_connect_works_together_with_phone():
    from gesture_control.app import parse_args

    args = parse_args(["--phone", "--connect", "192.168.1.42:41234"])
    assert args.phone and args.connect == "192.168.1.42:41234"
