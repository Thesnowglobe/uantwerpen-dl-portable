# Changelog — `flashcards.py` Stage 2 Redesign & Audit Fixes

## Summary of Changes

### Part A — Source-Grounded Stage 2 & Delimited Block Output
- **Stage 1 as a coverage contract (`PASS1_INSTRUCTIONS` / `AUDITOR_INSTRUCTIONS`):**
  - Checklist items now record `concept` (unique & specific), `slide_numbers`, `pdf` (source filename), `origin` (`"slide"` or `"transcript"`), `anchor` (short verbatim quote when `origin == "transcript"`), and plain-text `details` (treated as a non-authoritative hint in Stage 2).
- **Stage 2 grounded directly in source text (`_concepts_to_cards`):**
  - Batches are grouped by Stage 1 `(pdf, chunk_label)` window and capped at `--chunk-size`. Concepts with missing or out-of-range slide numbers receive the full chunk window.
  - Slide text ($\pm 1$ neighboring slides) extracted via `pypdf` (`_extract_pdf_pages_text`) is anchored with `--- SLIDE N (<pdf>) ---`, alongside anchor/keyword-matched transcript excerpts (with proportional window fallback).
  - Default Stage 2 model chain now matches Stage 1 (`gemini-3-flash-preview` on `--model auto`) instead of `flash-lite`, configurable via `--stage2-model`.
- **Delimited Stage 2 output (`_parse_card_blocks`, `json_mode=False`):**
  - Stage 2 sets `json_mode=False` in `_call_gemini()` (omitting `responseMimeType: application/json`) and requests `<<<CARD cXX>>> ... <<<END>>>` delimited blocks so LaTeX backslashes and multi-line code fences are emitted verbatim without JSON escaping.
  - `_parse_card_blocks()` tolerates missing final `<<<END>>>` and multi-line `FRONT`/`BACK`/`EXTRA`, logs warnings for missing IDs or missing `FRONT`/`BACK`, and removes legacy positional ID auto-assignment (legacy JSON mock/cache arrays remain supported when `"<<<CARD"` is absent).
- **Evidence & fuzzy grounding check (`_check_grounding`):**
  - Every card emits `EVIDENCE` and is checked with `difflib.SequenceMatcher` ($\ge 0.8$ sliding word window) against non-visual source text, persisting `"grounded": true | false | null` (`null` on visual-only slides). Ungrounded cards are never dropped; they are listed in `report.md`.
- **Verification & `MAX_TOKENS` recovery:**
  - `GeminiMaxTokens` splits oversized Stage 2 batches in half and retries recursively.
  - Missing `concept_id`s trigger at most 2 recovery calls; leftovers are recorded in `report.md`.
  - Front deduplication drops duplicate-front cards only when the owning `concept_id` still has another surviving card; otherwise the card is retained with a warning.

### Part B — Bug Fixes & Hardening
- **B1 — JSON-first parsing:** `_parse_json_payload()` now tries `json.loads(..., strict=False)` before running `_LATEX_JSON_ESCAPE_RE` (expanded with `\nexists`, `\ncong`, `\nrightarrow`, `\nolimits`, `\tbinom`, `\ttfamily`). Removed the post-parse `\n` repair branch from `_repair_corrupted_latex()` so real newlines before `\nsim`, `\nequals`, `\nless`, `\nsimilarly` are preserved.
- **B2 — Stray `$` encoding & non-blocking quarantine:** Non-math/shorthand/range `$` (`$100M`, `$5-$10`, `$5k`, lone `$`) are encoded as `&#36;` outside code/math. Validation failures in `_write_anki_tsv()` no longer raise; failing rows move to `<Course>/Flashcards/_quarantine.tsv` and valid rows are written atomically. `<Course>_ALL.tsv` rebuild runs in its own `try/except`.
- **B3 — Empty-source guard & `cache_meta.json`:** Chapters with no `.pdf`, `.txt`, or non-empty `CHAPTER_BUNDLE.md` print `no source files yet` and write no caches. Empty results (`[]`) are trusted only when `<chapter>/cache_meta.json` matches the SHA-256 `inputs_hash` (PDF/TXT/bundle bytes, prompt constants, stage models); legacy empty caches without `cache_meta.json` are re-evaluated.
- **B4 — `_merge_concepts` identity:** Scoped concept identity by `(pdf, concept)`. Distinct details on overlapping slides append with `" | "` instead of overwriting; slide-less duplicates with different details survive with `#2`, `#3` suffixes; identical concept names on the same slide in different PDFs both survive.
- **B5 — Normalized chapter tag:** `_format_chapter_tag()` produces lowercase `chapter::<slug>` replacing whitespace and unsafe characters with `_`.
- **B6 — Word-boundary bold:** `**bold**` converts to `<b>bold</b>` only via `(?<![\w*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![\w*])` so `x**2 and y**3` is untouched.
- **B7 — Math & HTML `<` escaping:** `<` inside `\(...\)` and `\[...\]` is always escaped to `&lt;`; outside math only exact safe HTML tags remain unescaped while bare `<` before letters (`a<b and c>d`) becomes `&lt;` and numeric comparisons (`p<0.05`) stay untouched.
- **B8 — Header auth & `GeminiBlocked` fallback:** `_call_gemini()` sends the API key via `x-goog-api-key` header (not the URL query string), caps total sleep at `180s`, and tries the next candidate model on empty/blocked responses before raising `GeminiBlocked`. Blocked Stage 1 chunks log to `report.md` and continue.
- **B9 — Per-chapter error isolation:** All chapters (single or multi) are wrapped in `try/except`, printing a chapter summary table and returning exit code `1` on any failure.
- **B10 — Partial hash validation:** `concepts_partial.json` and `flashcards_partial.json` store `inputs_hash`, `chunk_size`, and `n_batches` and are ignored on mismatch.
- **B11 — Ollama parity:** Ollama Stage 1 checkpoints to `concepts_partial.json` and Stage 2 uses source text + delimited blocks.
- **B12 — 5-column Anki TSV & deterministic GUIDs:** Exports 6 headers (`#separator:tab`, `#html:true`, `#notetype:<type>`, `#deck column:1`, `#tags column:4`, `#guid column:5`) and 5 columns. `_make_card_guid()` persists deterministic SHA-1 GUIDs in `flashcards.json` and preserves them across re-normalization and `--stage2-only`.
- **B13 — Atomic I/O (`tempfile` + `os.replace`)** across all JSON, TSV, `_quarantine.tsv`, `cache_meta.json`, and `report.md` files.

### Part C — Observability
- Writes `<chapter>/report.md` on every chapter run with status, timing, models used, retries, fallbacks, blocked chunks, concepts per PDF, slides with zero concepts (text vs. visual-only slides), cards per concept, concepts without cards, ungrounded cards, and quarantined rows, plus a CLI chapter summary table.

## New CLI Flags
- `--stage2-source {text,pages}` (default: `text`) — attach `pypdf` extracted text slides (`text`) or also inline sliced PDF pages (`pages`, via `_slice_pdf_pages`).
- `--stage2-model <model>` (default: same model chain as Stage 1) — override Stage 2 model independently.
- `--stage2-only` — reuse cached `concepts_checklist.json` and regenerate Stage 2 flashcards only (preserving existing card GUIDs).
- `--rebuild-tsv` — rebuild all `.tsv` exports from cached `flashcards.json` with zero API calls.
- `--notetype <name>` (default: `Basic`) — set the Anki `#notetype:` header.

## Cache & Format Compatibility Notes
- Legacy `concepts_checklist.json` and `flashcards_concepts.json` containing only `concept`, `slide_numbers`, and `details` remain directly readable by Stage 2 (`pdf`, `origin`, `anchor` default gracefully).
- Existing legacy empty caches (`flashcards.json == []`) without `cache_meta.json` are automatically re-evaluated once on the next run.
- Legacy 4-column Anki TSV imports from prior versions will fail the new 6-header / 5-column `validate_anki_tsv()` check until regenerated cleanly via `--rebuild-tsv`.

## Verification Notes
- `toledo-dl-portable`: `pytest -v` (`58 passed`), `python3 -m py_compile flashcards.py`, and `ruff check .` (`All checks passed!`).
- `canvas-dl-portable`: `pytest -v` (`60 passed`), `python3 -m py_compile flashcards.py`, and `ruff check .` (`All checks passed!`).
- All Gemini and Ollama API calls in `pytest` are mocked offline (zero network traffic). System `/usr/bin/python3` runs without `pypdf` installed (exercising fallback paths), while real PDF chunking (`_split_pdf_chunks`), slicing (`_slice_pdf_pages`), and slide extraction (`_extract_pdf_pages_text`) were verified with `pypdf 7.1.0` in `/home/raymundo21/.local/share/toledo-dl-app/.venv/bin/python`.

---

## Latest Session Updates — Chapter-First Folders & Explicit Chapter Matching

1. **Explicit Chapter Matching (`_explicit_chapters_in_stem`):**
   - Automatically parses explicit chapter/part markers from lecture stems (e.g. `Lecture 05 - 0. Introduction. 1. Probability` -> `{0, 1}`, `Lecture 01 - 2. Univariate` -> `{2}`, `Lecture 03 - 2. Univariate... 3. Multivariate...` -> `{2, 3}`, `Lecture 08 - 5 Sample statistics` -> `{5}`).
   - Solved the chronological floor inversion bug where later lecture numbers (e.g. Lecture 05) were forced by chronological filters to match higher chapters (e.g. Chapter 3/4) rather than their true earlier topics (Chapters 0 & 1).
   - `_plan_match_jobs` and `match_transcript_with_pdfs` now allow explicit chapters to bypass chronological floor restrictions and match their designated slide decks with high confidence.

2. **Chapter-First / PDF-First Folder Organization:**
   - Populates human-friendly numbered chapter folders (`00_Introduction`, `01_Probability`, `02_Random_Variables`, `03_Random_Vectors`, `04_Convergence_of_Random_Variables`, `05_Sample_Statistics`, `06_Intermediate_Test_Prep`) directly in `Transcripts & Slides/` and `LLM_Knowledge/Chapters/`.
   - Each chapter folder groups the slide deck PDF alongside all lecture transcripts (`.txt` and `.srt`) covering that chapter.
   - Also links lecture transcripts directly into the course root chapter directories (`1. Probability/`, `2. Random variables/`, `3. Random vectors/`, `4. Convergence of random variables/`, `5. Sample statistics/`) alongside the slides and exercise sheets.
   - Maintained backward compatibility with per-lecture test assertions and individual lecture folders.

3. **Archive Deduplication & Cleanup:**
   - Deduplicated redundant truncated lecture files (`Lecture 01 - 2.mp4` vs `Lecture 01 - 2. Univariate Distributions and Random Variables (part 2).mp4`, freeing ~4.35 GB of disk space in the user's archive).
   - Updated `.downloaded.json` and `.pdf_mapping.json` to map Kaltura IDs to canonical full titles so subsequent `sync` runs remain idempotent.
   - Cleaned up obsolete orphan folders in `Transcripts & Slides/`.

4. **Local GPU PDF Vision Installer Helper:**
   - Added `_ensure_local_pdf_vision_setup` to check and optionally one-click install missing `poppler-utils` and `ollama` dependencies when GPU vision is selected.

5. **Offline Test Suite:**
   - Added test coverage in `tests/test_pipeline.py` verifying explicit chapter extraction, floor overrides, and human-friendly chapter folder creation.
   - All 71 tests passing offline with `ruff check .` passing.

---

## Earlier Session Updates — Gemini 2.5 Pro Default & Unified `toledo flashcards` CLI

### 1. `flashcards.py` — Default `gemini-2.5-pro` Routing & Programmatic `main(argv)`
- **Model routing (`_gemini_candidate_models`, `_call_gemini`, `_extract_concepts`, `_concepts_to_cards`, `main`):**
  - Default Gemini API model when `--model auto` is used changed from `gemini-3-flash-preview` to `gemini-2.5-pro` across Pass 1 (`_extract_concepts`), Pass 2 (`_concepts_to_cards`), and `main()`, with fallback chain `["gemini-2.5-pro", "gemini-3-flash-preview", "gemini-flash-latest"]`.
- **Programmatic entrypoint (`main(argv: list[str] | None = None) -> int`):**
  - Updated `main()` signature to accept an optional `argv` list passed to `ap.parse_args(argv)` so `toledo_dl.py` can invoke `flashcards.main(...)` directly without mutating `sys.argv`.

### 2. `toledo_dl.py` — `flashcards` Subcommand & Opt-in `sync --flashcards`
- **New `cmd_flashcards(args)` & `flashcards` subparser:**
  - Added `toledo-dl flashcards [course_query...] [--course ...] [--chapter ...] [--all] [--model ...] [--dry-run] [--force]`.
  - **Interactive course selector:** When invoked in an interactive TTY without `--course`, `--all`, or positional `course_query`, lists downloaded courses from `OUT` with chapter counts and prompts `[1-N, a]`.
  - **Positional course matching:** Supports unquoted multi-word course names (e.g., `toledo flashcards multivariate statistics`), joining `course_query` tokens and forwarding `--course` to `flashcards.main()`.
- **Opt-in `--flashcards` / `--no-flashcards` on `toledo-dl sync`:**
  - Added optional Step 6 in `cmd_sync` to invoke `flashcards.main()` after `build_llm_index()` only when `--flashcards` is explicitly passed.
  - Background `bin/toledo-daily-sync` and `bin/toledo-sync` remain sync-only by default so API credits are never spent automatically during routine syncs.

### 3. `bin/toledo`, `install.sh`, `install-mac.sh` — `toledo` CLI Wrapper
- Added `bin/toledo` wrapper delegating to `toledo_dl.py` so users can run `toledo flashcards ...` alongside `toledo-dl flashcards ...` and `toledo-flashcards`.
- Updated `install.sh` and `install-mac.sh` CLI installation loops to install `toledo` into `~/.local/bin`.

### 4. `tests/test_cli.py` & `tests/test_flashcards.py` — Test Updates
- Updated model routing expectations in `tests/test_flashcards.py` (`test_pdf_chunking_and_pass1_model_routing`, `test_a3_b8_gemini_header_auth_model_fallback_and_stage1_blocked_continuation`) to `gemini-2.5-pro`.
- Updated `tests/test_cli.py` subcommand/flag assertions for `flashcards` and `sync --flashcards` / `--no-flashcards`, and added `test_cmd_flashcards_interactive_prompt` covering both interactive selection and positional `toledo-dl flashcards multivariate statistics --dry-run`.

---

## Codex Audit Fixes & AI-Ready Storage / Retrieval (`CODEX_AUDIT.md`)

### 1. CLI Follow-up Fixes
- **Exact course selection (`--exact-course`):** Selecting a course by number in `toledo flashcards` passes `--exact-course` so picking `Statistics` does not match `Applied Statistics`.
- **Unmatched filter & sync flags:** Unmatched `--course` / positional course queries exit `1` with a notice; `toledo-dl sync --flashcards --no-flashcards` properly suppresses flashcard generation; non-zero flashcard returns propagate exit code `1` from `cmd_sync`.

### 2. Core Audit Findings (Items 1–9)
- **Incomplete/blocked cache protection (`flashcards.py`):** Blocked Stage 1 chunks (`reason="blocked"`) and Stage 2 incomplete runs (`reason="incomplete"`) are no longer trusted on re-runs; cached/rebuild runs also recompute `concepts_without_cards` and `ungrounded_cards` for `report.md`.
- **Front-first GUID stability (`_assign_card_guids`):** Reordered cards for the same `concept_id` match existing GUIDs by normalized front text before falling back to unclaimed ordinals.
- **Empty `--rebuild-tsv` cleanup:** Emptying `flashcards.json` to `[]` rewrites existing chapter `.tsv` and `<Course>_ALL.tsv` exports with headers only instead of keeping stale rows.
- **Fenced code & TSV boundary safety:** `_parse_card_blocks()` ignores field markers inside fenced code blocks; `_clean_tsv_field()`, `_validate_tsv_row()`, and `validate_anki_tsv()` sanitize/validate tabs inside inline `<code>` spans and Unicode line separators (`\u2028`, `\u2029`, `\v`, `\f`, `\x85`, `\x1c-\x1e`).
- **Download/rename collision & reserved filename safety (`toledo_dl.py`):** `cmd_sync()` disambiguates distinct recordings with identical clean filenames via `reserved_stems`; `_maybe_rename_vague_lecture()` includes `.vtt` and `.md` in collision checks; `clean()` / `clean_filename()` prefix Windows reserved device names (`CON.pdf`, `NUL.txt`, `AUX.zip`, etc.).
- **Distinct PDF chapters & reorder migration (`_repair_and_build_chapter_bundles`):** Keys PDF units by path/content identity rather than basename, persists `.chapter_meta.json` to migrate renamed `NN_<stem>` folders cleanly without duplicate chapters, and prunes superseded managed folders.

### 3. AI-Ready Storage & Stage 2 Retrieval
- **Stable source identity & filtered index preservation (`build_llm_index`):** Disambiguates colliding transcript/PDF Markdown stems by relative folder, embeds `source_id` in frontmatter/`llm_manifest.jsonl`, and preserves other courses when running `toledo-dl index --course ...`.
- **Portable passage records & standalone materials:** Emits `LLM_Knowledge/passages.jsonl` (and per-chapter `passages.jsonl`) for PDF pages, `.srt` timestamped transcript segments, and `.docx`/script/CSV handouts (`_extract_docx_text`, `_extract_document_text`), and indexes unpaired PDFs, unpaired transcripts, and standalone handouts/scripts into `Chapters/`.
- **Focused retrieval & cited-slide grounding (`flashcards.py`):** `_match_transcript_excerpt()` centers excerpts around anchor hits inside oversized paragraphs; Stage 2 avoids duplicating `.txt` and `CHAPTER_BUNDLE.md`, auto-attaches visual-only PDF pages in `text` mode, stores `source_id`/`passage_ids` on cards, and `_check_grounding()` verifies quotes strictly against the cited PDF and slide number(s).

### 4. Flashcard Coverage Audit Brief (`flashcards.py`)
- **Explicit multi-item source reconciliation & empty-chunk auditing:** Added `_extract_explicit_source_items()` and `_find_missing_explicit_items()` to detect numbered/explicit lists (e.g. `8 key principles`, protocol/header tables) across slide text, handouts, and transcripts and trigger targeted auditor recovery; `_extract_concepts()` also runs Stage 1.5 auditing even when a chunk initially yields zero concepts.
- **Unpaired & long transcript coverage:** Added `TRANSCRIPT_PASS1_INSTRUCTIONS` and independent segment scanning for unpaired transcripts and transcripts exceeding 45k chars (`_read_chap_transcript_text()`), preserving spoken exam traps and verbal-only definitions without duplicating short-transcript Pass 1 calls.
- **Per-unit source inventory & visual-page protection:** `_build_chapter_source_inventory()` tracks PDF page inspectability (`_is_pdf_inspectable()`) and transcript/handout inspection status (`inspected` / `unresolved` / `pending`), surfacing unresolved textless PDFs, blocked chunks, and explicit-item gaps in `cache_meta.json` and `report.md` (`## Source Coverage & Inventory`).
- **Targeted gap retry & TSV export reconciliation:** Added targeted Stage 2 retry for missing/quarantined concepts while preserving surviving GUIDs, plus final TSV row reconciliation (`required_items_covered`, `export_gaps`) reported to `cache_meta.json` and `report.md`.
- **1-to-1 explicit member resolution (`_resolve_explicit_items`):** Replaced bidirectional substring/loose token matching in `_concept_covers_item()` with strict token-length-preserving normalization (`_normalize_explicit_name`, `_singularize_tokens`), ambiguous fuzzy rejection, and 1-to-1 distinct-member assignment so generic concepts (e.g. `"Distribution"`) or near-neighbors (e.g. `"Negative binomial distribution"` vs `"Binomial distribution"`) cannot falsely satisfy distinct explicit list members.

---

## Latest Antigravity -> Codex Handoff — Multimodal Cloud-First `match-slides` (2026-11-19)

### Summary of Changes
- **All-slide PDF outlines (`toledo_dl.py`):**
  - Added `_extract_pdf_outline()` and `_load_or_build_pdf_outlines()` to extract slide-by-slide headings across up to 120 pages via `pypdf` (deduplicating repeated Beamer/pause headers) and cache them by PDF SHA-256 in `<course>/.pdf_outlines.json` via `_write_json()`. Graceful fallback when `pypdf` is unavailable.
- **MP4 video frame sampling (`toledo_dl.py`):**
  - Added `_find_lecture_video()`, `_probe_video_duration()`, and `_sample_video_frames()` to sample 6 evenly spaced 1280px JPEG screenshots across sibling `.mp4`/`.mkv`/`.webm`/`.mov` files (`10%, 25%, 40%, 55%, 70%, 85%`) directly to memory via `ffmpeg` `image2pipe` (no temp files). Handles both full-screen slide recordings and split-screen Kaltura layouts.
- **Full-lecture transcript slicing (`_sample_transcript_slices`):**
  - Sends the full transcript if ≤ 1,200 words, or 4 evenly distributed slices (start, ~33%, ~66%, end) so mid-lecture topics are represented even without video frames.
- **Cloud-first matching with local GPU & CPU fallbacks (`toledo_dl.py`):**
  - Added `_resolve_match_backend()`, `_call_match_gemini()`, `_call_match_openai()`, `_call_match_ollama()`, and `_parse_json_object()`.
  - `match-slides` CLI now supports `--backend auto|gemini|openai|ollama|keyword`, `--model <name>`, and `--no-video`.
  - In `auto` mode: prefers **Gemini** (`gemini-2.5-flash` via `GEMINI_API_KEY`) → **OpenAI-compatible** (`gpt-4o-mini` via `OPENAI_API_KEY` + optional `OPENAI_BASE_URL`) → **local Ollama** (`phi4:14b`) → deterministic CPU keyword/topic matching.
  - Prompt enforces **sustained coverage** across sampled frames and transcript excerpts so brief 1-frame refreshers of an older chapter are ignored while genuine chapter transitions link both PDFs.
  - Preserves Codex's chronology rules (`_lecture_chapter_floor`, `_is_review_lecture`, stale-cache rechecking, SHA-256 PDF deduplication, and safe stale-link removal).
  - Persists additive fields (`backend`, `used_video_frames`, optional `slide_range`) in `.pdf_mapping.json`.
- **Security & Secret Hygiene (`toledo_dl.py`, `tests/test_security_and_recovery.py`):**
  - API keys are read strictly from environment variables (no plaintext key storage on disk), sent exclusively via HTTP headers (`x-goog-api-key` / `Authorization: Bearer`, never `?key=` URL query strings), and scrubbed in `_redact_secrets()`.

### Verification
- `TOLEDO_LECTURES_DIR=/tmp/toledo-test-env TOLEDO_DATA_DIR=/tmp/toledo-test-data pytest -v`: **45 passed** (added offline multimodal Gemini + OpenAI/fallback tests in `tests/test_pipeline.py` and updated `tests/test_security_and_recovery.py`).
- `python3 -m py_compile toledo_dl.py`: passed.
- `/home/raymundo21/.local/bin/uvx ruff check .`: **All checks passed!**

---

## Latest Antigravity -> Codex Handoff — Explicit Cloud Opt-In & Consent Guard for `match-slides` (2026-11-19)

### Summary of Changes (addressing `CODEX_AUDIT.md` Priority 1)
- **API keys are credentials, never implicit upload consent (`_resolve_match_backend` in `toledo_dl.py`):**
  - Default `match-slides` (`--backend auto`) now stays **strictly local** (Ollama → deterministic CPU fallback) even when `GEMINI_API_KEY` or `OPENAI_API_KEY` is populated in the environment.
  - Cloud slide matching only runs when explicitly opted in via `--backend gemini|openai` or `TOLEDO_MATCH_BACKEND=gemini|openai`.
  - Passing `--backend local` (new alias for local Ollama/CPU), `--backend ollama`, or `--backend keyword` always overrides `TOLEDO_MATCH_BACKEND`.
- **Pre-flight upload summary, `--dry-run`, and explicit confirmation (`cmd_match_slides` in `toledo_dl.py`):**
  - Added `_count_pending_match_lectures()`, `--dry-run`, and `-y`/`--yes` to `match-slides`.
  - Before any cloud slide-matching job uploads data, prints the selected provider, model, data types to be uploaded (transcript excerpts, PDF outlines, and video frames if enabled), and pending lecture/video counts.
  - Requires interactive `[y/N]` confirmation in a TTY (or explicit `--yes`; non-interactive runs without `--yes` abort cleanly with exit code `1` and zero requests).

### Verification
- `TOLEDO_LECTURES_DIR=/tmp/toledo-test-env TOLEDO_DATA_DIR=/tmp/toledo-test-data pytest -v`: **46 passed** (added `test_slide_match_keys_never_auto_upload_and_local_overrides_env` in `tests/test_pipeline.py`).
- `python3 -m py_compile toledo_dl.py`: passed.
- `/home/raymundo21/.local/bin/uvx ruff check .`: **All checks passed!**

### Next Design Doc for Codex Audit
- Drafted [CLOUD_AND_MATCHING_PLAN.md](CLOUD_AND_MATCHING_PLAN.md) covering the end-to-end UX matrix (regular laptop vs. GPU laptop), separate cloud opt-in/consent safeguards, resumable chunked cloud transcription, slow-CPU skip behavior, and open questions for Codex before implementing cloud transcription.

---

## Latest Antigravity -> Codex Handoff — High-Severity Upload Guards & Resumable Gemini Cloud Transcription (2026-11-19)

### 1. Fixed Both High-Severity Audit Findings in `match-slides` (`toledo_dl.py`)
- **Finding 1 (Stale match bypassing confirmation):**
  - Replaced `_count_pending_match_lectures()` with `_plan_match_jobs(course_dirs, force=False, limit=None)` using the exact same chronological ordering and `_lecture_chapter_floor()` / `eligible()` staleness check as `match_transcript_with_pdfs()`.
  - `cmd_match_slides()` now uses this **single explicit job list** for preview, `--limit`, `[y/N]` confirmation, and execution. Reporting `0 pending` returns immediately with zero upload calls, and any lecture not in the approved list cannot upload even if a chapter floor shifts mid-run.
- **Finding 2 (Automatic callers bypassing upload guard):**
  - Added `_make_cloud_approval()` and `_is_cloud_approved()`, enforced directly inside `match_transcript_with_pdfs()` before frame sampling or any network call. Approval is bound to `(feature, provider, model, allowed_paths, allow_video)`.
  - `update_course_pdf_matches()` defaults to `backend="local"`, `cmd_sync()` passes `backend="local"` to both `cmd_transcribe()` and `update_course_pdf_matches()`, and `transcribe_file()` calls `match_transcript_with_pdfs()` with `cloud_approval=None` so automatic callers never trigger cloud uploads even when `GEMINI_API_KEY`, `OPENAI_API_KEY`, and `TOLEDO_MATCH_BACKEND=gemini` are set.

### 2. Implemented Opt-In Resumable Gemini Cloud Transcription (`toledo_dl.py`)
- **Separate opt-in & slow-CPU safeguard (`_resolve_transcribe_backend`, `cmd_transcribe`):**
  - Added `--backend auto|local|gpu|cpu|gemini`, `--limit N`, `--max-minutes M` (default `180` min cap for cloud runs), and `-y`/`--yes` to `transcribe`.
  - Populated `GEMINI_API_KEY` alone never triggers cloud transcription. Explicit `--backend local|gpu|cpu` always overrides `TOLEDO_TRANSCRIBE_BACKEND`.
  - On CPU-only laptops without a GPU and without an opted-in cloud backend, `auto` mode skips transcription immediately (avoiding slow CPU hangs) and directs users to `--backend gemini` or explicit `--backend cpu`.
- **Pre-flight summary & workload caps (`_plan_transcribe_jobs`):**
  - Applies `--limit` and `--max-minutes` before showing the pre-flight summary (provider, model, data type, recording count, total minutes/hours, billing caveat), `--dry-run`, and `[y/N]` / `--yes` confirmation.
  - Enforces scoped `CloudApproval(feature="transcribe", provider="gemini", allowed_paths=...)` inside `_transcribe_audio_via_gemini()`.
- **Versioned private chunk cache & atomic completion marker (`_transcribe_audio_via_gemini`, `_commit_transcript_pair`):**
  - Splits audio via `ffmpeg` into uniform mono 16 kHz MP3 chunks (`900s`, 32 kbps) and uploads via `x-goog-api-key` header auth (never `?key=`). Checks `MAX_TOKENS` truncation and clamps/monotonizes timestamps.
  - Caches completed chunks privately in `TOLEDO_DATA_DIR/transcribe_cache/<sha>` (`0700`/`0600` on POSIX), keyed by `(version, recording_sha256, backend, endpoint, model, normalized_language, audio_format, chunk_seconds)`. Resuming after interruption uploads **only missing chunks**.
  - Commits `.txt`, `.srt`, and `.<stem>.transcript.done.json` atomically while guarding with `.<stem>.transcript.inprogress`, then prunes the chunk cache.
  - Updated `_is_transcript_complete()` and `_is_transcript_txt()` to honor completion/in-progress markers while preserving backward compatibility with legacy lone `.txt` files.
- **Clearer `toledo-dl doctor`:**
  - Reports detected `GEMINI_API_KEY` / `OPENAI_API_KEY` as `"detected in environment (connection not verified; cloud upload requires explicit --backend ...)"` without claiming connectivity.

### Verification
- `TOLEDO_LECTURES_DIR=/tmp/toledo-test-env TOLEDO_DATA_DIR=/tmp/toledo-test-data pytest -v`: **49 passed** (added `test_codex_high1_stale_match_requires_confirmation_and_zero_work_means_zero_uploads`, `test_codex_high2_automatic_callers_never_upload_even_with_cloud_env`, and `test_cloud_gemini_transcribe_chunking_resume_completion_marker_and_caps` in `tests/test_pipeline.py`).
- `python3 -m py_compile toledo_dl.py`: passed.
- `/home/raymundo21/.local/bin/uvx ruff check .`: **All checks passed!**

---

## Antigravity Handoff (Post-Codex Audit Fixes on `304069d`)

Fixed all 5 release-blocking findings from `CODEX_AUDIT.md` plus documentation updates:

1. **High — Failed transcript commit & marker rename (`_commit_transcript_pair`, `_course_match_transcripts`, `_maybe_rename_vague_lecture`):**
   - `_commit_transcript_pair()` now retains `.<stem>.transcript.inprogress` if writing `.txt`, `.srt`, or `.transcript.done.json` fails, unlinking it only after the full pair + completion marker succeed.
   - `_course_match_transcripts()` and `transcribe_file()` honor the in-progress and completion markers so partial `.txt` files are never matched or skipped.
   - `_maybe_rename_vague_lecture()` carries `.<stem>.transcript.done.json` and `.<stem>.transcript.inprogress` along with renamed stems.
2. **High — Course-scoped symlink escape (`_is_path_within_dir`, `cmd_transcribe`, `_transcribe_audio_via_gemini`, `_course_match_transcripts`, `_find_lecture_video`, `match_transcript_with_pdfs`):**
   - Course-scoped transcription and slide-matching resolve and validate all audio, transcript, and sibling video paths stay strictly inside the selected course directory both before preview and before cloud dispatch.
3. **Medium — Unknown-duration cap & full `CloudApproval` validation (`_plan_transcribe_jobs`, `_make_cloud_approval`, `_is_cloud_approved`):**
   - `_plan_transcribe_jobs(..., probe_duration=True)` defers recordings whose duration probe fails (`None` or `<= 0`) instead of treating them as `0s`.
   - `_is_cloud_approved()` now validates `model`, `data_types`, `course_dir` containment, file SHA-256 identity, and re-probed duration against `max_duration_sec` / `max_file_durations` before upload.
4. **Medium — Explicit `--backend cpu` / `--backend gpu` device selection (`transcribe_file`, `cmd_transcribe`):**
   - Added `device="auto"|"cpu"|"cuda"` to `transcribe_file()` and wired `cmd_transcribe()` so `--backend cpu` initializes Whisper directly on CPU without touching CUDA.
5. **Medium — Subtitle chunk-boundary clamping & `MAX_TOKENS` split-and-resume (`_call_gemini_audio_chunk`, `_run_gemini_chunk_or_split`, `_transcribe_audio_via_gemini`):**
   - Clamped segment timestamps to finite values strictly within `[0, chunk_duration_sec]` and globally monotonic across chunks.
   - Added `--chunk-seconds` to `transcribe` and bounded split-and-resume (halving chunks on `_GeminiMaxTokensError` up to depth 3 / 60s min, caching each sub-chunk).
6. **Docs:** Updated `README.md` and `CLOUD_AND_MATCHING_PLAN.md` with `--backend gemini`, `--backend cpu`, `--max-minutes`, `--chunk-seconds`, and revised defaults.

### Verification
- `TOLEDO_LECTURES_DIR=/tmp/toledo-test-env TOLEDO_DATA_DIR=/tmp/toledo-test-data uvx pytest -v`: **54 passed** (added 5 offline regression tests in `tests/test_pipeline.py`).
- `python3 -m py_compile toledo_dl.py`: passed.
- `uvx ruff check .`: **All checks passed!**

### Codex follow-up audit

Recorded four open findings and their offline regression cases in `CODEX_AUDIT.md`. Review status is `CHANGES_REQUESTED`.

Recorded the user’s release acceptance criteria for Toledo DL and Canvas DL in `RELEASE_ACCEPTANCE.md`, including NVIDIA GPU scope and separate user sign-off for both products.

---

## Antigravity Handoff — Round 1 Audit Fixes (Post-`fccf8c2`)

Fixed all 4 open findings from `CODEX_AUDIT.md`:

1. **High — Top-level course symlink escape (`cmd_match_slides`, `cmd_checkpoint`):**
   - Validated discovered course directories against `OUT` via `_is_path_within_dir(d, OUT)` (and guarded `OUT.exists()`) before planning or dispatching any matching jobs.
2. **High — Split retry fail-closed when `ffmpeg` is missing (`_extract_audio_subchunk`):**
   - Removed the fallback that read raw source recording bytes when `ffmpeg` is unavailable; `_extract_audio_subchunk()` now raises `RuntimeError` immediately so raw `.mp4`/source bytes are never uploaded under mono MP3 chunk approval.
3. **Medium — Local transcription atomic commit & recovery (`transcribe_file`):**
   - Switched `transcribe_file()` from direct `.write_text()` calls to the shared `_commit_transcript_pair()` atomic commit path so local retries publish `.txt`, `.srt`, and `.<stem>.transcript.done.json` and clear stale `.<stem>.transcript.inprogress` markers from failed cloud commits.
4. **Medium — Strict dispatch-time duration validation & reuse (`_is_cloud_approved`, `_split_audio_chunks`, `_transcribe_audio_via_gemini`):**
   - `_is_cloud_approved()` now fails closed whenever `max_duration_sec` or `max_file_durations` applies and `duration_sec` is `None`, non-finite, or `<= 0`.
   - `_split_audio_chunks()` accepts the validated `duration_sec` from `_transcribe_audio_via_gemini()` so chunking uses the exact approved duration without re-probing independently.

### Verification
- `TOLEDO_LECTURES_DIR=/tmp/toledo-test-env TOLEDO_DATA_DIR=/tmp/toledo-test-data pytest -v`: **58 passed** (added 4 offline regression tests in `tests/test_pipeline.py`).
- `python3 -m py_compile toledo_dl.py`: passed.
- `/home/raymundo21/.local/bin/uvx ruff check .`: **All checks passed!**

---

## Antigravity Handoff — Round 2 Audit Fixes (Post-`64949f2`)

Fixed both findings from Codex Round 1 audit:

1. **High — Sync, announcements, index, and search reject course symlinks outside `OUT` (`sync_files`, `sync_announcements`, `build_llm_index`, `cmd_index`, `search_knowledge_base`, `_repair_and_build_chapter_bundles`):**
   - `sync_files()` validates `course_root` and `dest` against `OUT` (not just against `course_root`).
   - `sync_announcements()` validates `course_dir` against `OUT` before creating directories or writing `Announcements.md`.
   - `build_llm_index()`, `cmd_index()`, and `search_knowledge_base()` filter course directories with `_is_path_within_dir(d, OUT)`, and `build_llm_index()` / `_repair_and_build_chapter_bundles()` validate scanned files stay within `cdir`.
2. **Medium — Pending/incomplete transcripts excluded from generic document indexing (`_is_incomplete_transcript_txt`, `build_llm_index`, `_repair_and_build_chapter_bundles`):**
   - Added `_is_incomplete_transcript_txt()` and excluded matching `.txt` files from `all_course_files` in `build_llm_index()` and `_repair_and_build_chapter_bundles()` so in-progress or partially committed transcripts are never published as generic `document_section` passages.

### Verification
- `TOLEDO_LECTURES_DIR=/tmp/toledo-test-env TOLEDO_DATA_DIR=/tmp/toledo-test-data pytest -v`: **59 passed** (added `test_sync_and_index_reject_symlinked_course_root` and extended `test_failed_commit_retains_inprogress_and_rename_carries_markers` in `tests/test_pipeline.py`).
- `python3 -m py_compile toledo_dl.py`: passed.
- `/home/raymundo21/.local/bin/uvx ruff check .`: **All checks passed!**

---

## Antigravity Handoff — Round 3 Audit Fixes (Post-`0d0a38d`)

Fixed both findings from Codex Round 2 audit:

1. **High — Output file symlink overwrite protection (`_safe_write_text_within`, `sync_announcements`, `sync_files`, `build_llm_index`):**
   - Added `_safe_write_text_within(path, root, text)` which rejects symlinks and paths resolving outside `root`, writing atomically via `_replace_bytes()`.
   - Wired `INDEX.md`, `Announcements.md`, `Links.md`, `.url` shortcuts, `passages.jsonl`, `LLM_INDEX.md`, and `llm_manifest.jsonl` through `_safe_write_text_within()`.
2. **Medium — Prune stale `passages.jsonl` when reindexing yields zero passages (`build_llm_index`):**
   - When `course_passages` (or a chapter's `c_pass`) is empty during reindexing, any existing non-symlink `passages.jsonl` within the course directory is unlinked so stale pending/removed text is no longer returned by `search_passages()`.

### Verification
- `TOLEDO_LECTURES_DIR=/tmp/toledo-test-env TOLEDO_DATA_DIR=/tmp/toledo-test-data pytest -v`: **60 passed** (added `test_output_file_symlinks_cannot_overwrite_external_files` and extended `test_failed_commit_retains_inprogress_and_rename_carries_markers` with pre-seeded stale `passages.jsonl` + `search_passages()` check in `tests/test_pipeline.py`).
- `python3 -m py_compile toledo_dl.py`: passed.
- `/home/raymundo21/.local/bin/uvx ruff check .`: **All checks passed!**

---

## Antigravity Handoff — Final Cleanup Pass (Post-`9ad0af7`)

Fixed all 4 remaining findings from Codex Round 3 audit:

1. **High — Guard generated Markdown/JSONL writes beneath symlinked `LLM_Knowledge` subdirectories (`build_llm_index`, `_repair_and_build_chapter_bundles`):**
   - Guarded `LLM_Knowledge/transcripts`, `LLM_Knowledge/pdfs`, and `LLM_Knowledge/Chapters` output writes via `_safe_write_text_within()` and `_is_path_within_dir()` checks so symlinked subdirectories cannot overwrite external files.
2. **Medium — Search ignores symlinked `passages.jsonl` (`search_passages`):**
   - `search_passages()` now skips `passages.jsonl` if it is a symlink or resolves outside the course directory.
3. **Medium — Pre-existing `Transcripts & Slides` pairings ignore pending transcripts (`_repair_and_build_chapter_bundles`):**
   - Verified resolved pairing targets stay within `cdir` and excluded `_is_incomplete_transcript_txt(resolved)` so pending transcripts are never embedded in `CHAPTER_BUNDLE.md`.
4. **Medium — Fixed announcement overwrite regression route (`tests/test_pipeline.py`):**
   - Updated mock route from `limit=100` to `limit=50`, asserted `new_anns == 1`, and added sentinel tests for symlinked `LLM_Knowledge/transcripts`, symlinked `passages.jsonl`, and pre-existing pairings to pending transcripts.

### Verification
- `TOLEDO_LECTURES_DIR=/tmp/toledo-test-env TOLEDO_DATA_DIR=/tmp/toledo-test-data pytest -v`: **60 passed**.
- `python3 -m py_compile toledo_dl.py`: passed.
- `/home/raymundo21/.local/bin/uvx ruff check .`: **All checks passed!**

### Final Sign-off (`6f29cc4`, `e782379`, `5b540f5`)
- Guarded `LLM_Knowledge`, `LLM_Knowledge/Chapters`, individual chapter subdirectories, and chapter migration destinations against symlinks in `_repair_and_build_chapter_bundles()`, preserving the original chapter directory (including `report.md`) when migration onto a symlink is blocked.
- Extended `test_output_file_symlinks_cannot_overwrite_external_files` with sentinel coverage for symlinked `LLM_Knowledge`, `LLM_Knowledge/Chapters`, and blocked chapter migration destinations.
- **Codex audit verdict (`5b540f5`):** `Status: RELEASE_READY` (0 open High/Medium findings; **60 passed**, `py_compile` and `ruff check .` clean).

---

## Local GPU & Cloud PDF Vision Backends + Interactive Sync Prompt

Added opt-in visual PDF scanning (local GPU Ollama vision and cloud Gemini Vision) and integrated it into interactive `toledo-dl sync` and `toledo-dl index`:

1. **Interactive `sync` prompt (`_prompt_pdf_vision_backend`, `cmd_sync`, `_load_sync_preferences`, `_save_sync_preferences`):**
   - Prompts whether to visually scan PDFs during interactive `sync`, explaining that it is recommended for diagram/image-heavy PDFs of high value and not recommended for plain text-heavy PDFs.
   - Offers `[1]` standard text extraction (default), `[2]` local GPU vision (Ollama, free & offline), or `[3]` cloud API (Gemini Vision — works without a GPU; incurs a small API cost).
   - Persists only the backend choice name (`pdf_vision_backend`, never API keys) in `sync_prefs.json`; non-interactive scheduled runs never auto-upload cloud PDFs without interactive approval or `--yes`.
2. **Local GPU & Cloud Vision PDF-to-Markdown (`_extract_pdf_via_ollama_vision`, `_extract_pdf_via_gemini_vision`, `_extract_pdf_markdown_with_pages`, `_plan_pdf_vision_jobs`):**
   - **Local (`--vision-backend ollama`):** Renders pages via `pdftoppm` and sends them to local Ollama (`qwen2.5vl:7b` default), falling back gracefully to `pypdf` if unavailable.
   - **Cloud (`--vision-backend gemini`):** Slices long decks into 20-page chunks, previews PDF/page counts and estimated cost (`_estimate_cloud_cost("pdf-vision", ...)`), enforces scoped `CloudApproval(feature="pdf-vision", provider="gemini")` and header auth (`x-goog-api-key`), and outputs structured slide-by-slide Markdown with arrow notation for diagrams/pathways.
   - **SHA-256 Vision Cache:** Stores `source_sha256`, `vision_backend`, and `vision_model` in `LLM_Knowledge/pdfs/<deck>.md` frontmatter so cached vision Markdown is reused with zero extra API/GPU calls and preserved even on subsequent plain `index` runs.
3. **CLI flags:** Added `--vision-backend {off,ollama,gemini}`, `--vision-model`, `--limit`, `--dry-run`, and `-y`/`--yes` to `toledo-dl index`, and `--pdf-vision-backend {off,ollama,gemini}` to `toledo-dl sync`.

### Verification
- `TOLEDO_LECTURES_DIR=/tmp/toledo-test-env TOLEDO_DATA_DIR=/tmp/toledo-test-data pytest -v`: **68 passed** (added 3 offline tests covering interactive prompt/prefs, cloud Gemini consent/header auth/caching/preservation, and local Ollama GPU vision + fallback).
- `python3 -m py_compile toledo_dl.py`: passed.
- `/home/raymundo21/.local/bin/uvx ruff check .`: **All checks passed!**

### Round 2 Audit Fixes (Post-`34723fd`)
Fixed all 7 findings from Codex PDF Vision Round 1 audit:
1. **High — Ollama loopback proxy bypass (`_call_ollama_pdf_vision_page`):** Uses `urllib.request.build_opener(urllib.request.ProxyHandler({}))` so rendered slide PNGs never route through `http_proxy`.
2. **Medium — Truncated `MAX_TOKENS` guard (`_call_gemini_pdf_vision_chunk`):** Raises `_GeminiMaxTokensError` when `finishReason == "MAX_TOKENS"` so truncated outputs are never cached as `vision_ok`.
3. **Medium — Strict 20-page chunk bound (`_slice_pdf_page_chunks`, `_estimate_pdf_pages_from_bytes`):** Fails closed if `pypdf` is absent or slicing fails on >20-page PDFs.
4. **Medium — Case-insensitive `.pdf` & per-course dedup (`_plan_pdf_vision_jobs`):** Uses `f.suffix.lower() == ".pdf"` and resets `seen_shas` per course.
5. **Medium — Model-aware vision cache (`_read_cached_vision_markdown`, `_plan_pdf_vision_jobs`, `_extract_pdf_markdown_with_pages`):** Checks `vision_model` when an active vision backend is selected while preserving cache reuse for plain `index`.
6. **Medium — Zero-pending `--dry-run` no-op (`cmd_index`):** Returns early on `dry_run` before `build_llm_index()` even when 0 PDFs are pending.
7. **Medium — Sync Stage 5 cancel fallback (`cmd_index`, `cmd_sync`):** Passes `fallback_on_cancel=True` from `cmd_sync()` so declining Gemini at Stage 5 still builds the standard text index with zero cloud calls.
8. **Round 3 / Final — Verified PDF page count via `pypdf` or `pdfinfo` (`_verified_pdf_page_count`, `_slice_pdf_page_chunks`):** Removed regex page estimation completely; Gemini PDF vision now requires an authoritative page count from `pypdf` or `pdfinfo` and fails closed otherwise (eliminating `/ObjStm` and hex-escaped `/Type /Pa#67e` bypasses, with offline regressions in `test_pdf_vision_audit_regressions`).
9. **One-click Local GPU PDF Vision Setup (`_check_local_pdf_vision_deps`, `_ensure_local_pdf_vision_setup`, `cmd_index`):** When selecting local GPU PDF vision (`ollama`), checks for `pdftoppm` (Poppler), `ollama` CLI/daemon, and the vision model (`qwen2.5vl:7b`), and offers an interactive `[y/N]` one-click install/start/`ollama pull` flow (cross-platform: `apt-get`/`curl` on Linux, `brew` on macOS, `winget` on Windows) before falling back cleanly if declined.
- **Verification:** **70 passed** (`pytest -q`), `py_compile`, `ruff check .`, and `git diff --check` all clean.
