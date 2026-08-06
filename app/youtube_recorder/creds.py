"""Credentials from the OS keychain (v0.2 §8; P0-07 fix — never in plist/config).

Backed by the `keyring` library instead of shelling out to macOS's `security`
CLI directly, so this module works unmodified on Windows (Credential Manager)
and Linux (Secret Service) too — keyring picks the right backend per platform
at runtime. On macOS it still reads/writes ordinary Keychain "generic
password" items under the same service names as before, so keys added the
old way keep working:
  security add-generic-password -s "ytrec-anthropic" -a "$USER" -w "sk-ant-..."
  security add-generic-password -s "ytrec-openai"    -a "$USER" -w "sk-..."

Equivalent one-liner via keyring itself (works on any platform):
  python -m keyring set ytrec-anthropic "$USER"
  python -m keyring set ytrec-openai    "$USER"
"""

from __future__ import annotations

import getpass
import os

_SERVICES = {"anthropic": "ytrec-anthropic", "openai": "ytrec-openai"}
_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def get_key(provider: str) -> str | None:
    env = os.environ.get(_ENV.get(provider, (provider or "").upper() + "_API_KEY"))
    if env:
        return env
    service = _SERVICES.get(provider) or (("ytrec-" + provider) if provider else None)
    if not service:
        return None
    try:
        import keyring
        import keyring.errors

        try:
            username = getpass.getuser()
        except Exception:
            username = os.environ.get("USER") or os.environ.get("USERNAME") or ""
        try:
            key = keyring.get_password(service, username)
        except keyring.errors.KeyringError:
            return None
        return key.strip() if key else None
    except ImportError:
        return None
