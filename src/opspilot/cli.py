"""Command-line interface: ``opspilot <command>``."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from opspilot.scenarios import SCENARIOS, TASKS, Episode

ROOT = Path.cwd()


def _print_step(e: dict) -> None:
    tier = e["tier"].upper()
    what = e.get("command") or (f"submit({e.get('answer')!r})" if e["action"] == "submit" else e["action"])
    print(f"\n\033[1m[{e['step']:02d}] {tier:5s}\033[0m {what}")
    g = e.get("guardrail", "")
    if g.startswith("BLOCKED"):
        print(f"      \033[31m{g}\033[0m")
    obs = str(e.get("observation", ""))
    print("      " + "\n      ".join(obs.splitlines()[:14]) + ("\n      ..." if obs.count("\n") > 14 else ""))


def cmd_scenarios(_args) -> int:
    for s in SCENARIOS.values():
        print(f"{s.id:14s} root={s.root_cause!s:12s} tasks={','.join(s.tasks):32s} {s.title}")
        print(f"{'':14s} AIOpsLab analogue: {s.aiopslab_analogue}")
    return 0


def _make_router(config: str, episode: Episode):
    from opspilot.bench import _router
    from opspilot.llm import make_tier_clients
    cache = {}

    def clients():
        cache.setdefault("c", make_tier_clients())
        return cache["c"]
    return _router(config, episode, clients)


def _env_for(backend: str, episode: Episode):
    if backend == "kind":
        from opspilot.live import KindEnv, ensure_kind_cluster
        ensure_kind_cluster()
        return KindEnv(episode)
    return None


def cmd_run(args) -> int:
    from opspilot.runner import run_episode
    sc = SCENARIOS[args.scenario]
    if args.task not in sc.tasks:
        print(f"scenario {sc.id} supports tasks: {sc.tasks}")
        return 2
    ep = Episode(sc, args.task)
    router, verify = _make_router(args.config, ep)
    env = _env_for(args.backend, ep)
    print(f"Episode {ep.id} | config={args.config} | backend={args.backend}")
    res = run_episode(ep, router, args.config, max_steps=args.max_steps, verify_before_submit=verify,
                      trace_dir=Path(args.out) / "traces", on_step=_print_step, env=env)
    print(f"\n{'SUCCESS' if res.success else 'FAIL'}: {res.detail} {res.error}")
    print(f"steps={res.steps} small_tokens={res.small_tokens} large_tokens={res.large_tokens} "
          f"escalated={res.escalated} ({res.escalation_reason}) blocked={res.blocked_actions}")
    return 0 if res.success else 1


def cmd_bench(args) -> int:
    from opspilot.bench import run_bench
    from opspilot.llm import default_models
    from opspilot.report import build_report
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.out) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    small, large = default_models()
    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    if configs == ["oracle"]:
        small = large = "scripted expert (no LLM)"
    meta = {"run": run_id, "backend": args.backend, "small model": small, "large model": large,
            "max steps": args.max_steps, "python": platform.python_version()}
    meta_f = run_dir / "meta.json"
    if not meta_f.exists():
        meta_f.write_text(json.dumps(meta, indent=2))
    env_factory = None
    if args.backend == "kind":
        from opspilot.live import KindEnv, ensure_kind_cluster
        ensure_kind_cluster()
        env_factory = KindEnv
    run_bench(run_dir, configs, args.scenarios.split(",") if args.scenarios else None,
              args.tasks.split(",") if args.tasks else None, max_steps=args.max_steps,
              pause_s=args.pause, env_factory=env_factory)
    out, md = build_report(run_dir)
    print("\n" + md + f"\n\nReport: {out}")
    return 0


def cmd_report(args) -> int:
    from opspilot.report import build_report
    out, md = build_report(Path(args.out) / args.run_id)
    print(md + f"\n\nReport: {out}")
    return 0


def cmd_shell(args) -> int:
    """Be the agent yourself: a guard-railed kubectl prompt against a faulty simulated cluster."""
    from opspilot.executor import run_pipeline
    from opspilot.guardrails import GuardrailPolicy
    from opspilot.runner import SimEnv
    sc = SCENARIOS[args.scenario]
    env = SimEnv(Episode(sc, "mitigation" if "mitigation" in sc.tasks else "detection"))
    guard = GuardrailPolicy(env.cluster.namespace, allow_writes=True)
    print(f"Simulated cluster with fault '{sc.id}' injected (namespace {env.cluster.namespace}).")
    print("Type kubectl commands; 'health' shows the oracle, 'exit' quits.")
    while True:
        try:
            line = input("opspilot$ ").strip()
        except EOFError:
            break
        if line in ("exit", "quit"):
            break
        if line == "health":
            print(json.dumps(env.cluster.health(), indent=2))
            continue
        d = guard.check(line)
        if not d.allowed:
            print(f"BLOCKED: {d.reason}")
            continue
        print(run_pipeline(env.backend, d.pipeline)[1])
    return 0


def cmd_models(_args) -> int:
    from opspilot.llm import ChatClient, default_models
    small, large = default_models()
    c = ChatClient(small)
    models = c.list_models()
    print(f"{len(models)} models available on {c.provider}:")
    for m in models:
        tag = " <- small" if m == small else " <- large" if m == large else ""
        print(f"  {m}{tag}")
    t0 = time.perf_counter()
    r = c.chat([{"role": "user", "content": "Reply with the single word: ready"}])
    print(f"\nping {small}: {r.text.strip()!r} in {time.perf_counter() - t0:.1f}s ({r.total_tokens} tokens)")
    return 0


def cmd_manifest(args) -> int:
    from opspilot.live import manifest_yaml
    sys.stdout.write(manifest_yaml())
    return 0


def cmd_diagnose(args) -> int:
    """Read-only triage of a LIVE cluster namespace (current kube-context)."""
    from opspilot.actions import parse_action
    from opspilot.agent import OpsPilotAgent
    from opspilot.executor import LiveBackend, run_pipeline, truncate
    from opspilot.guardrails import GuardrailPolicy
    from opspilot.llm import make_tier_clients
    from opspilot.prompts import system_prompt, task_prompt
    from opspilot.router import CascadeRouter
    small, large = make_tier_clients()
    router = CascadeRouter(small, large, mode=args.config)
    guard = GuardrailPolicy(args.namespace, allow_writes=False)
    agent = OpsPilotAgent(router, system_prompt(args.namespace, False, args.max_steps),
                          task_prompt(args.app, args.namespace, args.description, "localization")
                          .replace("The application has a fault.", "Users report errors."))
    backend = LiveBackend()
    for step in range(args.max_steps):
        out = agent.act()
        a = parse_action(out.text)
        if a.kind == "submit":
            print(f"\nOpsPilot verdict: likely root cause -> {a.arg}")
            return 0
        if a.kind == "invalid":
            agent.observe(a.error)
            continue
        d = guard.check(a.command)
        obs = f"BLOCKED: {d.reason}" if not d.allowed else truncate(run_pipeline(backend, d.pipeline)[1])
        print(f"[{step + 1:02d}] {out.tier:5s} {a.command}")
        agent.observe(obs)
    print("No verdict within the step budget.")
    return 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(prog="opspilot", description="Guard-railed, cost-aware SRE agent for Kubernetes.")
    sub = p.add_subparsers(dest="cmd", required=True)
    configs = "small, large, cascade, cascade-noverify, oracle"

    sub.add_parser("scenarios", help="list fault scenarios").set_defaults(fn=cmd_scenarios)

    r = sub.add_parser("run", help="run one episode with a live trace")
    r.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    r.add_argument("--task", default="mitigation", choices=TASKS)
    r.add_argument("--config", default="cascade", help=configs)
    r.add_argument("--backend", default="sim", choices=["sim", "kind"])
    r.add_argument("--max-steps", type=int, default=15)
    r.add_argument("--out", default="results/adhoc")
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("bench", help="run the benchmark and build the report")
    b.add_argument("--configs", default="small,cascade,large", help=configs)
    b.add_argument("--scenarios", default="", help="comma-separated subset")
    b.add_argument("--tasks", default="", help="comma-separated subset")
    b.add_argument("--backend", default="sim", choices=["sim", "kind"])
    b.add_argument("--run-id", default="")
    b.add_argument("--max-steps", type=int, default=15)
    b.add_argument("--pause", type=float, default=0.0, help="seconds to sleep between episodes (rate limits)")
    b.add_argument("--out", default="results")
    b.set_defaults(fn=cmd_bench)

    rep = sub.add_parser("report", help="rebuild the report for a run")
    rep.add_argument("run_id")
    rep.add_argument("--out", default="results")
    rep.set_defaults(fn=cmd_report)

    s = sub.add_parser("shell", help="be the agent: guard-railed kubectl on a faulty simulated cluster")
    s.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    s.set_defaults(fn=cmd_shell)

    sub.add_parser("models", help="verify your API key and list free models").set_defaults(fn=cmd_models)
    sub.add_parser("manifest", help="print the slim hotel-reservation manifests").set_defaults(fn=cmd_manifest)

    d = sub.add_parser("diagnose", help="read-only root-cause triage of a live namespace")
    d.add_argument("--namespace", required=True)
    d.add_argument("--app", default="application")
    d.add_argument("--description", default="A microservice application running on Kubernetes.")
    d.add_argument("--config", default="cascade", choices=["small", "large", "cascade"])
    d.add_argument("--max-steps", type=int, default=12)
    d.set_defaults(fn=cmd_diagnose)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # friendly top-level error
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

