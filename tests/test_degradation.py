import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import uantwerpen_dl as toledo_dl

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_heavy_ml_imports_are_lazy():
    res = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.path.insert(0, '.'); import uantwerpen_dl as toledo_dl; "
                "loaded = [m for m in ('faster_whisper', 'ctranslate2', 'torch') if m in sys.modules]; "
                "print(','.join(loaded))"
            ),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.strip() == ""


def test_require_playwright_missing_raises_friendly_error(monkeypatch):
    monkeypatch.setattr(toledo_dl, "sync_playwright", None)
    with pytest.raises(RuntimeError, match="Playwright is not installed"):
        toledo_dl._require_playwright()


def test_graceful_fallback_when_ollama_offline(isolate_toledo_env, monkeypatch):
    out_dir = isolate_toledo_env["out"]
    course_dir = out_dir / "Statistical Inference"
    lec_dir = course_dir / "Lectures"
    pdf_dir = course_dir / "Slides"
    lec_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    mp3 = lec_dir / "Lecture 04 - Session (10-14).mp3"
    txt = lec_dir / "Lecture 04 - Session (10-14).txt"
    txt_topic = lec_dir / "Lecture 04 - 4. Convergence of Random Variables.txt"
    pdf1 = pdf_dir / "1-proba.pdf"
    pdf4 = pdf_dir / "4-conv.pdf"

    mp3.write_bytes(b"FAKE")
    txt.write_text("Today we study convergence in probability and central limit theorems.\n" * 10, encoding="utf-8")
    txt_topic.write_text("Today we cover convergence of random variables and Slutsky's theorem.\n" * 10, encoding="utf-8")
    pdf1.write_bytes(b"%PDF-1")
    pdf4.write_bytes(b"%PDF-4")

    def _refused(*_args, **_kwargs):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _refused)

    # 1. Vague lecture renamer returns original path cleanly when Ollama is down
    renamed = toledo_dl._maybe_rename_vague_lecture(mp3)
    assert renamed == mp3
    assert mp3.exists()

    # 2. Match-slides falls back deterministically and creates Transcripts & Slides pairing
    entry = toledo_dl.match_transcript_with_pdfs(txt_topic, course_dir)
    assert entry is not None
    assert entry["matched_pdfs"] == ["4-conv.pdf"]
    pair_dir = course_dir / "Transcripts & Slides" / txt_topic.stem
    assert (pair_dir / "4-conv.pdf").exists()
    assert (pair_dir / txt_topic.name).exists()
