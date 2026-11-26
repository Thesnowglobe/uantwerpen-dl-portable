# Release acceptance for Toledo DL and Canvas DL

Each downloader is a separate product with its own archive and index. Both must produce a current, browsable degree archive that an AI assistant with local folder access can navigate and cite.

| Area | Acceptance check |
| --- | --- |
| Setup and sign-in | On Windows and Ubuntu, a fresh install on a compatible NVIDIA machine supports local GPU transcription without a separate setup command. The first GPU transcription fetches the Whisper model. A machine without compatible hardware can use sync without installing the local transcription stack. Explicit CPU transcription can set up its dependencies when needed. A startup sync with an expired session opens platform sign-in and resumes after authentication. |
| Downloading | Normal sync checks every discovered course, including older courses, for accessible attachments, announcements, links, and recordings. Keep current shortcut behavior and both MP4 and MP3 when available. Detect silent attachment replacements, update the usual path, and preserve the previous version. Report failed downloads and retry them on the next sync. Selecting specific lectures to download is lower priority. |
| Storage | Preserve each product’s existing course layout. In Toledo, keep the current platform-derived attachment folders and existing recording folders. Keep transcript and subtitle sidecars with the recording. Maintain separate archives for Toledo and Canvas. Verify Canvas’s current layout in its own repo before changing storage behavior. |
| Transcription | Interactive sync and manual transcription let the user choose GPU, CPU, or API. Offer local GPU mode for compatible NVIDIA hardware. Offer the full pending backlog, including recordings from earlier syncs. Keep skipped and failed work pending. Cloud transcription offers Gemini and OpenAI. |
| Slide matching | Interactive sync offers a separate matching backend choice. Cloud matching offers Gemini, OpenAI, and Anthropic Claude. Keep one full transcript and link it to both consecutive PDF decks when the lecture teaches from both. A passing mention alone does not qualify. Deck-level links satisfy this release. |
| Indexing | Index supported documents after downloading them. Add each completed transcript automatically, including after standalone transcription, and incorporate slide links when matching completes. Exclude incomplete transcripts. |
| Archive use | Give each archive a clear starting index and traceable source links. A user or AI with folder access can find the original document, slide page, or lecture timestamp where available. Terminal search is secondary to archive navigation. |
| Daily operation | Keep manual sync and opt-in daily scheduling with enable and disable controls. Use the latest interactive sync’s backend choices as scheduled defaults. When an interactive approval prompt is available during the same run, offer the API batch there. Otherwise leave API work pending and complete any available download and indexing work. |

For every cloud batch, preview the provider, model, files or data types, and estimated cost before asking for explicit approval. Approval for one stage or run does not carry over. API keys can come from the environment or a hidden prompt for the current run. Keep keys out of saved preferences and logs.

## When the loop ends

Use the repository’s bounded review loop for each set of changes. Review a fixed snapshot against the acceptance checks and reproduce material failures with offline tests. Stop a review round when the checks pass and the open release blockers are closed. Bring unresolved blockers back to the user after three rounds.

Before calling the release finished, ask the user to run a real sync with each downloader. Windows and Ubuntu are the hands-on test platforms, and both test machines have NVIDIA GPUs. Require the user’s sign-off for both separate products. Exercise unavailable-GPU and API approval behavior with offline tests. AMD GPU acceleration is outside this release.
