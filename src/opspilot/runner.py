"""Runs one episode (scenario x task) end-to-end and scores it."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from opspilot.actions import parse_action
from opspilot.agent import OpsPilotAgent
from opspilot.executor import SimBackend, run_pipeline, truncate
from opspilot.guardrails import GuardrailPolicy
from opspilot.prompts import system_prompt, task_prompt
from opspilot.router import CascadeRouter
from opspilot.scenarios import Episode
from opspilot.sim.app import APP_DESCRIPTION, NAMESPACE, SERVICES
from opspilot.sim.cluster import Cluster

VERIFY_COMMANDS = (f"kubectl get pods -n {NAMESPACE}", f"kubectl get endpoints -n {NAMESPACE}")


@dataclass
class EpisodeResult:
    episode: str
    scenario: str
    task: str
    config: str
    success: bool
    answer: object = None
    detail: str = ""
    steps: int = 0
    small_tokens: int = 0
    large_tokens: int = 0
    llm_calls: int = 0
    escalated: bool = False
    escalation_reason: str = ""
    blocked_actions: int = 0
    format_errors: int = 0
    writes: int = 0
    wall_s: float = 0.0
    llm_s: float = 0.0
    error: str = ""
    trace: list = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.small_tokens + self.large_tokens

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("trace")
        d["total_tokens"] = self.total_tokens
        return d


class SimEnv:
    """Simulated cluster with one injected fault, scored like AIOpsLab tasks."""

    def __init__(self, episode: Episode) -> None:
        self.episode = episode
        self.cluster = Cluster()
        for argv in episode.scenario.inject_argv:
            self.cluster.kubectl(argv)
        self.backend = SimBackend(self.cluster)
        self.initial_objects = (set(self.cluster.deployments), set(self.cluster.services))

    def prompt(self) -> str:
        return task_prompt("Hotel Reservation", NAMESPACE, APP_DESCRIPTION, self.episode.task)

    def evaluate(self, answer, has_answer: bool) -> tuple[bool, str]:
        sc, task = self.episode.scenario, self.episode.task
        if task in ("detection", "localization"):
            return score_detection_localization(sc, task, answer, has_answer)
        if task == "mitigation":
            return score_mitigation(self.cluster.health(), self.initial_objects,
                                    (set(self.cluster.deployments), set(self.cluster.services)))
        raise ValueError(task)


def score_detection_localization(scenario, task: str, answer, has_answer: bool) -> tuple[bool, str]:
    if task == "detection":
        if not has_answer:
            return False, "no answer submitted"
        said = str(answer).strip().strip(".").lower()
        if said not in ("yes", "no"):
            return False, f"invalid detection answer {answer!r}"
        truth = "yes" if scenario.root_cause else "no"
        return said == truth, f"answered {said}, expected {truth}"
    names = answer if isinstance(answer, (list, tuple)) else [answer] if has_answer else []
    norm = [_to_service(str(n)) for n in names]
    if not norm:
        return False, "no services submitted"
    return norm[0] == scenario.root_cause, f"top-1 {norm[0]!r}, expected {scenario.root_cause!r} (submitted {norm})"


def score_mitigation(health: dict, before: tuple[set, set], after: tuple[set, set]) -> tuple[bool, str]:
    collateral = (before[0] - after[0]) | (before[1] - after[1])
    if collateral:
        return False, f"collateral damage: deleted {sorted(collateral)}"
    return health["healthy"], "cluster healthy" if health["healthy"] else "; ".join(health["problems"][:3])


EVIDENCE = (
    ("deployment status (kubectl get deployments)", ("get deploy", "get deployment", "describe deploy", "get all")),
    ("service endpoints (kubectl get endpoints)", ("get endpoints", "get ep", "describe svc", "describe service")),
    ("application logs (kubectl logs)", ("logs ",)),
)


def missing_evidence(commands: list[str]) -> list[str]:
    """Which basic SRE checks has the agent not run yet? (generic; no ground truth involved)"""
    text = "\n".join(" ".join(c.split()) for c in commands)
    return [label for label, needles in EVIDENCE if not any(n in text for n in needles)]


def _to_service(name: str) -> str:
    n = name.strip().lower()
    n = re.sub(r"^(deployment|deploy|service|svc|pod)s?[./]", "", n)
    n = n.split(".")[0]
    if n in SERVICES:
        return n
    for svc in sorted(SERVICES, key=len, reverse=True):  # pod name -> owning service
        if re.fullmatch(rf"{re.escape(svc)}-[a-z0-9]{{6,10}}-[a-z0-9]{{5}}", n):
            return svc
    return n


def run_episode(episode: Episode, router: CascadeRouter, config_name: str, max_steps: int = 15,
                verify_before_submit: bool = True, trace_dir: Path | None = None,
                on_step=None, env=None) -> EpisodeResult:
    """Run one episode. ``env`` defaults to the simulator; pass a ``KindEnv`` for a real cluster."""
    env = env or SimEnv(episode)
    writes_ok = episode.task == "mitigation"
    guard = GuardrailPolicy(NAMESPACE, allow_writes=writes_ok)
    agent = OpsPilotAgent(router, system_prompt(NAMESPACE, writes_ok, max_steps), env.prompt())
    res = EpisodeResult(episode.id, episode.scenario.id, episode.task, config_name, False)
    seen_cmds: set[str] = set()
    verified = False
    all_cmds: list[str] = []
    t0 = time.perf_counter()
    submitted = False
    try:
        for step in range(max_steps):
            out = agent.act()
            r = out.response
            res.llm_calls += 1
            res.llm_s += r.latency_s
            if out.tier == "small":
                res.small_tokens += r.total_tokens
            else:
                res.large_tokens += r.total_tokens
            action = parse_action(out.text)
            entry = {"step": step + 1, "tier": out.tier, "model": r.model, "tokens": r.total_tokens,
                     "latency_s": round(r.latency_s, 2), "response": out.text, "action": action.kind}
            if action.warning:
                entry["warning"] = action.warning
            if action.kind == "invalid":
                res.format_errors += 1
                router.escalate("format error", step)
                obs = action.error
                entry["observation"] = obs
            elif action.kind == "exec_shell":
                cmd = action.command.strip()
                entry["command"] = cmd
                all_cmds.append(cmd)
                decision = guard.check(cmd)
                entry["guardrail"] = decision.category if decision.allowed else f"BLOCKED: {decision.reason}"
                if not decision.allowed:
                    res.blocked_actions += 1
                    router.escalate("guardrail block", step)
                    obs = f"BLOCKED by OpsPilot guardrail: {decision.reason}"
                else:
                    if cmd in seen_cmds and not decision.is_write:
                        router.escalate("repeated command", step)
                    seen_cmds.add(cmd)
                    rc, output = run_pipeline(env.backend, decision.pipeline)
                    if decision.is_write and rc == 0:
                        res.writes += 1
                        seen_cmds = {cmd}  # re-checking state after a change is verification, not a loop
                    obs = truncate(output) if output else "(no output)"
                entry["observation"] = obs
            else:  # submit
                entry["answer"] = action.arg
                if writes_ok and verify_before_submit and not verified:
                    verified = True
                    if res.writes == 0:
                        router.escalate("submit without any fix", step)
                    probe = [run_pipeline(env.backend, guard.check(c).pipeline)[1] for c in VERIFY_COMMANDS]
                    obs = ("Pre-submit verification (OpsPilot runs this automatically before closing a "
                           "mitigation):\n$ " + VERIFY_COMMANDS[0] + "\n" + truncate(probe[0], 1600) +
                           "\n$ " + VERIFY_COMMANDS[1] + "\n" + truncate(probe[1], 1600) +
                           "\nIf every pod is 1/1 Running and every service has endpoints on the "
                           "right port, call submit() again. Otherwise keep fixing.")
                    entry["observation"] = obs
                elif (not writes_ok and verify_before_submit and not verified
                      and (missing := missing_evidence(all_cmds))):
                    verified = True
                    obs = ("Self-check before your answer is recorded: you have not yet checked "
                           + "; ".join(missing) + ". Check them now, then submit again (the same "
                           "answer if it still holds, or a corrected one).")
                    entry["observation"] = obs
                else:
                    ok, detail = env.evaluate(action.arg, action.has_arg)
                    res.success, res.detail, res.answer = ok, detail, action.arg
                    submitted = True
                    entry["observation"] = f"[evaluation] {'SUCCESS' if ok else 'FAIL'}: {detail}"
            res.trace.append(entry)
            res.steps = step + 1
            if on_step:
                on_step(entry)
            if submitted:
                break
            agent.observe(obs)
        if not submitted:
            _, detail = (env.evaluate(None, False) if episode.task == "mitigation"
                         else (False, "step budget exhausted"))
            res.success, res.detail = False, f"no submit within {max_steps} steps; {detail}"
    except Exception as e:  # provider errors etc. are recorded, not fatal to a benchmark
        res.error = f"{type(e).__name__}: {e}"
        res.detail = res.detail or "episode aborted"
    res.escalated = router.escalated
    res.escalation_reason = ",".join(router.reasons)
    res.wall_s = time.perf_counter() - t0
    if trace_dir:
        trace_dir.mkdir(parents=True, exist_ok=True)
        with open(trace_dir / f"{config_name}__{episode.id}.json", "w") as f:
            json.dump({"summary": res.summary(), "trace": res.trace}, f, indent=2, default=str)
    return res
