"""Adapter for Microsoft AIOpsLab (https://github.com/microsoft/AIOpsLab).

AIOpsLab's orchestrator owns the environment: it calls ``await agent.get_action(obs)``
and executes whatever single code-block action the agent returns. OpsPilot keeps its
guardrail and cascade router *inside* ``get_action``: a blocked or malformed action is
fed back to the model and retried locally, so it never reaches the cluster.
"""

from __future__ import annotations

import re

from opspilot.actions import parse_action
from opspilot.agent import OpsPilotAgent
from opspilot.guardrails import GuardrailPolicy
from opspilot.prompts import WRITE_POLICY
from opspilot.router import CascadeRouter

AIOPSLAB_SYSTEM = """You are OpsPilot, an autonomous Site Reliability Engineer.

{problem_desc}

Each turn: one short line of reasoning, then exactly ONE action in a single ``` code block.
Shell API:
{shell_api}

Submit API:
{submit_api}

Rules: one action per reply; only kubectl commands (you may pipe into grep/head/tail/wc);
start broad, then drill into the suspicious object; follow errors down the dependency chain.
{write_policy}"""

MAX_LOCAL_RETRIES = 3


class OpsPilotAIOpsLabAgent:
    def __init__(self, router: CascadeRouter) -> None:
        self.router = router
        self.agent: OpsPilotAgent | None = None
        self.guard: GuardrailPolicy | None = None
        self.stats = {"blocked": 0, "format_errors": 0, "small_tokens": 0, "large_tokens": 0}

    def init_context(self, problem_desc: str, instructions: str, apis: dict[str, str],
                     problem_id: str = "") -> None:
        shell = {k: v for k, v in apis.items() if "exec_shell" in k}
        submit = {k: v for k, v in apis.items() if "submit" in k}
        def fmt(d):
            return "\n\n".join(f"{k}\n{v}" for k, v in d.items())
        writes = "mitigation" in (problem_id + instructions).lower()
        m = re.search(r"Namespace:\s*([\w-]+)", problem_desc)
        namespace = m.group(1) if m else "default"
        self.guard = GuardrailPolicy(namespace, allow_writes=writes)
        system = AIOPSLAB_SYSTEM.format(problem_desc=problem_desc, shell_api=fmt(shell),
                                        submit_api=fmt(submit), write_policy=WRITE_POLICY[writes])
        self.agent = OpsPilotAgent(self.router, system, instructions)

    async def get_action(self, input: str) -> str:
        assert self.agent and self.guard, "call init_context first"
        self.agent.observe(input)
        text = ""
        for _ in range(MAX_LOCAL_RETRIES):
            step = self.agent.act()
            key = "small_tokens" if step.tier == "small" else "large_tokens"
            self.stats[key] += step.response.total_tokens
            text = step.text
            action = parse_action(text)
            if action.kind == "invalid":
                self.stats["format_errors"] += 1
                self.router.escalate("format error", self.agent.step)
                self.agent.observe(action.error)
                continue
            if action.kind == "exec_shell":
                decision = self.guard.check(action.command)
                if not decision.allowed:
                    self.stats["blocked"] += 1
                    self.router.escalate("guardrail block", self.agent.step)
                    self.agent.observe(f"BLOCKED by OpsPilot guardrail: {decision.reason}")
                    continue
            return "OpsPilot action.\n" + action.canonical
        # Never forward an unsafe/malformed action; fall back to a harmless read.
        return f'Falling back to a safe read.\n```\nexec_shell("kubectl get pods -n {self.guard.namespace}")\n```'
