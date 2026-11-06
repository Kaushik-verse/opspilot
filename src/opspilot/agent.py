"""The OpsPilot agent: a ReAct-style policy over a chat LLM with bounded memory."""

from __future__ import annotations

from dataclasses import dataclass

from opspilot.llm import LLMResponse
from opspilot.router import CascadeRouter

HISTORY_CHAR_BUDGET = 10000  # keeps each request well inside free-tier token-per-minute limits


@dataclass
class AgentStep:
    text: str
    tier: str
    response: LLMResponse


class OpsPilotAgent:
    def __init__(self, router: CascadeRouter, system_prompt: str, task_prompt: str) -> None:
        self.router = router
        self.messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task_prompt},
        ]
        self.step = 0

    def _window(self) -> list[dict]:
        """System + task are pinned; older turns are dropped first when over budget."""
        pinned, rest = self.messages[:2], self.messages[2:]
        budget = HISTORY_CHAR_BUDGET - sum(len(m["content"]) for m in pinned)
        kept: list[dict] = []
        for m in reversed(rest):
            if budget - len(m["content"]) < 0 and kept:
                break
            kept.insert(0, m)
            budget -= len(m["content"])
        if kept and kept[0]["role"] == "assistant":
            kept.insert(0, {"role": "user", "content": "(earlier steps omitted to save context)"})
        return pinned + kept

    def observe(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def act(self) -> AgentStep:
        llm, tier = self.router.pick(self.step)
        resp = llm.chat(self._window())
        self.messages.append({"role": "assistant", "content": resp.text})
        self.step += 1
        return AgentStep(resp.text, tier, resp)
