# Cloud Transcription & Visual Slide-Matching Architecture Plan

> **Purpose:** Design & safety roadmap for Codex audit before implementing cloud transcription and remaining visual-matching upgrades in `toledo_dl.py`.

---

## 1. Core Safety Principles (No Surprise Bills or Silent Uploads)

1. **API keys are credentials, never implicit consent.**
   - Having `GEMINI_API_KEY`, `GROQ_API_KEY`, or `OPENAI_API_KEY` set in the environment **never** triggers cloud uploads or spending on its own.
   - Default `toledo-dl sync` and `toledo-dl match-slides` remain **strictly local** even when API keys are populated.
2. **Separate explicit opt-ins.**
   - **Cloud transcription** and **cloud slide matching** are controlled independently (`--backend` flags / separate env preferences `TOLEDO_TRANSCRIBE_BACKEND` and `TOLEDO_MATCH_BACKEND`).
   - Enabling cloud transcription must **never** silently enable cloud frame/transcript uploads for slide matching, and vice versa.
   - Passing `--backend local` (or `--backend ollama` / `--backend keyword` / `--backend cpu`) always overrides any cloud environment preference.
3. **Pre-flight transparency & confirmation before any cloud upload.**
   - Before sending data to a cloud provider, print:
     - Selected provider & model
     - Exact data being uploaded (audio chunks vs. transcript excerpts, PDF outlines, and MP4 frames)
     - Pending recording/lecture count and total audio duration (for transcription)
   - Support `--dry-run` (zero requests) and `--limit N` (cap batch size so a student with a large semester backlog cannot accidentally process everything at once).
   - Require interactive `[y/N]` confirmation in a TTY unless `--yes` / `-y` is explicitly passed; abort non-interactively without `--yes` so scheduled/background syncs never upload without explicit opt-in.
4. **Honest billing guidance (EEA / Belgium).**
   - Do not advertise a guaranteed "free tier" to KU Leuven students without verifying current provider terms (e.g., Google's Gemini API terms include a paid-services requirement for apps offered in the EEA/Belgium). Always make students aware that API usage may incur charges.
5. **Secret hygiene.**
   - API keys live strictly in environment variables (never saved to plaintext config files), are transmitted only via HTTP headers (`x-goog-api-key`, `Authorization: Bearer`), never in URL query strings (`?key=`), and are scrubbed by `_redact_secrets()`.

---

## 2. Out-of-the-Box User Experience Matrix

| Machine & Setup | Command | What Happens |
|---|---|---|
| **Regular laptop** (no GPU, no API keys) | `toledo-dl sync` | Downloads all lectures, PDFs, files, and announcements. **Skips transcription immediately** (no silent CPU hang) and clearly reports pending recordings + how to opt into a cloud API (or explicit `--backend cpu`). Slide matching runs locally via CPU keyword + outline + chronology fallback. |
| **Regular laptop** (no GPU, API key set) | `toledo-dl sync` | Still downloads everything locally and leaves API keys untouched (no silent spending). Reports pending recordings. |
| **Regular laptop** (no GPU, API key set) | `toledo-dl transcribe --backend <provider> [--course X] [--limit N]` | Shows pre-flight summary (provider, file count, total audio duration), asks `[y/N]` confirmation, then runs resumable chunked transcription and writes `.txt` + `.srt`. |
| **Regular laptop** (no GPU, API key set) | `toledo-dl match-slides --backend gemini\|openai [--course X]` | Shows pre-flight summary (provider, lecture/video count, data types), asks `[y/N]` confirmation, then matches using 6 evenly spaced MP4 frames + cached all-slide PDF outlines + transcript + chronology. |
| **High-end GPU laptop** (CUDA GPU + API keys set) | `toledo-dl sync` | Automatically detects CUDA GPU and runs fast local `faster-whisper` transcription. Slide matching stays local (Ollama → CPU fallback). **API keys remain completely untouched.** |
| **High-end GPU laptop** (wants API for one class) | `toledo-dl transcribe --course "X" --backend <provider>` | Explicitly overrides local GPU for that course only, after pre-flight summary and `[y/N]` confirmation. |

---

## 3. Slide-to-PDF Matching (`match-slides`)

### Accuracy Ranking by Mode
1. **Cloud Multimodal Vision (`--backend gemini|openai`, highest accuracy):** Samples 6 evenly spaced 1280px JPEG frames across the sibling `.mp4` (`10%, 25%, 40%, 55%, 70%, 85%`) — sharp enough for both full-screen slide decks and split-screen Kaltura layouts — paired with cached all-slide PDF outlines, sampled transcript excerpts, and Codex's chapter-floor chronology rule. Enforces **sustained coverage** (ignores brief 1-frame refreshers of older chapters; links two PDFs only on genuine chapter transitions).
2. **Local GPU (`--backend local|ollama`):** Uses local Ollama model over transcript excerpts, all-slide PDF outlines, and chronology rules.
3. **Zero-Hardware CPU (`--backend keyword` / automatic local fallback):** Deterministic keyword/title + all-slide outline + chronology matching with zero ML/GPU requirements.

### Status
- **Already implemented in workspace (`toledo_dl.py`, 46 offline tests passing):**
  - Explicit opt-in lockdown (`auto` stays local even when keys exist; `--backend local` overrides env).
  - Pre-flight summary, `--dry-run`, and `[y/N]` / `--yes` confirmation guard.
  - 6-frame 1280px MP4 sampler (`ffmpeg` `image2pipe`, no temp files), cached all-slide PDF outlines (`.pdf_outlines.json`), sustained-coverage prompt, header-only auth, and `_redact_secrets()` scrubbing.
- **Planned follow-up polish (after Codex audit):**
  - Optional `--limit N` on `match-slides` for large course backlogs.
  - Denser MP4 frame sampling around ambiguous mid-lecture chapter transitions to pinpoint slide-to-timestamp boundaries.

---

## 4. Cloud Transcription (`transcribe` & `sync`) — Implemented (Gemini First)

1. **Gemini-first opt-in cloud backend (`--backend gemini`):**
   - `toledo-dl transcribe --backend gemini [--course X] [--limit N] [--max-minutes 180] [--chunk-seconds 900] [--dry-run] [--yes]`
   - Single `GEMINI_API_KEY` UX alongside Gemini visual slide matching, with **separate** explicit upload approvals (`feature="transcribe"` vs `feature="match-slides"`), `model` + `data_types` + `course_dir` + SHA-256 + duration validation, and a `180`-minute default `--max-minutes` cap (unknown-duration recordings are deferred).
   - OpenAI/Groq Whisper (`/audio/transcriptions`) remains a future optional alternative.
2. **Resumable chunked processing & bounded `MAX_TOKENS` recovery:**
   - Splits long recordings via `ffmpeg` into uniform mono 16 kHz MP3 chunks (`900s` default, configurable via `--chunk-seconds`, 32 kbps).
   - Stores completed chunk outputs privately under `TOLEDO_DATA_DIR/transcribe_cache/` (POSIX `0700`/`0600` permissions), keyed by `(version, recording_sha256, backend, endpoint, model, language, audio_format, chunk_seconds)`.
   - On `MAX_TOKENS`, automatically splits the affected chunk in half (bounded to depth 3 / 60s minimum) and caches each sub-chunk for resume.
   - Clamps all timestamps to finite, strictly within-chunk and globally monotonic bounds, and commits `.txt`, `.srt`, and `.<stem>.transcript.done.json` atomically while retaining `.<stem>.transcript.inprogress` on failure.
3. **Explicit CPU & GPU device selection + slow-CPU safeguard:**
   - `--backend cpu` forces `device="cpu"` even when CUDA is detected; `--backend gpu` forces `device="cuda"`.
   - On CPU-only laptops without a GPU and without an opted-in cloud backend, `transcribe` (`auto`) and `sync` skip transcription immediately and print clear next steps (`--backend gemini` or explicit `--backend cpu`).
4. **`toledo-dl doctor` clarity:**
   - Reports local CUDA GPU and Ollama status alongside cloud API readiness, explicitly distinguishing **"detected in environment (connection not verified)"** from an actually verified connection.
