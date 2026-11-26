# Codex audit handoff

## Latest audit for Antigravity — PDF Vision Sign-off (`e228546` + test portability fix)

**Status: RELEASE_READY.** 0 open High or Medium findings in `toledo_dl.py`. Codex verified the authoritative `pypdf`/`pdfinfo` page count closes the 21-page hex-escaped `/Type /Pa#67e` case with zero provider calls, and `test_pdf_vision_audit_regressions` now mocks `_verified_pdf_page_count` for step 3 and explicitly tests the missing-`pdfinfo` + missing-`pypdf` fail-closed path so the offline suite is 100% portable without `pdfinfo` on `PATH` (**69 passed**, `py_compile` & `ruff check .` clean).

## Previous audit for Antigravity — PDF Vision Round 3 audit on `b543a49`

**Status: CHANGES_REQUESTED.** Round 3 of 3 (circuit breaker reached — handing back to user). Six of the seven Round 1 findings and the compressed `/ObjStm` case are closed. One remaining edge case:

1. **Medium — The Gemini Vision fallback still bypasses the 20-page limit when `pypdf` is disabled.** A valid 21-page PDF using a hex-escaped PDF name (`/Type /Pa#67e`) alongside 20 literal `/Type /Page` entries passes the regex page count as 20 (`toledo_dl.py:5048`). Fix by requiring `pypdf` (or `pdfinfo`) for a verified page count instead of regex estimation.

## Previous audit for Antigravity — PDF Vision Round 1 audit on `34723fd`

**Status: CHANGES_REQUESTED.** Seven findings identified:
1. **High — Local Ollama vision can send rendered slides to an HTTP proxy.** Bypass proxy env vars (`ProxyHandler({})`) so localhost Ollama requests stay on loopback.
2. **Medium — Gemini caches truncated decks as complete.** Reject `finishReason == "MAX_TOKENS"` so truncated responses are never cached as `vision_ok`.
3. **Medium — Gemini can bypass the 20-page chunk bound.** Fail closed when `pypdf` is missing or slicing fails on >20-page PDFs.
4. **Medium — Vision planning leaves eligible PDFs unscanned.** Match `.PDF` case-insensitively and deduplicate SHA-256 per course rather than across courses.
5. **Medium — Explicit `--vision-model` change silently reuses old model's cache.** Check `vision_model` when an explicit vision backend is requested while preserving cache reuse for plain `index`.
6. **Medium — Gemini `--dry-run` with zero pending PDFs can rewrite the index.** Return early on `dry_run` before `build_llm_index()`.
7. **Medium — Declining Gemini at sync Stage 5 skips the standard index.** Fall back to standard text indexing when declining cloud PDF vision during `sync`.

## Previous handoff — Local GPU & Cloud PDF Vision (`34723fd`)

**Status: READY_FOR_AUDIT.** Added opt-in visual PDF scanning (local GPU Ollama vision and cloud Gemini Vision) and an interactive `toledo-dl sync` prompt:

- **Interactive `sync` prompt & prefs:** `_prompt_pdf_vision_backend()`, `_load_sync_preferences()`, `_save_sync_preferences()`, and Stage 5 wiring in `cmd_sync()`. Recommends visual scanning for diagram/image-heavy PDFs, advises against it for text-heavy PDFs, offers standard text `[1]`, local GPU Ollama `[2]`, or cloud Gemini API `[3]` (with cost note), and keeps non-interactive scheduled runs from auto-uploading cloud PDFs.
- **Local GPU backend (`ollama`):** `_render_pdf_pages_to_pngs()`, `_call_ollama_pdf_vision_page()`, `_extract_pdf_via_ollama_vision()` (graceful fallback to `pypdf` if `pdftoppm`/Ollama is unavailable).
- **Cloud backend (`gemini`):** `_slice_pdf_page_chunks()` (20-page chunks), `_call_gemini_pdf_vision_chunk()` (`x-goog-api-key` header auth), `_extract_pdf_via_gemini_vision()` (strict `CloudApproval(feature="pdf-vision", provider="gemini")` + cost preview + `--dry-run`/`--limit`/`--yes`), and SHA-256 vision caching (`_read_cached_vision_markdown()`) preserved across plain `index` runs.
- **Offline tests (`68 passed`, `py_compile`, `ruff check .` clean):** `test_pdf_vision_interactive_prompt_and_preferences`, `test_pdf_vision_cloud_gemini_consent_cache_and_preservation`, and `test_pdf_vision_local_ollama_gpu_and_graceful_fallback` in `tests/test_pipeline.py`.

## Previous audit for Antigravity — Final sign-off on `5b540f5`

**Status: RELEASE_READY.** 0 open High or Medium findings. All prior audit findings (symlink escapes across `match-slides`/`sync`/`index`/`search`, `ffmpeg`-missing split fail-closed, local retry atomic commit recovery, strict dispatch-time duration validation, pending-transcript exclusion from passages/chapter bundles, output symlink overwrite guards, and preserved chapter directory on blocked symlink migration) are verified closed with offline regression coverage.

## Previous audit for Antigravity — Round 3 audit on `0b3cdf3`

**Status: CHANGES_REQUESTED.** Round 3 of 3 (circuit breaker reached — handing back to user).

1. **High — Indexing can still overwrite files outside the course.** Symlinking `Course/LLM_Knowledge/transcripts` to an external directory with an older `Lecture_01.md` lets `toledo-dl index --course Course` overwrite it via direct Markdown write (`toledo_dl.py:4938`). Add an offline sentinel regression for generated outputs beneath symlinked directories.
2. **Medium — Stale passages remain searchable through a symlink.** If `Course/LLM_Knowledge/passages.jsonl` is a symlink to an external JSONL file and the course has only a pending transcript, reindexing preserves the symlink and `search_passages()` still follows it (`toledo_dl.py:5136`, `toledo_dl.py:6084`). Add a symlink case asserting zero hits and preserving the target.
3. **Medium — Chapter bundles can publish pending transcripts.** In a course without a syllabus manifest, a pre-existing `Transcripts & Slides` pairing containing a PDF and a symlink to a pending transcript resolves the TXT without checking the in-progress marker and includes its text in `CHAPTER_BUNDLE.md` (`toledo_dl.py:4603`, `toledo_dl.py:4813`). Extend the failed-commit regression to include an existing pairing.
4. **Medium — The announcement overwrite regression never exercises the guard.** The mock route registers `limit=100` while `sync_announcements()` requests `limit=50` (`tests/test_pipeline.py:1745`, `toledo_dl.py:1439`), returning 404 before reaching the write guard. Fix the route and assert the mocked announcement was processed.

## Previous audit for Antigravity — Round 2 audit on `0d0a38d`

**Status: CHANGES_REQUESTED.** Commit `0d0a38d` closed both Round 1 reproductions. Two related cases remained:

1. **High — Index and announcement sync can overwrite files outside the library through output symlinks.** A symlinked `Course/INDEX.md` or `Course/Announcements.md` inside a real `OUT/Course` directory followed the symlink and overwrote the external sentinel file. Guard output writes against symlinks escaping the course root and add offline regressions.
2. **Medium — Reindexing leaves previously published pending text searchable when no passages remain.** Reindexing a course whose only source is now a pending `.txt` skipped rewriting `passages.jsonl`, leaving stale passages searchable. Prune stale `passages.jsonl` when `course_passages` is empty and extend the failed-commit regression with a pre-existing passage.

## Previous audit for Antigravity — Round 1 audit on `64949f2`

**Status: CHANGES_REQUESTED.** Review confirmed the four targeted fixes in `64949f2`. Two remaining findings were identified:

1. **High — Sync and index still follow course symlinks outside the library.** Pointing `OUT/Course` at a directory outside `OUT` allowed `toledo-dl index --course Course` to write `INDEX.md` externally and `sync_files()` to write into the external directory because it validated against the symlinked course root. Add offline sync and index regressions asserting the external directory stays untouched.
2. **Medium — Index publishes a pending transcript as a generic document.** After a failed SRT commit leaves `.txt` + `.transcript.inprogress`, the generic document branch in `build_llm_index()` still wrote its contents into `passages.jsonl` as a `document_section`. Extend the failed-commit regression to assert the index omits the pending TXT entirely.

## Previous audit for Antigravity — Post-audit fixes on `304069d`

**Status: CHANGES_REQUESTED.** Two High and two Medium findings remain. The 54 offline tests, Ruff, Python compilation, and `git diff --check` passed on the reviewed snapshot. The findings are from code inspection. Add focused offline regressions with synthetic files and mocked providers before closing them.

1. **High — Top-level course symlinks escape the lecture library.** `cmd_match_slides()` accepts a directory symlink under `OUT`. Later checks treat its resolved destination as the course root, allowing external transcripts, PDFs, and videos into the job plan. Validate the course directory against `OUT` before planning. Test a symlinked course root and assert zero provider calls.
2. **High — A split retry can upload the original recording bytes.** If `ffmpeg` is unavailable after initial chunking, `_extract_audio_subchunk()` reads the entire source. A small MP4 can therefore be sent under approval for mono MP3 audio chunks. Fail closed if extraction is unavailable. Mock the extraction helper in the split test and verify that a missing `ffmpeg` stops before a retry request.
3. **Medium — A successful local retry stays marked incomplete.** `transcribe_file()` rewrites both outputs after a failed Gemini commit but leaves the in-progress marker. Subsequent transcription runs queue the recording again and slide matching skips it. Use the shared atomic transcript-pair commit path for local output. Test recovery from a failed cloud commit through local transcription and normal discovery.
4. **Medium — A failed duration recheck skips approval validation.** If the dispatch-time probe returns `None`, `_is_cloud_approved()` skips the duration limits. Chunking probes independently, so a later successful probe can proceed without checking that result against the approval. Require a valid duration when limits apply and use the validated duration for chunking. Test a failed dispatch probe followed by a successful over-cap probe and assert zero provider calls.

## Previous audit for Antigravity — Gemini transcription implementation

**Prior findings verified.** Both earlier slide-matching consent regressions pass. The CLI uses a fixed job list, and automatic matching lacks the approval required for a cloud request. All 49 offline tests passed. The findings below were independently reproduced with temporary files and mocked providers. No real uploads were made.

1. **High — A failed transcript write becomes a completed recording.** In `_commit_transcript_pair()`, simulate a full disk during the SRT write. TXT is already present, SRT and the done marker are absent, and `finally` removes the in-progress marker. Both `_is_transcript_complete()` and `_is_transcript_txt()` then accept the lone TXT as legacy, so retries skip it and indexing can publish it. If the process stops and the marker does survive, `_course_match_transcripts()` can still pick the partial TXT through its numbered-lecture fallback. Retain pending state on failed commits, make each reader honor it, and test snapshot recovery after each individual write. Check that renaming recordings carries the completion metadata along.
2. **High — Course-scoped transcription can select files outside the course.** A lecture-named audio symlink inside a selected course pointing to a synthetic MP3 outside the archive was accepted and dispatched to the mocked Gemini worker. The preview displayed the link's course-local name, while approval stored its resolved external target. Resolve and validate candidates inside the selected course before preview and again before dispatch. Apply the same scope to transcript and sibling MP4 discovery for cloud matching. Add an offline regression asserting zero provider calls for a rejected source.
3. **Medium — The duration cap accepts recordings with unknown length.** With a failed duration probe and a one-minute cap, the plan selected a recording and reported zero seconds. The worker probes it separately. A later successful probe or replacement media could exceed the approved cap. Defer unknown-duration sources and revalidate exact source identity and duration before upload. Apply the same principle to matching. The approval records the model and data types but the shared validator currently only checks feature, provider, path, and optionally the video flag.
4. **Medium — Explicit CPU mode still runs CUDA.** With CUDA detected and `--backend cpu`, the mocked Whisper instance was initialized on CUDA. The flag only enables a CPU fallback. Pass an explicit device through to the transcription function and test both forced CPU and forced GPU.
5. **Medium — Cloud subtitle segments can cross chunk boundaries.** A segment ending at 902 seconds in a 900-second chunk was cleaned to 900.5 seconds, which can overlap the next chunk. Enforce finite, within-chunk and globally monotonic times. On `MAX_TOKENS`, the error suggests shorter chunks, but the CLI has no chunk-length option and retry uses the same chunk size. Give users a bounded split-and-resume path or a documented smaller-chunk flag.

Keep Gemini first with one key and separate approvals. Before release, update the README and plan to show the actual cloud transcription command, `--backend cpu`, the duration cap, and the revised defaults. The current README still describes cloud transcription as a manual upload. Test a real long lecture only after the offline release blockers are fixed and the user approves that upload.

## Earlier audit for Antigravity — Cloud plan and upload confirmation

Reviewed `CLOUD_AND_MATCHING_PLAN.md`, the latest cloud opt-in entry in `CHANGES.md`, and the matching code. All 46 offline tests passed. The two cases below were reproduced with synthetic files, a fake key, and a mocked provider. No cloud calls were made. Resolve both before the student release.

1. **High — A stale match bypasses CLI confirmation.** The preview at `_count_pending_match_lectures()` skips any recorded pairing with a folder. The matcher also checks whether the cached PDF still satisfies lecture chronology. With Chapters 8, 8, and 2 cached across Lectures 26–28, the cloud preview reports zero pending. A non-interactive run with no `--yes` then makes one mocked upload for Lecture 28 and exits successfully. Build one explicit job list for the preview, limit, confirmation, and execution. Test that zero reported work means zero upload calls. If an earlier match changes the chapter floor while executing, any additional work must wait for approval or stay local.
2. **High — Automatic callers bypass the CLI upload guard.** `transcribe_file()` calls `match_transcript_with_pdfs()` and `cmd_sync()` calls `update_course_pdf_matches()` with default `auto`. Setting `TOLEDO_MATCH_BACKEND=gemini` routes these paths to a provider without going through `cmd_match_slides()`, where the confirmation lives. Reproduced one mocked provider call using the same bulk helper that sync uses. Bind approval to the provider, inputs, and data types, and enforce it at the shared dispatch point. Automatic callers should pass `local` unless that specific run has an approved cloud matching job. Test direct transcription, manual sync, and scheduled sync with both provider keys and a cloud preference set.

### Decisions for the transcription plan

- **Build Gemini first.** The user chose Gemini as the default provider after a student opts into a cloud feature. Reuse one `GEMINI_API_KEY` for lecture transcription and visual slide matching, with separate upload approval for each feature. Fresh installs, default sync, and explicit local mode stay local regardless of which API keys are present. Keep manual CPU transcription available. Verify Gemini on a full technical lecture for missing speech, truncation, and subtitle timing before recommending it for a large batch. Preview audio hours and estimated usage. Check the applicable provider plan and terms before advertising free credits or free use. [Gemini audio](https://ai.google.dev/gemini-api/docs/audio) · [Gemini terms](https://ai.google.dev/gemini-api/terms)
- **Keep other providers optional.** Retain Groq or OpenAI as future transcription alternatives, and offer Claude or other providers only for the tasks each integration actually supports. Ship the one-key Gemini flow first. A key or successful doctor check grants no upload approval.
- **Use a versioned cache and explicit completion marker.** Include normalized language, model, provider and endpoint identity, format/settings, and the chunk boundary strategy or exact segment boundaries. Key recordings by content. Protect the cache with OS-appropriate permissions. A restart can reuse chunks already persisted, but the design cannot guarantee zero repeat charges after a request succeeds remotely and the process dies before saving it locally. Prune completed chunk data after the final transcript pair is committed and recoverable. Separate file renames are insufficient to make two output files collectively atomic. Have indexing honor the completion marker for new jobs while preserving support for legacy transcripts. The current transcription queue already treats a lone `.txt` as complete, which needs a regression test.
- **Cap the actual cloud workload.** Add `--limit` to both cloud commands and enforce a configurable audio-duration cap in addition to any warning. Apply the limit before showing and approving the job list. `sync --limit` currently limits recording downloads, and the later transcription step can still scan the whole backlog. Use a separate, explicit transcription cap if cloud work is ever allowed from sync.
- **Clarify current versus planned behavior.** Today the standard sync wrapper defers CPU work, while direct `toledo-dl sync` and `transcribe` can still use the CPU. Keep the existing manual CPU workflow usable and document any default change. Cloud vision is a promising matching mode. Its accuracy over local methods, split-screen readability, and slide-to-timestamp mapping still need evaluation. Ollama is a local backend that can run on CPU or GPU, and the current matching route sends it text.

## Earlier handoff for Antigravity — Cloud transcription and visual slide matching

**Finding.** The visual matcher is already in the working tree. In its current `auto` mode, the presence of a Gemini or OpenAI API key can select a cloud backend. The code can then send transcript excerpts, PDF outlines, and sampled frames from the lecture. Adding a key for transcription would therefore also change where slide matching runs. The proposed cloud transcription backends were still pending at the time of the review. All 45 existing offline tests passed.

### Implementation priorities

1. **Make cloud transcription and cloud slide matching separate opt-ins.** Treat API keys as credentials. Keep default sync and matching local, even when provider keys are present. Default transcription should use local CUDA when available and report pending work on CPU-only scheduled syncs. Keep manual CPU transcription available. An explicit `--backend local` should override a cloud environment preference. For a cloud job, tell students which provider is selected, what data it will receive, and the recording count and audio duration before sending. Require an explicit preference before scheduled sync can upload. In `doctor`, distinguish a detected key from a connection that has actually been verified.
2. **Ship one cloud transcription backend first.** Groq and OpenAI document structured segment timestamps and a 25 MB direct upload or attachment limit. Gemini documents timestamped structured transcription. If starting with Gemini, evaluate missed speech, timestamp accuracy, and truncation on a long lecture before recommending it. Check current provider terms and billing before advertising any free option for Belgium. Google's terms include a paid-services requirement when making API clients available to users in the EEA, Switzerland, or the UK. [Groq speech-to-text](https://console.groq.com/docs/speech-to-text) · [OpenAI transcription](https://developers.openai.com/api/docs/guides/speech-to-text) · [Gemini audio](https://ai.google.dev/gemini-api/docs/audio) · [Gemini terms](https://ai.google.dev/gemini-api/terms)
3. **Resume interrupted recordings safely.** Retain successfully processed chunks privately and key resumable work to the recording, backend, and chunk settings. Check upload size, output truncation, timestamps, and chunk boundaries. Publish the final UTF-8 `.txt` and `.srt` as a completed pair so indexing sees only finished transcripts. Keep the recording pending on failure. Resume only the missing chunks, and keep API keys in environment variables and request headers with redaction in logs.
4. **Use MP4 to connect PDF pages to lecture timestamps.** Start from the current six-frame sample to narrow down the likely deck. Sample more densely around an ambiguous transition to find when the lecturer switched slides. Keep chronology and distinguish a brief recap from sustained teaching of another chapter. Test split-screen, short transitions, and audio-only fallbacks. Timestamped screenshots can also help students recover handwritten annotations or code shown on screen.

**Acceptance checks.** Mock both providers and confirm that populated keys alone trigger no upload, explicit local mode wins, changing the transcription provider never enables cloud frame or transcript uploads for matching, and an interrupted job leaves no incomplete transcript in the index. Test that a resumed job processes only missing chunks, preserves original recordings, and produces the same subtitle times as a complete run. Use fixtures for visual matching. Keep the real student archive read-only.

## Earlier handoff for Antigravity — Lecture-to-slide chronology

**Confirmed in Multivariate Statistics.** `Lecture 28 - Bootstrap Aggregating (Bagging) Techniques` was linked to two copies of the Chapter 2 PCA PDF. The nearby classification lectures already pointed to later chapters. The read-only check against the real archive inferred Chapter 8 as the earliest plausible chapter and found the Bagging section in `8_classification_other_methods.pdf`.

**Implemented in the workspace.** The matcher requires two distinct lecture numbers among the preceding four in the same recording directory to agree on a numbered chapter before it filters out older numbered PDFs. The current chapter stays eligible across consecutive lectures. Reviews, recaps, revisions, and exercises can refer back to earlier chapters. When the sequence is sparse or unavailable, topic-based matching remains available. The matcher rechecks cached pairings that fall behind the inferred chapter, ignores generated study-bundle PDFs as candidates, and removes stale generated links or copies when their source can be verified.

**Review priority.** Check courses that restart chapter numbering within the same recording directory, sessions that cross a chapter boundary, and lectures that revisit earlier material without an explicit review label. Treat the inferred order as a heuristic. Keep source files and unrelated notes intact.

**Verification.** All 43 offline tests passed, including stale cache repair, an explicit review that can still match PCA, duplicate PDF handling, and preservation of originals and notes. The actual user archive was checked read-only. The installed copy and existing pairings still need the update before testing the repair on disk. After installing, run `toledo-dl match-slides --course "Multivariate statistics"` and then `toledo-dl index --course "Multivariate statistics"`.

## Earlier request for Antigravity — One Anki subdeck per official chapter PDF

The course command should follow the syllabus. Each official chapter PDF within the material covered in class defines one numbered Anki subdeck under the course deck. Its sources are the canonical PDF and the lecture transcripts that teach that chapter. A four-hour class might be recorded as four one-hour files, and a chapter might span several classes. Determine transcript membership from the course's source links or a confirmed course mapping, without assuming a fixed number of recordings per chapter.

The installed command currently discovers every indexed chapter folder, sorts by folder name, and uses that folder's transcripts. A dry run for Multivariate Statistics found 80 candidate folders, including later chapters, duplicate PDFs, exercises, supplements, and the temporary `Lecture_05_PCA` folder. Chapter 10 appeared before Chapter 1, and the main PCA folder included Lecture 03.

### Implementation

1. Resolve the ordered official chapters and their sources before either flashcard pass. Use the syllabus chapter number and title for deck names and order. Deduplicate copies of the same official PDF. Keep exercises, supplements, and temporary folders from creating extra decks in this mode.
2. Support a course-specific last-covered chapter so the ordinary course command processes the intended range. The presence of a later PDF on disk cannot establish that the class covered it. Use existing course metadata where reliable and allow a persistent mapping or correction when the official PDFs, cutoff, or transcript links are ambiguous. List unresolved links and missing transcripts in the preview or report. Allow a transcript span to be linked to each relevant chapter if a recording crosses a boundary.
3. Use the same resolved chapter set and order for generation, per-chapter exports, and the combined course TSV. A previously cached duplicate, temporary deck, or out-of-range chapter must be excluded from the combined TSV in this mode. Keep the source catalog, original files, existing TSVs, and existing CLI and cache compatibility. Invalidate a chapter's cached generation when its source membership changes. Extend `--dry-run` to show the selected chapter, canonical PDF, and linked lectures before model calls.

### Acceptance

- For Multivariate Statistics with the cutoff set to Chapter 7, `toledo flashcards "Multivariate Statistics"` targets exactly seven subdecks in syllabus order, from `01 Basic Concepts` through `07 Classification — Discriminant Analysis`. Chapter 1 uses PDF 1 and Lectures 01–04. Chapter 2 uses PDF 2 and exactly Lectures 05–08. Verify that both passes receive the correct source set and that the combined TSV contains exactly the seven intended deck names when all seven chapters generate cards. Include duplicate PDFs, later chapters, exercises, supplemental PDFs, and an already cached `Lecture_05_PCA` folder in the fixture.
- Use a Fundamental Concepts of Statistics fixture with five selected official chapter PDFs, an extra PDF outside the selected set, and a different number of transcript files per chapter. Verify five subdecks in chapter order. The real course's official PDFs and transcript links still need confirmation.
- Verify the preview and exports offline with mocked model calls. Include a missing or ambiguous transcript link and a changed chapter mapping on a cached rerun.

## Earlier audit findings

Reviewed `overnight-hardening` at `e624a52`, including the Stage 2 redesign in `fa1fa94`. Line references are relative to the reviewed revision. The reproductions used temporary files and mocked services. All findings are open.

## 1. Incomplete generations are cached as complete

**High.** See `flashcards.py:1064`, `:1521`, `:2011`, `:2063`, `:2091`, and `:2151`.

1. Create a chapter with a nonempty transcript. Mock Gemini to raise `GeminiBlocked` and run flashcards. It exits successfully and writes `flashcards.json` as `[]` with cache reason `blocked`.
2. Run again with the same inputs and arguments. It exits successfully and makes zero model calls.
3. For the Stage 2 variant, supply concepts `c01` and `c02`. Mock a card with ungrounded evidence for `c01` and an empty recovery response for `c02`. The run saves the partial card set with cache reason `ok`. The next run makes zero model calls and changes both the missing-concept and ungrounded-card counts in `report.md` from one to zero.

## 2. Reordering cards swaps existing Anki GUIDs

**High.** See `flashcards.py:1826`, `:1998`, and `:2089`.

1. Generate two cards for `c01` with fronts `Card A` and `Card B`. Record the front-to-GUID mapping in `flashcards.json`.
2. Run `--stage2-only` with a mocked response returning the same two cards in reverse order. Compare the mappings. The GUID previously assigned to `Card A` now identifies `Card B`, and vice versa. The lookup uses concept ID and ordinal.

## 3. Distinct recordings can share one downloaded file

**High.** See `toledo_dl.py:313`, `:3044`, and `:3077`.

Mock discovery to return two distinct Kaltura entry IDs titled `Recording 1` in the same course. Run sync in a fresh temporary folder with the downloader mocked to write the requested file. Sync exits successfully, invokes the downloader once, and records both IDs against `Lecture 01.mp4`.

## 4. Chapter indexing merges different PDFs and duplicates folders after reordering

**High.** See `toledo_dl.py:2775`, `:2782`, and `:2795`, plus `flashcards.py:1918`.

Put different files named `slides.pdf` in two separate `Transcripts & Slides` lecture folders, each with its own transcript. Run `_repair_and_build_chapter_bundles()`. Only one chapter is created, with the first PDF and both transcripts. Add a PDF named `A.pdf` to a lecture folder and rebuild. The builder creates `02_slides` and leaves `01_slides` in place. Both are eligible for flashcard generation.

## 5. Lecture rename can overwrite existing sidecars

**High.** See `toledo_dl.py:1589` and `:1601`.

Create `Lecture 01 - Session (09-23).mp3` and its `.vtt` sidecar, plus an unrelated `Lecture 01 - Regression.vtt`. Mock the topic as `Regression` and call `_maybe_rename_vague_lecture()`. On Linux, the existing target subtitle is overwritten. The collision check omits both `.vtt` and `.md`.

## 6. Empty cache rebuild preserves stale cards in the exports

**Medium.** See `flashcards.py:1985`, `:2136`, and `:2199`.

Generate a chapter TSV and a course `_ALL.tsv` for a one-chapter course. Replace its `flashcards.json` with `[]` and run `--rebuild-tsv`. The command exits successfully and both exports still contain their old cards byte for byte.

## 7. Field markers inside fenced code truncate answers

**Medium.** See `flashcards.py:1122` and `:1161`.

Pass `_parse_card_blocks()` a card whose `BACK` contains a fenced YAML block with unindented lines `TAGS: internal` and `key: value`. The answer ends at the opening fence and the YAML moves into tags.

## 8. TSV writer and validator disagree on field boundaries

**Medium.** See `flashcards.py:1583`, `:1616`, `:1631`, `:1661`, and `:1720`.

Write a row through `_write_anki_tsv()` with a front that contains a literal tab inside inline backticks. The writer restores that tab inside the code span and quarantines the row for having six columns. In a separate run, use a front containing U+2028 between two letters. The writer accepts the row, then `validate_anki_tsv()` rejects the export after splitting the front across two lines.

## 9. Filename cleanup leaves Windows reserved device names intact

**Medium.** See `toledo_dl.py:338` and `:1079`.

Call `clean_filename()` with `CON.pdf`, `NUL.txt`, and `AUX.zip`. All three pass through unchanged. On Linux, `PureWindowsPath.is_reserved()` flags each result. `sync_files()` uses the cleaned name for the destination.

## Follow-up review of the unified CLI and Gemini model changes

Reviewed the uncommitted changes on `overnight-hardening` against `e624a52`. The original audit items remain open. The working-tree line numbers below refer to the unified CLI changes.

- **Interactive selection can generate cards for additional courses.** Create `Applied Statistics` and `Statistics` with one chapter each, run `toledo flashcards --dry-run` in a TTY, and select `Statistics` by number. Both courses appear in the dry-run output. The wrapper passes the selected name through the substring filter. See `toledo_dl.py:3622`, `:3635`, and `flashcards.py:1907`.
- **Sync ignores flashcard failure.** Mock `flashcards.main()` to return `1` and run `toledo-dl sync --lectures-only --no-transcribe --flashcards` with a mocked successful sync. Sync exits `0` and prints its completion banner. See `toledo_dl.py:3171` and `:3186`.
- **The negative sync flag does not cancel the positive one.** Run the same mocked sync with `--flashcards --no-flashcards`. Flashcard generation still runs once. A bare sync, a sync with only `--no-flashcards`, and a dry run with `--flashcards` each make zero flashcard calls. See `toledo_dl.py:3164` and `:3708`.
- **An unmatched course filter exits silently.** Run `toledo flashcards 'Course That Does Not Exist' --dry-run` with an existing course directory. It prints the mode and exits `0` without indicating that no course matched. See `toledo_dl.py:3600` and `flashcards.py:1907`.
- **The model default change invalidates existing auto-model caches.** Seed a chapter cache with the `gemini-3-flash-preview` source/model hash. Running with the old model explicitly reuses the cards. Running with the new default, with source files unchanged, calls Stage 1 again. The default now routes through `gemini-2.5-pro`, `gemini-3-flash-preview`, and `gemini-flash-latest` in that order. See `flashcards.py:286`, `:730`, `:1892`, and `:2027`.

Offline verification passed with 60 tests, Python compilation, Ruff using the cached binary, and Bash syntax checks for the launcher and both Unix installers. Live Gemini and installation were outside this verification. At review time `bin/toledo` was executable and untracked, so a checkpoint must include it explicitly. `git diff --check` flagged trailing blank lines in `CHANGES.md` and `tests/test_cli.py`.

## AI-ready course storage and flashcard retrieval

The current pipeline preserves originals and builds a separate Markdown view, chapter bundles, and a JSONL file catalog. Flashcards read chapter folders directly and use a two-pass concept checklist and card generation process. Keep those entry points and outputs while improving what study tools can retrieve and cite.

### Verified gaps

- **Source paths can point to the wrong content.** Index `Module A/Lecture 01.txt` and `Module B/Lecture 01.txt`, each beside a recording. With the inputs older than the generated Markdown, the first transcript takes the shared output path and both manifest rows point to it. PDF Markdown has the same stem-based naming pattern. Chapter grouping also keys on PDF basename, as reproduced in issue 4. See `toledo_dl.py:2775–2797` and `:2843–2880`.
- **Standalone material has limited coverage.** Index an unpaired PDF and a Word handout, Stata script, and CSV. The PDF appears in the catalog without a flashcard chapter, and the other three appear as paths only. Chapter folders currently depend on `Transcripts & Slides` pairings. See `toledo_dl.py:2733–2737`, `:2870–2892`, and `flashcards.py:1914–1918`.
- **A filtered rebuild removes other courses from the global catalog.** Index two courses, then run `index --course` for just one. The resulting `llm_manifest.jsonl` only contains the selected course. Whisper also writes timestamps to `.srt`, while the index uses untimestamped `.txt` content. See `toledo_dl.py:1720–1727`, `:2843–2868`, and `:2941–2967`.
- **Stage 2 can lose an exact transcript match and duplicate its context.** Put the anchor after character 8,000 in a single long transcript paragraph and call `_match_transcript_excerpt()` with one batch. It finds the paragraph, exceeds the character budget, and falls back to a window that omits the anchor. When both the raw transcript and its chapter bundle are available, the same transcript text can appear twice in the Stage 2 prompt. See `flashcards.py:1241–1275` and `:1364–1377`.
- **Grounding is checked against the batch, not the cited location.** Give the checker a citation to slide 3 of `A.pdf` and put the quote only on slide 7 of `B.pdf` in the same source batch. The checker returns `True`. A blank page with the same number in an unrelated PDF can also cause an indeterminate result. In a mocked Stage 2 run with a textless PDF page, default `text` mode sent a placeholder and no page attachment. Explicit `pages` mode sent the page. See `flashcards.py:1199–1222`, `:1364–1371`, and `:1420–1429`.

### Proposed implementation order

1. **Fix source identity and catalog coverage.** Derive source IDs from the course and relative source path. Key chapter grouping by resolved source identity and give chapter folders stable IDs independent of sort order. Preserve other courses in filtered rebuilds. Make standalone slides, transcripts, and supported handouts and exercises discoverable even when matching fails. Preserve original files and existing export compatibility.
2. **Add portable passage records.** Keep Markdown for reading and add JSONL records for PDF pages, document sections, and timestamped transcript segments. Store a stable passage ID, course, source path, page or time range, and text. Keep a pointer to the original page or media for diagrams, formulas, and tables, and record when extraction failed or a format is unsupported.
3. **Use those records for flashcards.** Retrieve focused passages per concept, center transcript excerpts on matched anchors, deduplicate prompt text, and pass relevant original pages when visual evidence is needed. Persist the source and passage IDs in each card's JSON. Verify quotes against the cited passage and keep missing-concept and unsupported-card counts visible across cached runs. Maintain compatibility with legacy caches and Anki TSV exports.

**Offline acceptance fixture.** Use a Multivariate Statistics chapter with two same-name lecture files in separate modules, a PDF slide about an interaction term, timestamped lecture subtitles, and a standalone Stata exercise. Verify that each source remains distinct, a filtered rebuild preserves the other courses, retrieval returns the correct slide and transcript timestamp, and a card citing the wrong slide fails grounding. Use temporary fixtures and mock model calls.

## Implementation brief for Antigravity — Flashcard coverage

**Goal.** Prevent silent omissions between synced course material and the final Anki export. Both slides and lecture transcripts count as study sources. Prioritize coverage work. Defer personalized difficulty and practice preferences.

**Confirmed failure.** An offline fixture named eight items in a transcript. The mocked first pass found four and the auditor returned no additions. Stage 2 exported one card per discovered ID and reported zero missing IDs. A separate empty Stage 1 response skipped the auditor entirely. Start with `flashcards.py` functions `_extract_concepts()`, `_audit_chunk_concepts()`, `_concepts_to_cards()`, and `_write_anki_tsv()`, plus chapter construction in `toledo_dl.py`.

### Build order

1. **Inventory study sources after sync.** Give every eligible PDF page and timestamped transcript segment a stable source reference. Tie references to the course and source-relative path so identical filenames stay distinct. Use the original page for visual content. Use the SRT timestamps when available and a text location for legacy transcripts without subtitles. Include unpaired sources and retain their course association. Record source units as reviewed, intentionally skipped with a reason, or unresolved, depending on extraction and audit results. The existing chapter view can organize these records. Keep the current 15-page windows as model context.
2. **Extract and audit required items before card generation.** Scan slides and transcripts independently, then merge overlapping findings while preserving their source references. Give each course-required item its own ID and source location. Preserve members and labels of examinable lists, tables, and diagrams individually. For explicit enumerations, compare the required names against the raw source or an independently extracted enumeration, using source totals where available. A four-item checklist alone cannot establish that a source list is complete. Make transcript-only exam traps, common confusions, exceptions, and application-changing examples eligible as distinct items. Audit empty chunks too. Carry forward blocked calls, unreadable sources, and ambiguous visual content as unresolved until reviewed or successfully retried.
3. **Generate cards and reconcile coverage against the actual export.** Give Stage 2 the required item IDs and the exact slide or transcript passages. Retain citations in card JSON. After deduplication and TSV validation, count an item only when a source-supported card for it survives into the export. A failed, quarantined, or unrelated card leaves a gap. Target retries at the missing item IDs and their source passages. Report source units reviewed or unresolved, explicit items expected and found, required items covered in the export, and the remaining gaps by name. Persist incompleteness across reruns so a cache hit cannot turn a failed or partial run into a complete one. A clean coverage report should reflect the reviewed evidence and distinguish explicit counts from model judgment about implicit concepts.

### Offline regression tests

- Use an explicit eight-item source list with a mocked first pass returning four. The audit must recover the other four or the final chapter report must name all four gaps. Repeat with an empty Stage 1 result and verify an audit or unresolved status. Use a generic second fixture from a table or labeled diagram to exercise the same behavior outside statistics.
- Put an exam trap only in a long lecture transcript, after the beginning and outside the matching slide's proportional transcript window. Verify that an independent transcript scan finds it, that Stage 2 receives the right excerpt, and that the exported card retains a source reference to the correct timestamp in its JSON. Include an unpaired transcript case.
- Start with all eight items in the inventory and make one card fail TSV validation. Verify exported coverage is seven of eight, the report names the missing item, and the same gap remains visible on a cached rerun. Exercise retry recovery without issuing new IDs for surviving cards.
- Mark a textless PDF page as unresolved when its original page cannot be inspected. Verify the report flags unknown coverage rather than treating the page as an empty, completed section.

Keep the existing CLI and six-header, five-column Anki TSV format compatible. Read legacy caches, preserve existing GUIDs for identifiable surviving cards, and follow the atomic-write and non-destructive export rules in `AGENTS.md`. Use temporary fixtures, mock model calls, and run the repository verification commands.

## Next patch for Antigravity — False coverage for explicit lists

**Reproduced after the coverage implementation.** In `flashcards.py`, `_extract_explicit_source_items()` detects all eight rows of a numbered distribution list. With a checklist of only `[{"concept": "Distribution"}]`, `_find_missing_explicit_items()` returns `[]`. The generic concept matches every row through the bidirectional substring test in `_concept_covers_item()` at line 1088. Token overlap and fuzzy similarity can also produce ambiguous matches. A report can therefore imply that every named member was covered.

**Implement this first.** Resolve each distinct explicit member to a checklist entry that identifies that member specifically. A generic or merged concept cannot satisfy multiple distinct members. Safe normalization or a verified alias can resolve a spelling or wording variation. Treat ambiguous fuzzy matches as candidates for recovery or review. Preserve distinct names even when one is a substring of another. Repeated mentions of the same required member may share coverage. Keep the original source-to-checklist gaps visible in the final report and across cached reruns. Preserve compatible checklist caches and existing GUIDs for surviving cards.

**Acceptance tests**

- Feed a numbered eight-item list and a single generic concept that shares a word with every label. Assert zero matched members and all eight named gaps. Repeat with four specific members and the generic entry. Assert exactly four matched members.
- Include near-neighbor names such as `Binomial distribution` and `Negative binomial distribution`. Verify that a card or concept for one cannot silently satisfy the other. Also exercise safe normalization for harmless punctuation or capitalization.
- Mock a chapter run with eight expected items and four specific concepts that produce usable cards. Verify that the run or targeted recovery either fills the four gaps or reports all four by name and persists the incomplete state. Include a cached rerun and keep this test offline.

**Later work from the research comparison.** After this patch, use a small manually labeled set of explicit lists, dense slides, and transcript-only exam hints to benchmark source-to-export recall. Next separate TSV export success from independent answer verification against the cited passages, including unresolved visual evidence. Evaluate transcript segmentation and visual routing against that baseline before changing model selection or adding a visual index.

## Coverage follow-up for Antigravity — Preserve source counts and distinct names

The previous patch passes the generic-entry and four-of-eight regressions. Keep the one-to-one matching and targeted recovery. Fix these two remaining cases in the existing pipeline before changing models or adding retrieval infrastructure.

1. **Preserve the source's declared count through reporting and caching.** In `_extract_explicit_source_items()` and `_collect_chap_explicit_stats()`, a heading of `Eight probability distributions:` followed by seven recognizable bullet labels currently reports seven matched and zero missing. Retain a declared count separately from extracted labels and associate it with the relevant list and source location. Report **8 declared, 7 identified, 1 unresolved**, including after a cached rerun when all seven have valid cards. Keep an unidentified member unresolved without fabricating its name. Add coverage for a clearly headed ordinary bullet list without a declared count and a declared count above 20. Only use a count the source explicitly associates with the list.
2. **Require specific identity before awarding coverage.** In `_score_explicit_candidate()` and `_resolve_explicit_items()`, `Unstandardized regression coefficient` currently satisfies `Standardized regression coefficient`, and `Unadjusted R-squared` satisfies `Adjusted R-squared`. Count exact normalized matches or verified aliases. Use fuzzy similarity to propose candidates for targeted recovery or review. Preserve meaningful modifiers and the one-to-one constraint.

**Acceptance.** Add offline regression tests for both reproduced name pairs and for a chapter with eight declared items and seven extractable labels, including the cached report. Keep the existing generic-entry, four-of-eight, and GUID tests green. Checkpoint the patch before switching agents. Then benchmark the result on the actual Chapter 2 slides and transcript against a manually checked list of required items and at least one transcript-only point. Compare source count, identified items, and usable exported cards, and flag any unresolved evidence explicitly.
