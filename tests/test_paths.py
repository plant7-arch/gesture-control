"""Where config and the model live: the project folder when run from it, or
~/.config/gesture-control and ~/.cache/gesture-control when installed as a command."""

from __future__ import annotations

from pathlib import Path

import pytest

from gesture_control import config as config_mod
from gesture_control.config import PROJECT_ROOT, is_checkout, load_config, locate


def _checkout(root: Path) -> Path:
    (root / "config").mkdir(parents=True)
    (root / "config" / "gestures.yaml").write_text("{}\n")
    (root / "pyproject.toml").write_text("[project]\nname = 'gesture-control'\n")
    return root


def test_this_test_run_is_from_the_project_folder():
    assert is_checkout(PROJECT_ROOT)
    assert config_mod.FROM_CHECKOUT
    assert config_mod.CONFIG_DIR == PROJECT_ROOT / "config"
    assert config_mod.DATA_ROOT == PROJECT_ROOT


def test_project_folder_keeps_config_and_models_in_it(tmp_path):
    root = _checkout(tmp_path / "proj")
    assert locate(root, env={}, home=tmp_path / "home") == (True, root / "config", root)


def test_installed_package_uses_the_xdg_folders(tmp_path):
    """An installed package sits in site-packages: two levels up is lib/pythonX.Y,
    not a project folder, so the per-user folders are used."""
    site = tmp_path / "venv" / "lib" / "python3.12"
    site.mkdir(parents=True)
    home = tmp_path / "home"
    assert locate(site, env={}, home=home) == (
        False, home / ".config" / "gesture-control", home / ".cache" / "gesture-control")


def test_xdg_variables_are_honoured_when_absolute(tmp_path):
    home, cfg, cache = tmp_path / "home", tmp_path / "cfg", tmp_path / "cache"
    env = {"XDG_CONFIG_HOME": str(cfg), "XDG_CACHE_HOME": str(cache)}
    assert locate(tmp_path, env=env, home=home)[1:] == (cfg / "gesture-control",
                                                         cache / "gesture-control")
    # relative values are invalid per the XDG spec: fall back to the defaults
    env = {"XDG_CONFIG_HOME": "rel", "XDG_CACHE_HOME": ""}
    assert locate(tmp_path, env=env, home=home)[1:] == (home / ".config" / "gesture-control",
                                                         home / ".cache" / "gesture-control")


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Pretend the app is installed: config in a per-user folder that has no
    gestures.yaml (the defaults are built in)."""
    home = tmp_path / "home"
    cfg_dir = home / ".config" / "gesture-control"
    cfg_dir.mkdir(parents=True)
    monkeypatch.setattr(config_mod, "FROM_CHECKOUT", False)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_dir / "gestures.yaml")
    monkeypatch.setattr(config_mod, "DATA_ROOT", home / ".cache" / "gesture-control")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return cfg_dir


def test_installed_reads_local_yaml_from_the_user_folder(installed):
    (installed / "local.yaml").write_text("pinch:\n  start: 0.3\n")
    cfg = load_config()
    assert cfg["pinch"]["start"] == 0.3 and cfg["_loaded"] == ["local.yaml"]
    assert cfg["_config_dir"] == str(installed)
    assert cfg["tracker"]["model"].endswith(".cache/gesture-control/models/hand_landmarker.task")


def test_installed_without_any_file_runs_on_the_defaults(installed):
    cfg = load_config()
    assert cfg["_loaded"] == [] and cfg["_warnings"] == []


def test_installed_calibration_is_saved_in_the_user_folder(installed):
    out = config_mod.save_calibration((0.9, 0.9))
    assert out == installed / "calibration.yaml"
    assert load_config()["cursor"]["abs_scale"] == [0.9, 0.9]


def test_messages_name_the_right_file_and_command(installed, monkeypatch):
    assert config_mod.shown("local.yaml") == "~/.config/gesture-control/local.yaml"
    assert config_mod.command() == "gesture-control"
    monkeypatch.setattr(config_mod, "FROM_CHECKOUT", True)
    assert config_mod.shown("local.yaml") == "config/local.yaml"
    assert config_mod.command() == "uv run gesture-control"
