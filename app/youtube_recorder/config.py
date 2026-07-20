"""Config loading / validation / default generation for YouTube Recorder.

Design rules (v0.2):
- config.yaml is validated on load; invalid values raise ConfigError with a
  human-readable message rather than half-applying.
- API keys are NEVER stored here — they come from macOS Keychain (see creds.py, P8).
- Writes go through a temp file + atomic rename.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import APP_NAME, AUTHOR
from .paths import CONFIG_FILE

VALID_TRANSCRIBERS = ("macwhisper_watch_srt", "openai_audio", "whisper_cpp", "skip")
VALID_ARTICLE_MODES = ("edited_article", "faithful_cleanup", "wiki_note")
VALID_DIALOG_POLICY = ("on_new_videos", "always", "never")
VALID_ON_DIALOG_ERROR = ("run", "skip")
VALID_LAYOUTS = ("vault", "folder_split", "folder_flat")
VALID_LANGS = ("zh", "en")
VALID_AI_ROUTES = ("auto", "openai", "anthropic")


class ConfigError(ValueError):
    pass


DEFAULT_CONFIG: dict[str, Any] = {
    "app": {"name": APP_NAME, "branding": AUTHOR,
            "language": "zh"},  # zh | en
    "ai": {  # 各环节分别用哪家 API：auto=用已配置的（都配了优先 OpenAI）
        "article": "auto",   # 整理成文
        "visuals": "auto",   # 截图智能召回
        "qa": "auto",        # 报告内问答
    },
    "vault": {
        "root": "",  # set via GUI/CLI before vault writes are enabled
        "raw_subdir": "20-Raw/YouTube",
        "wiki_subdir": "30-Wiki",
        "attachments_subdir": "40-Attachments/YouTube",
        "governance_mode": "A",  # user decision 2026-07-19: narrow exception
        # 保存模式：vault=Obsidian库(分层) | folder_split=独立文件夹(Raw+Wiki)
        #          | folder_flat=独立文件夹(纯平铺，原文折叠随文章，无 Raw 副本)
        "layout": "vault",
    },
    "discovery": {
        "interval_minutes": 120,
        "max_new_videos_per_run": 5,
        "include_shorts": False,
        "include_live": False,
        "min_duration_sec": 90,
        "default_not_before": "subscription_added_at",
        # 运行模式：False=发现后直接自动处理（默认）；True=先列出等确认。
        # 两种模式下 Queue 里都可随时跳过单条视频。
        "review_gate": False,
    },
    "transcription": {
        "primary": "macwhisper_watch_srt",
        "fallback": "openai_audio",
        "inbox_dir": str(Path.home() / "Coding" / "YouTube Recorder" / "macwhisper-inbox"),
        "timeout_minutes": 180,
        "collect_wait_minutes": 45,  # 投稿后守着轮询回收 SRT 的最长时间
        "require_timestamps": True,
        "language": "auto",
        # openai_audio adapter
        "api_model": "whisper-1",
        "chunk_overlap_sec": 15,
        "max_upload_mb": 24,
    },
    "article": {
        "enabled": True,
        "mode": "edited_article",  # user decision 2026-07-19
        "preserve_source_mapping": True,
        "max_cost_per_video_usd": 2.0,
        # 用户附加 prompt：整理成文时追加到系统提示词末尾（GUI 可编辑）
        "custom_prompt": "",
        # AI 改写在前，原始文稿以可折叠 callout 附在文末
        "append_original": True,
        # 原文保留档位：0=关闭(自由整理)；50/60/70/80/90/100=正文中至少
        # 该比例字符逐字来自原文（程序保证，AI 只选句和写过渡）
        "verbatim_pct": 70,
    },
    "visuals": {
        "enabled": True,
        "image_density": 3,  # 1..5 slider
        "strict_fill": False,
        "search_before_sec": 3,
        "search_after_sec": 5,
        "max_width_px": 1600,
        "jpeg_quality": 85,
    },
    "scheduler": {
        "confirm_dialog": "on_new_videos",  # user decision 2026-07-19
        "confirm_timeout_sec": 30,
        "on_dialog_error": "run",
        "run_at_load": True,
        "hours": [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22],
    },
    "retention": {
        "trash_days": 3,   # 删除的文章在回收站保留天数，到期真删
        "keep_audio_days": 7,
        "keep_video_segments_days": 2,
        "keep_failed_work_days": 30,
        "keep_original_transcript": "forever",
    },
    "budget": {
        "max_monthly_cloud_usd": 20.0,
    },
}


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=dict)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def inbox_dir(self) -> Path:
        return Path(self.get("transcription.inbox_dir", "")).expanduser()

    @property
    def vault_root(self) -> Path | None:
        root = self.get("vault.root", "")
        return Path(root).expanduser() if root else None


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def validate(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    def check(dotted: str, ok: bool, msg: str) -> None:
        if not ok:
            errors.append(f"{dotted}: {msg}")

    t = data.get("transcription", {})
    check("transcription.primary", t.get("primary") in VALID_TRANSCRIBERS,
          f"must be one of {VALID_TRANSCRIBERS}")
    fb = t.get("fallback")
    check("transcription.fallback", fb is None or fb in VALID_TRANSCRIBERS,
          f"must be null or one of {VALID_TRANSCRIBERS}")
    check("transcription.chunk_overlap_sec",
          isinstance(t.get("chunk_overlap_sec"), int) and 0 <= t["chunk_overlap_sec"] <= 60,
          "must be int 0..60")

    a = data.get("article", {})
    check("article.mode", a.get("mode") in VALID_ARTICLE_MODES,
          f"must be one of {VALID_ARTICLE_MODES}")
    check("article.verbatim_pct",
          a.get("verbatim_pct", 70) in (0, 50, 60, 70, 80, 90, 100),
          "must be one of 0/50/60/70/80/90/100")

    v = data.get("visuals", {})
    check("visuals.image_density",
          isinstance(v.get("image_density"), int) and 1 <= v["image_density"] <= 5,
          "must be int 1..5")

    s = data.get("scheduler", {})
    check("scheduler.confirm_dialog", s.get("confirm_dialog") in VALID_DIALOG_POLICY,
          f"must be one of {VALID_DIALOG_POLICY}")
    check("scheduler.on_dialog_error", s.get("on_dialog_error") in VALID_ON_DIALOG_ERROR,
          f"must be one of {VALID_ON_DIALOG_ERROR}")

    hours = data.get("scheduler", {}).get("hours", [])
    check("scheduler.hours",
          isinstance(hours, list) and hours and
          all(isinstance(h, int) and 0 <= h <= 23 for h in hours),
          "must be a non-empty list of ints 0..23")

    d = data.get("discovery", {})
    check("discovery.max_new_videos_per_run",
          isinstance(d.get("max_new_videos_per_run"), int) and d["max_new_videos_per_run"] >= 1,
          "must be int >= 1")

    check("app.language",
          data.get("app", {}).get("language", "zh") in VALID_LANGS,
          f"must be one of {VALID_LANGS}")
    for k in ("article", "visuals", "qa"):
        check(f"ai.{k}",
              data.get("ai", {}).get(k, "auto") in VALID_AI_ROUTES,
              f"must be one of {VALID_AI_ROUTES}")
    check("vault.layout",
          data.get("vault", {}).get("layout", "vault") in VALID_LAYOUTS,
          f"must be one of {VALID_LAYOUTS}")

    root = data.get("vault", {}).get("root", "")
    if root:
        p = Path(root).expanduser()
        check("vault.root", p.is_absolute(), "must be an absolute path")
    return errors


def load(path: Path = CONFIG_FILE) -> Config:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            user_data = yaml.safe_load(f) or {}
        if not isinstance(user_data, dict):
            raise ConfigError(f"{path} is not a YAML mapping")
    else:
        user_data = {}
    data = _merge(DEFAULT_CONFIG, user_data)
    errors = validate(data)
    if errors:
        raise ConfigError("invalid config:\n  " + "\n  ".join(errors))
    return Config(data)


def save(cfg: Config, path: Path = CONFIG_FILE) -> None:
    errors = validate(cfg.data)
    if errors:
        raise ConfigError("refusing to save invalid config:\n  " + "\n  ".join(errors))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg.data, f, allow_unicode=True, sort_keys=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)  # atomic
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_default_if_missing(path: Path = CONFIG_FILE) -> bool:
    if path.exists():
        return False
    save(Config(dict(DEFAULT_CONFIG)), path)
    return True
