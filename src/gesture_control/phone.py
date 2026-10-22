"""An Android phone as the camera, through scrcpy (phone.enabled / --phone).

scrcpy streams the phone's camera over USB (or Wi-Fi) into a virtual camera made by
v4l2loopback, /dev/video<phone.video_device>, which the app then reads like any
webcam. This module starts scrcpy, waits until the picture is there, watches it while
the app runs, and stops it when the app quits, so one terminal is enough.

scrcpy runs in its own process group: Ctrl+C in the terminal reaches only the app,
which then stops scrcpy last, after releasing its own buttons and keys.
"""

from __future__ import annotations

import collections
import fcntl
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import threading
import time
from typing import Any

from .config import shown

MIN_VERSION = (3, 0)       # --no-window and the camera options used here
READY_LINE = "v4l2 sink started"
FIRST_FRAME_S = 0.2        # once frames flow, a moment before the app opens the camera
NO_PROBE_WAIT_S = 2.0      # if the camera's state can't be read, wait this long instead

# VIDIOC_QUERYCAP: _IOR('V', 0, struct v4l2_capability), 104 bytes. The capability
# flags sit at byte 84, the device's own flags at 88.
VIDIOC_QUERYCAP = 0x80685600
CAP_VIDEO_CAPTURE = 0x00000001
CAP_DEVICE_CAPS = 0x80000000


def capture_ready(path: str) -> bool | None:
    """Does the virtual camera offer a picture yet? v4l2loopback (exclusive_caps=1)
    only presents itself as a camera once scrcpy has written the first frame; before
    that, opening it fails ("can't open camera by index"). scrcpy's "v4l2 sink
    started" comes earlier, when it opens the device. None: couldn't be checked."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        buf = bytearray(104)
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)
    except OSError:
        return None
    finally:
        os.close(fd)
    caps, device_caps = struct.unpack_from("<II", buf, 84)
    if caps & CAP_DEVICE_CAPS:
        caps = device_caps
    return bool(caps & CAP_VIDEO_CAPTURE)
NOISE = ("non monotonically increasing dts",)  # repeated FFmpeg lines, kept out of -v

SIZE_RE = re.compile(r"^\d{2,5}x\d{2,5}$")
BIT_RATE_RE = re.compile(r"^\d+(\.\d+)?[KkMm]?$")
ADDRESS_RE = re.compile(r"^[\w.\-]+:\d{1,5}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")  # colours, if scrcpy adds any in a terminal

SETUP = """\
  sudo apt install scrcpy adb v4l2loopback-dkms
  sudo modprobe v4l2loopback video_nr={n} card_label="Phone camera" exclusive_caps=1
(README > Phone as the camera explains each step, and how to load it at every boot.)"""


class PhoneError(RuntimeError):
    """The phone camera can't start. The message says why and what to do."""


def check_settings(pc: dict[str, Any]) -> None:
    """Values in the phone: section that would make scrcpy fail (called by load_config)."""
    if pc["facing"] not in ("back", "front", "external"):
        raise ValueError("phone.facing must be back, front or external")
    if not SIZE_RE.match(str(pc["size"])):
        raise ValueError("phone.size must look like 640x480 (one of the sizes "
                         "scrcpy --video-source=camera --list-camera-sizes shows)")
    if not BIT_RATE_RE.match(str(pc["bit_rate"])):
        raise ValueError("phone.bit_rate must look like 8M, 4000K or 8000000")
    if pc["connect"] and not ADDRESS_RE.match(str(pc["connect"])):
        raise ValueError("phone.connect must be empty or an address like "
                         "192.168.1.42:41234 (from Wireless debugging on the phone)")
    extra = pc["extra_args"]
    if not isinstance(extra, list) or not all(isinstance(a, str) for a in extra):
        raise ValueError("phone.extra_args must be a list of scrcpy options, "
                         "e.g. [--camera-ar=4:3]")


def size_of(pc: dict[str, Any]) -> tuple[int, int]:
    w, h = str(pc["size"]).split("x")
    return int(w), int(h)


def device_node(pc: dict[str, Any]) -> str:
    return f"/dev/video{int(pc['video_device'])}"


def scrcpy_command(pc: dict[str, Any], exe: str = "scrcpy") -> list[str]:
    """The scrcpy command line for the phone: settings."""
    cmd = [exe]
    if pc["connect"]:
        cmd.append(f"--tcpip={pc['connect']}")        # connects over Wi-Fi by itself
    elif pc["serial"]:
        cmd.append(f"--serial={pc['serial']}")
    cmd += [
        "--video-source=camera",
        f"--camera-facing={pc['facing']}",
        f"--camera-size={pc['size']}",
        f"--camera-fps={int(pc['fps'])}",
        f"--video-bit-rate={pc['bit_rate']}",
        "--no-audio",
        f"--v4l2-sink={device_node(pc)}",
        "--no-window",
    ]
    return cmd + list(pc["extra_args"])


def parse_version(text: str) -> tuple[int, ...] | None:
    """'scrcpy 3.3.4 <https://...>' -> (3, 3, 4)."""
    m = re.search(r"scrcpy\s+v?(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(g) for g in m.groups() if g is not None) if m else None


class PhoneCamera:
    """Runs scrcpy for as long as the app needs the phone's camera."""

    def __init__(self, pc: dict[str, Any], verbose: bool = False,
                 exe: str | None = None, node_exists=os.path.exists,
                 probe=capture_ready):
        self.pc = pc
        self.verbose = verbose
        self.exe = exe
        self.node_exists = node_exists
        self.probe = probe
        self.proc: subprocess.Popen | None = None
        self._launched: subprocess.Popen | None = None  # for kill_now()
        self.lines: collections.deque[str] = collections.deque(maxlen=40)
        self._ready = threading.Event()
        self._reader: threading.Thread | None = None
        self._said_stopped = False
        # stop() may come from the main thread while start() runs in the startup
        # thread. The lock makes "launch scrcpy" and "stop" exclusive: either stop()
        # finds the process and stops it, or start() sees the flag and never launches.
        self._stopping = threading.Event()
        self._lock = threading.Lock()

    # -- start ---------------------------------------------------------------------
    def _check(self) -> str:
        exe = self.exe or shutil.which("scrcpy")
        n = int(self.pc["video_device"])
        if not exe:
            raise PhoneError("phone.enabled needs scrcpy, which isn't installed:\n"
                             + SETUP.format(n=n))
        if not self.node_exists(device_node(self.pc)):
            raise PhoneError(f"{device_node(self.pc)} doesn't exist: the virtual camera "
                             "for the phone isn't set up (or phone.video_device is a "
                             "different number). Create it with:\n"
                             f"  sudo modprobe v4l2loopback video_nr={n} "
                             'card_label="Phone camera" exclusive_caps=1')
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                 timeout=10, check=False).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise PhoneError(f"Couldn't run scrcpy: {exc}") from None
        version = parse_version(out)
        if version is not None and version[:2] < MIN_VERSION:
            have = ".".join(map(str, version))
            raise PhoneError(f"scrcpy {have} is too old for the phone camera (needs "
                             f"{MIN_VERSION[0]}.{MIN_VERSION[1]} or newer). Install the "
                             "latest release from https://github.com/Genymobile/scrcpy")
        return exe

    def command(self) -> list[str]:
        return scrcpy_command(self.pc, self.exe or "scrcpy")

    def start(self, timeout: float | None = None) -> None:
        """Start scrcpy and wait until the phone's frames reach the virtual camera."""
        self.exe = self._check()
        self._run(timeout)

    def restart(self, timeout: float | None = None) -> None:
        """Start scrcpy again after it quit while the app ran (the phone dropped out).
        The app keeps its reader on the virtual camera open: frames resume on it
        (measured: a second reader can't be opened while the first is). Raises
        PhoneError if it can't start now; failure_kind() says whether to retry."""
        self._run(timeout)

    def _run(self, timeout: float | None) -> None:
        self.lines.clear()            # this run's messages only (failure_kind reads them)
        self._ready.clear()
        self._said_stopped = False
        timeout = float(self.pc["start_timeout_s"]) if timeout is None else timeout
        if self.verbose:
            print("Phone camera: " + " ".join(self.command()))
        node = device_node(self.pc)
        # Was the device already offering a picture before scrcpy? (Only without
        # v4l2loopback's exclusive_caps: then it can't tell us when frames arrive.)
        offered_before = bool(self.probe(node))
        with self._lock:
            if self._stopping.is_set():
                # The app quit while the checks above ran: don't launch scrcpy only to
                # wait start_timeout_s for it (that took 22 s in v0.18.0).
                raise PhoneError("Stopped before the phone camera started")
            proc = self.proc = self._launched = self._launch()
        self._reader = threading.Thread(target=self._read, args=(proc,), name="scrcpy",
                                        daemon=True)
        self._reader.start()
        end = time.monotonic() + timeout

        def waiting() -> None:
            if proc.poll() is not None:  # failed, or stop() was called (quit at startup)
                self._reader.join(1)
                raise PhoneError(self._failure("scrcpy stopped before the camera started"))
            if time.monotonic() > end:
                self._terminate()  # not stop(): a reconnect may try again later
                raise PhoneError(self._failure(
                    f"The phone camera didn't start within {timeout:.0f} s"))

        # Ready = the phone's frames are arriving in the virtual camera: it then offers
        # video capture (with exclusive_caps=1, it doesn't before the first frame).
        # scrcpy's "v4l2 sink started" can't be relied on alone: it comes before the
        # first frame, and when scrcpy can't use a terminal it holds its messages back
        # until it exits.
        while True:
            waiting()
            state = self.probe(node)
            if state and (not offered_before or self._ready.is_set()):
                break
            if state is None and self._ready.is_set():
                # Can't check the device: give the first frames a fixed time instead.
                deadline = min(end, time.monotonic() + NO_PROBE_WAIT_S)
                while time.monotonic() < deadline:
                    waiting()
                    time.sleep(0.05)
                break
            time.sleep(0.05)
        time.sleep(FIRST_FRAME_S)

    def _launch(self) -> subprocess.Popen:
        """scrcpy, in a pseudo-terminal when possible: with plain pipes it holds its
        messages back until it exits, so neither -v nor errors would show in time."""
        common = dict(stdin=subprocess.DEVNULL,
                      start_new_session=True)  # Ctrl+C reaches the app only; it stops scrcpy
        try:
            master, slave = pty.openpty()
        except OSError:
            master = slave = None
        try:
            if master is None:
                proc = subprocess.Popen(self.command(), stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, **common)
                proc.output_fd = proc.stdout.fileno()
                return proc
            try:
                proc = subprocess.Popen(self.command(), stdout=slave, stderr=slave, **common)
            finally:
                os.close(slave)  # the child has its own copy
            proc.output_fd = master
            return proc
        except OSError as exc:
            if master is not None:
                os.close(master)
            raise PhoneError(f"Couldn't start scrcpy: {exc}") from None

    def _read(self, proc: subprocess.Popen) -> None:
        """scrcpy's output, line by line, as it comes."""
        fd, pending = proc.output_fd, b""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:  # EIO: the pseudo-terminal closed (scrcpy exited)
                chunk = b""
            if not chunk:
                break
            pending += chunk
            *lines, pending = re.split(rb"\r?\n", pending)
            for raw in lines:
                self._line(raw)
        if pending:
            self._line(pending)
        if proc.stdout is None:  # pseudo-terminal: our end is ours to close
            try:
                os.close(fd)
            except OSError:
                pass

    def _line(self, raw: bytes) -> None:
        line = ANSI_RE.sub("", raw.decode(errors="replace")).strip()
        if not line:
            return
        self.lines.append(line)
        if READY_LINE in line:
            self._ready.set()
        if self.verbose and not any(n in line for n in NOISE):
            print(f"[scrcpy] {line}", flush=True)

    def _failure(self, what: str) -> str:
        """The error, scrcpy's last words, and the most likely fixes."""
        text = "\n".join(self.lines)
        tips = []
        if "Could not find any ADB device" in text or "no devices" in text.lower():
            tips.append("Is the phone connected, with USB debugging on (or Wireless "
                        "debugging and phone.connect set)? `adb devices` must list it.")
        if "Multiple" in text and "devices" in text:
            tips.append("Several phones or connections are listed by `adb devices`: set "
                        f"phone.serial in {shown('local.yaml')} (or run adb disconnect).")
        if "unauthorized" in text:
            tips.append("Accept the 'Allow USB debugging?' prompt on the phone.")
        if "Could not connect" in text or "Connection refused" in text:
            tips.append("Wireless debugging's port changes each time it's turned on: "
                        "update phone.connect from the phone's Wireless debugging screen.")
        if "--camera-size" in text or "camera size" in text.lower():
            tips.append("phone.size must be one of the sizes listed by: scrcpy "
                        "--video-source=camera --list-camera-sizes")
        return (f"{what}.\nscrcpy said:\n{self.last_words()}"
                + "".join(f"\n{t}" for t in tips))

    def last_words(self) -> str:
        """scrcpy's last few lines, indented."""
        return "\n".join(f"  {ln}" for ln in list(self.lines)[-8:]) or "  (no output)"

    # -- while running ----------------------------------------------------------------
    def stopped_message(self) -> str:
        """Once, after scrcpy has quit on its own (phone unplugged, app closed on the
        phone): what happened. "" otherwise."""
        if self.proc is None or self.proc.poll() is None or self._said_stopped:
            return ""
        self._said_stopped = True
        return self._failure("The phone camera stopped (scrcpy quit)") + \
            "\nRestart gesture-control once the phone is connected again."

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def stopping(self) -> bool:
        """stop() was called: the app is quitting, nothing restarts scrcpy."""
        return self._stopping.is_set()

    def quit_reason(self) -> str:
        """scrcpy's own last warning or error, e.g. "Device disconnected"."""
        for line in reversed(self.lines):
            m = re.match(r"(?:\[server\]\s*)?(?:WARN|ERROR):\s*(.+)", line)
            if m:
                return m.group(1).strip()
        return "no message"

    def failure_kind(self) -> tuple[bool, str]:
        """After scrcpy failed or quit: (can a retry fix it?, a one-line hint or "").

        Retrying fixes a phone that's unplugged, still being set up by adb, waiting
        for the "Allow USB debugging?" prompt, or briefly off Wi-Fi. It can't fix a
        camera size or camera the phone doesn't offer: the same command fails again."""
        text = "\n".join(self.lines)
        low = text.lower()
        if "--camera-size" in text or "camera size" in low:
            return False, ("phone.size isn't a size this camera offers (see: scrcpy "
                           "--video-source=camera --list-camera-sizes)")
        if "--camera-facing" in text or "no camera" in low or "camera not found" in low:
            return False, "the phone has no camera facing phone.facing"
        if "unauthorized" in text:
            return True, "the phone asks 'Allow USB debugging?': accept it on the phone"
        if self.pc["connect"] and ("Could not connect" in text
                                   or "Connection refused" in text):
            return True, (f"can't reach {self.pc['connect']}. If Wireless debugging was "
                          "turned off and on, its port changed: put the new address in "
                          f"phone.connect ({shown('local.yaml')}) and restart the app")
        return True, ""

    # -- stop ---------------------------------------------------------------------------
    def kill_now(self) -> None:
        """Kill scrcpy at once, without waiting (the app's forced quit). Safe to call
        from a signal handler. Uses the last launched process, not self.proc: a normal
        stop() clears that before it waits up to 3 s for scrcpy to quit, and the
        forced quit usually comes during that wait."""
        proc = self._launched
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def stop(self) -> None:
        """Ask scrcpy to quit (like Ctrl+C), then make sure it has. Before start() has
        launched it, this makes start() give up instead, and no restart() follows."""
        with self._lock:
            self._stopping.set()
        self._terminate()

    def _terminate(self) -> None:
        """Stop this scrcpy run (a later restart() may start another)."""
        with self._lock:
            proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGINT)
                proc.wait(timeout=3)
            except (ProcessLookupError, PermissionError):
                pass
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2)
        self.proc = proc  # keep it for stopped_message(); poll() now says it ended
        self._said_stopped = True


def apply_settings(cfg: dict[str, Any]) -> None:
    """With the phone as the camera: read /dev/video<phone.video_device> at the size
    the phone sends."""
    pc = cfg["phone"]
    w, h = size_of(pc)
    cam = cfg["camera"]
    cam["index"] = int(pc["video_device"])
    cam["width"], cam["height"] = w, h
    cam["fps"] = int(pc["fps"])
    cam["lock_framerate"] = False  # the virtual camera has no exposure controls



# -- reconnecting while the app runs -------------------------------------------------

RETRY_S = 2.0            # try scrcpy again this often while the phone is away
FRAMES_WITHIN_S = 5.0    # scrcpy restarted but no frames this long after: try again
RECONNECT_START_TIMEOUT_S = 10.0  # one attempt's wait for the picture


class Reconnector:
    """Brings the phone camera back after it dropped out (cable out, Wi-Fi blip), in
    the background so the preview, q and Ctrl+C keep working.

    The app's reader on the virtual camera stays open the whole time: when scrcpy runs
    again, frames resume on it. (Measured on the phone this was built for: a second
    reader can't be opened while the first is, and frames do resume on the first.)

    state: "idle" (phone fine), "reconnecting" (retrying scrcpy), "started" (scrcpy
    runs again, waiting for frames), "failed" (a retry can't fix it: error says why).
    Messages for the terminal pile up in take_messages(), each said once."""

    def __init__(self, cam: PhoneCamera, retry_s: float = RETRY_S,
                 frames_within_s: float = FRAMES_WITHIN_S,
                 start_timeout: float = RECONNECT_START_TIMEOUT_S, clock=time.monotonic):
        self.cam = cam
        self.retry_s, self.frames_within_s = retry_s, frames_within_s
        self.start_timeout = start_timeout
        self.clock = clock
        self.state = "idle"
        self.error = ""
        self.since: float | None = None   # when the phone dropped out
        self._started_at = 0.0
        self._messages: collections.deque[str] = collections.deque()
        self._said: set[str] = set()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        """The phone is away and being brought back."""
        return self.state in ("reconnecting", "started")

    def take_messages(self) -> list[str]:
        out = []
        while self._messages:
            out.append(self._messages.popleft())
        return out

    def _say(self, text: str) -> None:
        if text not in self._said:
            self._said.add(text)
            self._messages.append(text)

    # -- what the app calls ------------------------------------------------------------
    def begin(self, now: float) -> None:
        """scrcpy has quit while the app ran: start bringing it back."""
        self.since = now
        self._said.clear()
        self._say(f"Phone disconnected (scrcpy: {self.cam.quit_reason()}). Reconnecting "
                  f"every {self.retry_s:g} s: plug it back in, or q / Ctrl+C to quit.")
        self._spawn()

    def check(self, now: float) -> None:
        """Call while no frames arrive. scrcpy that runs again but sends nothing (or
        quit again) is stopped and tried again."""
        if self.state != "started":
            return
        if not self.cam.running:
            self._spawn()                  # quit again (unplugged while restarting)
        elif now - self._started_at > self.frames_within_s:
            self._messages.append(f"scrcpy runs again, but no frames arrived within "
                                  f"{self.frames_within_s:g} s: trying again.")
            self.cam._terminate()
            self._spawn()

    def frames_back(self, now: float) -> bool:
        """A frame arrived. True (once) if it ends a disconnection."""
        if not self.active:
            return False
        self.state = "idle"
        self._messages.append(f"Phone camera back after {now - (self.since or now):.0f} s.")
        return True

    def stop(self) -> None:
        """The app is quitting: no more attempts (cam.stop() ends one in progress)."""
        self._wake.set()
        th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(timeout=1.0)

    # -- the background retries --------------------------------------------------------
    def _spawn(self) -> None:
        self.state = "reconnecting"
        self._thread = threading.Thread(target=self._loop, name="phone-reconnect",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not (self.cam.stopping or self._wake.is_set()):
            try:
                self.cam.restart(self.start_timeout)
            except PhoneError:
                if self.cam.stopping or self._wake.is_set():
                    return
                retry, hint = self.cam.failure_kind()
                if not retry:
                    self.error = (f"The phone camera can't reconnect: {hint}.\n"
                                  f"scrcpy said:\n{self.cam.last_words()}")
                    self.state = "failed"
                    return
                if hint:
                    self._say(hint[0].upper() + hint[1:] + ".")
                if self._wake.wait(self.retry_s):
                    return
                continue
            self._started_at = self.clock()
            self.state = "started"
            return


def quiet_opencv(quiet: bool, _saved: list = []) -> None:  # noqa: B006 (kept level)
    """While the phone is away, OpenCV's reader warns "select() timeout" every 10 s.
    quiet=True hides OpenCV's warnings (errors still show); quiet=False puts the
    previous level back."""
    import cv2

    log = cv2.utils.logging
    if quiet:
        if not _saved:
            _saved.append(log.getLogLevel())
            log.setLogLevel(log.LOG_LEVEL_ERROR)
    elif _saved:
        log.setLogLevel(_saved.pop())
