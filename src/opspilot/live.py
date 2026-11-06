"""Real-cluster backend: deploys a slim hotel-reservation app on any Kubernetes cluster
(tested with kind) and injects the same faults as the simulator.

Each service is a ~5 MB busybox container that
  * refuses to start if a required env var is missing (-> real CrashLoopBackOff),
  * serves HTTP on its containerPort (readiness probe = TCP check on that port),
  * calls each dependency every few seconds and logs real wget/DNS errors,
  * reports itself unhealthy (HTTP 404) while any dependency is failing, so faults
    propagate up the call graph exactly like gRPC errors do in the real app.
"""

from __future__ import annotations

import json
import subprocess
import time

import yaml

from opspilot.executor import LiveBackend
from opspilot.prompts import task_prompt
from opspilot.runner import score_detection_localization, score_mitigation
from opspilot.scenarios import Episode
from opspilot.sim.app import APP_DESCRIPTION, NAMESPACE, SERVICES, ServiceSpec

BASE_IMAGE = "busybox:1.36.1"
KIND_CLUSTER = "opspilot"


def _script(spec: ServiceSpec) -> str:
    checks = "".join(
        f'eval "v=\\${var}"; [ -n "$v" ] || {{ echo "level=fatal msg=\\"invalid configuration\\" '
        f'error=\\"required environment variable {var} is not set\\""; exit 2; }}\n'
        for var in spec.required_env)
    starts = "".join(f"echo '{line}'\n" for line in spec.startup_logs)
    deps = " ".join(f"{d}:{SERVICES[d].port}" for d in spec.depends_on)
    return f"""{checks}{starts}mkdir -p /www && echo ok > /www/index.html
httpd -f -p {spec.port} -h /www &
while true; do
  ok=1
  for t in {deps or '""'}; do
    [ -z "$t" ] && continue
    if ! out=$(wget -q -T 2 -O /dev/null "http://$t/" 2>&1); then
      ok=0; echo "level=error msg=\\"call failed: $out\\" target=$t"
    fi
  done
  if [ $ok = 1 ]; then echo ok > /www/index.html; else rm -f /www/index.html; fi
  sleep 3
done
"""


def manifests(namespace: str = NAMESPACE) -> list[dict]:
    docs: list[dict] = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}]
    for spec in SERVICES.values():
        container = {
            "name": spec.name,
            "image": BASE_IMAGE,
            "command": ["sh", "-c", _script(spec)],
            "ports": [{"containerPort": spec.port, "protocol": "TCP"}],
            "readinessProbe": {"tcpSocket": {"port": spec.port}, "periodSeconds": 2,
                               "initialDelaySeconds": 1},
            "resources": {"requests": {"cpu": "5m", "memory": "8Mi"},
                          "limits": {"cpu": "100m", "memory": "32Mi"}},
        }
        if spec.env:
            container["env"] = [{"name": k, "value": v} for k, v in spec.env.items()]
        docs.append({
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": spec.name, "namespace": namespace, "labels": {"app": spec.name}},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": spec.name}},
                     "template": {"metadata": {"labels": {"app": spec.name, "tier": spec.kind}},
                                  "spec": {"terminationGracePeriodSeconds": 1,
                                           "containers": [container]}}},
        })
        docs.append({
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": spec.name, "namespace": namespace, "labels": {"app": spec.name}},
            "spec": {"selector": {"app": spec.name},
                     "ports": [{"name": "grpc" if spec.kind == "app" else "mongo", "port": spec.port,
                                "targetPort": spec.port, "protocol": "TCP"}]},
        })
    return docs


def manifest_yaml(namespace: str = NAMESPACE) -> str:
    return yaml.safe_dump_all(manifests(namespace), sort_keys=False)


def _kubectl(*args: str, stdin: str | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], input=stdin, capture_output=True, text=True,
                          timeout=timeout, check=False)


def deploy_app(namespace: str = NAMESPACE, log=print) -> None:
    _kubectl("delete", "namespace", namespace, "--ignore-not-found", "--wait=true", timeout=240)
    p = _kubectl("apply", "-f", "-", stdin=manifest_yaml(namespace))
    if p.returncode:
        raise RuntimeError(f"kubectl apply failed: {p.stderr}")
    p = _kubectl("wait", "--for=condition=available", "deployment", "--all", "-n", namespace,
                 "--timeout=240s", timeout=260)
    if p.returncode:
        raise RuntimeError(f"app did not become available: {p.stderr or p.stdout}")
    time.sleep(8)  # let dependency loops complete a cycle
    h = live_health(namespace)
    if not h["healthy"]:
        raise RuntimeError(f"baseline app unhealthy: {h['problems']}")
    log(f"[kind] hotel-reservation deployed and healthy in namespace {namespace}")


def live_health(namespace: str = NAMESPACE) -> dict:
    p = _kubectl("get", "deployments,services,endpoints", "-n", namespace, "-o", "json")
    if p.returncode:
        return {"healthy": False, "problems": [p.stderr.strip()], "objects": (set(), set())}
    items = json.loads(p.stdout)["items"]
    deps = {i["metadata"]["name"]: i for i in items if i["kind"] == "Deployment"}
    svcs = {i["metadata"]["name"]: i for i in items if i["kind"] == "Service"}
    eps = {i["metadata"]["name"]: i for i in items if i["kind"] == "Endpoints"}
    problems = []
    for name in SERVICES:
        d = deps.get(name)
        if d is None:
            problems.append(f"deployment/{name} is missing")
            continue
        want = int(d["spec"].get("replicas") or 0)
        ready = int(d.get("status", {}).get("readyReplicas") or 0)
        updated = int(d.get("status", {}).get("updatedReplicas") or 0)
        total = int(d.get("status", {}).get("replicas") or 0)
        if want < 1:
            problems.append(f"deployment/{name} has 0 replicas")
        elif ready < want or updated < want or total > want:
            problems.append(f"deployment/{name} not fully rolled out ({ready}/{want} ready)")
        cports = {p["containerPort"] for c in d["spec"]["template"]["spec"]["containers"]
                  for p in c.get("ports", [])}
        if name not in svcs:
            problems.append(f"service/{name} is missing")
            continue
        subsets = (eps.get(name) or {}).get("subsets") or []
        addrs = [a for s in subsets for a in s.get("addresses", [])]
        ports = {p["port"] for s in subsets for p in s.get("ports", [])}
        if not addrs:
            problems.append(f"service/{name} has no ready endpoints")
        elif not ports & cports:
            problems.append(f"service/{name} targets port(s) {sorted(ports)}, container listens on {sorted(cports)}")
    return {"healthy": not problems, "problems": problems, "objects": (set(deps), set(svcs))}


class KindEnv:
    """Same interface as ``SimEnv`` but backed by a real cluster via kubectl."""

    def __init__(self, episode: Episode, namespace: str = NAMESPACE, log=print,
                 settle_s: int = 25) -> None:
        self.episode = episode
        self.namespace = namespace
        deploy_app(namespace, log=log)
        self.backend = LiveBackend()
        for argv in episode.scenario.inject_argv:
            rc, out = self.backend.kubectl(argv[1:])
            if rc:
                raise RuntimeError(f"fault injection failed: {out}")
            log(f"[kind] injected: {' '.join(argv)} -> {out}")
        if episode.scenario.inject:
            time.sleep(settle_s)  # let symptoms appear: crash loops, image pull back-off, error logs
        self.initial_objects = live_health(namespace)["objects"]

    def prompt(self) -> str:
        return task_prompt("Hotel Reservation", self.namespace, APP_DESCRIPTION, self.episode.task)

    def evaluate(self, answer, has_answer: bool) -> tuple[bool, str]:
        task = self.episode.task
        if task in ("detection", "localization"):
            return score_detection_localization(self.episode.scenario, task, answer, has_answer)
        deadline = time.time() + 90  # give the agent's fix time to roll out
        h = live_health(self.namespace)
        while not h["healthy"] and time.time() < deadline:
            time.sleep(5)
            h = live_health(self.namespace)
        return score_mitigation(h, self.initial_objects, h["objects"])


def ensure_kind_cluster(log=print) -> None:
    out = subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True).stdout.split()
    if KIND_CLUSTER not in out:
        log(f"[kind] creating cluster '{KIND_CLUSTER}' (one-time, ~1 minute)...")
        p = subprocess.run(["kind", "create", "cluster", "--name", KIND_CLUSTER, "--wait", "120s"],
                           capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError(p.stderr)
    subprocess.run(["kubectl", "config", "use-context", f"kind-{KIND_CLUSTER}"],
                   capture_output=True, text=True, check=True)
