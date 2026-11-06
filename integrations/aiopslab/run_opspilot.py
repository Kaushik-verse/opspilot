"""Run OpsPilot on Microsoft AIOpsLab problems.

Usage (from inside an AIOpsLab checkout, with its cluster set up per its README):

    pip install -e /path/to/opspilot
    python /path/to/opspilot/integrations/aiopslab/run_opspilot.py \
        --problems misconfig_app_hotel_res-detection-1,k8s_target_port-misconfig-mitigation-1 \
        --config cascade

Results are written by AIOpsLab under data/results/, plus opspilot_aiopslab_results.jsonl here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    from aiopslab.orchestrator import Orchestrator  # provided by the AIOpsLab checkout

    from opspilot.integrations.aiopslab import OpsPilotAIOpsLabAgent
    from opspilot.llm import make_tier_clients
    from opspilot.router import CascadeRouter

    p = argparse.ArgumentParser()
    p.add_argument("--problems", required=True, help="comma-separated AIOpsLab problem ids")
    p.add_argument("--config", default="cascade", choices=["small", "large", "cascade"])
    p.add_argument("--max-steps", type=int, default=20)
    args = p.parse_args()

    small, large = make_tier_clients()
    out = Path("opspilot_aiopslab_results.jsonl")
    for pid in [x.strip() for x in args.problems.split(",") if x.strip()]:
        router = CascadeRouter(small, large, mode=args.config)
        agent = OpsPilotAIOpsLabAgent(router)
        orch = Orchestrator()
        orch.register_agent(agent, name=f"opspilot-{args.config}")
        desc, instructions, apis = orch.init_problem(pid)
        agent.init_context(desc, instructions, apis, problem_id=pid)
        result = asyncio.run(orch.start_problem(max_steps=args.max_steps))
        row = {"problem": pid, "config": args.config, "escalated": router.escalated,
               **agent.stats, "results": (result or {}).get("results")}
        with out.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        print(json.dumps(row, indent=2, default=str))


if __name__ == "__main__":
    main()
