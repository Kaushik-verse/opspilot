"""Cost-aware model cascade (in the spirit of FrugalGPT / RouteLLM).

Most SRE steps are routine (``kubectl get pods``), so a small fast model handles them.
The router escalates to the large model, for the rest of the episode, only when the
small model shows signs of struggling:
  * it produced an unparseable reply,
  * it tried an action the guardrail blocked,
  * it repeated a command it had already run (a loop),
  * it is about to submit a final answer for a mutating task without having changed anything,
  * it has used ``escalate_after`` steps without finishing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opspilot.llm import LLM

MODES = ("small", "large", "cascade")


@dataclass
class CascadeRouter:
    small: LLM
    large: LLM
    mode: str = "cascade"
    escalate_after: int = 8
    escalated: bool = False
    reasons: list[str] = field(default_factory=list)
    escalated_at_step: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")

    def pick(self, step: int) -> tuple[LLM, str]:
        if self.mode == "small":
            return self.small, "small"
        if self.mode == "large":
            return self.large, "large"
        if not self.escalated and step >= self.escalate_after:
            self.escalate(f"no answer after {step} steps", step)
        return (self.large, "large") if self.escalated else (self.small, "small")

    def escalate(self, reason: str, step: int) -> None:
        if self.mode != "cascade" or self.escalated:
            return
        self.escalated = True
        self.escalated_at_step = step
        self.reasons.append(reason)
