# gesture-control

Control your mouse with hand gestures from a webcam. Uses MediaPipe Hand Landmarker for
tracking and a virtual uinput device for input, so it works on Wayland (Ubuntu 26.04 GNOME).

## Run

```bash
uv run gesture-control --pointer-test   # 1. check the virtual mouse (no camera)
uv run gesture-control --calibrate      # 2. map the screen edges (once per display setup)
uv run gesture-control                  # 3. full app with preview window
```

### Calibration (absolute mode)

GNOME can spread the virtual mouse's range over an area larger than the visible screen
(fractional display scaling does this), so the cursor hits the right/bottom edge long
before your hand reaches the far side of the box. `--calibrate` slowly moves the cursor
towards each edge; press Enter the moment it touches. The result is saved to
`config/calibration.yaml`. Re-run it after changing display scaling, resolution or
monitors, and delete that file to undo it. `--pointer-test` then visits all four screen
corners so you can check the result.

### Your own settings

Builds overwrite `config/gestures.yaml`. Put your changes in `config/local.yaml`
(same structure, only the keys you change). It is loaded last and never shipped in a build.

Useful options:

| Option | What it does |
|---|---|
| `--dry-run` | Track and recognize gestures, but print actions instead of moving the mouse |
| `--mode relative` | Use relative (mouse-like) cursor movement instead of absolute |
| `--calibrate` | Find the screen edges for absolute mode, then exit |
| `--start-paused` | Start paused; hold a fist ~1 s to begin |
| `--no-preview` | Run without the camera window (Ctrl+C to quit) |
| `-v` | Print every action to the terminal |
| `--camera N` | Use `/dev/videoN` |
| `--config PATH` | Use another config file |

In the preview window: `q` / `Esc` quits, `p` toggles pause.

## Gestures

| Gesture | Action |
|---|---|
| Index finger up, other fingers down | Move the cursor |
| ...then pinch thumb + index | Left click; keep pinching and move to drag |
| ...then pinch thumb + middle (index stays up) | Right click |
| Index + middle up (peace sign), move hand up/down | Scroll |
| Open palm | Neutral: nothing happens |
| Fist held ~1 s | Pause / resume everything |

The cursor follows the big dot at the base of your index finger, not the fingertip, so
pinching to click doesn't knock the cursor off target. Only the rectangle drawn in the
preview maps to the screen, so you don't have to reach the edges of the camera view.

## Tuning

All settings are in `config/gestures.yaml`, with comments. The preview shows the live values:

- **L / R bars**: thumb–index and thumb–middle pinch distances. The white ticks are
  `pinch.start` and `pinch.release`. If clicks don't register, raise `start`; if they
  fire on their own, lower it.
- **facing**: how squarely your hand faces the camera (1 = square on, 0 = edge-on).
  New pinches only start above `pinch.min_facing` (0.6); below it the bars turn grey,
  because a turned hand makes fingertips line up and look touching when they aren't.
  If real pinches get blocked, lower it a little; if false clicks still happen, raise it.
- **Pinch arming**: a pinch only counts after the bar has been seen open (past the
  right tick) while your hand faces the camera, then closes. Start from an open hand.
- **pose (raw)**: the debounced pose, with the raw per-frame pose in brackets when they differ.
- **fps / ms**: processing rate and camera-to-action latency.
- Cursor too jittery when still: lower `cursor.filter.min_cutoff`. Laggy on fast moves:
  raise `cursor.filter.beta`.

## Layout

```
config/gestures.yaml          settings (overwritten by new builds)
config/local.yaml             your overrides (optional, never overwritten)
config/calibration.yaml       written by --calibrate
src/gesture_control/
  app.py          main loop and CLI
  calibrate.py    screen-edge calibration (--calibrate)
  capture.py      threaded camera reader (newest frame only)
  tracker.py      MediaPipe Hand Landmarker wrapper
  features.py     finger states, pinch distances, pose classification
  filters.py      One Euro filter
  controller.py   gesture state machine (modes, debounce, pinch, pause)
  pointer.py      uinput virtual mouse
  overlay.py      preview window drawing
```
