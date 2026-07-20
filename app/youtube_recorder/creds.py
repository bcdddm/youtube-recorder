"""Credentials from macOS Keychain (v0.2 §8; P0-07 fix — never in plist/config).

Add keys once via Terminal (values never pass through this app's logs):
  security add-generic-password -s "ytrec-anthropic" -a "$USER" -w "sk-ant-..."
  security add-generic-password -s "ytrec-openai"    -a "$USER" -w "sk-..."
"""

from __future__ import annotations

import os
import subprocess

_SERVICES = {"anthropic": "ytrec-anthropic", "openai": "ytrec-openai"}
_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def get_key(provider: str) -> str | None:
    env = os.environ.get(_ENV.get(provider, ""))
    if env:
        return env
    service = _SERVICES.get(provider)
    if not service:
        return None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        key = out.stdout.strip()
        return key or None
    except (OSError, subprocess.TimeoutExpired):
        return None
