"""Structured JSONL logging (v0.2 design §13.1).

One line per event: run_id, video_id, stage, attempt, event, elapsed_ms,
result, error_code, provider, model. Secrets are never logged — redaction
is applied defensively to every string field.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import LOG_DIR

_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+|api[_-]?key['\"=:\s]+\S+)", re.I
)

KEEP_DAYS = 14


def _redact(v: Any) -> Any:
    if isinstance(v, str):
        return _SECRET_RE.sub("[REDACTED]", v)
    return v


class RunLogger:
    def __init__(self, run_id: str | None = None):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.path = LOG_DIR / f"ytrec-{day}.jsonl"
        self._t0 = time.monotonic()

    def event(self, event: str, **fields: Any) -> None:
        rec = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "run_id": self.run_id,
            "event": event,
        }
        rec.update({k: _redact(v) for k, v in fields.items() if v is not None})
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def summary(self, **counters: Any) -> None:
        self.event("run_summary", elapsed_ms=int((time.monotonic() - self._t0) * 1000),
                   **counters)

    @staticmethod
    def prune_old() -> int:
        """Delete log files older than KEEP_DAYS. Returns count removed."""
        cutoff = time.time() - KEEP_DAYS * 86400
        removed = 0
        for p in LOG_DIR.glob("ytrec-*.jsonl"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        return removed
