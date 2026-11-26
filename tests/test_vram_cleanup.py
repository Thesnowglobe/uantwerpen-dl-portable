import gc
import sys
import types

import uantwerpen_dl as toledo_dl


def test_transcribe_file_releases_vram_before_ollama(isolate_toledo_env, monkeypatch):
    out_dir = isolate_toledo_env["out"]
    course_dir = out_dir / "Applied Stats"
    lec_dir = course_dir / "Lectures"
    lec_dir.mkdir(parents=True, exist_ok=True)
    mp3 = lec_dir / "Lecture 01 - Intro to Regression.mp3"
    mp3.write_bytes(b"FAKE_MP3")

    events = []

    class FakeSegment:
        start = 0.0
        end = 2.0
        text = "Hello linear regression."

    class FakeInfo:
        language = "en"
        language_probability = 0.99

    class FakeWhisperModel:
        def __init__(self, *_a, **_kw):
            events.append("model_init")

        def transcribe(self, *_a, **_kw):
            events.append("model_transcribe")
            return [FakeSegment()], FakeInfo()

        def __del__(self):
            events.append("model_del")

    fake_fw = types.ModuleType("faster_whisper")
    fake_fw.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)

    fake_cuda = types.SimpleNamespace(
        is_available=lambda: True,
        empty_cache=lambda: events.append("cuda_empty_cache"),
    )
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = fake_cuda
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    orig_collect = gc.collect
    monkeypatch.setattr(gc, "collect", lambda: (events.append("gc_collect"), orig_collect())[1])

    def _fake_match(txt_dest, cdir):
        events.append("match_slides")
        return None

    monkeypatch.setattr(toledo_dl, "match_transcript_with_pdfs", _fake_match)

    ok = toledo_dl.transcribe_file(mp3, model_size="medium", language="en", device_index=1)
    assert ok is True
    assert "gc_collect" in events
    assert "cuda_empty_cache" in events
    assert "match_slides" in events
    assert events.index("cuda_empty_cache") < events.index("match_slides")
    assert events.index("gc_collect") < events.index("cuda_empty_cache")
