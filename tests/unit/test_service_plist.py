"""The launchd plist template that `make install-service` fills in."""

import plistlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy" / "com.jackpark.pm-engine.plist"


def _render(tmp_path: Path, **overrides: str) -> dict[str, Any]:
    out = tmp_path / "pm-engine.plist"
    args = ["make", "-C", str(ROOT), "render-plist", f"PLIST_DST={out}"]
    args.append("UVICORN=/opt/venv/bin/uvicorn")
    args += [f"{k}={v}" for k, v in overrides.items()]
    subprocess.run(args, check=True, capture_output=True)
    return plistlib.loads(out.read_bytes())


def test_template_exists_because_the_makefile_names_it() -> None:
    assert TEMPLATE.is_file()


def test_no_double_hyphen_inside_xml_comments() -> None:
    # XML forbids two hyphens inside a comment; strict parsers reject the file.
    comments = re.findall(r"<!--(.*?)-->", TEMPLATE.read_text(), re.S)
    assert comments
    assert [c for c in comments if "--" in c] == []


def test_template_is_a_valid_property_list() -> None:
    assert plistlib.loads(TEMPLATE.read_bytes())["Label"] == "com.jackpark.pm-engine"


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_render_fills_every_placeholder(tmp_path: Path) -> None:
    plist = _render(tmp_path)
    assert "__" not in (tmp_path / "pm-engine.plist").read_text()
    assert plist["ProgramArguments"][0] == "/opt/venv/bin/uvicorn"
    assert plist["EnvironmentVariables"]["PATH"].startswith("/opt/venv/bin:")
    assert plist["WorkingDirectory"] == str(ROOT)
    assert plist["StandardOutPath"] == f"{ROOT}/logs/server.log"


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_render_keeps_the_lessons_learned_and_the_default_bind(tmp_path: Path) -> None:
    plist = _render(tmp_path)
    assert plist["EnvironmentVariables"]["LANG"] == "en_US.UTF-8"
    assert plist["SoftResourceLimits"]["NumberOfFiles"] == 10240
    args = plist["ProgramArguments"]
    assert args[args.index("--host") + 1] == "0.0.0.0"
    assert args[args.index("--port") + 1] == "8000"


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_loopback_is_an_option_not_the_default(tmp_path: Path) -> None:
    args = _render(tmp_path, HOST="127.0.0.1")["ProgramArguments"]
    assert args[args.index("--host") + 1] == "127.0.0.1"
