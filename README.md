# Toledo DL

Keep a local copy of your KU Leuven Toledo course materials and search it by slide page or lecture timestamp. Toledo DL syncs the courses your account can access and builds an index you can use from the terminal or with an AI assistant that can read your course folder.

## Install and start

Download or clone this repository. On Windows, extract it and double-click `INSTALL.bat`. On Ubuntu or Debian, open a terminal in the repository and run `bash install.sh`. On macOS, run `bash install-mac.sh`.

The installers set up the Python environment and browser integration. They also install system dependencies where supported. Windows uses Winget when available. Ubuntu and Debian may ask for your administrator password. macOS may ask you to install Homebrew.

Open a fresh terminal after installation (on Windows, open **Command Prompt** or **PowerShell** from the Start menu; on macOS, open **Terminal** from Spotlight; on Ubuntu, open **Terminal**) and run:

```bash
toledo-sync run
```

If your Toledo session is missing or expired, a browser window opens. Sign in with your KU Leuven account and complete MFA if prompted. The browser closes and the sync continues. Automatic daily sync is **OFF by default**—installing or running `toledo-sync run` will never turn on background scheduling until you explicitly run `toledo-sync enable`. Toledo DL saves a local browser session. Keep its profile and session files private.

By default, your archive is in `Desktop/University` under your home directory, or `University` if you have no Desktop folder. You can override it with `TOLEDO_LECTURES_DIR`. If a command is missing or the browser cannot start, run `toledo-dl doctor`.

## Daily sync and progress

| Command | What you get |
|---|---|
| `toledo-sync run` | Run a sync now and see progress in the terminal |
| `toledo-sync enable` | Turn on the daily schedule |
| `toledo-sync disable` | Turn off the daily schedule |
| `toledo-sync status` | See the scheduler status |
| `toledo-sync follow` | Watch the current stage without opening system logs |
| `toledo-dl sync-status` | Show the current stage or the last completed result |

The schedule runs at 10:30 local time. Linux also has a startup timer, and macOS runs the sync when its LaunchAgent loads. Windows needs a logged-in desktop session for its scheduled task. Keep the session open if a new KU Leuven login is needed.

During a download, Toledo DL shows the recording's position in the queue. Downloads handled by yt-dlp also show the current file's percentage and ETA. Then you can see when transcription and search indexing start. Press Ctrl+C in the **follow** window to close the watcher.

The completion summary reports recordings first seen since a prior scan, downloaded files and recordings, revised attachments, and failed downloads by course. The first scan establishes a baseline. A recording first seen later could be an older recording that only recently became visible to your account. The count describes what Toledo DL observed. Desktop notifications require a desktop session with notifications available.

## Download options

Use `toledo-sync run` for the standard workflow. These commands give you more control after signing in:

| Command | What it does |
|---|---|
| `toledo-dl sync --course "Multivariate statistics" --dry-run` | Preview files and recordings |
| `toledo-dl sync --course "Multivariate statistics" --files-only` | Sync course files without downloading lecture recordings |
| `toledo-dl sync --audio-only --no-transcribe` | Download lecture audio and defer transcription |
| `toledo-dl sync --no-transcribe` | Download course materials and recordings, then build the index |
| `toledo-dl sync --check-revisions --course "Multivariate statistics"` | Compare eligible attachments even when Toledo supplies no change indicators |
| `toledo-dl calendar --upcoming` | Show upcoming classes |

The default download mode prefers MP4 video and extracts MP3 audio. Availability depends on the lecturer's publishing settings and your account's access. Some external links are saved as shortcuts.

### Revised slides and assignments

An ordinary sync checks existing attachments when Toledo reports a change or provides an HTTP validator such as an ETag. For items without either, `--check-revisions` fetches eligible course attachments and compares their bytes. This uses more bandwidth. A changed attachment keeps its usual path, and the previous bytes are saved under `revisions/` in the Toledo DL data directory. The sync log prints that backup path.

## How transcription works

Toledo DL transcribes downloaded recordings locally with faster-whisper or via opt-in Gemini cloud transcription. It writes a transcript (`.txt`) and timestamped subtitles (`.srt`) next to the recording. The first local transcription may need to download the Whisper model.

- **A compatible NVIDIA CUDA GPU was detected.** The standard runner uses GPU transcription (`medium` model, typically **~3 minutes per 1-hour lecture** on an RTX GPU). If CUDA fails when Whisper starts, the runner leaves that recording pending and reports the failure.
- **No compatible CUDA GPU was detected.** `toledo-dl sync` and `toledo-dl transcribe` (default `auto` mode) defer transcription instead of silently running slowly on CPU, and report how many recordings are pending.
- **Want to transcribe locally on CPU anyway?** Run `toledo-dl transcribe --backend cpu --course "Multivariate statistics" --model small`. With `small` (`int8`), expect roughly **~10 minutes per 1-hour lecture** on a fast desktop CPU, or **~20–35+ minutes** on a slower laptop CPU (accuracy depends on the model size, not the CPU; pass `--model medium` if you want higher accuracy on technical terms). You can restrict the run to one recording with `--file "Lecture 08"`, and the search index updates automatically afterward.
- **Optional / Advanced (bringing external transcripts or cloud API):** You can drop matching `.txt` + `.srt` files beside any recording and run `toledo-dl index`, or explicitly pass `--backend gemini` / `--backend openai` on `toledo-dl transcribe` if you have an API key set.

> **Dutch lectures (`--lang nl`):** Transcription defaults to English (`--lang en`). For Dutch courses, pass `--lang nl` (e.g. `toledo-dl sync --lang nl` or `toledo-dl transcribe --lang nl`)—otherwise Whisper will try to decode Dutch speech as English and accuracy will drop sharply.

## Search your archive

```bash
toledo-dl search "biplot projections" --course "Multivariate statistics"
```

Open the course's `INDEX.md` to browse its sources. The search command reads `LLM_Knowledge/passages.jsonl` and returns relevant PDF pages, transcript timestamps, or document excerpts. For an AI assistant with file access, point it at your archive and ask it to cite the original PDF page or lecture timestamp. Run `toledo-dl index` to rebuild search indexes from files already on disk.

Keep personal notes outside the generated `LLM_Knowledge/Chapters` and `Transcripts & Slides` folders. Toledo DL rebuilds those folders as the archive changes.

## Personalized Study Profile (`STUDY_PROFILE.md`)

Toledo DL creates a `STUDY_PROFILE.md` file in your archive root (and never overwrites it on later syncs). When you open your archive folder in an AI assistant with local file access (Antigravity, Codex, Claude Code), it is instructed to:
- **Deduce difficulty & pace from your interactions** (even if you don't explicitly say *"this is hard"*—for example, when you ask repeated follow-ups or spend extra time on a topic) and update `STUDY_PROFILE.md` automatically.
- **Build personalized schedules** by combining `STUDY_PROFILE.md` with `DEADLINES_AND_EXAMS.md`, `CALENDAR.md`, and each course's `Chapters/` density.

## Lecture and slide matching

When nearby numbered lectures agree on a numbered chapter, Toledo DL considers that chapter and later chapters for the next lecture in the same recording series. A chapter can span several lectures. Recordings labeled as a review, recap, revision, or exercise can match earlier chapters. When there is no clear sequence, the matcher uses the lecture topic and available slides.

To update stale slide matches in a course and refresh its study bundles, run:

```bash
toledo-dl match-slides --course "Multivariate statistics"
toledo-dl index --course "Multivariate statistics"
```
