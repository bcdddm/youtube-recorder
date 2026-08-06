"""creds.py — API key lookup via the `keyring` library (cross-platform swap-in
for the old macOS-only `security` CLI shell-out). Run: python3 -m pytest tests/ -q
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="ytrec-test-")
os.environ["YTREC_HOME"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parents[0] / ".."))

from youtube_recorder import creds  # noqa: E402


def test_get_key_prefers_env_var_over_keyring(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    with patch("keyring.get_password") as gp:
        assert creds.get_key("anthropic") == "sk-ant-from-env"
        gp.assert_not_called()


def test_get_key_falls_back_to_keyring_for_known_provider(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with patch("keyring.get_password", return_value="sk-ant-from-keychain") as gp:
        assert creds.get_key("anthropic") == "sk-ant-from-keychain"
        service, username = gp.call_args.args
        assert service == "ytrec-anthropic"
        assert username  # some non-empty account name was passed


def test_get_key_openai_uses_its_own_service_and_env_names(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with patch("keyring.get_password", return_value="sk-from-keychain") as gp:
        assert creds.get_key("openai") == "sk-from-keychain"
        assert gp.call_args.args[0] == "ytrec-openai"

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    with patch("keyring.get_password") as gp:
        assert creds.get_key("openai") == "sk-env"
        gp.assert_not_called()


def test_get_key_unknown_provider_derives_service_and_env_names(monkeypatch):
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    with patch("keyring.get_password", return_value="sk-sf") as gp:
        assert creds.get_key("siliconflow") == "sk-sf"
        assert gp.call_args.args[0] == "ytrec-siliconflow"

    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf-env")
    assert creds.get_key("siliconflow") == "sk-sf-env"


def test_get_key_empty_provider_returns_none():
    assert creds.get_key("") is None
    assert creds.get_key(None) is None


def test_get_key_strips_whitespace_and_treats_blank_as_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with patch("keyring.get_password", return_value="  sk-ant-padded  \n"):
        assert creds.get_key("anthropic") == "sk-ant-padded"
    with patch("keyring.get_password", return_value=""):
        assert creds.get_key("anthropic") is None
    with patch("keyring.get_password", return_value=None):
        assert creds.get_key("anthropic") is None


def test_get_key_swallows_keyring_errors(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import keyring.errors

    with patch("keyring.get_password", side_effect=keyring.errors.KeyringError("no backend")):
        assert creds.get_key("anthropic") is None


def test_get_key_returns_none_if_keyring_unavailable(monkeypatch):
    """If keyring somehow isn't installed (shouldn't happen in the packaged
    app, but defensive), get_key must not raise — just report no key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "keyring", None)
    try:
        assert creds.get_key("anthropic") is None
    finally:
        monkeypatch.undo()


def test_creds_module_has_no_macos_specific_subprocess_calls():
    """Regression guard for the Windows-portability audit: creds.py used to
    shell out to macOS's `security` CLI directly, which is a hard blocker
    for any other platform. Make sure that's gone for good."""
    src = Path(creds.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in src
    assert '"security"' not in src and "'security'" not in src
