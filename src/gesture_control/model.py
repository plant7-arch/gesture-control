"""The hand-tracking model: downloaded automatically on first start, and checked.

The app is tuned and tested against one exact model file, so it downloads that file
from a versioned URL (not "latest", which Google can change) and checks its SHA-256
before using it. A partial or different file is never used.
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .config import PROJECT_ROOT

DEFAULT_MODEL = PROJECT_ROOT / "models" / "hand_landmarker.task"
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task"
)
MODEL_SHA256 = "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"
MODEL_SIZE = 7_819_105  # bytes
TIMEOUT_S = 30


class ModelError(RuntimeError):
    """The model couldn't be found, downloaded or verified. The message says why and
    what to do."""


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def manual_steps(path: Path) -> str:
    return (f"To download it by hand:\n"
            f"  mkdir -p {path.parent}\n"
            f"  curl -L -o {path} \\\n    {MODEL_URL}\n"
            f"  sha256sum {path}   # must print {MODEL_SHA256}")


def ensure_model(path: str | Path, log: Callable[[str], None] = print) -> Path:
    """Return the path of a verified model, downloading it if needed.

    Only the default model (models/hand_landmarker.task) is downloaded and checked.
    A model set in the config (tracker.model) is the user's own choice: it is used
    as it is and must exist."""
    path = Path(path)
    if path.resolve() != DEFAULT_MODEL.resolve():
        if not path.exists():
            raise ModelError(f"Model not found: {path} (set by tracker.model in the config)")
        return path

    if path.exists():
        if sha256_of(path) == MODEL_SHA256:
            return path
        log(f"The hand model at {path} isn't the expected version (or is incomplete); "
            f"downloading the right one. The old file is kept as {path.name}.old.")
    else:
        log(f"Downloading the hand-tracking model ({MODEL_SIZE / 1e6:.1f} MB, once only)...")
    # The old file is only moved aside once the new one is downloaded and verified, so
    # a failed download really leaves nothing changed.
    download(path)
    log("Hand model downloaded and verified.")
    return path


def download(path: Path, url: str | None = None) -> None:
    """Download to path.part, verify, then move into place (never a half file)."""
    url = url or MODEL_URL
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp, part.open("wb") as out:
                while block := resp.read(1 << 16):
                    out.write(block)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise ModelError(f"Couldn't download the hand model: {reason}\n"
                             f"Check your internet connection and start the app again.\n"
                             + manual_steps(path)) from None
        size, digest = part.stat().st_size, sha256_of(part)
        if digest != MODEL_SHA256:
            raise ModelError(
                f"The downloaded hand model doesn't match the expected file "
                f"({size} bytes, sha256 {digest[:12]}..., expected {MODEL_SIZE} bytes, "
                f"{MODEL_SHA256[:12]}...). Nothing was changed. If this keeps happening, "
                f"Google may have moved the file.\n" + manual_steps(path))
        if path.exists():  # a wrong or partial model: keep it aside, not deleted
            os.replace(path, path.with_name(path.name + ".old"))
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)

