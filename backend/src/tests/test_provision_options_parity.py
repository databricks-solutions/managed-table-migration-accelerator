"""The install options are defined once (``provision.OPTIONS``); every front door reads them.

The notebook builds its widgets from ``OPTIONS``, the CLI builds its flags from ``OPTIONS``, and
the app's Settings form reads the golden fixture. A door that adds its own option drifts the
three apart, so each door is pinned here.
"""

import importlib.util
import json
import re
from pathlib import Path

from migration import provision as pv

ROOT = Path(__file__).resolve().parents[3]
NOTEBOOK = ROOT / "backend" / "notebooks" / "install.py"
CLI = ROOT / "backend" / "scripts" / "install.py"
FIXTURE = Path(__file__).parent / "fixtures" / "provision_cases.json"
APP_MIRROR = ROOT / "app" / "client" / "src" / "lib" / "provision.pure.ts"

_LITERAL_WIDGET = re.compile(r"dbutils\.widgets\.(?:text|dropdown|combobox|multiselect)\(\s*[\"'](\w+)")


def _load_cli():
    spec = importlib.util.spec_from_file_location("mtm_install_cli", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_option_names_are_unique_and_doors_are_consistent():
    names = [o.name for o in pv.OPTIONS]
    assert len(names) == len(set(names))
    for o in pv.OPTIONS:
        if o.values:
            assert o.default in o.values or o.default == "", o.name
        if o.app_values:
            assert set(o.app_values) <= set(o.values), o.name


def test_fixture_options_equal_engine():
    assert json.loads(FIXTURE.read_text())["options"] == pv.options_fixture()


def test_notebook_widgets_come_from_options():
    src = NOTEBOOK.read_text()
    assert "pv.OPTIONS" in src or "provision.OPTIONS" in src
    assert set(_LITERAL_WIDGET.findall(src)) <= set(pv.notebook_widget_names())


def test_cli_flags_come_from_options():
    parser = _load_cli().build_parser()
    longs = {s for a in parser._actions for s in a.option_strings if s.startswith("--")}
    longs.discard("--help")
    assert longs == set(pv.cli_flags()) | {pv.CHECK_ONLY_FLAG}


def test_app_mirror_names_every_app_option():
    src = APP_MIRROR.read_text()
    assert "provision_cases.json" in (ROOT / "app").joinpath(
        "client", "src", "lib", "provision.pure.test.ts").read_text()
    for name in pv.app_option_names():
        assert f'"{name}"' in src or f"'{name}'" in src or f"{name}:" in src, name
