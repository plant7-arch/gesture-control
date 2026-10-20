"""Main loop: camera -> hand landmarks -> features -> state machine -> virtual mouse."""

from __future__ import annotations

import argparse
import sys
import time

from .config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, load_config
from .pointer import VirtualPointer

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
    p.add_argument("--mode", choices=["absolute", "relative"], help="override cursor.mode")
    p.add_argument("--no-preview", action="store_true", help="run without the preview window")
    p.add_argument("--dry-run", action="store_true",
                   help="don't create a virtual mouse; print the actions instead")
    p.add_argument("--start-paused", action="store_true", help="start with gestures paused")
    p.add_argument("--pointer-test", action="store_true",
                   help="move the cursor around without the camera, then exit")
    p.add_argument("--calibrate", action="store_true",
                   help="find where the screen edges are (absolute mode), then exit")
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
        print("Done. If the cursor did not move in absolute mode, set cursor.mode: relative "
              "in config/gestures.yaml (or run with --mode relative).")
    finally:
        ptr.close()


def main(argv=None) -> None:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        sys.exit(f"Config error: {exc}")
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
    if args.pointer_test:
        pointer_test(cfg["cursor"]["mode"], args.dry_run, cfg["cursor"]["abs_scale"])
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
    from .features import anchor_point, compute_features
    from .tracker import HandTracker, feature_points

    pointer = _make_pointer(cfg["cursor"]["mode"], args.dry_run)
    camera = Camera(cfg["camera"])
    tracker = None
    overlay = None
    try:
        camera.start()
        w, h, fps_set = camera.actual
        print(f"Camera /dev/video{camera.index}: {w}x{h} @ {fps_set:.0f} fps | "
              f"cursor: {cfg['cursor']['mode']} | {'DRY RUN' if args.dry_run else 'live'}")
        if cfg["_loaded"]:
            print("Also loaded: " + ", ".join(f"config/{n}" for n in cfg["_loaded"]))
        if cfg["cursor"]["mode"] == "absolute" and "calibration.yaml" not in cfg["_loaded"]:
            print("Tip: if the cursor reaches the right/bottom screen edge too early, "
                  "run: uv run gesture-control --calibrate")
        tracker = HandTracker(cfg["tracker"])
        controller = GestureController(cfg, pointer)
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
        stall_since = None

        while True:
            item = camera.read(seq, timeout=1.0)
            if item is None:
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

            hand = tracker.process(frame, int(cap_ts * 1000))
            feats = anchor = None
            if hand is not None:
                fh, fw = frame.shape[:2]
                feats = compute_features(feature_points(hand, fw, fh), ext, curl)
                anchor = anchor_point(hand.image, anchor_name)
            now = time.monotonic()
            status = controller.update(feats, anchor, now)
            latency_ms = (time.monotonic() - cap_ts) * 1000

            n_fps += 1
            if now - t_fps >= 1.0:
                fps, t_fps, n_fps = n_fps / (now - t_fps), now, 0

            if args.verbose:
                for ev in status.events:
                    print(f"{time.strftime('%H:%M:%S')} {ev}")

            if overlay:
                overlay.draw(frame, hand, status, fps, latency_ms,
                             hand.handedness if hand else "", anchor)
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
        pointer.close()
        camera.stop()
        if tracker:
            tracker.close()
        if overlay:
            overlay.close()
        print("Stopped.")


if __name__ == "__main__":
    main()
