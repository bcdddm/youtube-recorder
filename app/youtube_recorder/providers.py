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

DEFAULT_MODELS = {"anthropic": "claude-sonnet-5", "openai": "gpt-4o-mini"}

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


_CALLERS = {"anthropic": _call_anthropic, "openai": _call_openai}


def complete(cfg, con, video_id: str, system: str, user: str,
             max_tokens: int = 8000, purpose: str = "article") -> str:
    """Try primary provider, fall back to the other. Records cost."""
    primary = cfg.get("article.provider", "anthropic")
    order = [primary] + [p for p in ("anthropic", "openai") if p != primary]
    last: Exception | None = None
    for prov in order:
        model = cfg.get(f"article.model_{prov}", DEFAULT_MODELS[prov])
        try:
            text, tin, tout = _CALLERS[prov](model, system, user, max_tokens)
        except ProviderError as e:
            last = e
            continue
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
