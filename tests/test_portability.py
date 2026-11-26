import ast
import json
import os
import urllib.request
from datetime import date
from pathlib import Path

import uantwerpen_dl as toledo_dl

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_all_text_io_explicitly_uses_utf8():
    for fname in ("uantwerpen_dl.py",):
        src = (REPO_ROOT / fname).read_text(encoding="utf-8")
        tree = ast.parse(src, filename=fname)
        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = ""
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("read_text", "write_text"):
                call_name = node.func.attr
            elif isinstance(node.func, ast.Name) and node.func.id == "open":
                is_bin = any(
                    isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "b" in arg.value
                    for arg in node.args[1:2]
                ) or any(
                    kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str) and "b" in kw.value.value
                    for kw in node.keywords
                )
                if not is_bin:
                    call_name = "open"
            if call_name:
                has_enc = any(kw.arg == "encoding" for kw in node.keywords)
                if not has_enc:
                    missing.append((node.lineno, call_name))
        assert missing == [], f"{fname} has text I/O without encoding=: {missing}"


def test_windows_symlink_fallback_in_match_and_chapters(isolate_toledo_env, monkeypatch):
    out_dir = isolate_toledo_env["out"]
    course_dir = out_dir / "Probability Theory"
    lec_dir = course_dir / "Lectures"
    pdf_dir = course_dir / "Slides"
    lec_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    mp3 = lec_dir / "Lecture 01 - Measure & σ-Fields.mp3"
    txt = lec_dir / "Lecture 01 - Measure & σ-Fields.txt"
    srt = lec_dir / "Lecture 01 - Measure & σ-Fields.srt"
    pdf = pdf_dir / "01_Measure_Theory.pdf"

    mp3.write_bytes(b"FAKE_MP3")
    txt.write_text("Welcome to Measure Theory and sigma-fields with μ and Σ.\n" * 15, encoding="utf-8")
    srt.write_text("1\n00:00:00,000 --> 00:00:05,000\nWelcome to Measure Theory.\n", encoding="utf-8")
    pdf.write_bytes(b"%PDF-1.4 mock")

    # Mock Ollama match response
    class FakeHTTPResp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "response": json.dumps({
                    "matched_pdfs": ["01_Measure_Theory.pdf"],
                    "confidence": "high",
                    "reason": "Topic matches",
                })
            }).encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_kw: FakeHTTPResp())

    # Simulate Windows unprivileged symlink failure
    def _deny_symlink(*_args, **_kwargs):
        raise OSError("WinError 1314: A required privilege is not held by the client")

    monkeypatch.setattr(os, "symlink", _deny_symlink)
    monkeypatch.setattr(Path, "symlink_to", _deny_symlink)

    entry = toledo_dl.match_transcript_with_pdfs(txt, course_dir)
    assert entry is not None
    assert entry["matched_pdfs"] == ["01_Measure_Theory.pdf"]

    pair_dir = course_dir / "Transcripts & Slides" / txt.stem
    assert (pair_dir / txt.name).is_file() and not (pair_dir / txt.name).is_symlink()
    assert (pair_dir / pdf.name).is_file() and not (pair_dir / pdf.name).is_symlink()

    # Build LLM index + Chapters under symlink failure
    indexed = toledo_dl.build_llm_index([course_dir])
    assert indexed >= 2
    chap_dir = course_dir / "LLM_Knowledge" / "Chapters" / "01_01_Measure_Theory"
    assert (chap_dir / pdf.name).is_file() and not (chap_dir / pdf.name).is_symlink()
    assert (chap_dir / txt.name).is_file() and not (chap_dir / txt.name).is_symlink()
    bundle = (chap_dir / "CHAPTER_BUNDLE.md").read_text(encoding="utf-8")
    assert "μ and Σ" in bundle


def test_recording_subfolder_and_dynamic_academic_year():
    assert toledo_dl._recording_subfolder("Any Arbitrary Course", "kaltura", "Lecture 01") == "Lectures"
    assert toledo_dl._recording_subfolder("Linear Models [G0Z50a]", "onedrive", "ANOVA Lecture 01") == "ANOVA Lectures"
    assert toledo_dl._recording_subfolder("Linear Models [G0Z50a]", "kaltura", "Lecture 01") == "Regression Analysis"
    assert toledo_dl._recording_subfolder("Any Course", "kaltura", "Lecture 01", custom_subfolder="Videos") == "Videos"

    assert toledo_dl._academic_year_for_month(10, today=date(2028, 11, 1)) == 2028
    assert toledo_dl._academic_year_for_month(2, today=date(2028, 11, 1)) == 2029
    assert toledo_dl._academic_year_for_month(10, today=date(2029, 3, 15)) == 2028
    assert toledo_dl._academic_year_for_month(3, today=date(2029, 3, 15)) == 2029
