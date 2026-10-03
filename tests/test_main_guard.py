"""Tests for the interpreter version guard in jobreach.__main__."""

from __future__ import annotations

import subprocess
import sys

from jobreach.__main__ import unsupported_message


def test_supported_version_returns_empty_string():
    assert unsupported_message((3, 12, 0)) == ""
    assert unsupported_message((3, 11, 0)) == ""
    assert unsupported_message((3, 14, 0)) == ""


def test_unsupported_version_names_minimum_running_and_uv_command():
    msg = unsupported_message((3, 9, 6))
    assert "3.11" in msg
    assert "3.9" in msg
    assert "uv run --python" in msg


def test_main_module_execution_with_version_flag():
    proc = subprocess.run(
        [sys.executable, "-m", "jobreach", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "jobreach" in proc.stdout
