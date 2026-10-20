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

To change any behaviour, see [Configuration](#configuration).

Useful options:

| Option | What it does |
|---|---|
| `--dry-run` | Track and recognize gestures, but print actions instead of moving the mouse |
| `--mode absolute` | Hand position = screen position (the default is relative, touchpad-like) |
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
| ...pinch thumb + index twice quickly | Double click |
| ...then pinch thumb + middle (index stays up) | Right click |
| Index + middle up (peace sign), move hand up/down | Scroll |
| Open palm | Neutral: nothing happens |
| Fist held ~1 s | Pause / resume everything |

The cursor works like a touchpad (relative mode, the default): it follows your hand's
movement, not its position. Move slowly for fine control, flick to cross the screen. When
you run out of room, open your palm (the cursor stops), move your hand back, and point
again. Movement is measured in palm lengths, so it feels the same at any distance from
the camera, and tracking follows the centre of your knuckles (the big dot), which stays
put when you pinch.

In absolute mode (`--mode absolute`) your hand position maps directly to the screen:
only the rectangle drawn in the preview maps to the screen.

Both modes drive the calibrated virtual tablet, so run `--calibrate` once either way.

## Configuration

### How the config files work

Settings are read from up to four layers. Each layer only needs the settings it changes,
and later layers win:

| Order | Source | Who writes it |
|---|---|---|
| 1 | Built-in defaults (`src/gesture_control/config.py`) | the code |
| 2 | `config/gestures.yaml` | each build (overwritten by `unzip -o`) |
| 3 | `config/calibration.yaml` | `--calibrate` |
| 4 | `config/local.yaml` | **you** |

Put your changes in **`config/local.yaml`**. It's never included in a build, so updates
can't overwrite it. Use `gestures.yaml` as a reference for names and defaults, but don't
edit it: the next build replaces it.

Settings are read once at startup. After editing, quit the app (`q`) and start it again.

### Writing local.yaml

Create the file with any text editor (`nano config/local.yaml`, or `gnome-text-editor`)
and copy in only the settings you want to change, keeping the section names above them:

```yaml
# config/local.yaml - my overrides
pinch:
  start: 0.32            # pinches register a little earlier

cursor:
  relative:
    gain_slow: 0.03      # finer aiming
    screen_aspect: 1.78  # 16:9 screen

double_click:
  window_ms: 900
```

YAML rules that matter here:

- Indent with **2 spaces per level, never tabs**. Indentation is what puts a setting
  inside a section: `start` belongs to `pinch` because it is indented under it.
- Write each setting as `name: value`, with a space after the colon.
- Text after `#` is a comment.
- Values: numbers (`0.32`, `700`), `true` / `false`, words (`relative`), and lists in
  square brackets (`[0.63, 0.58]`).
- Only include the settings you change; everything else keeps its default.

If something is wrong, the app tells you at startup:

```
Config error: local.yaml (line 3, column 11): mapping values are not allowed here. ...
Config warning: local.yaml: unknown setting 'pinch.strat' (typo?) - ignored
```

When `config/local.yaml` loads, the startup line `Also loaded: config/local.yaml`
confirms it. `uv run gesture-control --dry-run` is a safe way to try changes: everything
works except that clicks and movement are printed instead of performed.

To reset a setting, delete it from `local.yaml`. To reset everything, delete the file.
To undo calibration, delete `config/calibration.yaml`.

### Common changes

| I want to... | Setting in local.yaml |
|---|---|
| Aim more precisely | `cursor:` `relative:` `gain_slow: 0.03` |
| Cross the screen with less arm movement | `cursor:` `relative:` `gain_fast: 0.6` |
| Stop the cursor creeping while my hand is still | `cursor:` `relative:` `min_speed: 0.25` |
| Make pinches register more easily | `pinch:` `start: 0.33` and `release: 0.47` |
| Stop false clicks | `pinch:` `start: 0.24` |
| Allow pinches with my hand more turned | `pinch:` `min_facing: 0.5` |
| Get double clicks more easily | `double_click:` `window_ms: 900` |
| Scroll faster / in the other direction | `scroll:` `notches_per_height: 40` / `invert: true` |
| Pause/resume with a shorter fist hold | `pause:` `fist_hold_s: 0.7` |
| Use hand position = screen position | `cursor:` `mode: absolute` |
| Use a different camera | `camera:` `index: 2` |

### All settings

Distances like `pinch.start` are relative to your palm size (wrist to middle knuckle,
about 9 cm), so they don't change with distance from the camera.

#### `camera`

| Setting | Default | What it does |
|---|---|---|
| `index` | `0` | Camera device number: `0` = `/dev/video0` (`--camera N` overrides it) |
| `width`, `height` | `640`, `480` | Capture resolution. Higher doesn't improve tracking, it only costs speed |
| `fps` | `30` | Requested frame rate (your webcam's maximum is 30) |
| `fourcc` | `MJPG` | Camera format. `MJPG` gives full frame rate; `YUYV` is the uncompressed alternative |
| `mirror` | `true` | Mirror the image so moving your hand right moves the cursor right |
| `lock_framerate` | `true` | Stop auto-exposure lowering the frame rate in dim light |

#### `tracker` (MediaPipe hand tracking)

| Setting | Default | What it does |
|---|---|---|
| `model` | `models/hand_landmarker.task` | Model file, relative to the project folder |
| `num_hands` | `1` | Hands to detect. With more than 1, only the most confident hand is used |
| `min_detection_confidence` | `0.6` | How sure the model must be to find a new hand. Lower finds hands sooner, but with more false detections |
| `min_presence_confidence` | `0.6` | How sure it must be that the hand is still there |
| `min_tracking_confidence` | `0.5` | Below this it searches for the hand again instead of tracking it |

#### `pose` (finger up / curled detection)

| Setting | Default | What it does |
|---|---|---|
| `extend_ratio` | `1.15` | A finger is up when its tip is this many times further from the wrist than its middle joint (typical: up 1.3–1.5) |
| `curl_ratio` | `0.95` | A finger is curled below this (typical: 0.6–0.8). Used for the fist |
| `debounce_frames` | `3` | Frames a pose must hold before it counts (3 = about 100 ms). Higher = fewer accidental mode changes, but slower to react |

#### `pinch` (clicks)

| Setting | Default | What it does |
|---|---|---|
| `start` | `0.28` | A pinch begins when the thumb–fingertip distance drops below this (left tick on the L / R bars). Higher = registers more easily |
| `release` | `0.42` | It ends when the distance rises above this (right tick). Must be larger than `start` |
| `confirm_frames` | `3` | Frames below `start` before the click fires. Higher = fewer accidental clicks, slightly slower |
| `min_finger_reach` | `1.05` | Left pinch only counts if the index fingertip is at least this far from the wrist, so a fist isn't read as a click |
| `right_min_reach` | `0.0` | Same check for the middle finger and right click. `0` = off |
| `min_facing` | `0.6` | Pinches only start when your hand faces the camera at least this squarely (1 = square on, 0 = edge-on) |

#### `cursor`

| Setting | Default | What it does |
|---|---|---|
| `mode` | `relative` | `relative`: touchpad-like, the cursor follows hand movement. `absolute`: hand position = screen position |
| `anchor` | `knuckles` | Hand point that drives the cursor: `knuckles` (average of 4, steadiest), `index_mcp` (index knuckle), `index_tip` (fingertip, moves when you pinch), `palm_center` |
| `click_freeze_ms` | `180` | After a click starts, hold the cursor still this long so the click lands on target |
| `abs_scale` | `[1.0, 1.0]` | Written by `--calibrate` (into `calibration.yaml`); don't set it by hand |

`cursor.relative` (relative mode). Gain is screen widths of cursor travel per palm length
of hand travel. It rises smoothly from `gain_slow` to `gain_fast` as hand speed rises
from `speed_slow` to `speed_fast`:

| Setting | Default | What it does |
|---|---|---|
| `gain_slow` | `0.05` | Gain for slow movement (0.05 = 96 px per palm length on a 1920 px screen). Lower = more precise |
| `gain_fast` | `0.4` | Gain for fast movement. Higher = less arm movement to cross the screen |
| `speed_slow` | `0.6` | Hand speed (palm lengths per second) at or below which `gain_slow` applies |
| `speed_fast` | `5.0` | Hand speed at or above which `gain_fast` applies |
| `min_speed` | `0.15` | Slower than this counts as holding still: hides tracking jitter. Raise it if the cursor creeps |
| `screen_aspect` | `1.6` | Screen width ÷ height (1.6 for 16:10, 1.78 for 16:9), so vertical and horizontal movement match |
| `output` | `tablet` | `tablet`: uses the calibrated virtual tablet (recommended). `mouse`: plain mouse events, with GNOME's mouse acceleration added on top |
| `mouse_px_per_screen` | `1920` | Only for `output: mouse`: movement units per screen width |
| `filter.min_cutoff` | `1.0` | Smoothing when still. Lower = steadier, but more lag |
| `filter.beta` | `4.0` | Smoothing when moving. Higher = less lag on fast moves, but more jitter |
| `filter.d_cutoff` | `1.0` | Smoothing of the speed estimate; rarely needs changing |

Absolute mode only:

| Setting | Default | What it does |
|---|---|---|
| `region.x_min`, `x_max` | `0.2`, `0.8` | Left and right edges of the box (fractions of the camera image) that maps to the screen |
| `region.y_min`, `y_max` | `0.2`, `0.7` | Top and bottom edges of that box |
| `filter.min_cutoff` / `beta` / `d_cutoff` | `1.0` / `10.0` / `1.0` | Smoothing, as above |
| `deadzone` | `0.0015` | Ignore cursor changes smaller than this fraction of the screen |

#### `scroll` (peace sign)

| Setting | Default | What it does |
|---|---|---|
| `notches_per_height` | `25` | Wheel notches for moving your hand the full camera-image height. Higher = faster scrolling |
| `invert` | `false` | `true` = move your hand up to scroll down |
| `deadzone` | `0.0015` | Ignore vertical movement smaller than this per frame |
| `settle_ms` | `200` | Ignore movement just after switching to the scroll pose |
| `filter.*` | `1.0` / `5.0` / `1.0` | Smoothing, as above |

#### `double_click`

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `true` | Two quick pinches = double click |
| `window_ms` | `700` | The second pinch must start within this time of releasing the first |
| `max_move` | `0.02` | The cursor may drift at most this far between pinches (fraction of screen width, 0.02 = about 38 px) |
| `os_time_ms` | `auto` | The system double-click time. `auto` reads GNOME's setting (400 ms by default); or set a number |

#### `pause`, `safety`, `preview`

| Setting | Default | What it does |
|---|---|---|
| `pause.fist_hold_s` | `1.0` | Seconds to hold a fist to pause or resume |
| `pause.start_paused` | `false` | Start paused (same as `--start-paused`) |
| `safety.lost_hand_release_frames` | `8` | Release a held button (e.g. mid-drag) if the hand is lost for this many frames |
| `preview.enabled` | `true` | Show the camera window (`--no-preview` turns it off) |
| `preview.window_name` | `gesture-control` | Window title |
| `preview.show_landmarks` | `true` | Draw the hand skeleton |

## Tuning

The preview shows live values that help you choose settings:

- **L / R bars**: thumb–index and thumb–middle pinch distances. The white ticks are
  `pinch.start` and `pinch.release`. If clicks don't register, raise `start`; if they
  fire on their own, lower it.
- **facing**: how squarely your hand faces the camera (1 = square on, 0 = edge-on).
  New pinches only start above `pinch.min_facing` (0.6); below it the bars turn grey,
  because a turned hand makes fingertips line up and look touching when they aren't.
  If real pinches get blocked, lower it a little; if false clicks still happen, raise it.
- **Pinch arming**: a pinch only counts after the bar has been seen open (past the
  right tick) while your hand faces the camera, then closes. The L / R label turns green
  when armed. If a bar goes red but nothing clicks, check that its label was green first:
  open the fingers a little wider before tapping.
- **Double click** (`double_click`): the second pinch must start within `window_ms`
  (700) of letting go of the first, with the cursor within `max_move` of the first click.
  It then snaps back to the first click's spot, so a little drift doesn't matter.
  If double clicks come out as two single clicks, raise `window_ms`.
- **pose (raw)**: the debounced pose, with the raw per-frame pose in brackets when they differ.
- **fps / ms**: processing rate and camera-to-action latency.
- Relative mode feel (`cursor.relative`): too twitchy for precise aiming, lower
  `gain_slow`; too much arm movement to cross the screen, raise `gain_fast`. Cursor
  creeps while your hand is still, raise `min_speed`.
- Absolute mode: too jittery when still, lower `cursor.filter.min_cutoff`; laggy on fast
  moves, raise `cursor.filter.beta`.

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
