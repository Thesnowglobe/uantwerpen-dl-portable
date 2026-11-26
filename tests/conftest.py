import os
import sys
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def isolate_toledo_env(tmp_path, monkeypatch):
    """Isolate all paths and block real network/Playwright calls for every test."""
    out_dir = tmp_path / "University"
    data_dir = tmp_path / "toledo-data"
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("TOLEDO_LECTURES_DIR", str(out_dir))
    monkeypatch.setenv("TOLEDO_DATA_DIR", str(data_dir))

    import uantwerpen_dl as toledo_dl

    monkeypatch.setattr(toledo_dl, "OUT", out_dir)
    monkeypatch.setattr(toledo_dl, "DATA", data_dir)
    monkeypatch.setattr(toledo_dl, "PROFILE", data_dir / "profile")
    monkeypatch.setattr(toledo_dl, "STATE", data_dir / "state.json")
    monkeypatch.setattr(toledo_dl, "LOCK", data_dir / "lock")
    monkeypatch.setattr(toledo_dl, "DB", out_dir / ".downloaded.json")
    monkeypatch.setattr(toledo_dl, "PDF_DB", out_dir / ".downloaded_pdfs.json")
    monkeypatch.setattr(toledo_dl, "FILES_DB", out_dir / ".downloaded_files.json")
    monkeypatch.setattr(toledo_dl, "ANNOUNCEMENTS_DB", out_dir / ".checked_announcements.json")
    monkeypatch.setattr(toledo_dl, "CALENDAR_DB", out_dir / ".calendar_schedule.json")

    def _blocked_urlopen(*_args, **_kwargs):
        raise AssertionError("Real network call attempted in offline test! Mock urllib.request.urlopen.")

    def _blocked_playwright(*_args, **_kwargs):
        raise AssertionError("Real Playwright session attempted in offline test! Mock sync_playwright.")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked_urlopen)
    monkeypatch.setattr(toledo_dl, "sync_playwright", _blocked_playwright)

    # Guard against accidental reads/writes to real user directories
    assert str(toledo_dl.OUT) != str(Path.home() / "Desktop" / "University")
    assert str(toledo_dl.DATA) != str(Path.home() / ".local" / "share" / "toledo-dl")
    assert os.environ["TOLEDO_LECTURES_DIR"] == str(out_dir)

    return {"out": out_dir, "data": data_dir}
