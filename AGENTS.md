# AGENTS.md — `toledo-dl-portable`

Shared project instructions for **Google Antigravity** and **OpenAI Codex** working on this repository.

## Project Structure
- `toledo_dl.py` — KU Leuven Toledo (Blackboard Ultra / Kaltura) downloader, transcriber (local GPU/CPU Whisper + opt-in resumable Gemini API), visual/chronology slide matcher, and LLM knowledge-base indexer/search.
- `bin/`, `install.sh`, `install-mac.sh`, `INSTALL.bat`, `windows/`, `systemd/` — CLI launchers (`toledo`, `toledo-dl`, `toledo-sync`, `toledo-daily-sync`), installers, and systemd timer units.
- `tests/` — Offline `pytest` suite (`test_cli.py`, `test_degradation.py`, `test_doctor.py`, `test_installers.py`, `test_pipeline.py`, `test_portability.py`, `test_security_and_recovery.py`, `test_vram_cleanup.py`).
- `pyproject.toml` — Ruff (`select = ["E", "F", "W"]`, `ignore = ["E501"]`, `target-version = "py310"`) and pytest config.
- `CHANGES.md` — Changelog and latest Antigravity -> Codex handoff notes (always update after changes).
- `CODEX_AUDIT.md` — Codex audit findings and handoff priorities.

## Hard Rules & Intentional Quirks (Do Not Break)
1. **Stdlib + optional `pypdf`:** Do not add new third-party dependencies for slide matching or cloud APIs. System `/usr/bin/python3` may not have `pypdf` installed, so `pypdf = None` fallback paths must always remain functional.
2. **Explicit `encoding="utf-8"` on all text I/O:** `tests/test_portability.py` inspects `toledo_dl.py` via Python `ast` and fails if any non-binary `open()`, `.read_text()`, or `.write_text()` call omits `encoding=`.
3. **Never touch real user data in tests:** Always override `TOLEDO_LECTURES_DIR` and `TOLEDO_DATA_DIR` to `/tmp/...`. Never read or modify `~/Desktop/University`, `~/University`, or `~/.local/share/toledo-dl` in tests.
4. **Offline tests only:** Never make live Gemini, OpenAI, Ollama, Toledo, or Kaltura network requests in `pytest`. Mock `urllib.request.urlopen` and API helpers.
5. **Atomic writes & non-destructive operations:** Write JSON, `.txt`, `.srt`, and completion markers atomically via `_replace_bytes()` / `_write_json()`. Never delete user source files or unrelated personal notes.
6. **Credentials ≠ upload consent:** API keys (`GEMINI_API_KEY`, `OPENAI_API_KEY`) live strictly in env vars, use HTTP headers only (never `?key=`), and must never trigger cloud uploads without explicit opt-in and approval.

## Verification Commands
Run these after any edit before handing off or committing:
```bash
TOLEDO_LECTURES_DIR=/tmp/toledo-test-env TOLEDO_DATA_DIR=/tmp/toledo-test-data pytest -v
python3 -m py_compile toledo_dl.py
/home/raymundo21/.local/bin/uvx ruff check .
```

## Multi-Agent Coordination Rules (Antigravity + Codex)
- **Take turns:** Wait until one agent finishes and commit a checkpoint (`git commit -am "wip: ..."`) before prompting the other in the same directory, or use `git worktree` for parallel work.
- **Audit without stylistic churn:** When reviewing the other agent's diff, focus on correctness, security, edge cases, race conditions, Windows/Linux portability, and test gaps—do not rewrite working structure just for style.
- **No CLI wrapping:** Keep Antigravity and Codex side-by-side; do not script or wrap the `agy` CLI from Codex.

## Release Goal & Automated Loop Contract
The goal of the Antigravity <-> Codex loop is to make `toledo-dl-portable` **reliable enough for daily use on a real KU Leuven course**, not to chase endless theoretical perfection.

### 1. Target End-to-End Workflow (What "Done" Means)
- `toledo-dl doctor` accurately reports environment, CUDA/CPU, and API key status without implying unverified connectivity.
- `toledo-dl sync --course "<Course>"` idempotently downloads PDFs, course files, announcements, and recordings without duplicating, clobbering, or escaping the course directory.
- `toledo-dl transcribe` works reliably on local GPU (`--backend gpu`), explicit CPU (`--backend cpu`), and opt-in Gemini (`--backend gemini`, with `--limit`, `--max-minutes`, `--chunk-seconds`, chunk resume, and atomic `.txt`/`.srt`/`.done.json` commits).
- `toledo-dl match-slides` pairs lectures to PDFs cleanly (local fallback + opt-in cloud vision) while preserving chapter chronology.
- `toledo-dl index`, `toledo-dl search`, and `toledo-local` (`dashboard.py`) work cleanly on synced courses without seeing partial/in-progress transcripts.

### 2. Scope Freeze
- **No new features** (e.g., no new providers, no UI redesigns, no flashcards) unless required to fix a release-blocking bug.
- **No stylistic rewrites** of working code.

### 3. Severity & Stop Condition
- **Codex audit rule:** Log only **concrete, reproducible High or Medium bugs** (with exact reproduction or test gap) that break the target workflow, data integrity, path/symlink safety, or cloud upload consent. Do not block on stylistic preferences or low-impact hypotheticals.
- **Exit condition (STOP):** The loop is **DONE** when:
  1. `CODEX_AUDIT.md` reports **`0` open High/Medium findings** (`Status: RELEASE_READY`), AND
  2. All verification checks (`pytest -v`, `py_compile`, `ruff check .`) pass with zero failures.
- **Iteration cap:** Stop after **3 rounds** maximum and hand back to the user if any disagreement or real-world ambiguity remains.
- **Final human sign-off:** After `RELEASE_READY`, the user runs 1 real end-to-end KU Leuven course test (plus 1 real Gemini lecture test if using cloud transcription) before calling the release finished.
