"""Calibration runs: a dry run moves nothing, so it must save nothing."""

import io

import pytest

from gesture_control import calibrate
from gesture_control import trackball as tb


@pytest.fixture
def cfgdir(tmp_path):
    (tmp_path / "gestures.yaml").write_text("{}\n")
    return tmp_path


def test_dry_run_screen_edge_calibration_writes_nothing(cfgdir, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *a: "")
    monkeypatch.setattr(calibrate, "_enter_pressed", lambda timeout: True)
    calibrate.run_calibration(True, str(cfgdir / "gestures.yaml"))
    assert not (cfgdir / "calibration.yaml").exists()
    assert "nothing written" in capsys.readouterr().out


def test_dry_run_trackball_calibration_writes_nothing(cfgdir, monkeypatch, capsys):
    from conftest import make_config

    monkeypatch.setattr("builtins.input", lambda *a: "")
    monkeypatch.setattr(tb, "_enter_pressed", lambda timeout: True)
    monkeypatch.setattr(tb, "missing_steps", lambda cfg: [])
    monkeypatch.setattr(tb, "mouse_speed", lambda: 0.0)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    import gesture_control.display as display
    monkeypatch.setattr(display, "primary_display", lambda: None)
    tb.run_calibration(make_config(), True, str(cfgdir / "gestures.yaml"))
    assert not (cfgdir / "calibration.yaml").exists()
    assert "nothing written" in capsys.readouterr().out
