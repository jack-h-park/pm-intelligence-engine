"""The launchd plist template that `make install-service` fills in."""

import os
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


@pytest.fixture
def launchd_environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    binary = tmp_path / "launchctl"
    commands = tmp_path / "commands"
    binary.write_text(
        r"""#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_CALLS"
case "$*" in
    "print system/com.jackpark.pm-engine")
        [ "$SYSTEM_STATUS" = 0 ] || exit "$SYSTEM_STATUS"
        printf 'system/com.jackpark.pm-engine is running\n'
        ;;
    print\ gui/*) printf 'GUI registration is running\n' ;;
esac
"""
    )
    binary.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "LAUNCHCTL_CALLS": str(commands),
        "SYSTEM_STATUS": "0",
    }
    return env, commands, home


@pytest.mark.parametrize(
    "target",
    ["install-service", "render-plist", "service-start", "service-stop",
     "service-restart", "uninstall-service"],
)
def test_gui_mutation_refuses_installed_system_job(
    launchd_environment: tuple[dict[str, str], Path, Path], target: str
) -> None:
    env, commands, home = launchd_environment
    result = subprocess.run(
        ["make", target, "UVICORN=/opt/venv/bin/uvicorn"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "system/com.jackpark.pm-engine" in result.stderr
    assert commands.read_text().splitlines() == ["print system/com.jackpark.pm-engine"]
    assert not (home / "Library" / "LaunchAgents").exists()


def test_status_reports_registered_system_domain(
    launchd_environment: tuple[dict[str, str], Path, Path],
) -> None:
    env, commands, _ = launchd_environment
    result = subprocess.run(
        ["make", "service-status"], cwd=ROOT, env=env, check=True, capture_output=True, text=True
    )
    assert "system/com.jackpark.pm-engine is running" in result.stdout
    assert commands.read_text().splitlines() == ["print system/com.jackpark.pm-engine"]


def test_gui_restart_preserves_existing_target_when_system_job_is_absent(
    launchd_environment: tuple[dict[str, str], Path, Path],
) -> None:
    env, commands, _ = launchd_environment
    env["SYSTEM_STATUS"] = "113"
    subprocess.run(
        ["make", "service-restart"], cwd=ROOT, env=env, check=True, capture_output=True, text=True
    )
    assert commands.read_text().splitlines() == [
        "print system/com.jackpark.pm-engine",
        f"kickstart -k gui/{os.getuid()}/com.jackpark.pm-engine",
    ]


def test_refuses_gui_mutation_if_system_inspection_fails(
    launchd_environment: tuple[dict[str, str], Path, Path],
) -> None:
    env, commands, _ = launchd_environment
    env["SYSTEM_STATUS"] = "1"
    result = subprocess.run(
        ["make", "service-restart"], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "Could not inspect" in result.stderr
    assert commands.read_text().splitlines() == ["print system/com.jackpark.pm-engine"]
