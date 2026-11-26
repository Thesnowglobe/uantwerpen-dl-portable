import os
import subprocess
import sys
from pathlib import Path

import pytest

import uantwerpen_dl as toledo_dl

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_toledo_dl_help_and_subcommands(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["toledo-dl", "--help"])
    with pytest.raises(SystemExit) as exc:
        toledo_dl.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for cmd in (
        "login",
        "check",
        "doctor",
        "calendar",
        "list",
        "sync",
        "files",
        "pdfs",
        "announcements",
        "transcribe",
        "match-slides",
        "index",
        "search",
    ):
        assert cmd in out


@pytest.mark.parametrize(
    "subcmd,expected_flags",
    [
        ("sync", ["--course", "--limit", "--video", "--audio-only", "--dry-run", "--lectures-only", "--files-only", "--transcribe", "--no-transcribe", "--model", "--lang"]),
        ("files", ["--course", "--ext", "--dry-run"]),
        ("pdfs", ["--course", "--dry-run"]),
        ("announcements", ["--course", "--all"]),
        ("calendar", ["--course", "--semester", "--refresh", "--upcoming", "--json"]),
        ("transcribe", ["--course", "--file", "--model", "--lang", "--dry-run"]),
        ("match-slides", ["--course", "--model", "--force"]),
        ("index", ["--course"]),
        ("search", ["--course", "--kind", "--limit", "--context", "--json"]),
    ],
)
def test_subcommand_help_flags(subcmd, expected_flags, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["toledo-dl", subcmd, "--help"])
    with pytest.raises(SystemExit) as exc:
        toledo_dl.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in expected_flags:
        assert flag in out


def test_bin_toledo_sync_usage(tmp_path):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    res = subprocess.run(
        ["bash", str(REPO_ROOT / "bin" / "uantwerpen-sync")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res.returncode == 0
    for cmd in ("enable", "disable", "status", "run", "log"):
        assert cmd in res.stdout
