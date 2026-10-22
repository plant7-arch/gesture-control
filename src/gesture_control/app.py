"""Main loop: camera -> hand landmarks -> features -> state machine -> virtual mouse."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .config import (
    DEFAULT_CONFIG_PATH,
    command,
    load_config,
    pointer_device_mode,
    shown,
)
from .pointer import VirtualKeys, VirtualPointer

PHONE_OPEN_TRIES, PHONE_OPEN_RETRY_S = 10, 0.3  # phone camera: keep trying for ~3 s
PLACEHOLDER_AFTER_S = 0.5  # show the "Starting..." window only if startup is slower
CAMERA_STALL_HINT = "is another app using it?"
PHONE_STALL_HINT = ("is the phone still connected (cable or Wi-Fi) and its camera "
                    "still on?")


RECONNECTING_HINT = "phone disconnected: reconnecting, plug it back in"


def phone_stalled(phone_cam, reconnector, now: float) -> str:
    """No frames for a second, with the phone as the camera. Notices scrcpy quitting
    (phone unplugged, Wi-Fi gone) and brings it back, or says why it can't.
    Returns "stop" (quit the app), "reconnecting" (the phone is being brought
    back; the usual no-frames line isn't needed) or "" (scrcpy still runs)."""
    if reconnector is None:  # phone.reconnect: false
        if msg := phone_cam.stopped_message():
            print(msg.replace("Restart gesture-control once the phone is connected again.",
                              "Stopping, as phone.reconnect is false. Start the app again "
                              "once the phone is connected."), file=sys.stderr)
            return "stop"
        return ""
    from .phone import quiet_opencv

    if reconnector.state == "idle" and not phone_cam.running and not phone_cam.stopping:
        reconnector.begin(now)
        quiet_opencv(True)  # the open reader's "select() timeout" warnings, every 10 s
    reconnector.check(now)
    for msg in reconnector.take_messages():
        print(msg)
    if reconnector.state == "failed":
        print(reconnector.error, file=sys.stderr)
        return "stop"
    return "reconnecting" if reconnector.active else ""


def stall_message(seconds: float, phone: bool) -> str:
    """What the terminal says while no frames arrive."""
    what, hint = (("the phone", PHONE_STALL_HINT) if phone
                  else ("the camera", CAMERA_STALL_HINT))
    return f"No frames from {what} for {seconds:.0f} s - {hint}"


def _started(future, what: str, verbose: bool = False):
    """The result of a background start (camera, hand tracker). Whatever went wrong
    becomes one readable RuntimeError (reported as "Error: ...", cleanup still runs),
    not a traceback. -v prints the traceback too, for a bug report."""
    try:
        return future.result()
    except (RuntimeError, KeyboardInterrupt):
        raise  # already a readable message (or a quit)
    except Exception as exc:  # noqa: BLE001
        if verbose:
            import traceback

            traceback.print_exception(exc)
        raise RuntimeError(f"{what} couldn't start: {type(exc).__name__}: {exc}") from exc


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="gesture-control",
        description="Control the mouse with hand gestures (MediaPipe + uinput).",
    )
    p.add_argument("--config", help=f"YAML config (default: {DEFAULT_CONFIG_PATH})")
    p.add_argument("--camera", type=int, help="camera index, e.g. 0 for /dev/video0")
    phone = p.add_mutually_exclusive_group()
    phone.add_argument("--phone", action="store_true",
                       help="use an Android phone as the camera: starts scrcpy and stops it "
                            "at exit (sets phone.enabled; README > Phone as the camera)")
    phone.add_argument("--no-phone", action="store_true",
                       help="use the laptop camera even if phone.enabled is set")
    p.add_argument("--connect", metavar="IP:PORT", type=_wifi_address,
                   help="use the phone over Wi-Fi at this address, the 'IP address & "
                        "Port' on its Wireless debugging screen (it changes each time "
                        "that's turned on). Implies --phone; overrides phone.connect")
    p.add_argument("--mode", choices=["absolute", "relative"],
                   help="override cursor.mode (relative: touchpad-like, absolute: hand = screen)")
    p.add_argument("--no-preview", action="store_true", help="run without the preview window")
    p.add_argument("--dry-run", action="store_true",
                   help="don't create a virtual mouse; print the actions instead")
    p.add_argument("--start-paused", action="store_true", help="start with gestures paused")
    p.add_argument("--pointer-test", action="store_true",
                   help="move the cursor around without the camera, then exit")
    p.add_argument("--scroll-test", action="store_true",
                   help="scroll down, up, right, then left under the pointer (no camera), "
                        "then exit")
    p.add_argument("--scroll-method", choices=["touchpad", "wheel"],
                   help="override scroll.method (touchpad: a two-finger swipe, like a "
                        "touchpad; wheel: the mouse wheel)")
    p.add_argument("--zoom-test", action="store_true",
                   help="zoom in then out under the pointer (a real touchpad pinch by "
                        "default; no camera), then exit")
    p.add_argument("--calibrate", action="store_true",
                   help="find where the screen edges are (absolute mode), then exit")
    p.add_argument("--calibrate-trackball", action="store_true",
                   help="measure the trackball output (after its one-time setup), then exit")
    p.add_argument("-v", "--verbose", action="store_true", help="print every action")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    if args.connect and args.no_phone:
        p.error("argument --connect: not allowed with argument --no-phone")
    return args


def _wifi_address(text: str) -> str:
    """--connect's value: IP:PORT, as on the phone's Wireless debugging screen."""
    from .phone import ADDRESS_RE

    if not ADDRESS_RE.match(text):
        raise argparse.ArgumentTypeError(
            f"{text!r} isn't an address like 192.168.1.42:41234 (the 'IP address & Port' "
            "on the phone's Wireless debugging screen)")
    return text


def _make_pointer(mode: str, dry_run: bool, settle: float = 0.5) -> VirtualPointer:
    try:
        return VirtualPointer(mode=mode, dry_run=dry_run, settle=settle)
    except PermissionError:
        sys.exit(
            "Permission denied on /dev/uinput.\n"
            "Check: `groups` lists 'uinput' and `ls -l /dev/uinput` shows group uinput.\n"
            "If you just added the group, log out and back in. Setup: README > Setup, step 3."
        )
    except FileNotFoundError:
        sys.exit("/dev/uinput not found. Run: sudo modprobe uinput "
                 "(and README > Setup, step 3 to load it at every boot)")


def pointer_test(mode: str, dry_run: bool, abs_scale=(1.0, 1.0), ptr=None) -> None:
    """ptr: the pointer the app itself would use (e.g. the virtual trackball), if
    already created; otherwise a plain one for `mode` is made here."""
    if mode == "absolute":
        what = ("visit the centre and then each corner of the screen. If it stops short of "
                "or overshoots the right/bottom corners, run --calibrate")
    else:
        what = "trace a small square"
    print(f"Pointer test ({mode}): the cursor should {what}.")
    if ptr is None:
        ptr = _make_pointer(mode, dry_run)
    try:
        if mode == "absolute":
            sx, sy = abs_scale
            for x, y in [(0.5, 0.5), (0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.5, 0.5)]:
                ptr.move_abs(x * sx, y * sy)
                time.sleep(0.8)
        else:
            for dx, dy in [(15, 0), (0, 15), (-15, 0), (0, -15)]:
                for _ in range(20):
                    ptr.move_rel(dx, dy)
                    time.sleep(0.01)
        if mode == "absolute":
            print("Done. If the cursor did not move, the tablet device isn't accepted: set "
                  "cursor.mode: relative and cursor.relative.output: mouse in "
                  f"{shown('local.yaml')}.")
        else:
            print("Done.")
    finally:
        ptr.close()


def _make_keys(dry_run: bool, settle: float = 0.3) -> VirtualKeys:
    try:
        return VirtualKeys(dry_run=dry_run, settle=settle)
    except PermissionError:
        sys.exit("Permission denied on /dev/uinput: see README > Setup, step 3.")


def _make_touchpad(cfg, dry_run: bool, settle: float = 0.5):
    from .touchpad import VirtualTouchpad

    try:
        return VirtualTouchpad(cfg["zoom"]["touchpad"], dry_run=dry_run, settle=settle)
    except PermissionError:
        sys.exit("Permission denied on /dev/uinput: see README > Setup, step 3.")


def pinch_test(cfg, dry_run: bool) -> None:
    """A real two-finger pinch on the virtual touchpad: out (zoom in), then in."""
    from .touchpad import TouchpadSettings

    settings = TouchpadSettings(start=False)
    if not settings.usable:
        print(settings.message())
        return
    print("Pinch test (touchpad): click into Firefox, an image in Image Viewer, or a PDF in")
    print("Document Viewer now, and leave the mouse pointer over it.")
    print("Tip: in another terminal, `sudo libinput debug-events` shows GESTURE_PINCH lines.")
    for n in (3, 2, 1):
        print(f"  starting in {n}...")
        time.sleep(1)
    tp = _make_touchpad(cfg, dry_run)
    try:
        start = float(cfg["zoom"]["touchpad"]["start_mm"])
        for label, target in (("in", tp.max_sep - 2), ("out", tp.min_sep + 2)):
            print(f"Pinching to zoom {label}...")
            tp.begin(start)
            time.sleep(0.15)
            steps = 40
            for i in range(1, steps + 1):
                tp.move(start + (target - start) * i / steps)
                time.sleep(0.02)
            time.sleep(0.2)
            tp.end()
            time.sleep(0.8)
        print("Done. If nothing zoomed: check `sudo libinput debug-events` for GESTURE_PINCH "
              "lines, and try an app listed in the README (Zoom section).")
    finally:
        tp.close()


SCROLL_TEST_STEPS = (("down", 0, -1), ("up", 0, 1), ("right", 1, 0), ("left", -1, 0))


def _make_scroller(cfg, pointer, settings, dry_run: bool, settle: float = 0.0):
    """Scrolling as a touchpad swipe (scroll.method: touchpad) or the mouse wheel."""
    from .scrolling import PadScroller, Scroller

    if cfg["scroll"]["method"] != "touchpad":
        return Scroller(pointer)
    try:
        pad = PadScroller(cfg["scroll"]["touchpad"]["mm_per_notch"], settings,
                          dry_run=dry_run, settle=settle)
    except PermissionError:
        sys.exit("Permission denied on /dev/uinput: see README > Setup, step 3.")
    if settings is not None and not settings.scroll_usable:
        print(settings.scroll_message())
    return Scroller(pointer, pad, settings)


def scroll_test(scroller, wait=time.sleep, clock=time.monotonic) -> None:
    """Scroll 5 notches' worth each way, down, up, right, left, under the pointer, the
    way the app scrolls (touchpad swipe or mouse wheel): checks that it reaches your
    apps and how far it goes."""
    print(f"Scroll test ({scroller.method}): put the mouse pointer over a page or image "
          "that scrolls both ways (a zoomed-in picture, a pinch-zoomed web page, a map) now.")
    for n in (3, 2, 1):
        print(f"  starting in {n}...")
        wait(1)
    try:
        for label, sx, sy in SCROLL_TEST_STEPS:
            print(f"Scrolling {label}...")
            for _ in range(25):                  # 5 notches as 25 smooth steps
                scroller.scroll(sx * 24, sy * 24)
                scroller.flush(clock())
                wait(0.02)
            for _ in range(30):                  # pause: the fingers stop, then lift
                scroller.flush(clock())
                wait(0.02)
        print("Done. The content should have gone down and back up, then right and back "
              "left. To compare with the mouse wheel, run it again with "
              "--scroll-method wheel (or touchpad).")
        print("If only sideways failed, in Firefox on a page with its own scroll panel "
              "(an ongoing chat, a web app): Firefox keeps scrolling the panel for 1.5 s "
              "after up/down scrolling. README > Scrolling > If scrolling doesn't behave.")
    finally:
        scroller.close()
        scroller.pointer.close()


def zoom_test(cfg, dry_run: bool) -> None:
    method = cfg["zoom"]["method"]
    if method == "touchpad":
        pinch_test(cfg, dry_run)
        return
    print(f"Zoom test ({method}): click into a browser or image viewer window now.")
    for n in (3, 2, 1):
        print(f"  starting in {n}...")
        time.sleep(1)
    ptr = _make_pointer(pointer_device_mode(cfg), dry_run)
    keys = _make_keys(dry_run)
    try:
        for direction, label in ((1, "in"), (-1, "out")):
            print(f"Zooming {label} (3 steps)...")
            for _ in range(3):
                if method == "ctrl_scroll":
                    keys.press("ctrl")
                    for _ in range(6):  # one notch as 6 smooth steps
                        ptr.scroll(direction * 20)
                        time.sleep(0.02)
                    keys.release("ctrl")
                else:
                    keys.press("ctrl")
                    keys.tap("equal" if direction > 0 else "minus")
                    keys.release("ctrl")
                time.sleep(0.4)
            time.sleep(0.6)
        print(f"Done. If nothing zoomed, try: {command()} --zoom-test "
              f"with zoom.method: keys in {shown('local.yaml')}")
    finally:
        keys.close()
        ptr.close()


# A hand can't really move this far between two frames (about 4 m/s at 30 fps); a jump
# like this is the tracker switching hands or glitching, not movement.
MAX_JUMP_PALMS = 1.5


def _is_switch(anchor, prev_anchor, prev_other, palm) -> bool:
    """Did the primary hand change identity (or jump impossibly far) since last frame?"""
    if anchor is None or prev_anchor is None:
        return False
    d_prev = ((anchor[0] - prev_anchor[0]) ** 2 + (anchor[1] - prev_anchor[1]) ** 2) ** 0.5
    if prev_other is not None:
        d_other = ((anchor[0] - prev_other[0]) ** 2 + (anchor[1] - prev_other[1]) ** 2) ** 0.5
        if d_other < d_prev:
            return True  # we're now following the hand that used to be the other one
    return palm is not None and palm > 1e-4 and d_prev / palm > MAX_JUMP_PALMS


def _resolve_aspect(cfg, disp) -> None:
    """cursor.relative.screen_aspect: auto -> the screen's own width / height."""
    rc = cfg["cursor"]["relative"]
    if rc["screen_aspect"] == "auto":
        rc["screen_aspect"] = disp.aspect if disp is not None else 1.6


def _resolve_output(cfg, dry_run: bool, disp=None):
    """Settle cursor.relative.output ("auto" -> trackball or tablet) and, for the
    trackball, its movement units per screen. Returns the trackball pointer if that's
    what will be used (already created and verified)."""
    from . import trackball as tb

    rc = cfg["cursor"]["relative"]
    want = rc["output"]
    if cfg["cursor"]["mode"] != "relative" or want not in ("auto", "trackball"):
        if want == "auto":
            rc["output"] = "tablet"
        return None
    missing = tb.missing_steps(cfg)
    auto = tb.auto_counts(disp)
    if not tb.calibrated(cfg) and auto is None:
        missing.append("couldn't read the screen size and scale from GNOME; run "
                       f"{command()} --calibrate-trackball once instead")
    ptr = None
    if not missing:
        try:
            ptr = VirtualPointer(mode="relative", dry_run=dry_run, name=tb.DEVICE_NAME,
                                 settle=0.0)
        except PermissionError:
            sys.exit("Permission denied on /dev/uinput: see README > Setup, step 3.")
        ptr_path = None if dry_run else ptr.device_path
        if not dry_run and not tb.device_tagged(ptr_path):
            ptr.close()
            ptr = None
            missing = [(f"udev didn't tag the virtual trackball ({ptr_path}) "
                        "(run: sudo udevadm control --reload-rules)")]
    if missing:
        if want == "trackball":
            sys.exit("cursor.relative.output is trackball, but its setup isn't complete:\n"
                     + "".join(f"  - {m}\n" for m in missing)
                     + "Run these once:\n" + tb.SETUP_COMMANDS)
        rc["output"] = "tablet"
        print("Tip: the cursor jumps back after you use your real mouse or touchpad. A "
              "one-time setup fixes that: README > Using your mouse and gestures together "
              "(or set cursor.relative.output: tablet to hide this tip).")
        return None
    rc["output"] = "trackball"
    if tb.calibrated(cfg):
        cx, cy = (float(v) for v in rc["trackball_counts"])
        rc["_counts_source"] = f"{cx:.0f}x{cy:.0f} units, calibrated"
        saved, now = rc.get("trackball_mouse_speed"), tb.mouse_speed()
        if saved is not None and now is not None and abs(float(saved) - now) > 1e-6:
            print("Note: GNOME's mouse speed changed since the trackball calibration, so "
                  "gestures move the cursor a different distance. Delete the relative: "
                  f"block from {shown('calibration.yaml')} to use the automatic value.")
        elif auto is not None and max(abs(cx / auto[0] - 1), abs(cy / auto[1] - 1)) > 0.03:
            print(f"Note: the trackball calibration ({cx:.0f} x {cy:.0f}) differs from what "
                  f"GNOME reports ({auto[0]:.0f} x {auto[1]:.0f} for {disp.describe()}). "
                  "Using the calibration; delete the relative: block from "
                  f"{shown('calibration.yaml')} to use the automatic value.")
    else:
        rc["trackball_counts"] = [auto[0], auto[1]]
        rc["_counts_source"] = f"{auto[0]:.0f}x{auto[1]:.0f} units, auto"
    return ptr


def _pick_primary(hands, prev_anchor, anchor_name):
    """The hand that drives one-hand gestures: the one nearest to where the previous
    primary hand was, so a second hand coming into view doesn't steal the cursor."""
    from .features import anchor_point

    if len(hands) < 2 or prev_anchor is None:
        return hands[0] if hands else None

    def dist(h):
        ax, ay = anchor_point(h.image, anchor_name)
        return (ax - prev_anchor[0]) ** 2 + (ay - prev_anchor[1]) ** 2

    return min(hands[:2], key=dist)


class ShutdownGuard:
    """Ctrl+C, closing the terminal (SIGHUP) and `kill` (SIGTERM) all stop the app the
    same clean way (a KeyboardInterrupt), so held buttons, keys and touchpad fingers
    are released and the camera's settings put back.

    Installed first thing in main(), so a signal during the model download or startup
    is handled too. Once cleanup has begun (cleaning()), another signal must not cut
    it short: the first repeat only says it's still stopping; a second one quits at
    once (the kernel still removes the virtual devices, releasing what they held)."""

    SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    DOING = "releasing buttons, closing the camera"
    TRACKER_LOADING = "waiting for the hand tracker to finish loading, a few seconds"

    def __init__(self, exit_now=os._exit, say=print):
        self._exit_now, self._say = exit_now, say
        self._cleaning = False
        self._repeats = 0
        self._old: dict[int, object] = {}
        self.doing = self.DOING       # what cleanup is busy with, for the message
        # Run on a forced quit, just before exiting: things the kernel won't clean up
        # (scrcpy runs in its own process group, so Ctrl+C never reaches it).
        self.on_force_quit: list = []

    def install(self) -> None:
        for sig in self.SIGNALS:
            self._old[sig] = signal.signal(sig, self._handle)

    def restore(self) -> None:
        for sig, old in self._old.items():
            signal.signal(sig, old)
        self._old.clear()

    def cleaning(self) -> None:
        self._cleaning = True

    def _handle(self, signum, _frame) -> None:
        if not self._cleaning:
            raise KeyboardInterrupt
        self._repeats += 1
        if self._repeats == 1:
            try:  # the terminal may already be gone (SIGHUP)
                self._say(f"Still stopping ({self.doing})... "
                          "press Ctrl+C again to quit at once.")
            except OSError:
                pass
        else:
            for stop in self.on_force_quit:
                try:
                    stop()
                except Exception:  # noqa: BLE001  (quitting at once whatever happens)
                    pass
            self._exit_now(130)


def main(argv=None) -> None:
    args = parse_args(argv)
    guard = ShutdownGuard()
    guard.install()
    try:
        _main(args, guard)
    except KeyboardInterrupt:  # stopped before the main loop (download, a test mode)
        print("\nStopped.")
    finally:
        guard.restore()


def _main(args: argparse.Namespace, guard: ShutdownGuard) -> None:
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, TypeError) as exc:
        # TypeError: a wrong kind of value that slipped past the checks; still a
        # config problem, so report it the same way rather than with a traceback.
        sys.exit(f"Config error: {exc}")
    for warning in cfg["_warnings"]:
        print(f"Config warning: {warning}")
    if args.connect:  # the Wi-Fi address changes each time: given per run, not saved
        cfg["phone"]["connect"] = args.connect
    if args.phone or args.connect:
        cfg["phone"]["enabled"] = True
    if args.no_phone:
        cfg["phone"]["enabled"] = False
    if cfg["phone"]["enabled"]:
        from .phone import apply_settings

        apply_settings(cfg)  # read the phone's virtual camera, at the size it sends
        node = int(cfg["phone"]["video_device"])
        if args.camera is not None and args.camera != node:
            # scrcpy would send the phone's picture to /dev/video<node> while the app
            # read another camera: the phone would be started but never used.
            sys.exit(f"--camera {args.camera} can't be used with the phone camera, which "
                     f"is /dev/video{node} (phone.video_device). For the laptop camera, "
                     f"add --no-phone: {command()} --no-phone --camera {args.camera}")
    if args.camera is not None:
        cfg["camera"]["index"] = args.camera
    if args.scroll_method:
        cfg["scroll"]["method"] = args.scroll_method
    if args.mode:
        cfg["cursor"]["mode"] = args.mode
    if args.no_preview:
        cfg["preview"]["enabled"] = False
    if args.start_paused:
        cfg["pause"]["start_paused"] = True

    if args.calibrate:
        from .calibrate import run_calibration

        run_calibration(args.dry_run, args.config)
        return
    if args.calibrate_trackball:
        from .trackball import run_calibration as run_trackball_calibration

        run_trackball_calibration(cfg, args.dry_run, args.config)
        return
    if args.scroll_test:
        from .display import primary_display
        from .touchpad import TouchpadSettings

        ptr = _resolve_output(cfg, args.dry_run, primary_display())
        if ptr is None:
            ptr = _make_pointer(pointer_device_mode(cfg), args.dry_run, settle=0.5)
        settings = (TouchpadSettings(start=False) if cfg["scroll"]["method"] == "touchpad"
                    else None)
        scroll_test(_make_scroller(cfg, ptr, settings, args.dry_run, settle=0.5))
        return
    if args.zoom_test:
        zoom_test(cfg, args.dry_run)
        return
    if args.pointer_test:
        # Test the device the app will really use: settle cursor.relative.output
        # ("auto" -> trackball or tablet) the same way a normal start does.
        from .display import primary_display

        ptr = _resolve_output(cfg, args.dry_run, primary_display())
        pointer_test(pointer_device_mode(cfg), args.dry_run, cfg["cursor"]["abs_scale"], ptr)
        return

    from .model import ModelError, ensure_model

    # First start: download the hand model (checked against its SHA-256). Done before
    # anything else starts, so a failed download leaves nothing half set up.
    try:
        ensure_model(cfg["tracker"]["model"])
    except ModelError as exc:
        sys.exit(str(exc))
    except KeyboardInterrupt:
        sys.exit("\nDownload cancelled; nothing was changed.")  # .part file removed

    # Startup runs in parallel: the camera (slow to power up) and the hand tracker
    # (big imports, model load, first inference) start in background threads while the
    # virtual devices are created and a placeholder window is shown.
    from concurrent.futures import ThreadPoolExecutor

    t0 = time.monotonic()
    timings: dict[str, float] = {}

    def timed(name, fn):
        def run():
            t = time.monotonic()
            try:
                return fn()
            finally:
                timings[name] = time.monotonic() - t
        return run

    phone_cam = reconnector = None
    if cfg["phone"]["enabled"]:
        from .phone import PhoneCamera, Reconnector, quiet_opencv

        phone_cam = PhoneCamera(cfg["phone"], verbose=args.verbose)
        guard.on_force_quit.append(phone_cam.kill_now)  # never leave scrcpy running
        if cfg["phone"]["reconnect"]:
            reconnector = Reconnector(phone_cam)

    def start_camera():
        from .capture import Camera

        tries = 1
        if phone_cam is not None:
            phone_cam.start()  # scrcpy: the phone's picture into the virtual camera
            tries = PHONE_OPEN_TRIES  # in case the first frames are slow to settle
        for attempt in range(1, tries + 1):
            cam = Camera(cfg["camera"])
            try:
                cam.start()
                return cam
            except Exception:
                cam.stop()
                if attempt == tries or (phone_cam is not None and not phone_cam.running):
                    raise
                time.sleep(PHONE_OPEN_RETRY_S)

    def load_tracker():
        from .tracker import HandTracker

        trk = HandTracker(cfg["tracker"])
        trk.warm_up(int(cfg["camera"]["width"]), int(cfg["camera"]["height"]))
        return trk

    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="startup")
    camera_f = tracker_f = None
    camera = tracker = overlay = None
    pointer = keys = touchpad = tp_settings = scroller = None
    try:
        camera_f = pool.submit(timed("camera", start_camera))
        tracker_f = pool.submit(timed("hand tracker", load_tracker))
        t = time.monotonic()
        from .controller import GestureController
        from .display import primary_display
        from .features import (
            POINT,
            anchor_point,
            compute_features,
            hand_spread,
            palm_length,
        )
        from .touchpad import TouchpadSettings

        disp = primary_display()
        _resolve_aspect(cfg, disp)
        pointer = _resolve_output(cfg, args.dry_run, disp)
        device = pointer_device_mode(cfg)
        if pointer is None:
            pointer = _make_pointer(device, args.dry_run, settle=0.0)
        zoom_on = bool(cfg["zoom"]["enabled"])
        method = cfg["zoom"]["method"]
        if zoom_on and method != "touchpad":
            keys = _make_keys(args.dry_run, settle=0.0)
        if (zoom_on and method == "touchpad") or cfg["scroll"]["method"] == "touchpad":
            tp_settings = TouchpadSettings()  # GNOME's touchpad settings apply to both
        if zoom_on and method == "touchpad":
            touchpad = _make_touchpad(cfg, args.dry_run, settle=0.0)
            if not tp_settings.usable:
                print(tp_settings.message())
        elif (tp_settings.state if tp_settings else TouchpadSettings.read()) \
                == "disabled-on-external-mouse":
            print(TouchpadSettings.mouse_note())
        scroller = _make_scroller(cfg, pointer, tp_settings, args.dry_run)
        timings["devices"] = time.monotonic() - t

        preview = bool(cfg["preview"]["enabled"])
        if preview:
            from .overlay import Overlay
        # A quick start opens the window straight onto the camera image; only a slow one
        # (more than PLACEHOLDER_AFTER_S) shows a "Starting..." message first.
        while not (camera_f.done() and tracker_f.done()):
            if preview and overlay is None and time.monotonic() - t0 >= PLACEHOLDER_AFTER_S:
                overlay = Overlay(cfg)
                overlay.placeholder("Starting camera and hand tracker...")
            if overlay:
                key = overlay.poll(wait_ms=30)
                if key in (ord("q"), 27) or overlay.closed():
                    raise KeyboardInterrupt
            else:
                time.sleep(0.02)
        if preview and overlay is None:
            overlay = Overlay(cfg)
        if overlay and phone_cam is not None:
            overlay.stall_hint = PHONE_STALL_HINT
        camera = _started(camera_f, "The camera", args.verbose)  # re-raises its error
        tracker = _started(tracker_f, "The hand tracker", args.verbose)
        from .tracker import feature_points

        w, h, fps_set = camera.actual
        out_name = (cfg["cursor"]["mode"] if cfg["cursor"]["mode"] == "absolute"
                    else f"relative ({cfg['cursor']['relative']['output']}"
                         + (f", {cfg['cursor']['relative']['_counts_source']}"
                            if "_counts_source" in cfg["cursor"]["relative"] else "")
                         + ")")
        print(f"Camera /dev/video{camera.index}: {w}x{h} @ {fps_set:.0f} fps | "
              f"cursor: {out_name} | {'DRY RUN' if args.dry_run else 'live'}")
        if cfg["_loaded"]:
            folder = Path(cfg["_config_dir"])
            print("Also loaded: " + ", ".join(shown(n, folder) for n in cfg["_loaded"]))
        if device == "absolute" and "calibration.yaml" not in cfg["_loaded"]:
            print("Tip: if the cursor reaches the right/bottom screen edge too early, "
                  f"run: {command()} --calibrate")
        controller = GestureController(cfg, pointer, keys, touchpad, tp_settings, scroller)
        controller.set_frame_size(w, h)  # the size the camera gives, not the one asked for
        if (w, h) != (int(cfg["camera"]["width"]), int(cfg["camera"]["height"])) and w > 0:
            print(f"Note: the camera doesn't offer {cfg['camera']['width']}x"
                  f"{cfg['camera']['height']}, so it runs at {w}x{h} (camera.width/height).")
        if cfg["zoom"]["enabled"] and int(cfg["tracker"]["num_hands"]) < 2:
            print("Note: zoom needs tracker.num_hands: 2 - two-hand zoom is off.")
        if overlay:
            print("Preview: q / Esc quit, p pause/resume. Hold a fist ~1 s to pause/resume.")
        else:
            print("Running without preview. Ctrl+C to quit. Hold a fist ~1 s to pause/resume.")
        first_frame = True

        ext, curl = cfg["pose"]["extend_ratio"], cfg["pose"]["curl_ratio"]
        anchor_name = cfg["cursor"]["anchor"]
        mirror = bool(cfg["camera"]["mirror"])
        seq, fps, t_fps, n_fps = 0, 0.0, time.monotonic(), 0
        prev_anchor = prev_other = None
        stall_since = stall_said = None

        while True:
            item = camera.read(seq, timeout=1.0)
            if item is None:
                # No frame for a second: let go of buttons, Ctrl and touchpad fingers
                # (and perform a scheduled finger lift).
                status = controller.stalled(time.monotonic())
                if args.verbose:
                    for ev in status.events:
                        print(f"{time.strftime('%H:%M:%S')} {ev}")
                now = time.monotonic()
                prev_anchor = prev_other = None  # the hands are found afresh after it
                phone_state = ""
                if phone_cam is not None:
                    phone_state = phone_stalled(phone_cam, reconnector, now)
                    if phone_state == "stop":
                        break
                if stall_since is None:
                    stall_since = now - 1.0  # the read above already waited 1 s
                    stall_said = stall_since
                if phone_state == "reconnecting":
                    stall_said = now  # its own messages say what's happening
                elif now - stall_said > 5:
                    print(stall_message(now - stall_since, phone_cam is not None))
                    stall_said = now
                if overlay and phone_cam is not None:
                    overlay.stall_hint = (RECONNECTING_HINT if phone_state == "reconnecting"
                                          else PHONE_STALL_HINT)
                if overlay:
                    # Say so in the window (it would otherwise just freeze) and keep it
                    # responsive (q / Esc / close) even without frames.
                    key = overlay.show_stalled(now - stall_since)
                    if key in (ord("q"), 27) or overlay.closed():
                        break
                continue
            stall_since = None
            if reconnector is not None and reconnector.frames_back(time.monotonic()):
                for msg in reconnector.take_messages():
                    print(msg)
                quiet_opencv(False)
            frame, cap_ts, seq = item
            if mirror:
                frame = frame[:, ::-1].copy()  # mirror before detection: handedness stays correct

            hands = tracker.process_all(frame, int(cap_ts * 1000))
            hand = _pick_primary(hands, prev_anchor, anchor_name)
            other = next((h for h in hands if h is not hand), None)
            feats = anchor = palm = zoom_in = None
            switched = False
            fh, fw = frame.shape[:2]
            controller.set_frame_size(fw, fh)  # no-op unless the size changed
            if hand is not None:
                feats = compute_features(feature_points(hand, fw, fh), ext, curl)
                anchor = anchor_point(hand.image, anchor_name)
                palm = palm_length(hand.image, fw, fh)
                switched = _is_switch(anchor, prev_anchor, prev_other, palm)
                prev_anchor = anchor
            prev_other = anchor_point(other.image, anchor_name) if other is not None else None
            if hand is not None and other is not None:
                other_feats = compute_features(feature_points(other, fw, fh), ext, curl)
                both_point = feats.pose == POINT and other_feats.pose == POINT
                zoom_in = (both_point, hand_spread(hand.image, other.image, fw, fh))
            now = time.monotonic()
            # The time the frame was captured, not now: hand tracking takes a varying
            # time (7.7-62.7 ms between "now"s for frames 31-37 ms apart, measured),
            # and that noise would go straight into the hand-speed estimates.
            status = controller.update(feats, anchor, cap_ts, palm, zoom_in,
                                       rebase=switched)
            latency_ms = (time.monotonic() - cap_ts) * 1000

            n_fps += 1
            if now - t_fps >= 1.0:
                fps, t_fps, n_fps = n_fps / (now - t_fps), now, 0

            if args.verbose:
                for ev in status.events:
                    print(f"{time.strftime('%H:%M:%S')} {ev}")
            if status.zoom_blocked_new:
                print(status.zoom_blocked)  # the preview points here for the details

            if overlay:
                overlay.draw(frame, hand, status, fps, latency_ms,
                             hand.handedness if hand else "", anchor, other)
                key = overlay.show(frame)
            if first_frame:
                first_frame = False
                if args.verbose:
                    parts = ", ".join(f"{k} {v:.1f} s" for k, v in timings.items())
                    print(f"Startup: first frame shown {time.monotonic() - t0:.1f} s after "
                          f"the app started ({parts}; these run at the same time)")
            if overlay:
                if key in (ord("q"), 27) or overlay.closed():
                    break
                if key == ord("p"):
                    controller.toggle_pause()
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
    finally:
        guard.cleaning()  # from here, a repeated Ctrl+C can't cut the cleanup short
        if keys:
            keys.close()  # releases Ctrl if it was held
        if touchpad:
            touchpad.close()  # lifts the virtual fingers
        if scroller:
            scroller.close()  # lifts the scroll pad's fingers
        if tp_settings:
            tp_settings.stop()
        if pointer:
            pointer.close()
        if overlay:
            overlay.close()
        # Quitting during startup: let the background start finish, then undo it.
        if phone_cam is not None and camera_f is not None and not camera_f.done():
            phone_cam.stop()  # don't wait for the phone: its start fails right away
        if tracker_f is not None and not tracker_f.done():
            guard.doing = guard.TRACKER_LOADING  # it can't be cut short; say so
        for fut, close in ((camera_f, "stop"), (tracker_f, "close")):
            if fut is None:  # stopped before it was started
                continue
            try:
                obj = fut.result()
            except Exception:  # noqa: BLE001  (already reported, or never used)
                obj = None
            if obj is not None:
                getattr(obj, close)()
        pool.shutdown()
        guard.doing = guard.DOING
        if phone_cam is not None:
            phone_cam.stop()  # after the camera: it reads scrcpy's virtual camera
        if reconnector is not None:
            reconnector.stop()  # (phone_cam.stop() already ended an attempt under way)
            quiet_opencv(False)
        print("Stopped.")


if __name__ == "__main__":
    main()
