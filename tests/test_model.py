"""Automatic model download: the right file, verified, never half written.

The download is served from a local file (file:// URL), so these tests run offline.
test_pinned_model_is_still_online checks the real URL and only runs with
GESTURE_NET_TESTS=1.
"""

import hashlib
import os
import urllib.request

import pytest

from gesture_control import model as m

PAYLOAD = b"pretend this is a hand model\n" * 1000


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A 'remote' model at a file:// URL, and the default model path in tmp_path."""
    remote = tmp_path / "remote" / "hand_landmarker.task"
    remote.parent.mkdir()
    remote.write_bytes(PAYLOAD)
    local = tmp_path / "models" / "hand_landmarker.task"
    monkeypatch.setattr(m, "MODEL_URL", remote.as_uri())
    monkeypatch.setattr(m, "MODEL_SHA256", hashlib.sha256(PAYLOAD).hexdigest())
    monkeypatch.setattr(m, "MODEL_SIZE", len(PAYLOAD))
    monkeypatch.setattr(m, "DEFAULT_MODEL", local)
    return remote, local


def _quiet(_msg):
    pass


def _no_network(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("tried to download")
    monkeypatch.setattr(urllib.request, "urlopen", fail)


def test_missing_model_is_downloaded_and_verified(served):
    _, local = served
    logs = []
    assert m.ensure_model(local, logs.append) == local
    assert local.read_bytes() == PAYLOAD
    assert "Downloading" in logs[0] and "verified" in logs[-1]
    assert sorted(p.name for p in local.parent.iterdir()) == ["hand_landmarker.task"]


def test_good_model_is_not_downloaded_again(served, monkeypatch):
    _, local = served
    m.ensure_model(local, _quiet)
    _no_network(monkeypatch)
    logs = []
    m.ensure_model(local, logs.append)
    assert logs == []                     # silent on normal starts


def test_wrong_or_partial_model_is_replaced_and_kept_aside(served):
    _, local = served
    local.parent.mkdir()
    local.write_bytes(PAYLOAD[:100])      # e.g. an interrupted earlier download
    logs = []
    m.ensure_model(local, logs.append)
    assert local.read_bytes() == PAYLOAD
    assert (local.parent / "hand_landmarker.task.old").read_bytes() == PAYLOAD[:100]
    assert "isn't the expected version" in logs[0]


def test_download_that_does_not_match_is_refused(served):
    remote, local = served
    remote.write_bytes(b"something else entirely")      # e.g. Google changed the file
    with pytest.raises(m.ModelError, match="doesn't match") as exc:
        m.ensure_model(local, _quiet)
    assert "curl -L -o" in str(exc.value)          # says how to get it by hand
    assert not local.exists()
    assert list(local.parent.iterdir()) == []            # no .part left behind


def test_network_failure_explains_what_to_do(served, monkeypatch):
    _, local = served

    def offline(*a, **k):
        raise urllib.error.URLError("Temporary failure in name resolution")
    monkeypatch.setattr(urllib.request, "urlopen", offline)
    with pytest.raises(m.ModelError) as exc:
        m.ensure_model(local, _quiet)
    msg = str(exc.value)
    assert "name resolution" in msg and "curl -L -o" in msg and m.MODEL_SHA256 in msg
    assert not local.exists()


def test_download_interrupted_midway_leaves_no_file(served, monkeypatch):
    _, local = served

    class Broken:
        def __init__(self):
            self.n = 0

        def read(self, size):
            self.n += 1
            if self.n > 2:
                raise ConnectionResetError("connection reset by peer")
            return b"x" * 100

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Broken())
    with pytest.raises(m.ModelError, match="connection reset"):
        m.ensure_model(local, _quiet)
    assert list(local.parent.iterdir()) == []


def test_custom_model_is_used_as_is(served, tmp_path, monkeypatch):
    custom = tmp_path / "my_model.task"
    custom.write_bytes(b"my own model")
    _no_network(monkeypatch)
    assert m.ensure_model(custom, _quiet) == custom
    assert custom.read_bytes() == b"my own model"       # never checked or replaced


def test_missing_custom_model_is_an_error_not_a_download(served, tmp_path, monkeypatch):
    _no_network(monkeypatch)
    with pytest.raises(m.ModelError, match="tracker.model"):
        m.ensure_model(tmp_path / "nope.task", _quiet)


def test_default_config_points_at_the_downloaded_model(tmp_path):
    import shutil
    from pathlib import Path

    from gesture_control.config import DEFAULT_CONFIG_PATH, load_config
    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    cfg = load_config(tmp_path / "gestures.yaml")
    assert Path(cfg["tracker"]["model"]) == m.DEFAULT_MODEL


def test_url_is_pinned_to_a_version():
    assert "/latest/" not in m.MODEL_URL
    assert "/float16/1/" in m.MODEL_URL


@pytest.mark.skipif(os.environ.get("GESTURE_NET_TESTS") != "1",
                    reason="network test: set GESTURE_NET_TESTS=1")
def test_pinned_model_is_still_online(tmp_path):
    """The real URL still serves exactly the expected file."""
    target = tmp_path / "hand_landmarker.task"
    m.download(target)
    assert target.stat().st_size == m.MODEL_SIZE
    assert m.sha256_of(target) == m.MODEL_SHA256


def test_failed_download_leaves_the_old_model_where_it_was(served, monkeypatch):
    """A wrong model plus no internet: the error says nothing was changed, so the old
    file must still be in place (it's only moved aside after a verified download)."""
    _, local = served
    local.parent.mkdir()
    local.write_bytes(b"some other model")

    def offline(*a, **k):
        raise OSError("network is unreachable")
    monkeypatch.setattr(urllib.request, "urlopen", offline)
    with pytest.raises(m.ModelError):
        m.ensure_model(local, _quiet)
    assert local.read_bytes() == b"some other model"
    assert sorted(p.name for p in local.parent.iterdir()) == ["hand_landmarker.task"]
