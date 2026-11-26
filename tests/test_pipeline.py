import argparse
import json
import math
import sys
import types
from pathlib import Path

import pytest

import uantwerpen_dl as toledo_dl


class FakeResponse:
    def __init__(self, status=200, json_data=None, body=b"", text_data=""):
        self.status = status
        self._json = json_data
        self._body = body
        self._text = text_data

    def json(self):
        return self._json

    def body(self):
        return self._body

    def text(self):
        return self._text


class FakeRequestContext:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        handler = self.routes.get(url)
        if handler is None:
            return FakeResponse(status=404)
        return handler(url) if callable(handler) else handler


class FakeBrowserContext:
    def __init__(self, routes):
        self.request = FakeRequestContext(routes)

    def close(self):
        pass


def test_fetch_all_contents_pagination_and_401():
    base = toledo_dl.BASE
    routes = {
        f"{base}/learn/api/public/v1/courses/c1/contents?limit=2": FakeResponse(
            json_data={
                "results": [{"id": "1"}, {"id": "2"}],
                "paging": {"nextPage": "/learn/api/public/v1/courses/c1/contents?offset=2"},
            }
        ),
        f"{base}/learn/api/public/v1/courses/c1/contents?offset=2": FakeResponse(
            json_data={
                "results": [{"id": "3"}],
                "paging": {"nextPage": f"{base}/learn/api/public/v1/courses/c1/contents?offset=3"},
            }
        ),
        f"{base}/learn/api/public/v1/courses/c1/contents?offset=3": FakeResponse(
            json_data={"results": [{"id": "4"}], "paging": {}}
        ),
        f"{base}/learn/api/public/v1/courses/c_unauth/contents": FakeResponse(status=401),
    }
    ctx = FakeBrowserContext(routes)
    items = toledo_dl._fetch_all_contents(ctx, "/learn/api/public/v1/courses/c1/contents?limit=2")
    assert [it["id"] for it in items] == ["1", "2", "3", "4"]

    with pytest.raises(toledo_dl.LoginRequired):
        toledo_dl._fetch_all_contents(ctx, "/learn/api/public/v1/courses/c_unauth/contents")


def test_extract_body_files_and_gather_ultra_documents():
    base = toledo_dl.BASE
    bb_json = json.dumps({
        "displayName": "ANOVA & Slides.pdf",
        "resourceUrl": "/bbcswebdav/xid-101_1",
    }).replace('"', "&quot;")
    standalone_json = json.dumps({
        "originalName": "dataset_clean.csv",
        "resourceUrl": "/bbcswebdav/xid-202_1",
    }).replace('"', "&quot;")
    doc_body = (
        f'<p><a href="{base}/bbcswebdav/xid-101_1" data-bbfile="{bb_json}">Slides</a></p>'
        f'<div data-bbfile="{standalone_json}"></div>'
        f'<p><a href="{base}/bbcswebdav/xid-101_1">Duplicate Link</a></p>'
    )

    routes = {
        f"{base}/learn/api/public/v1/courses/c1/contents?limit=200": FakeResponse(
            json_data={
                "results": [
                    {
                        "id": "folder1",
                        "title": "Module II: ANOVA/Slides?",
                        "hasChildren": True,
                        "contentHandler": {"id": "resource/x-bb-folder"},
                    },
                    {
                        "id": "ext1",
                        "title": "OneDrive Recording Link",
                        "hasChildren": False,
                        "contentHandler": {"id": "resource/x-bb-externallink", "url": "https://example.com/lec1"},
                    },
                ]
            }
        ),
        f"{base}/learn/api/public/v1/courses/c1/contents/folder1/children?limit=200": FakeResponse(
            json_data={
                "results": [
                    {
                        "id": "udoc1",
                        "title": "ultraDocumentBody",
                        "hasChildren": True,
                        "body": doc_body,
                        "contentHandler": {"id": "resource/x-bb-document"},
                    }
                ]
            }
        ),
        f"{base}/learn/api/public/v1/courses/c1/contents/udoc1/children?limit=200": FakeResponse(
            json_data={"results": []}
        ),
        f"{base}/learn/api/public/v1/courses/c1/contents/udoc1/attachments": FakeResponse(json_data={"results": []}),
    }
    ctx = FakeBrowserContext(routes)

    all_files = toledo_dl.gather_files(ctx, "c1")
    names = [f["file_name"] for f in all_files]
    assert "ANOVA & Slides.pdf" in names
    assert "dataset_clean.csv" in names
    assert "OneDrive Recording Link.url" in names
    # Duplicate bbcswebdav link deduplicated via bb:<url_path>
    assert names.count("ANOVA & Slides.pdf") == 1
    # ultraDocumentBody wrapper skipped from folder_parts
    pdf_item = next(f for f in all_files if f["file_name"] == "ANOVA & Slides.pdf")
    assert pdf_item["folder_parts"] == ["Module II- ANOVA-Slides-"]

    pdfs_only = toledo_dl.gather_pdfs(ctx, "c1")
    assert [p["file_name"] for p in pdfs_only] == ["ANOVA & Slides.pdf"]

    csv_only = toledo_dl.gather_files(ctx, "c1", ext_filter={"csv"})
    assert [f["file_name"] for f in csv_only] == ["dataset_clean.csv"]


def test_sync_files_idempotency_links_videos_and_collisions(isolate_toledo_env):
    base = toledo_dl.BASE
    out_dir = isolate_toledo_env["out"]
    bb_a = json.dumps({"displayName": "notes.pdf", "resourceUrl": "/bbcswebdav/xid-a"}).replace('"', "&quot;")
    bb_b = json.dumps({"displayName": "notes.pdf", "resourceUrl": "/bbcswebdav/xid-b"}).replace('"', "&quot;")
    doc_body = (
        f'<a href="{base}/bbcswebdav/xid-a" data-bbfile="{bb_a}">A</a>'
        f'<a href="{base}/bbcswebdav/xid-b" data-bbfile="{bb_b}">B</a>'
    )

    download_hits = []

    def _make_bytes_handler(payload):
        def _handle(url):
            download_hits.append(url)
            return FakeResponse(status=200, body=payload)
        return _handle

    routes = {
        f"{base}/learn/api/public/v1/courses/c1/contents?limit=200": FakeResponse(
            json_data={
                "results": [
                    {
                        "id": "doc1",
                        "title": "Week 1",
                        "hasChildren": False,
                        "body": doc_body,
                        "contentHandler": {"id": "resource/x-bb-document"},
                    },
                    {
                        "id": "vid1",
                        "title": "Intro Clip",
                        "hasChildren": False,
                        "contentHandler": {"id": "resource/x-bb-file", "file": {"fileName": "demo.mp4"}},
                    },
                    {
                        "id": "lnk1",
                        "title": "Course Repo",
                        "hasChildren": False,
                        "contentHandler": {"id": "resource/x-bb-externallink", "url": "https://example.org/repo"},
                    },
                ]
            }
        ),
        f"{base}/learn/api/public/v1/courses/c1/contents/doc1/attachments": FakeResponse(json_data={"results": []}),
        f"{base}/learn/api/public/v1/courses/c1/contents/vid1/attachments": FakeResponse(
            json_data={"results": [{"id": "att_vid", "fileName": "demo.mp4"}]}
        ),
        f"{base}/learn/api/public/v1/courses/c1/contents/vid1/attachments/att_vid/download": _make_bytes_handler(b"MP4DATA"),
        f"{base}/bbcswebdav/xid-a": _make_bytes_handler(b"%PDF-A"),
        f"{base}/bbcswebdav/xid-b": _make_bytes_handler(b"%PDF-B"),
    }
    ctx = FakeBrowserContext(routes)
    course_list = [("c1", "Statistics 101 [G0A17a]")]

    first_count = toledo_dl.sync_files(ctx, course_list, dry_run=False)
    assert first_count == 4
    assert len(download_hits) == 3

    course_dir = out_dir / "Statistics 101 [G0A17a]"
    assert (course_dir / "PDFs" / "notes.pdf").read_bytes() == b"%PDF-A"
    assert (course_dir / "PDFs" / "notes_2.pdf").read_bytes() == b"%PDF-B"
    assert (course_dir / "Recordings" / "demo.mp4").read_bytes() == b"MP4DATA"
    assert "[InternetShortcut]\nURL=https://example.org/repo\n" in (course_dir / "Files" / "Course Repo.url").read_text(encoding="utf-8")
    links_md = (course_dir / "Links.md").read_text(encoding="utf-8")
    assert links_md.count("https://example.org/repo") == 1

    # Second run must be completely idempotent (0 new downloads)
    download_hits.clear()
    second_count = toledo_dl.sync_files(ctx, course_list, dry_run=False)
    assert second_count == 0
    assert download_hits == []
    assert (course_dir / "Links.md").read_text(encoding="utf-8").count("https://example.org/repo") == 1


def test_attachment_revision_preserves_previous_bytes(isolate_toledo_env, monkeypatch):
    item = {"content_id": "c", "att_id": "a", "file_name": "assignment.pdf", "remote_version": "v1"}
    monkeypatch.setattr(toledo_dl, "gather_files", lambda *_args, **_kwargs: [item])
    url = f"{toledo_dl.BASE}/learn/api/public/v1/courses/course/contents/c/attachments/a/download"
    routes = {url: FakeResponse(body=b"%PDF-first")}
    ctx = FakeBrowserContext(routes)
    course_list = [("course", "Statistics")]
    report = {"courses": {}}

    assert toledo_dl.sync_files(ctx, course_list, report=report) == 1
    assert report["courses"]["Statistics"] == {"files": 1}
    path = isolate_toledo_env["out"] / "Statistics" / "PDFs" / "assignment.pdf"
    ctx.request.calls.clear()
    assert toledo_dl.sync_files(ctx, course_list) == 0
    assert ctx.request.calls == []

    item["remote_version"] = "v2"
    routes[url] = FakeResponse(status=503)
    report = {"courses": {}}
    assert toledo_dl.sync_files(ctx, course_list, report=report) == 0
    assert report["courses"]["Statistics"] == {"failed": 1}
    assert path.read_bytes() == b"%PDF-first"

    routes[url] = FakeResponse(body=b"%PDF-second")
    report = {"courses": {}}
    assert toledo_dl.sync_files(ctx, course_list, report=report) == 0
    assert report["courses"]["Statistics"] == {"revised": 1}
    assert path.read_bytes() == b"%PDF-second"
    revisions = list((isolate_toledo_env["data"] / "revisions").rglob("assignment.pdf"))
    assert [p.read_bytes() for p in revisions] == [b"%PDF-first"]

    item.pop("remote_version")
    routes[url] = FakeResponse(body=b"%PDF-third")
    assert toledo_dl.sync_files(ctx, course_list, check_revisions=True) == 0
    assert path.read_bytes() == b"%PDF-third"


def test_background_transcription_defers_cpu(isolate_toledo_env, monkeypatch):
    from argparse import Namespace

    recording = isolate_toledo_env["out"] / "Statistics" / "Lecture 1.mp3"
    recording.parent.mkdir(parents=True)
    recording.write_bytes(b"audio")
    monkeypatch.setattr(toledo_dl, "_cuda_device_count", lambda: 0)
    monkeypatch.setattr(toledo_dl, "transcribe_file", lambda *_args, **_kwargs: pytest.fail("CPU work must be deferred"))
    report = {"courses": {}}
    assert toledo_dl.cmd_transcribe(Namespace(course=None, model="auto", lang="en", dry_run=False,
                                            defer_cpu=True, report=report)) == 0
    assert report["transcription_note"].startswith("1 transcription(s) deferred")


def test_sync_announcements_and_idempotency(isolate_toledo_env):
    base = toledo_dl.BASE
    out_dir = isolate_toledo_env["out"]
    ann_body = (
        f'<p>Exam room changed.<br/>See <a href="{base}/bbcswebdav/ann-1">schedule.pdf</a></p>'
    )
    routes = {
        f"{base}/learn/api/public/v1/courses/c1/announcements?limit=50": FakeResponse(
            json_data={
                "results": [
                    {
                        "id": "a1",
                        "title": "Room Update",
                        "created": "2026-11-10T09:15:00.000Z",
                        "body": ann_body,
                    }
                ]
            }
        ),
        f"{base}/bbcswebdav/ann-1": FakeResponse(status=200, body=b"%PDF-ANN"),
    }
    ctx = FakeBrowserContext(routes)
    course_list = [("c1", "Linear Models [G0Z50a]")]

    new1 = toledo_dl.sync_announcements(ctx, course_list)
    assert new1 == 1
    course_dir = out_dir / "Linear Models [G0Z50a]"
    assert (course_dir / "Announcements" / "schedule.pdf").read_bytes() == b"%PDF-ANN"
    ann_md = (course_dir / "Announcements.md").read_text(encoding="utf-8")
    assert "### Room Update" in ann_md
    assert "Exam room changed." in ann_md

    # Second run sees 0 new announcements
    new2 = toledo_dl.sync_announcements(ctx, course_list)
    assert new2 == 0


def test_filename_sanitization_and_max_path(tmp_path):
    assert toledo_dl.clean('Chapter 1: Intro / Models? <Test>* [CS101]') == "Chapter 1- Intro - Models- -Test-"
    assert toledo_dl.clean("   ...   ") == "untitled"

    long_stem = "A" * 200
    cleaned = toledo_dl.clean_filename(f"bad:name/{long_stem}.pdf")
    assert cleaned.endswith(".pdf")
    assert ":" not in cleaned and "/" not in cleaned
    assert cleaned.startswith("bad-name-")
    assert len(Path(cleaned).stem) <= 120

    deep_dir = tmp_path / ("sub_" * 25)
    deep_dir.mkdir(parents=True, exist_ok=True)
    dest = toledo_dl._unique_dest(deep_dir, "X" * 180 + ".pdf", "k1", {})
    assert len(str(dest)) <= 245
    assert dest.suffix == ".pdf"


def test_audit_windows_reserved_and_vague_rename_collision(isolate_toledo_env, monkeypatch):
    import ntpath

    for bad in ("CON.pdf", "NUL.txt", "AUX.zip"):
        cleaned = toledo_dl.clean_filename(bad)
        assert cleaned.startswith("_"), f"{bad} was not prefixed: {cleaned}"
        assert not ntpath.isreserved(cleaned)

    out_dir = isolate_toledo_env["out"]
    lec_dir = out_dir / "Stats" / "Lectures"
    lec_dir.mkdir(parents=True, exist_ok=True)

    mp3 = lec_dir / "Lecture 01 - Session (09-23).mp3"
    txt = lec_dir / "Lecture 01 - Session (09-23).txt"
    vtt = lec_dir / "Lecture 01 - Session (09-23).vtt"
    unrelated_vtt = lec_dir / "Lecture 01 - Regression.vtt"

    mp3.write_bytes(b"MP3")
    txt.write_text("Regression analysis lecture content.", encoding="utf-8")
    vtt.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nMine.\n", encoding="utf-8")
    unrelated_vtt.write_text("UNRELATED_SUBTITLE", encoding="utf-8")

    monkeypatch.setattr(toledo_dl, "_suggest_lecture_topic", lambda _p: "Regression")
    renamed = toledo_dl._maybe_rename_vague_lecture(mp3)
    assert renamed.stem == "Lecture 01 - Regression (2)"
    assert unrelated_vtt.read_text(encoding="utf-8") == "UNRELATED_SUBTITLE"
    assert (lec_dir / "Lecture 01 - Regression (2).vtt").exists()


def test_audit_same_title_kaltura_recordings_do_not_collide(isolate_toledo_env, monkeypatch):
    import sys

    out_dir = isolate_toledo_env["out"]

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
    monkeypatch.setattr(toledo_dl, "logged_in", lambda _c: True)
    monkeypatch.setattr(toledo_dl, "courses", lambda _c: [("c1", "Stats 101")])
    monkeypatch.setattr(
        toledo_dl,
        "gather",
        lambda _c, _course=None: [
            {"type": "kaltura", "entry": "1_entryAAA", "course_id": "c1", "content_id": "cnt1", "course": "Stats 101", "title": "Recording 1"},
            {"type": "kaltura", "entry": "1_entryBBB", "course_id": "c1", "content_id": "cnt2", "course": "Stats 101", "title": "Recording 1"},
        ],
    )
    monkeypatch.setattr(toledo_dl, "get_kaltura_ks", lambda *_a, **_kw: "ks_token")
    monkeypatch.setattr(toledo_dl, "save_state", lambda _c: None)
    monkeypatch.setattr(toledo_dl, "build_llm_index", lambda: 0)

    dl_calls = []

    def _mock_dl(_urls, dest, video=True, progress=None):
        progress("[download] 50.0% ETA 00:10")
        status = json.loads((isolate_toledo_env["data"] / "sync-status.json").read_text(encoding="utf-8"))
        assert f"Recording {len(dl_calls) + 1}/2" in status["detail"]
        assert "50.0%" in status["detail"]
        dl_calls.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"MP4")
        dest.with_suffix(".mp3").write_bytes(b"MP3")
        return True

    monkeypatch.setattr(toledo_dl, "download_media", _mock_dl)
    monkeypatch.setattr(sys, "argv", ["toledo-dl", "sync", "--lectures-only", "--no-transcribe"])
    with pytest.raises(SystemExit) as exc:
        toledo_dl.main()
    assert exc.value.code == 0

    assert len(dl_calls) == 2
    assert len({p.name for p in dl_calls}) == 2
    db = json.loads((out_dir / ".downloaded.json").read_text(encoding="utf-8"))
    assert db["1_entryAAA"] != db["1_entryBBB"]
    assert Path(db["1_entryAAA"]).exists() and Path(db["1_entryBBB"]).exists()


def test_audit_chapter_distinct_slides_and_reorder_migration(isolate_toledo_env):
    out_dir = isolate_toledo_env["out"]
    cdir = out_dir / "CourseX"
    kb_dir = cdir / "LLM_Knowledge"
    ts_dir = cdir / "Transcripts & Slides"

    lec1 = ts_dir / "Lec1"
    lec2 = ts_dir / "Lec2"
    lec1.mkdir(parents=True)
    lec2.mkdir(parents=True)

    (lec1 / "slides.pdf").write_bytes(b"%PDF-FIRST-DECK")
    (lec1 / "lec1.txt").write_text("First deck transcript.", encoding="utf-8")
    (lec2 / "slides.pdf").write_bytes(b"%PDF-SECOND-DECK")
    (lec2 / "lec2.txt").write_text("Second deck transcript.", encoding="utf-8")

    toledo_dl._repair_and_build_chapter_bundles(cdir, kb_dir)
    chaps = sorted((kb_dir / "Chapters").iterdir())
    assert len(chaps) == 2
    assert "First deck transcript" in (chaps[0] / "CHAPTER_BUNDLE.md").read_text(encoding="utf-8")
    assert "Second deck transcript" not in (chaps[0] / "CHAPTER_BUNDLE.md").read_text(encoding="utf-8")
    assert "Second deck transcript" in (chaps[1] / "CHAPTER_BUNDLE.md").read_text(encoding="utf-8")

    # Add A.pdf to Lec3 and rebuild -> existing chapters migrate without duplicate stale 01_ folders
    lec3 = ts_dir / "Lec3"
    lec3.mkdir(parents=True)
    (lec3 / "A.pdf").write_bytes(b"%PDF-AAA")
    (lec3 / "lec3.txt").write_text("Alpha deck transcript.", encoding="utf-8")

    toledo_dl._repair_and_build_chapter_bundles(cdir, kb_dir)
    chaps_after = sorted(d.name for d in (kb_dir / "Chapters").iterdir() if d.is_dir())
    assert len(chaps_after) == 3
    assert chaps_after[0] == "01_A"
    assert not any(name == "01_Lec1_slides" for name in chaps_after)


def test_codex_acceptance_multivariate_storage_and_retrieval(isolate_toledo_env, monkeypatch):
    import sys

    out_dir = isolate_toledo_env["out"]
    mv = out_dir / "Multivariate Statistics"
    mod_a = mv / "Module A"
    mod_b = mv / "Module B"
    mod_a.mkdir(parents=True)
    mod_b.mkdir(parents=True)

    (mod_a / "Lecture 01.mp3").write_bytes(b"MP3A")
    (mod_a / "Lecture 01.txt").write_text("Module A covers PCA variance.", encoding="utf-8")
    (mod_a / "Lecture 01.srt").write_text(
        "1\n00:01:15,000 --> 00:01:25,000\nModule A covers PCA variance.\n",
        encoding="utf-8",
    )

    (mod_b / "Lecture 01.mp3").write_bytes(b"MP3B")
    (mod_b / "Lecture 01.txt").write_text("Module B covers interaction term slopes.", encoding="utf-8")
    (mod_b / "Lecture 01.srt").write_text(
        "1\n00:04:10,000 --> 00:04:20,000\nModule B covers interaction term slopes.\n",
        encoding="utf-8",
    )

    (mv / "Slides").mkdir()
    pdf_a = mv / "Slides" / "interaction.pdf"
    pdf_a.write_bytes(b"%PDF-INTERACTION")
    (mv / "Files").mkdir()
    (mv / "Files" / "exercise.do").write_text("regress y c.x1##c.x2\nestimates store m1\n", encoding="utf-8")

    lm = out_dir / "Linear Models"
    (lm / "Lectures").mkdir(parents=True)
    (lm / "Lectures" / "ols.mp3").write_bytes(b"MP3OLS")
    (lm / "Lectures" / "ols.txt").write_text("OLS minimizes residual sum of squares.", encoding="utf-8")

    # Provide deterministic 2-page PDF extraction regardless of whether pypdf is installed in system Python
    def _fake_pdf_md(pdf_path, md_dest, course_name, rel_source):
        pages = ["Title page", "Interaction term captures moderation between x1 and x2."]
        md_dest.parent.mkdir(parents=True, exist_ok=True)
        md_dest.write_text(
            f"---\ncourse: {json.dumps(course_name)}\ntype: pdf\nsource: {json.dumps(rel_source)}\npages: 2\n---\n\n"
            f"## Page 1\n\n{pages[0]}\n\n## Page 2\n\n{pages[1]}\n",
            encoding="utf-8",
        )
        return True, pages, "ok"

    monkeypatch.setattr(toledo_dl, "_extract_pdf_markdown_with_pages", _fake_pdf_md)

    # Full index, then filtered index on Multivariate Statistics -> Linear Models must survive in manifest
    toledo_dl.build_llm_index()
    monkeypatch.setattr(sys, "argv", ["toledo-dl", "index", "--course", "Multivariate"])
    with pytest.raises(SystemExit) as exc:
        toledo_dl.main()
    assert exc.value.code == 0

    manifest_lines = [
        json.loads(ln)
        for ln in (out_dir / "llm_manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert any(e["course"] == "Linear Models" for e in manifest_lines)
    mv_tr = [e for e in manifest_lines if e["course"] == "Multivariate Statistics" and e["type"] == "transcript"]
    assert len(mv_tr) == 2
    assert len({e["source_id"] for e in mv_tr}) == 2
    assert len({e["markdown_path"] for e in mv_tr}) == 2

    # Check passages.jsonl preserves timestamps, PDF pages, and standalone Stata script
    passages = [
        json.loads(ln)
        for ln in (mv / "LLM_Knowledge" / "passages.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert any(p["kind"] == "transcript_segment" and p["start_ts"] == "00:04:10" for p in passages)
    assert any(p["kind"] == "pdf_page" and p["page"] == 2 and "moderation" in p["text"] for p in passages)
    assert any(p["kind"] == "document_section" and "regress y" in p["text"] for p in passages)

    # Standalone PDF and Stata script both appear as flashcard-eligible chapters
    chap_names = [d.name for d in (mv / "LLM_Knowledge" / "Chapters").iterdir() if d.is_dir()]
    assert any("interaction" in n for n in chap_names)
    assert any("exercise" in n for n in chap_names)


def test_index_content_dedup_and_search_with_surrounding_context(isolate_toledo_env, monkeypatch):
    out_dir = isolate_toledo_env["out"]
    cdir = out_dir / "SearchCourse"
    (cdir / "Slides").mkdir(parents=True, exist_ok=True)
    (cdir / "Lectures").mkdir(parents=True, exist_ok=True)

    # Two byte-identical PDFs (duplicate download)
    pdf_bytes = b"%PDF-1.4\nfake-identical-pdf-content\n"
    (cdir / "Slides" / "pca.pdf").write_bytes(pdf_bytes)
    (cdir / "Slides" / "pca_2.pdf").write_bytes(pdf_bytes)

    # Transcript with 3 timestamped .srt segments
    (cdir / "Lectures" / "Lec01.txt").write_text("Intro. Kaiser criterion keeps eigenvalues above one. Summary.", encoding="utf-8")
    (cdir / "Lectures" / "Lec01.srt").write_text(
        "1\n00:01:00,000 --> 00:01:10,000\nBefore we pick components, look at the scree plot.\n\n"
        "2\n00:01:10,000 --> 00:01:20,000\nThe Kaiser criterion keeps eigenvalues strictly above one.\n\n"
        "3\n00:01:20,000 --> 00:01:30,000\nThis rule assumes standardized correlation matrices.\n",
        encoding="utf-8",
    )

    def _fake_pdf(pdf_path, md_out, cname, rel):
        md_out.parent.mkdir(parents=True, exist_ok=True)
        md_out.write_text("# PCA\n", encoding="utf-8")
        return True, ["Slide 1: Eigenvalues and scree plot.", "Slide 2: Kaiser criterion threshold is 1.0."], "ok"

    monkeypatch.setattr(toledo_dl, "_extract_pdf_markdown_with_pages", _fake_pdf)
    toledo_dl.build_llm_index()

    # Manifest retains both paths, linking duplicate_of and alias_paths
    manifest = [json.loads(ln) for ln in (out_dir / "llm_manifest.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    pdf_entries = [m for m in manifest if m["type"] == "pdf"]
    assert len(pdf_entries) == 2
    canon = [m for m in pdf_entries if m["duplicate_of"] is None]
    dups = [m for m in pdf_entries if m["duplicate_of"] is not None]
    assert len(canon) == 1 and len(dups) == 1
    assert dups[0]["source_path"] in canon[0]["alias_paths"]

    # Search returns ONE deduplicated PDF hit (with alias_paths) and ONE transcript hit with surrounding context
    hits = toledo_dl.search_passages("Kaiser criterion", course_filter="SearchCourse", limit=10, context_window=1)
    pdf_hits = [h for h in hits if h["kind"] == "pdf_page"]
    tr_hits = [h for h in hits if h["kind"] == "transcript_segment"]
    assert len(pdf_hits) == 1
    assert pdf_hits[0]["page"] == 2
    assert len(pdf_hits[0]["alias_paths"]) == 1
    assert len(tr_hits) == 1
    assert tr_hits[0]["start_ts"] == "00:01:00"
    assert tr_hits[0]["end_ts"] == "00:01:30"
    assert "scree plot" in tr_hits[0]["context_text"]
    assert "standardized correlation matrices" in tr_hits[0]["context_text"]


def test_slide_match_reopens_stale_cache_and_keeps_recent_chapter(isolate_toledo_env, monkeypatch):
    course_dir = isolate_toledo_env["out"] / "Statistics"
    lectures = course_dir / "Lectures"
    slides = course_dir / "Slides"
    lectures.mkdir(parents=True)
    slides.mkdir()
    pca = slides / "2_principal_components.pdf"
    current = slides / "8_classification_other_methods.pdf"
    duplicate = slides / "8_classification_other_methods_2.pdf"
    for path, data in ((pca, b"pca"), (current, b"trees"), (duplicate, b"trees")):
        path.write_bytes(data)

    recent = [lectures / f"Lecture {n} - Classification.txt" for n in (26, 27)]
    lecture = lectures / "Lecture 28 - Bootstrap Aggregating (Bagging) Techniques.txt"
    review = lectures / "Lecture 29 - Review of PCA.txt"
    for transcript in [*recent, lecture, review]:
        transcript.write_text("First we draw bootstrap samples, fit trees and combine their predictions. " * 3, encoding="utf-8")

    pair_dir = course_dir / "Transcripts & Slides" / lecture.stem
    pair_dir.mkdir(parents=True)
    (pair_dir / pca.name).write_bytes(pca.read_bytes())
    (pair_dir / "my_notes.pdf").write_bytes(b"student notes")
    map_file = course_dir / ".pdf_mapping.json"
    paths = {p.relative_to(course_dir).as_posix(): {"matched_pdfs": [current.name, duplicate.name], "confidence": "high"} for p in recent}
    paths[lecture.relative_to(course_dir).as_posix()] = {"matched_pdfs": [pca.name], "confidence": "high"}
    map_file.write_text(json.dumps(paths), encoding="utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"response": json.dumps({"matched_pdfs": [pca.name]})}).encode("utf-8")

    class FakePage:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class FakePypdf:
        @staticmethod
        def PdfReader(path):
            from types import SimpleNamespace

            pages = [FakePage("Classification methods")]
            if Path(path).name == current.name:
                pages.append(FakePage("Bootstrap aggregation or bagging"))
            return SimpleNamespace(pages=pages)

    def match(req, **_kwargs):
        prompt = json.loads(req.data.decode())["prompt"]
        if lecture.stem in prompt:
            assert pca.name not in prompt
            assert duplicate.name not in prompt
        return FakeResponse()

    monkeypatch.setattr(toledo_dl.urllib.request, "urlopen", match)
    monkeypatch.setattr(toledo_dl, "pypdf", FakePypdf())
    assert toledo_dl.match_transcript_with_pdfs(lecture, course_dir)["matched_pdfs"] == [current.name]
    assert not (pair_dir / pca.name).exists()
    assert (pair_dir / current.name).is_file()
    assert (pair_dir / "my_notes.pdf").read_bytes() == b"student notes"
    assert pca.read_bytes() == b"pca"

    assert toledo_dl.match_transcript_with_pdfs(review, course_dir)["matched_pdfs"] == [pca.name]


def test_slide_match_cloud_gemini_multimodal_with_video_frames_and_outlines(isolate_toledo_env, monkeypatch):
    from types import SimpleNamespace

    course_dir = isolate_toledo_env["out"] / "Fundamental Concepts"
    lectures = course_dir / "Lectures"
    pdfs = course_dir / "PDFs"
    lectures.mkdir(parents=True)
    pdfs.mkdir(parents=True)

    pdf_uni = pdfs / "2_univariate_distributions.pdf"
    pdf_multi = pdfs / "3_multivariate_distributions.pdf"
    pdf_uni.write_bytes(b"%PDF-1.4 uni")
    pdf_multi.write_bytes(b"%PDF-1.4 multi")

    txt = lectures / "Lecture 02 - 3.txt"
    txt.write_text(" ".join(["multivariate", "random", "vectors", "joint", "density", "covariance"] * 8), encoding="utf-8")
    mp4 = lectures / "Lecture 02 - 3.mp4"
    mp4.write_bytes(b"fake-mp4-bytes")

    class FakePage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class FakePypdf:
        @staticmethod
        def PdfReader(path):
            if Path(path).name == pdf_multi.name:
                return SimpleNamespace(pages=[
                    FakePage("3. Multivariate Distributions and Random Vectors\nFundamental Concepts"),
                    FakePage("Joint Density and Marginal Distributions\nSection 3.2"),
                    FakePage("Covariance Matrix and Correlation\nSection 3.4"),
                ])
            return SimpleNamespace(pages=[FakePage("2. Univariate Distributions\nFundamental Concepts")])

    fake_frames = [
        {"timestamp": f"{m:02d}:00", "pct": pct, "mime_type": "image/jpeg", "b64": "ZmFrZWpwZWc="}
        for m, pct in [(5, 10), (12, 25), (20, 40), (27, 55), (35, 70), (42, 85)]
    ]

    captured = {}

    class FakeGeminiResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "id": "int_match_1",
                "status": "completed",
                "steps": [{
                    "type": "model_output",
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "matched_pdfs": [pdf_multi.name],
                            "confidence": "high",
                            "slide_range": "slides 1-32",
                            "reason": "Slides 3. Multivariate Distributions appear on 5 of 6 video frames.",
                        }),
                    }],
                }],
            }).encode("utf-8")

    def fake_urlopen(req, **_kwargs):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeGeminiResp()

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFakeTestKey")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TOLEDO_MATCH_BACKEND", raising=False)
    monkeypatch.setattr(toledo_dl, "pypdf", FakePypdf())
    monkeypatch.setattr(toledo_dl, "_sample_video_frames", lambda _p: fake_frames)
    monkeypatch.setattr(toledo_dl.urllib.request, "urlopen", fake_urlopen)

    approval = toledo_dl._make_cloud_approval(
        "match-slides", "gemini", toledo_dl._MATCH_DEFAULT_MODELS["gemini"], [txt], allow_video=True
    )
    entry = toledo_dl.match_transcript_with_pdfs(txt, course_dir, backend="gemini", visual=True, cloud_approval=approval)
    assert entry["matched_pdfs"] == [pdf_multi.name]
    assert entry["backend"] == "gemini"
    assert entry["used_video_frames"] == 6
    assert entry["slide_range"] == "slides 1-32"
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1/interactions"
    assert "?key=" not in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "AIzaFakeTestKey"
    assert captured["headers"]["api-revision"] == toledo_dl._GEMINI_INTERACTIONS_API_REVISION
    assert captured["body"]["model"] == "gemini-4-hydrogen"
    assert captured["body"]["store"] is False
    assert "temperature" not in captured["body"].get("generation_config", {})
    assert captured["body"]["response_format"]["mime_type"] == "application/json"

    input_blocks = captured["body"]["input"]
    inline_images = [p for p in input_blocks if p.get("type") == "image"]
    assert len(inline_images) == 6
    assert "Covariance Matrix and Correlation" in input_blocks[0]["text"]
    assert (course_dir / ".pdf_outlines.json").is_file()
    assert (course_dir / "Transcripts & Slides" / txt.stem / pdf_multi.name).is_file()


def test_slide_match_cloud_openai_and_graceful_fallback(isolate_toledo_env, monkeypatch):
    course_dir = isolate_toledo_env["out"] / "OpenAI Course"
    lectures = course_dir / "Lectures"
    pdfs = course_dir / "PDFs"
    lectures.mkdir(parents=True)
    pdfs.mkdir(parents=True)

    pdf_bag = pdfs / "8_bagging_methods.pdf"
    pdf_bag.write_bytes(b"%PDF-1.4 bagging")
    txt = lectures / "Lecture 10 - Bagging Methods.txt"
    txt.write_text(" ".join(["bagging", "bootstrap", "aggregating", "trees", "ensemble"] * 8), encoding="utf-8")
    (lectures / "Lecture 10 - Bagging Methods.mp4").write_bytes(b"fake-mp4")

    captured = {}

    class FakeOpenAIResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "matched_pdfs": [pdf_bag.name],
                            "confidence": "high",
                            "reason": "Visible on screen across lecture frames.",
                        })
                    }
                }]
            }).encode("utf-8")

    def fake_urlopen(req, **_kwargs):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeOpenAIResp()

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("TOLEDO_MATCH_BACKEND", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.example/api/v1")
    monkeypatch.setattr(
        toledo_dl,
        "_sample_video_frames",
        lambda _p: [{"timestamp": "10:00", "pct": 25, "mime_type": "image/jpeg", "b64": "ZmFrZQ=="}],
    )
    monkeypatch.setattr(toledo_dl.urllib.request, "urlopen", fake_urlopen)

    approval = toledo_dl._make_cloud_approval("match-slides", "openai", "gpt-4o-mini", [txt], allow_video=True)
    entry = toledo_dl.match_transcript_with_pdfs(txt, course_dir, backend="openai", visual=True, cloud_approval=approval)
    assert entry["matched_pdfs"] == [pdf_bag.name]
    assert entry["backend"] == "openai"
    assert entry["used_video_frames"] == 1
    assert captured["url"] == "https://openrouter.example/api/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer sk-test-key"
    content = captured["body"]["messages"][0]["content"]
    assert any(item.get("type") == "image_url" for item in content)

    # Verify graceful fallback to deterministic keyword matcher when API errors out
    def fail_urlopen(_req, **_kwargs):
        raise OSError("network down")

    monkeypatch.setattr(toledo_dl.urllib.request, "urlopen", fail_urlopen)
    fallback_entry = toledo_dl.match_transcript_with_pdfs(
        txt, course_dir, force=True, backend="openai", cloud_approval=approval
    )
    assert fallback_entry["matched_pdfs"] == [pdf_bag.name]
    assert fallback_entry["backend"] == "keyword"
    assert fallback_entry["used_video_frames"] == 0


def test_slide_match_keys_never_auto_upload_and_local_overrides_env(isolate_toledo_env, monkeypatch):
    import argparse

    course_dir = isolate_toledo_env["out"] / "Consent Course"
    lectures = course_dir / "Lectures"
    pdfs = course_dir / "PDFs"
    lectures.mkdir(parents=True)
    pdfs.mkdir(parents=True)

    pdf = pdfs / "1_intro_stats.pdf"
    pdf.write_bytes(b"%PDF-1.4 intro")
    txt = lectures / "Lecture 01 - Intro Stats.txt"
    txt.write_text(" ".join(["intro", "stats", "probability", "variance", "mean"] * 8), encoding="utf-8")
    (lectures / "Lecture 01 - Intro Stats.mp4").write_bytes(b"fake-mp4")

    cloud_urls = []

    def inspect_urlopen(req, **_kwargs):
        if "localhost:11434" not in req.full_url and "127.0.0.1:11434" not in req.full_url:
            cloud_urls.append(req.full_url)
        raise OSError("offline")

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFakeKey")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key")
    monkeypatch.delenv("TOLEDO_MATCH_BACKEND", raising=False)
    monkeypatch.setattr(toledo_dl.urllib.request, "urlopen", inspect_urlopen)

    # 1. Populated API keys alone must NEVER trigger cloud uploads in default auto mode
    entry = toledo_dl.match_transcript_with_pdfs(txt, course_dir, backend="auto")
    assert cloud_urls == []
    assert entry["backend"] == "keyword"
    assert entry["used_video_frames"] == 0

    # 2. Explicit backend="local" overrides TOLEDO_MATCH_BACKEND=gemini
    monkeypatch.setenv("TOLEDO_MATCH_BACKEND", "gemini")
    entry_local = toledo_dl.match_transcript_with_pdfs(txt, course_dir, force=True, backend="local")
    assert cloud_urls == []
    assert entry_local["backend"] == "keyword"

    # 3. cmd_match_slides dry-run and non-interactive cloud consent guard send zero requests
    rc_dry = toledo_dl.cmd_match_slides(argparse.Namespace(
        course="Consent",
        backend="gemini",
        model=None,
        no_video=False,
        dry_run=True,
        yes=False,
        force=True,
    ))
    assert rc_dry == 0
    assert cloud_urls == []

    monkeypatch.setattr(toledo_dl.sys.stdin, "isatty", lambda: False)
    rc_abort = toledo_dl.cmd_match_slides(argparse.Namespace(
        course="Consent",
        backend="gemini",
        model=None,
        no_video=False,
        limit=None,
        dry_run=False,
        yes=False,
        force=True,
    ))
    assert rc_abort == 1
    assert cloud_urls == []


def test_codex_high1_stale_match_requires_confirmation_and_zero_work_means_zero_uploads(isolate_toledo_env, monkeypatch):
    import argparse

    course_dir = isolate_toledo_env["out"] / "Multivariate Stats"
    lectures = course_dir / "Lectures"
    pdfs = course_dir / "PDFs"
    lectures.mkdir(parents=True)
    pdfs.mkdir(parents=True)

    pca = pdfs / "2_pca.pdf"
    chap8 = pdfs / "8_classification_other_methods.pdf"
    pca.write_bytes(b"%PDF-1.4 pca")
    chap8.write_bytes(b"%PDF-1.4 chap8")

    lec26 = lectures / "Lecture 26 - Trees.txt"
    lec27 = lectures / "Lecture 27 - Boosting.txt"
    lec28 = lectures / "Lecture 28 - Bagging.txt"
    for p in (lec26, lec27, lec28):
        p.write_text(" ".join(["classification", "trees", "bagging", "ensemble", "bootstrap"] * 8), encoding="utf-8")
        pair = course_dir / "Transcripts & Slides" / p.stem
        pair.mkdir(parents=True, exist_ok=True)

    # Seed Lectures 26-28 as Ch 8, Ch 8, and stale Ch 2 (exact Codex repro)
    (course_dir / ".pdf_mapping.json").write_text(json.dumps({
        "Lectures/Lecture 26 - Trees.txt": {"matched_pdfs": [chap8.name], "confidence": "high"},
        "Lectures/Lecture 27 - Boosting.txt": {"matched_pdfs": [chap8.name], "confidence": "high"},
        "Lectures/Lecture 28 - Bagging.txt": {"matched_pdfs": [pca.name], "confidence": "high"},
    }), encoding="utf-8")

    cloud_calls = []

    class FakeGeminiResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "id": "int_match_2",
                "status": "completed",
                "steps": [{
                    "type": "model_output",
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "matched_pdfs": [chap8.name],
                            "confidence": "high",
                            "slide_range": "",
                            "reason": "Bagging belongs to Chapter 8.",
                        }),
                    }],
                }],
            }).encode("utf-8")

    def fake_urlopen(req, **_kwargs):
        cloud_calls.append(req.full_url)
        return FakeGeminiResp()

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFakeKey")
    monkeypatch.setattr(toledo_dl.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(toledo_dl.sys.stdin, "isatty", lambda: False)

    # 1. Planner detects stale Lecture 28 (1 pending, NOT 0)
    jobs = toledo_dl._plan_match_jobs([course_dir], force=False)
    assert [j["txt_path"].name for j in jobs] == [lec28.name]

    # 2. Non-interactive without --yes must abort with ZERO uploads
    rc_abort = toledo_dl.cmd_match_slides(argparse.Namespace(
        course="Multivariate",
        backend="gemini",
        model=None,
        no_video=True,
        limit=None,
        dry_run=False,
        yes=False,
        force=False,
    ))
    assert rc_abort == 1
    assert cloud_calls == []

    # 3. Approved run uploads ONLY Lecture 28 (1 call)
    rc_ok = toledo_dl.cmd_match_slides(argparse.Namespace(
        course="Multivariate",
        backend="gemini",
        model=None,
        no_video=True,
        limit=1,
        dry_run=False,
        yes=True,
        force=False,
    ))
    assert rc_ok == 0
    assert len(cloud_calls) == 1

    # 4. Subsequent run sees 0 pending and makes 0 uploads
    rc_zero = toledo_dl.cmd_match_slides(argparse.Namespace(
        course="Multivariate",
        backend="gemini",
        model=None,
        no_video=True,
        limit=None,
        dry_run=False,
        yes=True,
        force=False,
    ))
    assert rc_zero == 0
    assert len(cloud_calls) == 1


def test_codex_high2_automatic_callers_never_upload_even_with_cloud_env(isolate_toledo_env, monkeypatch):
    import argparse

    course_dir = isolate_toledo_env["out"] / "AutoGuard Course"
    lectures = course_dir / "Lectures"
    pdfs = course_dir / "PDFs"
    lectures.mkdir(parents=True)
    pdfs.mkdir(parents=True)

    pdf = pdfs / "1_intro.pdf"
    pdf.write_bytes(b"%PDF-1.4 intro")
    txt = lectures / "Lecture 01 - Intro.txt"
    txt.write_text(" ".join(["intro", "statistics", "probability", "mean", "variance"] * 8), encoding="utf-8")
    mp3 = lectures / "Lecture 02 - Pending.mp3"
    mp3.write_bytes(b"fake-mp3-audio")

    cloud_urls = []

    def inspect_urlopen(req, **_kwargs):
        if "localhost:11434" not in req.full_url and "127.0.0.1:11434" not in req.full_url:
            cloud_urls.append(req.full_url)
        raise OSError("offline")

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFakeKey")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("TOLEDO_MATCH_BACKEND", "gemini")
    monkeypatch.setenv("TOLEDO_TRANSCRIBE_BACKEND", "gemini")
    monkeypatch.setattr(toledo_dl.urllib.request, "urlopen", inspect_urlopen)
    monkeypatch.setattr(toledo_dl, "_cuda_device_count", lambda: 0)

    # 1. Bulk helper used by sync stays strictly local (0 cloud uploads)
    toledo_dl.update_course_pdf_matches(course_dir, force=True)
    assert cloud_urls == []

    # 2. Direct unapproved match_transcript_with_pdfs(..., backend="gemini") refuses upload
    entry = toledo_dl.match_transcript_with_pdfs(txt, course_dir, force=True, backend="gemini")
    assert cloud_urls == []
    assert entry["backend"] == "keyword"

    # 3. Explicit local transcription (as used by sync) overrides TOLEDO_TRANSCRIBE_BACKEND=gemini
    rc = toledo_dl.cmd_transcribe(argparse.Namespace(
        course="AutoGuard",
        file=None,
        backend="local",
        model="auto",
        lang="en",
        limit=None,
        max_minutes=None,
        yes=False,
        dry_run=False,
        defer_cpu=True,
    ))
    assert rc == 0
    assert cloud_urls == []


def test_cloud_gemini_transcribe_chunking_resume_completion_marker_and_caps(isolate_toledo_env, monkeypatch):
    import argparse

    course_dir = isolate_toledo_env["out"] / "Cloud Transcribe Course"
    lectures = course_dir / "Lectures"
    lectures.mkdir(parents=True)

    rec1 = lectures / "Lecture 01 - Regression.mp3"
    rec2 = lectures / "Lecture 02 - Classification.mp3"
    rec1.write_bytes(b"FAKE-MP3-RECORDING-ONE")
    rec2.write_bytes(b"FAKE-MP3-RECORDING-TWO")

    # 1. Populated GEMINI_API_KEY alone triggers ZERO uploads in default auto mode (and skips slow CPU)
    upload_calls = []

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFakeKey")
    monkeypatch.delenv("TOLEDO_TRANSCRIBE_BACKEND", raising=False)
    monkeypatch.setenv("TOLEDO_MATCH_BACKEND", "gemini")  # must NOT trigger cloud slide matching!
    monkeypatch.setattr(toledo_dl, "_cuda_device_count", lambda: 0)
    monkeypatch.setattr(toledo_dl, "_probe_video_duration", lambda _p: 1800.0)  # 30 min each

    rc_auto = toledo_dl.cmd_transcribe(argparse.Namespace(
        course="Cloud Transcribe",
        file=None,
        backend="auto",
        model="auto",
        lang="en",
        limit=None,
        max_minutes=None,
        yes=False,
        dry_run=False,
        defer_cpu=False,
    ))
    assert rc_auto == 0
    assert not rec1.with_suffix(".txt").exists()

    # 2. Fake 2-chunk splitter + mock Gemini where chunk 2 fails on first attempt, then resumes cleanly
    def fake_split(audio_path, work_dir, chunk_seconds=900):
        c0 = work_dir / "chunk_0000.mp3"
        c1 = work_dir / "chunk_0001.mp3"
        c0.write_bytes(b"CHUNK0-" + audio_path.name.encode("utf-8"))
        c1.write_bytes(b"CHUNK1-" + audio_path.name.encode("utf-8"))
        return [
            {"index": 0, "offset_sec": 0.0, "duration_sec": 900.0, "path": c0},
            {"index": 1, "offset_sec": 900.0, "duration_sec": 900.0, "path": c1},
        ]

    fail_second_chunk = {"should_fail": True}

    def fake_chunk_call(chunk_bytes, *, model, api_key, language, chunk_duration_sec):
        upload_calls.append(chunk_bytes[:6].decode("utf-8"))
        if chunk_bytes.startswith(b"CHUNK1") and fail_second_chunk["should_fail"]:
            fail_second_chunk["should_fail"] = False
            raise RuntimeError("simulated network drop on chunk 2")
        if chunk_bytes.startswith(b"CHUNK0"):
            return [{"start": 1.0, "end": 10.5, "text": "Welcome to linear regression."}]
        return [{"start": 2.0, "end": 12.0, "text": "Now we estimate beta hat."}]

    match_cloud_calls = []
    monkeypatch.setattr(toledo_dl, "_split_audio_chunks", fake_split)
    monkeypatch.setattr(toledo_dl, "_call_gemini_audio_chunk", fake_chunk_call)
    monkeypatch.setattr(toledo_dl, "_call_match_gemini", lambda *a, **kw: match_cloud_calls.append(1) or {})
    monkeypatch.setattr(toledo_dl.sys.stdin, "isatty", lambda: False)

    # Dry-run and non-interactive without --yes make zero uploads
    assert toledo_dl.cmd_transcribe(argparse.Namespace(
        course="Cloud Transcribe", file=None, backend="gemini", model="auto", lang="en",
        limit=1, max_minutes=30.0, yes=False, dry_run=True,
    )) == 0
    assert toledo_dl.cmd_transcribe(argparse.Namespace(
        course="Cloud Transcribe", file=None, backend="gemini", model="auto", lang="en",
        limit=1, max_minutes=30.0, yes=False, dry_run=False,
    )) == 1
    assert upload_calls == []

    # Attempt 1 with --limit 1 & --max-minutes 30: chunk 0 succeeds & caches, chunk 1 fails -> nothing published
    rc_fail = toledo_dl.cmd_transcribe(argparse.Namespace(
        course="Cloud Transcribe", file=None, backend="gemini", model="auto", lang="en",
        limit=1, max_minutes=30.0, yes=True, dry_run=False,
    ))
    assert rc_fail == 1
    assert upload_calls == ["CHUNK0", "CHUNK1"]
    assert not rec1.with_suffix(".txt").exists()
    assert not rec1.with_suffix(".srt").exists()
    assert not toledo_dl._transcript_done_marker(rec1).exists()
    assert toledo_dl.build_llm_index([course_dir]) == 0

    # Attempt 2 (resume): MUST reuse cached chunk 0 and upload ONLY chunk 1!
    upload_calls.clear()
    rc_resume = toledo_dl.cmd_transcribe(argparse.Namespace(
        course="Cloud Transcribe", file=None, backend="gemini", model="auto", lang="en",
        limit=1, max_minutes=30.0, yes=True, dry_run=False,
    ))
    assert rc_resume == 0
    assert upload_calls == ["CHUNK1"]
    assert rec1.with_suffix(".txt").is_file()
    assert rec1.with_suffix(".srt").is_file()
    assert toledo_dl._transcript_done_marker(rec1).is_file()
    # Chunk 1 timestamp offset by 900s (00:15:02,000 --> 00:15:12,000)
    srt_text = rec1.with_suffix(".srt").read_text(encoding="utf-8")
    assert "00:00:01,000 --> 00:00:10,500" in srt_text
    assert "00:15:02,000 --> 00:15:12,000" in srt_text
    # Recording 2 was capped by --limit 1 and untouched
    assert not rec2.with_suffix(".txt").exists()
    # Enabling cloud transcription NEVER triggered cloud slide matching
    assert match_cloud_calls == []
    # Private chunk cache pruned after completion
    cache_root = isolate_toledo_env["data"] / "transcribe_cache"
    assert not any(cache_root.rglob("chunk_*.json"))

    # 3. Completion marker vs legacy lone .txt regression check
    legacy_audio = lectures / "Lecture 03 - Legacy.mp3"
    legacy_audio.write_bytes(b"legacy")
    legacy_txt = legacy_audio.with_suffix(".txt")
    legacy_txt.write_text("Legacy completed transcript.\n", encoding="utf-8")
    assert toledo_dl._is_transcript_complete(legacy_audio) is True
    assert toledo_dl._is_transcript_txt(legacy_txt) is True

    # An in-progress marker marks even an existing lone .txt as incomplete & hidden from index
    inprog = toledo_dl._transcript_inprogress_marker(legacy_audio)
    inprog.write_text("committing\n", encoding="utf-8")
    assert toledo_dl._is_transcript_complete(legacy_audio) is False
    assert toledo_dl._is_transcript_txt(legacy_txt) is False
    inprog.unlink()


def test_failed_commit_retains_inprogress_and_rename_carries_markers(isolate_toledo_env, monkeypatch):
    course_dir = isolate_toledo_env["out"] / "Commit Guard"
    lectures = course_dir / "Lectures"
    lectures.mkdir(parents=True)
    audio = lectures / "Lecture 1 - Intro.mp3"
    audio.write_bytes(b"audio")

    orig_replace = toledo_dl._replace_bytes

    def fail_on_srt(path, data):
        if Path(path).suffix == ".srt":
            raise OSError("No space left on device")
        return orig_replace(path, data)

    monkeypatch.setattr(toledo_dl, "_replace_bytes", fail_on_srt)
    with pytest.raises(OSError):
        toledo_dl._commit_transcript_pair(
            audio,
            [{"start": 0.0, "end": 2.0, "text": "Partial line."}],
            backend="gemini",
            model="gemini-2.5-flash",
            recording_sha256="abc123",
        )

    assert audio.with_suffix(".txt").is_file()
    assert not audio.with_suffix(".srt").exists()
    assert toledo_dl._transcript_inprogress_marker(audio).is_file()
    assert toledo_dl._is_transcript_complete(audio) is False
    assert toledo_dl._is_transcript_txt(audio.with_suffix(".txt")) is False
    assert toledo_dl._course_match_transcripts(course_dir) == []

    # Pre-seed a stale passage (simulating an older index that had indexed the pending TXT)
    kb_dir = course_dir / "LLM_Knowledge"
    kb_dir.mkdir(parents=True, exist_ok=True)
    passages_file = kb_dir / "passages.jsonl"
    passages_file.write_text(
        json.dumps({
            "passage_id": "stale:s1",
            "course": course_dir.name,
            "source_id": "stale",
            "source_path": f"{course_dir.name}/Lectures/{audio.with_suffix('.txt').name}",
            "source_name": audio.with_suffix(".txt").name,
            "kind": "document_section",
            "text": "Partial line.",
        }) + "\n",
        encoding="utf-8",
    )

    # Reindexing with only the pending transcript must prune stale passages.jsonl so search returns nothing
    assert toledo_dl.cmd_index(argparse.Namespace(course="Commit Guard")) == 0
    assert not passages_file.exists()
    assert toledo_dl.search_passages("Partial", course_filter="Commit Guard") == []

    # Also add a PDF and a pre-existing Transcripts & Slides pairing linking to the pending TXT
    pdfs = course_dir / "PDFs"
    pdfs.mkdir(parents=True, exist_ok=True)
    pdf_file = pdfs / "1_intro.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 intro")
    pair_dir = course_dir / "Transcripts & Slides" / audio.stem
    pair_dir.mkdir(parents=True, exist_ok=True)
    (pair_dir / pdf_file.name).symlink_to(pdf_file)
    (pair_dir / audio.with_suffix(".txt").name).symlink_to(audio.with_suffix(".txt"))

    # Reindexing must still omit the pending transcript from passages, manifest, and chapter bundles
    assert toledo_dl.cmd_index(argparse.Namespace(course="Commit Guard")) == 0
    assert toledo_dl.search_passages("Partial", course_filter="Commit Guard") == []
    for bundle in (course_dir / "LLM_Knowledge" / "Chapters").rglob("CHAPTER_BUNDLE.md") if (course_dir / "LLM_Knowledge" / "Chapters").exists() else []:
        assert "Partial line." not in bundle.read_text(encoding="utf-8")
    manifest_file = isolate_toledo_env["out"] / "llm_manifest.jsonl"
    if manifest_file.exists():
        assert audio.with_suffix(".txt").name not in manifest_file.read_text(encoding="utf-8")
    assert not any((course_dir / "LLM_Knowledge" / "transcripts").glob("*.md")) if (course_dir / "LLM_Knowledge" / "transcripts").exists() else True

    # Recovery commit succeeds and clears in-progress marker; rename carries done marker
    monkeypatch.setattr(toledo_dl, "_replace_bytes", orig_replace)
    vague = lectures / "Lecture 2 (10-12 09h00).mp3"
    vague.write_bytes(b"audio2")
    toledo_dl._commit_transcript_pair(
        vague,
        [{"start": 0.0, "end": 5.0, "text": "Topic: Dynamic Programming."}],
        backend="gemini",
        model="gemini-2.5-flash",
        recording_sha256="def456",
    )
    assert toledo_dl._transcript_done_marker(vague).is_file()
    renamed = toledo_dl._maybe_rename_vague_lecture(vague)
    if renamed != vague:
        assert toledo_dl._transcript_done_marker(renamed).is_file()
        assert not toledo_dl._transcript_done_marker(vague).exists()


def test_course_scoped_symlink_escape_rejected(isolate_toledo_env, monkeypatch, tmp_path):
    course_dir = isolate_toledo_env["out"] / "Scoped Course"
    lectures = course_dir / "Lectures"
    lectures.mkdir(parents=True)

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_mp3 = outside_dir / "secret.mp3"
    outside_mp3.write_bytes(b"outside-audio")
    outside_txt = outside_dir / "secret.txt"
    outside_txt.write_text(" ".join(["external"] * 30), encoding="utf-8")
    outside_mp4 = outside_dir / "secret.mp4"
    outside_mp4.write_bytes(b"outside-video")

    link_mp3 = lectures / "Lecture 1 - Escape.mp3"
    link_txt = lectures / "Lecture 1 - Escape.txt"
    link_mp4 = lectures / "Lecture 1 - Escape.mp4"
    link_mp3.symlink_to(outside_mp3)
    link_txt.symlink_to(outside_txt)
    link_mp4.symlink_to(outside_mp4)

    provider_calls = []
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFakeTestKey")
    monkeypatch.setattr(toledo_dl, "_probe_video_duration", lambda _p: 60.0)
    monkeypatch.setattr(toledo_dl.urllib.request, "urlopen", lambda *a, **kw: provider_calls.append(a))

    rc = toledo_dl.cmd_transcribe(argparse.Namespace(
        course="Scoped Course", file=None, backend="gemini", model="auto", lang="en",
        limit=None, max_minutes=60.0, chunk_seconds=None, yes=True, dry_run=False,
    ))
    assert rc == 0
    assert provider_calls == []
    assert toledo_dl._course_match_transcripts(course_dir) == []
    assert toledo_dl._find_lecture_video(link_txt, course_dir=course_dir) is None

    approval = toledo_dl._make_cloud_approval("match-slides", "gemini", "gemini-2.5-flash", [link_txt], allow_video=True)
    assert toledo_dl.match_transcript_with_pdfs(link_txt, course_dir, backend="gemini", visual=True, cloud_approval=approval) is None
    assert provider_calls == []


def test_unknown_duration_deferred_and_cloud_approval_checks_model_datatypes_and_hash(isolate_toledo_env, monkeypatch):
    course_dir = isolate_toledo_env["out"] / "Approval Course"
    lectures = course_dir / "Lectures"
    lectures.mkdir(parents=True)
    audio = lectures / "Lecture 1 - Unknown.mp3"
    audio.write_bytes(b"original-bytes")

    monkeypatch.setattr(toledo_dl, "_probe_video_duration", lambda _p: None)
    jobs, skipped, total_sec = toledo_dl._plan_transcribe_jobs([audio], max_minutes=1.0, probe_duration=True)
    assert jobs == []
    assert skipped == 1
    assert total_sec == 0.0

    sha = toledo_dl._file_sha256(audio)
    approval = toledo_dl._make_cloud_approval(
        "transcribe", "gemini", "gemini-2.5-flash", [audio],
        data_types=("audio_chunks",),
        course_dirs=[course_dir],
        expected_hashes={audio: sha},
        max_duration_sec=60.0,
    )
    assert toledo_dl._is_cloud_approved(
        approval, "transcribe", "gemini", audio,
        model="gemini-2.5-flash", data_types=("audio_chunks",), course_dir=course_dir, duration_sec=30.0,
    ) is True
    # Wrong model, wrong data type, over cap, or mutated file must all be rejected
    assert toledo_dl._is_cloud_approved(
        approval, "transcribe", "gemini", audio,
        model="gemini-2.5-pro", data_types=("audio_chunks",), course_dir=course_dir, duration_sec=30.0,
    ) is False
    assert toledo_dl._is_cloud_approved(
        approval, "transcribe", "gemini", audio,
        model="gemini-2.5-flash", data_types=("video_frames",), course_dir=course_dir, duration_sec=30.0,
    ) is False
    assert toledo_dl._is_cloud_approved(
        approval, "transcribe", "gemini", audio,
        model="gemini-2.5-flash", data_types=("audio_chunks",), course_dir=course_dir, duration_sec=120.0,
    ) is False
    audio.write_bytes(b"swapped-longer-media")
    assert toledo_dl._is_cloud_approved(
        approval, "transcribe", "gemini", audio,
        model="gemini-2.5-flash", data_types=("audio_chunks",), course_dir=course_dir, duration_sec=30.0,
    ) is False


def test_explicit_cpu_and_gpu_device_selection(isolate_toledo_env, monkeypatch):
    course_dir = isolate_toledo_env["out"] / "Device Course"
    lectures = course_dir / "Lectures"
    lectures.mkdir(parents=True)
    audio = lectures / "Lecture 1 - Device.mp3"
    audio.write_bytes(b"audio")

    init_devices = []

    class FakeSeg:
        def __init__(self):
            self.start = 0.0
            self.end = 1.0
            self.text = "Hello world."

    class FakeInfo:
        language = "en"
        language_probability = 0.99

    class FakeWhisperModel:
        def __init__(self, _model_size, device="auto", **_kwargs):
            init_devices.append(device)

        def transcribe(self, *_args, **_kwargs):
            return [FakeSeg()], FakeInfo()

    fake_fw = types.SimpleNamespace(WhisperModel=FakeWhisperModel)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)
    monkeypatch.setattr(toledo_dl, "_cuda_device_count", lambda: 1)

    rc_cpu = toledo_dl.cmd_transcribe(argparse.Namespace(
        course="Device Course", file=None, backend="cpu", model="small", lang="en",
        limit=None, max_minutes=None, chunk_seconds=None, yes=False, dry_run=False,
    ))
    assert rc_cpu == 0
    assert init_devices == ["cpu"]

    # Remove outputs and test forced GPU
    audio.with_suffix(".txt").unlink()
    audio.with_suffix(".srt").unlink()
    init_devices.clear()
    rc_gpu = toledo_dl.cmd_transcribe(argparse.Namespace(
        course="Device Course", file=None, backend="gpu", model="small", lang="en",
        limit=None, max_minutes=None, chunk_seconds=None, yes=False, dry_run=False,
    ))
    assert rc_gpu == 0
    assert init_devices == ["cuda"]


def test_gemini_chunk_boundary_clamping_and_max_tokens_split(isolate_toledo_env, monkeypatch):
    # 1. Out-of-bounds / NaN segment timestamps stay strictly within [0, 900]
    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

    captured_requests = []

    def fake_urlopen(req, **_kwargs):
        body = json.loads(req.data.decode("utf-8"))
        captured_requests.append({
            "url": req.full_url,
            "headers": {k.lower(): v for k, v in req.header_items()},
            "body": body,
        })
        if body.get("model") == "gemini-3.5-transcribe":
            return FakeResp({
                "id": "int_tx_1",
                "status": "completed",
                "steps": [{
                    "type": "model_output",
                    "content": [{
                        "type": "text",
                        "text": "Welkom bij de les. Vandaag bespreken we variantie.",
                        "annotations": [
                            {"type": "word_info", "text": "Welkom", "start_offset": "0.100s", "end_offset": "0.450s"},
                            {"type": "word_info", "text": "bij", "start_offset": "0.500s", "end_offset": "0.650s"},
                            {"type": "word_info", "text": "de", "start_offset": "0.700s", "end_offset": "0.800s"},
                            {"type": "word_info", "text": "les.", "start_offset": "0.850s", "end_offset": "1.200s"},
                            {"type": "word_info", "text": "Vandaag", "start_offset": "2.800s", "end_offset": "3.200s"},
                            {"type": "word_info", "text": "bespreken", "start_offset": "3.250s", "end_offset": "3.750s"},
                            {"type": "word_info", "text": "we", "start_offset": "3.800s", "end_offset": "3.950s"},
                            {"type": "word_info", "text": "variantie.", "start_offset": "4.000s", "end_offset": "4.600s"},
                        ],
                    }],
                }],
            })
        return FakeResp({
            "id": "int_tx_2",
            "status": "completed",
            "steps": [{
                "type": "model_output",
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "segments": [
                            {"start": -5.0, "end": 899.8, "text": "First."},
                            {"start": 899.9, "end": 902.0, "text": "Boundary."},
                            {"start": float("nan"), "end": float("inf"), "text": "Sanitized."},
                        ]
                    }),
                }],
            }],
        })

    monkeypatch.setattr(toledo_dl.urllib.request, "urlopen", fake_urlopen)

    # 1a. Dedicated gemini-3.5-transcribe uses v1beta/interactions + word_info timestamps + BCP-47 language hint
    tx_segs = toledo_dl._call_gemini_audio_chunk(
        b"mp3", model="gemini-3.5-transcribe", api_key="k", language="nl", chunk_duration_sec=900.0,
    )
    assert len(tx_segs) == 2
    assert tx_segs[0]["text"] == "Welkom bij de les."
    assert tx_segs[0]["start"] == 0.1 and tx_segs[0]["end"] == 1.2
    assert tx_segs[1]["text"] == "Vandaag bespreken we variantie."
    assert tx_segs[1]["start"] == 2.8 and tx_segs[1]["end"] == 4.6
    req_tx = captured_requests[0]
    assert req_tx["url"] == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert req_tx["headers"]["x-goog-api-key"] == "k"
    assert req_tx["headers"]["api-revision"] == toledo_dl._GEMINI_INTERACTIONS_API_REVISION
    assert req_tx["body"]["store"] is False
    assert req_tx["body"]["generation_config"]["transcription_config"]["language_codes"] == ["nl-NL"]
    assert req_tx["body"]["generation_config"]["transcription_config"]["mode"]["timestamp_granularities"] == ["word"]

    # 1b. General multimodal model override (gemini-4-hydrogen) uses v1/interactions + structured JSON schema + clamping
    segs = toledo_dl._call_gemini_audio_chunk(
        b"mp3", model="gemini-4-hydrogen", api_key="k", language="en", chunk_duration_sec=900.0,
    )
    req_gen = captured_requests[1]
    assert req_gen["url"] == "https://generativelanguage.googleapis.com/v1/interactions"
    assert req_gen["body"]["store"] is False
    assert req_gen["body"]["response_format"]["mime_type"] == "application/json"
    prev_end = 0.0
    for s in segs:
        assert math.isfinite(s["start"]) and math.isfinite(s["end"])
        assert 0.0 <= s["start"] <= s["end"] <= 900.0
        assert s["start"] >= prev_end
        prev_end = s["end"]

    # 1c. Incomplete status raises _GeminiMaxTokensError so bounded chunk-splitting triggers
    monkeypatch.setattr(
        toledo_dl.urllib.request,
        "urlopen",
        lambda *_a, **_kw: FakeResp({"id": "int_inc", "status": "incomplete", "steps": []}),
    )
    try:
        toledo_dl._call_gemini_audio_chunk(
            b"mp3", model="gemini-3.5-transcribe", api_key="k", language="en", chunk_duration_sec=900.0,
        )
        assert False, "Expected _GeminiMaxTokensError on incomplete interaction"
    except toledo_dl._GeminiMaxTokensError:
        pass

    # 2. MAX_TOKENS on a 900s chunk triggers bounded split-and-resume into 450s halves
    course_dir = isolate_toledo_env["out"] / "Split Course"
    lectures = course_dir / "Lectures"
    lectures.mkdir(parents=True)
    audio = lectures / "Lecture 1 - Long.mp3"
    audio.write_bytes(b"long-audio")

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFakeTestKey")
    monkeypatch.setattr(toledo_dl, "_probe_video_duration", lambda _p: 900.0)
    monkeypatch.setattr(toledo_dl, "_split_audio_chunks", lambda _p, tmp_dir, chunk_seconds=900: [
        {"index": 0, "offset_sec": 0.0, "duration_sec": 900.0, "path": audio}
    ])
    monkeypatch.setattr(toledo_dl, "_extract_audio_subchunk", lambda _s, _o, _off, _dur: b"subchunk")

    seen_durations = []

    def fake_chunk_call(chunk_bytes, *, model, api_key, language, chunk_duration_sec):
        seen_durations.append(chunk_duration_sec)
        if chunk_duration_sec > 500.0:
            raise toledo_dl._GeminiMaxTokensError("MAX_TOKENS")
        return [{"start": 1.0, "end": 10.0, "text": f"Half {int(chunk_duration_sec)}."}]

    monkeypatch.setattr(toledo_dl, "_call_gemini_audio_chunk", fake_chunk_call)
    approval = toledo_dl._make_cloud_approval(
        "transcribe", "gemini", toledo_dl._GEMINI_TRANSCRIBE_DEFAULT_MODEL, [audio],
        data_types=("audio_chunks",), course_dirs=[course_dir], max_duration_sec=1800.0,
    )
    assert toledo_dl._transcribe_audio_via_gemini(audio, cloud_approval=approval, course_dir=course_dir) is True
    assert seen_durations == [900.0, 450.0, 450.0]
    srt_text = audio.with_suffix(".srt").read_text(encoding="utf-8")
    assert "00:00:01,000 --> 00:00:10,500" in srt_text or "00:00:01,000 --> 00:00:10,000" in srt_text
    assert "00:07:31,000 --> 00:07:40,000" in srt_text


def test_top_level_symlinked_course_dir_rejected_in_match_slides(isolate_toledo_env, monkeypatch, tmp_path):
    external_course = tmp_path / "external_course"
    (external_course / "Lectures").mkdir(parents=True)
    (external_course / "PDFs").mkdir(parents=True)
    (external_course / "PDFs" / "1_secret.pdf").write_bytes(b"%PDF-1.4 secret")
    (external_course / "Lectures" / "Lecture 1 - Secret.txt").write_text(
        " ".join(["confidential", "external", "notes"] * 12),
        encoding="utf-8",
    )
    (external_course / "Lectures" / "Lecture 1 - Secret.mp4").write_bytes(b"fake-video")

    symlink_course = isolate_toledo_env["out"] / "Symlinked Course"
    symlink_course.symlink_to(external_course)

    provider_calls = []
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFakeTestKey")
    monkeypatch.setattr(toledo_dl.urllib.request, "urlopen", lambda *a, **kw: provider_calls.append(a))

    rc_named = toledo_dl.cmd_match_slides(argparse.Namespace(
        course="Symlinked Course",
        backend="gemini",
        model=None,
        no_video=False,
        limit=None,
        dry_run=False,
        yes=True,
        force=False,
    ))
    rc_all = toledo_dl.cmd_match_slides(argparse.Namespace(
        course=None,
        backend="gemini",
        model=None,
        no_video=False,
        limit=None,
        dry_run=False,
        yes=True,
        force=False,
    ))
    assert rc_named == 1
    assert rc_all == 1
    assert provider_calls == []


def test_split_retry_fails_closed_when_ffmpeg_missing(isolate_toledo_env, monkeypatch):
    course_dir = isolate_toledo_env["out"] / "Split Fail Closed"
    lectures = course_dir / "Lectures"
    lectures.mkdir(parents=True)
    video = lectures / "Lecture 1 - Video.mp4"
    video.write_bytes(b"RAW-MP4-BYTES-MUST-NOT-UPLOAD")

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFakeTestKey")
    monkeypatch.setattr(toledo_dl, "_probe_video_duration", lambda _p: 900.0)

    def fake_split(_p, work_dir, chunk_seconds=900, **_kw):
        chunk_mp3 = work_dir / "chunk_0000.mp3"
        chunk_mp3.write_bytes(b"INITIAL-MP3-CHUNK")
        return [{"index": 0, "offset_sec": 0.0, "duration_sec": 900.0, "path": chunk_mp3}]

    monkeypatch.setattr(toledo_dl, "_split_audio_chunks", fake_split)
    monkeypatch.setattr(toledo_dl.shutil, "which", lambda _cmd: None)

    uploaded_payloads = []

    def fake_chunk_call(chunk_bytes, *, model, api_key, language, chunk_duration_sec):
        uploaded_payloads.append(chunk_bytes)
        raise toledo_dl._GeminiMaxTokensError("MAX_TOKENS")

    monkeypatch.setattr(toledo_dl, "_call_gemini_audio_chunk", fake_chunk_call)
    approval = toledo_dl._make_cloud_approval(
        "transcribe", "gemini", toledo_dl._GEMINI_TRANSCRIBE_DEFAULT_MODEL, [video],
        data_types=("audio_chunks",), course_dirs=[course_dir], max_duration_sec=1800.0,
    )
    with pytest.raises(RuntimeError, match="ffmpeg"):
        toledo_dl._transcribe_audio_via_gemini(video, cloud_approval=approval, course_dir=course_dir)

    # Only the initial MP3 chunk call ran; zero retry calls and zero raw MP4 uploads.
    assert uploaded_payloads == [b"INITIAL-MP3-CHUNK"]


def test_local_transcribe_recovers_from_failed_cloud_commit(isolate_toledo_env, monkeypatch):
    course_dir = isolate_toledo_env["out"] / "Local Recovery Course"
    lectures = course_dir / "Lectures"
    lectures.mkdir(parents=True)
    audio = lectures / "Lecture 1 - Recovery.mp3"
    audio.write_bytes(b"fake-audio-bytes")

    orig_replace = toledo_dl._replace_bytes

    def fail_on_srt(path, data):
        if Path(path).suffix == ".srt":
            raise OSError("disk full")
        return orig_replace(path, data)

    monkeypatch.setattr(toledo_dl, "_replace_bytes", fail_on_srt)
    with pytest.raises(OSError):
        toledo_dl._commit_transcript_pair(
            audio,
            [{"start": 0.0, "end": 2.0, "text": "Partial cloud transcript."}],
            backend="gemini",
            model="gemini-2.5-flash",
            recording_sha256="deadbeef",
        )
    assert toledo_dl._transcript_inprogress_marker(audio).is_file()
    assert toledo_dl._is_transcript_complete(audio) is False
    assert toledo_dl._course_match_transcripts(course_dir) == []

    # Restore disk writes and recover via local Whisper transcription
    monkeypatch.setattr(toledo_dl, "_replace_bytes", orig_replace)
    whisper_calls = []

    class FakeSeg:
        def __init__(self):
            self.start = 0.0
            self.end = 3.0
            self.text = "Recovered local transcript text."

    class FakeInfo:
        language = "en"
        language_probability = 0.99

    class FakeWhisperModel:
        def __init__(self, *_args, **_kwargs):
            whisper_calls.append("init")

        def transcribe(self, *_args, **_kwargs):
            return [FakeSeg()], FakeInfo()

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeWhisperModel))
    assert toledo_dl.transcribe_file(audio, model_size="small", device="cpu") is True

    # In-progress marker is cleared, done marker + pair exist, and normal discovery sees it
    assert not toledo_dl._transcript_inprogress_marker(audio).exists()
    assert toledo_dl._transcript_done_marker(audio).is_file()
    assert audio.with_suffix(".txt").is_file()
    assert audio.with_suffix(".srt").is_file()
    assert toledo_dl._is_transcript_complete(audio) is True
    assert toledo_dl._is_transcript_txt(audio.with_suffix(".txt")) is True
    assert toledo_dl._course_match_transcripts(course_dir) == [audio.with_suffix(".txt")]

    # Subsequent transcription run must NOT re-queue the recovered recording
    whisper_calls.clear()
    rc = toledo_dl.cmd_transcribe(argparse.Namespace(
        course="Local Recovery Course", file=None, backend="cpu", model="small", lang="en",
        limit=None, max_minutes=None, chunk_seconds=None, yes=False, dry_run=False,
    ))
    assert rc == 0
    assert whisper_calls == []


def test_failed_dispatch_duration_probe_blocks_over_cap_chunking(isolate_toledo_env, monkeypatch):
    course_dir = isolate_toledo_env["out"] / "Probe Guard Course"
    lectures = course_dir / "Lectures"
    lectures.mkdir(parents=True)
    audio = lectures / "Lecture 1 - Probe.mp3"
    audio.write_bytes(b"probe-audio")

    probe_results = iter([None, 600.0])
    probe_calls = []

    def flaky_probe(_p):
        val = next(probe_results)
        probe_calls.append(val)
        return val

    provider_calls = []
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFakeTestKey")
    monkeypatch.setattr(toledo_dl, "_probe_video_duration", flaky_probe)
    monkeypatch.setattr(toledo_dl, "_call_gemini_audio_chunk", lambda *a, **kw: provider_calls.append(a))
    monkeypatch.setattr(toledo_dl.urllib.request, "urlopen", lambda *a, **kw: provider_calls.append(a))

    approval = toledo_dl._make_cloud_approval(
        "transcribe", "gemini", toledo_dl._GEMINI_TRANSCRIBE_DEFAULT_MODEL, [audio],
        data_types=("audio_chunks",),
        course_dirs=[course_dir],
        expected_hashes={audio: toledo_dl._file_sha256(audio)},
        max_duration_sec=60.0,
        max_file_durations={audio: 60.0},
    )
    ok = toledo_dl._transcribe_audio_via_gemini(audio, cloud_approval=approval, course_dir=course_dir)
    assert ok is False
    assert probe_calls == [None]
    assert provider_calls == []

    # When dispatch probe succeeds within cap, chunking receives the validated duration without re-probing
    received_durs = []

    def spy_split(_p, work_dir, chunk_seconds=900, *, duration_sec=None):
        received_durs.append(duration_sec)
        c0 = work_dir / "chunk_0000.mp3"
        c0.write_bytes(b"MP3")
        return [{"index": 0, "offset_sec": 0.0, "duration_sec": float(duration_sec), "path": c0}]

    probe_calls.clear()
    monkeypatch.setattr(toledo_dl, "_probe_video_duration", lambda _p: probe_calls.append(30.0) or 30.0)
    monkeypatch.setattr(toledo_dl, "_split_audio_chunks", spy_split)
    monkeypatch.setattr(
        toledo_dl,
        "_call_gemini_audio_chunk",
        lambda *_a, **_kw: [{"start": 0.0, "end": 5.0, "text": "Within cap."}],
    )
    assert toledo_dl._transcribe_audio_via_gemini(audio, cloud_approval=approval, course_dir=course_dir) is True
    assert probe_calls == [30.0]
    assert received_durs == [30.0]


def test_sync_and_index_reject_symlinked_course_root(isolate_toledo_env, tmp_path):
    base = toledo_dl.BASE
    external_course = tmp_path / "external_sync_index"
    external_course.mkdir(parents=True)
    sentinel = external_course / "notes.txt"
    sentinel.write_text("external content", encoding="utf-8")

    symlink_course = isolate_toledo_env["out"] / "Symlinked Course"
    symlink_course.symlink_to(external_course)

    # 1. Index (both --course and all-courses) must leave external directory untouched
    assert toledo_dl.cmd_index(argparse.Namespace(course="Symlinked Course")) == 0
    assert toledo_dl.cmd_index(argparse.Namespace(course=None)) == 0
    assert not (external_course / "INDEX.md").exists()
    assert not (external_course / "LLM_Knowledge").exists()

    # 2. sync_files must reject writing downloaded PDFs through a symlinked course root
    routes = {
        f"{base}/learn/api/public/v1/courses/c_sym/contents?limit=200": FakeResponse(
            json_data={
                "results": [
                    {
                        "id": "pdf1",
                        "title": "Slides",
                        "hasChildren": False,
                        "contentHandler": {"id": "resource/x-bb-file", "file": {"fileName": "slides.pdf"}},
                    }
                ]
            }
        ),
        f"{base}/learn/api/public/v1/courses/c_sym/contents/pdf1/attachments": FakeResponse(
            json_data={"results": [{"id": "att1", "fileName": "slides.pdf"}]}
        ),
        f"{base}/learn/api/public/v1/courses/c_sym/contents/pdf1/attachments/att1/download": FakeResponse(
            status=200, body=b"%PDF-1.4 payload"
        ),
    }
    ctx = FakeBrowserContext(routes)
    report = {"courses": {}}
    total_dl = toledo_dl.sync_files(ctx, [("c_sym", "Symlinked Course")], report=report)
    assert total_dl == 0
    assert list(external_course.iterdir()) == [sentinel]
    assert report["courses"].get("Symlinked Course", {}).get("failed", 0) == 1


def test_output_file_symlinks_cannot_overwrite_external_files(isolate_toledo_env, tmp_path):
    import os

    base = toledo_dl.BASE
    course_dir = isolate_toledo_env["out"] / "Safe Course"
    course_dir.mkdir(parents=True)

    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel_index = outside / "sentinel_index.md"
    sentinel_ann = outside / "sentinel_ann.md"
    sentinel_index.write_text("DO NOT OVERWRITE INDEX", encoding="utf-8")
    sentinel_ann.write_text("DO NOT OVERWRITE ANN", encoding="utf-8")

    # Symlinked LLM_Knowledge/transcripts directory with an older sentinel .md
    ext_tr_dir = outside / "ext_transcripts"
    ext_tr_dir.mkdir()
    sentinel_tr_md = ext_tr_dir / "Lecture_01.md"
    sentinel_tr_md.write_text('---\nsource: "Lecture_01.txt"\n---\nDO NOT OVERWRITE TR\n', encoding="utf-8")
    os.utime(sentinel_tr_md, (1000000, 1000000))

    # Symlinked LLM_Knowledge/passages.jsonl pointing to an external stale passage file
    sentinel_passages = outside / "ext_passages.jsonl"
    stale_line = json.dumps({
        "passage_id": "ext:1",
        "course": "Safe Course",
        "source_id": "ext",
        "source_path": "Safe Course/Lecture_01.txt",
        "source_name": "Lecture_01.txt",
        "kind": "document_section",
        "text": "StaleSymlinkToken must never be searchable.",
    }) + "\n"
    sentinel_passages.write_text(stale_line, encoding="utf-8")

    # Symlinked LLM_Knowledge/Chapters directory with a pre-existing chapter subdir containing sentinel.txt
    ext_chapters = outside / "ext_chapters"
    ext_chap_sub = ext_chapters / "01_1_intro"
    ext_chap_sub.mkdir(parents=True)
    sentinel_chap_file = ext_chap_sub / "sentinel.txt"
    sentinel_chap_file.write_text("DO NOT DELETE CHAPTER SENTINEL", encoding="utf-8")

    (course_dir / "INDEX.md").symlink_to(sentinel_index)
    (course_dir / "Announcements.md").symlink_to(sentinel_ann)
    kb_dir = course_dir / "LLM_Knowledge"
    kb_dir.mkdir(parents=True)
    (kb_dir / "transcripts").symlink_to(ext_tr_dir)
    (kb_dir / "passages.jsonl").symlink_to(sentinel_passages)
    (kb_dir / "Chapters").symlink_to(ext_chapters)

    # Add a valid transcript + audio + PDF so index attempts to export Lecture_01.md and populate Chapters/01_1_intro
    (course_dir / "Lecture_01.mp3").write_bytes(b"mp3")
    (course_dir / "Lecture_01.txt").write_text("Fresh lecture transcript text.\n", encoding="utf-8")
    (course_dir / "PDFs").mkdir(parents=True)
    (course_dir / "PDFs" / "1_intro.pdf").write_bytes(b"%PDF-1.4 intro")

    # 1. Reindexing must not overwrite external INDEX.md, transcripts/*.md, passages.jsonl, or delete/modify Chapters/
    assert toledo_dl.cmd_index(argparse.Namespace(course="Safe Course")) == 0
    assert sentinel_index.read_text(encoding="utf-8") == "DO NOT OVERWRITE INDEX"
    assert sentinel_tr_md.read_text(encoding="utf-8") == '---\nsource: "Lecture_01.txt"\n---\nDO NOT OVERWRITE TR\n'
    assert sentinel_passages.read_text(encoding="utf-8") == stale_line
    assert sentinel_chap_file.read_text(encoding="utf-8") == "DO NOT DELETE CHAPTER SENTINEL"
    assert list(ext_chap_sub.iterdir()) == [sentinel_chap_file]
    assert list(ext_chapters.iterdir()) == [ext_chap_sub]
    # Search must NOT follow the symlinked passages.jsonl
    assert toledo_dl.search_passages("StaleSymlinkToken", course_filter="Safe Course") == []

    # 2. Announcement sync must process the mocked announcement (limit=50) without overwriting external Announcements.md
    routes = {
        f"{base}/learn/api/public/v1/courses/c_safe/announcements?limit=50": FakeResponse(
            json_data={
                "results": [
                    {
                        "id": "ann1",
                        "title": "Exam Date",
                        "body": "<p>Exam is next Monday.</p>",
                        "created": "2026-11-19T10:00:00Z",
                    }
                ]
            }
        )
    }
    ctx = FakeBrowserContext(routes)
    new_anns = toledo_dl.sync_announcements(ctx, [("c_safe", "Safe Course")], show_all=True)
    assert new_anns == 1
    assert sentinel_ann.read_text(encoding="utf-8") == "DO NOT OVERWRITE ANN"

    # 3. Symlinked LLM_Knowledge root and symlinked chapter migration destination must not abort or touch external files
    kb_sym_course = isolate_toledo_env["out"] / "KB Symlink Course"
    (kb_sym_course / "PDFs").mkdir(parents=True)
    (kb_sym_course / "PDFs" / "1_intro.pdf").write_bytes(b"%PDF-1.4 intro")
    ext_kb = outside / "ext_kb"
    ext_kb.mkdir()
    kb_sentinel = ext_kb / "keep.txt"
    kb_sentinel.write_text("KEEP KB", encoding="utf-8")
    (kb_sym_course / "LLM_Knowledge").symlink_to(ext_kb)
    assert toledo_dl.cmd_index(argparse.Namespace(course="KB Symlink Course")) == 0
    assert list(ext_kb.iterdir()) == [kb_sentinel]

    mig_course = isolate_toledo_env["out"] / "Migration Symlink Course"
    (mig_course / "PDFs").mkdir(parents=True)
    (mig_course / "PDFs" / "2_later.pdf").write_bytes(b"%PDF-1.4 later")
    assert toledo_dl.cmd_index(argparse.Namespace(course="Migration Symlink Course")) == 0
    chapters_root = mig_course / "LLM_Knowledge" / "Chapters"
    orig_chap = chapters_root / "01_2_later"
    assert orig_chap.is_dir()
    orig_report = orig_chap / "report.md"
    orig_report.write_text("KEEP ORIGINAL REPORT", encoding="utf-8")

    (mig_course / "PDFs" / "1_intro.pdf").write_bytes(b"%PDF-1.4 intro")
    ext_mig = outside / "ext_mig_dest"
    ext_mig.mkdir()
    mig_sentinel = ext_mig / "sentinel.txt"
    mig_sentinel.write_text("KEEP MIG", encoding="utf-8")
    (chapters_root / "02_2_later").symlink_to(ext_mig)
    assert toledo_dl.cmd_index(argparse.Namespace(course="Migration Symlink Course")) == 0
    assert list(ext_mig.iterdir()) == [mig_sentinel]
    assert not any(p.name.startswith(".__migrate_") for p in chapters_root.iterdir())
    assert orig_report.read_text(encoding="utf-8") == "KEEP ORIGINAL REPORT"
    assert (chapters_root / "01_1_intro" / "CHAPTER_BUNDLE.md").is_file()


def test_release_acceptance_openai_transcribe_and_auto_reindex(isolate_toledo_env, monkeypatch, capsys):
    course_dir = isolate_toledo_env["out"] / "OpenAI Course"
    rec_dir = course_dir / "Lectures"
    rec_dir.mkdir(parents=True)
    audio = rec_dir / "Lecture 01 - Intro.mp3"
    audio.write_bytes(b"FAKE-MP3-DATA")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-secret")
    monkeypatch.setattr(toledo_dl, "_probe_video_duration", lambda _p: 120.0)
    monkeypatch.setattr(
        toledo_dl,
        "_split_audio_chunks",
        lambda _p, _tmp, chunk_seconds=900, duration_sec=None: [
            {"index": 0, "offset_sec": 0.0, "duration_sec": 120.0, "path": audio}
        ],
    )
    monkeypatch.setattr(
        toledo_dl,
        "_call_openai_audio_chunk",
        lambda chunk_bytes, *, model, api_key, language, chunk_duration_sec: [
            {"start": 0.0, "end": 5.0, "text": "OpenAIUniqueTranscriptToken works cleanly."}
        ],
    )

    rc = toledo_dl.cmd_transcribe(argparse.Namespace(
        course="OpenAI Course",
        file=None,
        backend="openai",
        model="auto",
        lang="en",
        limit=None,
        max_minutes=None,
        chunk_seconds=None,
        yes=True,
        dry_run=False,
    ))
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "Estimated cost:" in out_text
    assert "sk-test-openai-secret" not in out_text
    assert audio.with_suffix(".txt").is_file()
    assert audio.with_suffix(".srt").is_file()
    # Standalone transcribe must automatically update the search index.
    hits = toledo_dl.search_passages("OpenAIUniqueTranscriptToken", course_filter="OpenAI Course")
    assert len(hits) >= 1


def test_release_acceptance_anthropic_match_and_auto_reindex(isolate_toledo_env, monkeypatch, capsys):
    course_dir = isolate_toledo_env["out"] / "Claude Match Course"
    (course_dir / "PDFs").mkdir(parents=True)
    (course_dir / "Lectures").mkdir(parents=True)
    pdf = course_dir / "PDFs" / "1_intro.pdf"
    pdf.write_bytes(b"%PDF-1.4 intro")
    txt = course_dir / "Lectures" / "Lecture 01 - Intro.txt"
    txt.write_text(
        "Introductory lecture covering ClaudeMatchUniqueToken and statistical foundations "
        "including probability, distributions, random variables, estimation, and regression modeling "
        "in detail for first year graduate students.",
        encoding="utf-8",
    )
    txt.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nIntro\n", encoding="utf-8")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-key")
    monkeypatch.setattr(toledo_dl, "pypdf", object())
    monkeypatch.setattr(
        toledo_dl,
        "_extract_pdf_outline",
        lambda _p: ("Intro slides", 5, "Slide 1: Intro"),
    )
    monkeypatch.setattr(
        toledo_dl,
        "_call_match_anthropic",
        lambda prompt, frames, model, api_key: {
            "matched_pdfs": [pdf.name],
            "confidence": "high",
            "reason": "Direct match.",
        },
    )

    rc = toledo_dl.cmd_match_slides(argparse.Namespace(
        course="Claude Match Course",
        backend="anthropic",
        model=None,
        no_video=True,
        limit=None,
        dry_run=False,
        yes=True,
        force=False,
    ))
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "Estimated cost:" in out_text
    assert "sk-ant-secret-key" not in out_text
    mapping = json.loads((course_dir / ".pdf_mapping.json").read_text(encoding="utf-8"))
    assert mapping["Lectures/Lecture 01 - Intro.txt"]["matched_pdfs"] == [pdf.name]
    assert (course_dir / "INDEX.md").is_file()


def test_release_acceptance_sync_prefs_no_keys_and_non_interactive_cloud_deferred(isolate_toledo_env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaShouldNeverBeSaved")
    toledo_dl._save_sync_preferences(transcribe_backend="gemini", match_backend="anthropic")
    prefs_raw = toledo_dl._sync_prefs_path().read_text(encoding="utf-8")
    assert "AIzaShouldNeverBeSaved" not in prefs_raw
    loaded = toledo_dl._load_sync_preferences()
    assert loaded == {"transcribe_backend": "gemini", "match_backend": "anthropic"}

    # Estimate helper returns readable cost strings.
    assert "$" in toledo_dl._estimate_cloud_cost("transcribe", "openai", "whisper-1", total_minutes=60.0)
    assert "$" in toledo_dl._estimate_cloud_cost("match-slides", "anthropic", "claude-sonnet-4-5", item_count=3, has_video=True)


def test_cpu_matcher_hypothetical_edge_cases(tmp_path):
    # 1. Joined-token + folder path: "Homework 2 - FEV" must match Homework2.pdf over chapter handouts
    c1 = tmp_path / "Course1"
    hw_dir = c1 / "Homeworks" / "Homework 2" / "Exercises"
    sl_dir = c1 / "Slides"
    hw_dir.mkdir(parents=True)
    sl_dir.mkdir(parents=True)
    hw2 = hw_dir / "Homework2.pdf"
    ch1 = sl_dir / "Chapter 1_handouts.pdf"
    ch6 = sl_dir / "Chapter 6_handouts.pdf"
    for p in (hw2, ch1, ch6):
        p.write_bytes(b"%PDF-1.4")
    txt_hw = c1 / "Recordings" / "Homework 2 - FEV.txt"
    txt_hw.parent.mkdir(parents=True)
    txt_hw.write_text("Solution to homework 2 on FEV data.", encoding="utf-8")
    outlines = {
        "Homework2.pdf": "Homework 2 exercises on FEV dataset",
        "Chapter 1_handouts.pdf": "Introduction to linear regression models",
        "Chapter 6_handouts.pdf": "Model assumptions and transformations",
    }
    res1 = toledo_dl._deterministic_pdf_match(
        txt_hw, "Solution to homework 2", "weighted least squares", [ch1, ch6, hw2], outlines,
        full_text="Solution to homework 2 on FEV data and weighted least squares regression.",
    )
    assert "Homework2.pdf" in res1

    # 2. Two-half split: first half covers PCA, second half covers CFA -> both decks linked
    pca = sl_dir / "2_pca.pdf"
    cfa = sl_dir / "4_cfa.pdf"
    other = sl_dir / "9_clustering.pdf"
    for p in (pca, cfa, other):
        p.write_bytes(b"%PDF-1.4")
    txt_split = c1 / "Recordings" / "Lecture 12 - Combined.txt"
    full_split = (
        ("eigenvalue decomposition principal components biplot variance " * 15)
        + ("confirmatory factor analysis latent variables path diagram " * 15)
    )
    txt_split.write_text(full_split, encoding="utf-8")
    outlines2 = {
        "2_pca.pdf": "Principal Components Analysis eigenvalue decomposition biplot",
        "4_cfa.pdf": "Confirmatory Factor Analysis latent variables path diagram",
        "9_clustering.pdf": "Hierarchical clustering dendrogram k-means",
    }
    res2 = toledo_dl._deterministic_pdf_match(
        txt_split, full_split[:150], full_split[-150:], [pca, cfa, other], outlines2, full_text=full_split,
    )
    assert set(res2) == {"2_pca.pdf", "4_cfa.pdf"}

    # 3. Whisper CPU speech-to-text typos ("heteroskeleton" vs "heteroskedasticity")
    het = sl_dir / "7_diagnostics.pdf"
    het.write_bytes(b"%PDF-1.4")
    txt_typo = c1 / "Recordings" / "Lecture 15 - Diagnostics.txt"
    typo_text = "Today we check heteroskeleton sticity and heteroskeleton system in residuals."
    txt_typo.write_text(typo_text, encoding="utf-8")
    res3 = toledo_dl._deterministic_pdf_match(
        txt_typo, typo_text, typo_text, [het, other],
        {"7_diagnostics.pdf": "Heteroskedasticity diagnostics and residual plots", "9_clustering.pdf": "Clustering"},
        full_text=typo_text,
    )
    assert res3[0] == "7_diagnostics.pdf"

    # 4. Empty text / zero PDFs must not crash
    assert toledo_dl._deterministic_pdf_match(txt_typo, "", "", [], {}, full_text="") == []
    assert toledo_dl._deterministic_pdf_match(txt_typo, "", "", [het], {}, full_text="") == ["7_diagnostics.pdf"]


def test_sync_calendar_and_deadlines_writes_prominent_files(isolate_toledo_env, monkeypatch):
    course_dir = isolate_toledo_env["out"] / "Linear Models [G0Z50a]"
    course_dir.mkdir(parents=True)

    monkeypatch.setattr(
        toledo_dl,
        "get_schedule",
        lambda _c=None, refresh=False: [
            {
                "date": "2099-01-15",
                "startTime": "09:00",
                "endTime": "12:00",
                "course": "Linear Models [G0Z50a]",
                "title": "Written Exam Linear Models",
                "eventType": "Exam",
                "location": "Aula 200K",
            },
            {
                "date": "2099-01-05",
                "startTime": "10:30",
                "endTime": "12:30",
                "course": "Linear Models [G0Z50a]",
                "title": "Lecture 12",
                "eventType": "Lecture",
                "location": "200G 00.06",
            },
        ],
    )

    def fake_fetch(_c, url):
        if "calendars/items" in url:
            return [{"calendarId": "cid1", "title": "Peer Review", "end": "2099-01-02T23:59:00Z", "type": "Event"}]
        if "gradebook/columns" in url:
            return [{"name": "Assignment 2", "grading": {"due": "2099-01-08T18:00:00Z"}}]
        return []

    monkeypatch.setattr(toledo_dl, "_fetch_all_contents", fake_fetch)
    toledo_dl.sync_calendar_and_deadlines(object(), [("cid1", "Linear Models [G0Z50a]")])
    toledo_dl.build_llm_index()

    dl_md = (isolate_toledo_env["out"] / "DEADLINES_AND_EXAMS.md").read_text(encoding="utf-8")
    cal_md = (isolate_toledo_env["out"] / "CALENDAR.md").read_text(encoding="utf-8")
    course_dl = (course_dir / "Deadlines.md").read_text(encoding="utf-8")
    llm_idx = (isolate_toledo_env["out"] / "LLM_INDEX.md").read_text(encoding="utf-8")

    assert "Written Exam Linear Models" in dl_md
    assert "Assignment 2" in dl_md
    assert "Peer Review" in dl_md
    assert "Aula 200K" in dl_md
    assert "200G 00.06" in cal_md
    assert "Assignment 2" in course_dl
    assert "DEADLINES_AND_EXAMS.md" in llm_idx
    assert "CALENDAR.md" in llm_idx


def test_pdf_vision_interactive_prompt_and_preferences(isolate_toledo_env, monkeypatch, capsys):
    # Default (empty input) selects "off" and prints diagram vs text-heavy recommendation + cost warning
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    assert toledo_dl._prompt_pdf_vision_backend(has_gpu=True) == "off"
    out = capsys.readouterr().out
    assert "Recommended if your PDFs have lots of high-value diagrams" in out
    assert "Not recommended (usually overkill) if your PDFs are mostly plain text" in out
    assert "Local GPU vision" in out
    assert "incurs a small API cost" in out

    # Explicit choices 2 (ollama) and 3 (gemini)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "2")
    assert toledo_dl._prompt_pdf_vision_backend(has_gpu=False) == "ollama"
    monkeypatch.setattr("builtins.input", lambda _prompt="": "3")
    assert toledo_dl._prompt_pdf_vision_backend(has_gpu=True) == "gemini"

    # Saved preferences store pdf_vision_backend cleanly
    toledo_dl._save_sync_preferences(pdf_vision_backend="ollama")
    prefs = toledo_dl._load_sync_preferences()
    assert prefs.get("pdf_vision_backend") == "ollama"


def _make_valid_pdf_bytes(num_pages: int = 1, *, hex_escape_first: bool = False) -> bytes:
    """Build a minimal valid N-page PDF parsed identically by pypdf and pdfinfo."""
    kids = b" ".join(f"{n} 0 R".encode() for n in range(3, 3 + num_pages))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count " + str(num_pages).encode() + b" /Kids [" + kids + b"] >>",
    ]
    for i in range(num_pages):
        page_type = b"/Pa#67e" if (hex_escape_first and i == 0) else b"/Page"
        objs.append(b"<< /Type " + page_type + b" /Parent 2 0 R /MediaBox [0 0 100 100] /Resources <<>> >>")
    raw = b"%PDF-1.4\n"
    offsets = []
    for n, obj in enumerate(objs, 1):
        offsets.append(len(raw))
        raw += f"{n} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(raw)
    raw += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    raw += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    raw += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return raw


def test_pdf_vision_cloud_gemini_consent_cache_and_preservation(isolate_toledo_env, monkeypatch):
    course_dir = isolate_toledo_env["out"] / "Bio Physiology"
    pdfs_dir = course_dir / "PDFs"
    pdfs_dir.mkdir(parents=True)
    pdf_file = pdfs_dir / "7_suikerstofwisseling.pdf"
    pdf_file.write_bytes(_make_valid_pdf_bytes(1))

    cloud_requests = []

    class FakeGeminiResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "candidates": [{
                    "content": {
                        "parts": [{
                            "text": (
                                "## Slide 1: Suikerstofwisseling\n"
                                "- Glycogenese: glucose -> glycogeen (via insuline)\n"
                                "- Glycogenolyse: glycogeen -> glucose (via glucagon, adrenaline)\n"
                            )
                        }]
                    }
                }]
            }).encode("utf-8")

    def fake_urlopen(req, **_kwargs):
        cloud_requests.append((req.full_url, dict(req.header_items())))
        return FakeGeminiResp()

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFakeVisionKey")
    monkeypatch.setattr(toledo_dl.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(toledo_dl.sys.stdin, "isatty", lambda: False)

    # 1. Plain index (vision_backend=off) makes ZERO cloud calls even with GEMINI_API_KEY set
    assert toledo_dl.cmd_index(argparse.Namespace(course="Bio Physiology")) == 0
    assert cloud_requests == []

    # 2. Dry run and unapproved non-interactive run make ZERO cloud calls
    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Bio Physiology", vision_backend="gemini", vision_model=None, limit=None, dry_run=True, yes=False,
    )) == 0
    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Bio Physiology", vision_backend="gemini", vision_model=None, limit=None, dry_run=False, yes=False,
    )) == 1
    assert cloud_requests == []

    # 3. Direct unapproved call refuses upload
    with pytest.raises(PermissionError):
        toledo_dl._extract_pdf_via_gemini_vision(pdf_file, cloud_approval=None, course_dir=course_dir)
    assert cloud_requests == []

    # 4. Approved run uploads once via header auth (never ?key=), writes vision Markdown & searchable passages
    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Bio Physiology", vision_backend="gemini", vision_model=None, limit=None, dry_run=False, yes=True,
    )) == 0
    assert len(cloud_requests) == 1
    url, headers = cloud_requests[0]
    assert "?key=" not in url
    assert headers.get("X-goog-api-key") == "AIzaFakeVisionKey"

    md_file = course_dir / "LLM_Knowledge" / "pdfs" / "7_suikerstofwisseling.md"
    assert md_file.is_file()
    md_text = md_file.read_text(encoding="utf-8")
    assert 'vision_backend: "gemini"' in md_text
    assert "glucose -> glycogeen (via insuline)" in md_text

    hits = toledo_dl.search_passages("glycogeen", course_filter="Bio Physiology")
    assert len(hits) == 1
    assert "insuline" in hits[0]["context_text"]

    # 5. Re-running gemini vision OR plain index reuses/preserves the cached vision Markdown with ZERO API calls
    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Bio Physiology", vision_backend="gemini", vision_model=None, limit=None, dry_run=False, yes=True,
    )) == 0
    assert toledo_dl.cmd_index(argparse.Namespace(course="Bio Physiology")) == 0
    assert len(cloud_requests) == 1
    assert "glucose -> glycogeen (via insuline)" in md_file.read_text(encoding="utf-8")


def test_pdf_vision_local_ollama_gpu_and_graceful_fallback(isolate_toledo_env, monkeypatch):
    course_dir = isolate_toledo_env["out"] / "Local Vision Course"
    pdfs_dir = course_dir / "PDFs"
    pdfs_dir.mkdir(parents=True)
    pdf_file = pdfs_dir / "2_pathways.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake-diagram-pdf")

    ollama_calls = []

    def fake_render(_pdf, out_dir, dpi=150):
        p1 = out_dir / "page-1.png"
        p1.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return [p1]

    def fake_http_open(_self, req):
        import io
        import urllib.response

        assert req.host == "localhost:11434"
        assert "localhost:11434" in req.full_url
        ollama_calls.append((req.host, req.full_url))
        body = json.dumps({
            "response": "## Slide 1: Pathway\n- fructose -> glucose (via fructase)\n"
        }).encode("utf-8")
        resp = urllib.response.addinfourl(io.BytesIO(body), {}, req.full_url, code=200)
        resp.msg = "OK"
        return resp

    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(toledo_dl, "_ensure_local_pdf_vision_setup", lambda *_a, **_kw: True)
    monkeypatch.setattr(toledo_dl, "_render_pdf_pages_to_pngs", fake_render)
    monkeypatch.setattr(toledo_dl.urllib.request.HTTPHandler, "http_open", fake_http_open)

    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Local Vision Course", vision_backend="ollama", vision_model=None, limit=None, dry_run=False, yes=False,
    )) == 0
    assert len(ollama_calls) == 1
    assert ollama_calls[0][0] == "localhost:11434"
    md_file = course_dir / "LLM_Knowledge" / "pdfs" / "2_pathways.md"
    assert 'vision_backend: "ollama"' in md_file.read_text(encoding="utf-8")
    assert len(toledo_dl.search_passages("fructase", course_filter="Local Vision Course")) == 1

    # Second run hits SHA-256 vision cache with zero Ollama calls
    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Local Vision Course", vision_backend="ollama", vision_model=None, limit=None, dry_run=False, yes=False,
    )) == 0
    assert len(ollama_calls) == 1

    # Missing pdftoppm on a new PDF falls back cleanly without crashing or making cloud requests
    pdf2 = pdfs_dir / "3_fallback.pdf"
    pdf2.write_bytes(b"%PDF-1.4 fallback-pdf")
    monkeypatch.setattr(toledo_dl, "_render_pdf_pages_to_pngs", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("pdftoppm missing")))
    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Local Vision Course", vision_backend="ollama", vision_model=None, limit=None, dry_run=False, yes=False,
    )) == 0
    assert len(ollama_calls) == 1


def test_ollama_vision_single_slide_timeout_falls_back_per_slide(isolate_toledo_env, monkeypatch):
    course_dir = isolate_toledo_env["out"] / "Partial Slide Vision"
    pdfs_dir = course_dir / "PDFs"
    pdfs_dir.mkdir(parents=True)
    pdf_file = pdfs_dir / "logic_slides_v3.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 multi-slide")

    def fake_render(_pdf, out_dir, dpi=150):
        p1 = out_dir / "page-1.png"
        p2 = out_dir / "page-2.png"
        p1.write_bytes(b"\x89PNG\r\n\x1a\n1")
        p2.write_bytes(b"\x89PNG\r\n\x1a\n2")
        return [p1, p2]

    calls = []

    def fake_call_page(_img_bytes, *, model, page_num):
        calls.append(page_num)
        if page_num == 2:
            raise TimeoutError("timed out")
        return "## Slide 1: Propositional Logic\n- P -> Q\n"

    monkeypatch.setattr(toledo_dl, "_ensure_local_pdf_vision_setup", lambda *_a, **_kw: True)
    monkeypatch.setattr(toledo_dl, "_render_pdf_pages_to_pngs", fake_render)
    monkeypatch.setattr(toledo_dl, "_call_ollama_pdf_vision_page", fake_call_page)

    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Partial Slide Vision", vision_backend="ollama", vision_model=None, limit=None, dry_run=False, yes=False,
    )) == 0
    assert calls == [1, 2]
    md_file = course_dir / "LLM_Knowledge" / "pdfs" / "logic_slides_v3.md"
    md_text = md_file.read_text(encoding="utf-8")
    assert 'vision_backend: "ollama"' in md_text
    assert "## Slide 1: Propositional Logic" in md_text
    assert "## Slide 2" in md_text

    # Subsequent sync/index sees it cached and does NOT re-scan the entire PDF
    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Partial Slide Vision", vision_backend="ollama", vision_model=None, limit=None, dry_run=False, yes=False,
    )) == 0
    assert calls == [1, 2]


def test_pdf_vision_audit_regressions(isolate_toledo_env, monkeypatch):
    # 1. MAX_TOKENS truncated Gemini response must NOT be cached as vision_ok
    c_trunc = isolate_toledo_env["out"] / "Trunc Course"
    (c_trunc / "PDFs").mkdir(parents=True)
    p_trunc = c_trunc / "PDFs" / "1_trunc.pdf"
    p_trunc.write_bytes(b"%PDF-1.4\n/Type /Page\n")

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFakeKey")
    monkeypatch.setattr(toledo_dl, "pypdf", None)
    monkeypatch.setattr(
        toledo_dl.urllib.request,
        "urlopen",
        lambda *_a, **_kw: FakeResp({
            "candidates": [{
                "finishReason": "MAX_TOKENS",
                "content": {"parts": [{"text": "## Slide 1: Cut off\nPartial text..."}]},
            }]
        }),
    )
    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Trunc Course", vision_backend="gemini", vision_model=None, limit=None, dry_run=False, yes=True,
    )) == 0
    trunc_md = c_trunc / "LLM_Knowledge" / "pdfs" / "1_trunc.md"
    if trunc_md.exists():
        assert 'vision_backend: "gemini"' not in trunc_md.read_text(encoding="utf-8")
    assert len(toledo_dl._plan_pdf_vision_jobs([c_trunc], vision_backend="gemini")) == 1

    # 2. 21-page PDF (including hex-escaped /Type /Pa#67e and compressed /ObjStm) must never bypass 20-page limit when pypdf is None
    p_21 = c_trunc / "PDFs" / "21_pages.pdf"
    p_21.write_bytes(_make_valid_pdf_bytes(21, hex_escape_first=True))
    hex_calls = []
    orig_chunk_fn = toledo_dl._call_gemini_pdf_vision_chunk
    monkeypatch.setattr(toledo_dl, "_call_gemini_pdf_vision_chunk", lambda *a, **kw: hex_calls.append(1) or "## Slide 1")
    approval_hex = toledo_dl._make_cloud_approval(
        "pdf-vision", "gemini", toledo_dl._PDF_VISION_GEMINI_DEFAULT_MODEL, [p_21],
        data_types=("pdf_pages",), course_dirs=[c_trunc],
    )
    with pytest.raises(RuntimeError, match="20-page"):
        toledo_dl._extract_pdf_via_gemini_vision(p_21, cloud_approval=approval_hex, course_dir=c_trunc)
    assert hex_calls == []

    # Compressed / unverifiable PDF also fails closed with zero provider calls
    p_objstm = c_trunc / "PDFs" / "21_compressed.pdf"
    p_objstm.write_bytes(b"%PDF-1.5\n<< /Type /Pages /Count 21 >>\n<< /Type /ObjStm /Filter /FlateDecode >>\n%%EOF\n")
    approval_objstm = toledo_dl._make_cloud_approval(
        "pdf-vision", "gemini", toledo_dl._PDF_VISION_GEMINI_DEFAULT_MODEL, [p_objstm],
        data_types=("pdf_pages",), course_dirs=[c_trunc],
    )
    with pytest.raises(RuntimeError, match="20-page"):
        toledo_dl._extract_pdf_via_gemini_vision(p_objstm, cloud_approval=approval_objstm, course_dir=c_trunc)
    # Missing both pypdf and pdfinfo also fails closed with zero provider calls even on a 1-page PDF
    p_1 = c_trunc / "PDFs" / "1_unverifiable.pdf"
    p_1.write_bytes(_make_valid_pdf_bytes(1))
    orig_which = toledo_dl.shutil.which
    monkeypatch.setattr(toledo_dl.shutil, "which", lambda _cmd: None)
    approval_unv = toledo_dl._make_cloud_approval(
        "pdf-vision", "gemini", toledo_dl._PDF_VISION_GEMINI_DEFAULT_MODEL, [p_1],
        data_types=("pdf_pages",), course_dirs=[c_trunc],
    )
    with pytest.raises(RuntimeError, match="20-page"):
        toledo_dl._extract_pdf_via_gemini_vision(p_1, cloud_approval=approval_unv, course_dir=c_trunc)
    assert hex_calls == []
    monkeypatch.setattr(toledo_dl.shutil, "which", orig_which)
    monkeypatch.setattr(toledo_dl, "_call_gemini_pdf_vision_chunk", orig_chunk_fn)

    class FailingWriter:
        def add_page(self, _p):
            pass

        def write(self, _buf):
            raise RuntimeError("writer failed")

    fake_pypdf = types.SimpleNamespace(
        PdfReader=lambda _s: types.SimpleNamespace(pages=[object()] * 21),
        PdfWriter=FailingWriter,
    )
    monkeypatch.setattr(toledo_dl, "pypdf", fake_pypdf)
    with pytest.raises(RuntimeError, match="writer failed"):
        toledo_dl._slice_pdf_page_chunks(p_21, chunk_pages=20)
    monkeypatch.setattr(toledo_dl, "pypdf", None)
    monkeypatch.setattr(toledo_dl, "_verified_pdf_page_count", lambda _p: 1)

    # 3. Uppercase Deck.PDF + cross-course duplicate SHA-256 + explicit --vision-model change + zero-pending dry-run
    c_a = isolate_toledo_env["out"] / "Course A"
    c_b = isolate_toledo_env["out"] / "Course B"
    (c_a / "PDFs").mkdir(parents=True)
    (c_b / "PDFs").mkdir(parents=True)
    same_bytes = _make_valid_pdf_bytes(1)
    (c_a / "PDFs" / "Deck.PDF").write_bytes(same_bytes)
    (c_b / "PDFs" / "Deck.pdf").write_bytes(same_bytes)

    jobs_both = toledo_dl._plan_pdf_vision_jobs([c_a, c_b], vision_backend="gemini", vision_model="model-a")
    assert len(jobs_both) == 2

    api_calls = []

    def fake_ok_urlopen(req, **_kw):
        api_calls.append(req.full_url)
        return FakeResp({
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "## Slide 1: Deck\nA -> B\n"}]},
            }]
        })

    monkeypatch.setattr(toledo_dl.urllib.request, "urlopen", fake_ok_urlopen)
    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Course A", vision_backend="gemini", vision_model="model-a", limit=None, dry_run=False, yes=True,
    )) == 0
    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Course B", vision_backend="gemini", vision_model="model-a", limit=None, dry_run=False, yes=True,
    )) == 0
    assert len(api_calls) == 2
    assert 'vision_model: "model-a"' in (c_a / "LLM_Knowledge" / "pdfs" / "Deck.md").read_text(encoding="utf-8")
    assert 'vision_model: "model-a"' in (c_b / "LLM_Knowledge" / "pdfs" / "Deck.md").read_text(encoding="utf-8")

    # Explicit --vision-model change invalidates model-a cache and rescans with model-b
    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Course A", vision_backend="gemini", vision_model="model-b", limit=None, dry_run=False, yes=True,
    )) == 0
    assert len(api_calls) == 3
    assert 'vision_model: "model-b"' in (c_a / "LLM_Knowledge" / "pdfs" / "Deck.md").read_text(encoding="utf-8")

    # Plain index preserves model-b cache with zero calls
    assert toledo_dl.cmd_index(argparse.Namespace(course="Course A")) == 0
    assert len(api_calls) == 3
    assert 'vision_model: "model-b"' in (c_a / "LLM_Knowledge" / "pdfs" / "Deck.md").read_text(encoding="utf-8")

    # Zero-pending gemini --dry-run must NOT call build_llm_index()
    idx_called = []
    orig_build = toledo_dl.build_llm_index
    monkeypatch.setattr(toledo_dl, "build_llm_index", lambda *a, **kw: idx_called.append(1) or 0)
    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Course A", vision_backend="gemini", vision_model="model-b", limit=None, dry_run=True, yes=False,
    )) == 0
    assert idx_called == []
    monkeypatch.setattr(toledo_dl, "build_llm_index", orig_build)

    # 4. Declining Gemini at sync Stage 5 (fallback_on_cancel=True) makes zero provider calls AND writes standard INDEX.md
    c_dec = isolate_toledo_env["out"] / "Decline Course"
    (c_dec / "PDFs").mkdir(parents=True)
    (c_dec / "PDFs" / "1_slides.pdf").write_bytes(b"%PDF-1.4\n/Type /Page\n")
    api_calls.clear()
    monkeypatch.setattr(toledo_dl.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p="": "n")
    assert toledo_dl.cmd_index(argparse.Namespace(
        course="Decline Course", vision_backend="gemini", vision_model=None,
        limit=None, dry_run=False, yes=False, fallback_on_cancel=True,
    )) == 0
    assert api_calls == []
    assert (c_dec / "INDEX.md").is_file()


def test_local_pdf_vision_one_click_setup_helper(isolate_toledo_env, monkeypatch):
    # Non-interactive without --yes never auto-installs missing dependencies
    monkeypatch.setattr(toledo_dl.shutil, "which", lambda _cmd: None)
    monkeypatch.setattr(toledo_dl, "_ollama_list_models", lambda: (False, set()))
    assert toledo_dl._ensure_local_pdf_vision_setup("qwen2.5vl:7b", interactive=False, auto_install=False) is False

    # Interactive approval installs poppler + ollama + pulls model and returns True once ready
    state = {"poppler": False, "ollama": False, "running": False, "model": False}
    actions = []

    def fake_which(cmd):
        if cmd == "pdftoppm":
            return "/usr/bin/pdftoppm" if state["poppler"] else None
        if cmd == "ollama":
            return "/usr/local/bin/ollama" if state["ollama"] else None
        return None

    def fake_list_models():
        return (state["running"], {"qwen2.5vl:7b"} if state["model"] else set())

    def fake_install_pkg(kind):
        actions.append(f"install:{kind}")
        state[kind] = True
        return True

    def fake_popen(cmd, **_kw):
        actions.append("serve")
        state["running"] = True
        return types.SimpleNamespace(poll=lambda: None)

    def fake_run(cmd, **_kw):
        actions.append(f"run:{' '.join(cmd)}")
        if "pull" in cmd:
            state["model"] = True
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(toledo_dl.shutil, "which", fake_which)
    monkeypatch.setattr(toledo_dl, "_ollama_list_models", fake_list_models)
    monkeypatch.setattr(toledo_dl, "_install_local_vision_package", fake_install_pkg)
    monkeypatch.setattr(toledo_dl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(toledo_dl.subprocess, "run", fake_run)
    monkeypatch.setattr("builtins.input", lambda _p="": "y")

    assert toledo_dl._ensure_local_pdf_vision_setup("qwen2.5vl:7b", interactive=True, auto_install=False) is True
    assert actions == ["install:poppler", "install:ollama", "serve", "run:/usr/local/bin/ollama pull qwen2.5vl:7b"]


def test_explicit_chapter_matching_and_human_friendly_transcripts_slides(isolate_toledo_env):
    # 1. Stem extraction accuracy
    assert toledo_dl._explicit_chapters_in_stem("Lecture 01 - 2. Univariate Distributions and Random Variables (part 2)") == {2}
    assert toledo_dl._explicit_chapters_in_stem("Lecture 03 - 2. Univariate Distributions. 3. Multivariate Vectors") == {2, 3}
    assert toledo_dl._explicit_chapters_in_stem("Lecture 05 - 0. Introduction. 1. Probability") == {0, 1}
    assert toledo_dl._explicit_chapters_in_stem("Lecture 08 - 5 Sample statistics") == {5}
    assert toledo_dl._explicit_chapters_in_stem("Lecture 09 - instructions intermediate test") == set()

    # 2. Match with explicit chapters overriding floor from earlier lectures
    course_dir = isolate_toledo_env["out"] / "Explicit Course"
    lectures = course_dir / "Lectures"
    pdfs = course_dir / "PDFs"
    lectures.mkdir(parents=True)
    pdfs.mkdir(parents=True)

    intro_pdf = pdfs / "0-intro.pdf"
    proba_pdf = pdfs / "1-proba.pdf"
    rvar_pdf = pdfs / "2-rvar.pdf"
    rvec_pdf = pdfs / "3-rvec.pdf"
    for p in (intro_pdf, proba_pdf, rvar_pdf, rvec_pdf):
        p.write_bytes(f"%PDF-1.4 {p.name}".encode("utf-8"))

    # Lectures 1-4 established floor at Chapter 3
    mapping = {
        "Lectures/Lecture 01 - 2. Univariate.txt": {"matched_pdfs": [rvar_pdf.name], "confidence": "high"},
        "Lectures/Lecture 02 - 3. Vectors.txt": {"matched_pdfs": [rvec_pdf.name], "confidence": "high"},
        "Lectures/Lecture 03 - 3. Vectors.txt": {"matched_pdfs": [rvec_pdf.name], "confidence": "high"},
        "Lectures/Lecture 04 - 3. Vectors.txt": {"matched_pdfs": [rvec_pdf.name], "confidence": "high"},
    }
    for rel in mapping:
        f = course_dir / rel
        f.write_text("Lecture content " * 10, encoding="utf-8")
        pair = course_dir / "Transcripts & Slides" / f.stem
        pair.mkdir(parents=True, exist_ok=True)
    (course_dir / ".pdf_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")

    # Lecture 05 explicitly states Chapter 0 & 1
    lec05 = lectures / "Lecture 05 - 0. Introduction. 1. Probability.txt"
    lec05.write_text("Today we introduce probability and course foundations. " * 10, encoding="utf-8")

    entry = toledo_dl.match_transcript_with_pdfs(lec05, course_dir, backend="auto")
    assert entry is not None
    assert sorted(entry["matched_pdfs"]) == ["0-intro.pdf", "1-proba.pdf"]

    # 3. Chapter bundle populates human-friendly Chapter folders in Transcripts & Slides
    kb_dir = course_dir / "LLM_Knowledge"
    toledo_dl._repair_and_build_chapter_bundles(course_dir, kb_dir)

    ts_dir = course_dir / "Transcripts & Slides"
    ts_folders = {p.name for p in ts_dir.iterdir() if p.is_dir()}
    assert any("proba" in name.lower() or "01_" in name for name in ts_folders)

    proba_chap = next(p for p in ts_dir.iterdir() if p.is_dir() and "proba" in p.name.lower())
    assert (proba_chap / proba_pdf.name).is_file()
    assert (proba_chap / lec05.name).is_file()


def test_collaborate_tool_detection_supports_ultra_identifier_shape():
    assert toledo_dl._is_collab_tool({
        "identifier": "bb-collab-ultra",
        "isVisible": True,
        "toolEnabledSetting": {"isAvailable": True},
        "applicationLabelAsMessage": {"handle": "collab-ultra"},
    }) is True
    assert toledo_dl._is_collab_tool({
        "identifier": "bb-collab-ultra",
        "isVisible": False,
        "toolEnabledSetting": {"isAvailable": True},
    }) is False
    assert toledo_dl._is_collab_tool({"toolId": "collabultra"}) is True
    assert toledo_dl._is_collab_tool({"toolId": None, "identifier": "announcements"}) is False


