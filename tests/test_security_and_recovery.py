import argparse
import errno
import fcntl
import json
import os
import stat
from pathlib import Path

import uantwerpen_dl as toledo_dl

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_session_permissions_and_log_redaction_and_no_ai_upload(isolate_toledo_env):
    # 1. save_state locks down DATA (0700) and STATE (0600) on POSIX
    class _FakeCtx:
        def storage_state(self, path):
            Path(path).write_text('{"cookies": [{"name": "JSESSIONID", "value": "secret"}]}', encoding="utf-8")

    toledo_dl.save_state(_FakeCtx())
    if os.name != "nt":
        assert stat.S_IMODE(toledo_dl.DATA.stat().st_mode) == 0o700
        assert stat.S_IMODE(toledo_dl.STATE.stat().st_mode) == 0o600

    # 2. Signed URLs, API key headers, and Bearer tokens are redacted from logs
    leaked = (
        "Failed GET https://cdn.kaltura.com/p/1/play?ks=SUPERSECRET123&token=ABC999 "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret x-goog-api-key: AIzaTestSecretKey"
    )
    redacted = toledo_dl._redact_secrets(leaked)
    assert "SUPERSECRET123" not in redacted
    assert "ABC999" not in redacted
    assert "eyJhbGciOiJIUzI1NiJ9" not in redacted
    assert "AIzaTestSecretKey" not in redacted
    assert "ks=[REDACTED]" in redacted
    assert "token=[REDACTED]" in redacted
    assert "Bearer [REDACTED]" in redacted
    assert "x-goog-api-key: [REDACTED]" in redacted

    # 3. Dead plaintext key-storage code remains absent and Gemini never passes API keys in URL query strings
    src = (REPO_ROOT / "uantwerpen_dl.py").read_text(encoding="utf-8")
    for forbidden in ("_transcribe_gemini", "save_gemini_key", "generativelanguage.googleapis.com/v1beta/models/?key="):
        assert forbidden not in src
    assert "?key=" not in src


def test_symlink_escape_path_traversal_and_personal_notes_preserved(isolate_toledo_env, monkeypatch, tmp_path):
    out_dir = isolate_toledo_env["out"]
    outside_dir = tmp_path / "outside_victim"
    outside_dir.mkdir()
    victim_file = outside_dir / "keep.txt"
    victim_file.write_text("DO_NOT_TOUCH", encoding="utf-8")

    # 1. Symlink escape inside course folder must be rejected by sync_files
    course_dir = out_dir / "Security Course"
    course_dir.mkdir(parents=True)
    evil_link = course_dir / "Slides"
    evil_link.symlink_to(outside_dir)

    monkeypatch.setattr(
        toledo_dl,
        "gather_files",
        lambda _c, _cid, ext_filter=None: [{
            "content_id": "c1",
            "att_id": "a1",
            "file_name": "evil.pdf",
            "folder_parts": ["Slides"],
            "url": "https://toledo.kuleuven.be/bbcswebdav/evil.pdf",
            "is_link": False,
        }],
    )
    report = {"courses": {}}
    toledo_dl.sync_files(object(), [("cid1", "Security Course")], report=report)
    assert not (outside_dir / "evil.pdf").exists()
    assert report["courses"]["Security Course"]["failed"] == 1

    # 2. _safe_rmtree must never follow or delete an outside symlink
    chapters_dir = course_dir / "LLM_Knowledge" / "Chapters"
    chapters_dir.mkdir(parents=True)
    chap_sym = chapters_dir / "01_Evil"
    chap_sym.symlink_to(outside_dir)
    toledo_dl._safe_rmtree(chap_sym, chapters_dir)
    assert victim_file.exists()

    # 3. Personal notes outside generated folders survive build_llm_index()
    personal_note = course_dir / "My_Personal_Notes.md"
    personal_note.write_text("# My own exam summary\n", encoding="utf-8")
    toledo_dl.build_llm_index()
    assert personal_note.read_text(encoding="utf-8") == "# My own exam summary\n"

    # 4. Failed PDF revision update (ENOSPC) preserves original PDF bytes untouched
    pdf_path = course_dir / "PDFs" / "lecture1.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4 ORIGINAL_BYTES")

    orig_ntf = toledo_dl.tempfile.NamedTemporaryFile

    class _FullDiskTmp:
        def __init__(self, real):
            self._real = real
            self.name = real.name

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._real.close()
            return False

        def write(self, _data):
            raise OSError(errno.ENOSPC, "No space left on device")

        def close(self):
            self._real.close()

    monkeypatch.setattr(toledo_dl.tempfile, "NamedTemporaryFile", lambda *a, **kw: _FullDiskTmp(orig_ntf(*a, **kw)))
    try:
        toledo_dl._replace_bytes(pdf_path, b"%PDF-1.4 CORRUPT_PARTIAL")
    except OSError as exc:
        assert exc.errno == errno.ENOSPC
    assert pdf_path.read_bytes() == b"%PDF-1.4 ORIGINAL_BYTES"


def test_concurrent_sync_lock_expired_login_and_failure_status(isolate_toledo_env, monkeypatch, capsys):
    # 1. Two syncs at once: second sync exits 1 immediately when LOCK is held
    toledo_dl.DATA.mkdir(parents=True, exist_ok=True)
    held = open(toledo_dl.LOCK, "a+", encoding="utf-8")
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc = toledo_dl.cmd_sync(argparse.Namespace(
            course=None, dry_run=False, lectures_only=True, files_only=False,
            no_transcribe=True, audio_only=False, limit=None,
            defer_cpu_transcription=True, notify=False, check_revisions=False,
        ))
        assert rc == 1
        assert "Another toledo-dl run is in progress" in capsys.readouterr().out
    finally:
        held.close()

    # 2. Expired login returns 2 and records "Login required" in sync-status.json
    class _FakePW:
        def __enter__(self):
            return object()

        def __exit__(self, *_a):
            return False

    class _FakeCtx:
        def close(self):
            pass

    monkeypatch.setattr(toledo_dl, "_require_playwright", lambda: _FakePW())
    monkeypatch.setattr(toledo_dl, "open_ctx", lambda _p: _FakeCtx())
    monkeypatch.setattr(toledo_dl, "logged_in", lambda _c: False)

    rc_exp = toledo_dl.cmd_sync(argparse.Namespace(
        course=None, dry_run=False, lectures_only=True, files_only=False,
        no_transcribe=True, audio_only=False, limit=None,
        defer_cpu_transcription=True, notify=False, check_revisions=False,
    ))
    assert rc_exp == 2
    st = json.loads((toledo_dl.DATA / "sync-status.json").read_text(encoding="utf-8"))
    assert st["stage"] == "Login required" and st["finished"] is True

    # 3. Partial/failed download reports "Completed with errors" and exits 1
    monkeypatch.setattr(toledo_dl, "logged_in", lambda _c: True)
    monkeypatch.setattr(toledo_dl, "courses", lambda _c: [("cid1", "Stats")])
    monkeypatch.setattr(toledo_dl, "save_state", lambda _c: None)
    monkeypatch.setattr(toledo_dl, "build_llm_index", lambda: 0)
    monkeypatch.setattr(toledo_dl, "sync_announcements", lambda _c, _cl: 0)

    def _fail_files(_c, _cl, dry_run=False, ext_filter=None, *, report=None, check_revisions=False):
        toledo_dl._count_sync_item(report, "Stats", "failed")
        return 0

    monkeypatch.setattr(toledo_dl, "sync_files", _fail_files)
    monkeypatch.setattr(toledo_dl, "gather", lambda _c, _course=None: [])

    rc_err = toledo_dl.cmd_sync(argparse.Namespace(
        course=None, dry_run=False, lectures_only=False, files_only=True,
        no_transcribe=True, audio_only=False, limit=None,
        defer_cpu_transcription=True, notify=False, check_revisions=False,
    ))
    assert rc_err == 1
    st_err = json.loads((toledo_dl.DATA / "sync-status.json").read_text(encoding="utf-8"))
    assert st_err["stage"] == "Completed with errors"
    assert "1 download(s) failed" in st_err["detail"]


def test_interrupted_onedrive_download_leaves_no_partial_mp4(isolate_toledo_env, monkeypatch):
    dest = isolate_toledo_env["out"] / "Stats" / "Lectures" / "Lecture 01"

    class _FakeDL:
        def __init__(self, fail=True):
            self.fail = fail

        def save_as(self, path):
            Path(path).write_bytes(b"PARTIAL_BYTES")
            if self.fail:
                raise OSError("connection reset mid-stream")

    class _FakeExpect:
        def __init__(self, fail):
            self.value = _FakeDL(fail)

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _FakeBtn:
        def click(self):
            pass

    class _FakePage:
        def __init__(self, fail):
            self.fail = fail

        def goto(self, *_a, **_kw):
            pass

        def expect_download(self, **_kw):
            return _FakeExpect(self.fail)

        def query_selector(self, *_a):
            return _FakeBtn()

        def close(self):
            pass

    class _FakeCtx:
        def __init__(self, fail):
            self.fail = fail

        def new_page(self):
            return _FakePage(self.fail)

    monkeypatch.setattr(toledo_dl.time, "sleep", lambda _s: None)
    # First attempt interrupted: must return False and leave NO .mp4 or .tmp.mp4
    ok1 = toledo_dl.download_onedrive_media(_FakeCtx(fail=True), "https://onedrive.example/v?token=SECRET", dest)
    assert ok1 is False
    assert not dest.with_suffix(".mp4").exists()
    assert not dest.with_suffix(".tmp.mp4").exists()

    # Retry succeeds atomically
    monkeypatch.setattr(toledo_dl.subprocess, "run", lambda *_a, **_kw: argparse.Namespace(returncode=1))
    ok2 = toledo_dl.download_onedrive_media(_FakeCtx(fail=False), "https://onedrive.example/v?token=SECRET", dest)
    assert ok2 is True
    assert dest.with_suffix(".mp4").exists()
    assert not dest.with_suffix(".tmp.mp4").exists()
