"""Benchmark: every (config x scenario x task), resumable, with per-episode JSON traces."""

from __future__ import annotations

import json
import time
from pathlib import Path

from opspilot.oracle import ScriptedLLM, oracle_script
from opspilot.router import CascadeRouter
from opspilot.runner import EpisodeResult, run_episode
from opspilot.scenarios import Episode, all_episodes

CONFIGS = {
    # name: (router mode, verify_before_submit)
    "small": ("small", True),
    "large": ("large", True),
    "cascade": ("cascade", True),
    "cascade-noverify": ("cascade", False),
    "oracle": ("oracle", True),
}


def _router(config: str, episode: Episode, clients) -> tuple[CascadeRouter, bool]:
    mode, verify = CONFIGS[config]
    if mode == "oracle":
        llm = ScriptedLLM(oracle_script(episode.scenario.id, episode.task), model="oracle")
        return CascadeRouter(llm, llm, mode="small"), verify
    small, large = clients()
    return CascadeRouter(small, large, mode=mode), verify


def load_done(results_file: Path) -> set[tuple[str, str]]:
    done = set()
    if results_file.exists():
        for line in results_file.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if not r.get("error"):
                    done.add((r["config"], r["episode"]))
    return done


def run_bench(run_dir: Path, configs: list[str], scenarios: list[str] | None = None,
              tasks: list[str] | None = None, max_steps: int = 15, pause_s: float = 0.0,
              log=print, env_factory=None) -> list[dict]:
    for c in configs:
        if c not in CONFIGS:
            raise ValueError(f"unknown config {c!r}; choose from {sorted(CONFIGS)}")
    run_dir.mkdir(parents=True, exist_ok=True)
    results_file = run_dir / "results.jsonl"
    done = load_done(results_file)
    cache: dict = {}

    def clients():
        if "c" not in cache:
            from opspilot.llm import make_tier_clients
            cache["c"] = make_tier_clients()
        return cache["c"]

    episodes = all_episodes(scenarios, tasks)
    rows = []
    for config in configs:
        for ep in episodes:
            if (config, ep.id) in done:
                log(f"skip  {config:17s} {ep.id} (already done)")
                continue
            router, verify = _router(config, ep, clients)
            try:
                env = env_factory(ep) if env_factory else None
            except Exception as e:  # cluster problems are recorded, not fatal
                log(f"ERR   {config:17s} {ep.id}: environment setup failed: {e}")
                continue
            res: EpisodeResult = run_episode(ep, router, config, max_steps=max_steps,
                                             verify_before_submit=verify,
                                             trace_dir=run_dir / "traces", env=env)
            row = res.summary()
            with open(results_file, "a") as f:
                f.write(json.dumps(row, default=str) + "\n")
            rows.append(row)
            status = "PASS" if res.success else ("ERR " if res.error else "FAIL")
            log(f"{status}  {config:17s} {ep.id:28s} steps={res.steps:2d} tokens={res.total_tokens:6d} "
                f"{'(escalated: ' + res.escalation_reason + ')' if res.escalated else ''} {res.error[:120]}")
            if pause_s and config != "oracle":
                time.sleep(pause_s)
    return rows
