"""LLM provider adapters: Claude primary, OpenAI fallback (user decision).

- JSON-mode helper with one repair retry.
- Rough cost tracking into the costs table.
- Keys from Keychain/env via creds.py; never logged.
"""

from __future__ import annotations

import json
import re

from . import db as dbm
from .creds import get_key

DEFAULT_MODELS = {"anthropic": "claude-sonnet-5", "openai": "gpt-4o-mini",
                  "claude_cli": "sonnet", "ollama": "llama3.1"}

# 订阅/本地渠道：不产生按量费用
FREE_PROVIDERS = {"claude_cli", "ollama"}

# USD per 1M tokens (in, out) — rough, for budget tracking only
PRICES = {
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-4o-mini": (0.15, 0.60),
}


class ProviderError(RuntimeError):
    def __init__(self, msg: str, transient: bool = True):
        super().__init__(msg)
        self.transient = transient


def _call_anthropic(model: str, system: str, user: str, max_tokens: int):
    key = get_key("anthropic")
    if not key:
        raise ProviderError("no anthropic key (Keychain: ytrec-anthropic)",
                            transient=False)
    try:
        import anthropic
    except ImportError as e:
        raise ProviderError(f"anthropic import failed: {e}", transient=False)
    client = anthropic.Anthropic(api_key=key)
    try:
        r = client.messages.create(model=model, max_tokens=max_tokens,
                                   system=system,
                                   messages=[{"role": "user", "content": user}])
    except anthropic.APIStatusError as e:
        raise ProviderError(f"anthropic http {e.status_code}",
                            transient=e.status_code in (429, 500, 502, 503, 529))
    except anthropic.APIError as e:
        raise ProviderError(f"anthropic: {e}")
    return r.content[0].text, r.usage.input_tokens, r.usage.output_tokens


def _call_openai(model: str, system: str, user: str, max_tokens: int):
    key = get_key("openai")
    if not key:
        raise ProviderError("no openai key (Keychain: ytrec-openai)",
                            transient=False)
    try:
        from openai import OpenAI, APIStatusError, APIError
    except ImportError as e:
        raise ProviderError(f"openai import failed: {e}", transient=False)
    client = OpenAI(api_key=key)
    try:
        r = client.chat.completions.create(
            model=model, max_completion_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}])
    except APIStatusError as e:
        raise ProviderError(f"openai http {e.status_code}",
                            transient=e.status_code in (429, 500, 502, 503))
    except APIError as e:
        raise ProviderError(f"openai: {e}")
    u = r.usage
    return r.choices[0].message.content, u.prompt_tokens, u.completion_tokens


def _claude_cli_path() -> str | None:
    import shutil, os
    return (shutil.which("claude")
            or (os.path.expanduser("~/.local/bin/claude")
                if os.path.exists(os.path.expanduser("~/.local/bin/claude"))
                else None))


def _parse_claude_cli_json(stdout: str):
    """claude -p --output-format json → (text, tin, tout)。"""
    j = json.loads(stdout)
    if j.get("is_error"):
        raise ProviderError("claude_cli: " + str(j.get("result", ""))[:200])
    u = j.get("usage") or {}
    return (j.get("result") or "", int(u.get("input_tokens") or 0),
            int(u.get("output_tokens") or 0))


def claude_cli_proxy_issue() -> str | None:
    """~/.claude/settings.json 可把 CLI 指向本地代理（如 CC Switch）。
    代理挂了 CLI 会无限等待——这里 1 秒探活，死代理直接快败。"""
    import os, socket
    from urllib.parse import urlparse
    try:
        with open(os.path.expanduser("~/.claude/settings.json"),
                  encoding="utf-8") as f:
            base = (json.load(f).get("env") or {}).get("ANTHROPIC_BASE_URL", "")
    except Exception:
        return None
    if not base:
        return None
    u = urlparse(base)
    if u.hostname not in ("127.0.0.1", "localhost"):
        return None
    try:
        with socket.create_connection((u.hostname, u.port or 80), timeout=1):
            return None
    except OSError:
        return (f"local proxy {u.hostname}:{u.port} not running "
                "(start CC Switch or remove ANTHROPIC_BASE_URL from "
                "~/.claude/settings.json)")


def _call_claude_cli(model: str, system: str, user: str, max_tokens: int):
    """Claude Code 无头模式：走本机登录的订阅额度，不用 API key。
    纯文本问答；cwd 指向空目录避免读到任何项目文件，--max-turns 1 禁止工具循环。"""
    import os, subprocess, tempfile
    exe = _claude_cli_path()
    if not exe:
        raise ProviderError("claude CLI not found (install Claude Code)",
                            transient=False)
    issue = claude_cli_proxy_issue()
    if issue:
        raise ProviderError("claude_cli: " + issue, transient=False)
    scratch = os.path.join(tempfile.gettempdir(), "ytrec-cli-scratch")
    os.makedirs(scratch, exist_ok=True)
    cmd = [exe, "-p", "--output-format", "json", "--max-turns", "1",
           "--model", model or "sonnet", "--append-system-prompt", system]
    try:
        r = subprocess.run(cmd, input=user, capture_output=True, text=True,
                           timeout=420, cwd=scratch)
    except subprocess.TimeoutExpired:
        raise ProviderError("claude_cli: timeout (420s)", transient=True)
    except OSError as e:
        raise ProviderError(f"claude_cli spawn: {e}", transient=False)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "")[:200]
        low = err.lower()
        raise ProviderError("claude_cli: " + err,
                            transient=any(k in low for k in
                                          ("limit", "overload", "timeout",
                                           "network", "5")))
    try:
        return _parse_claude_cli_json(r.stdout)
    except (ValueError, KeyError) as e:
        raise ProviderError(f"claude_cli bad output: {e}", transient=True)


def _call_ollama(model: str, system: str, user: str, max_tokens: int):
    """本地 Ollama（http://127.0.0.1:11434），免费、离线。"""
    import urllib.request, urllib.error
    if not model:
        raise ProviderError("ollama: no model selected (Settings ⑥)",
                            transient=False)
    body = json.dumps({
        "model": model, "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "options": {"num_predict": max_tokens},
    }).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:11434/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            j = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ProviderError(f"ollama http {e.code}: {e.read()[:120]}",
                            transient=e.code >= 500)
    except OSError as e:
        raise ProviderError(f"ollama unreachable: {e}", transient=False)
    text = (j.get("message") or {}).get("content", "")
    if not text:
        raise ProviderError("ollama: empty reply", transient=True)
    return text, int(j.get("prompt_eval_count") or 0), int(j.get("eval_count") or 0)


_CALLERS = {"anthropic": _call_anthropic, "openai": _call_openai,
            "claude_cli": _call_claude_cli, "ollama": _call_ollama}


PURPOSE_GROUP = {
    "chunk_notes": "article", "chunk_notes_retry": "article",
    "compose": "article", "compose_retry": "article",
    "visual_recall": "visuals", "report_qa": "qa",
}


def _route(cfg, purpose: str) -> list[str]:
    """按环节选择 API：ai.article / ai.visuals / ai.qa（auto=先用已配置的）。"""
    group = PURPOSE_GROUP.get(purpose, "article")
    sel = cfg.get(f"ai.{group}", "auto") if cfg else "auto"
    if sel in ("openai", "anthropic", "claude_cli", "ollama"):
        return [sel] + [x for x in ("openai", "anthropic") if x != sel]
    from .creds import get_key
    return (["openai", "anthropic"] if get_key("openai")
            else ["anthropic", "openai"])


def complete(cfg, con, video_id: str, system: str, user: str,
             max_tokens: int = 8000, purpose: str = "article") -> str:
    """Provider order decided per-purpose (ai.* routing); falls back. Records cost."""
    order = _route(cfg, purpose)
    last: Exception | None = None
    for prov in order:
        model = cfg.get(f"article.model_{prov}", DEFAULT_MODELS[prov])
        try:
            text, tin, tout = _CALLERS[prov](model, system, user, max_tokens)
        except ProviderError as e:
            last = e
            continue
        if prov in FREE_PROVIDERS:
            cost = 0.0
        else:
            pin, pout = PRICES.get(model, (3.0, 15.0))
            cost = (tin * pin + tout * pout) / 1e6
        if con is not None:
            con.execute(
                "INSERT INTO costs(video_id,provider,model,units,unit_type,"
                "estimated_cost_usd,at) VALUES(?,?,?,?,?,?,?)",
                (video_id, prov, model, tin + tout, "tokens", round(cost, 5),
                 dbm.now()))
            con.commit()
        return text
    raise last or ProviderError("no provider available", transient=False)


def extract_json(text: str) -> dict:
    """Parse a JSON object out of an LLM reply (handles ```json fences)."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else text
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(raw[start:end + 1])
