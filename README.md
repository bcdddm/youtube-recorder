# YouTube Recorder · By Leoluchino

English | [中文](README.zh-CN.md)

YouTube subscriptions → automatic transcription → AI-written articles (with smart screenshots) → your Obsidian vault. A local-first macOS menu-bar app. Current version **v0.4.2**.

## What it does

Subscribe to YouTube channels (or paste a single video link). On a schedule you control, YouTube Recorder discovers new videos, grabs captions when available or transcribes the audio, rewrites the full spoken transcript into a readable Chinese article (faithful, traceable to timecodes, never truncating the middle), captures relevant on-screen frames by content semantics, and writes everything into your Obsidian vault or a plain folder — with the original transcript preserved in a collapsible block.

## v0.4 highlights

- **Verbatim retention tiers** — 0/40/50/60/70/80/90/100%: at least that share of body characters is copied **verbatim from the transcript**, guaranteed by the program (the AI only picks sentences and writes bridges; picked sentences are copied as-is and topped up automatically; the measured ratio goes into frontmatter). Tiers below 70% allow sentence reordering; meaningless sentences may be dropped; kept sentences may only be "corrected" (fewer than 3 changed words), never rewritten.
- **AI re-punctuation** — re-segments the spoken stream with proper punctuation, verified char-by-char (punctuation stripped) so content cannot change; falls back to rule-based punctuation on any mismatch. Plus deterministic filler removal and a typo-proofread pass.
- **Event-based sections** — one event per section, max 600 characters (a ≤2-minute read), auto-split when longer.
- **Smart screenshots, density 1–5** — rule-based cue recall plus LLM inference for narration-style videos; **level 5 guarantees at least one image per section** (auto-captured at the section's timestamp when no cue matches, distributed one-per-section at write time).
- **Reports library** — horizontal timeline as the default view, multi-select group pills, tag cloud (measured two-row collapse, hover expands to 3× before scrolling), **AI synonym-tag merging** (e.g. "AI tech / AI investing → AI"; display-layer mapping, source data untouched), daily intel digest (full point coverage with 【title】 citations, 30-day cache), per-article "Ask AI" and one-click re-summarize, trash with 3-day restore.
- **Per-stage AI routing** — compose / shot recall / Q&A can each use OpenAI or Anthropic with a concrete model, refreshed live from each provider's models API using your key.
- **Bilingual UI (zh/en)** — pick the language in Settings section ⓪; it applies instantly; the language block itself stays bilingual.
- **Release-channel updates** — in-app "Check for updates" follows published GitHub Release tags only; unpublished commits on main are never pushed to users.
- **Channel groups** — a channel can belong to multiple groups, filtering both reports and digest scope; finished queue items link straight to their article page.

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

## Reliability & safety

SQLite state machine (idempotent by video ID), atomic writes with read-back verification, structured JSONL logs with secret redaction, per-class retry policy, launchd scheduling that survives sleep. API keys live in the macOS Keychain only; the GUI binds to 127.0.0.1 with CSRF protection and vault path-escape guards.

## Layout

```
app/                    Python package (pipeline, GUI, tray, adapters)
app/scripts/            .app bundle build + launchd plist + release updater
YouTube Recorder-设计文档-v0.2.md   architecture (Chinese)
P1-spike结论-*.md       MacWhisper watch-folder spike findings
```

## License & content note

Personal-use tool. It does not bypass any access controls; downloaded media and transcripts are for your own private library — respect YouTube's Terms of Service and copyright law in your jurisdiction.
