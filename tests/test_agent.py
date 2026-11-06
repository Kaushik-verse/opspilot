import asyncio
import json

import pytest

from opspilot.actions import normalize_for_aiopslab, parse_action
from opspilot.bench import run_bench
from opspilot.integrations.aiopslab import OpsPilotAIOpsLabAgent
from opspilot.oracle import ScriptedLLM, oracle_script
from opspilot.report import build_report
from opspilot.router import CascadeRouter
from opspilot.runner import _to_service, run_episode
from opspilot.scenarios import SCENARIOS, Episode, all_episodes

NS = "hotel-reservation"


def test_parse_actions():
    a = parse_action('Check pods.\n```\nexec_shell("kubectl get pods -n x")\n```')
    assert a.kind == "exec_shell" and a.command == "kubectl get pods -n x"
    assert parse_action('```python\nsubmit(["geo"])\n```').arg == ["geo"]
    assert parse_action("```\nsubmit()\n```").kind == "submit"
    assert parse_action("no block").kind == "invalid"
    assert parse_action("```\na()\n```\n```\nb()\n```").kind == "invalid"
    assert parse_action("```\nos.system('x')\n```").kind == "invalid"
    assert normalize_for_aiopslab("```python\nsubmit()\n```") == "```\nsubmit()\n```"


def test_service_name_normalization():
    assert _to_service("deployment/geo") == "geo"
    assert _to_service("geo.hotel-reservation.svc.cluster.local") == "geo"
    assert _to_service("rate-0123456789-abcde") == "rate"
    assert _to_service("mongodb-geo") == "mongodb-geo"


@pytest.mark.parametrize("ep", all_episodes(), ids=lambda e: e.id)
def test_oracle_solves_every_episode(ep):
    llm = ScriptedLLM(oracle_script(ep.scenario.id, ep.task))
    r = run_episode(ep, CascadeRouter(llm, llm, mode="small"), "oracle")
    assert r.success, r.detail


def _reply(action: str) -> str:
    return f"thinking\n```\n{action}\n```"


def test_guardrail_blocks_and_cascade_escalates():
    small = ScriptedLLM([_reply(f'exec_shell("kubectl delete deployment rate -n {NS}")')], "small")
    large = ScriptedLLM([
        _reply(f'exec_shell("kubectl rollout undo deployment/rate -n {NS}")'),
        _reply("submit()"), _reply("submit()")], "large")
    ep = Episode(SCENARIOS["bad_image"], "mitigation")
    router = CascadeRouter(small, large, mode="cascade")
    r = run_episode(ep, router, "cascade")
    assert r.success and r.blocked_actions == 1 and r.escalated
    assert "guardrail block" in r.escalation_reason
    assert r.small_tokens > 0 and r.large_tokens > 0


def test_read_only_task_cannot_mutate():
    llm = ScriptedLLM([_reply(f'exec_shell("kubectl scale deployment profile --replicas=1 -n {NS}")'),
                       _reply('submit(["profile"])')])
    r = run_episode(Episode(SCENARIOS["scale_zero"], "localization"), CascadeRouter(llm, llm, "small"), "t")
    assert r.success and r.blocked_actions == 1 and r.writes == 0


def test_premature_submit_gets_verification_then_fails():
    llm = ScriptedLLM([_reply("submit()")])
    r = run_episode(Episode(SCENARIOS["target_port"], "mitigation"), CascadeRouter(llm, llm, "small"), "t")
    assert not r.success and "Pre-submit verification" in r.trace[0]["observation"]


def test_format_errors_are_recovered():
    llm = ScriptedLLM(["I think it is geo", _reply('submit(["geo"])')])
    r = run_episode(Episode(SCENARIOS["target_port"], "localization"), CascadeRouter(llm, llm, "small"), "t")
    assert r.success and r.format_errors == 1


def test_step_budget():
    llm = ScriptedLLM([_reply(f'exec_shell("kubectl get pods -n {NS}")')])
    r = run_episode(Episode(SCENARIOS["healthy"], "detection"), CascadeRouter(llm, llm, "small"), "t", max_steps=3)
    assert not r.success and r.steps == 3


def test_bench_and_report(tmp_path):
    rows = run_bench(tmp_path, ["oracle"], log=lambda *_: None)
    assert len(rows) == len(all_episodes()) and all(r["success"] for r in rows)
    assert run_bench(tmp_path, ["oracle"], log=lambda *_: None) == []  # resumable
    (tmp_path / "meta.json").write_text(json.dumps({"run": "test"}))
    out, md = build_report(tmp_path)
    assert out.exists() and "<svg" in out.read_text() and "`oracle` | **100%**" in md


def test_aiopslab_adapter_retries_blocked_actions_locally():
    llm = ScriptedLLM([_reply("exec_shell(\"kubectl delete namespace test-hotel-reservation\")"),
                       _reply("exec_shell(\"kubectl get pods -n test-hotel-reservation\")")])
    agent = OpsPilotAIOpsLabAgent(CascadeRouter(llm, llm, mode="small"))
    agent.init_context("Service Name: Hotel Reservation\nNamespace: test-hotel-reservation\n",
                       "Detect anomalies.", {"exec_shell(command: str)": "run", "submit(has_anomaly)": "s"},
                       problem_id="misconfig_app_hotel_res-detection-1")
    out = asyncio.run(agent.get_action("Start"))
    assert "kubectl get pods" in out and agent.stats["blocked"] == 1


def test_lenient_formatting_but_strict_actions():
    a = parse_action('```\nexec_shell("kubectl get pods -n x")\n```\n```\nsubmit()\n```')
    assert a.kind == "exec_shell" and a.warning and a.canonical.count("```") == 2
    assert parse_action('submit(["geo"])').arg == ["geo"]
    assert parse_action("```\nexec_shell(cmd='ls')\n```").kind == "invalid"


def test_read_only_self_check_gate():
    llm = ScriptedLLM([_reply(f'exec_shell("kubectl get pods -n {NS}")'), _reply('submit("No")'),
                       _reply(f'exec_shell("kubectl get deployments -n {NS}")'), _reply('submit("Yes")')])
    r = run_episode(Episode(SCENARIOS["scale_zero"], "detection"), CascadeRouter(llm, llm, "small"), "t")
    assert r.success and "Self-check" in r.trace[1]["observation"]
    llm2 = ScriptedLLM([_reply('submit("No")')])
    r2 = run_episode(Episode(SCENARIOS["scale_zero"], "detection"), CascadeRouter(llm2, llm2, "small"), "t",
                     verify_before_submit=False)
    assert not r2.success and r2.steps == 1
