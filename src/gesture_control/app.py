"""Main loop: camera -> hand landmarks -> features -> state machine -> virtual mouse."""

from __future__ import annotations

import argparse
import signal
import sys
import time

from .config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, load_config, pointer_device_mode
from .pointer import VirtualKeys, VirtualPointer

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/latest/hand_landmarker.task"
)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="gesture-control",
        description="Control the mouse with hand gestures (MediaPipe + uinput).",
    )
    p.add_argument("--config", help=f"YAML config (default: {DEFAULT_CONFIG_PATH})")
    p.add_argument("--camera", type=int, help="camera index, e.g. 0 for /dev/video0")
    p.add_argument("--mode", choices=["absolute", "relative"],
                   help="override cursor.mode (relative: touchpad-like, absolute: hand = screen)")
    p.add_argument("--no-preview", action="store_true", help="run without the preview window")
    p.add_argument("--dry-run", action="store_true",
                   help="don't create a virtual mouse; print the actions instead")
    p.add_argument("--start-paused", action="store_true", help="start with gestures paused")
    p.add_argument("--pointer-test", action="store_true",
                   help="move the cursor around without the camera, then exit")
    p.add_argument("--zoom-test", action="store_true",
                   help="zoom in then out under the pointer (a real touchpad pinch by "
                        "default; no camera), then exit")
    p.add_argument("--calibrate", action="store_true",
                   help="find where the screen edges are (absolute mode), then exit")
    p.add_argument("--calibrate-trackball", action="store_true",
                   help="measure the trackball output (after its one-time setup), then exit")
    p.add_argument("-v", "--verbose", action="store_true", help="print every action")
    return p.parse_args(argv)


def _make_pointer(mode: str, dry_run: bool) -> VirtualPointer:
    try:
        return VirtualPointer(mode=mode, dry_run=dry_run)
    except PermissionError:
        sys.exit(
            "Permission denied on /dev/uinput.\n"
            "Check: `groups` lists 'uinput' and `ls -l /dev/uinput` shows group uinput.\n"
            "If you just added the group, log out and back in."
        )
    except FileNotFoundError:
        sys.exit("/dev/uinput not found. Run: sudo modprobe uinput")


def pointer_test(mode: str, dry_run: bool, abs_scale=(1.0, 1.0)) -> None:
    if mode == "absolute":
        what = ("visit the centre and then each corner of the screen. If it stops short of "
                "or overshoots the right/bottom corners, run --calibrate")
    else:
        what = "trace a small square"
    print(f"Pointer test ({mode}): the cursor should {what}.")
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
                  "cursor.mode: relative and cursor.relative.output: mouse in config/local.yaml.")
        else:
            print("Done.")
    finally:
        ptr.close()


def _make_keys(dry_run: bool) -> VirtualKeys:
    try:
        return VirtualKeys(dry_run=dry_run)
    except PermissionError:
        sys.exit("Permission denied on /dev/uinput (see the setup steps in the README).")


def _make_touchpad(cfg, dry_run: bool):
    from .touchpad import VirtualTouchpad

    try:
        return VirtualTouchpad(cfg["zoom"]["touchpad"], dry_run=dry_run)
    except PermissionError:
        sys.exit("Permission denied on /dev/uinput (see the setup steps in the README).")


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
        print("Done. If nothing zoomed, try: uv run gesture-control --zoom-test "
              "with zoom.method: keys in config/local.yaml")
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


def _resolve_output(cfg, dry_run: bool):
    """Settle cursor.relative.output ("auto" -> trackball or tablet). Returns the
    trackball pointer if that's what will be used (already created and verified)."""
    from . import trackball as tb

    rc = cfg["cursor"]["relative"]
    want = rc["output"]
    if cfg["cursor"]["mode"] != "relative" or want not in ("auto", "trackball"):
        if want == "auto":
            rc["output"] = "tablet"
        return None
    missing = tb.missing_steps(cfg)
    ptr = None
    if not missing:
        try:
            ptr = VirtualPointer(mode="relative", dry_run=dry_run, name=tb.DEVICE_NAME)
        except PermissionError:
            sys.exit("Permission denied on /dev/uinput (see the setup steps in the README).")
        if not dry_run and not tb.device_tagged(ptr.device_path):
            ptr.close()
            ptr = None
            missing = [("udev didn't tag the virtual trackball "
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
    saved, now = rc.get("trackball_mouse_speed"), tb.mouse_speed()
    if saved is not None and now is not None and abs(float(saved) - now) > 1e-6:
        print("Note: GNOME's mouse speed changed since the trackball calibration, so "
              "gestures move the cursor a different distance. Re-run: "
              "uv run gesture-control --calibrate-trackball")
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


def main(argv=None) -> None:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        sys.exit(f"Config error: {exc}")
    for warning in cfg["_warnings"]:
        print(f"Config warning: {warning}")
    if args.camera is not None:
        cfg["camera"]["index"] = args.camera
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
    if args.zoom_test:
        zoom_test(cfg, args.dry_run)
        return
    if args.pointer_test:
        pointer_test(pointer_device_mode(cfg), args.dry_run, cfg["cursor"]["abs_scale"])
        return

    from pathlib import Path

    model = Path(cfg["tracker"]["model"])
    if not model.exists():
        sys.exit(
            f"Model not found: {model}\nDownload it with:\n"
            f"  mkdir -p {PROJECT_ROOT / 'models'} && wget -O {model} \\\n    {MODEL_URL}"
        )

    # Heavy imports after argument handling so --help and --pointer-test stay fast.
    from .capture import Camera
    from .controller import GestureController
    from .features import (
        POINT,
        anchor_point,
        compute_features,
        hand_spread,
        palm_length,
    )
    from .tracker import HandTracker, feature_points

    pointer = _resolve_output(cfg, args.dry_run)
    device = pointer_device_mode(cfg)
    if pointer is None:
        pointer = _make_pointer(device, args.dry_run)
    zoom_on = bool(cfg["zoom"]["enabled"])
    method = cfg["zoom"]["method"]
    keys = _make_keys(args.dry_run) if zoom_on and method != "touchpad" else None
    touchpad = tp_settings = None
    from .touchpad import TouchpadSettings

    if zoom_on and method == "touchpad":
        touchpad = _make_touchpad(cfg, args.dry_run)
        tp_settings = TouchpadSettings()
        if not tp_settings.usable:
            print(tp_settings.message())
    elif TouchpadSettings.read() == "disabled-on-external-mouse":
        print(TouchpadSettings.mouse_note())

    def _stop_on_signal(signum, _frame):
        # Closing the terminal (SIGHUP) or `kill` (SIGTERM): shut down cleanly like
        # Ctrl+C, so held keys/buttons/fingers are released and the camera restored.
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _stop_on_signal)
    camera = Camera(cfg["camera"])
    tracker = None
    overlay = None
    try:
        camera.start()
        w, h, fps_set = camera.actual
        out_name = (cfg["cursor"]["mode"] if cfg["cursor"]["mode"] == "absolute"
                    else f"relative ({cfg['cursor']['relative']['output']})")
        print(f"Camera /dev/video{camera.index}: {w}x{h} @ {fps_set:.0f} fps | "
              f"cursor: {out_name} | {'DRY RUN' if args.dry_run else 'live'}")
        if cfg["_loaded"]:
            print("Also loaded: " + ", ".join(f"config/{n}" for n in cfg["_loaded"]))
        if device == "absolute" and "calibration.yaml" not in cfg["_loaded"]:
            print("Tip: if the cursor reaches the right/bottom screen edge too early, "
                  "run: uv run gesture-control --calibrate")
        tracker = HandTracker(cfg["tracker"])
        controller = GestureController(cfg, pointer, keys, touchpad, tp_settings)
        if cfg["zoom"]["enabled"] and int(cfg["tracker"]["num_hands"]) < 2:
            print("Note: zoom needs tracker.num_hands: 2 - two-hand zoom is off.")
        if cfg["preview"]["enabled"]:
            from .overlay import Overlay

            overlay = Overlay(cfg)
            print("Preview: q / Esc quit, p pause/resume. Hold a fist ~1 s to pause/resume.")
        else:
            print("Running without preview. Ctrl+C to quit. Hold a fist ~1 s to pause/resume.")

        ext, curl = cfg["pose"]["extend_ratio"], cfg["pose"]["curl_ratio"]
        anchor_name = cfg["cursor"]["anchor"]
        mirror = bool(cfg["camera"]["mirror"])
        seq, fps, t_fps, n_fps = 0, 0.0, time.monotonic(), 0
        prev_anchor = prev_other = None
        stall_since = None

        while True:
            item = camera.read(seq, timeout=1.0)
            if item is None:
                controller.tick()  # e.g. a scheduled finger lift
                stall_since = stall_since or time.monotonic()
                if time.monotonic() - stall_since > 5:
                    print("No frames from the camera for 5 s - is another app using it?")
                    stall_since = time.monotonic()
                if overlay:
                    # Keep the window responsive (q / Esc / close) even without frames.
                    key = overlay.poll()
                    if key in (ord("q"), 27) or overlay.closed():
                        break
                continue
            stall_since = None
            frame, cap_ts, seq = item
            if mirror:
                frame = frame[:, ::-1].copy()  # mirror before detection: handedness stays correct

            hands = tracker.process_all(frame, int(cap_ts * 1000))
            hand = _pick_primary(hands, prev_anchor, anchor_name)
            other = next((h for h in hands if h is not hand), None)
            feats = anchor = palm = zoom_in = None
            switched = False
            fh, fw = frame.shape[:2]
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
            status = controller.update(feats, anchor, now, palm, zoom_in, rebase=switched)
            latency_ms = (time.monotonic() - cap_ts) * 1000

            n_fps += 1
            if now - t_fps >= 1.0:
                fps, t_fps, n_fps = n_fps / (now - t_fps), now, 0

            if args.verbose:
                for ev in status.events:
                    print(f"{time.strftime('%H:%M:%S')} {ev}")
            if status.zoom_blocked and any("unavailable" in ev for ev in status.events):
                print(status.zoom_blocked)  # the preview points here for the details

            if overlay:
                overlay.draw(frame, hand, status, fps, latency_ms,
                             hand.handedness if hand else "", anchor, other)
                key = overlay.show(frame)
                if key in (ord("q"), 27) or overlay.closed():
                    break
                if key == ord("p"):
                    controller.toggle_pause()
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
    finally:
        if keys:
            keys.close()  # releases Ctrl if it was held
        if touchpad:
            touchpad.close()  # lifts the virtual fingers
        if tp_settings:
            tp_settings.stop()
        pointer.close()
        camera.stop()
        if tracker:
            tracker.close()
        if overlay:
            overlay.close()
        print("Stopped.")


if __name__ == "__main__":
    main()
