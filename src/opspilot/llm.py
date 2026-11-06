"""Minimal OpenAI-compatible chat client, tuned for free-tier providers.

Defaults to Groq's free tier. Any OpenAI-compatible endpoint works (for example a local
Ollama server at http://localhost:11434/v1), so no paid API is ever required.
Free tiers rate-limit aggressively, so the client honours ``Retry-After`` and backs off.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": None},
    "openai_compatible": {"base_url": None, "key_env": "OPSPILOT_API_KEY"},
}


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLM(Protocol):
    model: str

    def chat(self, messages: list[dict]) -> LLMResponse: ...


class LLMError(RuntimeError):
    pass


class ChatClient:
    def __init__(self, model: str, provider: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, temperature: float = 0.0, max_tokens: int = 700,
                 max_retries: int = 8, timeout_s: float = 90.0) -> None:
        provider = provider or os.getenv("OPSPILOT_PROVIDER", "groq")
        if provider not in PROVIDERS:
            raise LLMError(f"unknown provider {provider!r}; choose one of {sorted(PROVIDERS)}")
        cfg = PROVIDERS[provider]
        self.provider = provider
        self.model = model
        self.base_url = (base_url or os.getenv("OPSPILOT_BASE_URL") or cfg["base_url"] or "").rstrip("/")
        if not self.base_url:
            raise LLMError("OPSPILOT_BASE_URL must be set for the openai_compatible provider")
        key_env = cfg["key_env"]
        self.api_key = api_key or (os.getenv(key_env) if key_env else None)
        if key_env and not self.api_key:
            raise LLMError(f"{key_env} is not set. Put it in .env (see .env.example).")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self._http = httpx.Client(timeout=timeout_s)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def list_models(self) -> list[str]:
        r = self._http.get(f"{self.base_url}/models", headers=self._headers())
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []))

    def chat(self, messages: list[dict]) -> LLMResponse:
        body = {"model": self.model, "messages": messages, "temperature": self.temperature,
                "max_tokens": self.max_tokens}
        if "gpt-oss" in self.model:  # reasoning models: thinking tokens count against max_tokens
            body["reasoning_effort"] = os.getenv("OPSPILOT_REASONING_EFFORT", "low")
            body["max_tokens"] = max(self.max_tokens, 2048)
        delay = 2.0
        last = ""
        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                r = self._http.post(f"{self.base_url}/chat/completions", json=body,
                                    headers=self._headers())
            except httpx.HTTPError as e:
                last = f"network error: {e}"
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code == 200:
                data = r.json()
                usage = data.get("usage") or {}
                text = data["choices"][0]["message"].get("content") or ""
                return LLMResponse(
                    text=text, model=self.model,
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    latency_s=time.perf_counter() - t0)
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code == 400 and "tool_use_failed" in r.text:
                recovered = recover_tool_call(r.text)
                if recovered:  # the model emitted a native tool call; convert it to our text format
                    return LLMResponse(recovered, self.model, sum(len(m["content"]) for m in messages) // 4,
                                       len(recovered) // 4, time.perf_counter() - t0)
                if attempt < self.max_retries:
                    body["messages"] = messages + [{"role": "user", "content": (
                        "Tool/function calling is disabled. Reply in plain text with exactly one "
                        "``` code block containing exec_shell(\"...\") or submit(...).")}]
                    continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = r.headers.get("retry-after")
                wait = float(retry_after) if retry_after and _is_number(retry_after) else delay
                time.sleep(min(wait + 0.5, 90))
                delay = min(delay * 2, 60)
                continue
            break
        raise LLMError(f"{self.provider}/{self.model} failed after retries: {last}")


def recover_tool_call(error_body: str) -> str | None:
    """Translate a rejected native tool call (Groq ``failed_generation``) into an action block."""
    try:
        gen = json.loads(json.loads(error_body)["error"]["failed_generation"])
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(gen, dict):
        return None
    name = str(gen.get("name", ""))
    args = gen.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"cmd": args}
    if "submit" in name:
        val = next(iter(args.values()), None) if isinstance(args, dict) and args else None
        return f"```\nsubmit({'' if val is None else repr(val)})\n```"
    cmd = args.get("cmd") or args.get("command") if isinstance(args, dict) else None
    if isinstance(cmd, list):
        cmd = cmd[-1] if cmd and str(cmd[0]) in ("bash", "sh", "/bin/bash", "/bin/sh") else " ".join(map(str, cmd))
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    return f"```\nexec_shell({json.dumps(cmd.strip())})\n```"


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def default_models() -> tuple[str, str]:
    """(small, large) model ids for the cascade, overridable via environment variables."""
    return (os.getenv("OPSPILOT_SMALL_MODEL", "openai/gpt-oss-20b"),
            os.getenv("OPSPILOT_LARGE_MODEL", "openai/gpt-oss-120b"))


def make_tier_clients() -> tuple[ChatClient, ChatClient]:
    """Build (small, large) clients. Each tier may use its own free provider, e.g.
    OPSPILOT_LARGE_PROVIDER=openai_compatible with OPSPILOT_LARGE_BASE_URL/OPSPILOT_LARGE_API_KEY."""
    small_m, large_m = default_models()
    clients = []
    for tier, model in (("SMALL", small_m), ("LARGE", large_m)):
        provider = os.getenv(f"OPSPILOT_{tier}_PROVIDER") or os.getenv("OPSPILOT_PROVIDER", "groq")
        clients.append(ChatClient(model, provider=provider,
                                  base_url=os.getenv(f"OPSPILOT_{tier}_BASE_URL"),
                                  api_key=os.getenv(f"OPSPILOT_{tier}_API_KEY")))
    return clients[0], clients[1]
