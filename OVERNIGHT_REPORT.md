# Overnight Hardening Report (`toledo-dl-portable`)

**Branch:** `overnight-hardening` (branched from `master` baseline commit `03c0250`)  
**Scope:** Hardening, offline test coverage, cross-platform portability, VRAM cleanup, flashcard resilience, diagnostics, and installer idempotency across `toledo_dl.py`, `flashcards.py`, `bin/*`, `install.sh`, `install-mac.sh`, and `windows/install.ps1`.  
**Safety Compliance:** Real user directories (`~/Desktop/University`, `~/University`, `~/.local/share/toledo-dl`) were never accessed, modified, or read. All tests ran strictly against isolated `tmp_path` directories with all HTTP, Playwright, `yt-dlp`, `ffmpeg`, Ollama, and Gemini calls mocked.

---

## 1. Commits on `overnight-hardening`

| Commit | Task | Summary |
|---|---|---|
| `03c0250` | Baseline | Initial untouched baseline snapshot on `master` |
| `a945d3a` | Task 1 | Added `TOLEDO_DATA_DIR` / `TOLEDO_LECTURES_DIR` overrides, updated `toledo_dl.py` docstring, added `--lang`/`--model` to `sync` and subcommand help texts, added `pyproject.toml` + `.gitignore`, fixed 28 `ruff` `E`/`F`/`W` issues, added `tests/conftest.py` and `tests/test_cli.py` |
| `f85d143` | Task 2 | Switched `recordings_kaltura` and `recordings_onedrive` to `_fetch_all_contents` (with 401 `LoginRequired` check), hardened `clean`/`clean_filename`/`_unique_dest` for slashes and Windows `MAX_PATH`, added `tests/test_pipeline.py` |
| `f6a2bbb` | Task 3 | Enforced explicit `encoding="utf-8"` on all remaining text I/O, added `_recording_subfolder` and `_academic_year_for_month`, replaced `/tmp/toledo_frames` with `tempfile.gettempdir()`, unified `link_or_copy` fallback in `_repair_and_build_chapter_bundles`, added `tests/test_portability.py` |
| `4a80d23` | Task 4 | Added `_require_playwright()` guard for non-Playwright environments, implemented deterministic `_deterministic_pdf_match` fallback when Ollama is offline, added `tests/test_degradation.py` |
| `b40dacf` | Task 5 | Added `_release_gpu_vram()` (`gc.collect()` + `torch.cuda.empty_cache()`) inside `try/finally` after Whisper transcription in `transcribe_file` and at the end of `cmd_transcribe`, added `tests/test_vram_cleanup.py` |
| `ccaf2e7` | Task 6 | Hardened `_parse_json_payload` (code fences, trailing commas, LaTeX backslashes, truncated array salvage), added 4x exponential backoff on `429`/`503`/timeouts, added `flashcards_partial.json` batch resume, normalized MathJax delimiters, added `validate_anki_tsv()`, added `tests/test_flashcards.py` |
| `97062ca` | Task 7 | Added `toledo-dl doctor` (`cmd_doctor`), unified friendly `LOGIN_REQUIRED` onboarding prompt across all authenticated commands, updated `README.md`, added `tests/test_doctor.py` |
| *(HEAD)* | Task 8 | Restored `Find-RealPython` (ignoring Windows Store `WindowsApps` stub), escaped `^(Daily at 08:30^)` and normalized Windows `PATH` check in `windows/install.ps1`, added top-level `INSTALL.bat`, added macOS `LaunchAgent` check in `bin/toledo-sync status`, ensured `~/.bashrc` creation in `install.sh`, added `tests/test_installers.py` and `OVERNIGHT_REPORT.md` |

---

## 2. Bugs Fixed

1. **Slash corruption in folder/file names (`toledo_dl.py`):** `clean_filename("Module II: ANOVA/Slides?")` previously passed raw `/` to `Path()`, silently dropping `"Module II: ANOVA"`. Fixed by sanitizing `/` and `\` before `Path()` and clamping full destination paths in `_unique_dest` for Windows `MAX_PATH` safety.
2. **Missing pagination in recording discovery (`toledo_dl.py`):** `recordings_kaltura` and `recordings_onedrive` previously fetched only page 1 (`?limit=200`); both now paginate via `_fetch_all_contents` and raise `LoginRequired` on HTTP 401.
3. **Unprivileged Windows symlink crashes (`toledo_dl.py`):** `_repair_and_build_chapter_bundles` previously called `symlink_to` directly when repairing broken symlinks; now uses `link_or_copy()` with `shutil.copy2` fallback.
4. **Windows `cp1252` encoding hazards (`toledo_dl.py`):** Added explicit `encoding="utf-8"` to all 16 remaining `read_text`, `write_text`, and `open` text calls (verified by AST scan).
5. **Hardcoded academic years (`2026`/`2027`) & `/tmp/toledo_frames` (`toledo_dl.py`):** Replaced with dynamic `_academic_year_for_month()` and `tempfile.gettempdir()`.
6. **Offline Ollama breaking `match-slides` (`toledo_dl.py`):** Added `_deterministic_pdf_match()` scoring chapter numbers, filename tokens, and PDF slide-1 overlap so `Transcripts & Slides/` still populates on laptops without Ollama.
7. **Whisper VRAM lingering into Ollama stage (`toledo_dl.py`):** Added `del model` + `gc.collect()` + `torch.cuda.empty_cache()` immediately after transcription finishes.
8. **Truncated/LaTeX JSON failures & interrupted runs (`flashcards.py`):** Added trailing-comma stripping, truncated-array object salvage, `flashcards_partial.json` batch resume, `$...$` → `\(...\)` normalization, and `validate_anki_tsv()`.
9. **Windows `WindowsApps` Python stub trap (`windows/install.ps1`):** Restored `Find-RealPython` to ignore `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` and install Python 3.12 via `winget` automatically.

---

## 3. Test Coverage & Verification

All **33 tests** across 8 test modules pass (`pytest -v`) and `ruff check .` reports `All checks passed!`:
- `tests/test_cli.py` (11 tests): Root/subcommand `--help`, flag parity against `Toledo-DL-Complete-Guide.md`, `flashcards.py --dry-run`, `bin/toledo-sync` usage.
- `tests/test_pipeline.py` (5 tests): Pagination + 401 handling, Ultra document HTML (`bbcswebdav` / `data-bbfile`) extraction, `.url`/`Links.md`/video routing, announcement attachment sync, incremental idempotency, collision safety, `MAX_PATH`.
- `tests/test_portability.py` (3 tests): AST scan enforcing `encoding="utf-8"` on every text I/O call, Windows symlink `OSError` fallback in `match-slides` + `Chapters/`, generalized subfolder & academic-year helpers.
- `tests/test_degradation.py` (3 tests): Lazy ML imports (no `faster_whisper`/`ctranslate2`/`torch` on import), missing-Playwright error guard, offline Ollama fallback for renamer + `match-slides`.
- `tests/test_vram_cleanup.py` (1 test): Verifies `gc.collect()` + `torch.cuda.empty_cache()` execute before post-transcription slide matching.
- `tests/test_flashcards.py` (4 tests): Malformed/truncated JSON salvage, `429` exponential backoff, `flashcards_partial.json` resume, Anki `.tsv` header/column/MathJax validation.
- `tests/test_doctor.py` (3 tests): `toledo-dl doctor` all-OK & missing-dependency diagnostics, unauthenticated `cmd_list` and `cmd_sync` returning exit code `2` with friendly `toledo-login` message.
- `tests/test_installers.py` (3 tests): `bash -n` on `install.sh`, `install-mac.sh`, and `bin/*`, rc-file `PATH` idempotency, Windows installer checks & `systemd` unit path consistency.

---

## 4. Proposed Improvements

Items below were identified during auditing but intentionally left untouched per the plan's non-rewriting guardrail:

1. **Remove unreferenced legacy functions (`toledo_dl.py` lines 1930–2310, 2577–2609, 3446–3485):**
   - `extract_visual_checkpoint_from_video`, `extract_checkpoint_*`, `update_course_checkpoints`, `cmd_checkpoint`, `cmd_ui`, `cmd_drive_sync`, and `_transcribe_gemini` (lines 1345–1501) are no longer wired to any CLI subcommand.
   - *Estimated impact:* Removes ~550 lines of dead code, shrinks `toledo_dl.py` by ~16%, and eliminates dead `/usr/bin/rclone` and `dashboard.py` references.
2. **Avoid redundant course & content tree API walks during `cmd_sync` (`toledo_dl.py` lines 510–517, 2997–3035):**
   - `cmd_sync` fetches `courses(c)` once for `sync_files`, then `gather(c, args.course)` fetches `courses(c)` again, and both `recordings_kaltura` and `recordings_onedrive` walk the exact same `/courses/{cid}/contents` tree separately (plus `gather_files` walks it a third time).
   - *Estimated impact:* Combining the course content walk into one cached traversal per course would cut Blackboard REST API calls by ~50–65% during `sync`.
3. **Cache `pypdf` Page-1 extraction across `match_transcript_with_pdfs` (`toledo_dl.py` lines 2425–2442):**
   - `match_transcript_with_pdfs` re-opens and parses page 1 of every PDF in the course for every single transcript (`N_transcripts * N_pdfs` PDF reads). Passing a per-course `pdf_summaries` cache inside `update_course_pdf_matches` would parse each PDF once per course.
   - *Estimated impact:* Cuts PDF I/O during `match-slides` by ~5–15x on courses with 15+ PDFs and 20+ lectures.
4. **Refresh `Transcripts & Slides/` when `_maybe_rename_vague_lecture` renames a transcript (`toledo_dl.py` lines 1564–1615, 1715–1730):**
   - Currently `transcribe_file` calls `match_transcript_with_pdfs` *before* `_maybe_rename_vague_lecture`; when a vague lecture is later renamed, `_repair_and_build_chapter_bundles` fixes the broken symlink during Stage 5 (`index`), but `.pdf_mapping.json` still holds the pre-rename `rel_key`. Running `_maybe_rename_vague_lecture` *before* `match_transcript_with_pdfs` avoids creating a stale symlink/mapping key altogether.
   - *Estimated impact:* Prevents broken intermediate symlinks in `Transcripts & Slides/` on vague-titled lectures.
