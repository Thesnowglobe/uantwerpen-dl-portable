import argparse
import json
import shutil
import sys
import types
import urllib.error
import urllib.request

import uantwerpen_dl as toledo_dl


def test_doctor_all_ok(isolate_toledo_env, monkeypatch, capsys):
    # Fake ffmpeg & yt-dlp in PATH
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}" if name in ("ffmpeg", "yt-dlp", "google-chrome") else None)
    monkeypatch.setattr(toledo_dl, "sync_playwright", lambda: None)

    # Valid saved session cookies and Gemini key
    state_file = isolate_toledo_env["data"] / "state.json"
    state_file.write_text(json.dumps({"cookies": [{"name": "sid", "value": "abc"}]}), encoding="utf-8")
    (isolate_toledo_env["data"] / "gemini_key.txt").write_text("fake-key\n", encoding="utf-8")

    # Fake ctranslate2 with 2 CUDA GPUs
    fake_ct2 = types.ModuleType("ctranslate2")
    fake_ct2.get_cuda_device_count = lambda: 2
    monkeypatch.setitem(sys.modules, "ctranslate2", fake_ct2)

    # Fake Ollama reachable
    class FakeOllamaResp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps({"models": [{"name": "phi4:14b"}]}).encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_kw: FakeOllamaResp())

    rc = toledo_dl.cmd_doctor(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "[OK]      Python" in out
    assert "[OK]      ffmpeg" in out
    assert "[OK]      yt-dlp" in out
    assert "[OK]      Browser: Google Chrome + Playwright" in out
    assert "[OK]      Toledo login session saved" in out
    assert "[OK]      Compute backend: 2 CUDA GPU(s)" in out
    assert "[OK]      Local Ollama reachable" in out


def test_doctor_missing_deps_and_warnings(isolate_toledo_env, monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(toledo_dl, "YTDLP", "/nonexistent/yt-dlp")
    monkeypatch.setattr(toledo_dl, "_browser_channel", lambda: None)
    monkeypatch.setattr(toledo_dl, "sync_playwright", None)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_kw: (_ for _ in ()).throw(urllib.error.URLError("down")))
    monkeypatch.setitem(sys.modules, "ctranslate2", None)

    rc = toledo_dl.cmd_doctor(argparse.Namespace())
    assert rc == 1
    out = capsys.readouterr().out
    assert "[MISSING] ffmpeg" in out
    assert "[MISSING] yt-dlp" in out
    assert "[MISSING] Playwright not installed" in out
    assert "[WARN]    No saved Toledo login session" in out
    assert "toledo-login" in out
    assert "[WARN]    Local Ollama not running" in out


def test_unauthenticated_sync_and_list_return_friendly_login_prompt(isolate_toledo_env, monkeypatch, capsys):
    class UnauthPlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_a):
            return False

    class FakeCtx:
        def close(self):
            pass

    monkeypatch.setattr(toledo_dl, "sync_playwright", lambda: UnauthPlaywright())
    monkeypatch.setattr(toledo_dl, "open_ctx", lambda _p, headless=True: FakeCtx())
    monkeypatch.setattr(toledo_dl, "logged_in", lambda _c: False)

    rc_list = toledo_dl.cmd_list(argparse.Namespace(course=None))
    out_list = capsys.readouterr().out
    assert rc_list == 2
    assert "uantwerpen-login" in out_list
    assert "Traceback" not in out_list

    rc_sync = toledo_dl.cmd_sync(
        argparse.Namespace(
            course=None,
            limit=None,
            video=True,
            audio_only=False,
            dry_run=True,
            lectures_only=False,
            files_only=False,
            no_transcribe=True,
            model="auto",
            lang="en",
        )
    )
    out_sync = capsys.readouterr().out
    assert rc_sync == 2
    assert "uantwerpen-login" in out_sync
    assert "Traceback" not in out_sync
