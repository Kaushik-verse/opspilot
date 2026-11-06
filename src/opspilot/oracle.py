"""Offline LLM stand-ins: a scripted expert (proves every task is solvable) and a replay LLM.

``OracleLLM`` replays a hand-written SRE playbook per scenario. It is used by the test suite
and by ``opspilot bench --configs oracle`` to sanity-check the environment and scorer
without spending any API quota.
"""

from __future__ import annotations

from opspilot.llm import LLMResponse
from opspilot.sim.app import NAMESPACE as NS

_FIX = {
    "scale_zero": f"kubectl scale deployment profile --replicas=1 -n {NS}",
    "target_port": (f"kubectl patch service geo -n {NS} -p "
                    "'{\"spec\":{\"ports\":[{\"port\":8083,\"targetPort\":8083}]}}'"),
    "bad_image": f"kubectl rollout undo deployment/rate -n {NS}",
    "node_selector": f"kubectl rollout undo deployment/reservation -n {NS}",
    "missing_env": f"kubectl rollout undo deployment/user -n {NS}",
}
_ROOT = {"scale_zero": "profile", "target_port": "geo", "bad_image": "rate",
         "node_selector": "reservation", "missing_env": "user", "healthy": None}


def _reply(thought: str, action: str) -> str:
    return f"{thought}\n```\n{action}\n```"


def oracle_script(scenario: str, task: str) -> list[str]:
    look = [
        _reply("Start broad: check workload status.", f'exec_shell("kubectl get pods -n {NS}")'),
        _reply("Check that every service has endpoints.", f'exec_shell("kubectl get endpoints -n {NS}")'),
    ]
    if task == "detection":
        return look + [_reply("Decide.", f'submit("{"Yes" if _ROOT[scenario] else "No"}")')]
    if task == "localization":
        return look + [_reply("Root cause identified.", f'submit(["{_ROOT[scenario]}"])')]
    fix = _FIX[scenario].replace('"', '\\"')
    return look + [
        _reply("Apply the minimal fix.", f'exec_shell("{fix}")'),
        _reply("Done; submit (harness verifies first).", "submit()"),
        _reply("Verified healthy.", "submit()"),
    ]


class ScriptedLLM:
    """Returns pre-written replies in order; reports ~4 chars/token usage."""

    def __init__(self, replies: list[str], model: str = "scripted") -> None:
        self.replies = list(replies)
        self.model = model
        self.calls = 0

    def chat(self, messages: list[dict]) -> LLMResponse:
        text = self.replies[min(self.calls, len(self.replies) - 1)] if self.replies else ""
        self.calls += 1
        prompt_chars = sum(len(m["content"]) for m in messages)
        return LLMResponse(text, self.model, prompt_chars // 4, len(text) // 4, 0.0)
