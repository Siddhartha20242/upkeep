"""Minimal client for Baseten's OpenAI-compatible Model APIs.

Standard library only. The API key is read from the environment (or a local,
git-ignored ``.env``) and is never logged or included in error messages.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

BASE_URL = "https://inference.baseten.co/v1"
# The hackathon guide says to use this slug instead of the one in Baseten's docs.
DEFAULT_MODEL = "meta-models/muse-spark-open-1026"


class LLMError(RuntimeError):
    pass


def load_env(path: Path | str = ".env") -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing values."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Reply:
    message: dict
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0
    model: str = ""
    finish_reason: str = ""

    @property
    def text(self) -> str:
        return self.message.get("content") or ""

    @property
    def reasoning(self) -> str:
        """Muse Spark Open always reasons; Baseten's docs describe where it appears.
        Field names vary by provider, so look in the common places. Treat this as
        the model's own narration, never as evidence."""
        for key in ("reasoning_content", "reasoning"):
            v = self.message.get(key)
            if isinstance(v, str) and v:
                return v
        return ""

    @property
    def tool_calls(self) -> list[dict]:
        return self.message.get("tool_calls") or []

    @property
    def truncated(self) -> bool:
        """True if generation stopped on the token limit (reasoning can eat the budget)."""
        return self.finish_reason == "length"


PROVIDERS = {
    # name: (base_url, env var holding the key, default model)
    "baseten": (BASE_URL, "BASETEN_API_KEY", DEFAULT_MODEL),
    "meta": ("https://api.meta.ai/v1", "META_API_KEY", "muse-spark-1.3"),
}


def client_for(provider: str, model: str | None = None) -> "Client":
    base, env, default_model = PROVIDERS[provider]
    key = os.environ.get(env)
    if not key:
        raise LLMError(f"{env} is not set (put it in .env or your environment)")
    return Client(api_key=key, model=model or default_model, base_url=base)


class Client:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = BASE_URL,
        timeout: int = 120,
    ):
        self._key = api_key or os.environ.get("BASETEN_API_KEY")
        if not self._key:
            raise LLMError("BASETEN_API_KEY is not set (put it in .env or your environment)")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            # Baseten's OpenAI-compatible endpoint: same as the OpenAI SDK's Bearer auth.
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - fixed https URL
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500].replace(self._key, "***")
            raise LLMError(f"HTTP {e.code} from {path}: {body}") from None
        except urllib.error.URLError as e:
            raise LLMError(f"network error calling {path}: {e.reason}") from None

    def list_models(self) -> list[str]:
        return [m.get("id", "") for m in self._request("GET", "/models").get("data", [])]

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,  # generous: the model always reasons first
    ) -> Reply:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        t0 = time.monotonic()
        resp = self._request("POST", "/chat/completions", payload)
        dt = time.monotonic() - t0
        try:
            choice = resp["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError):
            raise LLMError(f"unexpected response shape: {str(resp)[:300]}") from None
        return Reply(
            msg, resp.get("usage", {}), dt, resp.get("model", self.model), choice.get("finish_reason") or ""
        )
