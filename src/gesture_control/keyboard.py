"""Air keyboard: the layout drawn in the preview, hit-testing, and typing the keys.

The keyboard is drawn over the camera image. The index fingertip picks a key (its
position in the camera picture, so what you see is what you hit) and a thumb + index
pinch types it. Keys go out through the virtual keyboard (pointer.VirtualKeys) into
whichever window has focus. The labels are for the US layout: the virtual keyboard
sends key positions, and GNOME turns them into characters with your layout.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .pointer import KEY_NAMES

MODIFIERS = ("ctrl", "alt", "shift", "super")

# Keys that type a character: name -> (character, character with Shift), US layout.
CHARS: dict[str, tuple[str, str]] = {
    **{c: (c, c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"},
    **dict(zip("1234567890", zip("1234567890", "!@#$%^&*()"))),
    "grave": ("`", "~"), "minus": ("-", "_"), "equal": ("=", "+"),
    "leftbrace": ("[", "{"), "rightbrace": ("]", "}"), "backslash": ("\\", "|"),
    "semicolon": (";", ":"), "apostrophe": ("'", '"'), "comma": (",", "<"),
    "dot": (".", ">"), "slash": ("/", "?"), "space": (" ", " "),
}
# Written as a character in a key spec ("ctrl+=", "ctrl+/"): the key's name. Not "+",
# which separates the parts of a spec.
CHAR_NAMES = {normal: name for name, (normal, _) in CHARS.items()
              if normal != name and normal not in " +"}

NICE = {
    "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super", "esc": "Esc",
    "tab": "Tab", "enter": "Enter", "backspace": "Bksp", "delete": "Del",
    "insert": "Ins", "home": "Home", "end": "End", "pageup": "PgUp", "pagedown": "PgDn",
    "space": "Space", "left": "Left", "right": "Right", "up": "Up", "down": "Down",
    "caps": "Caps",
    **{f"f{n}": f"F{n}" for n in range(1, 13)},
}

# Holding the pinch on one of these repeats it, like holding a real key.
REPEATABLE = {"backspace", "delete", "left", "right", "up", "down", "space"}

ARROWS = ("left", "up", "down", "right")

# The keyboard below the shortcut row: (key name, width in key units). Every row is
# 15 units wide.
ROWS: list[list[tuple[str, float]]] = [
    [("grave", 1), *((d, 1) for d in "1234567890"), ("minus", 1), ("equal", 1),
     ("backspace", 2)],
    [*((c, 1) for c in "qwertyuiop"), ("leftbrace", 1), ("rightbrace", 1),
     ("backslash", 1), ("delete", 2)],
    [("caps", 1.75), *((c, 1) for c in "asdfghjkl"), ("semicolon", 1),
     ("apostrophe", 1), ("enter", 2.25)],
    [("shift", 2.25), *((c, 1) for c in "zxcvbnm"), ("comma", 1), ("dot", 1),
     ("slash", 1), ("shift", 2.75)],
    [("ctrl", 1.5), ("alt", 1.5), ("home", 1), ("space", 6), ("end", 1),
     *((a, 1) for a in ARROWS)],
]
ROW_UNITS = 15.0


def parse_spec(spec: str) -> tuple[tuple[str, ...], str]:
    """A key or shortcut as written in the config: "enter", "ctrl+z", "ctrl+shift+t",
    "alt+tab", "ctrl+=". Returns (modifiers, key name). ValueError if it isn't one."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError(f"{spec!r} isn't a key: write it like enter, ctrl+z or alt+tab")
    parts = [p.strip().lower() for p in spec.split("+")]
    if any(not p for p in parts):
        raise ValueError(f"{spec!r} isn't a key: write it like enter, ctrl+z or alt+tab")
    *mods, key = parts
    key = CHAR_NAMES.get(key, key)
    for m in mods:
        if m not in MODIFIERS:
            raise ValueError(f"{spec!r}: {m!r} isn't a modifier (ctrl, alt, shift, super)")
    if len(set(mods)) != len(mods):
        raise ValueError(f"{spec!r}: a modifier is given twice")
    if key not in KEY_NAMES:
        raise ValueError(f"{spec!r}: unknown key {key!r} (names like a, 5, enter, "
                         "backspace, esc, tab, home, f5, left, or characters like = and /)")
    return tuple(mods), key


def key_label(name: str) -> str:
    if name in NICE:
        return NICE[name]
    if name in CHARS:
        return CHARS[name][0].upper()
    return name


def spec_label(spec: str) -> str:
    """ "ctrl+z" -> "Ctrl+Z", for the key caps and messages."""
    mods, key = parse_spec(spec)
    return "+".join([NICE[m] for m in mods] + [key_label(key)])


@dataclass(frozen=True)
class Key:
    kind: str     # "char" (types a character), "mod" (one-shot modifier), "caps", "spec"
    name: str     # key name, or the shortcut spec ("ctrl+z") for the shortcut row
    label: str    # what the key cap shows
    shifted: str  # label with Shift / Caps applied (character keys)
    rect: tuple[float, float, float, float]  # x0, y0, x1, y1 in camera-frame fractions
    row: int


def _make_key(name: str, rect, row: int, spec: bool = False) -> Key:
    if spec:
        return Key("spec", name, spec_label(name), spec_label(name), rect, row)
    if name in ("shift", "ctrl", "alt"):
        return Key("mod", name, NICE[name], NICE[name], rect, row)
    if name == "caps":
        return Key("caps", name, "Caps", "Caps", rect, row)
    if name in CHARS and name != "space":
        normal, shifted = CHARS[name]
        label = normal.upper() if normal.isalpha() else normal
        return Key("char", name, label, shifted, rect, row)
    if name == "space":
        return Key("char", name, "Space", "Space", rect, row)
    return Key("spec", name, key_label(name), key_label(name), rect, row)


class Layout:
    """Where every key is, in camera-frame fractions (0..1). The shortcut row (if
    any) comes first, stretched to the full width; then the US keyboard rows."""

    def __init__(self, region: tuple[float, float, float, float], shortcuts: list[str]):
        x0, x1, y0, y1 = region
        self.region = region
        rows: list[list[tuple[str, float, bool]]] = []
        if shortcuts:
            rows.append([(s, ROW_UNITS / len(shortcuts), True) for s in shortcuts])
        rows += [[(n, w, False) for n, w in r] for r in ROWS]
        self.rows = len(rows)
        row_h = (y1 - y0) / self.rows
        unit = (x1 - x0) / ROW_UNITS
        self.keys: list[Key] = []
        for r, row in enumerate(rows):
            x = x0
            top, bottom = y0 + r * row_h, y0 + (r + 1) * row_h
            for i, (name, w, spec) in enumerate(row):
                right = x1 if i == len(row) - 1 else x + w * unit  # no rounding gap
                self.keys.append(_make_key(name, (x, top, right, bottom), r, spec))
                x = right
        self.shortcut_rows = 1 if shortcuts else 0

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> Layout:
        kc = cfg["keyboard"]
        r = kc["region"]
        return cls((float(r["x_min"]), float(r["x_max"]), float(r["y_min"]),
                    float(r["y_max"])), list(kc["shortcuts"]))

    def hit(self, x: float, y: float) -> int | None:
        """Index of the key under (x, y), or None outside the keyboard. Keys touch
        each other (the gaps are only drawn), so there's no dead space between them."""
        x0, x1, y0, y1 = self.region
        if not (x0 <= x < x1 and y0 <= y < y1):
            return None
        for i, k in enumerate(self.keys):
            kx0, ky0, kx1, ky1 = k.rect
            if kx0 <= x < kx1 and ky0 <= y < ky1:
                return i
        return None

    def find(self, name: str) -> int:
        """Index of the first key with this name (tests, and the overlay)."""
        return next(i for i, k in enumerate(self.keys) if k.name == name)


class Typer:
    """Types keys on a VirtualKeys device.

    * Shift, Ctrl and Alt on the keyboard are one-shot: tap one, and it applies to the
      next key (combining with a shortcut's own, so Shift then Ctrl+Z is Ctrl+Shift+Z).
      Tap it again to cancel it.
    * Caps: letters come out in capitals until it's tapped again.
    * Alt+Tab holds Alt so GNOME's window switcher stays open; each further Alt+Tab
      moves to the next window. Alt is let go (choosing the window) by any other key,
      after alt_tab_hold_s without another Alt+Tab, or when the keyboard closes.
    * Holding the pinch on a REPEATABLE key repeats it after repeat_delay_s.
    * text: the last characters typed, for the preview (Backspace removes one,
      Enter clears it).
    """

    def __init__(self, keys, repeat_delay_s: float = 0.5, repeat_rate: float = 10.0,
                 alt_tab_hold_s: float = 1.5, echo_len: int = 40, clock=time.monotonic):
        self.keys = keys
        self.repeat_delay = float(repeat_delay_s)
        self.repeat_interval = 1.0 / max(float(repeat_rate), 1e-3)
        self.alt_tab_hold = float(alt_tab_hold_s)
        self.echo_len = int(echo_len)
        self.clock = clock  # real time, for noticing keys that went into the preview
        self.latched: set[str] = set()
        self.caps = False
        self.text = ""
        self.last = ""                       # label of the last thing typed
        self.sent_at = -1e9                  # clock() when a key was last sent
        self._alt_until: float | None = None  # Alt held for the window switcher
        self._held: tuple[tuple[str, ...], str] | None = None  # being repeated
        self._next_repeat = 0.0

    # -- what the controller calls ---------------------------------------------------
    def press(self, key: Key, t: float) -> str:
        """A layout key was pinched. Returns what happened, for the action log."""
        if key.kind == "mod":
            if key.name in self.latched:
                self.latched.discard(key.name)
                return f"{key.label} off"
            self.latched.add(key.name)
            return f"{key.label} on"
        if key.kind == "caps":
            self.caps = not self.caps
            return "Caps on" if self.caps else "Caps off"
        return self.press_spec(key.name, t)  # a character, or a key / shortcut

    def press_spec(self, spec: str, t: float) -> str:
        """Type a key or shortcut ("enter", "ctrl+z"), with any latched modifiers."""
        mods, key = parse_spec(spec)
        mods_set = set(mods) | self.latched
        if len(key) == 1 and key.isalpha() and self.caps:
            mods_set ^= {"shift"}  # Caps: capitals; Shift with Caps: small letters
        self.latched.clear()
        self._send(mods_set, key, t)
        self._echo(mods_set, key)
        if key in REPEATABLE:
            self._held = (tuple(sorted(mods_set)), key)
            self._next_repeat = t + self.repeat_delay
        else:
            self._held = None
        ordered = [m for m in MODIFIERS if m in mods_set]
        self.last = "+".join([NICE[m] for m in ordered] + [key_label(key)])
        return f"key {self.last}"

    def hold(self, t: float) -> None:
        """The pinch is still closed: repeat a repeatable key once its delay is over."""
        if self._held is None or t < self._next_repeat:
            return
        mods, key = self._held
        self._send(set(mods), key, t)
        self._echo(set(mods), key)
        # One repeat per frame at most: a slow frame doesn't fire a burst.
        self._next_repeat = max(self._next_repeat + self.repeat_interval, t)

    def lift(self) -> None:
        """The pinch opened: stop repeating."""
        self._held = None

    def tick(self, t: float) -> None:
        if self._alt_until is not None and t >= self._alt_until:
            self.end_switch()

    def end_switch(self) -> None:
        """Let go of Alt held for the window switcher (GNOME picks the window)."""
        if self._alt_until is not None:
            self._alt_until = None
            self.keys.release("alt")

    def release_all(self) -> None:
        """Let go of everything held (hand lost, camera stalled, keyboard closed)."""
        self.end_switch()
        self.lift()
        self.keys.release_all()

    @property
    def switching(self) -> bool:
        return self._alt_until is not None

    # -- internals -------------------------------------------------------------------
    def _send(self, mods: set[str], key: str, t: float) -> None:
        if key == "tab" and "alt" in mods:  # window switcher: keep Alt down
            others = [m for m in MODIFIERS if m in mods and m != "alt"]
            if self._alt_until is None:
                self.keys.press("alt")
            for m in others:
                self.keys.press(m)
            self.keys.tap("tab")
            for m in reversed(others):
                self.keys.release(m)
            self._alt_until = t + self.alt_tab_hold
            self.sent_at = self.clock()
            return
        self.end_switch()  # any other key ends the switcher first
        ordered = [m for m in MODIFIERS if m in mods]
        for m in ordered:
            self.keys.press(m)
        self.keys.tap(key)
        for m in reversed(ordered):
            self.keys.release(m)
        self.sent_at = self.clock()

    def _echo(self, mods: set[str], key: str) -> None:
        if mods & {"ctrl", "alt", "super"}:
            return
        if key == "backspace":
            self.text = self.text[:-1]
        elif key == "enter":
            self.text = ""
        elif key == "tab":
            self.text += " "
        elif key in CHARS:
            normal, shifted = CHARS[key]
            self.text += shifted if "shift" in mods else normal
        self.text = self.text[-self.echo_len:]


def specs_for_text(text: str) -> list[str]:
    """The key presses that type `text` (US layout): "Hi!" -> ["shift+h", "i",
    "shift+1"]. ValueError for a character the keyboard doesn't have."""
    out = []
    for ch in text:
        for name, (normal, shifted) in CHARS.items():
            if ch == normal:
                out.append(name)
                break
            if ch == shifted:
                out.append(f"shift+{name}")
                break
        else:
            raise ValueError(f"no key types {ch!r}")
    return out


def gnome_layout(run=None) -> str | None:
    """The first keyboard layout in GNOME's input sources ("us", "de", "us+intl"), or
    None when it can't be read. The keyboard's labels are right for "us" only."""
    import ast
    import shutil
    import subprocess

    if run is None:
        if not shutil.which("gsettings"):
            return None

        def run(cmd):
            return subprocess.run(cmd, capture_output=True, text=True, timeout=2,
                                  check=False).stdout
    try:
        out = run(["gsettings", "get", "org.gnome.desktop.input-sources", "sources"])
        sources = ast.literal_eval(out.strip().removeprefix("@a(ss) "))
    except (OSError, subprocess.SubprocessError, ValueError, SyntaxError):
        return None
    for kind, name in sources:
        if kind == "xkb":
            return name
    return None
