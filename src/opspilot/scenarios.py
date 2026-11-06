"""Fault scenarios, mirroring fault families from Microsoft AIOpsLab's problem registry.

Each fault is injected with ordinary kubectl mutations, so the agent sees exactly the
symptoms a real cluster would show (pending pods, crash loops, empty endpoints, error logs).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from opspilot.sim.app import NAMESPACE

NS = NAMESPACE
TASKS = ("detection", "localization", "mitigation")


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    root_cause: str | None  # faulty service, or None for a healthy cluster
    fault_type: str
    inject: tuple[str, ...]
    aiopslab_analogue: str
    tasks: tuple[str, ...] = TASKS
    notes: str = ""

    @property
    def inject_argv(self) -> list[list[str]]:
        return [shlex.split(c) for c in self.inject]


SCENARIOS: dict[str, Scenario] = {
    s.id: s
    for s in [
        Scenario(
            id="scale_zero",
            title="Deployment scaled to zero replicas",
            root_cause="profile",
            fault_type="misconfiguration",
            inject=(f"kubectl scale deployment profile --replicas=0 -n {NS}",),
            aiopslab_analogue="scale_pod_zero_social_net-*",
            notes="No pod is broken; the profile pods simply do not exist, so frontend calls fail.",
        ),
        Scenario(
            id="target_port",
            title="Service targetPort misconfigured",
            root_cause="geo",
            fault_type="misconfiguration",
            inject=(
                f"kubectl patch service geo -n {NS} -p "
                '\'{"spec":{"ports":[{"port":8083,"targetPort":9999}]}}\'',
            ),
            aiopslab_analogue="k8s_target_port-misconfig-*",
            notes="Every pod is Running and Ready; only endpoints and caller logs reveal the fault, "
                  "and errors surface first in search/frontend (two hops away from geo).",
        ),
        Scenario(
            id="bad_image",
            title="Rollout of a non-existent image tag",
            root_cause="rate",
            fault_type="bad_deployment",
            inject=(f"kubectl set image deployment/rate rate=opspilot/hotel-rate:1.5.0-rc3 -n {NS}",),
            aiopslab_analogue="wrong_bin_usage-* / misconfig_app_hotel_res-*",
        ),
        Scenario(
            id="node_selector",
            title="Pods pinned to a node that does not exist",
            root_cause="reservation",
            fault_type="scheduling",
            inject=(
                f"kubectl patch deployment reservation -n {NS} -p "
                '\'{"spec":{"template":{"spec":{"nodeSelector":{"kubernetes.io/hostname":"gpu-node-7"}}}}}\'',
            ),
            aiopslab_analogue="assign_to_non_existent_node_social_net-*",
        ),
        Scenario(
            id="missing_env",
            title="Required configuration removed (crash loop)",
            root_cause="user",
            fault_type="misconfiguration",
            inject=(f"kubectl set env deployment/user DB_URL- -n {NS}",),
            aiopslab_analogue="misconfig_app_hotel_res-* / user_unregistered_mongodb-*",
        ),
        Scenario(
            id="healthy",
            title="No fault (false-positive check)",
            root_cause=None,
            fault_type="none",
            inject=(),
            aiopslab_analogue="(negative control)",
            tasks=("detection",),
            notes="The agent must answer 'No' despite benign warning logs.",
        ),
    ]
}


@dataclass(frozen=True)
class Episode:
    scenario: Scenario
    task: str

    @property
    def id(self) -> str:
        return f"{self.scenario.id}-{self.task}"


def all_episodes(scenario_ids: list[str] | None = None, tasks: list[str] | None = None) -> list[Episode]:
    eps = []
    for sid, sc in SCENARIOS.items():
        if scenario_ids and sid not in scenario_ids:
            continue
        for t in sc.tasks:
            if tasks and t not in tasks:
                continue
            eps.append(Episode(sc, t))
    return eps
