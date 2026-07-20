# YouTube Recorder · By Leoluchino

English | [中文](README.zh-CN.md)

YouTube subscriptions → automatic transcription → AI-written articles (with smart screenshots) → your Obsidian vault. A local-first macOS menu-bar app. v0.3.5

## What it does

Subscribe to YouTube channels (or paste a single video link). On a schedule you control, YouTube Recorder discovers new videos, grabs captions when available or transcribes the audio, rewrites the full spoken transcript into a readable Chinese article (faithful, traceable to timecodes, never truncating the middle), captures relevant on-screen frames, and writes everything into your Obsidian vault or a plain folder — with the original transcript preserved in a collapsible block.

## Highlights

- **Menu-bar resident app** — closing the window keeps the tray and scheduler alive; native window (no browser).
- **Two transcription paths** — MacWhisper Watch Folder (local, free) as primary with automatic timeout failover to the OpenAI audio API; files over 24 MB are compressed, then split into segments with 15-second overlaps and merged by timecode.
- **Faithful AI articles** — chunked full-transcript processing (parallel), no invented facts, per-section source mapping, user-editable extra prompt, original transcript appended as a foldable callout.
- **Smart screenshots** — rule-based visual-cue recall plus LLM inference for narration-style videos; multi-frame extraction, sharpness pick, perceptual-hash dedup, density slider 1–5.
- **Reports library** — read / horizontal timeline / manage modes, auto tags with tag filtering, per-article "ask AI" Q&A and one-click re-summarize, trash with 3-day restore.
- **Process-ordered settings** — the settings page doubles as an onboarding guide: Discover → Download → Transcribe → Compose → Read & Save → AI credentials.
- **Reliability** — SQLite state machine, idempotent by video ID, atomic writes with read-back verification, structured JSONL logs with secret redaction, per-class retry policy, launchd scheduling that survives sleep.
- **Privacy & safety** — API keys live in the macOS Keychain only; GUI binds to 127.0.0.1 with CSRF protection and vault path-escape guards. Designed for personal, private use of content you have access to.

## Requirements

macOS (Apple Silicon), Python 3.10+, `yt-dlp`, `ffmpeg` (visuals & API chunking), and optionally MacWhisper Pro (Watch Folders). Python deps: `pyyaml certifi flask markdown pillow anthropic openai pywebview rumps`.

## Quick start

```bash
cd app
pip3 install --user pyyaml certifi flask markdown pillow anthropic openai pywebview rumps yt-dlp
python3 -m youtube_recorder.cli init
python3 -m youtube_recorder.cli channels add "https://www.youtube.com/@SomeChannel"
python3 -m youtube_recorder.cli run --once      # one full pass
python3 -m youtube_recorder.cli tray            # menu-bar app + GUI at 127.0.0.1:8765
./scripts/build_app.sh                          # build the double-clickable .app
```

Add API keys via the in-app Settings page (stored in Keychain), or:

```bash
security add-generic-password -s "ytrec-openai" -a "$USER" -w "sk-..."
```

## Layout

```
app/                    Python package (pipeline, GUI, tray, adapters)
app/scripts/            .app bundle build + launchd plist
YouTube Recorder-设计文档-v0.2.md   architecture (Chinese)
P1-spike结论-*.md       MacWhisper watch-folder spike findings
```

## License & content note

Personal-use tool. It does not bypass any access controls; downloaded media and transcripts are for your own private library — respect YouTube's Terms of Service and copyright law in your jurisdiction.
